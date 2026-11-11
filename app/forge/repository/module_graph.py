"""Module Dependency Graph and Cycle Detection engine for FORGE Phase 11.

Builds deterministic, cycle-safe module dependency graphs across workspace
Python files without executing code, strictly enforcing Phase 10 isolation boundaries.
"""

from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field, PrivateAttr

from forge.repository.graph_schemas import (
    CallResolutionStatus,
    DependencyCycle,
    ExternalImportRecord,
    ImportEvidence,
    ImportRecord,
    ModuleDependencyGraphSummary,
    ModuleEdge,
    ModuleNode,
    StronglyConnectedComponent,
    UnresolvedImportRecord,
)
from forge.repository.indexer import discover_workspace_python_files, parse_python_file_details
from forge.repository.protection import is_protected_path
from forge.repository.resolver import WorkspaceImportResolver
from forge.repository.validation import assert_contained_path


class ModuleDependencyGraph(BaseModel):
    """Repository-wide Python module dependency graph and cycle analysis."""
    nodes: Dict[str, ModuleNode] = Field(default_factory=dict)
    edges: Dict[str, ModuleEdge] = Field(default_factory=dict)
    forward_adjacency: Dict[str, List[str]] = Field(default_factory=dict)
    reverse_adjacency: Dict[str, List[str]] = Field(default_factory=dict)
    external_imports: List[ExternalImportRecord] = Field(default_factory=list)
    unresolved_imports: List[UnresolvedImportRecord] = Field(default_factory=list)
    summary: ModuleDependencyGraphSummary = Field(default_factory=ModuleDependencyGraphSummary)

    # Lookup indices
    alias_to_module: Dict[str, str] = Field(default_factory=dict)
    file_to_module: Dict[str, str] = Field(default_factory=dict)
    _cached_cycles: Dict[Tuple[int, int], List[DependencyCycle]] = PrivateAttr(default_factory=dict)

    def get_module(self, module_identifier: str) -> Optional[ModuleNode]:
        """Lookup a module node by module_id, file path, or known module alias."""
        if module_identifier in self.nodes:
            return self.nodes[module_identifier]
        if module_identifier in self.alias_to_module:
            canonical = self.alias_to_module[module_identifier]
            if canonical in self.nodes:
                return self.nodes[canonical]
        if module_identifier in self.file_to_module:
            canonical = self.file_to_module[module_identifier]
            if canonical in self.nodes:
                return self.nodes[canonical]

        # Normalized path matching
        normalized = module_identifier.replace("\\", "/").strip()
        for node in self.nodes.values():
            if node.file_path == normalized:
                return node

        # Suffix matching for dotted module names (e.g. 'repository.indexer' -> 'app.forge.repository.indexer')
        for node in self.nodes.values():
            if node.module_id == module_identifier or node.module_id.endswith(f".{module_identifier}"):
                return node

        return None

    def get_dependencies(self, module_identifier: str) -> List[str]:
        """Return direct internal dependency module IDs imported by the given module."""
        node = self.get_module(module_identifier)
        if not node:
            return []
        deps = self.forward_adjacency.get(node.module_id, [])
        return sorted(list(set(deps)))

    def get_dependents(self, module_identifier: str) -> List[str]:
        """Return direct internal dependent module IDs that import the given module."""
        node = self.get_module(module_identifier)
        if not node:
            return []
        dependents = self.reverse_adjacency.get(node.module_id, [])
        return sorted(list(set(dependents)))

    def get_transitive_dependencies(
        self,
        module_identifier: str,
        max_depth: int = 10,
        max_nodes: int = 500,
    ) -> List[str]:
        """Cycle-safe bounded BFS traversal of transitive dependencies (modules imported directly or indirectly)."""
        return self._traverse(module_identifier, self.forward_adjacency, max_depth=max_depth, max_nodes=max_nodes)

    def get_transitive_dependents(
        self,
        module_identifier: str,
        max_depth: int = 10,
        max_nodes: int = 500,
    ) -> List[str]:
        """Cycle-safe bounded BFS traversal of transitive dependents (modules that depend on this module)."""
        return self._traverse(module_identifier, self.reverse_adjacency, max_depth=max_depth, max_nodes=max_nodes)

    def _traverse(
        self,
        start_identifier: str,
        adjacency: Dict[str, List[str]],
        max_depth: int,
        max_nodes: int,
    ) -> List[str]:
        """Deterministic, cycle-safe, bounded BFS traversal across the module graph."""
        node = self.get_module(start_identifier)
        if not node or max_depth < 1 or max_nodes < 1:
            return []

        visited: Set[str] = {node.module_id}
        result: List[str] = []
        queue = deque([(node.module_id, 0)])

        while queue and len(result) < max_nodes:
            curr_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            neighbors = sorted(list(set(adjacency.get(curr_id, []))))
            for neighbor in neighbors:
                if neighbor not in visited:
                    visited.add(neighbor)
                    result.append(neighbor)
                    if len(result) >= max_nodes:
                        break
                    queue.append((neighbor, depth + 1))

        return result

    def get_edge(self, source_identifier: str, target_identifier: str) -> Optional[ModuleEdge]:
        """Lookup a directed dependency edge between two modules."""
        src_node = self.get_module(source_identifier)
        tgt_node = self.get_module(target_identifier)
        if not src_node or not tgt_node:
            return None
        edge_key = f"{src_node.module_id}->{tgt_node.module_id}"
        return self.edges.get(edge_key)

    def get_strongly_connected_components(self, only_cyclic: bool = False) -> List[StronglyConnectedComponent]:
        """Compute strongly connected components (SCCs) using Tarjan's algorithm.

        The graph is partitioned into maximal subgraphs where every module is reachable
        from every other module. A component is cyclic if it contains > 1 module or a direct self-loop.
        """
        index_counter = 0
        indices: Dict[str, int] = {}
        lowlinks: Dict[str, int] = {}
        on_stack: Set[str] = set()
        stack: List[str] = []
        sccs: List[StronglyConnectedComponent] = []

        all_modules = sorted(list(self.nodes.keys()))

        def strongconnect(u: str) -> None:
            nonlocal index_counter
            indices[u] = index_counter
            lowlinks[u] = index_counter
            index_counter += 1
            stack.append(u)
            on_stack.add(u)

            neighbors = sorted(self.forward_adjacency.get(u, []))
            for v in neighbors:
                if v not in indices:
                    strongconnect(v)
                    lowlinks[u] = min(lowlinks[u], lowlinks[v])
                elif v in on_stack:
                    lowlinks[u] = min(lowlinks[u], indices[v])

            if lowlinks[u] == indices[u]:
                component: List[str] = []
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    component.append(w)
                    if w == u:
                        break

                sorted_comp = sorted(component)
                is_cyclic = False
                if len(sorted_comp) > 1:
                    is_cyclic = True
                elif len(sorted_comp) == 1:
                    single = sorted_comp[0]
                    if single in self.forward_adjacency.get(single, []):
                        is_cyclic = True

                scc_id = f"scc:{sorted_comp[0]}"
                sccs.append(
                    StronglyConnectedComponent(
                        scc_id=scc_id,
                        modules=sorted_comp,
                        size=len(sorted_comp),
                        is_cyclic=is_cyclic,
                    )
                )

        for mod_id in all_modules:
            if mod_id not in indices:
                strongconnect(mod_id)

        # Deterministic sort: cyclic first, then by first module name
        sccs.sort(key=lambda c: (not c.is_cyclic, c.modules[0] if c.modules else ""))

        if only_cyclic:
            return [c for c in sccs if c.is_cyclic]
        return sccs

    def detect_cycles(self, max_cycles: int = 500, max_depth: int = 50) -> List[DependencyCycle]:
        """Detect and return deterministic elementary dependency cycles within cyclic components.

        Reports:
        - Direct self-dependencies (length 1, [A, A])
        - Two-module cycles (length 2, [A, B, A])
        - Multi-module cycles (length >= 3, [A, B, ..., A])
        All cycle representations are canonicalized to start and end with their lexicographically smallest module.
        """
        cache_key = (max_cycles, max_depth)
        if cache_key in self._cached_cycles:
            return list(self._cached_cycles[cache_key])

        cyclic_sccs = self.get_strongly_connected_components(only_cyclic=True)
        detected_cycles: List[DependencyCycle] = []
        seen_cycle_signatures: Set[str] = set()

        for scc in cyclic_sccs:
            scc_set = set(scc.modules)
            sorted_scc = list(scc.modules)

            # 1. Direct self-dependency
            for u in sorted_scc:
                if u in self.forward_adjacency.get(u, []):
                    cycle_sig = f"{u}->{u}"
                    if cycle_sig not in seen_cycle_signatures:
                        seen_cycle_signatures.add(cycle_sig)
                        detected_cycles.append(
                            DependencyCycle(
                                cycle_id=f"cycle:{cycle_sig}",
                                modules=[u, u],
                                length=1,
                                is_self_cycle=True,
                                edges=[(u, u)],
                            )
                        )
                        if len(detected_cycles) >= max_cycles:
                            break

            if len(detected_cycles) >= max_cycles:
                break

            # 2. Multi-module cycles (size > 1)
            if len(sorted_scc) > 1:
                for start_node in sorted_scc:
                    path = [start_node]
                    visited_in_path = {start_node}

                    def dfs(curr: str) -> None:
                        if len(detected_cycles) >= max_cycles:
                            return
                        if len(path) > max_depth:
                            return

                        neighbors = sorted(self.forward_adjacency.get(curr, []))
                        for nxt in neighbors:
                            if nxt not in scc_set:
                                continue
                            if nxt < start_node:
                                # Canonicalization: cycle must start with smallest vertex
                                continue

                            if nxt == start_node and len(path) > 1:
                                # Closed cycle found
                                cycle_modules = path + [start_node]
                                cycle_sig = "->".join(cycle_modules)
                                if cycle_sig not in seen_cycle_signatures:
                                    seen_cycle_signatures.add(cycle_sig)
                                    cycle_edges = [
                                        (cycle_modules[i], cycle_modules[i + 1])
                                        for i in range(len(cycle_modules) - 1)
                                    ]
                                    detected_cycles.append(
                                        DependencyCycle(
                                            cycle_id=f"cycle:{cycle_sig}",
                                            modules=cycle_modules,
                                            length=len(path),
                                            is_self_cycle=False,
                                            edges=cycle_edges,
                                        )
                                    )
                            elif nxt not in visited_in_path:
                                visited_in_path.add(nxt)
                                path.append(nxt)
                                dfs(nxt)
                                path.pop()
                                visited_in_path.remove(nxt)

                    dfs(start_node)
                    if len(detected_cycles) >= max_cycles:
                        break

        # Sort cycles deterministically by length, then by cycle_id
        detected_cycles.sort(key=lambda c: (c.length, c.cycle_id))
        self._cached_cycles[(max_cycles, max_depth)] = list(detected_cycles)
        return detected_cycles

    def is_in_cycle(self, module_identifier: str) -> bool:
        """Return True if the module participates in any dependency cycle."""
        node = self.get_module(module_identifier)
        if not node:
            return False
        cyclic_sccs = self.get_strongly_connected_components(only_cyclic=True)
        for scc in cyclic_sccs:
            if node.module_id in scc.modules:
                return True
        return False

    def get_external_dependencies(self, module_identifier: str) -> List[ExternalImportRecord]:
        """Return all external import records originating from the given module."""
        node = self.get_module(module_identifier)
        if not node:
            return []
        return [
            rec for rec in self.external_imports
            if rec.importing_module == node.module_id or rec.importing_file == node.file_path
        ]

    def get_unresolved_imports(self, module_identifier: str) -> List[UnresolvedImportRecord]:
        """Return all unresolved import records originating from the given module."""
        node = self.get_module(module_identifier)
        if not node:
            return []
        return [
            rec for rec in self.unresolved_imports
            if rec.importing_module == node.module_id or rec.importing_file == node.file_path
        ]

    def get_isolated_modules(self) -> List[str]:
        """Return module IDs that have zero internal dependencies and zero internal dependents."""
        isolated = [
            node.module_id for node in self.nodes.values()
            if not node.internal_dependencies and not node.internal_dependents
        ]
        return sorted(isolated)

    def get_dependency_summary(self) -> ModuleDependencyGraphSummary:
        """Return the statistical summary of the dependency graph."""
        return self.summary


