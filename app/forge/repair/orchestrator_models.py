"""Data models, transition rules, and state machine contracts for Phase 4 M4 repair orchestration.

Guarantees:
- Deterministic typed lifecycle transitions:
  CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED.
- Verified patches are NEVER automatically approved (must pass through AWAITING_HUMAN_REVIEW).
- Pinned immutable contexts: target repository, base commit SHA, finding ID, plan ID, patch digest, and policy version.
- Transactional optimistic concurrency control via monotonic versioning.
- Outbound secret scrubbing on all event payloads and diagnostic notes.
- Terminal states (APPROVED, REJECTED) are immutable and cannot transition further.
"""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field

from forge.repair.contracts import RepairOutcome, RepairSafetyError
from forge.schemas import EventType


class InvalidWorkflowTransitionError(RepairSafetyError):
    """Raised when an illegal workflow state transition is attempted."""


class WorkflowConcurrencyError(RepairSafetyError):
    """Raised when concurrent competing updates or state version conflicts occur."""


class RepairWorkflowState(str, Enum):
    """Deterministic typed state machine for controlled repair orchestration."""
    CREATED = "created"
    EVIDENCE_VALIDATED = "evidence_validated"
    PLAN_GENERATED = "plan_generated"
    PATCH_VALIDATED = "patch_validated"
    VERIFICATION_PENDING = "verification_pending"
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    APPROVED = "approved"
    REJECTED = "rejected"


ALLOWED_WORKFLOW_TRANSITIONS: Dict[RepairWorkflowState, Set[RepairWorkflowState]] = {
    RepairWorkflowState.CREATED: {
        RepairWorkflowState.EVIDENCE_VALIDATED,
        RepairWorkflowState.BLOCKED,
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.EVIDENCE_VALIDATED: {
        RepairWorkflowState.PLAN_GENERATED,
        RepairWorkflowState.BLOCKED,
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.PLAN_GENERATED: {
        RepairWorkflowState.PATCH_VALIDATED,
        RepairWorkflowState.BLOCKED,
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.PATCH_VALIDATED: {
        RepairWorkflowState.VERIFICATION_PENDING,
        RepairWorkflowState.AWAITING_HUMAN_REVIEW,  # Dry-run / plan-only mode
        RepairWorkflowState.BLOCKED,
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.VERIFICATION_PENDING: {
        RepairWorkflowState.VERIFIED,
        RepairWorkflowState.FAILED,
        RepairWorkflowState.BLOCKED,
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.VERIFIED: {
        RepairWorkflowState.AWAITING_HUMAN_REVIEW,
    },
    RepairWorkflowState.FAILED: {
        RepairWorkflowState.VERIFICATION_PENDING,  # Retry verification if eligible
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.BLOCKED: {
        RepairWorkflowState.EVIDENCE_VALIDATED,     # Retry / unblocked from planning
        RepairWorkflowState.VERIFICATION_PENDING,  # Retry / unblocked from verification
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.AWAITING_HUMAN_REVIEW: {
        RepairWorkflowState.APPROVED,
        RepairWorkflowState.REJECTED,
    },
    RepairWorkflowState.APPROVED: set(),  # Terminal state
    RepairWorkflowState.REJECTED: set(),  # Terminal state
}


def validate_workflow_transition(
    from_state: RepairWorkflowState,
    to_state: RepairWorkflowState,
) -> None:
    """Validate that transition from from_state to to_state is explicitly permitted.
    
    Raises:
        InvalidWorkflowTransitionError: If the transition is illegal.
    """
    allowed = ALLOWED_WORKFLOW_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        allowed_names = sorted([s.value for s in allowed]) or ["None (terminal)"]
        raise InvalidWorkflowTransitionError(
            f"Illegal workflow transition: cannot transition from '{from_state.value}' to '{to_state.value}'. "
            f"Permitted target states: {allowed_names}"
        )


class RepairWorkflowEvent(BaseModel):
    """Immutable record of an append-only state transition event."""
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(description="Unique event identifier")
    workflow_id: str = Field(description="Associated workflow identifier")
    sequence: int = Field(ge=1, description="Strictly monotonic event sequence number")
    from_state: Optional[RepairWorkflowState] = Field(default=None, description="Source workflow state")
    to_state: RepairWorkflowState = Field(description="Destination workflow state")
    stage: str = Field(description="Orchestration stage name (e.g. 'validate_evidence', 'review')")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Sanitized stage payload")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RepairWorkflowRecord(BaseModel):
    """Persistent, resumable state record for an end-to-end repair orchestration workflow."""
    model_config = ConfigDict(frozen=True)

    workflow_id: str = Field(description="Unique deterministic workflow run ID")
    finding_id: str = Field(description="Finding being addressed")
    run_id: Optional[str] = Field(default=None, description="Associated investigation run ID")
    target_repo: str = Field(description="Target repository directory path")
    base_commit_sha: str = Field(description="Immutable base commit SHA pinned to the finding")
    policy_version: str = Field(default="v1", description="Policy version pinned to this workflow")
    state: RepairWorkflowState = Field(default=RepairWorkflowState.CREATED, description="Current workflow state")
    plan_id: Optional[str] = Field(default=None, description="Pinned repair plan ID")
    patch_digest: Optional[str] = Field(default=None, description="SHA256 digest of candidate patch")
    dry_run: bool = Field(default=False, description="Whether workflow is in dry-run/plan-only mode")
    sandbox_available: bool = Field(default=True, description="Whether container sandbox was available")
    outcome: Optional[RepairOutcome] = Field(default=None, description="Classified verification outcome")
    verification_attempt_id: Optional[str] = Field(default=None, description="ID of verification attempt")
    human_decision: Optional[str] = Field(default=None, description="Explicit human review decision ('approved' or 'rejected')")
    human_notes: Optional[str] = Field(default=None, description="Human reviewer justification notes")
    error_message: Optional[str] = Field(default=None, description="Sanitized error or diagnostic message")
    version: int = Field(default=1, description="Monotonically increasing version for optimistic concurrency")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        """Whether the workflow is in a terminal state (approved or rejected)."""
        return self.state in (RepairWorkflowState.APPROVED, RepairWorkflowState.REJECTED)

    @property
    def is_actionable_for_human(self) -> bool:
        """Whether the workflow is currently paused awaiting human review."""
        return self.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW

    @property
    def is_resumable(self) -> bool:
        """Whether the workflow can be resumed (e.g. from blocked, failed, or interrupted state)."""
        return self.state in (
            RepairWorkflowState.BLOCKED,
            RepairWorkflowState.FAILED,
            RepairWorkflowState.CREATED,
            RepairWorkflowState.EVIDENCE_VALIDATED,
            RepairWorkflowState.PLAN_GENERATED,
            RepairWorkflowState.PATCH_VALIDATED,
            RepairWorkflowState.VERIFICATION_PENDING,
        )
