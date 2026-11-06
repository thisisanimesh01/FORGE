"""Core Change-Impact Analysis Engine for FORGE Phase 11 Milestone 4.

Computes reverse invocation and module dependency propagation to identify
which repository components may be affected by changes to files, modules, classes,
or functions without executing code or compromising Phase 10 isolation boundaries.
"""

from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.analysis.impact.extractor import (
    extract_changeset_from_diff,
    extract_changeset_from_files,
)
from forge.analysis.impact.schemas import (
    ChangeSet,
    ChangeType,
    EntityType,
    ImpactClassification,
    ImpactPathStep,
    ImpactReport,
    ImpactSummary,
    ImpactedEntity,
    SymbolChange,
)
from forge.repository.call_graph import CallGraph, build_call_graph
from forge.repository.graph_schemas import CallResolutionStatus, NodeKind
from forge.repository.module_graph import ModuleDependencyGraph, build_module_dependency_graph
from forge.repository.protection import is_protected_path


def _classification_rank(classification: ImpactClassification) -> int:
    """Deterministic ranking for impact severity sorting."""
    ranks = {
        ImpactClassification.UNRESOLVED: 0,
        ImpactClassification.DIRECT: 1,
        ImpactClassification.TRANSITIVE: 2,
        ImpactClassification.POTENTIAL: 3,
        ImpactClassification.UNAFFECTED: 4,
    }
    return ranks.get(classification, 5)


def _file_path_to_module_id(file_path: str) -> str:
    """Normalize workspace file path to dotted Python module identifier."""
    p = str(file_path).replace("\\", "/").strip("/")
    if p.endswith(".py"):
        p = p[:-3]
    if p.endswith("/__init__"):
        p = p[:-9]
    return p.replace("/", ".")


