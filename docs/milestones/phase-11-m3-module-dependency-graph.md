# Phase 11, Milestone 3 — Module Dependency Graph & Cycle Detection

## 1. Executive Summary

Phase 11, Milestone 3 introduces a comprehensive, deterministic, static **Module Dependency Graph & Cycle Detection Engine** for FORGE (`app/forge/repository/module_graph.py`, `app/forge/repository/graph_schemas.py`).

Milestone 3 builds directly on the import-resolution infrastructure of Milestone 2 (`WorkspaceImportResolver`), constructing a directed graph of all workspace Python modules, resolving internal dependencies, classifying external dependencies (standard library vs. third-party), consolidating duplicate imports with statement-level evidence, partitioning the graph via Tarjan's strongly connected components algorithm, and extracting canonical elementary dependency cycles without executing repository code or compromising Phase 10 isolation boundaries.

---

## 2. Architectural Design

```
+-----------------------------------------------------------------------------------+
|                           Workspace Python Modules (*.py)                         |
|                    (Sanitized workspace, protected files excluded)                |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  parse_python_file_details() (AST)
+-----------------------------------------------------------------------------------+
|                                 Raw Imports                                       |
|             (ImportRecord: direct, from-module, aliased, relative, star)          |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  WorkspaceImportResolver
+-----------------------------------------------------------------------------------+
|                             Import Classification                                 |
|   +-----------------------+   +-----------------------+   +-------------------+   |
|   |   RESOLVED_INTERNAL   |   |   RESOLVED_EXTERNAL   |   |    UNRESOLVED     |   |
|   |   (Workspace Module)  |   |   (Stdlib vs 3rd-Party|   | (Escapes / Ghost) |   |
|   +-----------------------+   +-----------------------+   +-------------------+   |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                            ModuleDependencyGraph                                  |
|  - Nodes: ModuleNode (module_id, file_path, is_pkg, lines, docstring, in/out deg)|
|  - Edges: ModuleEdge (source -> target, weight, evidence: [ImportEvidence, ...]) |
|  - Adjacency: Forward (dependencies) & Reverse (dependents)                       |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                        Cycle & Component Analysis                                 |
|  - Tarjan's Strongly Connected Components: get_strongly_connected_components()     |
|  - Canonical Elementary Cycles: detect_cycles() -> [DependencyCycle, ...]         |
|  - Bounded BFS Queries: get_transitive_dependencies(), get_transitive_dependents() |
+-----------------------------------------------------------------------------------+
```

---

## 3. Schemas & Data Models (`forge.repository.graph_schemas`)

### `ModuleNode`
Represents an individual Python module or package initialization file in the workspace:
- `module_id`: Canonical dotted name (e.g. `app.forge.repository.indexer`).
- `file_path`: Workspace-relative file path (e.g. `app/forge/repository/indexer.py`).
- `is_package`: `True` if module is an `__init__.py` file.
- `docstring`: Module-level docstring if present.
- `total_lines`: Source line count.
- `internal_dependencies`: Sorted list of direct internal module IDs imported by this module.
- `internal_dependents`: Sorted list of direct internal module IDs that import this module.
- `external_dependencies`: Sorted list of external package names imported by this module.
- `in_degree`: In-degree (number of modules depending on this module).
- `out_degree`: Out-degree (number of modules this module depends on).

### `ModuleEdge`
A directed dependency edge connecting `source_module` to `target_module` (`source_module` $\rightarrow$ `target_module`):
- `source_module`: Importing module ID.
- `target_module`: Imported module ID.
- `source_file`: Workspace-relative path of the importing module.
- `target_file`: Workspace-relative path of the imported module.
- `evidence`: List of `ImportEvidence` records consolidating all import statements between these two modules.
- `weight`: Count of import statements connecting the modules.
- `is_self_dependency`: `True` if `source_module == target_module` (direct self-import).

