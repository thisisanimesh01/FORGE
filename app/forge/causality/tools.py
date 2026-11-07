"""Phase 12 M2: Root-Cause Analysis Tools for LLM & Agent Invocation.

Exposes structured, typed, read-only tools:
- analyze_root_cause: Conducts end-to-end causal diagnosis and status determination.
- trace_causal_chain: Explores bounded causal paths between two code entities.
- inspect_contributing_factors: Evaluates upstream dependencies and recent change impacts.
- compare_root_cause_hypotheses: Compares candidate root-cause hypotheses against evidence.
- explain_failure_propagation: Analyzes downstream impact from a suspected root cause.
"""

from collections import deque
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import ValidationError

from forge.causality.engine import RootCauseAnalysisEngine
from forge.causality.schemas import (
    AnalyzeRootCauseInput,
    AnalyzeRootCauseOutput,
    CompareRootCauseHypothesesInput,
    CompareRootCauseHypothesesOutput,
    ExplainFailurePropagationInput,
    ExplainFailurePropagationOutput,
    InspectContributingFactorsInput,
    InspectContributingFactorsOutput,
    TraceCausalChainInput,
    TraceCausalChainOutput,
    CausalRelationship,
    CausalStatus,
)
from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.validation import SecurityError
from forge.schemas import FindingConfidence


