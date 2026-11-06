# Phase 11 Specification: Intelligent Repository Understanding

## Overview

Phase 11 equips FORGE with semantic understanding of target software repositories beyond isolated single-file AST inspection. It transforms raw repository code into structured symbol graphs, cross-file invocation graphs, module dependency DAGs, and change-impact graphs while strictly adhering to FORGE's core invariants:
- **Audit-Only**: Zero modification of target repository files or Git history during analysis.
- **Fail-Secure Static Analysis**: No dynamic execution or `importlib` evaluation of untrusted repository code.
- **Zero-Knowledge Boundaries**: Zero indexing, hashing, or reading of protected files (`.env*`, secrets, SQLite databases, private logs, or Git mirror metadata).
- **Explicit Resolution Provenance**: Statically confirmed relationships are clearly distinguished from unresolved, external, ambiguous, or dynamic invocations without fabricating edges.

---

## Milestone Roadmap

- **Milestone 1**: Intelligent Repository Understanding Architecture Audit `[COMPLETED]`
  - Comprehensive architectural review of existing repository intelligence, AST indexing, manifest parsing, and context assembly pipelines.
  - Classified 13 core intelligence capabilities; identified existing gaps (cross-file resolution, call graphs, module DAGs).
- **Milestone 2**: Symbol Graph & Cross-File Call-Graph Engine `[COMPLETED]`
  - Stable qualified symbol identities across packages, classes, methods, and nested scopes.
  - Cross-file internal import resolver with alias and relative traversal support.
  - Static invocation extractor (`ast.Call`) supporting direct, module-qualified, aliased, `self.method()`, `cls.method()`, and inherited calls.
  - Cycle-safe, bounded BFS/DFS call-graph traversal APIs (`get_callers`, `get_callees`, `get_direct_callers`, `get_direct_callees`).
- **Milestone 3**: Module Dependency Graph & Cycle Detection `[COMPLETED]`
  - Statically extracted directed module dependency graph using AST imports without code execution.
  - Full import coverage: direct, from-module, aliased, relative multi-level, and star imports.
  - Consolidated duplicate dependency edges with statement-level import evidence.
  - Deterministic Strongly Connected Component partitioning via Tarjan's algorithm.
  - Canonical elementary cycle detection identifying self-dependencies, 2-module cycles, and multi-module cycles.
  - Bounded dependency/dependent query APIs with depth and node caps.
- **Milestone 4**: Change-Impact Analysis `[COMPLETED]`
  - Deterministic reverse call-graph and module dependency propagation engine.
  - Unified diff and explicit file/symbol changeset extraction with Phase 10 boundary enforcement.
  - Five-tier impact classification: `DIRECT`, `TRANSITIVE`, `POTENTIAL`, `UNRESOLVED`, `UNAFFECTED`.
  - Dynamic dispatch and star-import impact identification.
  - Cycle-safe bounded traversal with `max_depth` and `max_nodes` budgeting.
  - Dedicated milestone documentation in [`docs/milestones/phase-11-m4-change-impact-analysis.md`](../milestones/phase-11-m4-change-impact-analysis.md).
- **Milestone 5**: Incremental Indexing & Caching `[PLANNED]`
- **Milestone 6**: LLM Tooling & Context Integration `[PLANNED]`

---

## Milestone 2 Architecture: Symbol Graph & Call-Graph Engine

### 1. Data Models & Schemas (`forge.repository.graph_schemas`)

#### Graph Nodes (`GraphNode`)
Every class, function, method, and module in the repository is assigned a stable, collision-free identity:
- `node_id`: Stable identifier formatted as `<rel_path>:<scoped_symbol_name>`, e.g. `services/user.py:User.describe` or `utils/math.py:pipeline.<locals>.inner_step`.
- `kind`: `NodeKind` enum (`MODULE`, `CLASS`, `FUNCTION`, `METHOD`, `VARIABLE`).
- `name`: Simple identifier name (e.g. `describe`).
- `qualified_name`: Canonical dotted path (e.g. `services.user.User.describe`).
- `file_path`: Normalized workspace-relative path.
- `start_line` / `end_line`: Source location boundaries (1-indexed).
- `parent_scope`: Optional enclosing class or function scope.
- `parameters`: Parameter list for callables.
- `is_async`: Boolean flag indicating `async def`.

#### Invocation Edges (`CallEdge`)
Every call site extracted from the AST generates an explicit, provenance-backed edge:
- `source_id`: The `node_id` of the enclosing function, method, or module.
- `target_id`: The resolved callee `node_id` (if resolved internally), otherwise `None`.
- `source_file`: Workspace-relative path of the caller.
- `line_number` / `col_offset`: Precise location of the call site.
- `raw_call_expr`: Normalized string representation of the call expression.
- `resolution_status`: `CallResolutionStatus` enum.
- `resolution_reason`: Deterministic rationale explaining how the target was identified or why it was marked unresolved.
- `target_qualified_name`: Resolved qualified name if determined.

