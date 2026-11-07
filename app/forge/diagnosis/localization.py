"""Phase 12 M1: Failure Localization Engine.

Extracts, correlates, and ranks candidate failure locations from error messages,
stack traces, test failures, symbol call graphs, change sets, and static findings.
"""

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.analysis.evidence import normalize_relative_path
from forge.diagnosis.schemas import (
    CandidateLocation,
    DiagnosisInput,
    LocationSource,
)
from forge.repository.call_graph import CallGraph
from forge.repository.indexer import SymbolIndex
from forge.repository.module_graph import ModuleDependencyGraph
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path, SecurityError


# Regex patterns for Python stack trace lines
TRACEBACK_FRAME_REGEX = re.compile(
    r'File\s+["\']([^"\']+)["\'],\s+line\s+(\d+)(?:,\s+in\s+([a-zA-Z0-9_<>\.]+))?'
)
# Patterns for inline error messages like 'in pkg/mod.py:42', 'File "src/file.py", line 10'
INLINE_ERROR_LOC_REGEX = re.compile(
    r'(?:in\s+)?(?:File\s+)?["\']?([a-zA-Z0-9_\-\./\\]+\.py)["\']?[:,\s]+(?:line\s+)?(\d+)',
    re.I,
)


class FailureLocalizer:
    """Discovers and ranks candidate defect locations using multiple evidence sources."""

    def __init__(
        self,
        workspace_path: Path,
        symbol_index: Optional[SymbolIndex] = None,
        call_graph: Optional[CallGraph] = None,
        module_graph: Optional[ModuleDependencyGraph] = None,
        impact_analyzer: Optional[Any] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.symbol_index = symbol_index
        self.call_graph = call_graph
        self.module_graph = module_graph
        self.impact_analyzer = impact_analyzer

    def localize(self, input_data: DiagnosisInput) -> List[CandidateLocation]:
        """Discover candidate defect locations ranked by confidence weight."""
        candidates: List[CandidateLocation] = []

        # 1. Stack trace parsing (direct execution crash frames)
        raw_traces = []
        if input_data.stack_trace:
            raw_traces.append(input_data.stack_trace)
        if input_data.test_failure and input_data.test_failure.stack_trace:
            raw_traces.append(input_data.test_failure.stack_trace)

        for trace_text in raw_traces:
            trace_candidates = self._parse_stack_trace(trace_text)
            candidates.extend(trace_candidates)

        # 2. Error message inline locations
        error_texts = []
        if input_data.error_message:
            error_texts.append(input_data.error_message)
        if input_data.test_failure and input_data.test_failure.failure_message:
            error_texts.append(input_data.test_failure.failure_message)

        for err_msg in error_texts:
            msg_candidates = self._parse_error_message(err_msg)
            candidates.extend(msg_candidates)

        # 3. Direct Test Failure Location & Callee Correlation
        if input_data.test_failure:
            tf = input_data.test_failure
            if tf.test_file and tf.line_number:
                candidates.append(
                    CandidateLocation(
                        file_path=tf.test_file,
                        start_line=tf.line_number,
                        end_line=tf.line_number,
                        source=LocationSource.TEST_FAILURE,
                        confidence_weight=0.75,
                        reason=f"Failing assertion or error point in test '{tf.test_name}'",
                    )
                )

            # Discover functions called by the failing test via CallGraph
            if tf.test_name and self.call_graph:
                callee_candidates = self._correlate_test_callees(tf.test_name, tf.test_file)
                candidates.extend(callee_candidates)

        # 4. Static Finding Location
        if input_data.static_finding:
            sf = input_data.static_finding
            candidates.append(
                CandidateLocation(
                    file_path=sf.file_path,
                    start_line=sf.line_number,
                    end_line=sf.line_number,
                    source=LocationSource.STATIC_ANALYSIS,
                    confidence_weight=0.70,
                    reason=f"Static analysis finding flagged by rule '{sf.rule_id}': {sf.message}",
                )
            )

        # 5. Target Symbols Query
        if input_data.target_symbols and self.symbol_index:
            for sym_name in input_data.target_symbols:
                matches = self.symbol_index.query(sym_name)
                for s in matches[:3]:
                    candidates.append(
                        CandidateLocation(
                            file_path=s.file_path,
                            start_line=s.start_line,
                            end_line=s.end_line,
                            symbol_id=s.name,
                            source=LocationSource.SYMBOL_RELATIONSHIP,
                            confidence_weight=0.80,
                            reason=f"Symbol '{s.name}' explicitly targeted in diagnosis input",
                        )
                    )

        # 6. Change-Impact Correlation
        if (input_data.diff or input_data.modified_files) and self.impact_analyzer:
            change_candidates = self._correlate_change_impact(input_data.diff, input_data.modified_files)
            candidates.extend(change_candidates)

        # 7. User-specified target files
        if input_data.target_files:
            for tf in input_data.target_files:
                candidates.append(
                    CandidateLocation(
                        file_path=tf,
                        start_line=1,
                        end_line=50,
                        source=LocationSource.USER_SPECIFIED,
                        confidence_weight=0.50,
                        reason=f"File '{tf}' explicitly targeted by user",
                    )
                )

        # 8. Filter, verify on disk, clamp bounds, and deduplicate
        verified_candidates = self._verify_and_deduplicate(candidates)
        return verified_candidates

    # -----------------------------------------------------------------------
    # Helper: Stack Trace Parsing
    # -----------------------------------------------------------------------

    def _parse_stack_trace(self, trace_text: str) -> List[CandidateLocation]:
        """Extract workspace-relative source locations from Python stack trace frames."""
        matches = TRACEBACK_FRAME_REGEX.findall(trace_text)
        if not matches:
            return []

        raw_frames: List[Tuple[str, int, Optional[str]]] = []
        for file_str, line_str, func_str in matches:
            try:
                line_no = int(line_str)
            except ValueError:
                continue

            # Ignore Python standard library and virtual environment paths
            if "<frozen" in file_str or "/lib/python" in file_str or "site-packages" in file_str:
                continue

            raw_frames.append((file_str, line_no, func_str if func_str else None))

        candidates: List[CandidateLocation] = []
        total_frames = len(raw_frames)

        for idx, (raw_path, line_no, func_name) in enumerate(raw_frames):
            norm_rel = self._resolve_to_workspace_rel(raw_path)
            if not norm_rel:
                continue

            # Crash frame (last frame in trace) receives highest confidence
            is_top_crash = (idx == total_frames - 1)
            weight = 0.90 if is_top_crash else max(0.60, 0.85 - (total_frames - 1 - idx) * 0.05)
            reason = (
                f"Stack trace crash point in '{func_name or norm_rel}' at line {line_no}"
                if is_top_crash
                else f"Stack trace intermediate frame in '{func_name or norm_rel}' at line {line_no}"
            )

            candidates.append(
                CandidateLocation(
                    file_path=norm_rel,
                    start_line=line_no,
                    end_line=line_no,
                    symbol_id=func_name,
                    source=LocationSource.STACK_TRACE,
                    confidence_weight=round(weight, 2),
                    reason=reason,
                )
            )

        return candidates

    # -----------------------------------------------------------------------
    # Helper: Error Message Parsing
    # -----------------------------------------------------------------------

    def _parse_error_message(self, message: str) -> List[CandidateLocation]:
        """Extract source locations referenced directly in error messages."""
        candidates: List[CandidateLocation] = []
        matches = INLINE_ERROR_LOC_REGEX.findall(message)
        for path_str, line_str in matches:
            try:
                line_no = int(line_str)
            except ValueError:
                continue

            norm_rel = self._resolve_to_workspace_rel(path_str)
            if not norm_rel:
                continue

            candidates.append(
                CandidateLocation(
                    file_path=norm_rel,
                    start_line=line_no,
                    end_line=line_no,
                    source=LocationSource.ERROR_MESSAGE,
                    confidence_weight=0.85,
                    reason=f"Location '{norm_rel}:{line_no}' referenced directly in failure message",
                )
            )
        return candidates

    # -----------------------------------------------------------------------
    # Helper: Test Callee Discovery
    # -----------------------------------------------------------------------

    def _correlate_test_callees(self, test_name: str, test_file: Optional[str]) -> List[CandidateLocation]:
        """Identify production code symbols invoked directly by the failing test."""
        candidates: List[CandidateLocation] = []
        if not self.call_graph:
            return candidates

        # Find matching test node in call graph
        clean_name = test_name.split("::")[-1].split("[")[0].strip()
        matching_nodes = [
            node for node in self.call_graph.nodes.values()
            if node.name == clean_name or node.qualified_name.endswith(f":{clean_name}")
        ]

        for node in matching_nodes:
            # Query direct callees (returns list of callee node IDs)
            callee_ids = self.call_graph.get_direct_callees(node.node_id)
            for callee_id in callee_ids[:5]:
                target_node = self.call_graph.nodes.get(callee_id) or self.call_graph.get_node(callee_id)
                if target_node and not target_node.file_path.startswith("tests/"):
                    candidates.append(
                        CandidateLocation(
                            file_path=target_node.file_path,
                            start_line=target_node.start_line,
                            end_line=target_node.end_line,
                            symbol_id=target_node.name,
                            source=LocationSource.SYMBOL_RELATIONSHIP,
                            confidence_weight=0.80,
                            reason=f"Failing test '{clean_name}' invokes '{target_node.name}' in '{target_node.file_path}'",
                        )
                    )
        return candidates

    # -----------------------------------------------------------------------
    # Helper: Change Impact Correlation
    # -----------------------------------------------------------------------

    def _correlate_change_impact(self, diff: Optional[str], modified_files: List[str]) -> List[CandidateLocation]:
        """Correlate candidate failure locations with recent changeset modifications."""
        candidates: List[CandidateLocation] = []
        if not self.impact_analyzer:
            return candidates

        try:
            if diff:
                report = self.impact_analyzer.analyze_diff(diff)
            elif modified_files:
                report = self.impact_analyzer.analyze_file_paths(modified_files)
            else:
                return candidates

            for ent in report.impacted_entities[:8]:
                # Locate symbol in symbol index if available
                start_l, end_l = 1, 30
                if self.symbol_index:
                    matches = self.symbol_index.query(ent.entity_id)
                    for m in matches:
                        if m.file_path == ent.file_path:
                            start_l, end_l = m.start_line, m.end_line
                            break

                weight = 0.85 if ent.depth == 0 else max(0.50, 0.75 - ent.depth * 0.05)
                candidates.append(
                    CandidateLocation(
                        file_path=ent.file_path,
                        start_line=start_l,
                        end_line=end_l,
                        symbol_id=ent.entity_id,
                        source=LocationSource.CHANGE_IMPACT,
                        confidence_weight=round(weight, 2),
                        reason=f"Entity '{ent.entity_id}' impacted by recent change (classification: {ent.classification.value})",
                    )
                )
        except Exception:
            pass

        return candidates

    # -----------------------------------------------------------------------
    # Helper: Path Resolution, Disk Verification & Deduplication
    # -----------------------------------------------------------------------

    def _resolve_to_workspace_rel(self, path_str: str) -> Optional[str]:
        """Safely convert absolute or relative path string into normalized workspace-relative path."""
        try:
            norm_raw = normalize_relative_path(path_str)
            if not norm_raw:
                # Might be absolute path
                cand_path = Path(path_str)
                if cand_path.is_absolute():
                    try:
                        rel = cand_path.resolve().relative_to(self.workspace_path)
                        norm_raw = str(rel).replace("\\", "/")
                    except ValueError:
                        return None
                else:
                    return None

            if is_protected_path(norm_raw, base_dir=self.workspace_path):
                return None

            assert_contained_path(self.workspace_path / norm_raw, self.workspace_path)
            return norm_raw
        except Exception:
            return None

    def _verify_and_deduplicate(self, candidates: List[CandidateLocation]) -> List[CandidateLocation]:
        """Validate candidate locations against actual disk files and deduplicate."""
        verified_map: Dict[Tuple[str, int, int], CandidateLocation] = {}

        for cand in candidates:
            norm_rel = self._resolve_to_workspace_rel(cand.file_path)
            if not norm_rel:
                continue

            full_path = self.workspace_path / norm_rel
            if not full_path.exists() or not full_path.is_file():
                continue

            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                total_lines = len(content.splitlines())
                if total_lines == 0:
                    continue

                clamped_start = max(1, min(cand.start_line, total_lines))
                clamped_end = max(clamped_start, min(cand.end_line, total_lines))

                verified_cand = CandidateLocation(
                    file_path=norm_rel,
                    start_line=clamped_start,
                    end_line=clamped_end,
                    symbol_id=cand.symbol_id,
                    source=cand.source,
                    confidence_weight=cand.confidence_weight,
                    reason=cand.reason,
                    is_verified_on_disk=True,
                )

                key = (norm_rel, clamped_start, clamped_end)
                if key not in verified_map or verified_cand.confidence_weight > verified_map[key].confidence_weight:
                    verified_map[key] = verified_cand
            except Exception:
                continue

        def _loc_sort_key(c: CandidateLocation):
            is_test = (
                c.file_path.startswith("tests/")
                or "/tests/" in c.file_path
                or Path(c.file_path).name.startswith("test_")
            )
            return (1 if is_test else 0, -c.confidence_weight, c.file_path, c.start_line)

        result = list(verified_map.values())
        result.sort(key=_loc_sort_key)
        return result[:15]

    localize_candidates = localize
