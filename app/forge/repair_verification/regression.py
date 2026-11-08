"""Phase 12 M5: Deterministic Regression Analysis Engine.

Compares baseline and repaired verification outcomes to categorize findings:
- NO_REGRESSION_OBSERVED: All checked tests/properties pass across both workspaces.
- REGRESSION_DETECTED: Check passed in baseline but failed/errored in repaired workspace.
- PRE_EXISTING_FAILURE: Check failed in baseline and failed identically in repaired workspace.
- IMPROVED: Check failed in baseline but succeeded in repaired workspace (repair efficacy).
- INCONCLUSIVE: Baseline results missing, abnormal termination, or inconclusive diff.
- ENVIRONMENT_MISMATCH: Incompatible test selection or environment definitions.
"""

from typing import Dict, List, Optional, Tuple

from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    RegressionClassification,
    RegressionFinding,
    SuiteExecutionSummary,
)


class DeterministicRegressionEngine:
    """Evaluates baseline versus repaired execution outcomes without LLM hallucination."""

    @classmethod
    def analyze_regressions(
        cls,
        baseline_summary: Optional[SuiteExecutionSummary],
        repaired_summary: SuiteExecutionSummary,
    ) -> Tuple[RegressionClassification, List[RegressionFinding]]:
        """Compare baseline and repaired suites and determine regression status.

        Returns:
            Tuple of (overall_classification, list_of_findings)
        """
        findings: List[RegressionFinding] = []

        if not baseline_summary:
            # Baseline was not run or unavailable
            if repaired_summary.all_passed:
                return RegressionClassification.NO_REGRESSION_OBSERVED, findings
            return RegressionClassification.VERIFICATION_INCOMPLETE, findings

        if not baseline_summary.sandbox_available or not repaired_summary.sandbox_available:
            return RegressionClassification.INCONCLUSIVE, findings

        # Index baseline checks by check_id
        base_checks_map: Dict[str, CheckExecutionResult] = {
            c.check_id: c for c in baseline_summary.checks
        }

        has_regression = False
        has_improvement = False
        has_preexisting = False

        for rep_check in repaired_summary.checks:
            base_check = base_checks_map.get(rep_check.check_id)

            if not base_check:
                # Check not in baseline - evaluate based on repaired outcome
                if rep_check.outcome != CheckOutcome.PASSED:
                    has_regression = True
                    findings.append(
                        RegressionFinding(
                            check_id=rep_check.check_id,
                            classification=RegressionClassification.REGRESSION_DETECTED,
                            baseline_outcome=None,
                            repaired_outcome=rep_check.outcome,
                            explanation=f"New check {rep_check.check_id} failed in repaired workspace with outcome {rep_check.outcome.value}.",
                        )
                    )
                continue

            # Compare test-level items if available
            base_test_map = {t.test_id: t for t in base_check.test_results}
            rep_test_map = {t.test_id: t for t in rep_check.test_results}

            if base_test_map and rep_test_map:
                for tid, rep_t in rep_test_map.items():
                    base_t = base_test_map.get(tid)
                    if not base_t:
                        if rep_t.outcome != CheckOutcome.PASSED:
                            has_regression = True
                            findings.append(
                                RegressionFinding(
                                    check_id=rep_check.check_id,
                                    test_id=tid,
                                    classification=RegressionClassification.REGRESSION_DETECTED,
                                    baseline_outcome=None,
                                    repaired_outcome=rep_t.outcome,
                                    explanation=f"Test {tid} failed in repaired workspace with {rep_t.outcome.value}.",
                                )
                            )
                        continue

                    # Baseline vs Repaired item
                    if base_t.outcome == CheckOutcome.PASSED and rep_t.outcome != CheckOutcome.PASSED:
                        has_regression = True
                        findings.append(
                            RegressionFinding(
                                check_id=rep_check.check_id,
                                test_id=tid,
                                classification=RegressionClassification.REGRESSION_DETECTED,
                                baseline_outcome=base_t.outcome,
                                repaired_outcome=rep_t.outcome,
                                explanation=f"Regression: test {tid} passed in baseline but failed ({rep_t.outcome.value}) in repaired workspace.",
                            )
                        )
                    elif base_t.outcome != CheckOutcome.PASSED and rep_t.outcome == CheckOutcome.PASSED:
                        has_improvement = True
                        findings.append(
                            RegressionFinding(
                                check_id=rep_check.check_id,
                                test_id=tid,
                                classification=RegressionClassification.IMPROVED,
                                baseline_outcome=base_t.outcome,
                                repaired_outcome=rep_t.outcome,
                                explanation=f"Improvement: test {tid} failed ({base_t.outcome.value}) in baseline but passed in repaired workspace.",
                            )
                        )
                    elif base_t.outcome != CheckOutcome.PASSED and rep_t.outcome != CheckOutcome.PASSED:
                        has_preexisting = True
                        findings.append(
                            RegressionFinding(
                                check_id=rep_check.check_id,
                                test_id=tid,
                                classification=RegressionClassification.PRE_EXISTING_FAILURE,
                                baseline_outcome=base_t.outcome,
                                repaired_outcome=rep_t.outcome,
                                explanation=f"Pre-existing failure: test {tid} failed in both baseline and repaired workspaces.",
                            )
                        )
            else:
                # Aggregate check level comparison
                if base_check.outcome == CheckOutcome.PASSED and rep_check.outcome != CheckOutcome.PASSED:
                    has_regression = True
                    findings.append(
                        RegressionFinding(
                            check_id=rep_check.check_id,
                            classification=RegressionClassification.REGRESSION_DETECTED,
                            baseline_outcome=base_check.outcome,
                            repaired_outcome=rep_check.outcome,
                            explanation=f"Regression: check {rep_check.check_id} passed in baseline but failed ({rep_check.outcome.value}) in repaired workspace.",
                        )
                    )
                elif base_check.outcome != CheckOutcome.PASSED and rep_check.outcome == CheckOutcome.PASSED:
                    has_improvement = True
                    findings.append(
                        RegressionFinding(
                            check_id=rep_check.check_id,
                            classification=RegressionClassification.IMPROVED,
                            baseline_outcome=base_check.outcome,
                            repaired_outcome=rep_check.outcome,
                            explanation=f"Improvement: check {rep_check.check_id} failed in baseline but passed in repaired workspace.",
                        )
                    )
                elif base_check.outcome != CheckOutcome.PASSED and rep_check.outcome != CheckOutcome.PASSED:
                    has_preexisting = True
                    findings.append(
                        RegressionFinding(
                            check_id=rep_check.check_id,
                            classification=RegressionClassification.PRE_EXISTING_FAILURE,
                            baseline_outcome=base_check.outcome,
                            repaired_outcome=rep_check.outcome,
                            explanation=f"Pre-existing failure: check {rep_check.check_id} failed in both baseline and repaired workspaces.",
                        )
                    )

        # Synthesize overall classification
        if has_regression:
            return RegressionClassification.REGRESSION_DETECTED, findings
        if has_improvement and repaired_summary.all_passed:
            return RegressionClassification.IMPROVED, findings
        if repaired_summary.all_passed:
            return RegressionClassification.NO_REGRESSION_OBSERVED, findings
        if has_preexisting and not has_regression:
            return RegressionClassification.PRE_EXISTING_FAILURE, findings

        return RegressionClassification.INCONCLUSIVE, findings