#### Resolution Status Categories (`CallResolutionStatus`)
- `RESOLVED_INTERNAL`: Statically linked to an internal symbol defined within the workspace.
- `RESOLVED_EXTERNAL`: Determined to be a Python built-in function or imported from an external library/standard library.
- `UNRESOLVED`: Static resolution could not unambiguously locate the symbol within workspace files or known imports.
- `AMBIGUOUS`: Multiple conflicting symbol definitions exist (e.g. symbol present in multiple star-imported modules).
- `DYNAMIC`: Invocation uses dynamic evaluation (e.g. `getattr(obj, "func")()` or callable variables).

---

### 2. Static Workspace Import Resolver (`forge.repository.resolver`)

`WorkspaceImportResolver` constructs a workspace-wide module catalog:
- Discovers source roots (`src/`, `app/`, `lib/`, package roots containing `__init__.py`).
- Resolves absolute imports (`import pkg.mod`, `from pkg.mod import func`).
- Resolves import aliases (`from pkg.mod import func as alias`).
- Resolves relative imports:
  - Level 1 (`from . import func`): current package directory.
  - Level 2 (`from ..sub import func`): parent package directory.
  - Traversal Security: Fails closed (`UNRESOLVED`) if relative import attempts to climb above the workspace root.
- Handles module vs package disambiguation (`pkg/__init__.py` vs `pkg/sub.py`).
- Classifies standard library (`sys.stdlib_module_names`) and external third-party dependencies as `RESOLVED_EXTERNAL`.

---

### 3. Call-Graph Engine (`forge.repository.call_graph`)

The `CallGraph` model manages the symbol invocation graph:

#### Supported Invocation Patterns
1. **Direct Calls**: `foo()` $\rightarrow$ resolved against local nested functions, same-file declarations, imported symbols, and built-ins.
2. **Module-Qualified Calls**: `module.func()` $\rightarrow$ resolved via imported workspace modules.
3. **Imported Aliases**: `from mod import func as my_func; my_func()` $\rightarrow$ resolves to original `mod:func`.
4. **Method Invocations (`self.method()`)**: Resolved against the enclosing class; searches known base classes in same file or imported files for inherited methods.
5. **Class Method Invocations (`cls.method()`)**: Resolved identically across inheritance hierarchies.
6. **Nested Function Scopes**: Functions defined inside callables are isolated within `<locals>` scopes and resolved accurately.
7. **Recursive Functions**: Recursive calls are modeled safely with self-referential edges.
8. **Mutually Recursive Functions**: Cycles (e.g. `ping()` $\leftrightarrow$ `pong()`) are fully supported without infinite loops.
9. **Multi-Call Site Preservation**: If a caller invokes a callee multiple times, each distinct call site is preserved in `edges` with its unique line number.
10. **Star Imports**: Symbols from `from mod import *` resolve internally if uniquely matched; flagged `AMBIGUOUS` if present in multiple star-imported modules.
11. **Dynamic Invocations**: `getattr(...)()` and complex receiver expressions are classified as `DYNAMIC` without fabricating edges.

#### Traversal & Query APIs
- `get_direct_callees(symbol_id) -> List[str]`: Directly called internal symbol IDs.
- `get_direct_callers(symbol_id) -> List[str]`: Internal symbols that directly call this symbol.
- `get_callees(symbol_id, max_depth=2, max_nodes=100) -> List[str]`: Transitive callees via cycle-safe BFS traversal.
- `get_callers(symbol_id, max_depth=2, max_nodes=100) -> List[str]`: Transitive callers via cycle-safe BFS traversal.
- `get_call_edges(source_id=None, target_id=None) -> List[CallEdge]`: Filtered call sites.

All traversal queries enforce:
- Deterministic lexicographic sorting.
- Explicit cycle protection via `visited` sets.
- Strict bounded recursion (`max_depth`) and node budgeting (`max_nodes`).
- Fail-safe response (`[]`) for non-existent symbols.

---

### 4. Empirical Performance & Verification

Tested against the complete FORGE codebase (266 Python files):
- **Symbol Indexing Time**: 2.342 seconds (9,654 symbols).
- **Call-Graph Construction Time**: 2.870 seconds (3,682 nodes, 25,392 call edges).
- **Query Traversal Latency**: 0.001 ms per 1,000 BFS traversals ($1.2\ \mu\text{s}$ per query).
- **Memory Footprint**: Parsed ASTs are discarded immediately after node/call extraction.
- **Test Suite Results**:
  - `tests/unit/test_repository_resolver.py`: 12 passed.
  - `tests/unit/test_call_graph.py`: 23 passed (including transitive inheritance and shadowing regression tests).
  - Full FORGE test suite: **1,638 passed, 7 skipped (100% pass rate)**.

