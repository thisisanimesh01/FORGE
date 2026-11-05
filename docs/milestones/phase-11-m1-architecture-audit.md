# Phase 11, Milestone 1 — Intelligent Repository Understanding Architecture Audit

## 1. Executive Summary

FORGE is designed to find bugs, observe and understand code, repair defects, generate and verify tests, and engineer pull requests. Following the safe completion and verification of Phase 10 (Protected-File Boundaries & Option C Mirror Isolation), Phase 11 initiates the design and construction of **Intelligent Repository Understanding**.

This Milestone 1 audit provides a comprehensive, read-only architectural evaluation of FORGE's existing static intelligence, indexing, dependency analysis, and LLM context assembly subsystems across `app/forge/repository/`, `app/forge/analysis/`, `app/forge/investigation/`, and `app/forge/workflow/`.

### Key Findings
1. **Existing Baseline Strengths**:
   - Resilient single-file AST symbol extractor (`forge.repository.indexer`).
   - Repository topology and framework signature detector (`forge.repository.detector`).
   - External package dependency manifest parser (`forge.analysis.dependency_manifest`).
   - Token- and character-budgeted context prompt builder (`forge.workflow.context`).
   - Strict read-only isolation guarantees (Phase 10 Option C) preventing untrusted code or protected files from entering analysis workspaces.
2. **Identified Architectural Gaps**:
   - **No Cross-File Symbol Identity**: Symbols were identified only by simple unqualified names (`query` instead of `forge.repository.indexer.SymbolIndex.query`).
   - **No Internal Import Resolution**: Import statements (`from a.b import c`) were recorded as raw metadata strings without resolving to workspace target files or exports.
   - **No Call Graph or Invocation Hierarchy**: Function invocation relationships (`ast.Call`), caller/callee trees, and cross-file method invocations were absent.
   - **No Directed Module Dependency DAG**: Dependency analysis was limited to external manifests and filename stem heuristics (`test_<stem>.py` $\leftrightarrow$ `<stem>.py`).
   - **No Semantic Change-Impact Analysis**: Git diffs identified line changes, but could not compute downstream impacted symbols or affected test suites.
   - **No Incremental Indexing Cache**: Every indexing pass performed a fresh disk walk.

---

## 2. Capabilities Classification

Each capability was evaluated and classified against the codebase:

| Capability | Status | Source Code Reference |
| :--- | :--- | :--- |
| **1. Python AST Parsing** | **Implemented & Verified** | `app/forge/repository/indexer.py` (`ast.parse`, `ASTSymbolVisitor`) |
| **2. Function & Class Extraction** | **Implemented & Verified** | `app/forge/repository/indexer.py` (`visit_ClassDef`, `visit_FunctionDef`) |
| **3. Symbol Indexing** | **Implemented & Verified** | `app/forge/repository/indexer.py` (`SymbolRecord`, `SymbolIndex`) |
| **4. Import Resolution** | **Partially Implemented** | `app/forge/repository/indexer.py` (extracts raw strings; internal path resolution missing) |
| **5. Cross-File Reference Resolution** | **Not Implemented** | No symbol linker or workspace-wide occurrence table |
| **6. Function Call Relationships** | **Not Implemented** | `visit_Call` absent from original AST visitor |
| **7. Dependency Graph Construction** | **Partially Implemented** | `app/forge/analysis/dependency_manifest.py` (stem heuristic only; no module DAG) |
| **8. Call-Chain Traversal** | **Not Implemented** | No call graph data structure existed |
| **9. Data-Flow Analysis** | **Not Implemented** | Benchmark fixtures present, zero analyzer logic in application |
| **10. Change-Impact Analysis** | **Partially Implemented** | `app/forge/repository/git_mediator.py` (diff extraction only; no semantic impact graph) |
| **11. Incremental Indexing** | **Not Implemented** | Full sweep `rglob("*.py")` on every run; no cache or file hashes |
| **12. Multi-Language Understanding** | **Not Implemented** | Exclusively Python `ast`; other languages only recorded in extension counts |
| **13. Repository-Wide Context** | **Implemented & Verified** | `app/forge/repository/architecture.py`, `app/forge/workflow/context.py` (`RepositoryMap`, budgeted prompt) |

