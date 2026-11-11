"""Phase 12 M5: Verification Gates and State Transitions.

Enforces strict verification gate evaluation:
- Fail-closed if sandbox execution is unavailable (SANDBOX_UNAVAILABLE / BLOCKED).
- Fail-closed if baseline snapshot is missing, corrupted, or modified (RECOVERY_REQUIRED).
- Fail-closed if workspace contains unauthorized mutations or escaping symlinks.
- Fail-closed if any required check failed, timed out, crashed, or encountered collection errors.
- Fail-closed if regressions are detected when policy forbids regressions.
- Only marks PASSED if all required gates pass with verifiable evidence.
"""

from typing import Dict, List, Optional, Tuple

from forge.repair_execution.schemas import ExecutionStatus, RepairExecutionResult
from forge.repair_verification.schemas import (
    CheckOutcome,
    RegressionClassification,
    SuiteExecutionSummary,
    VerificationPlan,
    VerificationPolicy,
    VerificationStatus,
)


class VerificationGateKeeper:
    """Evaluates strict gate criteria for repair verification."""

    @classmethod
    def evaluate_gates(
        cls,
        execution_result: RepairExecutionResult,
        plan: VerificationPlan,
        sandbox_available: bool,
        baseline_summary: Optional[SuiteExecutionSummary],
        repaired_summary: SuiteExecutionSummary,
        regression_class: RegressionClassification,
        workspace_integrity_passed: bool,
        integrity_violations: List[str],
    ) -> Tuple[VerificationStatus, str, List[str], List[str], List[str]]:
        """Evaluate all verification gates.

        Returns:
            Tuple of:
            - final_status: VerificationStatus
            - gate_decision: str ("PASSED", "FAILED", "BLOCKED", "RECOVERY_REQUIRED")
            - required_completed: List[str]
            - required_failed: List[str]
            - optional_failed: List[str]
        """
        required_completed: List[str] = []
        required_failed: List[str] = []
        optional_failed: List[str] = []

        # Gate 0: Execution state verification
        if execution_result.execution_status != ExecutionStatus.APPLIED:
            return (
                VerificationStatus.BLOCKED,
                "BLOCKED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # Gate 0b: Cryptographic digest alignment
        if plan.expected_patch_digest != execution_result.applied_patch_digest:
            return (
                VerificationStatus.BLOCKED,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )
        if baseline_summary and baseline_summary.tree_digest and plan.expected_baseline_digest != baseline_summary.tree_digest:
            return (
                VerificationStatus.BLOCKED,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # Gate 1: Sandbox availability
        if not sandbox_available:
            return (
                VerificationStatus.SANDBOX_UNAVAILABLE,
                "BLOCKED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # Gate 2: Workspace & Snapshot Integrity
        if not workspace_integrity_passed:
            return (
                VerificationStatus.RECOVERY_REQUIRED,
                "RECOVERY_REQUIRED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # Gate 3: Evaluate checks in repaired suite
        if not plan.selected_checks:
            return (
                VerificationStatus.INCOMPLETE,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )

        rep_checks_map = {c.check_id: c for c in repaired_summary.checks}

        for chk in plan.selected_checks:
            result = rep_checks_map.get(chk.check_id)
            if not result:
                if chk.is_required:
                    required_failed.append(chk.check_id)
                else:
                    optional_failed.append(chk.check_id)
                continue

            # Check outcome
            if result.outcome == CheckOutcome.PASSED:
                if chk.is_required:
                    required_completed.append(chk.check_id)
            else:
                if chk.is_required:
                    required_failed.append(chk.check_id)
                else:
                    optional_failed.append(chk.check_id)

        # Gate 4: Regression & Baseline Completeness evaluation
        if plan.policy.fail_on_regression and regression_class == RegressionClassification.REGRESSION_DETECTED:
            return (
                VerificationStatus.REGRESSION_DETECTED,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )

        if plan.policy.require_baseline_comparison and (
            baseline_summary is None
            or regression_class
            in (
                RegressionClassification.INCONCLUSIVE,
                RegressionClassification.ENVIRONMENT_MISMATCH,
            )
        ):
            return (
                VerificationStatus.INCOMPLETE,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # Gate 5: Required checks failure check
        if required_failed:
            return (
                VerificationStatus.FAILED,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # Gate 6: Completed checks check (must have at least one completed required check)
        if not required_completed:
            return (
                VerificationStatus.INCOMPLETE,
                "FAILED",
                required_completed,
                required_failed,
                optional_failed,
            )

        # All gates passed
        return (
            VerificationStatus.PASSED,
            "PASSED",
            required_completed,
            required_failed,
            optional_failed,
        )
