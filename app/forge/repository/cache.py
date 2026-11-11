"""Cache storage and lifecycle management for repository indexing."""

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Dict, List, Optional, Set, Tuple, Union
import uuid

from forge.repository.cache_schemas import (
    INDEX_SCHEMA_VERSION,
    INDEXER_PARSER_VERSION,
    CacheEntry,
    CacheStats,
    FileIndexRecord,
)
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path


class IndexCacheManager:
    """Manages disk-backed caching of parsed file index records with atomic writes,

    corruption recovery, checksum verification, and LRU eviction.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_entries: int = 5000,
        max_bytes: int = 50_000_000,  # 50 MB default bound
        schema_version: str = INDEX_SCHEMA_VERSION,
        parser_version: str = INDEXER_PARSER_VERSION,
    ):
        self.cache_dir = Path(cache_dir).resolve()
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.schema_version = schema_version
        self.parser_version = parser_version

        self._lock = threading.RLock()
        self.stats = CacheStats()
        # Maps entry filename -> (rel_path, last_accessed_at, size_bytes)
        self._manifest: Optional[Dict[str, Tuple[Optional[str], float, int]]] = None
        self._total_bytes: int = 0
        self._ensure_cache_dir()
        self.cleanup_interrupted_writes()

    def _ensure_cache_dir(self) -> None:
        """Create cache directory if it does not exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_entry_path(self, rel_path: str) -> Path:
        """Deterministic cache file path derived from SHA-256 of relative path."""
        path_hash = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()
        return self.cache_dir / f"entry_{path_hash}.json"

    def _ensure_manifest_loaded(self) -> Dict[str, Tuple[Optional[str], float, int]]:
        """Populate in-memory cache manifest from disk once on first demand."""
        if self._manifest is not None:
            return self._manifest

        manifest: Dict[str, Tuple[Optional[str], float, int]] = {}
        total_bytes = 0
        if self.cache_dir.exists():
            try:
                with os.scandir(self.cache_dir) as it:
                    for entry in it:
                        name = entry.name
                        if not (name.startswith("entry_") and name.endswith(".json")):
                            continue
                        try:
                            if not entry.is_file():
                                continue
                            st = entry.stat()
                            size = st.st_size
                            accessed = st.st_mtime
                            rel_path: Optional[str] = None
                            try:
                                raw = Path(entry.path).read_text(encoding="utf-8")
                                data = json.loads(raw)
                                rel_path = data.get("file_path")
                                accessed = float(data.get("last_accessed_at", accessed))
                            except Exception:
                                pass
                            manifest[name] = (rel_path, accessed, size)
                            total_bytes += size
                        except OSError:
                            continue
            except OSError:
                pass

        self._manifest = manifest
        self._total_bytes = total_bytes
        self.stats.total_entries = len(manifest)
        self.stats.total_bytes = total_bytes
        return manifest

    def _manifest_remove(self, filename: str) -> None:
        if self._manifest is not None and filename in self._manifest:
            _, _, old_size = self._manifest.pop(filename)
            self._total_bytes = max(0, self._total_bytes - old_size)
            self.stats.total_entries = len(self._manifest)
            self.stats.total_bytes = self._total_bytes

    @staticmethod
    def compute_content_hash(content: Union[str, bytes]) -> str:
        """Compute SHA-256 hash of text or binary content."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def compute_file_content_hash(file_path: Path, repo_root: Path) -> Optional[str]:
        """Compute SHA-256 hash of file content after strictly validating containment and protection."""
        try:
            resolved = assert_contained_path(file_path, repo_root)
        except Exception:
            return None

        if is_protected_path(resolved, base_dir=repo_root):
            return None

        try:
            raw_bytes = resolved.read_bytes()
            return hashlib.sha256(raw_bytes).hexdigest()
        except (OSError, IOError):
            return None

    def cleanup_interrupted_writes(self) -> int:
        """Scan cache directory for dangling temporary files (.tmp.*) from aborted writes and delete them."""
        cleaned = 0
        if not self.cache_dir.exists():
            return 0

        try:
            with os.scandir(self.cache_dir) as it:
                for entry in it:
                    name = entry.name
                    if (name.startswith(".tmp.") or name.endswith(".tmp")) and entry.is_file():
                        try:
                            os.unlink(entry.path)
                            cleaned += 1
                        except OSError:
                            pass
        except OSError:
            pass
        return cleaned

    def get(self, rel_path: str, expected_hash: str) -> Optional[FileIndexRecord]:
        """Retrieve a cached index record if it exists, is valid, uncorrupted, and matches expected hash."""
        entry_file = self._get_entry_path(rel_path)
        if not entry_file.exists():
            self.stats.cache_misses += 1
            return None

        try:
            raw_data = entry_file.read_text(encoding="utf-8")
            entry_dict = json.loads(raw_data)
        except Exception:
            # Corrupted JSON format
            self._handle_corruption(entry_file, rel_path)
            return None

        # Verify schema and parser version
        if (
            entry_dict.get("schema_version") != self.schema_version
            or entry_dict.get("parser_version") != self.parser_version
        ):
            self.stats.invalidations += 1
            self.stats.cache_misses += 1
            try:
                entry_file.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                self._manifest_remove(entry_file.name)
            return None

        # Verify content hash match
        if entry_dict.get("content_hash") != expected_hash or entry_dict.get("file_path") != rel_path:
            self.stats.invalidations += 1
            self.stats.cache_misses += 1
            return None

        # Verify payload checksum integrity
        payload_dict = entry_dict.get("payload")
        if not isinstance(payload_dict, dict):
            self._handle_corruption(entry_file, rel_path)
            return None

        recorded_checksum = entry_dict.get("checksum")
        payload_canonical_str = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
        actual_checksum = hashlib.sha256(payload_canonical_str.encode("utf-8")).hexdigest()

        if recorded_checksum != actual_checksum:
            self._handle_corruption(entry_file, rel_path)
            return None

        # Validate through Pydantic
        try:
            record = FileIndexRecord.model_validate(payload_dict)
        except Exception:
            self._handle_corruption(entry_file, rel_path)
            return None

        now = time.time()
        with self._lock:
            if self._manifest is not None and entry_file.name in self._manifest:
                fp, _, sz = self._manifest[entry_file.name]
                self._manifest[entry_file.name] = (fp, now, sz)

        self.stats.cache_hits += 1
        return record

    def put(self, rel_path: str, record: FileIndexRecord) -> None:
        """Persist a FileIndexRecord atomically with checksum validation and bound checks."""
        self._ensure_cache_dir()
        entry_file = self._get_entry_path(rel_path)

        # Prepare payload and checksum in a single dump
        payload_dict = record.model_dump(mode="json")
        payload_canonical_str = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(payload_canonical_str.encode("utf-8")).hexdigest()

        now = time.time()
        entry_dict = {
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "file_path": rel_path,
            "content_hash": record.content_hash,
            "created_at": now,
            "last_accessed_at": now,
            "payload": payload_dict,
            "checksum": checksum,
        }
        entry_bytes = json.dumps(entry_dict, separators=(",", ":")).encode("utf-8")
        temp_file = self.cache_dir / f".tmp.{os.getpid()}.{uuid.uuid4().hex}"

        try:
            with open(temp_file, "wb") as f:
                f.write(entry_bytes)
            # Atomic replace guarantees readers see either old or complete new entry
            os.replace(temp_file, entry_file)
        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise e

        new_size = len(entry_bytes)
        with self._lock:
            manifest = self._ensure_manifest_loaded()
            old_entry = manifest.get(entry_file.name)
            old_size = old_entry[2] if old_entry is not None else 0
            manifest[entry_file.name] = (rel_path, now, new_size)
            self._total_bytes = max(0, self._total_bytes - old_size + new_size)
            self.stats.total_entries = len(manifest)
            self.stats.total_bytes = self._total_bytes

            if len(manifest) > self.max_entries or self._total_bytes > self.max_bytes:
                self.evict_if_needed()

    def _handle_corruption(self, entry_file: Path, rel_path: str) -> None:
        """Safely record and recover from cache entry corruption."""
        self.stats.corrupted_entries_recovered += 1
        self.stats.cache_misses += 1
        try:
            entry_file.unlink(missing_ok=True)
        except OSError:
            pass
        with self._lock:
            self._manifest_remove(entry_file.name)

    def invalidate(self, rel_path: str) -> bool:
        """Invalidate cache entry for a specific relative file path."""
        entry_file = self._get_entry_path(rel_path)
        with self._lock:
            self._manifest_remove(entry_file.name)
        if entry_file.exists():
            try:
                entry_file.unlink(missing_ok=True)
                self.stats.invalidations += 1
                return True
            except OSError:
                pass
        return False

    def prune_obsolete(self, current_valid_files: Set[str]) -> int:
        """Remove cached entries for files that no longer exist in the repository."""
        pruned_count = 0
        if not self.cache_dir.exists():
            return 0

        with self._lock:
            manifest = self._ensure_manifest_loaded()
            to_remove: List[str] = []
            for fname, (rel_path, _, _) in manifest.items():
                if rel_path is None:
                    to_remove.append(fname)
                    self.stats.corrupted_entries_recovered += 1
                elif rel_path not in current_valid_files:
                    to_remove.append(fname)
                    self.stats.invalidations += 1

            for fname in to_remove:
                try:
                    (self.cache_dir / fname).unlink(missing_ok=True)
                except OSError:
                    pass
                self._manifest_remove(fname)
                pruned_count += 1

        return pruned_count

    def evict_if_needed(self) -> int:
        """Evict oldest entries according to LRU policy if entry count or byte size bounds are exceeded."""
        if not self.cache_dir.exists():
            return 0

        with self._lock:
            manifest = self._ensure_manifest_loaded()
            if len(manifest) <= self.max_entries and self._total_bytes <= self.max_bytes:
                self.stats.total_entries = len(manifest)
                self.stats.total_bytes = self._total_bytes
                return 0

            entries = sorted(
                ((fname, accessed, sz) for fname, (_, accessed, sz) in manifest.items()),
                key=lambda item: item[1],
            )

            evicted = 0
            remaining_count = len(entries)
            total_bytes = self._total_bytes
            for fname, _, entry_size in entries:
                if remaining_count <= self.max_entries and total_bytes <= self.max_bytes:
                    break
                try:
                    (self.cache_dir / fname).unlink(missing_ok=True)
                    evicted += 1
                    remaining_count -= 1
                    total_bytes = max(0, total_bytes - entry_size)
                    manifest.pop(fname, None)
                    self.stats.evicted_entries += 1
                except OSError:
                    continue

            self._total_bytes = total_bytes
            self.stats.total_entries = len(manifest)
            self.stats.total_bytes = total_bytes
            return evicted

    def clear(self) -> int:
        """Clear all cache entries in cache directory."""
        cleared = 0
        if not self.cache_dir.exists():
            return 0

        with self._lock:
            with os.scandir(self.cache_dir) as it:
                for entry in it:
                    if entry.is_file():
                        try:
                            os.unlink(entry.path)
                            cleared += 1
                        except OSError:
                            pass
            self._manifest = {}
            self._total_bytes = 0
            self.stats.total_entries = 0
            self.stats.total_bytes = 0
        return cleared
