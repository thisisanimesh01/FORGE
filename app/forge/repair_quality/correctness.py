"""Phase 12 M6: Repair Correctness Evaluator.

Evaluates root-cause alignment, strategy conformance, and M5 verification evidence.
Fails closed: an unverified repair or a repair with failing tests cannot be rated correct.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
)


class RepairCorrectnessEvaluator:
    """Evaluates evidence-backed correctness of an applied repair."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def evaluate_correctness(
        self,
        repair_plan: Dict[str, Any],
        verification_result: Dict[str, Any],
        modified_files: List[str],
        root_cause_locations: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore]:
        """Evaluate repair correctness based on verification evidence and root cause alignment."""
        findings: List[str] = []
        normalized_modified = {f.strip("/").replace("\\", "/") for f in modified_files}

        # 1. Root Cause Alignment
        rc_aligned = True
        if root_cause_locations:
            matched_rc = False
            for loc in root_cause_locations:
                # loc can be file path or file:line
                loc_file = loc.split(":")[0].strip("/").replace("\\", "/")
                if loc_file in normalized_modified:
                    matched_rc = True
                    break
            if not matched_rc:
                rc_aligned = False
                findings.append(
                    f"Root cause misalignment: modified files {list(normalized_modified)} do not cover root cause locations {root_cause_locations}"
                )

        # 2. Strategy Conformance
        selected_strategy = repair_plan.get("selected_strategy_id", "unknown")
        strategy_matched = True
        if not selected_strategy or selected_strategy == "unknown":
            findings.append("Selected repair strategy is unspecified")
            strategy_matched = False

        # 3. Verification Evidence
        verif_status = str(verification_result.get("verification_status", "")).lower()
        req_failed = verification_result.get("required_checks_failed") or []
        req_completed = verification_result.get("required_checks_completed") or []
        regression_class = str(verification_result.get("regression_classification", "")).lower()

        is_passed = (verif_status == "passed") and (not req_failed) and (len(req_completed) > 0)
        has_regression = "regression_detected" in regression_class

        # Score determination
        if has_regression:
            score = 0.0
            status = QualityStatus.REGRESSION_RISK
            findings.append("M5 verification detected regressions; correctness score clamped to 0.0")
        elif not is_passed:
            score = 0.0
            status = QualityStatus.FAILED
            findings.append(
                f"M5 verification did not pass (status: '{verif_status}', failed checks: {req_failed}); correctness score clamped to 0.0"
            )
        else:
            # Base verified score
            score = 1.0
            if not rc_aligned:
                score -= 0.3
            if not strategy_matched:
                score -= 0.1

            score = max(0.0, min(1.0, score))
            status = (
                QualityStatus.PASSED
                if score >= self.policy.min_dimension_score
                else QualityStatus.FAILED
            )

        metrics = {
            "root_cause_aligned": rc_aligned,
            "strategy_matched": strategy_matched,
            "verification_status": verif_status,
            "has_regression": has_regression,
            "required_checks_passed_count": len(req_completed),
            "required_checks_failed_count": len(req_failed),
        }

        explanation = (
            f"Correctness evaluated: verification '{verif_status}', "
            f"regression: '{regression_class}', RC aligned: {rc_aligned}. "
            f"Passed checks: {len(req_completed)}, Failed checks: {len(req_failed)}."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.CORRECTNESS,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.CORRECTNESS, 0.25),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=True,
        )

        return metrics, dimension_score
