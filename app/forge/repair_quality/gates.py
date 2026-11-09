"""Phase 12 M6: Quality Gatekeeper Engine.

Enforces strict, fail-closed quality gate transitions.
A repair can only transition to PASSED when all 10 non-negotiable
quality and verification criteria are completely satisfied.
"""

from typing import Dict, List, Optional, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityGateDecision,
    QualityStatus,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RiskSeverity,
)


class RepairQualityGateKeeper:
    """Enforces multi-stage fail-closed gates for repair quality authorization."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def evaluate_gates(
        self,
        overall_score: Optional[float],
        dimension_scores: Dict[QualityDimension, QualityDimensionScore],
        metrics: RepairQualityMetrics,
        policy_violations: List[str],
        missing_evidence: List[str],
        risk_level: RiskSeverity,
        verification_status: str,
        is_evidence_valid: bool = True,
        policy_digest: Optional[str] = None,
    ) -> Tuple[QualityStatus, QualityGateDecision, List[str]]:
        """Evaluate all quality gates and determine final status and decision."""
        reasons: List[str] = []

        # 0. Policy provenance & digest verification (prevent policy substitution attacks)
        if policy_digest is not None:
            expected_digest = self.policy.compute_digest()
            if policy_digest != expected_digest:
                reasons.append(
                    f"Policy substitution detected: score was computed under digest {policy_digest[:8]}... "
                    f"but gatekeeper was configured with {expected_digest[:8]}..."
                )
                return QualityStatus.FAILED, QualityGateDecision.BLOCKED, reasons

        # 1. Missing evidence or invalid bindings
        if not is_evidence_valid or missing_evidence:
            reasons.append(f"Evidence validation failed or missing evidence: {missing_evidence}")
            return QualityStatus.INSUFFICIENT_EVIDENCE, QualityGateDecision.BLOCKED, reasons

        # 1b. Historical reliability store corruption / unavailability
        if metrics.historical_error_observed or metrics.historical_status in ("corrupted", "unavailable", "schema_error"):
            if self.policy.require_all_evidence:
                reasons.append(
                    f"Historical reliability store is {metrics.historical_status}; required historical evidence cannot be verified"
                )
                return QualityStatus.INSUFFICIENT_EVIDENCE, QualityGateDecision.BLOCKED, reasons

        # 2. Regressions detected
        if metrics.regressions_detected_count > 0:
            reasons.append(
                f"Regressions detected ({metrics.regressions_detected_count} test(s) regressed)"
            )
            return QualityStatus.REGRESSION_RISK, QualityGateDecision.FAILED, reasons

        # 3. M5 Verification not PASSED
        if str(verification_status).lower() != "passed":
            reasons.append(f"M5 verification status is '{verification_status}' (must be 'passed')")
            return QualityStatus.INSUFFICIENT_EVIDENCE, QualityGateDecision.FAILED, reasons

        # 4. Critical or High Risk
        if risk_level in (RiskSeverity.CRITICAL, RiskSeverity.HIGH):
            reasons.append(f"Security risk severity is '{risk_level.value}'")
            return QualityStatus.HIGH_RISK, QualityGateDecision.BLOCKED, reasons

        # 5. Completeness & Required Changes
        completeness_dim = dimension_scores.get(QualityDimension.COMPLETENESS)
        if completeness_dim and completeness_dim.status != QualityStatus.PASSED:
            reasons.append("Completeness evaluation failed (unaddressed changes or missing files)")
            return QualityStatus.FAILED, QualityGateDecision.FAILED, reasons

        # 6. Policy Violations
        if policy_violations:
            reasons.extend(policy_violations)
            return QualityStatus.FAILED, QualityGateDecision.FAILED, reasons

        # 7. Overall Score Threshold
        if overall_score is None or overall_score < self.policy.min_overall_score:
            score_str = f"{overall_score:.2f}" if overall_score is not None else "None"
            reasons.append(
                f"Overall score {score_str} does not meet threshold {self.policy.min_overall_score:.2f}"
            )
            return QualityStatus.FAILED, QualityGateDecision.FAILED, reasons

        # 8. All dimension scores must pass minimum threshold
        for dim, d_score in dimension_scores.items():
            if d_score.score is not None and d_score.score < self.policy.min_dimension_score:
                reasons.append(
                    f"Dimension '{dim.value}' score {d_score.score:.2f} below minimum {self.policy.min_dimension_score:.2f}"
                )
                return QualityStatus.FAILED, QualityGateDecision.FAILED, reasons

        # All 10 gates satisfied -> PASSED
        reasons.append(
            f"All quality gates satisfied: overall score {overall_score:.2f}, 0 regressions, low risk"
        )
        return QualityStatus.PASSED, QualityGateDecision.PASSED, reasons
