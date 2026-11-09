"""Phase 12 M6: Repair Quality Tools for Agent & LLM Invocation.

Exposes 12 structured, typed, read-only tools to ToolRegistry:
1. create_repair_quality_request
2. validate_repair_quality_evidence
3. analyze_repair_patch_quality
4. analyze_repair_maintainability
5. analyze_repair_completeness
6. evaluate_repair_correctness
7. analyze_repair_reliability
8. analyze_repair_risk
9. calculate_repair_quality_score
10. evaluate_repair_quality_gates
11. inspect_repair_quality_history
12. get_repair_quality_summary
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.repair_quality.completeness import RepairCompletenessAnalyzer
from forge.repair_quality.correctness import RepairCorrectnessEvaluator
from forge.repair_quality.evidence import RepairQualityEvidenceValidator
from forge.repair_quality.gates import RepairQualityGateKeeper
from forge.repair_quality.history import HistoricalReliabilityStore
from forge.repair_quality.maintainability import RepairMaintainabilityAnalyzer
from forge.repair_quality.patch_analysis import RepairPatchAnalyzer
from forge.repair_quality.reliability import RepairReliabilityAnalyzer
from forge.repair_quality.risk import RepairRiskAnalyzer
from forge.repair_quality.schemas import (
    AnalyzeRepairCompletenessInput,
    AnalyzeRepairMaintainabilityInput,
    AnalyzeRepairPatchQualityInput,
    AnalyzeRepairReliabilityInput,
    AnalyzeRepairRiskInput,
    CalculateRepairQualityScoreInput,
    CreateRepairQualityRequestInput,
    EvaluateRepairCorrectnessInput,
    EvaluateRepairQualityGatesInput,
    GetRepairQualitySummaryInput,
    InspectRepairQualityHistoryInput,
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityRequest,
    RepairQualityResult,
    RiskSeverity,
    ValidateRepairQualityEvidenceInput,
)
from forge.repair_quality.scoring import RepairQualityScorer


class RepairQualityTools:
    """Tool invocation handler for Phase 12 M6 Repair Quality & Reliability."""

    def __init__(
        self,
        repository_path: Path,
        history_store: Optional[HistoricalReliabilityStore] = None,
        policy: Optional[RepairQualityPolicy] = None,
        max_output_chars: int = 50_000,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.max_output_chars = max_output_chars
        self.policy = policy or RepairQualityPolicy()
        self.history_store = history_store or HistoricalReliabilityStore()

        self.evidence_validator = RepairQualityEvidenceValidator()
        self.patch_analyzer = RepairPatchAnalyzer(self.policy)
        self.maintainability_analyzer = RepairMaintainabilityAnalyzer(self.policy)
        self.completeness_analyzer = RepairCompletenessAnalyzer(self.policy)
        self.correctness_evaluator = RepairCorrectnessEvaluator(self.policy)
        self.reliability_analyzer = RepairReliabilityAnalyzer(self.policy, self.history_store)
        self.risk_analyzer = RepairRiskAnalyzer(self.policy)
        self.scorer = RepairQualityScorer(self.policy)
        self.gatekeeper = RepairQualityGateKeeper(self.policy)

        self._requests: Dict[str, RepairQualityRequest] = {}
        self._results: Dict[str, RepairQualityResult] = {}

    def register_result(self, result: RepairQualityResult) -> None:
        """Register result in memory for tool summary lookups."""
        self._results[result.result_id] = result

    # -------------------------------------------------------------
    # Tool Handlers
    # -------------------------------------------------------------

    def create_repair_quality_request(self, args: Dict[str, Any]) -> str:
        """Tool: create_repair_quality_request."""
        try:
            inp = CreateRepairQualityRequestInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        req_id = f"rqreq_{inp.repair_plan_id[:8]}"
        req = RepairQualityRequest(
            request_id=req_id,
            repair_plan_id=inp.repair_plan_id,
            repair_plan_digest=inp.repair_plan_digest,
            execution_id=inp.execution_id,
            applied_patch_digest=inp.applied_patch_digest,
            baseline_tree_digest=inp.baseline_tree_digest,
            verification_id=inp.verification_id,
            verification_status=inp.verification_status,
            workspace_id=inp.workspace_id,
            policy_version=inp.policy_version,
            requested_scope=inp.requested_scope,
        )
        self._requests[req_id] = req
        return req.model_dump_json()

    def validate_repair_quality_evidence(self, args: Dict[str, Any]) -> str:
        """Tool: validate_repair_quality_evidence."""
        try:
            inp = ValidateRepairQualityEvidenceInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        req = self._requests.get(inp.request_id)
        if not req:
            req = RepairQualityRequest(
                request_id=inp.request_id,
                repair_plan_id=inp.repair_plan.get("repair_plan_id", "unknown"),
                repair_plan_digest=inp.repair_plan.get("plan_digest", ""),
                execution_id=inp.execution_result.get("execution_id", "unknown"),
                applied_patch_digest=inp.execution_result.get("applied_patch_digest", ""),
                baseline_tree_digest=inp.execution_result.get("baseline_tree_digest", ""),
                verification_id=inp.verification_result.get("verification_id", "unknown"),
                verification_status=inp.verification_result.get("verification_status", "unknown"),
                workspace_id=inp.execution_result.get("workspace_id", "unknown"),
            )

        is_valid, records, missing, errors = self.evidence_validator.validate_request_bindings(
            request=req,
            repair_plan=inp.repair_plan,
            execution_result=inp.execution_result,
            verification_result=inp.verification_result,
            diagnosis_result=inp.diagnosis_result,
        )

        return json.dumps({
            "is_valid": is_valid,
            "evidence_count": len(records),
            "missing_evidence": missing,
            "validation_errors": errors,
        })

    def analyze_repair_patch_quality(self, args: Dict[str, Any]) -> str:
        """Tool: analyze_repair_patch_quality."""
        try:
            inp = AnalyzeRepairPatchQualityInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        metrics, score = self.patch_analyzer.analyze_patch(
            patch_content=inp.patch_content,
            approved_files=inp.approved_files,
            repair_plan_id=inp.repair_plan_id,
        )
        return json.dumps({
            "metrics": metrics,
            "dimension_score": score.model_dump(),
        })

    def analyze_repair_maintainability(self, args: Dict[str, Any]) -> str:
        """Tool: analyze_repair_maintainability."""
        try:
            inp = AnalyzeRepairMaintainabilityInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        ws_path = Path(inp.workspace_path)
        base_path = Path(inp.baseline_path) if inp.baseline_path else None
        metrics, score = self.maintainability_analyzer.analyze_maintainability(
            workspace_path=ws_path,
            modified_files=inp.modified_files,
            baseline_path=base_path,
        )
        return json.dumps({
            "metrics": metrics,
            "dimension_score": score.model_dump(),
        })

    def analyze_repair_completeness(self, args: Dict[str, Any]) -> str:
        """Tool: analyze_repair_completeness."""
        try:
            inp = AnalyzeRepairCompletenessInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        metrics, score = self.completeness_analyzer.analyze_completeness(
            repair_plan=inp.repair_plan,
            modified_files=inp.modified_files,
            verification_result=inp.verification_result,
        )
        return json.dumps({
            "metrics": metrics,
            "dimension_score": score.model_dump(),
        })

    def evaluate_repair_correctness(self, args: Dict[str, Any]) -> str:
        """Tool: evaluate_repair_correctness."""
        try:
            inp = EvaluateRepairCorrectnessInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        metrics, score = self.correctness_evaluator.evaluate_correctness(
            repair_plan=inp.repair_plan,
            verification_result=inp.verification_result,
            modified_files=inp.modified_files,
            root_cause_locations=inp.root_cause_locations,
        )
        return json.dumps({
            "metrics": metrics,
            "dimension_score": score.model_dump(),
        })

    def analyze_repair_reliability(self, args: Dict[str, Any]) -> str:
        """Tool: analyze_repair_reliability."""
        try:
            inp = AnalyzeRepairReliabilityInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        metrics, score, confidence = self.reliability_analyzer.analyze_reliability(
            verification_result=inp.verification_result,
            strategy_id=inp.strategy_id,
            target_module=inp.target_module,
        )
        return json.dumps({
            "metrics": metrics,
            "dimension_score": score.model_dump(),
            "confidence": confidence.value,
        })

    def analyze_repair_risk(self, args: Dict[str, Any]) -> str:
        """Tool: analyze_repair_risk."""
        try:
            inp = AnalyzeRepairRiskInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        metrics, score, risk_level = self.risk_analyzer.analyze_risk(
            patch_content=inp.patch_content,
            modified_files=inp.modified_files,
            metrics=inp.metrics,
        )
        return json.dumps({
            "metrics": metrics,
            "dimension_score": score.model_dump(),
            "risk_severity": risk_level.value,
        })

    def calculate_repair_quality_score(self, args: Dict[str, Any]) -> str:
        """Tool: calculate_repair_quality_score."""
        try:
            inp = CalculateRepairQualityScoreInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        parsed_scores: Dict[QualityDimension, QualityDimensionScore] = {}
        for dim_name, s_data in inp.dimension_scores.items():
            try:
                dim_enum = QualityDimension(dim_name)
                parsed_scores[dim_enum] = QualityDimensionScore.model_validate(s_data)
            except Exception:
                pass

        score, violations = self.scorer.calculate_score(dimension_scores=parsed_scores)
        return json.dumps({
            "overall_score": score,
            "policy_violations": violations,
        })

    def evaluate_repair_quality_gates(self, args: Dict[str, Any]) -> str:
        """Tool: evaluate_repair_quality_gates."""
        try:
            inp = EvaluateRepairQualityGatesInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        parsed_scores: Dict[QualityDimension, QualityDimensionScore] = {}
        for dim_name, s_data in inp.dimension_scores.items():
            try:
                dim_enum = QualityDimension(dim_name)
                parsed_scores[dim_enum] = QualityDimensionScore.model_validate(s_data)
            except Exception:
                pass

        metrics = RepairQualityMetrics.model_validate(inp.metrics)
        status, decision, reasons = self.gatekeeper.evaluate_gates(
            overall_score=inp.overall_score,
            dimension_scores=parsed_scores,
            metrics=metrics,
            policy_violations=inp.policy_violations,
            missing_evidence=inp.missing_evidence,
            risk_level=RiskSeverity(inp.risk_level),
            verification_status=inp.verification_status,
        )
        return json.dumps({
            "quality_status": status.value,
            "gate_decision": decision.value,
            "reasons": reasons,
        })

    def inspect_repair_quality_history(self, args: Dict[str, Any]) -> str:
        """Tool: inspect_repair_quality_history."""
        try:
            inp = InspectRepairQualityHistoryInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        history = self.history_store.get_history(
            strategy_id=inp.strategy_id,
            target_file=inp.target_file,
            limit=inp.limit,
        )
        return json.dumps({
            "records": history,
            "count": len(history),
        })

    def get_repair_quality_summary(self, args: Dict[str, Any]) -> str:
        """Tool: get_repair_quality_summary."""
        try:
            inp = GetRepairQualitySummaryInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        res = self._results.get(inp.result_id)
        if not res:
            return json.dumps({"error": f"Result '{inp.result_id}' not found."})

        lines = [
            f"# Repair Quality Summary: {res.result_id}",
            f"- **Quality Status**: `{res.quality_status.value}`",
            f"- **Gate Decision**: `{res.gate_decision.value}`",
            f"- **Overall Score**: `{res.overall_score if res.overall_score is not None else 'N/A'}`",
            f"- **Risk Level**: `{res.risk_level.value}`",
            f"- **Confidence**: `{res.confidence.value}`",
            f"- **Human Review Required**: `{res.requires_human_review}`",
            "\n### Dimension Scores:",
        ]
        for dim, ds in res.dimension_scores.items():
            s_str = f"{ds.score:.2f}" if ds.score is not None else "N/A"
            lines.append(f"- **{dim.value}**: {s_str} ({ds.status.value}) - {ds.explanation}")

        if res.policy_violations:
            lines.append("\n### Policy Violations:")
            for pv in res.policy_violations:
                lines.append(f"- {pv}")

        return json.dumps({
            "result_id": res.result_id,
            "summary_markdown": "\n".join(lines),
            "gate_decision": res.gate_decision.value,
            "requires_human_review": res.requires_human_review,
        })

    # -------------------------------------------------------------
    # Tool Definitions
    # -------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinition schemas for all 12 quality tools."""
        return [
            ToolDefinition(
                name="create_repair_quality_request",
                description="Bind quality evaluation inputs to verified plan, execution, and verification digests.",
                parameters=CreateRepairQualityRequestInput.model_json_schema(),
            ),
            ToolDefinition(
                name="validate_repair_quality_evidence",
                description="Verify cryptographic bindings and freshness across M2, M3, M4, and M5 milestones.",
                parameters=ValidateRepairQualityEvidenceInput.model_json_schema(),
            ),
            ToolDefinition(
                name="analyze_repair_patch_quality",
                description="Evaluate patch size, proportionality, concentration, and scope alignment.",
                parameters=AnalyzeRepairPatchQualityInput.model_json_schema(),
            ),
            ToolDefinition(
                name="analyze_repair_maintainability",
                description="Evaluate AST cyclomatic complexity delta, function length changes, and API alterations.",
                parameters=AnalyzeRepairMaintainabilityInput.model_json_schema(),
            ),
            ToolDefinition(
                name="analyze_repair_completeness",
                description="Audit whether all required changes and affected files were comprehensively addressed.",
                parameters=AnalyzeRepairCompletenessInput.model_json_schema(),
            ),
            ToolDefinition(
                name="evaluate_repair_correctness",
                description="Evaluate root-cause alignment, strategy conformance, and M5 verification outcome.",
                parameters=EvaluateRepairCorrectnessInput.model_json_schema(),
            ),
            ToolDefinition(
                name="analyze_repair_reliability",
                description="Evaluate baseline regression classifications and historical strategy stability.",
                parameters=AnalyzeRepairReliabilityInput.model_json_schema(),
            ),
            ToolDefinition(
                name="analyze_repair_risk",
                description="Perform static inspection for security-sensitive APIs, credentials, and dependency changes.",
                parameters=AnalyzeRepairRiskInput.model_json_schema(),
            ),
            ToolDefinition(
                name="calculate_repair_quality_score",
                description="Calculate deterministic multi-dimensional composite quality score.",
                parameters=CalculateRepairQualityScoreInput.model_json_schema(),
            ),
            ToolDefinition(
                name="evaluate_repair_quality_gates",
                description="Evaluate fail-closed quality gate state machine for repair authorization.",
                parameters=EvaluateRepairQualityGatesInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_repair_quality_history",
                description="Query historical repair reliability records and success rates from bounded storage.",
                parameters=InspectRepairQualityHistoryInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_repair_quality_summary",
                description="Retrieve formatted markdown summary of repair quality evaluation.",
                parameters=GetRepairQualitySummaryInput.model_json_schema(),
            ),
        ]