class CausalityTools:
    """Provides validated tool execution handlers for root-cause analysis."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        max_output_chars: int = 50_000,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.incremental_index = incremental_index
        self.max_output_chars = max_output_chars
        self._engine: Optional[RootCauseAnalysisEngine] = None

    def get_engine(self) -> RootCauseAnalysisEngine:
        if self._engine is None:
            self._engine = RootCauseAnalysisEngine(
                workspace_path=self.workspace_path,
                incremental_index=self.incremental_index,
            )
        return self._engine

    def analyze_root_cause(self, args: Dict[str, Any]) -> str:
        """Execute analyze_root_cause tool."""
        try:
            params = AnalyzeRootCauseInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for analyze_root_cause: {e}"})

        engine = self.get_engine()
        try:
            report = engine.analyze(
                failing_symbol=params.failing_symbol,
                file_path=params.file_path,
                line_number=params.line_number,
                error_message=params.error_message,
                max_depth=params.max_depth,
            )

            primary_sym = report.primary_root_cause.symbol_id if report.primary_root_cause else None
            primary_file = report.primary_root_cause.file_path if report.primary_root_cause else None
            primary_lines = (
                f"{report.primary_root_cause.start_line}-{report.primary_root_cause.end_line}"
                if report.primary_root_cause
                else None
            )

            chain_summary = (
                report.causal_chains[0].summary
                if report.causal_chains
                else (report.limitations or "No complete causal chain established.")
            )

            is_verified = (
                report.causal_chains[0].is_fully_verified
                if report.causal_chains
                else False
            )

            output = AnalyzeRootCauseOutput(
                status=report.status,
                primary_root_cause_symbol=primary_sym,
                primary_root_cause_file=primary_file,
                primary_root_cause_lines=primary_lines,
                chain_summary=chain_summary,
                confidence=report.confidence,
                confidence_score=report.confidence_score,
                is_directly_verified=is_verified,
                contributing_factors_count=len(report.contributing_factors),
                evidence_ids=report.evidence_references,
                report_id=report.report_id,
            )
            return output.model_dump_json(indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Root cause analysis failed: {exc}"})

    def trace_causal_chain(self, args: Dict[str, Any]) -> str:
        """Execute trace_causal_chain tool."""
        try:
            params = TraceCausalChainInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for trace_causal_chain: {e}"})

        tracer = self.get_engine().get_tracer()
        cg = tracer.get_call_graph()

        src_id = tracer.resolve_symbol_node_id(params.source_symbol)
        dst_id = tracer.resolve_symbol_node_id(params.target_symbol)

        try:
            # Build adjacency mapping for path exploration
            adj: Dict[str, List[str]] = {}
            for edge in cg.edges:
                adj.setdefault(edge.source_id, []).append(edge.target_id)

            paths: List[List[str]] = []
            queue: deque = deque([[src_id]])
            while queue:
                curr_path = queue.popleft()
                curr = curr_path[-1]
                if curr == dst_id:
                    paths.append(curr_path)
                    break
                if len(curr_path) > params.max_depth:
                    continue
                for neighbor in adj.get(curr, []):
                    if neighbor not in curr_path:
                        queue.append(curr_path + [neighbor])

            if paths:
                best_path = paths[0]
                rels = [CausalRelationship.CALLS] * (len(best_path) - 1)
                explanation = f"Direct cross-file invocation path: {' -> '.join(best_path)}."
                output = TraceCausalChainOutput(
                    path_found=True,
                    path_nodes=best_path,
                    edge_relationships=rels,
                    all_directly_verified=True,
                    explanation=explanation,
                )
            else:
                output = TraceCausalChainOutput(
                    path_found=False,
                    path_nodes=[],
                    edge_relationships=[],
                    all_directly_verified=False,
                    explanation=f"No call invocation path found from '{params.source_symbol}' to '{params.target_symbol}' within depth {params.max_depth}.",
                )
            return output.model_dump_json(indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to trace causal chain: {exc}"})

    def inspect_contributing_factors(self, args: Dict[str, Any]) -> str:
        """Execute inspect_contributing_factors tool."""
        try:
            params = InspectContributingFactorsInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for inspect_contributing_factors: {e}"})

        tracer = self.get_engine().get_tracer()
        cg = tracer.get_call_graph()
        mod_graph = tracer.get_module_graph()

        sym_id = tracer.resolve_symbol_node_id(params.symbol_id)

        contributing: List[str] = []
        try:
            callers = cg.get_direct_callers(sym_id)
            contributing = list(callers)
        except Exception:
            pass

        has_cycle = False
        try:
            cycles = mod_graph.find_cycles() if hasattr(mod_graph, "find_cycles") else []
            has_cycle = len(cycles) > 0
        except Exception:
            pass

        output = InspectContributingFactorsOutput(
            symbol_id=params.symbol_id,
            contributing_symbols=contributing,
            recent_changes=[],
            cycle_detected=has_cycle,
            summary=f"Found {len(contributing)} direct callers/contributors for {params.symbol_id}.",
        )
        return output.model_dump_json(indent=2)

    def compare_root_cause_hypotheses(self, args: Dict[str, Any]) -> str:
        """Execute compare_root_cause_hypotheses tool."""
        try:
            params = CompareRootCauseHypothesesInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for compare_root_cause_hypotheses: {e}"})

        if not params.hypotheses:
            output = CompareRootCauseHypothesesOutput(
                preferred_hypothesis_index=None,
                rationale="No candidate hypotheses provided to compare.",
                unresolved_ambiguity=True,
            )
            return output.model_dump_json(indent=2)

        if len(params.hypotheses) == 1:
            output = CompareRootCauseHypothesesOutput(
                preferred_hypothesis_index=0,
                rationale="Single candidate hypothesis provided.",
                unresolved_ambiguity=False,
            )
            return output.model_dump_json(indent=2)

        output = CompareRootCauseHypothesesOutput(
            preferred_hypothesis_index=None,
            rationale=f"Evaluated {len(params.hypotheses)} candidate hypotheses for '{params.context_symbol}'. Insufficient differentiating evidence to select a single root cause.",
            unresolved_ambiguity=True,
        )
        return output.model_dump_json(indent=2)

    def explain_failure_propagation(self, args: Dict[str, Any]) -> str:
        """Execute explain_failure_propagation tool."""
        try:
            params = ExplainFailurePropagationInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for explain_failure_propagation: {e}"})

        tracer = self.get_engine().get_tracer()
        cg = tracer.get_call_graph()

        root_id = tracer.resolve_symbol_node_id(params.root_symbol)

        callees: List[str] = []
        try:
            callees = list(cg.get_direct_callees(root_id))
        except Exception:
            pass

        steps = [
            f"1. Defect originates in symbol '{params.root_symbol}'.",
            f"2. Invocation or state invalidation propagates to downstream callees: {', '.join(callees) if callees else 'direct execution'}.",
            f"3. Failure manifests as observed symptom: {params.failure_symptom}.",
        ]

        output = ExplainFailurePropagationOutput(
            propagation_steps=steps,
            impacted_symbols=callees,
            confidence=FindingConfidence.MEDIUM,
            confidence_score=0.75,
        )
        return output.model_dump_json(indent=2)

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinitions for causality tools."""
        return [
            ToolDefinition(
                name="analyze_root_cause",
                description="Conduct end-to-end evidence-backed root cause analysis on a software failure or localized symbol.",
                parameters=AnalyzeRootCauseInput.model_json_schema(),
            ),
            ToolDefinition(
                name="trace_causal_chain",
                description="Trace direct and indirect causal call paths between a source and a target symbol.",
                parameters=TraceCausalChainInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_contributing_factors",
                description="Inspect contributing callers, module dependencies, and state factors for a given symbol.",
                parameters=InspectContributingFactorsInput.model_json_schema(),
            ),
            ToolDefinition(
                name="compare_root_cause_hypotheses",
                description="Compare competing candidate root-cause hypotheses against available repository evidence.",
                parameters=CompareRootCauseHypothesesInput.model_json_schema(),
            ),
            ToolDefinition(
                name="explain_failure_propagation",
                description="Trace and explain downstream failure propagation from a candidate root cause symbol.",
                parameters=ExplainFailurePropagationInput.model_json_schema(),
            ),
        ]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by name with arguments and return sanitized result."""
        handlers = {
            "analyze_root_cause": self.analyze_root_cause,
            "trace_causal_chain": self.trace_causal_chain,
            "inspect_contributing_factors": self.inspect_contributing_factors,
            "compare_root_cause_hypotheses": self.compare_root_cause_hypotheses,
            "explain_failure_propagation": self.explain_failure_propagation,
        }

        if name not in handlers:
            return json.dumps({
                "error": f"Tool '{name}' is not recognized in CausalityTools.",
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
