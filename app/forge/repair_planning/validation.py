"""Phase 12 M3: Plan Invariant and Feasibility Validation.

Enforces non-negotiable safety, certainty, and containment invariants on RepairPlans:
- Execution permission invariant: execution_permitted is strictly False.
- Approval requirement invariant: approval_required is strictly True.
- Workspace containment: all affected files exist within the repository boundary.
- Protected path isolation: protected files cannot be targeted.
- Test existence integrity: existing targeted tests must genuinely exist on disk;
  recommended new tests must be explicitly labeled as proposed.
- Root-cause certainty alignment: uncertain or unresolved causes cannot produce
  actionable high-confidence plans.
"""

from pathlib import Path
from typing import List, Optional, Tuple

from forge.analysis.evidence import normalize_relative_path
from forge.causality.schemas import CausalStatus, RootCauseReport
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path, SecurityError
from forge.repair_planning.schemas import (
    RepairPlan,
    RepairPlanStatus,
    StrategyInvasiveness,
)


class RepairPlanValidator:
    """Validates that a generated RepairPlan satisfies all M3 safety invariants."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path.resolve()

    def validate_plan(
        self,
        plan: RepairPlan,
        root_cause_report: Optional[RootCauseReport] = None,
    ) -> Tuple[bool, List[str], List[str]]:
        """Validate a RepairPlan against all safety and architectural invariants.

        Returns:
            Tuple of (is_valid, validation_errors, checked_invariants)
        """
        errors: List[str] = []
        checked: List[str] = []

        # -------------------------------------------------------------------
        # Invariant 1: Approval Required is strictly True
        # -------------------------------------------------------------------
        checked.append("approval_required_is_true")
        if not plan.approval_required:
            errors.append("Safety invariant violation: plan.approval_required must be True.")

        # -------------------------------------------------------------------
        # Invariant 2: Execution Permitted is strictly False
        # -------------------------------------------------------------------
        checked.append("execution_permitted_is_false")
        if plan.execution_permitted:
            errors.append(
                "Safety invariant violation: plan.execution_permitted must be False in Phase 12 M3 (planning only)."
            )

        # -------------------------------------------------------------------
        # Invariant 3: Affected Files Containment & Existence
        # -------------------------------------------------------------------
        checked.append("affected_files_containment_and_existence")
        for f in plan.affected_files:
            if not f or f == "unknown":
                continue
            norm_path = normalize_relative_path(f)
            if not norm_path:
                errors.append(f"Invalid affected_file path format: '{f}'")
                continue

            if is_protected_path(norm_path, base_dir=self.workspace_path):
                errors.append(f"Protected boundary violation: affected_file '{norm_path}' is protected.")
                continue

            try:
                abs_path = assert_contained_path(self.workspace_path / norm_path, self.workspace_path)
                if not abs_path.exists() or not abs_path.is_file():
                    errors.append(f"Target file '{norm_path}' does not exist on disk as a regular file.")
            except SecurityError as sec_err:
                errors.append(f"Security violation: path traversal on affected_file '{norm_path}': {sec_err}")

        # -------------------------------------------------------------------
        # Invariant 4: Required Changes Line Range Sanity
        # -------------------------------------------------------------------
        checked.append("required_changes_bounds")
        for chg in plan.required_changes:
            if chg.start_line is not None and chg.end_line is not None:
                if chg.end_line < chg.start_line:
                    errors.append(
                        f"Line bound error in required_changes on '{chg.file_path}': "
                        f"end_line ({chg.end_line}) < start_line ({chg.start_line})"
                    )

        # -------------------------------------------------------------------
        # Invariant 5: Root-Cause Certainty Gating
        # -------------------------------------------------------------------
        checked.append("root_cause_certainty_alignment")
        if root_cause_report:
            status = root_cause_report.status

            if status in (CausalStatus.INSUFFICIENT_EVIDENCE, CausalStatus.UNRESOLVED):
                if plan.status == RepairPlanStatus.ACTIONABLE:
                    errors.append(
                        f"Certainty violation: root-cause status '{status.value}' cannot produce an ACTIONABLE repair plan."
                    )
                if plan.selected_strategy_id:
                    # Selected strategy must not be an actionable modification
                    matching = [s for s in plan.candidate_strategies if s.strategy_id == plan.selected_strategy_id]
                    if matching and not matching[0].rejection_reasons:
                        errors.append(
                            f"Certainty violation: unverified status '{status.value}' cannot select an active code repair strategy."
                        )

            elif status == CausalStatus.PLAUSIBLE:
                if plan.status == RepairPlanStatus.ACTIONABLE:
                    errors.append(
                        "Certainty violation: PLAUSIBLE root cause with inferred links must produce a CONDITIONAL repair plan."
                    )

            elif status == CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES:
                if plan.status not in (RepairPlanStatus.MULTIPLE_CANDIDATES, RepairPlanStatus.CONDITIONAL):
                    errors.append(
                        "Certainty violation: MULTIPLE_PLAUSIBLE_ROOT_CAUSES must preserve MULTIPLE_CANDIDATES plan status."
                    )

        # -------------------------------------------------------------------
        # Invariant 6: Verification Test Existence Integrity
        # -------------------------------------------------------------------
        checked.append("verification_test_existence")
        for test_path in plan.verification_plan.existing_targeted_tests:
            norm_test = normalize_relative_path(test_path) or test_path
            abs_test = self.workspace_path / norm_test
            if not abs_test.exists():
                errors.append(
                    f"Fabrication violation: existing_targeted_test '{test_path}' does not exist on disk."
                )

        # Recommended new tests must not claim to be existing files
        for new_test in plan.verification_plan.recommended_new_tests:
            if not ("proposed" in new_test.lower() or "recommended" in new_test.lower() or "new" in new_test.lower()):
                errors.append(
                    f"Labeling violation: recommended_new_test '{new_test}' must explicitly be identified as proposed/new."
                )

        # -------------------------------------------------------------------
        # Invariant 7: Minimality Invariant
        # -------------------------------------------------------------------
        checked.append("strategy_minimality")
        if plan.selected_strategy_id:
            selected_strat = next(
                (s for s in plan.candidate_strategies if s.strategy_id == plan.selected_strategy_id),
                None,
            )
            if selected_strat and selected_strat.invasiveness == StrategyInvasiveness.EXTENSIVE:
                # Check if a minimal strategy was also available
                has_minimal = any(
                    s.invasiveness == StrategyInvasiveness.MINIMAL and not s.rejection_reasons
                    for s in plan.candidate_strategies
                )
                if has_minimal:
                    errors.append(
                        "Minimality violation: selected strategy is EXTENSIVE when a safe MINIMAL strategy was available."
                    )

        is_valid = len(errors) == 0
        return is_valid, errors, checked
