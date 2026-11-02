"""Phase 8 Repair Execution Architecture — Guarded State Machine.

Defines the legal lifecycle transitions, actor authorization matrices,
and transition invariants for software repair execution.
"""

from typing import Any, Dict, Optional, Set, Tuple

from forge.repair.execution.models import (
    FailureCategory,
    FailureClassification,
    PatchArtifact,
    RepairActor,
    RepairExecutionPlan,
    RepairExecutionState,
    RepairRequest,
    ValidationResult,
)


# =============================================================================
# State Transition Exceptions
# =============================================================================

class RepairExecutionStateError(Exception):
    """Base exception for repair execution state machine violations."""


class InvalidRepairTransitionError(RepairExecutionStateError):
    """Raised when an illegal lifecycle transition is attempted."""


class UnauthorizedTransitionActorError(RepairExecutionStateError):
    """Raised when a component or actor attempts an unauthorized state transition."""


class IllegalStateModificationError(RepairExecutionStateError):
    """Raised when attempting to modify an immutable terminal state."""


# =============================================================================
# Transition Matrices and Authorization Tables
# =============================================================================

ALLOWED_REPAIR_TRANSITIONS: Dict[RepairExecutionState, Set[RepairExecutionState]] = {
    RepairExecutionState.CREATED: {
        RepairExecutionState.PLANNED,
        RepairExecutionState.REJECTED,
        RepairExecutionState.CANCELLED,
    },
    RepairExecutionState.PLANNED: {
        RepairExecutionState.VALIDATING,
        RepairExecutionState.FAILED,
        RepairExecutionState.REJECTED,
        RepairExecutionState.CANCELLED,
    },
    RepairExecutionState.VALIDATING: {
        RepairExecutionState.READY,
        RepairExecutionState.FAILED,
        RepairExecutionState.REJECTED,
        RepairExecutionState.CANCELLED,
    },
    RepairExecutionState.READY: {
        RepairExecutionState.EXECUTING,
        RepairExecutionState.FAILED,
        RepairExecutionState.CANCELLED,
    },
    RepairExecutionState.EXECUTING: {
        RepairExecutionState.PATCH_GENERATED,
        RepairExecutionState.FAILED,
        RepairExecutionState.CANCELLED,
    },
    RepairExecutionState.PATCH_GENERATED: {
        RepairExecutionState.PATCH_VALIDATING,
        RepairExecutionState.FAILED,
        RepairExecutionState.REJECTED,
        RepairExecutionState.CANCELLED,
    },
    RepairExecutionState.PATCH_VALIDATING: {
        RepairExecutionState.VERIFIED,
        RepairExecutionState.FAILED,
        RepairExecutionState.REJECTED,
        RepairExecutionState.CANCELLED,
    },
    # Terminal states have empty outgoing transition sets:
    RepairExecutionState.VERIFIED: set(),
    RepairExecutionState.FAILED: set(),
    RepairExecutionState.REJECTED: set(),
    RepairExecutionState.CANCELLED: set(),
}

TERMINAL_STATES: Set[RepairExecutionState] = {
    RepairExecutionState.VERIFIED,
    RepairExecutionState.FAILED,
    RepairExecutionState.REJECTED,
    RepairExecutionState.CANCELLED,
}

RESUMABLE_STATES: Set[RepairExecutionState] = {
    RepairExecutionState.CREATED,
    RepairExecutionState.PLANNED,
    RepairExecutionState.VALIDATING,
    RepairExecutionState.READY,
    RepairExecutionState.EXECUTING,
    RepairExecutionState.PATCH_GENERATED,
    RepairExecutionState.PATCH_VALIDATING,
}

