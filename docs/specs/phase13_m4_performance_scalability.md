# Phase 13, Milestone 4: Performance & Scalability Engineering Specification

## 1. Executive Summary

Phase 13 Milestone 4 focuses on systematic performance profiling, elimination of measured hotspots, bounded resource allocation, and scale verification across large and deep codebases without compromising any Phase 10–13 security invariants.

FORGE operates as a strictly audit-safe and deterministic autonomous engineering intelligence platform. In Milestone 4, every architectural subsystem was profiled on synthetic benchmark suites scaling up to 250 modules and 297 Python files. Optimizations were deployed to address measured quadratic behaviors, file system sync latency, connection thrashing, and redundant AST traversals.

## 2. Key Architecture Optimizations

### 2.1 O(1) Cache Eviction & In-Memory Manifest (`forge.repository.cache`)
- **Problem**: Previously, `IndexCacheManager.put()` performed `_evict_if_needed()` by scanning the cache directory with `Path.glob("entry_*.json")` on every insertion, followed by file stats and `fsync()`. Across 297 files, this resulted in $O(N^2)$ disk scans and over 26 seconds of cold indexing time.
- **Solution**: Implemented an in-memory `_manifest: Dict[str, Tuple[str, float, int]]` populated once at cache startup. Insertions, access-time updates, and LRU evictions operate in $O(1)$ memory time. Evicted entries are unlinked directly without full directory scans.
- **Empirical Impact**: Cold incremental build on a 250-module repository reduced from **26,391 ms** to **1,867 ms** (>14x speedup).

### 2.2 Thread-Local SQLite Connection Pooling (`forge.storage.sqlite_pool`)
- **Problem**: `OrchestrationStateStore`, `IdempotencyLedger`, and `ApprovalLedger` opened and closed fresh SQLite connections on every query or transaction. Under concurrent workflows or batch recording (e.g. 100 workflows + events + checkpoints), connection setup and PRAGMA overhead dominated persistence latency.
- **Solution**: Developed `ThreadLocalSQLitePool` (`forge.storage.sqlite_pool`) maintaining one open connection per thread with WAL mode, foreign key enforcement, and explicit timeout configuration. Connection cleanup is managed via `weakref.finalize` and thread termination hooks.
- **Empirical Impact**:
  - 100 workflow + event + checkpoint writes: **276.8 ms** $\rightarrow$ **26.1 ms** (10.6x speedup, 0 connection re-opens).
  - 100 workflow reads: **159.6 ms** $\rightarrow$ **7.3 ms** (21.8x speedup).
  - 100 crash recovery inspections: **232.0 ms** $\rightarrow$ **13.3 ms** (17.4x speedup).
  - 8-thread concurrent contention (40 workflows): **354.0 ms** $\rightarrow$ **12.9 ms** (27.4x speedup).

### 2.3 Single-Pass AST Parsing & Discovery Pruning (`forge.repository.indexer`, `incremental`)
- **Problem**: Separate parsing passes occurred during symbol indexing, call graph construction, and module dependency graph generation. Directory traversal visited directories without early component-level pruning.
- **Solution**:
  - `discover_workspace_python_files()` performs top-down `os.walk` with early exclusion of protected directory names (`.git`, `.venv`, `.env*`, etc.) before recursing.
  - `IncrementalRepositoryIndex` parses each source file once (`parse_python_file_details`) and passes the pre-verified AST to both `build_call_graph` and `build_module_dependency_graph`.
- **Empirical Impact**: Full index construction latency on large repositories reduced from **7,641 ms** to **3,262 ms** (2.3x speedup). Invariant tests verify `ast.parse` call count exactly equals total workspace files.

### 2.4 Prefix-Based Fast Import Resolution (`forge.repository.resolver`)
- **Problem**: `WorkspaceImportResolver._build_workspace_module_map()` repeatedly invoked `Path.relative_to()` inside nested loops across candidate source roots, generating thousands of caught `ValueError` exceptions.
- **Solution**: Introduced normalized relative prefix slicing (`candidate_prefixes: List[str]`) and `known_python_files` fast paths. Dotted module resolution operates via direct string manipulation and dictionary lookups.
- **Empirical Impact**: Module graph construction latency dropped from **2,713 ms** to **1,382 ms** (nearly 2x speedup).

### 2.5 Bounded LRU Context Caching (`forge.llm.context`)
- **Problem**: Repeated context builds for the same repository state recomputed full symbol indices, call trees, and dependency summaries.
- **Solution**: Implemented an in-memory 16-entry LRU cache (`_context_cache`) keyed by `(query, target_files_tuple, stat_signature)`. The workspace stat signature evaluates file modification times and byte sizes; any disk mutation invalidates the cache automatically.
- **Empirical Impact**: Repeated context queries dropped from **2,729 ms** to **3.15 ms** (~860x speedup).

### 2.6 Shared Repository Index Across Adapters (`forge.orchestration.adapters`, `orchestrator`)
- **Problem**: In end-to-end orchestration, `DefaultDiagnosisAdapter`, `DefaultRootCauseAdapter`, and `DefaultPlanningAdapter` each instantiated separate repository indices.
- **Solution**: Orchestrator initializes a shared `IncrementalRepositoryIndex` instance and injects it across all default stage adapters.
- **Empirical Impact**: End-to-end diagnosis, root cause, and planning stages execute with at most 1–2 index builds per run instead of 4+.

### 2.7 Connection Pooling & Lifecycle in HTTP Provider (`forge.llm.openai`)
- **Problem**: `OpenAICompatibleProvider` instantiated a new `httpx.Client` per completion request, thrashing TCP sockets and SSL contexts.
- **Solution**: Implemented a thread-safe, lock-guarded persistent client (`_shared_client`) with clean `close()` lifecycle management.

### 2.8 Single-Pass Workspace Tree Mirroring (`forge.repair_execution.workspace`)
- **Problem**: Allocating repair workspaces copied files and then performed a second full directory walk to compute cryptographic baseline digests.
- **Solution**: Consolidated file copying, protected path filtering, and SHA-256 tree digest accumulation into a single directory traversal pass (`_safe_copy_dual_and_digest`).
- **Empirical Impact**: Workspace allocation latency dropped from **3,534 ms** to **674 ms** (5.2x speedup).

## 3. Security & Non-Regression Invariants

1. **Phase 10 Containment**: All path checks continue to enforce `assert_contained_path()` and `is_protected_path()` fail-closed.
2. **Phase 13 M3 SSRF Protection**: `OpenAICompatibleProvider` retains full SSRF URL validation, loopback verification, IP pinning, and private network rejection.
3. **Phase 13 M1 Human Approval Boundary**: Non-negotiable human approval invariants (`requires_human_review = True`) remain hardcoded and enforced across all execution stages.
4. **Equivalence Invariant**: `IncrementalRepositoryIndex.verify_equivalence()` mathematically verifies that incremental indexing yields 100% identical symbols, call edges, and module dependency graphs to cold full builds.

## 4. Verification Suite

- 13 dedicated deterministic performance & scalability regression tests in `tests/unit/test_phase13_m4_performance_scalability.py`.
- Full regression suite passing: 2,133+ unit tests and 4 security suites passing with 0 failures.
