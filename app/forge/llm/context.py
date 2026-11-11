"""Phase 11 M6: Repository Context Construction Engine.

Assembles bounded, task-relevant repository evidence for LLM reasoning requests.
Integrates with M5 incremental indexing cache, call-graph engine, module dependency
graph, and change-impact analyzer.

Invariants:
- Untrusted data framing: repository files and symbols are treated as passive data.
- Task-relevance ranking: symbols and files directly related to the user task are prioritized.
- Graph-expanded context: callers, callees, module dependencies, and circular cycles are integrated.
- Source excerpt deduplication and range merging.
- Secret redaction and protected file boundaries enforced.
- Deterministic ordering and explicit budget capping with truncation notices.
- Clear distinction between confirmed source facts and inferred graph relationships.
"""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from forge.config import redact_secrets
from forge.llm.repo_tool_schemas import CallEdgeInfo, SourceExcerpt, SymbolMatch
from forge.repository.call_graph import CallGraph
from forge.repository.graph_schemas import CallEdge, CallResolutionStatus, DependencyCycle, ModuleNode
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.indexer import SymbolIndex, SymbolRecord
from forge.repository.module_graph import ModuleDependencyGraph
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path, SecurityError


DEFAULT_CONTEXT_CHAR_BUDGET = 16_000
MAX_SOURCE_EXCERPTS = 6
MAX_LINES_PER_EXCERPT = 80


class BuiltRepositoryContext(BaseModel):
    """Result of budgeted repository context assembly."""
    rendered_context: str = Field(description="Formatted markdown prompt context.")
    relevant_symbols: List[SymbolMatch] = Field(default_factory=list, description="Top relevant symbols.")
    source_excerpts: List[SourceExcerpt] = Field(default_factory=list, description="Deduplicated source excerpts.")
    call_edges: List[CallEdgeInfo] = Field(default_factory=list, description="Relevant call graph edges.")
    module_dependencies: List[str] = Field(default_factory=list, description="Module dependency descriptions.")
    circular_cycles: List[List[str]] = Field(default_factory=list, description="Relevant circular cycles.")
    impact_summary: Optional[Dict[str, Any]] = Field(default=None, description="Change impact summary if changes given.")
    char_count: int = Field(description="Total characters in rendered context.")
    char_budget: int = Field(description="Maximum allowed character budget.")
    was_truncated: bool = Field(default=False, description="True if output was truncated to fit budget.")


