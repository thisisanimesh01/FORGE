"""Pydantic schemas and metadata contracts for Phase 11 Milestone 5 Incremental Indexing and Caching."""

from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from forge.repository.graph_schemas import CallSiteRecord, GraphNode, ImportRecord
from forge.repository.indexer import SymbolRecord

INDEX_SCHEMA_VERSION = "1.0.0"
INDEXER_PARSER_VERSION = "1.0.0"


class FileIndexRecord(BaseModel):
    """Cached index representation of a single parsed Python source file."""
    file_path: str = Field(..., description="Workspace-relative path to the source file")
    content_hash: str = Field(..., description="SHA-256 hash of the file content")
    size_bytes: int = Field(..., ge=0, description="File size in bytes")
    mtime_ns: int = Field(0, ge=0, description="File modification timestamp in nanoseconds")
    module_name: str = Field(..., description="Canonical dotted module name")
    docstring: Optional[str] = Field(None, description="Module top-level docstring")
    total_lines: int = Field(0, ge=0, description="Total line count")
    symbols: List[SymbolRecord] = Field(default_factory=list, description="Extracted code symbols")
    nodes: List[GraphNode] = Field(default_factory=list, description="Extracted graph nodes")
    call_sites: List[CallSiteRecord] = Field(default_factory=list, description="Extracted call sites")
    imports: List[ImportRecord] = Field(default_factory=list, description="Extracted import records")
    class_bases: Dict[str, List[str]] = Field(default_factory=dict, description="Class inheritance mappings")
    callable_local_vars: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Local variable names per callable node ID (for shadowing detection)",
    )


class CacheEntry(BaseModel):
    """Wrapper envelope for a persisted cache file on disk."""
    schema_version: str = Field(default=INDEX_SCHEMA_VERSION, description="Cache envelope schema version")
    parser_version: str = Field(default=INDEXER_PARSER_VERSION, description="AST parser extraction version")
    file_path: str = Field(..., description="Workspace-relative file path")
    content_hash: str = Field(..., description="SHA-256 hash of the indexed file content")
    created_at: float = Field(..., description="Epoch timestamp when entry was created")
    last_accessed_at: float = Field(..., description="Epoch timestamp when entry was last read")
    payload: FileIndexRecord = Field(..., description="The actual indexed file data")
    checksum: str = Field(..., description="SHA-256 checksum of payload JSON for corruption verification")


class CacheStats(BaseModel):
    """Telemetry and statistics for the indexing cache."""
    total_entries: int = 0
    total_bytes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    invalidations: int = 0
    corrupted_entries_recovered: int = 0
    evicted_entries: int = 0


class IncrementalUpdateSummary(BaseModel):
    """Audit summary of an incremental indexing operation."""
    total_files_scanned: int = 0
    unchanged_files: int = 0
    added_files: int = 0
    modified_files: int = 0
    deleted_files: int = 0
    renamed_files: int = 0
    invalidated_dependents: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    duration_seconds: float = 0.0
    rebuild_fallback_triggered: bool = False
    rebuild_fallback_reason: Optional[str] = None


class RepositoryIndexState(BaseModel):
    """Persisted snapshot of the repository state across indexing runs."""
    schema_version: str = Field(default=INDEX_SCHEMA_VERSION)
    parser_version: str = Field(default=INDEXER_PARSER_VERSION)
    repo_fingerprint: str = Field(..., description="Combined SHA-256 fingerprint of all indexed file hashes")
    file_hashes: Dict[str, str] = Field(default_factory=dict, description="Relative file path -> SHA-256 content hash")
    last_updated_at: float = Field(..., description="Epoch timestamp of last update")