def build_module_dependency_graph(
    repo_root: Path,
    max_python_files: int = 1000,
    max_file_size_bytes: int = 1_000_000,
) -> ModuleDependencyGraph:
    """Build a deterministic module dependency graph for a workspace repository.

    Extracts import statements statically via AST parsing, resolves module destinations
    via WorkspaceImportResolver, and performs Tarjan SCC cycle analysis without code execution.
    """
    root = repo_root.resolve()
    if not root.exists() or not root.is_dir():
        return ModuleDependencyGraph()

    # 1. Discover all candidate workspace Python files via pruned os.walk
    discovered_files = discover_workspace_python_files(
        root,
        max_python_files=max_python_files,
        max_file_size_bytes=max_file_size_bytes,
        exclude_forge=False,
    )
    resolver = WorkspaceImportResolver(root, known_python_files=discovered_files)

    # 2. Initialize ModuleNode for each discovered file
    nodes: Dict[str, ModuleNode] = {}
    file_to_module: Dict[str, str] = {}
    alias_to_module: Dict[str, str] = {}

    for file_path in discovered_files:
        rel_str = str(file_path.relative_to(root))
        mod_id = resolver.file_to_module.get(rel_str) or resolver.get_module_name_for_file(rel_str)
        if not mod_id:
            mod_id = Path(rel_str).stem

        file_to_module[rel_str] = mod_id
        is_pkg = file_path.name == "__init__.py"

        docstring = None
        total_lines = 0
        try:
            raw_bytes = file_path.read_bytes()
            if raw_bytes:
                total_lines = raw_bytes.count(b"\n") + (0 if raw_bytes.endswith(b"\n") else 1)
        except Exception:
            pass

        node = ModuleNode(
            module_id=mod_id,
            file_path=rel_str,
            is_package=is_pkg,
            docstring=docstring,
            total_lines=total_lines,
        )
        nodes[mod_id] = node

    # Register known aliases from resolver (e.g. 'forge.repository.indexer' -> 'app.forge.repository.indexer')
    for alias_mod, rel_file in resolver.module_to_file.items():
        if rel_file in file_to_module:
            canonical_mod = file_to_module[rel_file]
            if alias_mod != canonical_mod:
                alias_to_module[alias_mod] = canonical_mod

    # 3. Extract imports and build dependency edges
    edge_map: Dict[str, ModuleEdge] = {}
    external_imports: List[ExternalImportRecord] = []
    unresolved_imports: List[UnresolvedImportRecord] = []
    ambiguous_count = 0

    for file_path in discovered_files:
        rel_str = str(file_path.relative_to(root))
        source_mod_id = file_to_module.get(rel_str)
        if not source_mod_id or source_mod_id not in nodes:
            continue

        visitor = parse_python_file_details(file_path, root)
        if not visitor:
            continue

        # Update docstring if found from visitor
        if visitor.symbols:
            for sym in visitor.symbols:
                if sym.symbol_type == "class" or sym.symbol_type == "function":
                    break

        for imp in visitor.imports:
            # Determine import type
            if imp.name == "*":
                import_type = "star"
            elif imp.level > 0:
                import_type = "relative"
            elif imp.from_module:
                import_type = "from"
            else:
                import_type = "direct"

            resolved = resolver.resolve_import(rel_str, imp)

            if resolved.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL and resolved.target_file:
                target_mod_id = file_to_module.get(resolved.target_file)
                if not target_mod_id:
                    target_mod_id = resolver.get_module_name_for_file(resolved.target_file)

                if target_mod_id and target_mod_id in nodes:
                    edge_key = f"{source_mod_id}->{target_mod_id}"
                    evidence = ImportEvidence(
                        importing_file=rel_str,
                        line_number=imp.line,
                        imported_name=imp.name,
                        alias=imp.alias,
                        from_module=imp.from_module,
                        level=imp.level,
                        import_type=import_type,
                        resolution_status=resolved.resolution_status,
                        resolution_reason=resolved.reason,
                    )

                    if edge_key in edge_map:
                        edge = edge_map[edge_key]
                        edge.evidence.append(evidence)
                        edge.weight += 1
                    else:
                        is_self = source_mod_id == target_mod_id
                        edge_map[edge_key] = ModuleEdge(
                            source_module=source_mod_id,
                            target_module=target_mod_id,
                            source_file=rel_str,
                            target_file=resolved.target_file,
                            evidence=[evidence],
                            weight=1,
                            is_self_dependency=is_self,
                        )

            elif resolved.resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL:
                pkg_name = imp.from_module.split(".")[0] if imp.from_module else imp.name.split(".")[0]
                is_std = resolver.is_stdlib_module(pkg_name)
                ext_rec = ExternalImportRecord(
                    importing_module=source_mod_id,
                    importing_file=rel_str,
                    package_name=pkg_name,
                    imported_name=imp.name,
                    is_stdlib=is_std,
                    line_number=imp.line,
                )
                external_imports.append(ext_rec)
                if pkg_name not in nodes[source_mod_id].external_dependencies:
                    nodes[source_mod_id].external_dependencies.append(pkg_name)

            elif resolved.resolution_status == CallResolutionStatus.AMBIGUOUS:
                ambiguous_count += 1

            else:
                unres_rec = UnresolvedImportRecord(
                    importing_module=source_mod_id,
                    importing_file=rel_str,
                    imported_name=imp.name,
                    from_module=imp.from_module,
                    level=imp.level,
                    line_number=imp.line,
                    reason=resolved.reason,
                )
                unresolved_imports.append(unres_rec)

    # 4. Construct adjacency maps and populate node dependency lists
    forward_adjacency: Dict[str, List[str]] = {m: [] for m in nodes}
    reverse_adjacency: Dict[str, List[str]] = {m: [] for m in nodes}

    for edge in edge_map.values():
        forward_adjacency[edge.source_module].append(edge.target_module)
        reverse_adjacency[edge.target_module].append(edge.source_module)

    for mod_id, node in nodes.items():
        node.internal_dependencies = sorted(list(set(forward_adjacency.get(mod_id, []))))
        node.internal_dependents = sorted(list(set(reverse_adjacency.get(mod_id, []))))
        node.external_dependencies = sorted(node.external_dependencies)
        node.out_degree = len(node.internal_dependencies)
        node.in_degree = len(node.internal_dependents)
        forward_adjacency[mod_id] = sorted(list(set(forward_adjacency.get(mod_id, []))))
        reverse_adjacency[mod_id] = sorted(list(set(reverse_adjacency.get(mod_id, []))))

    # 5. Build preliminary graph to run SCC and cycle detection
    graph = ModuleDependencyGraph(
        nodes=nodes,
        edges=edge_map,
        forward_adjacency=forward_adjacency,
        reverse_adjacency=reverse_adjacency,
        external_imports=external_imports,
        unresolved_imports=unresolved_imports,
        alias_to_module=alias_to_module,
        file_to_module=file_to_module,
    )

    sccs = graph.get_strongly_connected_components()
    cyclic_sccs = [s for s in sccs if s.is_cyclic]
    cycles = graph.detect_cycles()

    cyclic_module_set: Set[str] = set()
    for s in cyclic_sccs:
        cyclic_module_set.update(s.modules)

    isolated_count = sum(
        1 for n in nodes.values() if not n.internal_dependencies and not n.internal_dependents
    )

    stdlib_count = sum(1 for e in external_imports if e.is_stdlib)
    third_party_count = sum(1 for e in external_imports if not e.is_stdlib)

    summary = ModuleDependencyGraphSummary(
        total_modules=len(nodes),
        total_internal_edges=len(edge_map),
        total_external_imports=len(external_imports),
        total_stdlib_imports=stdlib_count,
        total_third_party_imports=third_party_count,
        total_unresolved_imports=len(unresolved_imports),
        total_ambiguous_imports=ambiguous_count,
        total_cycles=len(cycles),
        total_sccs=len(sccs),
        cyclic_sccs=len(cyclic_sccs),
        modules_in_cycles=len(cyclic_module_set),
        isolated_modules=isolated_count,
    )
    graph.summary = summary
    return graph
