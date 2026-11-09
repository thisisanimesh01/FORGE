"""Phase 12 M6: Quality Scoring Engine.

Computes transparent, reproducible, weighted multi-dimensional quality scores.
Enforces hard non-overridable safety clamps: regressions or verification failures
strictly prevent high overall scores regardless of other dimension points.
"""

from typing import Dict, List, Optional, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
    RiskSeverity,
)


class RepairQualityScorer:
    """Calculates deterministic composite quality scores and audits policy violations."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def calculate_score(
        self,
        dimension_scores: Dict[QualityDimension, QualityDimensionScore],
        risk_level: RiskSeverity = RiskSeverity.LOW,
        regressions_detected: bool = False,
    ) -> Tuple[Optional[float], List[str]]:
        """Calculate weighted composite score and detect policy violations."""
        policy_violations: List[str] = []

        # Audit critical non-overridable violations
        if regressions_detected:
            policy_violations.append("Policy violation: active regressions detected in verification")

        if risk_level in (RiskSeverity.CRITICAL, RiskSeverity.HIGH):
            policy_violations.append(
                f"Policy violation: assessed risk severity is '{risk_level.value}'"
            )

        # Calculate weighted average over measured dimensions
        total_weight = 0.0
        weighted_sum = 0.0

        for dim, dim_score in dimension_scores.items():
            if dim_score.score is not None and dim_score.is_measured:
                weight = self.policy.dimension_weights.get(dim, 0.15)
                weighted_sum += dim_score.score * weight
                total_weight += weight

                if dim_score.score < self.policy.min_dimension_score:
                    policy_violations.append(
                        f"Policy violation: dimension '{dim.value}' score {dim_score.score:.2f} "
                        f"is below minimum threshold {self.policy.min_dimension_score:.2f}"
                    )

        if total_weight <= 0.0:
            return None, policy_violations

        raw_overall = weighted_sum / total_weight

        # Hard clamp rules:
        # If regression detected or critical risk, overall score capped at 0.30
        if regressions_detected or risk_level == RiskSeverity.CRITICAL:
            raw_overall = min(raw_overall, 0.20)
        elif risk_level == RiskSeverity.HIGH:
            raw_overall = min(raw_overall, 0.40)

        # Check correctness dimension specifically
        correctness_dim = dimension_scores.get(QualityDimension.CORRECTNESS)
        if correctness_dim and (correctness_dim.score is None or correctness_dim.score <= 0.0):
            raw_overall = min(raw_overall, 0.30)

        # Check reliability dimension: if evidence is insufficient (e.g. database corrupted), clamp score
        reliability_dim = dimension_scores.get(QualityDimension.RELIABILITY)
        if reliability_dim and reliability_dim.status == QualityStatus.INSUFFICIENT_EVIDENCE:
            policy_violations.append(
                "Policy violation: reliability dimension has insufficient evidence (historical data unavailable or corrupted)"
            )
            raw_overall = min(raw_overall, 0.50)

        final_score = round(max(0.0, min(1.0, raw_overall)), 4)

        if final_score < self.policy.min_overall_score:
            policy_violations.append(
                f"Policy violation: overall score {final_score:.2f} is below minimum required {self.policy.min_overall_score:.2f}"
            )

        return final_score, policy_violations
