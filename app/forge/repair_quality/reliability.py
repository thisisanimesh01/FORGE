"""Phase 12 M6: Repair Reliability Analyzer.

Evaluates M5 baseline comparison, test stability, regression classification,
and historical repair reliability across previous runs.
Enforces statistical validity thresholds so small sample sizes are not
misrepresented as conclusive evidence.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.repair_quality.history import HistoricalReliabilityStore
from forge.repair_quality.schemas import (
    ConfidenceClassification,
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
)


class RepairReliabilityAnalyzer:
    """Evaluates short-term verification outcomes and historical reliability trends."""

    def __init__(
        self,
        policy: Optional[RepairQualityPolicy] = None,
        history_store: Optional[HistoricalReliabilityStore] = None,
    ):
        self.policy = policy or RepairQualityPolicy()
        self.history_store = history_store

    def analyze_reliability(
        self,
        verification_result: Dict[str, Any],
        strategy_id: Optional[str] = None,
        target_module: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore, ConfidenceClassification]:
        """Analyze current verification outcomes against historical reliability patterns."""
        findings: List[str] = []

        # 1. Verification Outcome & Regression Classification
        regression_class = str(verification_result.get("regression_classification", "")).lower()
        verif_status = str(verification_result.get("verification_status", "")).lower()

        regressions_detected = "regression_detected" in regression_class
        pre_existing = "pre_existing_failure" in regression_class
        improved = "improved" in regression_class
        no_regression = "no_regression_observed" in regression_class

        # 2. Historical Records & Statistical Significance
        historical_available = False
        strategy_success_rate = 1.0
        past_attempts = 0
        past_regressions = 0
        is_statistically_significant = False

        if self.history_store and strategy_id:
            try:
                stats = self.history_store.get_strategy_stats(strategy_id)
                past_attempts = stats.get("total_attempts", 0)
                if past_attempts > 0:
                    historical_available = True
                    strategy_success_rate = stats.get("success_rate", 0.0)
                    past_regressions = stats.get("regressions_count", 0)

                    min_sample = self.policy.min_statistical_sample_size
                    if past_attempts >= min_sample:
                        is_statistically_significant = True
                        if past_regressions > 0:
                            findings.append(
                                f"Historical warning (N={past_attempts}): strategy '{strategy_id}' previously caused {past_regressions} regression(s)"
                            )
                        if strategy_success_rate < 0.50:
                            findings.append(
                                f"Statistically significant reliability concern (N={past_attempts}): strategy '{strategy_id}' has a low historical success rate of {strategy_success_rate:.1%}"
                            )
                        elif strategy_success_rate >= 0.80:
                            findings.append(
                                f"High historical stability (N={past_attempts}): strategy '{strategy_id}' has a {strategy_success_rate:.1%} success rate"
                            )
                    else:
                        findings.append(
                            f"Historical sample size for strategy '{strategy_id}' (N={past_attempts}) is below statistical threshold ({min_sample}); observing trends without penalizing current repair"
                        )
            except Exception as e:
                findings.append(f"Historical query degraded: {e}")

        # 3. Score & Status Calculation
        if regressions_detected:
            score = 0.0
            status = QualityStatus.REGRESSION_RISK
            findings.append("Active regression detected in M5 verification")
            confidence = ConfidenceClassification.HIGH
        elif verif_status != "passed":
            score = 0.0
            status = QualityStatus.FAILED
            findings.append(f"Verification status is '{verif_status}' rather than 'passed'")
            confidence = ConfidenceClassification.INSUFFICIENT_EVIDENCE
        else:
            # Base passed score
            score = 1.0
            if improved:
                findings.append("Positive verification observation: repair improved previously failing checks")
            if pre_existing:
                findings.append("Pre-existing baseline failures observed; repair did not exacerbate them")

            # Historical adjustment only when statistically significant
            if historical_available:
                if is_statistically_significant:
                    if strategy_success_rate >= 0.80:
                        confidence = ConfidenceClassification.VERY_HIGH
                    elif strategy_success_rate < 0.50:
                        score -= 0.3
                        confidence = ConfidenceClassification.HIGH
                    else:
                        confidence = ConfidenceClassification.HIGH
                else:
                    # Small sample size (N < min_sample)
                    confidence = ConfidenceClassification.MEDIUM
            else:
                confidence = ConfidenceClassification.MEDIUM
                findings.append("No historical data available for strategy; evaluation based solely on current run")

            score = max(0.0, min(1.0, score))
            status = (
                QualityStatus.PASSED
                if score >= self.policy.min_dimension_score
                else QualityStatus.FAILED
            )

        metrics = {
            "regression_classification": regression_class,
            "regressions_detected": regressions_detected,
            "pre_existing_failures_observed": pre_existing,
            "improvements_observed": improved,
            "historical_data_available": historical_available,
            "past_attempts": past_attempts,
            "past_regressions": past_regressions,
            "strategy_success_rate": strategy_success_rate,
            "is_statistically_significant": is_statistically_significant,
        }

        explanation = (
            f"Reliability assessed: regression outcome '{regression_class}', "
            f"verif status '{verif_status}'. History: {past_attempts} prior runs "
            f"(success rate: {strategy_success_rate:.1%}, significant: {is_statistically_significant})."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.RELIABILITY,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.RELIABILITY, 0.15),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=True,
        )

        return metrics, dimension_score, confidence
