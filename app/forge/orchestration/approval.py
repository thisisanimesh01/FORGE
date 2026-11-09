"""Phase 13 M1: Non-Negotiable Human Approval Boundary.

Enforces strict human authorization before autonomous repair execution:
- Binds approval directly to canonical SHA-256 repair plan digest.
- Verifies strategy ID and permitted file scope allowlists.
- Interrogates M4 durable ApprovalLedger for revocation and replay protection.
- Fails closed on plan changes, scope expansion, expiration, or replay.
- Prevents cross-workflow approval substitution and rejects synthetic/AI identities.
"""

from typing import Dict, List, Optional, Set, Tuple

from forge.repair_execution.approval import RepairApprovalValidator, compute_plan_digest
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.schemas import ApprovalStatus, RepairApproval
from forge.repair_planning.schemas import RepairPlan


class OrchestrationApprovalError(Exception):
    """Base exception for approval boundary rejections."""


ApprovalValidationError = OrchestrationApprovalError


class ApprovalVerificationOutcome:
    """Detailed result of approval verification."""

    def __init__(self, is_valid: bool, errors: Optional[List[str]] = None):
        self.is_valid = is_valid
        self.errors = errors or []

    def __bool__(self) -> bool:
        return self.is_valid


class OrchestrationApprovalManager:
    """Manages the strict human approval gate for engineering orchestration."""

    def __init__(
        self,
        ledger: Optional[ApprovalLedger] = None,
        trusted_approvers: Optional[Set[str]] = None,
    ) -> None:
        self.ledger = ledger or ApprovalLedger()
        self.trusted_approvers = trusted_approvers
        self._m4_validator = RepairApprovalValidator()
        self._approved_plans: Dict[str, RepairApproval] = {}
        self._approved_workflows: Dict[str, str] = {}

    def verify_approval(
        self,
        approval: RepairApproval,
        plan: RepairPlan,
        workflow_id: Optional[str] = None,
    ) -> ApprovalVerificationOutcome:
        """Validate human approval against the active repair plan.

        Verifies:
        1. Plan existence and strategy selection.
        2. Exact SHA-256 canonical plan digest match.
        3. Strategy ID alignment.
        4. Approved scope coverage (no unapproved file modifications or scope expansion).
        5. Expiration and revocation status.
        6. Replay protection via durable ApprovalLedger.
        7. Trusted approver identity and rejection of AI/synthetic identities.
        8. Cross-workflow approval substitution protection.

        Returns:
            ApprovalVerificationOutcome with is_valid=True/False and detailed errors.
        """
        errors: List[str] = []

        if not plan:
            return ApprovalVerificationOutcome(False, ["No repair plan provided for approval verification."])

        if not plan.selected_strategy_id:
            return ApprovalVerificationOutcome(False, ["Repair plan has no selected repair strategy."])

        # 1. Plan digest verification
        expected_digest = compute_plan_digest(plan)
        if approval.plan_digest != expected_digest:
            errors.append(
                f"Plan digest mismatch: approval digest '{approval.plan_digest}' "
                f"does not match current canonical plan digest '{expected_digest}'."
            )

        # 2. Plan ID alignment
        if approval.repair_plan_id != plan.plan_id:
            errors.append(
                f"Plan ID mismatch: approval references plan '{approval.repair_plan_id}', "
                f"but active plan is '{plan.plan_id}'."
            )

        # 3. Strategy ID verification
        if approval.approved_strategy_id and approval.approved_strategy_id != plan.selected_strategy_id:
            errors.append(
                f"Strategy mismatch: approval specifies strategy '{approval.approved_strategy_id}', "
                f"but plan selected '{plan.selected_strategy_id}'."
            )

        # 4. Scope verification (prevent unapproved file modifications and scope expansion)
        if approval.approved_file_paths is not None:
            approved_set = set(approval.approved_file_paths)
            plan_set = set(plan.affected_files)
            for affected_file in plan.affected_files:
                if affected_file not in approved_set:
                    errors.append(
                        f"Scope breach: plan modifies file '{affected_file}' which is outside "
                        f"the approved file scope."
                    )
            for approved_file in approved_set:
                if approved_file not in plan_set:
                    errors.append(
                        f"Scope expansion: approval authorizes file '{approved_file}' which is outside "
                        f"the plan's affected files."
                    )

        # 5. Expiry check
        if approval.approval_status == ApprovalStatus.EXPIRED or approval.is_expired():
            errors.append(f"Approval '{approval.approval_id}' has expired.")

        # 6. Revocation and replay check via durable ledger
        revoked_map = self.ledger.get_revoked_map()
        if approval.approval_id in revoked_map:
            errors.append(f"Approval '{approval.approval_id}' was revoked: {revoked_map[approval.approval_id]}.")

        consumed_map = self.ledger.get_consumed_map()
        if approval.approval_id in consumed_map:
            errors.append(
                f"Replay detected: approval '{approval.approval_id}' has already been consumed "
                f"by execution '{consumed_map[approval.approval_id]}'."
            )

        # 7. Human approver identity verification & rejection of AI/synthetic identities
        prohibited_identities = {
            "unknown",
            "none",
            "auto",
            "llm",
            "ai",
            "model",
            "agent",
            "forge",
            "assistant",
            "subagent",
            "bot",
            "synthetic",
            "system",
        }
        approver_raw = approval.approver_reference.strip() if approval.approver_reference else ""
        approver_lower = approver_raw.lower()

        if (
            not approver_raw
            or approver_lower in prohibited_identities
            or any(term in approver_lower for term in ("llm", "subagent", "synthetic", "auto"))
        ):
            errors.append(
                f"Invalid approver identity '{approval.approver_reference}': Explicit human approver identity "
                "is required (AI, model, agent, auto, or synthetic identities are strictly forbidden)."
            )
        elif self.trusted_approvers is not None:
            if approver_raw not in self.trusted_approvers:
                errors.append(
                    f"Untrusted approver: identity '{approver_raw}' is not in the configured "
                    f"trusted human approver registry."
                )

        # 8. Cross-workflow approval substitution check
        if workflow_id:
            bound_wf = self._approved_workflows.get(approval.approval_id)
            if bound_wf and bound_wf != workflow_id:
                errors.append(
                    f"Cross-workflow approval substitution: approval '{approval.approval_id}' was registered "
                    f"for workflow '{bound_wf}', cannot be reused for workflow '{workflow_id}'."
                )

        # 9. Defense-in-depth with M4 validator
        m4_check = self._m4_validator.validate_approval(
            approval=approval,
            plan=plan,
            revoked_approvals=set(revoked_map.keys()),
            consumed_approvals=consumed_map,
            trusted_approver_identities=self.trusted_approvers,
        )
        if not m4_check.is_valid:
            for err in m4_check.errors:
                if err not in errors:
                    errors.append(err)

        return ApprovalVerificationOutcome(is_valid=len(errors) == 0, errors=errors)

    def validate_and_record_approval(
        self,
        approval: RepairApproval,
        plan: RepairPlan,
        workflow_id: Optional[str] = None,
    ) -> None:
        """Validate human approval and record it, raising ApprovalValidationError on failure."""
        outcome = self.verify_approval(approval, plan, workflow_id=workflow_id)
        if not outcome.is_valid:
            raise ApprovalValidationError("; ".join(outcome.errors))
        self._approved_plans[plan.plan_id] = approval
        if workflow_id:
            self._approved_workflows[approval.approval_id] = workflow_id

    def is_approved(self, plan_id: str) -> bool:
        """Check if a plan has an active validated approval."""
        return plan_id in self._approved_plans

    def get_approval(self, plan_id: str) -> Optional[RepairApproval]:
        """Retrieve approved contract for a plan."""
        return self._approved_plans.get(plan_id)

    def revoke_approval(self, plan_id: str, reason: str = "revoked by operator") -> None:
        """Revoke approval for a plan."""
        appr = self._approved_plans.pop(plan_id, None)
        if appr:
            self._approved_workflows.pop(appr.approval_id, None)
            self.ledger.record_revoked(appr.approval_id, reason)