### `ImportEvidence`
Statement-level provenance for every import statement:
- `importing_file`: Relative file path.
- `line_number`: 1-indexed source line.
- `imported_name`: Symbol or submodule name.
- `alias`: Alias if imported with `as`.
- `from_module`: Base module clause for from-imports.
- `level`: Relative climb level (`0` for absolute, `1` for `.`, `2` for `..`).
- `import_type`: Categorized as `direct`, `from`, `relative`, or `star`.
- `resolution_status`: `CallResolutionStatus` (`RESOLVED_INTERNAL`, `RESOLVED_EXTERNAL`, etc.).
- `resolution_reason`: Deterministic rationale.

### `ExternalImportRecord` & `UnresolvedImportRecord`
- `ExternalImportRecord`: Captures external imports with `package_name`, `is_stdlib` (distinguishing Python standard library from third-party packages), and `line_number`.
- `UnresolvedImportRecord`: Preserves unresolvable import statements without fabricating spurious internal dependency edges.

### `StronglyConnectedComponent` & `DependencyCycle`
- `StronglyConnectedComponent`: Subgraph component with `scc_id`, sorted `modules`, `size`, and `is_cyclic` (`True` if size $> 1$ or self-loop).
- `DependencyCycle`: Canonical elementary cycle path `[A, B, ..., A]` starting and ending at the lexicographically minimum module, with `length`, `is_self_cycle`, and `edges`.

---

## 4. Import Resolution & Consolidation Semantics

1. **Absolute Imports**: Direct module imports (`import a.b`) and from-module imports (`from a.b import c`) are mapped via `WorkspaceImportResolver` to candidate workspace files.
2. **Relative Imports**: Multi-level relative imports (`.`, `..`, etc.) are resolved relative to the package hierarchy. Traversals attempting to climb outside the workspace root fail closed as `UNRESOLVED`.
3. **Aliased Imports**: Local aliases (`import x as y`, `from x import y as z`) resolve to the underlying module, recording the alias in `ImportEvidence.alias`.
4. **Package Initialization**: Relationships between `pkg/__init__.py` and submodules `pkg/sub.py` are accurately resolved in both directions.
5. **Star Imports**: `from pkg import *` resolves to the target module with `imported_name = "*"`.
6. **Duplicate Consolidation**: Multiple import statements targeting the same module from the same source file are consolidated into a single `ModuleEdge` with `weight = len(evidence)` while retaining each statement's line number and provenance.
7. **External Classification**: External imports are partitioned into standard library (via `sys.stdlib_module_names`) and third-party packages.

---

## 5. Cycle Detection Algorithm

The cycle analysis engine operates in two strictly separated, deterministic phases:

### Phase 1: Strongly Connected Components (Tarjan's Algorithm)
- Evaluates the directed internal dependency graph $G = (V, E)$ in deterministic lexicographic order.
- Computes maximal subgraphs where every module can reach every other module.
- Identifies cyclic components:
  - Component size $> 1$, OR
  - Component size $= 1$ with a direct self-edge ($u \to u$).
- Time Complexity: $O(|V| + |E|)$ linear time.

### Phase 2: Canonical Elementary Cycle Extraction
- For each cyclic SCC $C$ with sorted vertices $V_C$:
  - If $|V_C| == 1$: Direct self-dependency $[u, u]$ (length 1, `is_self_cycle=True`).
  - If $|V_C| > 1$: Searches elementary cycles starting from each $s \in V_C$ using vertices $v \ge s$.
  - Enforces canonical representation: cycle starts and ends with the lexicographically smallest module ID.
  - Bounded by `max_cycles=500` and `max_depth=50` to safeguard against combinatorial path explosion on dense cliques.

---

## 6. Query and Traversal APIs

