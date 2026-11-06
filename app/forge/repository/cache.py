"""Cache storage and lifecycle management for repository indexing."""

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Dict, List, Optional, Set, Union
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

        self.stats = CacheStats()
        self._ensure_cache_dir()
        self.cleanup_interrupted_writes()

    def _ensure_cache_dir(self) -> None:
        """Create cache directory if it does not exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_entry_path(self, rel_path: str) -> Path:
        """Deterministic cache file path derived from SHA-256 of relative path."""
        path_hash = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()
        return self.cache_dir / f"entry_{path_hash}.json"

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
            for item in self.cache_dir.iterdir():
                if item.is_file() and (item.name.startswith(".tmp.") or item.suffix == ".tmp"):
                    try:
                        item.unlink(missing_ok=True)
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

        # Update access time safely
        now = time.time()
        try:
            entry_dict["last_accessed_at"] = now
            # Write updated access time back periodically or keep in memory
        except Exception:
            pass

        self.stats.cache_hits += 1
        return record

    def put(self, rel_path: str, record: FileIndexRecord) -> None:
        """Persist a FileIndexRecord atomically with checksum validation and bound checks."""
        self._ensure_cache_dir()
        entry_file = self._get_entry_path(rel_path)

        # Prepare payload and checksum
        payload_dict = record.model_dump(mode="json")
        payload_canonical_str = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(payload_canonical_str.encode("utf-8")).hexdigest()

        now = time.time()
        entry = CacheEntry(
            schema_version=self.schema_version,
            parser_version=self.parser_version,
            file_path=rel_path,
            content_hash=record.content_hash,
            created_at=now,
            last_accessed_at=now,
            payload=record,
            checksum=checksum,
        )

        entry_json = entry.model_dump_json(indent=2)
        temp_file = self.cache_dir / f".tmp.{os.getpid()}.{uuid.uuid4().hex}"

        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(entry_json)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace
            os.replace(temp_file, entry_file)
        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise e

        # Check bounds and evict if needed
        self.evict_if_needed()

    def _handle_corruption(self, entry_file: Path, rel_path: str) -> None:
        """Safely record and recover from cache entry corruption."""
        self.stats.corrupted_entries_recovered += 1
        self.stats.cache_misses += 1
        try:
            entry_file.unlink(missing_ok=True)
        except OSError:
            pass

    def invalidate(self, rel_path: str) -> bool:
        """Invalidate cache entry for a specific relative file path."""
        entry_file = self._get_entry_path(rel_path)
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

        for item in self.cache_dir.iterdir():
            if not item.is_file() or not item.name.startswith("entry_") or not item.name.endswith(".json"):
                continue
            try:
                raw_data = item.read_text(encoding="utf-8")
                data = json.loads(raw_data)
                rel_path = data.get("file_path")
                if rel_path and rel_path not in current_valid_files:
                    item.unlink(missing_ok=True)
                    pruned_count += 1
                    self.stats.invalidations += 1
            except Exception:
                # Corrupted or unreadable file: remove it
                item.unlink(missing_ok=True)
                pruned_count += 1
                self.stats.corrupted_entries_recovered += 1
        return pruned_count

    def evict_if_needed(self) -> int:
        """Evict oldest entries according to LRU policy if entry count or byte size bounds are exceeded."""
        if not self.cache_dir.exists():
            return 0

        entries: List[Dict[str, Union[Path, float, int]]] = []
        total_bytes = 0

        for item in self.cache_dir.iterdir():
            if not item.is_file() or not item.name.startswith("entry_") or not item.name.endswith(".json"):
                continue
            try:
                stat = item.stat()
                total_bytes += stat.st_size
                # Read last_accessed_at if possible, fallback to st_mtime
                access_time = stat.st_mtime
                try:
                    data = json.loads(item.read_text(encoding="utf-8"))
                    access_time = float(data.get("last_accessed_at", access_time))
                except Exception:
                    pass
                entries.append({
                    "path": item,
                    "accessed": access_time,
                    "size": stat.st_size,
                })
            except OSError:
                continue

        self.stats.total_entries = len(entries)
        self.stats.total_bytes = total_bytes

        if len(entries) <= self.max_entries and total_bytes <= self.max_bytes:
            return 0

        # Sort entries by last_accessed_at ascending (oldest first)
        entries.sort(key=lambda e: float(e["accessed"]))

        evicted = 0
        for entry in entries:
            if len(entries) - evicted <= self.max_entries and total_bytes <= self.max_bytes:
                break
            try:
                entry_path = entry["path"]
                assert isinstance(entry_path, Path)
                size = int(entry["size"])
                entry_path.unlink(missing_ok=True)
                total_bytes -= size
                evicted += 1
                self.stats.evicted_entries += 1
            except OSError:
                pass

        self.stats.total_entries = len(entries) - evicted
        self.stats.total_bytes = max(0, total_bytes)
        return evicted

    def clear(self) -> int:
        """Clear all cache entries in cache directory."""
        cleared = 0
        if not self.cache_dir.exists():
            return 0

        for item in self.cache_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink(missing_ok=True)
                    cleared += 1
                except OSError:
                    pass
        self.stats.total_entries = 0
        self.stats.total_bytes = 0
        return cleared
