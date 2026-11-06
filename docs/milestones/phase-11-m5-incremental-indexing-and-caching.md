# Phase 11, Milestone 5: Incremental Indexing & Caching

## 1. Executive Summary & Objective

Phase 11 Milestone 5 implements a deterministic, versioned, corrupt-resistant incremental indexing and caching engine for FORGE. As software repositories scale, repeatedly parsing hundreds of abstract syntax trees (ASTs) on every inspection or repair run imposes unnecessary CPU and latency overhead.

Milestone 5 eliminates redundant work while maintaining FORGE's foundational invariants:
- **Audit-Only & Immutable**: Zero modification of target repository files or Git metadata.
- **Fail-Secure Static Analysis**: No dynamic execution or `importlib` evaluation of untrusted repository code.
- **Zero-Knowledge Boundaries**: Strict upfront exclusion of protected files (`.env*`, secrets, SQLite databases, logs, Git mirror metadata) before hashing or caching.
- **Critical Semantic Equivalence Invariant**: For any given repository state, incremental indexing produces **100% semantically identical results to a clean full rebuild from scratch**.
- **Content Hash Authority**: Delta detection relies on cryptographic SHA-256 content hashes, preventing missed modifications from timestamp tampering or restoration.

---

## 2. Architectural Design

The incremental indexing architecture is structured across three focused modules within `forge.repository`:

```
app/forge/repository/
├── cache_schemas.py    # Pydantic schemas, version contracts, and update summaries
├── cache.py            # IndexCacheManager: atomic disk storage, LRU bounds, and corruption recovery
└── incremental.py      # IncrementalRepositoryIndex: delta detection, graph assembly, and equivalence validation
```

### 2.1 Metadata Contracts & Data Schemas (`forge.repository.cache_schemas`)

- `FileIndexRecord`: Captures the complete AST-extracted state of an individual source file:
  - `file_path`, `content_hash`, `size_bytes`, `mtime_ns`, `module_name`, `docstring`, `total_lines`.
  - `symbols: List[SymbolRecord]`, `nodes: List[GraphNode]`, `call_sites: List[CallSiteRecord]`, `imports: List[ImportRecord]`.
  - `class_bases: Dict[str, List[str]]` (inheritance hierarchies).
  - `callable_local_vars: Dict[str, List[str]]` (deterministic caller-scoped local variables for shadowing detection).
- `CacheEntry`: Envelope enclosing disk-persisted records:
  - `schema_version`: Global cache contract version (`INDEX_SCHEMA_VERSION = "1.0.0"`).
  - `parser_version`: AST extractor version (`INDEXER_PARSER_VERSION = "1.0.0"`).
  - `payload: FileIndexRecord`: Serialized file record.
  - `checksum`: SHA-256 digest of canonical JSON payload for corruption detection.
- `CacheStats`: Real-time telemetry tracking `cache_hits`, `cache_misses`, `invalidations`, `corrupted_entries_recovered`, and `evicted_entries`.
- `IncrementalUpdateSummary`: Audit record returned from indexing operations:
  - Tracks `total_files_scanned`, `unchanged_files`, `added_files`, `modified_files`, `deleted_files`, `renamed_files`, `invalidated_dependents`, `cache_hits`, `cache_misses`, `duration_seconds`, `rebuild_fallback_triggered`, and `rebuild_fallback_reason`.
- `RepositoryIndexState`: Persisted snapshot capturing repository-wide fingerprint (`repo_fingerprint`) and individual file SHA-256 mapping.

### 2.2 Cache Storage & Lifecycle Management (`forge.repository.cache.IndexCacheManager`)

- **Atomic Writes**: Writes entries to temporary files (`.tmp.{pid}.{uuid}`) in the cache directory, flushes and calls `os.fsync`, then commits via atomic POSIX `os.replace`. Eliminates partial-read corruptions during crashes or power loss.
- **Interrupted Write Cleanup**: `cleanup_interrupted_writes()` scans for and deletes orphaned `.tmp.*` files on initialization.
- **Integrity & Checksum Verification**: Every read verifies schema version, parser version, expected content hash, and recomputes the SHA-256 payload checksum.
- **Corruption Recovery**: Tampered or unparseable JSON files trigger `_handle_corruption()`, safely deleting the corrupt file, logging telemetry, and falling back to re-parsing.
- **Bounded Storage & LRU Eviction**: Configurable bounds (`max_entries = 5000`, `max_bytes = 50 MB`). `evict_if_needed()` evicts the oldest accessed entries according to an LRU policy when thresholds are exceeded.
- **Obsolete Entry Pruning**: `prune_obsolete()` scans disk entries and safely unlinks records for files deleted from the workspace.

### 2.3 Incremental Coordination & Equivalence Engine (`forge.repository.incremental.IncrementalRepositoryIndex`)

- **Delta Detection**: Compares current workspace file hashes against `RepositoryIndexState`:
  - `unchanged`: Content hash identical $\rightarrow$ loaded directly from cache via `cache_manager.get()`.
  - `modified`: Content hash changed $\rightarrow$ old cache invalidated, file re-parsed and re-cached.
  - `added`: New path $\rightarrow$ parsed and cached.
  - `deleted`: Path absent $\rightarrow$ cache entry purged and omitted from graphs.
  - `renamed`: Deleted path with identical content hash to an added path $\rightarrow$ recognized as rename.