---

---

## Milestone 3 Architecture: Module Dependency Graph & Cycle Detection

### 1. Data Models & Schemas (`forge.repository.graph_schemas`)

#### Module Nodes (`ModuleNode`)
Every Python module in the workspace is mapped to an authoritative `ModuleNode`:
- `module_id`: Canonical dotted module identifier (e.g. `app.forge.repository.indexer`).
- `file_path`: Workspace-relative file path (e.g. `app/forge/repository/indexer.py`).
- `is_package`: Boolean flag indicating whether the module is a package initialization file (`__init__.py`).
- `docstring`: Extracted top-level module docstring.
- `total_lines`: Source line count.
- `internal_dependencies`: Sorted list of direct internal module IDs imported.
- `internal_dependents`: Sorted list of direct internal module IDs that import this module.
- `external_dependencies`: Sorted list of external packages imported (e.g. `pydantic`, `pytest`).
- `in_degree` / `out_degree`: Internal dependent and dependency counts.

#### Dependency Edges (`ModuleEdge`)
Directed dependency relationships where `source_module` imports `target_module`:
- `source_module`: Importing module identifier.
- `target_module`: Imported module identifier.
- `source_file` / `target_file`: Workspace-relative paths for both endpoints.
- `evidence`: List of `ImportEvidence` records consolidating all import statements between the modules.
- `weight`: Count of import occurrences connecting the modules.
- `is_self_dependency`: Boolean indicating direct self-import (`source_module == target_module`).

#### Import Evidence (`ImportEvidence`)
Detailed statement-level provenance for every import statement:
- `importing_file`: Relative file path.
- `line_number`: 1-indexed source line.
- `imported_name`: Symbol or submodule name.
- `alias`: Local alias name if specified with `as`.
- `from_module`: Base module clause for from-imports.
- `level`: Relative import climb level (`0` for absolute, `1` for `.`, `2` for `..`).
- `import_type`: Classified category (`direct`, `from`, `relative`, `star`).
- `resolution_status`: `CallResolutionStatus` category.
- `resolution_reason`: Deterministic rationale for resolution.

#### External & Unresolved Records
- `ExternalImportRecord`: Captures external dependencies with `package_name`, `is_stdlib` (distinguishing Python standard library vs third-party), and line numbers.
- `UnresolvedImportRecord`: Records import statements that could not be statically resolved without fabricating spurious edges.

#### Cycles & Components (`DependencyCycle`, `StronglyConnectedComponent`)
- `StronglyConnectedComponent`: Partitioned subgraphs computed via Tarjan's algorithm, with `modules`, `size`, and `is_cyclic`.
- `DependencyCycle`: Canonical elementary cycle path `[A, B, ..., A]` starting and ending at the lexicographically minimum module, with `length`, `is_self_cycle`, and `edges`.

---

### 2. Module Dependency Graph Engine (`forge.repository.module_graph`)

The `ModuleDependencyGraph` model provides query, traversal, and cycle analysis APIs:
- `get_module(module_identifier) -> Optional[ModuleNode]`: Unified lookup accepting module IDs, file paths, or module aliases.
- `get_dependencies(module_identifier) -> List[str]`: Directly imported internal module IDs.
- `get_dependents(module_identifier) -> List[str]`: Direct internal dependent module IDs.
- `get_transitive_dependencies(module_identifier, max_depth=10, max_nodes=500) -> List[str]`: Bounded BFS forward traversal.
- `get_transitive_dependents(module_identifier, max_depth=10, max_nodes=500) -> List[str]`: Bounded BFS reverse traversal.
- `get_edge(source_identifier, target_identifier) -> Optional[ModuleEdge]`: Lookup of consolidated dependency edge.
- `get_strongly_connected_components(only_cyclic=False) -> List[StronglyConnectedComponent]`: Tarjan's SCC algorithm.
- `detect_cycles(max_cycles=500, max_depth=50) -> List[DependencyCycle]`: Deterministic elementary cycle detection.
- `is_in_cycle(module_identifier) -> bool`: Quick check whether a module belongs to any cyclic SCC.
- `get_isolated_modules() -> List[str]`: Modules with zero internal dependencies and zero internal dependents.
- `get_dependency_summary() -> ModuleDependencyGraphSummary`: Statistical metrics across the repository.

---

### 3. Empirical Performance & Verification

