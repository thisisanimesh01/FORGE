"""Phase 13 M1: Non-Negotiable Human Approval Boundary.

Enforces strict human authorization before autonomous repair execution:
- Binds approval directly to canonical SHA-256 repair plan digest.
- Verifies strategy ID and permitted file scope allowlists.
- Interrogates M4 durable ApprovalLedger for revocation and replay protection.
- Fails closed on plan changes, scope expansion, expiration, or replay.
- Prevents cross-workflow approval substitution and rejects synthetic/AI identities.
"""

import os
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
        if trusted_approvers is not None:
            self.trusted_approvers: Set[str] = set(trusted_approvers)
        else:
            raw_env = os.environ.get("FORGE_TRUSTED_APPROVERS", "")
            self.trusted_approvers = {x.strip() for x in raw_env.split(",") if x.strip()}

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

        # 0. Mandatory Allowlist Enforcement: Fail closed if absent, empty, or misconfigured
        if not self.trusted_approvers:
            return ApprovalVerificationOutcome(
                False,
                [
                    "Approval configuration error: No trusted human approver allowlist configured. "
                    "Production approval strictly requires an explicitly configured, non-empty trusted approver allowlist "
                    "(set via trusted_approvers or FORGE_TRUSTED_APPROVERS). All approvals fail closed."
                ],
            )

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
        if approval.approval_id in consumed_map or self.ledger.is_consumed(approval.approval_id):
            exec_id = consumed_map.get(approval.approval_id, "recorded_execution")
            errors.append(
                f"Replay detected: approval '{approval.approval_id}' has already been consumed "
                f"by execution '{exec_id}'."
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
        elif approver_raw not in self.trusted_approvers:
            errors.append(
                f"Unauthorized approver: Identity '{approver_raw}' is not an authorized human principal "
                f"in the configured trusted approver allowlist."
            )

        # 7b. Authentication Channel & Principal Verification (Strict Allowlist)
        allowed_auth_channels = {"interactive_cli", "trusted_operator", "cli"}
        auth_chan = getattr(approval, "auth_channel", "interactive_cli")
        if not auth_chan or auth_chan not in allowed_auth_channels:
            errors.append(
                f"Untrusted approval channel '{auth_chan}': Autonomous tool callers or untrusted processes "
                "cannot manufacture human approval. Approval must originate from an authenticated human operator."
            )

        op_principal = getattr(approval, "operator_principal", None)
        if op_principal is not None:
            op_clean = str(op_principal).strip()
            op_lower = op_clean.lower()
            if (
                not op_clean
                or op_lower in prohibited_identities
                or any(term in op_lower for term in ("llm", "subagent", "synthetic", "auto"))
            ):
                errors.append(
                    f"Invalid operator principal '{op_principal}': AI, model, agent, or synthetic principals are strictly forbidden."
                )
            elif op_clean not in self.trusted_approvers:
                errors.append(
                    f"Unauthorized operator principal: '{op_clean}' is not in the configured trusted approver allowlist."
                )

        # 8. Cross-workflow approval substitution check (durable across restarts)
        bound_wf = self._approved_workflows.get(approval.approval_id) or self.ledger.get_workflow_for_approval(approval.approval_id)
        if bound_wf and workflow_id and bound_wf != workflow_id:
            errors.append(
                f"Cross-workflow approval substitution: approval '{approval.approval_id}' is bound to workflow '{bound_wf}', "
                f"cannot be reused for workflow '{workflow_id}'."
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
        wf_id = workflow_id or "unspecified_workflow"
        self._approved_workflows[approval.approval_id] = wf_id

        # Persist to durable ledger (survives process restart)
        auth_chan = getattr(approval, "auth_channel", "interactive_cli")
        op_principal = getattr(approval, "operator_principal", None)
        self.ledger.record_approval(
            approval_id=approval.approval_id,
            workflow_id=wf_id,
            repair_plan_id=plan.plan_id,
            plan_digest=approval.plan_digest,
            approved_strategy_id=approval.approved_strategy_id,
            approved_file_paths=approval.approved_file_paths,
            approval_timestamp=approval.approval_timestamp,
            approval_expiry=approval.approval_expiry,
            approver_reference=approval.approver_reference,
            auth_channel=auth_chan,
            operator_principal=op_principal,
        )

    def is_approved(self, plan_id: str) -> bool:
        """Check if a plan has an active, unconsumed, unrevoked, unexpired approval."""
        if plan_id in self._approved_plans:
            appr = self._approved_plans[plan_id]
            if (
                not appr.is_expired()
                and not self.ledger.is_revoked(appr.approval_id)
                and not self.ledger.is_consumed(appr.approval_id)
            ):
                return True
        persisted = self.ledger.get_approval_for_plan(plan_id)
        if persisted:
            appr_id = persisted["approval_id"]
            if not self.ledger.is_revoked(appr_id) and not self.ledger.is_consumed(appr_id):
                hydrated = self.get_approval(plan_id)
                if hydrated and not hydrated.is_expired():
                    return True
        return False

    def get_approval(self, plan_id: str) -> Optional[RepairApproval]:
        """Retrieve approved contract for a plan."""
        appr = self._approved_plans.get(plan_id)
        if appr:
            return appr
        persisted = self.ledger.get_approval_for_plan(plan_id)
        if persisted:
            return RepairApproval(
                approval_id=persisted["approval_id"],
                repair_plan_id=persisted["repair_plan_id"],
                plan_digest=persisted["plan_digest"],
                approved_strategy_id=persisted["approved_strategy_id"],
                approved_file_paths=persisted["approved_file_paths"],
                approval_timestamp=persisted["approval_timestamp"],
                approval_expiry=persisted["approval_expiry"],
                approver_reference=persisted["approver_reference"],
                approval_status=ApprovalStatus(persisted.get("status", "approved")),
                auth_channel=persisted.get("auth_channel", "interactive_cli"),
                operator_principal=persisted.get("operator_principal"),
            )
        return None

    def revoke_approval(self, plan_id: str, reason: str = "revoked by operator") -> None:
        """Revoke approval for a plan."""
        appr = self._approved_plans.pop(plan_id, None)
        if appr:
            self._approved_workflows.pop(appr.approval_id, None)
            self.ledger.record_revoked(appr.approval_id, reason)
        else:
            persisted = self.ledger.get_approval_for_plan(plan_id)
            if persisted:
                self.ledger.record_revoked(persisted["approval_id"], reason)