# Explicit mapping: (from_state, to_state) -> Set[Authorized Actor]
ACTOR_TRANSITION_PERMISSIONS: Dict[Tuple[RepairExecutionState, RepairExecutionState], Set[RepairActor]] = {
    # CREATED -> ...
    (RepairExecutionState.CREATED, RepairExecutionState.PLANNED): {
        RepairActor.PLANNER,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.CREATED, RepairExecutionState.REJECTED): {
        RepairActor.VALIDATOR,
        RepairActor.OPERATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.CREATED, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },

    # PLANNED -> ...
    (RepairExecutionState.PLANNED, RepairExecutionState.VALIDATING): {
        RepairActor.VALIDATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PLANNED, RepairExecutionState.FAILED): {
        RepairActor.PLANNER,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PLANNED, RepairExecutionState.REJECTED): {
        RepairActor.VALIDATOR,
        RepairActor.OPERATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PLANNED, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },

    # VALIDATING -> ...
    (RepairExecutionState.VALIDATING, RepairExecutionState.READY): {
        RepairActor.VALIDATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.VALIDATING, RepairExecutionState.FAILED): {
        RepairActor.VALIDATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.VALIDATING, RepairExecutionState.REJECTED): {
        RepairActor.VALIDATOR,
        RepairActor.OPERATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.VALIDATING, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },

    # READY -> ...
    (RepairExecutionState.READY, RepairExecutionState.EXECUTING): {
        RepairActor.EXECUTOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.READY, RepairExecutionState.FAILED): {
        RepairActor.EXECUTOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.READY, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },

    # EXECUTING -> ...
    (RepairExecutionState.EXECUTING, RepairExecutionState.PATCH_GENERATED): {
        RepairActor.EXECUTOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.EXECUTING, RepairExecutionState.FAILED): {
        RepairActor.EXECUTOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.EXECUTING, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },

    # PATCH_GENERATED -> ...
    (RepairExecutionState.PATCH_GENERATED, RepairExecutionState.PATCH_VALIDATING): {
        RepairActor.VALIDATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PATCH_GENERATED, RepairExecutionState.FAILED): {
        RepairActor.EXECUTOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PATCH_GENERATED, RepairExecutionState.REJECTED): {
        RepairActor.VALIDATOR,
        RepairActor.OPERATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PATCH_GENERATED, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },

    # PATCH_VALIDATING -> ...
    (RepairExecutionState.PATCH_VALIDATING, RepairExecutionState.VERIFIED): {
        RepairActor.VERIFIER,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PATCH_VALIDATING, RepairExecutionState.FAILED): {
        RepairActor.VERIFIER,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PATCH_VALIDATING, RepairExecutionState.REJECTED): {
        RepairActor.VALIDATOR,
        RepairActor.OPERATOR,
        RepairActor.ORCHESTRATOR,
    },
    (RepairExecutionState.PATCH_VALIDATING, RepairExecutionState.CANCELLED): {
        RepairActor.OPERATOR,
    },
}


# =============================================================================
# Transition Verification Functions
# =============================================================================