Tested against the complete FORGE codebase (269 Python files):
- **Graph Construction Time**: 2.594 seconds (269 modules, 1,428 internal edges).
- **External Imports**: 1,844 total (1,573 standard library, 271 third-party).
- **Unresolved / Ambiguous Imports**: 0 unresolved, 0 ambiguous.
- **Cycle Analysis**: 2 cyclic SCCs detected:
  - `app.forge.repair.contracts` $\leftrightarrow$ `app.forge.repair.models`
  - `app.forge.workflow.dossier_models` $\leftrightarrow$ `app.forge.workflow.models`
- **Isolated Modules**: 43 modules without internal dependencies or dependents.
- **Test Suite Results**:
  - `tests/unit/test_module_graph.py`: 24 passed.
  - `tests/unit/test_repository_resolver.py`: 13 passed.
  - `tests/unit/test_call_graph.py`: 23 passed.
  - Phase 11 M2 & M3 unit suite: **60 passed in 0.73s (100% pass rate)**.

---

## 4. Milestone 4 Architecture: Change-Impact Analysis Engine

### 1. Data Models & Schemas (`forge.analysis.impact.schemas`)

- `ChangeSet`: Structured collection of touched files (`FileChange`) and touched AST symbols (`SymbolChange`).
- `ImpactClassification`: Strict five-tier taxonomy:
  - `DIRECT`: Direct caller of modified symbol or direct importer of modified module (depth=1, confidence=1.0).
  - `TRANSITIVE`: Indirect caller or transitive module dependent (depth $\ge 2$, confidence decayed by distance $0.95^{\text{depth}-1}$).
  - `POTENTIAL`: Dynamic call site or star-import consumer where static certainty is absent (confidence=0.60–0.75).
  - `UNRESOLVED`: Dependent calling or importing a deleted/renamed symbol or module (confidence=0.95).
  - `UNAFFECTED`: Explicitly classified when verifying non-impacted status.
- `ImpactedEntity`: Entity record with `entity_id`, `entity_type`, `file_path`, `classification`, `depth`, `impact_path`, `path_steps`, `trigger_changes`, `confidence`, and `explanation`.
- `ImpactReport`: Top-level report bundling `changeset`, `impacted_entities`, `summary`, and `warnings`.

### 2. Analysis & Extraction Pipeline

- `extract_changeset_from_diff(diff_text, repo_root, call_graph)`: Extracts file changes and correlates modified line numbers with static AST symbols via `parse_unified_diff()`. Enforces strict Phase 10 protected-file exclusion, traversal rejection, and containment safety.
- `extract_changeset_from_files(changed_files, repo_root, call_graph)`: Extracts changesets from file paths or dictionaries.
- `ChangeImpactAnalyzer(repo_root, call_graph, module_graph, max_depth, max_nodes)`:
  - Propagates symbol changes to callers via `CallGraph.get_direct_callers()` and transitive BFS.
  - Method modifications propagate to enclosing classes and cross-module method call sites.
  - Propagates module changes to dependents via `ModuleDependencyGraph.get_dependents()` and transitive BFS.
  - Detects dynamic dispatch (`globals().get()()`, `getattr()`) and star imports (`from ... import *`).
  - Identifies breaking changes to deleted or renamed symbols/modules as `UNRESOLVED`.
  - Enforces traversal budgets (`max_depth`, `max_nodes`) and deduplicates paths preserving minimal depth.

### 3. Empirical Verification & Performance

Tested on the FORGE codebase (modifying `app/forge/repository/graph_schemas.py`):
- **16 Changed Symbols** analyzed.
- **242 Impacted Entities** detected (32 direct, 210 transitive, max depth 5).
- **Execution Latency**: 2.65 seconds (well within the $< 3.0$ second performance target).
- **Unit Test Suite**: `tests/unit/test_change_impact.py`: **31 passed in 0.50s (100% pass rate)**.
- **Phase 11 Total Unit Tests**: **91 passed (100% pass rate)**.

---

## 5. Remaining Project Roadmap & Final Release Plan

The remaining FORGE project roadmap is strictly bounded to three phases:

- **Phase 11: Intelligent Repository Understanding** `[CURRENT]`
  - M1: Architecture Audit `[COMPLETED]`
  - M2: Symbol Graph & Cross-File Call-Graph Engine `[COMPLETED]`
  - M3: Module Dependency Graph & Cycle Detection `[COMPLETED]`
  - M4: Change-Impact Analysis `[COMPLETED]`
  - M5: Incremental Indexing & Caching `[REMAINING]`
  - M6: LLM Tooling & Context Integration `[REMAINING]`
- **Phase 12: Advanced Intelligence and Autonomous Repair** `[REMAINING]`
- **Phase 13: Complete Engineering Agent and Final Release** `[REMAINING]`

**Explicit Architectural Boundary**: There are NO separate Phases 14, 15, or 16. All capabilities through v1.0 release conclude within Phase 13.
