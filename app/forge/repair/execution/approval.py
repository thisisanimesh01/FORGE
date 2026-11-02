"""Phase 8 Repair Execution Architecture — Operator Review & Approval Workflow.

Implements the guarded approval state machine, artifact-bound verification guarantees,
invalidation on artifact tampering, and immutable SQLite decision records.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from forge.config import redact_secrets
from forge.repair.execution.dossier import ReviewDossierBuilder
from forge.repair.execution.failures import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalInvalidatedError,
    ApprovalPreconditionError,
    ArtifactBindingMismatchError,
    DuplicateDecisionError,
    InvalidApprovalTransitionError,
    UnauthorizedOperatorError,
)
from forge.repair.execution.models import (
    ApprovalBinding,
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    FailureCategory,
    FailureClassification,
    IndependentValidationResult,
    OperatorDecisionType,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionState,
    RepairRequest,
    ReviewDossier,
    ValidationOutcome,
    VerificationOutcome,
    VerificationRunRecord,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.schemas import EvidenceRecord, Finding
from forge.storage.store import ForgeStore


# =============================================================================
# Approval State Machine
# =============================================================================

ALLOWED_APPROVAL_TRANSITIONS: Dict[ApprovalState, Set[ApprovalState]] = {
    ApprovalState.PENDING_REVIEW: {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
        ApprovalState.CHANGES_REQUESTED,
        ApprovalState.WITHDRAWN,
        ApprovalState.EXPIRED,
        ApprovalState.INVALIDATED,
    },
    ApprovalState.APPROVED: {
        ApprovalState.WITHDRAWN,
        ApprovalState.EXPIRED,
        ApprovalState.INVALIDATED,
    },
    ApprovalState.CHANGES_REQUESTED: {
        ApprovalState.PENDING_REVIEW,
        ApprovalState.REJECTED,
        ApprovalState.WITHDRAWN,
    },
    # Terminal approval states:
    ApprovalState.REJECTED: set(),
    ApprovalState.WITHDRAWN: set(),
    ApprovalState.EXPIRED: set(),
    ApprovalState.INVALIDATED: set(),
}


class ApprovalStateMachine:
    """Validates legal approval lifecycle transitions."""

    @staticmethod
    def validate_transition(
        from_state: ApprovalState,
        to_state: ApprovalState,
    ) -> None:
        """Validate if a state transition is legal according to the approval matrix."""
        if from_state == to_state:
            raise DuplicateDecisionError(
                f"Approval decision is already in state '{from_state.value}'."
            )

        allowed = ALLOWED_APPROVAL_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise InvalidApprovalTransitionError(
                f"Illegal approval transition from '{from_state.value}' to '{to_state.value}'."
            )


# =============================================================================
# Approval Manager
# =============================================================================

class ApprovalManager:
    """Manages review dossier generation, operator approvals, and artifact binding verification."""

    def __init__(
        self,
        execution_store: RepairExecutionStore,
        forge_store: Optional[ForgeStore] = None,
        dossier_builder: Optional[ReviewDossierBuilder] = None,
    ):
        self.exec_store = execution_store
        self.forge_store = forge_store
        self.dossier_builder = dossier_builder or ReviewDossierBuilder(
            execution_store=execution_store,
            forge_store=forge_store,
        )

    def get_or_create_dossier(
        self,
        repair_id: str,
        request: Optional[RepairRequest] = None,
        patch: Optional[PatchArtifact] = None,
        validation_result: Optional[IndependentValidationResult] = None,
        verification_run: Optional[VerificationRunRecord] = None,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        dossier_version: str = "1.0.0",
    ) -> ReviewDossier:
        """Fetch existing latest ReviewDossier or build a new one."""
        latest = self.exec_store.get_latest_review_dossier_for_repair(repair_id)
        if latest:
            # If current patch hash matches existing dossier, reuse it
            req = request or self.exec_store.get_repair_request(repair_id)
            if req and req.patch_artifact_id:
                p_art = patch or self.exec_store.get_patch_artifact(req.patch_artifact_id)
                if p_art and p_art.content_hash == latest.patch_sha256:
                    return latest

        return self.dossier_builder.build_dossier(
            repair_id=repair_id,
            request=request,
            patch=patch,
            validation_result=validation_result,
            verification_run=verification_run,
            finding=finding,
            evidence_records=evidence_records,
            dossier_version=dossier_version,
        )

    def submit_decision(
        self,
        repair_id: str,
        decision: OperatorDecisionType,
        actor: str,
        reason: str,
        authorized_action: Optional[AuthorizedAction] = None,
        feedback: Optional[str] = None,
        ttl_hours: Optional[float] = 24.0,
        policy_version: str = "1.0.0",
    ) -> ApprovalDecisionRecord:
        """Submit and persist an explicit operator review decision with artifact binding."""
        # 1. Validate operator identity
        if not actor or not actor.strip():
            raise UnauthorizedOperatorError("Operator identity cannot be empty or whitespace.")
        actor_clean = actor.strip()

        # 2. Validate reason
        if not reason or not reason.strip():
            raise ApprovalError("A mandatory justification reason must be provided.")
        reason_clean = reason.strip()

        # 3. Resolve Repair Request & Related Artifacts
        req = self.exec_store.get_repair_request(repair_id)
        if not req:
            raise ApprovalPreconditionError(f"Repair request '{repair_id}' does not exist.")

        if not req.patch_artifact_id:
            raise ApprovalPreconditionError(f"No candidate patch artifact associated with repair '{repair_id}'.")

        patch = self.exec_store.get_patch_artifact(req.patch_artifact_id)
        if not patch:
            raise ApprovalPreconditionError(f"Patch artifact '{req.patch_artifact_id}' does not exist in store.")

        # Resolve validation and verification records
        val_res = self.exec_store.get_validation_result_for_patch(patch.artifact_id)
        if not val_res:
            val_list = self.exec_store.list_validation_results_for_repair(repair_id)
            if val_list:
                val_res = val_list[0]

        verif_run = self.exec_store.get_latest_verification_run_for_repair(repair_id)

        # 4. Check Prior Decisions to Prevent Duplicate/Illegal Replay
        existing = self.exec_store.get_latest_approval_decision_for_repair(repair_id)
        current_state = existing.state if existing else ApprovalState.PENDING_REVIEW

        # Determine target state
        if decision == OperatorDecisionType.APPROVE:
            target_state = ApprovalState.APPROVED
        elif decision == OperatorDecisionType.REJECT:
            target_state = ApprovalState.REJECTED
        elif decision == OperatorDecisionType.REQUEST_CHANGES:
            target_state = ApprovalState.CHANGES_REQUESTED
        elif decision == OperatorDecisionType.WITHDRAW:
            target_state = ApprovalState.WITHDRAWN
        else:
            raise ApprovalError(f"Unrecognized decision type: '{decision}'")

        # Validate transition
        ApprovalStateMachine.validate_transition(current_state, target_state)

        # 5. Strict Preconditions for APPROVE
        if decision == OperatorDecisionType.APPROVE:
            if not authorized_action:
                raise ApprovalError(
                    "Approval requires specifying an explicit AuthorizedAction "
                    "(EXPORT_PATCH, STAGE_FOR_PR, or MANUAL_INTEGRATION)."
                )

            # Precondition: M4 Independent Validation must be ACCEPTED
            if not val_res:
                raise ApprovalPreconditionError(
                    "Cannot approve repair: M4 independent validation result is missing."
                )
            if val_res.overall_status != ValidationOutcome.ACCEPTED:
                raise ApprovalPreconditionError(
                    f"Cannot approve repair: M4 independent validation status is '{val_res.overall_status.value}', "
                    "not 'ACCEPTED'."
                )

            # Precondition: M5 Sandbox Verification must be VERIFIED
            if not verif_run:
                raise ApprovalPreconditionError(
                    "Cannot approve repair: M5 sandbox verification run is missing."
                )
            if verif_run.overall_outcome != VerificationOutcome.VERIFIED:
                raise ApprovalPreconditionError(
                    f"Cannot approve repair: M5 sandbox verification outcome is '{verif_run.overall_outcome.value}', "
                    "not 'VERIFIED'."
                )

            # Precondition: Digest matches candidate patch artifact exactly
            if val_res.patch_sha256 != patch.content_hash:
                raise ArtifactBindingMismatchError(
                    f"Patch content hash mismatch: M4 validated '{val_res.patch_sha256}', "
                    f"patch has '{patch.content_hash}'."
                )
            if verif_run.patch_sha256 != patch.content_hash:
                raise ArtifactBindingMismatchError(
                    f"Patch content hash mismatch: M5 verified '{verif_run.patch_sha256}', "
                    f"patch has '{patch.content_hash}'."
                )

            # Precondition: Pinned base commit revision matches
            if req.base_commit_sha != patch.base_commit_sha:
                raise ArtifactBindingMismatchError(
                    f"Base commit mismatch: request has '{req.base_commit_sha}', "
                    f"patch has '{patch.base_commit_sha}'."
                )
            if verif_run.base_commit_sha != req.base_commit_sha:
                raise ArtifactBindingMismatchError(
                    f"Base commit mismatch: verification has '{verif_run.base_commit_sha}', "
                    f"request has '{req.base_commit_sha}'."
                )

            # Precondition: Zero regressions detected
            if verif_run.regression_analysis.regression_detected:
                raise ApprovalPreconditionError(
                    "Cannot approve repair: M5 verification detected regressions introduced by candidate patch."
                )

            # Precondition: Defect demonstrably resolved
            if not verif_run.regression_analysis.defect_resolved:
                raise ApprovalPreconditionError(
                    "Cannot approve repair: defect reproduction check did not resolve."
                )

        # 6. Ensure Review Dossier Exists
        dossier = self.get_or_create_dossier(
            repair_id=repair_id,
            request=req,
            patch=patch,
            validation_result=val_res,
            verification_run=verif_run,
        )

        # 7. Formulate Artifact Binding
        binding = ApprovalBinding(
            repair_id=repair_id,
            finding_id=req.finding_id,
            repository_target=str(req.target_repo),
            base_commit_sha=req.base_commit_sha,
            patch_artifact_id=patch.artifact_id,
            patch_sha256=patch.content_hash,
            validation_id=val_res.validation_id if val_res else "NONE",
            verification_id=verif_run.verification_id if verif_run else "NONE",
            dossier_id=dossier.dossier_id,
            dossier_version=dossier.dossier_version,
            policy_version=policy_version,
        )

        now = datetime.now(timezone.utc)
        expires_at = None
        if decision == OperatorDecisionType.APPROVE and ttl_hours is not None:
            expires_at = now + timedelta(hours=ttl_hours)

        record = ApprovalDecisionRecord(
            decision_id=f"dec_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            binding=binding,
            state=target_state,
            decision=decision,
            authorized_action=authorized_action if decision == OperatorDecisionType.APPROVE else None,
            actor=actor_clean,
            reason=reason_clean,
            feedback=feedback.strip() if feedback else None,
            expires_at=expires_at,
            created_at=now,
        )

        # 8. Persist Decision Record
        self.exec_store.save_approval_decision(record)

        # 9. Record Sequential Audit Event
        self.exec_store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{uuid.uuid4().hex[:12]}",
                repair_id=repair_id,
                sequence=1,
                from_state=req.state,
                to_state=req.state,
                actor=RepairActor.OPERATOR,
                payload={
                    "action": "operator_decision_submitted",
                    "decision_id": record.decision_id,
                    "decision": decision.value,
                    "approval_state": target_state.value,
                    "authorized_action": authorized_action.value if authorized_action else None,
                    "actor": actor_clean,
                    "reason": reason_clean,
                    "patch_sha256": patch.content_hash,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
                timestamp=now,
            )
        )

        # 10. Update RepairRequest state if REJECTED
        if target_state == ApprovalState.REJECTED:
            if req.state != RepairExecutionState.REJECTED and req.state not in (
                RepairExecutionState.FAILED,
                RepairExecutionState.CANCELLED,
            ):
                try:
                    self.exec_store.transition_repair_state(
                        repair_id=repair_id,
                        to_state=RepairExecutionState.REJECTED,
                        actor=RepairActor.OPERATOR,
                        context={
                            "rejection_reason": reason_clean,
                            "decision_id": record.decision_id,
                        },
                    )
                except Exception:
                    pass

        return record

    def validate_approval_binding(
        self,
        repair_id: str,
        decision_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Verify that current repair artifacts still match the exact binding tuple.

        If any bound artifact has been modified, tampered with, or expired,
        the approval record is immediately invalidated in persistent storage.
        """
        # Resolve target decision
        if decision_id:
            dec = self.exec_store.get_approval_decision(decision_id)
        else:
            dec = self.exec_store.get_latest_approval_decision_for_repair(repair_id)

        if not dec:
            return False, f"No approval decision found for repair '{repair_id}'."

        if dec.state != ApprovalState.APPROVED:
            return False, f"Decision is not in APPROVED state (current state: '{dec.state.value}')."

        # Check expiration
        now = datetime.now(timezone.utc)
        if dec.expires_at and now >= dec.expires_at:
            invalidation_reason = f"Approval decision '{dec.decision_id}' expired at {dec.expires_at.isoformat()}."
            self._invalidate_decision(dec, ApprovalState.EXPIRED, invalidation_reason)
            return False, invalidation_reason

        # Resolve current repair state
        req = self.exec_store.get_repair_request(repair_id)
        if not req:
            invalidation_reason = f"Repair request '{repair_id}' no longer exists."
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        patch = self.exec_store.get_patch_artifact(req.patch_artifact_id) if req.patch_artifact_id else None
        if not patch:
            invalidation_reason = "Bound patch artifact is missing from store."
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        # Invariant 1: Patch artifact ID match
        if patch.artifact_id != dec.binding.patch_artifact_id:
            invalidation_reason = (
                f"Patch artifact ID changed: bound to '{dec.binding.patch_artifact_id}', "
                f"current is '{patch.artifact_id}'."
            )
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        # Invariant 2: Exact patch SHA-256 match
        if patch.content_hash != dec.binding.patch_sha256:
            invalidation_reason = (
                f"Patch SHA-256 hash tampered or modified: bound to '{dec.binding.patch_sha256}', "
                f"current is '{patch.content_hash}'."
            )
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        # Invariant 3: Base commit SHA match
        if req.base_commit_sha != dec.binding.base_commit_sha:
            invalidation_reason = (
                f"Repository base commit SHA changed: bound to '{dec.binding.base_commit_sha}', "
                f"current is '{req.base_commit_sha}'."
            )
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        # Invariant 4: Target repository match
        if str(req.target_repo) != dec.binding.repository_target:
            invalidation_reason = (
                f"Target repository changed: bound to '{dec.binding.repository_target}', "
                f"current is '{req.target_repo}'."
            )
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        # Invariant 5: Associated finding match
        if req.finding_id != dec.binding.finding_id:
            invalidation_reason = (
                f"Finding ID changed: bound to '{dec.binding.finding_id}', "
                f"current is '{req.finding_id}'."
            )
            self._invalidate_decision(dec, ApprovalState.INVALIDATED, invalidation_reason)
            return False, invalidation_reason

        # All invariants hold cleanly
        return True, None

    def _invalidate_decision(
        self,
        decision: ApprovalDecisionRecord,
        new_state: ApprovalState,
        reason: str,
    ) -> None:
        """Mark an approval decision invalidated or expired in persistent storage."""
        now = datetime.now(timezone.utc)
        updated = decision.model_copy(
            update={
                "state": new_state,
                "invalidation_reason": reason,
                "invalidation_timestamp": now,
            }
        )
        self.exec_store.save_approval_decision(updated)

        self.exec_store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{uuid.uuid4().hex[:12]}",
                repair_id=decision.repair_id,
                sequence=1,
                from_state=RepairExecutionState.VERIFIED,
                to_state=RepairExecutionState.VERIFIED,
                actor=RepairActor.OPERATOR,
                payload={
                    "action": "approval_invalidated",
                    "decision_id": decision.decision_id,
                    "previous_state": decision.state.value,
                    "new_state": new_state.value,
                    "invalidation_reason": reason,
                },
                timestamp=now,
            )
        )
