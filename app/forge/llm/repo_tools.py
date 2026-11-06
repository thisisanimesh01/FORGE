"""Phase 11 M6: Repository Intelligence Tools implementation.

Provides structured, typed, read-only tools exposing:
- Repository architecture & incremental cache freshness overview
- Symbol search, details, and ambiguous match disambiguation
- Cross-file call-graph exploration (callers, callees, bounded paths)
- Module dependency graph exploration and cycle detection
- Change-impact analysis with deterministic confidence semantics
- Bounded source context retrieval with symbol metadata and secret redaction
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import ValidationError

from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.llm.repo_tool_schemas import (
    CallDirection,
    CallEdgeInfo,
    CallGraphExploreInput,
    CallGraphExploreOutput,
    ChangeImpactInput,
    ChangeImpactOutput,
    ImpactPathInfo,
    ImpactedEntityInfo,
    ModuleCycleInfo,
    ModuleDependencyInput,
    ModuleDependencyOutput,
    RepositoryOverviewInput,
    RepositoryOverviewOutput,
    CacheFreshnessInfo,
    SourceContextInput,
    SourceContextOutput,
    SourceSymbolMetadata,
    SymbolDetailsInput,
    SymbolDetailsOutput,
    SymbolMatch,
    SymbolRelationship,
    SymbolSearchInput,
    SymbolSearchOutput,
)
from forge.repository.architecture import RepositoryMap
from forge.repository.call_graph import CallGraph
from forge.repository.graph_schemas import CallEdge, CallResolutionStatus, DependencyCycle, GraphNode
from forge.repository.detector import detect_repository
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.indexer import SymbolIndex, SymbolRecord
from forge.repository.module_graph import ModuleDependencyGraph
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path, SecurityError


MAX_EXCERPT_WINDOW_LINES = 250
MAX_OUTPUT_CHARS_DEFAULT = 50_000


class RepositoryIntelligenceTools:
    """Manages execution and schemas for repository intelligence LLM tools."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        max_output_chars: int = MAX_OUTPUT_CHARS_DEFAULT,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.max_output_chars = max_output_chars
        self._incremental_index = incremental_index
        self._impact_analyzer: Optional[ChangeImpactAnalyzer] = None

    def get_incremental_index(self) -> IncrementalRepositoryIndex:
        """Lazily initialize or return the IncrementalRepositoryIndex."""
        if self._incremental_index is None:
            self._incremental_index = IncrementalRepositoryIndex(repo_root=self.workspace_path)
        return self._incremental_index

    def get_impact_analyzer(self) -> Any:
        """Lazily initialize or return the ChangeImpactAnalyzer."""
        if self._impact_analyzer is None:
            from forge.analysis.impact.analyzer import ChangeImpactAnalyzer
            idx = self.get_incremental_index()
            self._impact_analyzer = ChangeImpactAnalyzer(
                repo_root=self.workspace_path,
                incremental_index=idx,
            )
        return self._impact_analyzer

    # -----------------------------------------------------------------------
    # Tool A: Repository Overview
    # -----------------------------------------------------------------------

    def get_repository_overview(self, args: Dict[str, Any]) -> str:
        """Execute get_repository_overview tool."""
        try:
            params = RepositoryOverviewInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for get_repository_overview: {e}"})

        idx = self.get_incremental_index()
        # Build or get existing index
        symbols, call_graph, module_graph, summary = idx.build()

        freshness_info: Optional[CacheFreshnessInfo] = None
        if params.include_cache_stats:
            stats = idx.cache_manager.stats
            persisted = idx._load_persisted_state()
            last_indexed = persisted.last_updated_at if persisted else None
            is_fresh = True
            if params.check_freshness:
                # Check if file counts match
                discovered = idx._discover_python_files()
                if persisted and len(discovered) != len(persisted.file_hashes):
                    is_fresh = False

            cache_entries = [p for p in idx.cache_manager.cache_dir.glob("entry_*.json") if p.is_file()]
            freshness_info = CacheFreshnessInfo(
                is_fresh=is_fresh,
                last_indexed_at=last_indexed,
                cache_entries_count=len(cache_entries),
                cache_disk_bytes=sum(p.stat().st_size for p in cache_entries),
                cache_hits=stats.cache_hits,
                cache_misses=stats.cache_misses,
                rebuild_fallback_triggered=summary.rebuild_fallback_triggered if summary else False,
            )

        intel = detect_repository(self.workspace_path)
        cycles = module_graph.detect_cycles()

        output = RepositoryOverviewOutput(
            repository_root=self.workspace_path.name,
            total_files_scanned=len(symbols.file_symbols),
            total_symbols=len(symbols.symbols),
            total_classes=symbols.total_classes,
            total_functions=symbols.total_functions,
            total_methods=symbols.total_methods,
            total_call_nodes=len(call_graph.nodes),
            total_call_edges=len(call_graph.edges),
            total_modules=len(module_graph.nodes),
            total_module_edges=len(module_graph.edges),
            total_circular_cycles=len(cycles),
            frameworks_detected=intel.detected_frameworks,
            cache_freshness=freshness_info,
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool B: Symbol Exploration
    # -----------------------------------------------------------------------

    def search_symbols(self, args: Dict[str, Any]) -> str:
        """Execute search_symbols tool with ambiguity detection."""
        try:
            params = SymbolSearchInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for search_symbols: {e}"})

        idx = self.get_incremental_index()
        symbols = idx.get_symbol_index()

        sym_type = params.symbol_type.value if params.symbol_type else None
        matches = symbols.query(params.query, symbol_type=sym_type)

        if params.exact_match:
            matches = [s for s in matches if s.name.lower() == params.query.lower()]

        # Check for ambiguity: multiple symbols with the exact same name across different files
        names_seen: Dict[str, List[str]] = {}
        for s in matches:
            can_id = f"{s.file_path}:{s.name}"
            names_seen.setdefault(s.name, []).append(can_id)

        ambiguous_candidates: List[str] = []
        is_ambiguous = False
        for name, can_ids in names_seen.items():
            if len(can_ids) > 1:
                is_ambiguous = True
                ambiguous_candidates.extend(can_ids)

        total_found = len(matches)
        capped_matches = matches[: params.max_results]

        symbol_matches: List[SymbolMatch] = []
        for s in capped_matches:
            can_id = f"{s.file_path}:{s.name}"
            doc_preview = None
            if s.docstring:
                first_line = s.docstring.strip().splitlines()[0].strip()
                doc_preview = first_line[:120] if first_line else None

            symbol_matches.append(
                SymbolMatch(
                    symbol_id=can_id,
                    name=s.name,
                    symbol_type=s.symbol_type,
                    file_path=s.file_path,
                    start_line=s.start_line,
                    end_line=s.end_line,
                    parent_name=s.parent_class,
                    docstring_preview=doc_preview,
                )
            )

        output = SymbolSearchOutput(
            query=params.query,
            matches_count=len(symbol_matches),
            total_matches_found=total_found,
            is_ambiguous=is_ambiguous,
            ambiguous_candidate_ids=sorted(list(set(ambiguous_candidates))),
            symbols=symbol_matches,
        )
        return output.model_dump_json(indent=2)

    def get_symbol_details(self, args: Dict[str, Any]) -> str:
        """Execute get_symbol_details tool."""
        try:
            params = SymbolDetailsInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for get_symbol_details: {e}"})

        idx = self.get_incremental_index()
        symbols = idx.get_symbol_index()
        call_graph = idx.get_call_graph()

        query_id = params.symbol_id.strip()
        target_file = params.file_path.strip() if params.file_path else None

        # Resolve candidate symbols
        candidates: List[SymbolRecord] = []
        for s in symbols.symbols:
            can_id = f"{s.file_path}:{s.name}"
            dotted_id = f"{s.file_path.replace('/', '.').removesuffix('.py')}.{s.name}"
            if query_id in (can_id, dotted_id, s.name):
                if target_file is None or s.file_path == target_file:
                    candidates.append(s)

        if not candidates:
            return json.dumps({
                "error": f"Symbol '{query_id}' not found in repository index.",
                "hint": "Try using 'search_symbols' with a broader query.",
            })

        is_ambiguous = len(candidates) > 1
        disambiguation_options = [f"{c.file_path}:{c.name}" for c in candidates]

        # Select first candidate as primary
        sym = candidates[0]
        can_id = f"{sym.file_path}:{sym.name}"

        # Collect relationships from call graph and symbol properties
        relationships: List[SymbolRelationship] = []

        # Find callers and callees in call graph
        for edge in call_graph.edges:
            callee_target = edge.target_id or edge.target_qualified_name or edge.raw_call_expr
            if edge.target_id == can_id or edge.target_qualified_name == sym.name or callee_target == sym.name:
                relationships.append(
                    SymbolRelationship(
                        relationship_type="caller",
                        target_id=edge.source_id,
                        file_path=edge.source_file,
                        line_number=edge.line_number,
                    )
                )
            if edge.source_id == can_id or edge.source_id.endswith(f":{sym.name}"):
                relationships.append(
                    SymbolRelationship(
                        relationship_type="callee",
                        target_id=callee_target,
                        file_path=None,
                        line_number=edge.line_number,
                    )
                )

        output = SymbolDetailsOutput(
            symbol_id=can_id,
            name=sym.name,
            symbol_type=sym.symbol_type,
            file_path=sym.file_path,
            start_line=sym.start_line,
            end_line=sym.end_line,
            parent_name=sym.parent_class,
            parameters=sym.parameters,
            docstring=sym.docstring,
            relationships=relationships[:50],
            is_ambiguous=is_ambiguous,
            disambiguation_options=disambiguation_options if is_ambiguous else [],
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool C: Call-Graph Exploration
    # -----------------------------------------------------------------------

    def explore_call_graph(self, args: Dict[str, Any]) -> str:
        """Execute explore_call_graph tool."""
        try:
            params = CallGraphExploreInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for explore_call_graph: {e}"})

        idx = self.get_incremental_index()
        call_graph = idx.get_call_graph()

        sym_id = params.symbol_id.strip()

        # Find matching call nodes
        matching_nodes = [
            n for n in call_graph.nodes.values()
            if n.name == sym_id or n.qualified_name.endswith(f".{sym_id}") or n.node_id.endswith(f":{sym_id}") or sym_id in (n.name, n.qualified_name, n.node_id)
        ]

        target_nid = matching_nodes[0].node_id if matching_nodes else sym_id
        target_file = matching_nodes[0].file_path if matching_nodes else None

        callers_list: List[CallEdgeInfo] = []
        callees_list: List[CallEdgeInfo] = []
        unresolved_count = 0

        # Direct callers (incoming edges)
        if params.direction in (CallDirection.CALLERS, CallDirection.BOTH):
            for edge in call_graph.edges:
                callee_name = edge.target_id or edge.target_qualified_name or edge.raw_call_expr
                if edge.target_id == target_nid or callee_name == sym_id or (edge.target_qualified_name and edge.target_qualified_name.endswith(f".{sym_id}")):
                    if len(callers_list) >= params.max_results:
                        break
                    res_val = edge.resolution_status.value if hasattr(edge.resolution_status, "value") else str(edge.resolution_status)
                    if res_val in ("unresolved", "dynamic"):
                        unresolved_count += 1
                    callers_list.append(
                        CallEdgeInfo(
                            caller_id=edge.source_id,
                            callee_id=callee_name,
                            caller_file=edge.source_file,
                            line_number=edge.line_number,
                            resolution_type=res_val,
                            resolution_reason=edge.resolution_reason,
                            depth=1,
                        )
                    )

        # Direct callees (outgoing edges)
        if params.direction in (CallDirection.CALLEES, CallDirection.BOTH):
            for edge in call_graph.edges:
                if edge.source_id == target_nid or edge.source_id.endswith(f":{sym_id}") or (matching_nodes and edge.source_id == matching_nodes[0].node_id):
                    if len(callees_list) >= params.max_results:
                        break
                    callee_name = edge.target_id or edge.target_qualified_name or edge.raw_call_expr
                    res_val = edge.resolution_status.value if hasattr(edge.resolution_status, "value") else str(edge.resolution_status)
                    if res_val in ("unresolved", "dynamic"):
                        unresolved_count += 1
                    callees_list.append(
                        CallEdgeInfo(
                            caller_id=edge.source_id,
                            callee_id=callee_name,
                            caller_file=edge.source_file,
                            line_number=edge.line_number,
                            resolution_type=res_val,
                            resolution_reason=edge.resolution_reason,
                            depth=1,
                        )
                    )

        is_truncated = (len(callers_list) >= params.max_results) or (len(callees_list) >= params.max_results)

        output = CallGraphExploreOutput(
            target_symbol_id=target_nid,
            target_file=target_file,
            direction=params.direction.value,
            callers_count=len(callers_list),
            callees_count=len(callees_list),
            callers=callers_list,
            callees=callees_list,
            unresolved_calls_count=unresolved_count,
            is_truncated=is_truncated,
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool D: Module Dependency Exploration
    # -----------------------------------------------------------------------

    def explore_module_dependencies(self, args: Dict[str, Any]) -> str:
        """Execute explore_module_dependencies tool."""
        try:
            params = ModuleDependencyInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for explore_module_dependencies: {e}"})

        idx = self.get_incremental_index()
        module_graph = idx.get_module_graph()

        direct_deps: List[str] = []
        direct_dependents: List[str] = []
        transitive_dependents: List[str] = []
        target_file: Optional[str] = None

        if params.module_name:
            mod_name = params.module_name.strip()
            # Normalize possible file path to module name
            if "/" in mod_name or mod_name.endswith(".py"):
                mod_name = mod_name.removesuffix(".py").replace("/", ".")

            node = module_graph.nodes.get(mod_name)
            if node:
                target_file = node.file_path
                direct_deps = sorted(list(node.internal_dependencies))[: params.max_results]
                direct_dependents = sorted(list(node.internal_dependents))[: params.max_results]
            else:
                # Fuzzy match module
                matching = [m for m in module_graph.nodes if m.endswith(f".{mod_name}") or mod_name in m]
                if matching:
                    mod_name = matching[0]
                    node = module_graph.nodes[mod_name]
                    target_file = node.file_path
                    direct_deps = sorted(list(node.internal_dependencies))[: params.max_results]
                    direct_dependents = sorted(list(node.internal_dependents))[: params.max_results]

            if params.transitive:
                all_trans = module_graph.get_transitive_dependents(mod_name, max_depth=params.max_depth)
                transitive_dependents = sorted(list(all_trans))[: params.max_results]

        cycles_info: List[ModuleCycleInfo] = []
        if params.include_cycles:
            raw_cycles = module_graph.detect_cycles()
            for c in raw_cycles[: params.max_results]:
                cycles_info.append(
                    ModuleCycleInfo(
                        cycle_id=c.cycle_id,
                        length=c.length,
                        cycle_path=list(c.modules),
                    )
                )

        output = ModuleDependencyOutput(
            target_module=params.module_name,
            target_file=target_file,
            direct_dependencies=direct_deps,
            direct_dependents=direct_dependents,
            transitive_dependents=transitive_dependents,
            circular_cycles=cycles_info,
            total_workspace_modules=len(module_graph.nodes),
            total_workspace_edges=len(module_graph.edges),
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool E: Change-Impact Analysis
    # -----------------------------------------------------------------------

    def analyze_change_impact(self, args: Dict[str, Any]) -> str:
        """Execute analyze_change_impact tool."""
        try:
            params = ChangeImpactInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for analyze_change_impact: {e}"})

        # Security check: modified files cannot be protected paths
        for f in params.modified_files:
            if is_protected_path(f, base_dir=self.workspace_path):
                return json.dumps({"error": f"Path '{f}' is protected and excluded from modification analysis."})
            try:
                assert_contained_path(self.workspace_path / f, self.workspace_path)
            except SecurityError as sec_err:
                return json.dumps({"error": f"Security violation: {sec_err}"})

        from forge.analysis.impact.schemas import ChangeSet, ImpactReport
        analyzer = self.get_impact_analyzer()
        if params.diff:
            impact_report: ImpactReport = analyzer.analyze_diff(params.diff)
        elif params.modified_files:
            impact_report = analyzer.analyze_file_paths(params.modified_files)
        else:
            impact_report = analyzer.analyze(ChangeSet())

        impacted_entities: List[ImpactedEntityInfo] = []
        impacted_files_set: Set[str] = set()

        for ent in impact_report.impacted_entities[: params.max_results]:
            impacted_files_set.add(ent.file_path)
            impacted_entities.append(
                ImpactedEntityInfo(
                    entity_id=ent.entity_id,
                    entity_type=ent.entity_type.value if hasattr(ent.entity_type, "value") else str(ent.entity_type),
                    file_path=ent.file_path,
                    impact_type=ent.classification.value if hasattr(ent.classification, "value") else str(ent.classification),
                    confidence=ent.classification.value if hasattr(ent.classification, "value") else str(ent.classification),
                    heuristic_score=round(ent.confidence, 4),
                    hop_distance=ent.depth,
                    impact_rationale=ent.explanation,
                )
            )

        sample_paths: List[ImpactPathInfo] = []
        for ent in impact_report.impacted_entities[:10]:
            if ent.impact_path:
                sample_paths.append(
                    ImpactPathInfo(
                        target_id=ent.entity_id,
                        path=ent.impact_path,
                        path_type=ent.relationship_type,
                    )
                )

        is_truncated = len(impact_report.impacted_entities) > params.max_results

        output = ChangeImpactOutput(
            total_impacted_entities=impact_report.summary.total_impacted_entities,
            direct_impact_count=impact_report.summary.direct_impacts,
            transitive_impact_count=impact_report.summary.transitive_impacts,
            impacted_files=sorted(list(impacted_files_set)),
            impacted_entities=impacted_entities,
            sample_impact_paths=sample_paths,
            is_truncated=is_truncated,
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool F: Bounded Source Context Retrieval
    # -----------------------------------------------------------------------

    def get_source_context(self, args: Dict[str, Any]) -> str:
        """Execute get_source_context tool with bounds, symbol metadata, and secret redaction."""
        try:
            params = SourceContextInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for get_source_context: {e}"})

        file_path_str = params.file_path.strip().replace("\\", "/")
        if is_protected_path(file_path_str, base_dir=self.workspace_path):
            return json.dumps({"error": f"Access denied: path '{file_path_str}' is protected."})

        try:
            abs_path = assert_contained_path(self.workspace_path / file_path_str, self.workspace_path)
        except SecurityError as sec_err:
            return json.dumps({"error": f"Security violation: {sec_err}"})

        if not abs_path.exists() or not abs_path.is_file():
            return json.dumps({"error": f"File '{file_path_str}' does not exist or is not a file."})

        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return json.dumps({"error": f"Failed to read file '{file_path_str}': {e}"})

        lines = text.splitlines()
        total_lines = len(lines)

        start_line = max(1, params.start_line)
        end_line = min(total_lines, params.end_line)
        if start_line > end_line:
            return json.dumps({
                "error": f"Invalid line range: start_line ({start_line}) cannot exceed end_line ({end_line}).",
            })

        was_truncated = False
        if (end_line - start_line + 1) > MAX_EXCERPT_WINDOW_LINES:
            end_line = start_line + MAX_EXCERPT_WINDOW_LINES - 1
            was_truncated = True

        selected_slice = lines[start_line - 1 : end_line]
        raw_slice_str = "\n".join(selected_slice)
        content_hash = hashlib.sha256(raw_slice_str.encode("utf-8")).hexdigest()

        # Format with 1-based line numbers and redact secrets
        formatted_lines: List[str] = []
        for idx_num, line_str in enumerate(selected_slice, start=start_line):
            redacted_line = redact_secrets(line_str)
            formatted_lines.append(f"{idx_num:4d} | {redacted_line}")

        source_content = "\n".join(formatted_lines)

        symbols_in_range: List[SourceSymbolMetadata] = []
        if params.include_symbols:
            idx = self.get_incremental_index()
            sym_index = idx.get_symbol_index()
            rel_path = str(abs_path.relative_to(self.workspace_path))
            for s in sym_index.symbols:
                if s.file_path == rel_path and not (s.end_line < start_line or s.start_line > end_line):
                    symbols_in_range.append(
                        SourceSymbolMetadata(
                            name=s.name,
                            symbol_type=s.symbol_type,
                            start_line=s.start_line,
                            end_line=s.end_line,
                        )
                    )

        output = SourceContextOutput(
            file_path=str(abs_path.relative_to(self.workspace_path)),
            start_line=start_line,
            end_line=end_line,
            total_file_lines=total_lines,
            content_sha256=content_hash,
            source_content=source_content,
            symbols_in_range=symbols_in_range,
            was_truncated=was_truncated,
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool Registry & JSON Schema Definitions
    # -----------------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return standardized ToolDefinitions for all repository intelligence tools."""
        return [
            ToolDefinition(
                name="get_repository_overview",
                description="Get repository architecture summary, module metrics, circular dependency counts, and incremental cache freshness.",
                parameters=RepositoryOverviewInput.model_json_schema(),
            ),
            ToolDefinition(
                name="search_symbols",
                description="Search repository symbols by name or type with explicit ambiguity reporting.",
                parameters=SymbolSearchInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_symbol_details",
                description="Retrieve symbol definition, docstring, parameters, callers, callees, and disambiguation options.",
                parameters=SymbolDetailsInput.model_json_schema(),
            ),
            ToolDefinition(
                name="explore_call_graph",
                description="Explore cross-file call relationships (callers and callees) with resolution classification (internal, unresolved, dynamic).",
                parameters=CallGraphExploreInput.model_json_schema(),
            ),
            ToolDefinition(
                name="explore_module_dependencies",
                description="Inspect module imports, direct dependents, transitive downstream dependents, and circular dependency cycles.",
                parameters=ModuleDependencyInput.model_json_schema(),
            ),
            ToolDefinition(
                name="analyze_change_impact",
                description="Compute direct and transitive change impact from modified files or diffs using call and module dependency graphs.",
                parameters=ChangeImpactInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_source_context",
                description="Retrieve bounded, line-numbered, secret-redacted source excerpts with SHA-256 verification and symbol metadata.",
                parameters=SourceContextInput.model_json_schema(),
            ),
        ]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by name with arguments and return sanitized result string."""
        handlers = {
            "get_repository_overview": self.get_repository_overview,
            "search_symbols": self.search_symbols,
            "get_symbol_details": self.get_symbol_details,
            "explore_call_graph": self.explore_call_graph,
            "explore_module_dependencies": self.explore_module_dependencies,
            "analyze_change_impact": self.analyze_change_impact,
            "get_source_context": self.get_source_context,
        }

        if name not in handlers:
            return json.dumps({
                "error": f"Tool '{name}' is not recognized or available.",
                "available_tools": list(handlers.keys()),
            })

        try:
            raw_output = handlers[name](arguments)
            redacted = redact_secrets(raw_output)
            if len(redacted) > self.max_output_chars:
                return (
                    redacted[: self.max_output_chars]
                    + f"\n\n[TRUNCATED: Output exceeded maximum allowed size of {self.max_output_chars} characters]"
                )
            return redacted
        except SecurityError as sec_err:
            return json.dumps({"error": f"Security violation: {sec_err}"})
        except Exception as exc:
            return json.dumps({"error": f"Unexpected tool execution error: {exc}"})
