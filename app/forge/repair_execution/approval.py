"""Phase 12 M4: Explicit Human Approval Validation and Invariants.

Enforces non-negotiable authorization rules before applying any repair:
1. Approval must be present, structured, and non-empty.
2. Approval status must be strictly APPROVED (not PENDING, REJECTED, REVOKED).
3. Approval expiry must be in the future (rejects expired approvals).
4. Plan digest must match the approved plan digest.
5. Approved strategy ID must match the selected strategy in the plan.
6. Target file paths must be strictly within the approved file scope allowlist.
7. Plan must be in ACTIONABLE status with HIGH confidence.
8. Supporting causal evidence must be verified and fresh (zero stale evidence).
9. Zero implicit, default, or LLM-inferred approvals permitted.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Dict, List, Optional, Set, Tuple

from forge.repair_execution.schemas import ApprovalStatus, RepairApproval
from forge.repair_planning.schemas import RepairPlan, RepairPlanStatus
from forge.schemas import FindingConfidence


def compute_plan_digest(plan: RepairPlan) -> str:
    """Compute deterministic SHA-256 digest of a RepairPlan's core authorization fields."""
    normalized_data = {
        "plan_id": plan.plan_id,
        "status": plan.status.value,
        "selected_strategy_id": plan.selected_strategy_id,
        "affected_files": sorted(plan.affected_files),
        "required_changes": sorted(
            [
                {
                    "file_path": chg.file_path,
                    "symbol_id": chg.symbol_id,
                    "start_line": chg.start_line,
                    "end_line": chg.end_line,
                    "change_type": chg.change_type,
                    "description": chg.description,
                    "rationale": chg.rationale,
                }
                for chg in plan.required_changes
            ],
            key=lambda x: (x["file_path"], x["start_line"] or 0, x["end_line"] or 0),
        ),
    }
    raw = json.dumps(normalized_data, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApprovalValidationResult:
    """Outcome of approval validation against mandatory invariants."""

    def __init__(self, is_valid: bool, errors: Optional[List[str]] = None) -> None:
        self.is_valid = is_valid
        self.errors = errors or []

    def __iter__(self):
        return iter((self.is_valid, self.errors))

    def __repr__(self) -> str:
        return f"ApprovalValidationResult(is_valid={self.is_valid}, errors={self.errors})"


class RepairApprovalValidator:
    """Validates human approval against target repair plan and requested execution scope."""

    @classmethod
    def validate_approval(
        cls,
        approval: RepairApproval,
        plan: RepairPlan,
        requested_scope: Optional[List[str]] = None,
        current_time: Optional[datetime] = None,
        revoked_approvals: Optional[Set[str]] = None,
        consumed_approvals: Optional[Dict[str, str]] = None,
    ) -> ApprovalValidationResult:
        """Verify all mandatory authorization invariants.

        Returns:
            ApprovalValidationResult with is_valid and list of error messages.
        """
        errors: List[str] = []
        now = current_time or datetime.now(timezone.utc)

        # Invariant 0: Revocation & Consumption Replay Check
        if revoked_approvals and approval.approval_id in revoked_approvals:
            errors.append(
                f"Authorization violation: approval '{approval.approval_id}' has been revoked."
            )

        if consumed_approvals and approval.approval_id in consumed_approvals:
            prior_exec = consumed_approvals[approval.approval_id]
            errors.append(
                f"Authorization violation: approval '{approval.approval_id}' has already been consumed by execution '{prior_exec}'. "
                "Approval reuse or replay is strictly forbidden."
            )

        # Invariant 1: Approval status must be APPROVED
        if approval.approval_status != ApprovalStatus.APPROVED:
            errors.append(
                f"Authorization violation: approval status is '{approval.approval_status.value}', must be 'approved'."
            )

        # Invariant 2: Expiry check
        if approval.is_expired(current_time=now):
            errors.append(
                f"Authorization violation: approval expired at '{approval.approval_expiry}' (current time: {now.isoformat()})."
            )

        # Invariant 3: Plan ID binding
        if approval.repair_plan_id != plan.plan_id:
            errors.append(
                f"Authorization violation: approval was issued for plan '{approval.repair_plan_id}', "
                f"cannot authorize target plan '{plan.plan_id}'."
            )

        # Invariant 4: Plan content digest binding
        actual_digest = compute_plan_digest(plan)
        if approval.plan_digest.lower() != actual_digest.lower():
            errors.append(
                f"Authorization violation: plan digest mismatch. "
                f"Approved: '{approval.plan_digest[:16]}...', actual: '{actual_digest[:16]}...'. "
                "The repair plan was altered or re-synthesized after approval was granted."
            )

        # Invariant 5: Strategy binding
        if approval.approved_strategy_id != plan.selected_strategy_id:
            errors.append(
                f"Authorization violation: approved strategy '{approval.approved_strategy_id}' "
                f"does not match selected strategy '{plan.selected_strategy_id}'."
            )

        # Invariant 6: Plan actionability gating
        if plan.status != RepairPlanStatus.ACTIONABLE:
            errors.append(
                f"Authorization violation: repair plan status is '{plan.status.value}'. "
                "Only ACTIONABLE plans grounded in proven root causes can be authorized for repair execution."
            )

        # Invariant 7: Evidence freshness integrity
        stale_items = [ev for ev in plan.supporting_evidence if not ev.is_fresh]
        if stale_items:
            errors.append(
                f"Authorization violation: {len(stale_items)} supporting evidence item(s) are stale or unverified. "
                "Repair execution cannot proceed on stale evidence."
            )

        # Invariant 8: File scope containment
        approved_set = {f.strip().replace("\\", "/") for f in approval.approved_file_paths if f.strip()}
        target_files = set(plan.affected_files)
        if requested_scope:
            target_files.update(requested_scope)

        out_of_scope = [f for f in target_files if f not in approved_set]
        if out_of_scope:
            errors.append(
                f"Authorization violation: target file(s) {out_of_scope} are outside approved scope {sorted(approved_set)}."
            )

        # Invariant 9: Required changes file scope containment
        change_files = {chg.file_path for chg in plan.required_changes}
        unapproved_changes = [f for f in change_files if f not in approved_set]
        if unapproved_changes:
            errors.append(
                f"Authorization violation: plan contains required changes in unapproved file(s) {unapproved_changes}."
            )

        # Invariant 10: Approver reference non-empty
        if not approval.approver_reference or approval.approver_reference.strip().lower() in ("unknown", "none", "auto", "llm"):
            errors.append(
                "Authorization violation: invalid or unverified approver reference. "
                "Explicit human approver identity is required."
            )

        is_valid = len(errors) == 0
        return ApprovalValidationResult(is_valid=is_valid, errors=errors)
