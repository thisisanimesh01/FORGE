"""Phase 12 M6: Repair Quality Executor.

Orchestrates the complete multi-dimensional repair quality evaluation pipeline:
evidence validation, patch analysis, maintainability analysis, completeness
audit, correctness evaluation, reliability assessment, risk inspection,
transparent scoring, and fail-closed quality gatekeeper decisions.
Operates strictly read-only on the repository and isolated workspaces.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.repair_quality.completeness import RepairCompletenessAnalyzer
from forge.repair_quality.correctness import RepairCorrectnessEvaluator
from forge.repair_quality.events import RepairQualityEventRecorder
from forge.repair_quality.evidence import RepairQualityEvidenceValidator
from forge.repair_quality.gates import RepairQualityGateKeeper
from forge.repair_quality.history import HistoricalReliabilityStore
from forge.repair_quality.maintainability import RepairMaintainabilityAnalyzer
from forge.repair_quality.patch_analysis import RepairPatchAnalyzer
from forge.repair_quality.reliability import RepairReliabilityAnalyzer
from forge.repair_quality.risk import RepairRiskAnalyzer
from forge.repair_quality.schemas import (
    ConfidenceClassification,
    QualityDimension,
    QualityDimensionScore,
    QualityGateDecision,
    QualityStatus,
    RepairQualityEventType,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityRequest,
    RepairQualityResult,
    RiskSeverity,
)
from forge.repair_quality.scoring import RepairQualityScorer


class RepairQualityExecutor:
    """Coordinates end-to-end evaluation of repair engineering quality and reliability."""

    def __init__(
        self,
        policy: Optional[RepairQualityPolicy] = None,
        history_store: Optional[HistoricalReliabilityStore] = None,
    ):
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

    def evaluate_quality(
        self,
        request: RepairQualityRequest,
        repair_plan: Dict[str, Any],
        execution_result: Dict[str, Any],
        verification_result: Dict[str, Any],
        patch_content: str,
        workspace_path: Path,
        baseline_path: Optional[Path] = None,
        diagnosis_result: Optional[Dict[str, Any]] = None,
        root_cause_locations: Optional[List[str]] = None,
        strategy_id: Optional[str] = None,
    ) -> RepairQualityResult:
        """Execute the complete repair quality evaluation pipeline."""
        analysis_id = f"rqa_{request.request_id[:8]}"
        events = RepairQualityEventRecorder(analysis_id)
        events.record_event(
            RepairQualityEventType.QUALITY_ANALYSIS_STARTED,
            f"Started repair quality evaluation for request {request.request_id}",
        )

        all_findings: List[str] = []

        # 1. Evidence Validation
        is_evidence_valid, evidence_records, missing_evidence, val_errors = (
            self.evidence_validator.validate_request_bindings(
                request=request,
                repair_plan=repair_plan,
                execution_result=execution_result,
                verification_result=verification_result,
                diagnosis_result=diagnosis_result,
            )
        )
        all_findings.extend(val_errors)
        events.record_event(
            RepairQualityEventType.EVIDENCE_VALIDATED,
            f"Validated evidence: valid={is_evidence_valid}, items={len(evidence_records)}, missing={missing_evidence}",
            outcome="success" if is_evidence_valid else "failure",
        )

        # 2. Extract approved files & modified files
        approved_files = (
            repair_plan.get("approved_file_paths")
            or repair_plan.get("affected_files")
            or []
        )
        modified_files = execution_result.get("modified_files") or []

        # 3. Patch Analysis
        patch_metrics, patch_score = self.patch_analyzer.analyze_patch(
            patch_content=patch_content,
            approved_files=approved_files,
            repair_plan_id=request.repair_plan_id,
        )
        all_findings.extend(patch_score.findings)
        events.record_event(
            RepairQualityEventType.PATCH_QUALITY_ANALYZED,
            f"Analyzed patch: lines={patch_metrics.get('patch_total_lines')}, score={patch_score.score}",
        )

        # 4. Maintainability Analysis
        maint_metrics, maint_score = self.maintainability_analyzer.analyze_maintainability(
            workspace_path=workspace_path,
            modified_files=modified_files,
            baseline_path=baseline_path,
        )
        all_findings.extend(maint_score.findings)
        events.record_event(
            RepairQualityEventType.MAINTAINABILITY_ANALYZED,
            f"Analyzed maintainability: complexity_delta={maint_metrics.get('cyclomatic_complexity_delta')}, score={maint_score.score}",
        )

        # 5. Completeness Analysis
        comp_metrics, comp_score = self.completeness_analyzer.analyze_completeness(
            repair_plan=repair_plan,
            modified_files=modified_files,
            verification_result=verification_result,
        )
        all_findings.extend(comp_score.findings)
        events.record_event(
            RepairQualityEventType.COMPLETENESS_ANALYZED,
            f"Analyzed completeness: addressed={comp_metrics.get('addressed_changes_count')}, score={comp_score.score}",
        )

        # 6. Correctness Evaluation
        corr_metrics, corr_score = self.correctness_evaluator.evaluate_correctness(
            repair_plan=repair_plan,
            verification_result=verification_result,
            modified_files=modified_files,
            root_cause_locations=root_cause_locations,
        )
        all_findings.extend(corr_score.findings)
        events.record_event(
            RepairQualityEventType.CORRECTNESS_EVALUATED,
            f"Evaluated correctness: verification_status={corr_metrics.get('verification_status')}, score={corr_score.score}",
        )

        # 7. Reliability Assessment
        selected_strat = strategy_id or repair_plan.get("selected_strategy_id")
        rel_metrics, rel_score, confidence = self.reliability_analyzer.analyze_reliability(
            verification_result=verification_result,
            strategy_id=selected_strat,
        )
        all_findings.extend(rel_score.findings)
        events.record_event(
            RepairQualityEventType.RELIABILITY_ANALYZED,
            f"Analyzed reliability: score={rel_score.score}, confidence={confidence.value}",
        )

        # 8. Risk Assessment
        risk_metrics, risk_score, risk_level = self.risk_analyzer.analyze_risk(
            patch_content=patch_content,
            modified_files=modified_files,
            metrics=maint_metrics,
        )
        all_findings.extend(risk_score.findings)
        events.record_event(
            RepairQualityEventType.RISK_ANALYZED,
            f"Analyzed risk: severity={risk_level.value}, score={risk_score.score}",
        )

        # Map all dimension scores
        dimension_scores = {
            QualityDimension.CORRECTNESS: corr_score,
            QualityDimension.COMPLETENESS: comp_score,
            QualityDimension.MAINTAINABILITY: maint_score,
            QualityDimension.PATCH_QUALITY: patch_score,
            QualityDimension.RELIABILITY: rel_score,
            QualityDimension.SECURITY_RISK: risk_score,
        }

        # 9. Score Calculation
        regressions_detected = corr_metrics.get("has_regression", False) or rel_metrics.get("regressions_detected", False)
        overall_score, policy_violations = self.scorer.calculate_score(
            dimension_scores=dimension_scores,
            risk_level=risk_level,
            regressions_detected=regressions_detected,
        )
        events.record_event(
            RepairQualityEventType.QUALITY_SCORE_CALCULATED,
            f"Calculated score: overall={overall_score}, violations={len(policy_violations)}",
        )

        # 10. Synthesize Complete Metrics
        metrics = RepairQualityMetrics(
            patch_lines_added=patch_metrics.get("patch_lines_added", 0),
            patch_lines_removed=patch_metrics.get("patch_lines_removed", 0),
            patch_total_lines=patch_metrics.get("patch_total_lines", 0),
            files_changed_count=patch_metrics.get("files_changed_count", 0),
            change_concentration=patch_metrics.get("change_concentration", 1.0),
            unnecessary_change_indicators=patch_metrics.get("unnecessary_change_indicators", []),
            cyclomatic_complexity_delta=maint_metrics.get("cyclomatic_complexity_delta"),
            function_length_delta=maint_metrics.get("function_length_delta"),
            duplication_indicators=maint_metrics.get("duplication_indicators", []),
            dependency_changes=maint_metrics.get("dependency_changes", []),
            public_api_changes=maint_metrics.get("public_api_changes", []),
            type_lint_findings=maint_metrics.get("type_lint_findings", []),
            dead_code_indicators=maint_metrics.get("dead_code_indicators", []),
            verification_completeness_ratio=comp_metrics.get("verification_completeness_ratio"),
            regressions_detected_count=1 if regressions_detected else 0,
            pre_existing_failures_count=1 if rel_metrics.get("pre_existing_failures_observed") else 0,
            improvements_count=1 if rel_metrics.get("improvements_observed") else 0,
            complexity_measured=maint_metrics.get("complexity_measured", False),
            historical_data_available=rel_metrics.get("historical_data_available", False),
            historical_status=rel_metrics.get("historical_status", "empty"),
            historical_error_observed=rel_metrics.get("historical_error_observed", False),
        )

        # 11. Evaluate Quality Gates
        verif_status = verification_result.get("verification_status", "unknown")
        quality_status, gate_decision, gate_reasons = self.gatekeeper.evaluate_gates(
            overall_score=overall_score,
            dimension_scores=dimension_scores,
            metrics=metrics,
            policy_violations=policy_violations,
            missing_evidence=missing_evidence,
            risk_level=risk_level,
            verification_status=verif_status,
            is_evidence_valid=is_evidence_valid,
            policy_digest=self.policy.compute_digest(),
        )
        all_findings.extend(gate_reasons)
        events.record_event(
            RepairQualityEventType.QUALITY_GATE_EVALUATED,
            f"Evaluated gates: status={quality_status.value}, decision={gate_decision.value}",
        )

        # 12. Record to Historical Storage
        try:
            self.history_store.record_quality_result(
                request_id=request.request_id,
                repair_plan_id=request.repair_plan_id,
                execution_id=request.execution_id,
                strategy_id=selected_strat or "unknown",
                target_files=modified_files,
                quality_status=quality_status.value,
                overall_score=overall_score,
                gate_decision=gate_decision.value,
                risk_level=risk_level.value,
                regressions_count=metrics.regressions_detected_count,
            )
        except Exception:
            pass

        events.record_event(
            RepairQualityEventType.QUALITY_ANALYSIS_COMPLETED,
            f"Quality evaluation completed with decision {gate_decision.value}",
        )

        # Invariant: requires_human_review is permanently True
        result = RepairQualityResult(
            result_id=f"rqr_{request.request_id[:8]}",
            request_id=request.request_id,
            quality_status=quality_status,
            overall_score=overall_score,
            dimension_scores=dimension_scores,
            metrics=metrics,
            evidence=evidence_records,
            findings=all_findings,
            missing_evidence=missing_evidence,
            policy_violations=policy_violations,
            risk_level=risk_level,
            confidence=confidence,
            gate_decision=gate_decision,
            policy_version=self.policy.policy_version,
            policy_digest=self.policy.compute_digest(),
            requires_human_review=True,
        )

        return result