| Method | Description |
| :--- | :--- |
| `get_module(module_identifier)` | Lookup module node by canonical ID, alias, or file path |
| `get_dependencies(module_identifier)` | Directly imported internal module IDs (sorted) |
| `get_dependents(module_identifier)` | Direct internal dependent module IDs (sorted) |
| `get_transitive_dependencies(module_id, max_depth, max_nodes)` | Bounded forward BFS traversal of dependencies |
| `get_transitive_dependents(module_id, max_depth, max_nodes)` | Bounded reverse BFS traversal of dependents |
| `get_edge(src, tgt)` | Consolidated dependency edge lookup |
| `get_strongly_connected_components(only_cyclic)` | Tarjan's SCC partition |
| `detect_cycles(max_cycles, max_depth)` | Deterministic elementary cycles |
| `is_in_cycle(module_identifier)` | Boolean check for cycle membership |
| `get_isolated_modules()` | Modules with 0 internal dependencies and 0 internal dependents |
| `get_external_dependencies(module_id)` | External imports originating from module |
| `get_unresolved_imports(module_id)` | Unresolved imports originating from module |
| `get_dependency_summary()` | Complete repository metrics summary |

---

## 7. Performance Benchmark (FORGE Repository)

Benchmark executed against the active FORGE codebase (269 Python files):
- **Graph Construction Time**: 2.594 seconds.
- **Total Internal Modules**: 269 modules.
- **Total Internal Dependency Edges**: 1,428 edges.
- **Total External Imports**: 1,844 (1,573 standard library, 271 third-party).
- **Unresolved / Ambiguous Imports**: 0 unresolved, 0 ambiguous.
- **Cycle Analysis**: Exactly 2 cyclic SCCs detected:
  1. `app.forge.repair.contracts` $\leftrightarrow$ `app.forge.repair.models`
  2. `app.forge.workflow.dossier_models` $\leftrightarrow$ `app.forge.workflow.models`
- **Isolated Modules**: 43 modules.
- **Traversal Latency**: $< 0.05\text{ ms}$ per transitive query.

---

## 8. Security and Isolation Compliance

1. **Zero Dynamic Execution**: No repository modules are loaded via `importlib` or executed. Analysis is 100% static AST parsing.
2. **Phase 10 Protected-Path Exclusion**: Protected files (`.env*`, `*.db*`, `contacts.json`, `*.log`, trusted Git mirrors) are strictly excluded before reading or parsing.
3. **Symlink and Containment Defenses**: Relative file paths are resolved through `assert_contained_path`, rejecting escaping symlinks fail-closed.
4. **Zero Fabrication**: External or unresolvable imports are never converted into spurious internal dependency edges.

---

## 9. Test Suite Verification

### Dedicated Unit Tests (`tests/unit/test_module_graph.py`)
24 tests covering:
1. Basic module dependency extraction.
2. Absolute imports.
3. Relative imports (same directory and parent directory).
4. Aliased imports.
5. Package `__init__.py` relationships.
6. Multiple imports from one module.
7. Duplicate edge consolidation with multiple evidence items.
8. Standard-library classification.
9. Third-party classification.
10. Unresolved imports.
11. Star import resolution.
12. Self-dependency cycles (length 1).
13. Two-module cycles (length 2).
14. Three-module cycles (length 3).
15. Long dependency cycles (length 6).
16. Multiple independent cycles in separate components.
17. Strongly connected components correctness via Tarjan's algorithm.
18. Deterministic cycle output across repeated indexing runs.
19. Transitive forward dependency queries.
20. Transitive reverse dependent queries.
21. Bounded traversal (`max_depth` and `max_nodes`).
22. Empty and disconnected graphs.
23. Syntax-error resilience.
24. Symlink containment and protected-file exclusion.

### Complete Phase 11 Suite
- `tests/unit/test_module_graph.py`: 24 passed.
- `tests/unit/test_repository_resolver.py`: 13 passed.
- `tests/unit/test_call_graph.py`: 23 passed.
- **Subtotal: 60 passed in 0.73s (100% pass rate)**.
