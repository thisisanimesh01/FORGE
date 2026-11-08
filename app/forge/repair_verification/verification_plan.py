"""Phase 12 M5: Verification Plan Generator.

Constructs strict, validated VerificationPlan contracts:
- Binds plan to execution ID, workspace ID, baseline tree digest, and applied patch digest.
- Integrates intelligent test selection across targeted, regression, and static checks.
- Enforces execution policy constraints (timeouts, resource limits, allowlisted binaries).
- Rejects unapplied, rolled-back, or corrupted executions.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import List, Optional

from forge.repair_execution.schemas import ExecutionStatus, RepairExecutionResult, RepairWorkspace
from forge.repair_verification.schemas import (
    SelectedCheck,
    TestIdentifier,
    VerificationPlan,
    VerificationPolicy,
)
from forge.repair_verification.test_selection import IntelligentTestSelector


class VerificationPlanGenerator:
    """Generates a structured VerificationPlan for an applied repair execution."""

    def __init__(self, policy: Optional[VerificationPolicy] = None) -> None:
        self.policy = policy or VerificationPolicy()

    def generate_plan(
        self,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        custom_checks: Optional[List[SelectedCheck]] = None,
    ) -> VerificationPlan:
        """Synthesize a complete VerificationPlan for an applied repair.

        Raises:
            ValueError: If execution state is not APPLIED or digests are missing.
        """
        if execution_result.execution_status != ExecutionStatus.APPLIED:
            raise ValueError(
                f"Cannot generate verification plan: execution status is '{execution_result.execution_status.value}', expected 'applied'"
            )

        if not execution_result.applied_patch_digest:
            raise ValueError("Cannot generate verification plan: applied_patch_digest is missing")

        if not workspace.baseline_tree_digest:
            raise ValueError("Cannot generate verification plan: workspace baseline_tree_digest is missing")

        workspace_path = Path(workspace.isolated_workspace_path)

        # Select checks using intelligent selector if custom checks are not provided
        if custom_checks is not None:
            selected_checks = custom_checks
            test_identifiers: List[TestIdentifier] = []
            excluded_checks = {}
            is_partial = False
            selection_method = "custom_checks"
            rationale = "User-supplied custom checks"
        else:
            selector = IntelligentTestSelector(workspace_path=workspace_path, policy=self.policy)
            (
                selected_checks,
                test_identifiers,
                excluded_checks,
                is_partial,
                rationale,
            ) = selector.select_checks(modified_files=execution_result.modified_files)
            selection_method = "intelligent_test_selection"

        # Deterministic plan ID
        plan_seed = (
            f"{execution_result.execution_id}:"
            f"{workspace.workspace_id}:"
            f"{execution_result.applied_patch_digest}:"
            f"{len(selected_checks)}"
        )
        plan_hash = hashlib.sha256(plan_seed.encode("utf-8")).hexdigest()[:12]
        plan_id = f"vplan_{plan_hash}"

        return VerificationPlan(
            plan_id=plan_id,
            repair_plan_id=execution_result.repair_plan_id,
            execution_id=execution_result.execution_id,
            workspace_id=workspace.workspace_id,
            selected_checks=selected_checks,
            test_identifiers=test_identifiers,
            selection_method=selection_method,
            selection_rationale=rationale,
            excluded_checks=excluded_checks,
            is_partial_verification=is_partial,
            policy=self.policy,
            expected_baseline_digest=workspace.baseline_tree_digest,
            expected_patch_digest=execution_result.applied_patch_digest,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