def validate_repair_transition(
    from_state: RepairExecutionState,
    to_state: RepairExecutionState,
    actor: RepairActor,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Validate that a requested lifecycle transition is legal and authorized.

    Args:
        from_state: Current state of the repair execution.
        to_state: Target state to transition into.
        actor: Component or human initiating the transition.
        context: Optional contextual parameters (e.g. plan, patch, validation, failure).

    Raises:
        InvalidRepairTransitionError: If the transition path is not allowed.
        UnauthorizedTransitionActorError: If the actor lacks permission for this transition.
        IllegalStateModificationError: If attempting to transition out of a terminal state.
    """
    ctx = context or {}

    # Check terminal state immutability
    if from_state in TERMINAL_STATES:
        raise IllegalStateModificationError(
            f"Cannot transition out of terminal state '{from_state.value}'. "
            f"Terminal states are immutable."
        )

    # Check same-state transition
    if from_state == to_state:
        raise InvalidRepairTransitionError(
            f"Idempotent transition rejected: repair execution is already in state '{from_state.value}'"
        )

    # Check legal transition path
    allowed_targets = ALLOWED_REPAIR_TRANSITIONS.get(from_state, set())
    if to_state not in allowed_targets:
        valid_options = sorted([s.value for s in allowed_targets]) or ["none (terminal)"]
        raise InvalidRepairTransitionError(
            f"Illegal state transition from '{from_state.value}' to '{to_state.value}'. "
            f"Allowed transitions from '{from_state.value}': {valid_options}"
        )

    # Check actor authorization
    authorized_actors = ACTOR_TRANSITION_PERMISSIONS.get((from_state, to_state), set())
    if actor not in authorized_actors:
        valid_actors = sorted([a.value for a in authorized_actors])
        raise UnauthorizedTransitionActorError(
            f"Actor '{actor.value}' is not authorized to transition from "
            f"'{from_state.value}' to '{to_state.value}'. "
            f"Authorized actors: {valid_actors}"
        )

    # Contextual guards
    if to_state == RepairExecutionState.PLANNED:
        plan = ctx.get("plan")
        if plan is None:
            raise InvalidRepairTransitionError(
                "Transition to PLANNED requires a valid RepairExecutionPlan in context."
            )

    elif to_state == RepairExecutionState.READY:
        val = ctx.get("validation")
        if val is None or not getattr(val, "is_valid", False):
            raise InvalidRepairTransitionError(
                "Transition to READY requires a passed ValidationResult in context."
            )

    elif to_state == RepairExecutionState.PATCH_GENERATED:
        patch = ctx.get("patch")
        if patch is None or not isinstance(patch, PatchArtifact):
            raise InvalidRepairTransitionError(
                "Transition to PATCH_GENERATED requires a valid PatchArtifact in context."
            )

    elif to_state == RepairExecutionState.VERIFIED:
        # Zero fabrication invariant: verification must be observed
        verification_passed = ctx.get("verification_passed", False)
        if not verification_passed:
            raise InvalidRepairTransitionError(
                "Transition to VERIFIED strictly requires verified proof that defect reproduction "
                "is resolved and regression checks passed (verification_passed=True)."
            )

    elif to_state == RepairExecutionState.FAILED:
        # A failure must have an explanation or classification
        failure = ctx.get("failure")
        error_msg = ctx.get("error_message")
        if not failure and not error_msg:
            raise InvalidRepairTransitionError(
                "Transition to FAILED requires failure classification or error message in context."
            )


class RepairExecutionStateMachine:
    """State machine controller executing guarded transitions on RepairRequest."""

    def __init__(self, request: RepairRequest):
        self._request = request

    @property
    def current_state(self) -> RepairExecutionState:
        return self._request.state

    @property
    def is_terminal(self) -> bool:
        return self._request.state in TERMINAL_STATES

    @property
    def is_resumable(self) -> bool:
        return self._request.state in RESUMABLE_STATES

    def transition(
        self,
        to_state: RepairExecutionState,
        actor: RepairActor,
        context: Optional[Dict[str, Any]] = None,
    ) -> RepairExecutionState:
        """Execute a guarded transition on the underlying request.

        Updates request state and timestamp if valid, or raises an exception.
        """
        from datetime import datetime, timezone

        from_state = self._request.state
        validate_repair_transition(from_state, to_state, actor, context)

        self._request.state = to_state
        self._request.updated_at = datetime.now(timezone.utc)

        # Context sync
        ctx = context or {}
        if to_state == RepairExecutionState.FAILED:
            failure = ctx.get("failure")
            if failure and isinstance(failure, FailureClassification):
                self._request.failure = failure
            elif ctx.get("error_message"):
                self._request.failure = FailureClassification(
                    category=ctx.get("failure_category", FailureCategory.INTERNAL_ERROR),
                    message=str(ctx["error_message"]),
                    details=ctx.get("details", {}),
                )
        elif to_state == RepairExecutionState.PATCH_GENERATED:
            patch = ctx.get("patch")
            if patch and isinstance(patch, PatchArtifact):
                self._request.patch_artifact_id = patch.artifact_id
        elif to_state == RepairExecutionState.VERIFIED:
            self._request.verification_status = "passed"

        return self._request.state