---

## 3. Existing Architecture Analysis

### Indexer & Detector (`app/forge/repository/`)
- `ASTSymbolVisitor` intercepted `ast.Import`, `ast.ImportFrom`, `ast.ClassDef`, `ast.FunctionDef`, and `ast.AsyncFunctionDef`.
- Extracted parameter names, docstrings, line numbers, and async flags.
- Enforced safety limits: skipped files $> 500\text{ KB}$, capped files at 500, handled encoding failures with `errors="replace"`, and skipped syntax errors gracefully.
- `detect_repository` scanned topology (`__init__.py` packages, test directories, entrypoints, configuration files) and framework signatures (`fastapi`, `flask`, `django`, `pytest`, `pydantic`).

### Dependency & Context Subsystems (`app/forge/analysis/`, `app/forge/workflow/`)
- `DependencyManifest` parsed `pyproject.toml`, `requirements.txt`, and `setup.cfg`.
- Filename stem heuristics linked test files to candidate implementation files (`test_foo.py` $\leftrightarrow$ `foo.py`).
- `render_context_prompt()` ranked top symbols, summarized frameworks, and truncated instruction files (`AGENTS.md`, `README.md`) under strict character budgets.

---

## 4. Proposed Phase 11 Architecture

To bridge these gaps while adhering strictly to FORGE's Fail-Secure, Audit-Only, and Option C isolation rules, Phase 11 defines five implementation milestones (M2–M6):

1. **Milestone 2: Symbol Graph & Cross-File Call-Graph Engine**:
   - Qualified symbol identities (`<rel_path>:<scoped_name>` and `<module>.<scope>`).
   - Static workspace import resolver supporting absolute, relative, aliased, and package imports.
   - Invocation extractor (`ast.Call`) supporting direct calls, module-qualified calls, aliased imports, `self.method()`, `cls.method()`, inheritance, and recursion.
   - Cycle-safe bounded BFS/DFS traversal APIs (`get_callers`, `get_callees`, `get_direct_callers`, `get_direct_callees`).
2. **Milestone 3: Module Dependency Graph & Cycle Detection**:
   - Directed acyclic graph (DAG) of workspace modules.
   - Topological sorting and circular import detection.
   - Test-to-implementation module mapping.
3. **Milestone 4: Change-Impact Analysis Engine**:
   - Git diff line-to-symbol mapping.
   - Downstream impact propagation across call graphs and dependency graphs.
   - Affected test suite identification for targeted regression verification.
4. **Milestone 5: Incremental Indexing & Caching**:
   - File content hashing (SHA-256) and mtime tracking.
   - Delta-only parsing for modified files.
   - Fast cache invalidation without reading protected paths.
5. **Milestone 6: LLM Tooling & Context Integration**:
   - Safe, budgeted investigation tools (`get_callers`, `get_callees`, `get_impacted_symbols`).
   - Subgraph visualization and targeted context injection for the investigation loop.

---

## 5. Security & Boundary Invariants

- **Sanitized Analysis Workspace Only**: All graph analysis operates strictly on the sanitized workspace (`acquired.workspace_path`).
- **No Dynamic Execution**: Never use `importlib` or dynamic module execution to inspect symbols. All intelligence is derived purely via static AST inspection.
- **Fail-Secure Path Containment**: Relative imports and module paths are validated with `assert_contained_path()` to prevent traversal or symlink escapes.
- **Zero-Knowledge Protected Path Guarantee**: Protected paths (`.env*`, `*.db*`, `*.log`, credentials, `.git`) are excluded before any reading, parsing, or hashing.

---

## 6. Complete Roadmap Boundary

The remaining FORGE project roadmap consists strictly of:
- **Phase 11: Intelligent Repository Understanding** (M1 & M2 Complete, M3–M6 Remaining).
- **Phase 12: Advanced Intelligence and Autonomous Repair** (Remaining).
- **Phase 13: Complete Engineering Agent and Final Release** (Remaining).

There are NO Phases 14, 15, or 16.