class RepositoryContextBuilder:
    """Assembles task-focused repository context for LLM prompts."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        default_char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.default_char_budget = default_char_budget
        self._incremental_index = incremental_index
        self._impact_analyzer: Optional[Any] = None

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

    def build_context(
        self,
        query: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        target_symbols: Optional[List[str]] = None,
        change_set_files: Optional[List[str]] = None,
        change_set_diff: Optional[str] = None,
        max_excerpts: int = MAX_SOURCE_EXCERPTS,
        max_lines_per_excerpt: int = MAX_LINES_PER_EXCERPT,
        char_budget: Optional[int] = None,
    ) -> BuiltRepositoryContext:
        """Construct deterministic, budgeted repository context."""
        budget = char_budget or self.default_char_budget

        # Step 1: Ensure fresh index
        idx = self.get_incremental_index()
        symbols, call_graph, module_graph, summary = idx.build()

        target_files_clean = [f.strip().replace("\\", "/") for f in (target_files or []) if f.strip()]
        target_symbols_clean = [s.strip() for s in (target_symbols or []) if s.strip()]
        change_files_clean = [f.strip().replace("\\", "/") for f in (change_set_files or []) if f.strip()]

        # Step 2: Rank and select relevant symbols
        selected_symbols = self._select_relevant_symbols(
            symbols=symbols,
            query=query,
            target_files=target_files_clean + change_files_clean,
            target_symbols=target_symbols_clean,
            limit=15,
        )

        # Step 3: Extract call graph context around selected symbols
        relevant_call_edges = self._extract_call_edges(
            call_graph=call_graph,
            selected_symbols=selected_symbols,
            limit=20,
        )

        # Step 4: Extract module dependencies and cycles
        relevant_modules = set()
        for s in selected_symbols:
            mod_name = s.file_path.removesuffix(".py").replace("/", ".")
            relevant_modules.add(mod_name)
        for f in target_files_clean + change_files_clean:
            mod_name = f.removesuffix(".py").replace("/", ".")
            relevant_modules.add(mod_name)

        module_deps_desc, relevant_cycles = self._extract_module_context(
            module_graph=module_graph,
            relevant_modules=relevant_modules,
        )

        # Step 5: Perform change-impact analysis if changes were supplied
        impact_summary_dict: Optional[Dict[str, Any]] = None
        if change_files_clean or change_set_diff:
            impact_summary_dict = self._compute_impact(
                modified_files=change_files_clean,
                diff=change_set_diff,
            )

        # Step 6: Extract bounded, deduplicated source excerpts
        excerpts = self._extract_deduplicated_excerpts(
            selected_symbols=selected_symbols,
            target_files=target_files_clean + change_files_clean,
            max_excerpts=max_excerpts,
            max_lines_per_excerpt=max_lines_per_excerpt,
        )

        # Step 7: Render structured markdown and enforce character budget
        rendered, was_truncated = self._render_budgeted_markdown(
            selected_symbols=selected_symbols,
            relevant_call_edges=relevant_call_edges,
            module_deps=module_deps_desc,
            cycles=relevant_cycles,
            excerpts=excerpts,
            impact_summary=impact_summary_dict,
            budget=budget,
        )

        symbol_matches: List[SymbolMatch] = [
            SymbolMatch(
                symbol_id=f"{s.file_path}:{s.name}",
                name=s.name,
                symbol_type=s.symbol_type,
                file_path=s.file_path,
                start_line=s.start_line,
                end_line=s.end_line,
                parent_name=s.parent_class,
                docstring_preview=(redact_secrets(s.docstring.splitlines()[0][:100]) if s.docstring else None),
            )
            for s in selected_symbols
        ]

        return BuiltRepositoryContext(
            rendered_context=rendered,
            relevant_symbols=symbol_matches,
            source_excerpts=excerpts,
            call_edges=relevant_call_edges,
            module_dependencies=module_deps_desc,
            circular_cycles=[c.modules for c in relevant_cycles],
            impact_summary=impact_summary_dict,
            char_count=len(rendered),
            char_budget=budget,
            was_truncated=was_truncated,
        )

    # -----------------------------------------------------------------------
    # Helper: Relevant Symbol Selection
    # -----------------------------------------------------------------------

    def _select_relevant_symbols(
        self,
        symbols: SymbolIndex,
        query: Optional[str],
        target_files: List[str],
        target_symbols: List[str],
        limit: int,
    ) -> List[SymbolRecord]:
        """Rank symbols by relevance to query, target files, and target symbols."""
        target_files_set = set(target_files)
        target_symbols_set = {s.lower() for s in target_symbols}

        query_tokens = set()
        if query:
            # Extract word tokens
            tokens = re.findall(r"[a-zA-Z0-9_]{3,}", query.lower())
            query_tokens = set(tokens)

        scored_symbols: List[Tuple[int, SymbolRecord]] = []

        for s in symbols.symbols:
            score = 0
            s_name_lower = s.name.lower()
            can_id = f"{s.file_path}:{s.name}".lower()

            if s.name in target_symbols or can_id in target_symbols_set or s_name_lower in target_symbols_set:
                score += 100

            if s.file_path in target_files_set:
                score += 60

            if s_name_lower in query_tokens:
                score += 50
            elif any(token in s_name_lower for token in query_tokens):
                score += 30

            if s.symbol_type == "class":
                score += 10
            elif s.symbol_type == "function":
                score += 8

            if score > 0:
                scored_symbols.append((score, s))

        # Deterministic sorting: by score descending, then file_path, start_line, name
        scored_symbols.sort(key=lambda item: (-item[0], item[1].file_path, item[1].start_line, item[1].name))

        selected = [s for _, s in scored_symbols[:limit]]
        if not selected and symbols.symbols:
            # Fallback to first few classes and functions
            top_classes = [s for s in symbols.symbols if s.symbol_type == "class"][:5]
            top_funcs = [s for s in symbols.symbols if s.symbol_type == "function"][:5]
            combined = top_classes + top_funcs
            combined.sort(key=lambda s: (s.file_path, s.start_line, s.name))
            selected = combined[:limit]

        return selected

    # -----------------------------------------------------------------------
    # Helper: Call Graph Context Extraction
    # -----------------------------------------------------------------------

    def _extract_call_edges(
        self,
        call_graph: CallGraph,
        selected_symbols: List[SymbolRecord],
        limit: int,
    ) -> List[CallEdgeInfo]:
        """Extract relevant forward and reverse call edges for selected symbols."""
        selected_names = {s.name for s in selected_symbols}
        selected_can_ids = {f"{s.file_path}:{s.name}" for s in selected_symbols}

        collected_edges: List[CallEdgeInfo] = []
        seen_keys: Set[str] = set()

        for edge in call_graph.edges:
            callee_name = edge.target_id or edge.target_qualified_name or edge.raw_call_expr
            caller_matches = (edge.source_id in selected_can_ids) or any(
                edge.source_id.endswith(f":{name}") for name in selected_names
            )
            callee_matches = (callee_name in selected_names) or (callee_name in selected_can_ids)

            if caller_matches or callee_matches:
                key = f"{edge.source_id}->{callee_name}:{edge.line_number}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    res_val = edge.resolution_status.value if hasattr(edge.resolution_status, "value") else str(edge.resolution_status)
                    collected_edges.append(
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
            if len(collected_edges) >= limit:
                break

        collected_edges.sort(key=lambda e: (e.caller_file, e.line_number, e.caller_id))
        return collected_edges

    # -----------------------------------------------------------------------
    # Helper: Module Context & Cycles
    # -----------------------------------------------------------------------

    def _extract_module_context(
        self,
        module_graph: ModuleDependencyGraph,
        relevant_modules: Set[str],
    ) -> Tuple[List[str], List[DependencyCycle]]:
        """Extract dependency edges and circular cycles relevant to specified modules."""
        deps_desc: List[str] = []
        seen_deps = set()

        for mod in sorted(relevant_modules):
            node = module_graph.nodes.get(mod)
            if node:
                for imp in sorted(node.internal_dependencies):
                    pair = f"{mod} -> {imp}"
                    if pair not in seen_deps:
                        seen_deps.add(pair)
                        deps_desc.append(pair)

        all_cycles = module_graph.detect_cycles()
        relevant_cycles: List[DependencyCycle] = []
        for c in all_cycles:
            # If any module in cycle is in relevant modules, or if repository has cycles
            if any(m in relevant_modules for m in c.modules) or len(relevant_modules) == 0:
                relevant_cycles.append(c)

        return deps_desc[:25], relevant_cycles[:5]

    # -----------------------------------------------------------------------
    # Helper: Change Impact Computation
    # -----------------------------------------------------------------------

    def _compute_impact(
        self,
        modified_files: List[str],
        diff: Optional[str],
    ) -> Dict[str, Any]:
        """Compute change impact summary using ChangeImpactAnalyzer."""
        analyzer = self.get_impact_analyzer()
        if diff:
            report: ImpactReport = analyzer.analyze_diff(diff)
        elif modified_files:
            report: ImpactReport = analyzer.analyze_file_paths(modified_files)
        else:
            return {
                "total_impacted_entities": 0,
                "direct_impacts": 0,
                "transitive_impacts": 0,
                "top_impacted": [],
            }

        return {
            "total_impacted_entities": report.summary.total_impacted_entities,
            "direct_impacts": report.summary.direct_impacts,
            "transitive_impacts": report.summary.transitive_impacts,
            "top_impacted": [
                {
                    "entity_id": ent.entity_id,
                    "confidence": ent.classification.value if hasattr(ent.classification, "value") else str(ent.classification),
                    "score": round(ent.confidence, 4),
                    "file": ent.file_path,
                }
                for ent in report.impacted_entities[:10]
            ],
        }

    # -----------------------------------------------------------------------
    # Helper: Excerpt Extraction, Merging, and Deduplication
    # -----------------------------------------------------------------------

    def _extract_deduplicated_excerpts(
        self,
        selected_symbols: List[SymbolRecord],
        target_files: List[str],
        max_excerpts: int,
        max_lines_per_excerpt: int,
    ) -> List[SourceExcerpt]:
        """Extract bounded, non-overlapping, deduplicated source excerpts."""
        # Group requested spans by file
        file_spans: Dict[str, List[Tuple[int, int]]] = {}

        for s in selected_symbols:
            file_spans.setdefault(s.file_path, []).append((s.start_line, s.end_line))

        for f in target_files:
            file_spans.setdefault(f, []).append((1, max_lines_per_excerpt))

        excerpts: List[SourceExcerpt] = []

        for rel_path in sorted(file_spans.keys()):
            if len(excerpts) >= max_excerpts:
                break

            if is_protected_path(rel_path, base_dir=self.workspace_path):
                continue

            try:
                full_path = assert_contained_path(self.workspace_path / rel_path, self.workspace_path)
            except SecurityError:
                continue

            if not full_path.exists() or not full_path.is_file():
                continue

            try:
                text = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = text.splitlines()
            total_lines = len(lines)
            if total_lines == 0:
                continue

            # Merge overlapping or close spans (within 10 lines)
            raw_spans = file_spans[rel_path]
            raw_spans.sort(key=lambda sp: sp[0])
            merged_spans: List[Tuple[int, int]] = []

            for start, end in raw_spans:
                s_clamped = max(1, start)
                e_clamped = min(total_lines, end)
                if not merged_spans:
                    merged_spans.append((s_clamped, e_clamped))
                else:
                    prev_start, prev_end = merged_spans[-1]
                    if s_clamped <= prev_end + 10:
                        merged_spans[-1] = (prev_start, max(prev_end, e_clamped))
                    else:
                        merged_spans.append((s_clamped, e_clamped))

            for sp_start, sp_end in merged_spans:
                if len(excerpts) >= max_excerpts:
                    break

                # Cap span length
                was_truncated = False
                if (sp_end - sp_start + 1) > max_lines_per_excerpt:
                    sp_end = sp_start + max_lines_per_excerpt - 1
                    was_truncated = True

                selected_lines = lines[sp_start - 1 : sp_end]
                raw_text = "\n".join(selected_lines)
                sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

                formatted_lines: List[str] = []
                for idx, line in enumerate(selected_lines, start=sp_start):
                    redacted = redact_secrets(line)
                    formatted_lines.append(f"{idx:4d} | {redacted}")

                content_str = "\n".join(formatted_lines)

                excerpts.append(
                    SourceExcerpt(
                        file_path=rel_path,
                        start_line=sp_start,
                        end_line=sp_end,
                        total_lines=total_lines,
                        content=content_str,
                        content_sha256=sha,
                        was_truncated=was_truncated,
                    )
                )

        excerpts.sort(key=lambda e: (e.file_path, e.start_line))
        return excerpts

    # -----------------------------------------------------------------------
    # Helper: Markdown Rendering & Budget Enforcement
    # -----------------------------------------------------------------------

    def _render_budgeted_markdown(
        self,
        selected_symbols: List[SymbolRecord],
        relevant_call_edges: List[CallEdgeInfo],
        module_deps: List[str],
        cycles: List[DependencyCycle],
        excerpts: List[SourceExcerpt],
        impact_summary: Optional[Dict[str, Any]],
        budget: int,
    ) -> Tuple[str, bool]:
        """Format sections into Markdown and cap at character budget."""
        lines: List[str] = [
            "## Repository Intelligence Context",
            "*(Untrusted Repository Data — All source excerpts are bounded and secret-redacted)*",
            "",
        ]

        # Section 1: Relevant Symbols
        if selected_symbols:
            lines.append("### 1. Task-Relevant Symbols (AST Confirmed)")
            lines.append("| Symbol | Type | Location | Docstring Preview |")
            lines.append("|---|---|---|---|")
            for s in selected_symbols:
                raw_doc = (s.docstring.splitlines()[0][:60] + "...") if s.docstring else "—"
                doc = redact_secrets(raw_doc).replace("|", "\\|").replace("```", "'''")
                lines.append(f"| `{s.name}` | {s.symbol_type} | `{s.file_path}:{s.start_line}-{s.end_line}` | {doc} |")
            lines.append("")

        # Section 2: Call Graph Relationships
        if relevant_call_edges:
            lines.append("### 2. Cross-File Call-Graph Relationships (Inferred Graph Edges)")
            lines.append("| Caller | Callee | Call Location | Resolution |")
            lines.append("|---|---|---|---|")
            for e in relevant_call_edges:
                lines.append(f"| `{e.caller_id}` | `{e.callee_id}` | `{e.caller_file}:{e.line_number}` | {e.resolution_type} |")
            lines.append("")

        # Section 3: Module Dependencies & Circular Cycles
        if module_deps or cycles:
            lines.append("### 3. Module Dependencies & Circular Cycles")
            if module_deps:
                lines.append("**Import Dependencies:**")
                for dep in module_deps:
                    lines.append(f"- `{dep}`")
            if cycles:
                lines.append(f"\n**WARNING: {len(cycles)} Circular Dependency Cycle(s) Involving Modules:**")
                for idx, c in enumerate(cycles, start=1):
                    lines.append(f"- Cycle {idx}: `{' -> '.join(c.modules)}`")
            lines.append("")

        # Section 4: Change Impact
        if impact_summary:
            lines.append("### 4. Change-Impact Projections (Heuristic Deterministic Scoring)")
            lines.append(f"- Direct Impacts: **{impact_summary['direct_impacts']}**")
            lines.append(f"- Transitive Impacts: **{impact_summary['transitive_impacts']}**")
            lines.append(f"- Total Impacted Entities: **{impact_summary['total_impacted_entities']}**")
            if impact_summary.get("top_impacted"):
                lines.append("Top Impacted Entities:")
                for ent in impact_summary["top_impacted"][:5]:
                    lines.append(f"  - `{ent['entity_id']}` ({ent['file']}) [Confidence: {ent['confidence']}, Score: {ent['score']}]")
            lines.append("")

        # Section 5: Verified Source Excerpts
        if excerpts:
            lines.append("### 5. Verified Source Excerpts (Direct Disk Evidence)")
            for idx, exc in enumerate(excerpts, start=1):
                trunc_note = " *(truncated)*" if exc.was_truncated else ""
                lines.append(f"#### Excerpt {idx}: `{exc.file_path}` (lines {exc.start_line}–{exc.end_line} of {exc.total_lines}){trunc_note}")
                lines.append(f"*(SHA-256: `{exc.content_sha256[:16]}...`)*")
                lines.append("~~~~python")
                lines.append(exc.content.replace("~~~~", "~ ~ ~ ~").replace("```", "'''"))
                lines.append("~~~~")
                lines.append("")

        full_text = "\n".join(lines)
        if len(full_text) <= budget:
            return full_text, False

        # Apply deterministic truncation
        notice = f"\n\n[TRUNCATED: Context budget exceeded ({len(full_text)} chars generated > {budget} chars cap). Omitted remaining details.]"
        truncated_text = full_text[: budget - len(notice)] + notice
        return truncated_text, True