- **Downstream Dependent Invalidation**: Traces transitive dependents via `ModuleDependencyGraph.get_transitive_dependents()` for all modified, added, or deleted files, recording `invalidated_dependents`.
- **Syntax Error Resilience**: Files with deliberate syntax errors or unparseable structures are safely cached as empty records, guaranteeing consistent module node representation across full and incremental builds without crashing.
- **Cross-File Resolution Integration**:
  - `build_symbol_index_from_records()`: Assembles workspace `SymbolIndex` with exact classification counts.
  - `resolve_call_graph_from_records()`: Shared between `call_graph.py` and `incremental.py`, resolving local, self/cls, direct, qualified, aliased, recursive, and star calls.
  - `build_module_graph_from_records()`: Resolves module edges, internal dependencies, external packages, Tarjan SCCs, and elementary cycles.
- **Rebuild Fallback**: If an unexpected exception occurs during incremental resolution, the engine safely clears cache state, triggers a full clean rebuild from scratch (`rebuild_fallback_triggered = True`), and records `rebuild_fallback_reason`.

---

## 3. Empirical Verification & Benchmarks

### 3.1 Unit Test Suite (`tests/unit/test_incremental_indexing.py`)

A comprehensive 19-test suite validates all functional, edge-case, security, and equivalence requirements:

| Test Case | Description | Result |
| :--- | :--- | :--- |
| `test_first_indexing_creates_valid_cache` | Verifies cold build creates cache entries and state snapshot | `PASSED` |
| `test_warm_indexing_produces_cache_hits` | Verifies warm build achieves 100% cache hits and 0 misses | `PASSED` |
| `test_content_change_invalidates_entry` | Verifies modifying a file invalidates only that entry | `PASSED` |
| `test_hash_based_detection_same_timestamp` | Verifies hash authority when file timestamp is artificially restored | `PASSED` |
| `test_parser_version_change_invalidates` | Verifies upgrading parser version triggers complete cache invalidation | `PASSED` |
| `test_schema_version_change_invalidates` | Verifies upgrading schema version triggers complete cache invalidation | `PASSED` |
| `test_corrupted_cache_entry_safe_recovery` | Verifies tampered/corrupt JSON safely recovers via re-parse | `PASSED` |
| `test_checksum_mismatch_detected` | Verifies payload tampering with intact JSON fails checksum check | `PASSED` |
| `test_interrupted_writes_cleaned_up` | Verifies orphaned `.tmp.*` files are safely detected and deleted | `PASSED` |
| `test_lru_cache_eviction` | Verifies bounded storage evicts oldest accessed entries under LRU | `PASSED` |
| `test_file_added_lifecycle` | Verifies newly created files are indexed and included in graphs | `PASSED` |
| `test_file_deleted_lifecycle` | Verifies deleted files are cleanly removed without stale references | `PASSED` |
| `test_file_renamed_lifecycle` | Verifies file renaming without content changes is recognized | `PASSED` |
| `test_downstream_dependents_invalidation` | Verifies changing a base module invalidates dependent modules | `PASSED` |
| `test_cycle_detection_updates_incrementally`| Verifies adding/removing cyclic imports dynamically updates cycles | `PASSED` |
| `test_protected_files_never_cached` | Verifies `.env`, `test.db`, `contacts.json`, `logs` are never read/cached | `PASSED` |
| `test_escaping_symlink_rejected` | Verifies symlinks escaping the repository are rejected fail-closed | `PASSED` |
| `test_rebuild_fallback_safety` | Verifies simulated unexpected errors trigger clean full rebuilds | `PASSED` |
| `test_critical_equivalence_with_full_rebuild` | **Asserts exact semantic equality with full rebuild and M4 impact** | `PASSED` |

### 3.2 Full Regression Suite

The full FORGE regression suite was executed across all phases and modules:
- **1,713 passed, 7 skipped in 169.96s (2m 49s)**
- Zero regressions across Call Graph (M2), Module Dependency Graph (M3), Change-Impact Analysis (M4), Protected Boundaries (Phase 10), and Governance (Phase 9).

### 3.3 Real-World Performance on the FORGE Codebase

Evaluated across the entire FORGE repository (279 Python source files):

| Metric | Measurement | Notes |
| :--- | :--- | :--- |
| **Workspace Scope** | 279 Python files | Complete FORGE codebase |
| **Cold Full Build (no cache)** | **7.38s** | Parsing all ASTs and resolving all graphs |
| **Cold Incremental Build** | **16.66s** | First run: AST parsing + atomic JSON writes to disk |
| **Warm Incremental Build (0 changes)** | **2.32s** | **279 cache hits, 0 cache misses (100.0% hit rate)** |
| **Speedup vs Cold Full Rebuild** | **3.19x faster** | 2.32s vs 7.38s |
| **Single-File Delta Build** | **2.37s** | Only modified file re-parsed; 278 cache hits |
| **Multi-File Delta Build (3 files)** | **2.26s** | Only 3 files re-parsed; 276 cache hits |
| **Total Cache Storage** | **19.35 MB** | 279 serialized `FileIndexRecord` JSON envelopes |
| **Semantic Equivalence** | **100.0% Verified (`True`)** | All 14 equivalence checks pass identically |

---

## 4. Phase 11 Roadmap Status

- [x] **Milestone 1**: Intelligent Repository Understanding Architecture Audit `[COMPLETED]`
- [x] **Milestone 2**: Symbol Graph & Cross-File Call-Graph Engine `[COMPLETED]`
- [x] **Milestone 3**: Module Dependency Graph & Cycle Detection `[COMPLETED]`
- [x] **Milestone 4**: Change-Impact Analysis `[COMPLETED]`
- [x] **Milestone 5**: Incremental Indexing & Caching `[COMPLETED]`
- [ ] **Milestone 6**: LLM Tooling & Context Integration `[PLANNED]`