class ChangeImpactAnalyzer:
    """Deterministic, evidence-backed change-impact engine.

    Reuses M2 CallGraph and M3 ModuleDependencyGraph to trace reverse invocation
    and module dependency relationships for file and symbol changesets.
    """

    def __init__(
        self,
        repo_root: Path,
        call_graph: Optional[CallGraph] = None,
        module_graph: Optional[ModuleDependencyGraph] = None,
        max_depth: int = 5,
        max_nodes: int = 500,
        incremental_index: Optional[Any] = None,
    ):
        self.repo_root = repo_root.resolve()
        self.max_depth = max(1, max_depth)
        self.max_nodes = max(1, max_nodes)
        self.incremental_index = incremental_index

        # Lazy / eager graph initialization
        self._call_graph = call_graph
        self._module_graph = module_graph

    @property
    def call_graph(self) -> CallGraph:
        """Lazily build or return the symbol call graph."""
        if self._call_graph is None:
            if self.incremental_index is not None:
                self._call_graph = self.incremental_index.get_call_graph()
            else:
                self._call_graph = build_call_graph(self.repo_root)
        return self._call_graph

    @property
    def module_graph(self) -> ModuleDependencyGraph:
        """Lazily build or return the module dependency graph."""
        if self._module_graph is None:
            if self.incremental_index is not None:
                self._module_graph = self.incremental_index.get_module_graph()
            else:
                self._module_graph = build_module_dependency_graph(self.repo_root)
        return self._module_graph

    def analyze_diff(self, diff_text: str) -> ImpactReport:
        """Perform change-impact analysis from unified diff text."""
        changeset = extract_changeset_from_diff(
            diff_text,
            self.repo_root,
            call_graph=self.call_graph,
        )
        return self.analyze(changeset)

    def analyze_file_paths(self, file_paths: List[str]) -> ImpactReport:
        """Perform change-impact analysis from a list of workspace-relative file paths."""
        file_items = [{"path": p, "change_type": "modified"} for p in file_paths]
        changeset = extract_changeset_from_files(
            file_items,
            self.repo_root,
            call_graph=self.call_graph,
        )
        return self.analyze(changeset)

    def analyze(
        self,
        changeset: ChangeSet,
        max_depth: Optional[int] = None,
        max_nodes: Optional[int] = None,
    ) -> ImpactReport:
        """Analyze a structured ChangeSet and compute the impact report."""
        effective_max_depth = self.max_depth if max_depth is None else max(0, max_depth)
        effective_max_nodes = self.max_nodes if max_nodes is None else max(0, max_nodes)

        warnings: List[str] = []
        impacted_map: Dict[str, ImpactedEntity] = {}
        is_truncated = False
        truncation_reason: Optional[str] = None

        # Filter out any protected paths that may have entered the changeset
        clean_files = []
        for fc in changeset.files:
            if is_protected_path(fc.path, base_dir=self.repo_root):
                warnings.append(f"Protected path '{fc.path}' excluded from change-impact analysis")
            else:
                clean_files.append(fc)
        changeset.files = clean_files

        clean_symbols = []
        for sc in changeset.symbols:
            if is_protected_path(sc.file_path, base_dir=self.repo_root):
                warnings.append(f"Symbol '{sc.symbol_id}' in protected file excluded")
            else:
                clean_symbols.append(sc)
        changeset.symbols = clean_symbols

        if not changeset.files and not changeset.symbols:
            return ImpactReport(
                changeset=changeset,
                impacted_entities=[],
                summary=ImpactSummary(),
                warnings=warnings,
            )

        def record_impact(entity: ImpactedEntity) -> None:
            nonlocal is_truncated, truncation_reason
            if len(impacted_map) >= effective_max_nodes and entity.entity_id not in impacted_map:
                if not is_truncated:
                    is_truncated = True
                    truncation_reason = f"Node budget exceeded (capped at {effective_max_nodes} entities)"
                    warnings.append(f"Analysis truncated: {truncation_reason}")
                return

            if entity.entity_id in impacted_map:
                existing = impacted_map[entity.entity_id]
                # Merge: preserve trigger changes
                for tc in entity.trigger_changes:
                    if tc not in existing.trigger_changes:
                        existing.trigger_changes.append(tc)

                # Upgrade if shorter depth or higher severity classification
                if entity.depth < existing.depth:
                    existing.depth = entity.depth
                    existing.impact_path = entity.impact_path
                    existing.path_steps = entity.path_steps
                    existing.explanation = entity.explanation

                if _classification_rank(entity.classification) < _classification_rank(existing.classification):
                    existing.classification = entity.classification
                    existing.confidence = max(existing.confidence, entity.confidence)
            else:
                impacted_map[entity.entity_id] = entity

        # -----------------------------------------------------------------
        # 1. Propagate Symbol Changes (Call-Graph Reverse Traversal)
        # -----------------------------------------------------------------
        for sym_change in changeset.symbols:
            s_id = sym_change.symbol_id
            is_deletion = sym_change.change_type in (ChangeType.DELETED, ChangeType.RENAMED)

            target_ids = {s_id}
            if sym_change.old_symbol_id:
                target_ids.add(sym_change.old_symbol_id)

            target_names = {sym_change.name}
            if sym_change.old_symbol_id:
                target_names.add(sym_change.old_symbol_id.split(":")[-1].split(".")[-1])

            # Direct callers via call graph
            direct_callers: Set[str] = set()
            for t_id in target_ids:
                direct_callers.update(self.call_graph.get_direct_callers(t_id))

            for caller_id in direct_callers:
                if caller_id in target_ids:
                    continue  # Skip self recursion

                caller_node = self.call_graph.get_node(caller_id)
                caller_file = caller_node.file_path if caller_node else sym_change.file_path
                caller_line = caller_node.start_line if caller_node else None

                step = ImpactPathStep(
                    from_entity=s_id,
                    to_entity=caller_id,
                    relationship="calls",
                    file_path=caller_file,
                    line_number=caller_line,
                )

                if is_deletion:
                    classification = ImpactClassification.UNRESOLVED
                    conf = 0.95
                    expl = (
                        f"Direct caller of {sym_change.change_type.value} symbol '{s_id}'; "
                        f"invocation will fail unless updated"
                    )
                else:
                    classification = ImpactClassification.DIRECT
                    conf = 1.0
                    expl = f"Direct static caller of {sym_change.change_type.value} symbol '{s_id}'"

                record_impact(
                    ImpactedEntity(
                        entity_id=caller_id,
                        entity_type=_node_kind_to_entity_type(caller_node.kind if caller_node else NodeKind.FUNCTION),
                        file_path=caller_file,
                        line_number=caller_line,
                        classification=classification,
                        depth=1,
                        impact_path=[s_id, caller_id],
                        path_steps=[step],
                        trigger_changes=[s_id],
                        relationship_type="direct_caller",
                        confidence=conf,
                        explanation=expl,
                    )
                )

            # Additional search across call edges for deleted/renamed symbols or unresolved calls
            for edge in self.call_graph.edges:
                if edge.source_id in target_ids:
                    continue

                is_target_call = False
                if edge.target_id in target_ids:
                    is_target_call = True
                elif edge.target_qualified_name and (
                    edge.target_qualified_name == sym_change.qualified_name or
                    any(edge.target_qualified_name.endswith(f".{n}") for n in target_names)
                ):
                    is_target_call = True
                elif (
                    is_deletion and
                    any(edge.raw_call_expr == f"{n}()" or edge.raw_call_expr.startswith(f"{n}(") for n in target_names)
                ):
                    is_target_call = True

                if is_target_call and edge.source_id not in direct_callers:
                    caller_node = self.call_graph.get_node(edge.source_id)
                    caller_file = caller_node.file_path if caller_node else edge.source_file
                    record_impact(
                        ImpactedEntity(
                            entity_id=edge.source_id,
                            entity_type=_node_kind_to_entity_type(caller_node.kind if caller_node else NodeKind.FUNCTION),
                            file_path=caller_file,
                            line_number=edge.line_number,
                            classification=ImpactClassification.UNRESOLVED if is_deletion else ImpactClassification.DIRECT,
                            depth=1,
                            impact_path=[s_id, edge.source_id],
                            path_steps=[
                                ImpactPathStep(
                                    from_entity=s_id,
                                    to_entity=edge.source_id,
                                    relationship="calls",
                                    file_path=caller_file,
                                    line_number=edge.line_number,
                                )
                            ],
                            trigger_changes=[s_id],
                            relationship_type="caller_of_deleted_or_renamed_symbol" if is_deletion else "direct_caller",
                            confidence=0.95 if is_deletion else 0.90,
                            explanation=f"Call site '{edge.raw_call_expr}' targets {sym_change.change_type.value} symbol '{s_id}'",
                        )
                    )
                    direct_callers.add(edge.source_id)

            # Transitive callers (depth >= 2)
            visited_callers: Set[str] = set(target_ids) | set(direct_callers)
            queue: deque = deque([(c_id, 1, [s_id, c_id], [
                ImpactPathStep(from_entity=s_id, to_entity=c_id, relationship="calls")
            ]) for c_id in direct_callers if c_id not in target_ids])

            while queue and len(impacted_map) < effective_max_nodes:
                curr_id, depth, path, steps = queue.popleft()
                if depth >= effective_max_depth:
                    continue

                upstream_callers = self.call_graph.get_direct_callers(curr_id)
                for upstream_id in upstream_callers:
                    if upstream_id not in visited_callers:
                        visited_callers.add(upstream_id)
                        up_node = self.call_graph.get_node(upstream_id)
                        up_file = up_node.file_path if up_node else sym_change.file_path
                        up_line = up_node.start_line if up_node else None

                        new_step = ImpactPathStep(
                            from_entity=curr_id,
                            to_entity=upstream_id,
                            relationship="calls",
                            file_path=up_file,
                            line_number=up_line,
                        )
                        new_path = path + [upstream_id]
                        new_steps = steps + [new_step]

                        conf = round(0.95 ** depth, 3)
                        expl = (
                            f"Transitive caller at depth {depth + 1} reached via invocation chain: "
                            f"{' -> '.join(new_path)}"
                        )

                        record_impact(
                            ImpactedEntity(
                                entity_id=upstream_id,
                                entity_type=_node_kind_to_entity_type(up_node.kind if up_node else NodeKind.FUNCTION),
                                file_path=up_file,
                                line_number=up_line,
                                classification=ImpactClassification.TRANSITIVE,
                                depth=depth + 1,
                                impact_path=new_path,
                                path_steps=new_steps,
                                trigger_changes=[s_id],
                                relationship_type="transitive_caller",
                                confidence=conf,
                                explanation=expl,
                            )
                        )
                        queue.append((upstream_id, depth + 1, new_path, new_steps))

            # Enclosing class impact
            if sym_change.entity_type == EntityType.METHOD:
                class_id = None
                sym_node = self.call_graph.get_node(s_id)
                if sym_node and sym_node.parent_scope:
                    class_id = f"{sym_node.file_path}:{sym_node.parent_scope}"
                elif "." in s_id:
                    class_id = s_id.rsplit(".", 1)[0]

                if class_id:
                    class_node = self.call_graph.get_node(class_id)
                    record_impact(
                        ImpactedEntity(
                            entity_id=class_id,
                            entity_type=EntityType.CLASS,
                            file_path=sym_change.file_path,
                            line_number=class_node.start_line if class_node else None,
                            classification=ImpactClassification.DIRECT,
                            depth=1,
                            impact_path=[s_id, class_id],
                            path_steps=[ImpactPathStep(from_entity=s_id, to_entity=class_id, relationship="enclosed_by")],
                            trigger_changes=[s_id],
                            relationship_type="enclosing_class",
                            confidence=1.0,
                            explanation=f"Enclosing class containing modified method '{s_id}'",
                        )
                    )

                # Check method invocations on instances across modules
                mod_name = _file_path_to_module_id(sym_change.file_path)
                for edge in self.call_graph.edges:
                    if edge.target_id is None and (
                        f".{sym_change.name}(" in edge.raw_call_expr or
                        edge.raw_call_expr.endswith(f".{sym_change.name}()")
                    ):
                        caller_mod = _file_path_to_module_id(edge.source_file)
                        has_import = caller_mod == mod_name or self.module_graph.get_edge(caller_mod, mod_name) is not None
                        if not has_import:
                            has_import = any(
                                ext.importing_module == caller_mod and (ext.package_name == mod_name or ext.imported_name == mod_name)
                                for ext in self.module_graph.external_imports
                            )

                        if has_import and edge.source_id not in direct_callers:
                            caller_node = self.call_graph.get_node(edge.source_id)
                            record_impact(
                                ImpactedEntity(
                                    entity_id=edge.source_id,
                                    entity_type=_node_kind_to_entity_type(caller_node.kind if caller_node else NodeKind.FUNCTION),
                                    file_path=edge.source_file,
                                    line_number=edge.line_number,
                                    classification=ImpactClassification.DIRECT,
                                    depth=1,
                                    impact_path=[s_id, edge.source_id],
                                    path_steps=[
                                        ImpactPathStep(
                                            from_entity=s_id,
                                            to_entity=edge.source_id,
                                            relationship="method_call",
                                            file_path=edge.source_file,
                                            line_number=edge.line_number,
                                        )
                                    ],
                                    trigger_changes=[s_id],
                                    relationship_type="direct_caller",
                                    confidence=0.90,
                                    explanation=f"Calls method '{sym_change.name}' on an instance in module importing '{mod_name}'",
                                )
                            )

        # -----------------------------------------------------------------
        # 2. Propagate Module Dependencies (Module-Graph Reverse Traversal)
        # -----------------------------------------------------------------
        for file_change in changeset.files:
            file_path = file_change.path
            mod = self.module_graph.get_module(file_path)
            mod_id = mod.module_id if mod else _file_path_to_module_id(file_path)
            is_deletion = file_change.change_type in (ChangeType.DELETED, ChangeType.RENAMED)

            target_mods = {mod_id}
            if file_change.old_path:
                target_mods.add(_file_path_to_module_id(file_change.old_path))

            direct_dependents: Set[str] = set()
            for t_mod in target_mods:
                direct_dependents.update(self.module_graph.get_dependents(t_mod))

            # If deleted or renamed, also scan external and unresolved imports
            if is_deletion:
                for ext in self.module_graph.external_imports:
                    if ext.package_name in target_mods or ext.imported_name.split(".")[0] in target_mods:
                        direct_dependents.add(ext.importing_module)
                for unres in self.module_graph.unresolved_imports:
                    if unres.package_name in target_mods:
                        direct_dependents.add(unres.importing_module)

            for dep_id in direct_dependents:
                if dep_id in target_mods:
                    continue  # Self-dependency

                dep_mod = self.module_graph.get_module(dep_id)
                dep_file = dep_mod.file_path if dep_mod else file_path

                edge = None
                for t_mod in target_mods:
                    edge = self.module_graph.get_edge(dep_id, t_mod)
                    if edge:
                        break

                edge_lines = [ev.line_number for ev in edge.evidence] if edge else []
                dep_line = edge_lines[0] if edge_lines else None

                step = ImpactPathStep(
                    from_entity=mod_id,
                    to_entity=dep_id,
                    relationship="imports",
                    file_path=dep_file,
                    line_number=dep_line,
                )

                if is_deletion:
                    classification = ImpactClassification.UNRESOLVED
                    conf = 0.95
                    expl = (
                        f"Direct dependent module importing {file_change.change_type.value} module '{mod_id}'; "
                        f"import statement will raise ModuleNotFoundError"
                    )
                else:
                    classification = ImpactClassification.DIRECT
                    conf = 1.0
                    expl = f"Direct dependent module importing changed module '{mod_id}'"

                record_impact(
                    ImpactedEntity(
                        entity_id=dep_id,
                        entity_type=EntityType.MODULE,
                        file_path=dep_file,
                        line_number=dep_line,
                        classification=classification,
                        depth=1,
                        impact_path=[mod_id, dep_id],
                        path_steps=[step],
                        trigger_changes=[file_path],
                        relationship_type="direct_dependent_module",
                        confidence=conf,
                        explanation=expl,
                    )
                )

            # Transitive dependent modules (depth >= 2)
            visited_deps: Set[str] = set(target_mods) | set(direct_dependents)
            dep_queue: deque = deque([(d_id, 1, [mod_id, d_id], [
                ImpactPathStep(from_entity=mod_id, to_entity=d_id, relationship="imports")
            ]) for d_id in direct_dependents if d_id not in target_mods])

            while dep_queue and len(impacted_map) < effective_max_nodes:
                curr_id, depth, path, steps = dep_queue.popleft()
                if depth >= effective_max_depth:
                    continue

                upstream_deps = self.module_graph.get_dependents(curr_id)
                for upstream_id in upstream_deps:
                    if upstream_id not in visited_deps:
                        visited_deps.add(upstream_id)
                        up_mod = self.module_graph.get_module(upstream_id)
                        up_file = up_mod.file_path if up_mod else file_path

                        new_step = ImpactPathStep(
                            from_entity=curr_id,
                            to_entity=upstream_id,
                            relationship="imports",
                            file_path=up_file,
                        )
                        new_path = path + [upstream_id]
                        new_steps = steps + [new_step]

                        conf = round(0.95 ** depth, 3)
                        expl = (
                            f"Transitive dependent module at depth {depth + 1} reached via import chain: "
                            f"{' -> '.join(new_path)}"
                        )

                        record_impact(
                            ImpactedEntity(
                                entity_id=upstream_id,
                                entity_type=EntityType.MODULE,
                                file_path=up_file,
                                line_number=None,
                                classification=ImpactClassification.TRANSITIVE,
                                depth=depth + 1,
                                impact_path=new_path,
                                path_steps=new_steps,
                                trigger_changes=[file_path],
                                relationship_type="transitive_dependent_module",
                                confidence=conf,
                                explanation=expl,
                            )
                        )
                        dep_queue.append((upstream_id, depth + 1, new_path, new_steps))

        # -----------------------------------------------------------------
        # 3. Dynamic & Star-Import Impacts (POTENTIAL)
        # -----------------------------------------------------------------
        all_changed_paths = {fc.path for fc in changeset.files} | {sc.file_path for sc in changeset.symbols}
        all_changed_modules = {_file_path_to_module_id(p) for p in all_changed_paths}

        # Star-import checking
        for changed_mod_id in all_changed_modules:
            for edge in self.module_graph.edges.values():
                if edge.target_module == changed_mod_id:
                    for ev in edge.evidence:
                        if ev.import_type == "star" and edge.source_module != changed_mod_id:
                            src_mod = self.module_graph.get_module(edge.source_module)
                            src_file = src_mod.file_path if src_mod else ev.importing_file
                            record_impact(
                                ImpactedEntity(
                                    entity_id=edge.source_module,
                                    entity_type=EntityType.MODULE,
                                    file_path=src_file,
                                    line_number=ev.line_number,
                                    classification=ImpactClassification.POTENTIAL,
                                    depth=1,
                                    impact_path=[changed_mod_id, edge.source_module],
                                    path_steps=[
                                        ImpactPathStep(
                                            from_entity=changed_mod_id,
                                            to_entity=edge.source_module,
                                            relationship="star_imports",
                                            file_path=src_file,
                                            line_number=ev.line_number,
                                        )
                                    ],
                                    trigger_changes=list(all_changed_paths),
                                    relationship_type="star_importer",
                                    confidence=0.75,
                                    explanation=f"Imports changed module '{changed_mod_id}' via star-import ('from ... import *'); potential namespace impact",
                                )
                            )

        # Dynamic call checking
        for edge in self.call_graph.edges:
            if edge.resolution_status in (CallResolutionStatus.DYNAMIC, CallResolutionStatus.AMBIGUOUS):
                matches = (
                    edge.source_file in all_changed_paths or
                    any(sc.name in edge.raw_call_expr for sc in changeset.symbols) or
                    (edge.target_qualified_name and any(sc.name in edge.target_qualified_name for sc in changeset.symbols))
                )
                if matches:
                    caller_node = self.call_graph.get_node(edge.source_id)
                    caller_file = caller_node.file_path if caller_node else edge.source_file
                    record_impact(
                        ImpactedEntity(
                            entity_id=edge.source_id,
                            entity_type=_node_kind_to_entity_type(caller_node.kind if caller_node else NodeKind.FUNCTION),
                            file_path=caller_file,
                            line_number=edge.line_number,
                            classification=ImpactClassification.POTENTIAL,
                            depth=1,
                            impact_path=[edge.source_id],
                            path_steps=[
                                ImpactPathStep(
                                    from_entity=edge.source_id,
                                    to_entity=edge.source_id,
                                    relationship="dynamic_call",
                                    file_path=caller_file,
                                    line_number=edge.line_number,
                                )
                            ],
                            trigger_changes=list(all_changed_paths),
                            relationship_type="dynamic_or_ambiguous_caller",
                            confidence=0.60,
                            explanation=f"Dynamic dispatch site '{edge.raw_call_expr}' in module associated with changeset",
                        )
                    )

        # -----------------------------------------------------------------
        # 4. Final Aggregation & Deterministic Sorting
        # -----------------------------------------------------------------
        all_impacted = list(impacted_map.values())

        # Sort deterministically by (depth, classification_severity, file_path, entity_id)
        all_impacted.sort(
            key=lambda e: (
                e.depth,
                _classification_rank(e.classification),
                e.file_path,
                e.entity_id,
            )
        )

        # Calculate metrics
        direct_count = sum(1 for e in all_impacted if e.classification == ImpactClassification.DIRECT)
        transitive_count = sum(1 for e in all_impacted if e.classification == ImpactClassification.TRANSITIVE)
        potential_count = sum(1 for e in all_impacted if e.classification == ImpactClassification.POTENTIAL)
        unresolved_count = sum(1 for e in all_impacted if e.classification == ImpactClassification.UNRESOLVED)

        impacted_mods = len(set(e.entity_id for e in all_impacted if e.entity_type == EntityType.MODULE))
        impacted_syms = len(set(e.entity_id for e in all_impacted if e.entity_type in (EntityType.FUNCTION, EntityType.METHOD, EntityType.CLASS)))
        impacted_files = len(set(e.file_path for e in all_impacted))
        max_depth_reached = max((e.depth for e in all_impacted), default=0)

        summary = ImpactSummary(
            total_changed_files=len(changeset.files),
            total_changed_symbols=len(changeset.symbols),
            total_impacted_entities=len(all_impacted),
            direct_impacts=direct_count,
            transitive_impacts=transitive_count,
            potential_impacts=potential_count,
            unresolved_impacts=unresolved_count,
            impacted_modules=impacted_mods,
            impacted_symbols=impacted_syms,
            impacted_files=impacted_files,
            max_depth_reached=max_depth_reached,
            is_truncated=is_truncated,
            truncation_reason=truncation_reason,
        )

        return ImpactReport(
            changeset=changeset,
            impacted_entities=all_impacted,
            summary=summary,
            warnings=warnings,
        )


def _node_kind_to_entity_type(kind: NodeKind) -> EntityType:
    """Map NodeKind to EntityType."""
    if kind == NodeKind.FUNCTION:
        return EntityType.FUNCTION
    if kind == NodeKind.METHOD:
        return EntityType.METHOD
    if kind == NodeKind.CLASS:
        return EntityType.CLASS
    if kind == NodeKind.MODULE:
        return EntityType.MODULE
    if kind == NodeKind.VARIABLE:
        return EntityType.VARIABLE
    return EntityType.UNKNOWN
