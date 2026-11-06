"""Forward and reverse call-graph engine for FORGE Phase 11.

Builds deterministic, cycle-safe call graphs across workspace Python files without code execution.
"""

from collections import deque
import builtins
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from forge.repository.graph_schemas import (
    CallEdge,
    CallGraphSummary,
    CallResolutionStatus,
    GraphNode,
    NodeKind,
)
from forge.repository.indexer import parse_python_file_details
from forge.repository.protection import is_protected_path
from forge.repository.resolver import WorkspaceImportResolver
from forge.repository.validation import assert_contained_path


_BUILTIN_FUNCTIONS: Set[str] = {
    name for name, obj in builtins.__dict__.items()
    if callable(obj) or isinstance(obj, type)
}


class CallGraph(BaseModel):
    """Repository-wide Python symbol call-graph."""
    nodes: Dict[str, GraphNode] = Field(default_factory=dict)
    edges: List[CallEdge] = Field(default_factory=list)
    forward_adjacency: Dict[str, List[str]] = Field(default_factory=dict)
    reverse_adjacency: Dict[str, List[str]] = Field(default_factory=dict)
    summary: CallGraphSummary = Field(default_factory=CallGraphSummary)

    def get_node(self, symbol_id: str) -> Optional[GraphNode]:
        """Lookup a graph node by symbol_id (node_id or qualified_name)."""
        if symbol_id in self.nodes:
            return self.nodes[symbol_id]
        # Fallback to qualified_name lookup
        for node in self.nodes.values():
            if node.qualified_name == symbol_id:
                return node
        return None

    def get_direct_callees(self, symbol_id: str) -> List[str]:
        """Return direct callee node IDs deterministically sorted."""
        node = self.get_node(symbol_id)
        if not node:
            return []
        callees = self.forward_adjacency.get(node.node_id, [])
        return sorted(list(set(callees)))

    def get_direct_callers(self, symbol_id: str) -> List[str]:
        """Return direct caller node IDs deterministically sorted."""
        node = self.get_node(symbol_id)
        if not node:
            return []
        callers = self.reverse_adjacency.get(node.node_id, [])
        return sorted(list(set(callers)))

    def get_callees(self, symbol_id: str, max_depth: int = 2, max_nodes: int = 100) -> List[str]:
        """Cycle-safe bounded BFS traversal of transitive callees."""
        return self._traverse(symbol_id, self.forward_adjacency, max_depth=max_depth, max_nodes=max_nodes)

    def get_callers(self, symbol_id: str, max_depth: int = 2, max_nodes: int = 100) -> List[str]:
        """Cycle-safe bounded BFS traversal of transitive callers."""
        return self._traverse(symbol_id, self.reverse_adjacency, max_depth=max_depth, max_nodes=max_nodes)

    def _traverse(self, start_id: str, adjacency: Dict[str, List[str]], max_depth: int, max_nodes: int) -> List[str]:
        """Deterministic, cycle-safe, bounded BFS traversal."""
        node = self.get_node(start_id)
        if not node or max_depth < 1 or max_nodes < 1:
            return []

        visited: Set[str] = {node.node_id}
        result: List[str] = []
        queue = deque([(node.node_id, 0)])

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

    def get_call_edges(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[CallEdge]:
        """Filter call edges by caller (source_id) or callee (target_id)."""
        filtered = []
        for edge in self.edges:
            if source_id is not None and edge.source_id != source_id:
                continue
            if target_id is not None and edge.target_id != target_id:
                continue
            filtered.append(edge)
        return filtered


def _find_method_in_hierarchy(
    start_file: str,
    class_name: str,
    method_name: str,
    all_nodes: Dict[str, GraphNode],
    class_bases_map: Dict[str, List[str]],
    file_resolved_imports: Dict[str, Dict[str, any]],
    visited_classes: Optional[Set[Tuple[str, str]]] = None,
) -> Tuple[Optional[str], Optional[str], CallResolutionStatus, str]:
    """Recursively search for a method along the class inheritance hierarchy.

    Returns: (target_id, target_qualified_name, resolution_status, resolution_reason)
    """
    if visited_classes is None:
        visited_classes = set()

    state_key = (start_file, class_name)
    if state_key in visited_classes:
        return None, None, CallResolutionStatus.UNRESOLVED, f"Circular inheritance detected on '{class_name}'"
    visited_classes.add(state_key)

    # 1. Direct method on current class
    cand_id = f"{start_file}:{class_name}.{method_name}"
    if cand_id in all_nodes:
        return (
            cand_id,
            all_nodes[cand_id].qualified_name,
            CallResolutionStatus.RESOLVED_INTERNAL,
            f"Resolved method '{method_name}' on class '{class_name}'",
        )

    # 2. Check base classes in MRO order
    base_list = class_bases_map.get(f"{start_file}:{class_name}", [])
    imports = file_resolved_imports.get(start_file, {})

    for base_name in base_list:
        # Case A: Base class defined in same file
        if f"{start_file}:{base_name}" in all_nodes:
            t_id, t_qual, t_stat, t_reason = _find_method_in_hierarchy(
                start_file, base_name, method_name, all_nodes, class_bases_map, file_resolved_imports, visited_classes
            )
            if t_stat in (CallResolutionStatus.RESOLVED_INTERNAL, CallResolutionStatus.RESOLVED_EXTERNAL):
                return t_id, t_qual, t_stat, f"Resolved method '{method_name}' via base '{base_name}': {t_reason}"

        # Case B: Base class imported from workspace file
        elif base_name in imports and imports[base_name].target_file:
            imp = imports[base_name]
            imported_file = imp.target_file
            imported_sym = imp.target_symbol or base_name
            t_id, t_qual, t_stat, t_reason = _find_method_in_hierarchy(
                imported_file, imported_sym, method_name, all_nodes, class_bases_map, file_resolved_imports, visited_classes
            )
            if t_stat in (CallResolutionStatus.RESOLVED_INTERNAL, CallResolutionStatus.RESOLVED_EXTERNAL):
                return t_id, t_qual, t_stat, f"Resolved method '{method_name}' via imported base '{base_name}' from '{imported_file}': {t_reason}"

        # Case C: Base class is external
        elif base_name in imports and imports[base_name].is_external:
            return None, None, CallResolutionStatus.RESOLVED_EXTERNAL, f"Method '{method_name}' inherited from external base class '{base_name}'"

    return None, None, CallResolutionStatus.UNRESOLVED, f"Method '{method_name}' not defined on class '{class_name}' or known base classes"


def build_call_graph(
    repo_root: Path,
    max_python_files: int = 500,
    max_file_size_bytes: int = 500_000,
) -> CallGraph:
    """Build a complete call graph across all eligible Python files in repo_root."""
    root = repo_root.resolve()
    resolver = WorkspaceImportResolver(root)

    # Step 1: Parse AST and collect symbols from all eligible files
    parsed_files: Dict[str, any] = {}
    all_nodes: Dict[str, GraphNode] = {}
    class_bases_map: Dict[str, List[str]] = {}  # "rel_path:ClassName" -> list of base class names

    processed_count = 0
    for py_file in root.rglob("*.py"):
        if is_protected_path(py_file, base_dir=root):
            continue

        try:
            resolved = assert_contained_path(py_file, root)
        except Exception:
            continue

        if is_protected_path(resolved, base_dir=root):
            continue

        parts = resolved.relative_to(root).parts
        if any(p in [".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", ".git"] for p in parts):
            continue

        if py_file.stat().st_size > max_file_size_bytes:
            continue

        visitor = parse_python_file_details(resolved, root)
        if visitor:
            rel_str = str(resolved.relative_to(root))
            parsed_files[rel_str] = visitor

            for node in visitor.nodes:
                all_nodes[node.node_id] = node

            for cls_name, bases in visitor.class_bases.items():
                class_bases_map[f"{rel_str}:{cls_name}"] = bases

        processed_count += 1
        if processed_count >= max_python_files:
            break

    # Step 2: Precompute resolved imports for each file
    file_resolved_imports: Dict[str, Dict[str, any]] = {}
    file_star_imports: Dict[str, List[str]] = {}

    for rel_path, visitor in parsed_files.items():
        import_map = {}
        star_targets = []
        for imp in visitor.imports:
            resolved_imp = resolver.resolve_import(rel_path, imp)
            import_map[resolved_imp.local_name] = resolved_imp
            if imp.name == "*" and resolved_imp.target_file:
                star_targets.append(resolved_imp.target_file)

        file_resolved_imports[rel_path] = import_map
        file_star_imports[rel_path] = star_targets

    # Step 3: Resolve each call site into a CallEdge
    edges: List[CallEdge] = []
    forward_adj: Dict[str, List[str]] = {}
    reverse_adj: Dict[str, List[str]] = {}

    res_internal = 0
    res_external = 0
    res_unresolved = 0
    res_ambiguous = 0
    res_dynamic = 0

    for rel_path, visitor in parsed_files.items():
        imports = file_resolved_imports.get(rel_path, {})
        stars = file_star_imports.get(rel_path, [])

        for cs in visitor.call_sites:
            target_id: Optional[str] = None
            target_qual_name: Optional[str] = None
            status: CallResolutionStatus = CallResolutionStatus.UNRESOLVED
            reason: str = ""

            caller_locals = visitor.callable_local_vars.get(cs.caller_id, set())

            # Case A: Dynamic call
            if cs.receiver_kind == "dynamic":
                status = CallResolutionStatus.DYNAMIC
                reason = f"Dynamic or indirect invocation expression '{cs.callee_expr}'"

            # Case B: self.method() or cls.method()
            elif cs.receiver_kind in ("self", "cls"):
                callee_name = cs.callee_name or ""
                caller_node = all_nodes.get(cs.caller_id)
                parent_class = None

                if caller_node and caller_node.kind == NodeKind.METHOD and caller_node.parent_scope:
                    # parent_scope contains the class name
                    parent_class = caller_node.parent_scope.split(".")[-1]

                if parent_class:
                    target_id, target_qual_name, status, reason = _find_method_in_hierarchy(
                        rel_path, parent_class, callee_name, all_nodes, class_bases_map, file_resolved_imports
                    )
                else:
                    status = CallResolutionStatus.UNRESOLVED
                    reason = f"Receiver '{cs.receiver_kind}' used outside class method scope"

            # Case C: Direct simple function call foo()
            elif cs.receiver_kind == "name":
                callee_name = cs.callee_name or cs.callee_expr

                # Shadowing check: Is callee_name a local parameter or variable in caller?
                if callee_name in caller_locals:
                    status = CallResolutionStatus.DYNAMIC
                    reason = f"Callable '{callee_name}' is shadowed by a local parameter or variable in '{cs.caller_id}'"

                # 1. Check local nested function in caller
                elif f"{cs.caller_id}.<locals>.{callee_name}" in all_nodes:
                    nested_id = f"{cs.caller_id}.<locals>.{callee_name}"
                    target_id = nested_id
                    target_qual_name = all_nodes[nested_id].qualified_name
                    status = CallResolutionStatus.RESOLVED_INTERNAL
                    reason = f"Resolved local nested function '{callee_name}'"

                # 2. Check recursive call to caller itself
                elif cs.caller_id in all_nodes and all_nodes[cs.caller_id].name == callee_name:
                    target_id = cs.caller_id
                    target_qual_name = all_nodes[cs.caller_id].qualified_name
                    status = CallResolutionStatus.RESOLVED_INTERNAL
                    reason = f"Recursive call to '{callee_name}'"

                # 3. Check same-file function or class
                elif f"{rel_path}:{callee_name}" in all_nodes:
                    cand_id = f"{rel_path}:{callee_name}"
                    target_id = cand_id
                    target_qual_name = all_nodes[cand_id].qualified_name
                    status = CallResolutionStatus.RESOLVED_INTERNAL
                    reason = f"Resolved symbol '{callee_name}' in current file"

                # 4. Check imports in current file
                elif callee_name in imports:
                    imp_rec = imports[callee_name]
                    if imp_rec.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL and imp_rec.target_file:
                        target_sym = imp_rec.target_symbol or callee_name
                        cand_id = f"{imp_rec.target_file}:{target_sym}"
                        if cand_id in all_nodes:
                            target_id = cand_id
                            target_qual_name = all_nodes[cand_id].qualified_name
                            status = CallResolutionStatus.RESOLVED_INTERNAL
                            reason = f"Resolved cross-file import '{callee_name}' -> '{cand_id}'"
                        else:
                            status = CallResolutionStatus.UNRESOLVED
                            reason = f"Imported symbol '{target_sym}' not found in target file '{imp_rec.target_file}'"
                    elif imp_rec.resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL:
                        status = CallResolutionStatus.RESOLVED_EXTERNAL
                        reason = f"External imported callable '{callee_name}'"
                    else:
                        status = imp_rec.resolution_status
                        reason = imp_rec.reason

                # 5. Check Python built-in functions
                elif callee_name in _BUILTIN_FUNCTIONS:
                    status = CallResolutionStatus.RESOLVED_EXTERNAL
                    reason = f"Python built-in callable '{callee_name}'"

                # 6. Check star imports
                elif stars:
                    matching_star_targets = []
                    for s_file in stars:
                        if f"{s_file}:{callee_name}" in all_nodes:
                            matching_star_targets.append(s_file)

                    if len(matching_star_targets) == 1:
                        target_file = matching_star_targets[0]
                        cand_id = f"{target_file}:{callee_name}"
                        target_id = cand_id
                        target_qual_name = all_nodes[cand_id].qualified_name
                        status = CallResolutionStatus.RESOLVED_INTERNAL
                        reason = f"Resolved via star-import from '{target_file}'"
                    elif len(matching_star_targets) > 1:
                        status = CallResolutionStatus.AMBIGUOUS
                        reason = f"Ambiguous symbol '{callee_name}' found in multiple star-imported files: {matching_star_targets}"
                    else:
                        status = CallResolutionStatus.UNRESOLVED
                        reason = f"Symbol '{callee_name}' not found in file, imports, or star-imports"
                else:
                    status = CallResolutionStatus.UNRESOLVED
                    reason = f"Symbol '{callee_name}' not defined in scope or imports"

            # Case D: Module-qualified call module.func()
            elif cs.receiver_kind == "module_or_instance":
                receiver = cs.receiver_name or ""
                callee_name = cs.callee_name or ""

                # Shadowing check: Is receiver a local parameter or variable in caller?
                if receiver in caller_locals:
                    status = CallResolutionStatus.UNRESOLVED
                    reason = f"Receiver '{receiver}' is a local variable with unknown type in '{cs.caller_id}'"
                elif receiver in imports:
                    imp_rec = imports[receiver]
                    if imp_rec.target_file:
                        cand_id = f"{imp_rec.target_file}:{callee_name}"
                        if cand_id in all_nodes:
                            target_id = cand_id
                            target_qual_name = all_nodes[cand_id].qualified_name
                            status = CallResolutionStatus.RESOLVED_INTERNAL
                            reason = f"Resolved qualified call on module '{receiver}' -> '{cand_id}'"
                        else:
                            status = CallResolutionStatus.UNRESOLVED
                            reason = f"Symbol '{callee_name}' not found in imported module file '{imp_rec.target_file}'"
                    elif imp_rec.is_external:
                        status = CallResolutionStatus.RESOLVED_EXTERNAL
                        reason = f"Qualified call on external module '{receiver}'"
                    else:
                        status = imp_rec.resolution_status
                        reason = imp_rec.reason
                else:
                    # Check if receiver is a known instantiated class in same file
                    if f"{rel_path}:{receiver}" in all_nodes:
                        inst_node = all_nodes[f"{rel_path}:{receiver}"]
                        if inst_node.kind == NodeKind.CLASS:
                            method_id = f"{rel_path}:{receiver}.{callee_name}"
                            if method_id in all_nodes:
                                target_id = method_id
                                target_qual_name = all_nodes[method_id].qualified_name
                                status = CallResolutionStatus.RESOLVED_INTERNAL
                                reason = f"Resolved class method call on '{receiver}'"
                    if status == CallResolutionStatus.UNRESOLVED:
                        reason = f"Receiver '{receiver}' cannot be statically resolved to a known module or class"

            # Case E: Chained or complex call
            else:
                status = CallResolutionStatus.UNRESOLVED
                reason = f"Chained or complex invocation expression '{cs.callee_expr}' cannot be statically resolved"

            # Create edge
            edge = CallEdge(
                source_id=cs.caller_id,
                target_id=target_id,
                source_file=rel_path,
                line_number=cs.line,
                col_offset=cs.col_offset,
                raw_call_expr=cs.raw_expr,
                resolution_status=status,
                resolution_reason=reason,
                target_qualified_name=target_qual_name,
            )
            edges.append(edge)

            # Update forward and reverse adjacency if target is resolved
            if target_id:
                if cs.caller_id not in forward_adj:
                    forward_adj[cs.caller_id] = []
                forward_adj[cs.caller_id].append(target_id)

                if target_id not in reverse_adj:
                    reverse_adj[target_id] = []
                reverse_adj[target_id].append(cs.caller_id)

            # Stats
            if status == CallResolutionStatus.RESOLVED_INTERNAL:
                res_internal += 1
            elif status == CallResolutionStatus.RESOLVED_EXTERNAL:
                res_external += 1
            elif status == CallResolutionStatus.UNRESOLVED:
                res_unresolved += 1
            elif status == CallResolutionStatus.AMBIGUOUS:
                res_ambiguous += 1
            elif status == CallResolutionStatus.DYNAMIC:
                res_dynamic += 1

    # Sort adjacency lists deterministically
    sorted_forward = {k: sorted(list(set(v))) for k, v in sorted(forward_adj.items())}
    sorted_reverse = {k: sorted(list(set(v))) for k, v in sorted(reverse_adj.items())}

    summary = CallGraphSummary(
        total_nodes=len(all_nodes),
        total_edges=len(edges),
        resolved_internal_calls=res_internal,
        resolved_external_calls=res_external,
        unresolved_calls=res_unresolved,
        ambiguous_calls=res_ambiguous,
        dynamic_calls=res_dynamic,
    )

    return CallGraph(
        nodes=all_nodes,
        edges=edges,
        forward_adjacency=sorted_forward,
        reverse_adjacency=sorted_reverse,
        summary=summary,
    )
