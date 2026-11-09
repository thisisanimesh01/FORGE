"""Phase 13 M1: Explicit Lifecycle State Machine and Transition Rules.

Enforces deterministic, fail-closed state transitions for autonomous software
engineering workflows. Rejects illegal skips, unauthorized branches, and
terminal restarts.
"""

from typing import Dict, Set

from forge.orchestration.schemas import WorkflowState


class WorkflowStateError(Exception):
    """Base exception for workflow state machine errors."""


class InvalidTransitionError(WorkflowStateError):
    """Raised when an illegal or unauthorized state transition is attempted."""


class IllegalStateTransitionError(InvalidTransitionError):
    """Raised when an illegal transition is attempted."""


class TerminalStateError(InvalidTransitionError):
    """Raised when an illegal transition from a terminal state is attempted."""


# Strict state transition matrix
ALLOWED_TRANSITIONS: Dict[WorkflowState, Set[WorkflowState]] = {
    WorkflowState.CREATED: {
        WorkflowState.DIAGNOSING,
        WorkflowState.CANCELLED,
    },
    WorkflowState.DIAGNOSING: {
        WorkflowState.DIAGNOSIS_COMPLETED,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.DIAGNOSIS_COMPLETED: {
        WorkflowState.ROOT_CAUSE_ANALYSIS,
        WorkflowState.PLANNING,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.ROOT_CAUSE_ANALYSIS: {
        WorkflowState.ROOT_CAUSE_COMPLETED,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.ROOT_CAUSE_COMPLETED: {
        WorkflowState.PLANNING,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.PLANNING: {
        WorkflowState.PLAN_READY,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.PLAN_READY: {
        WorkflowState.AWAITING_HUMAN_APPROVAL,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.AWAITING_HUMAN_APPROVAL: {
        WorkflowState.APPROVED,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.APPROVED: {
        WorkflowState.REPAIR_EXECUTING,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.REPAIR_EXECUTING: {
        WorkflowState.REPAIR_COMPLETED,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.REPAIR_COMPLETED: {
        WorkflowState.VERIFICATION_RUNNING,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.VERIFICATION_RUNNING: {
        WorkflowState.VERIFICATION_COMPLETED,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.VERIFICATION_COMPLETED: {
        WorkflowState.QUALITY_EVALUATION,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.QUALITY_EVALUATION: {
        WorkflowState.COMPLETED,
        WorkflowState.BLOCKED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    },
    # Terminal states have no progression
    WorkflowState.COMPLETED: set(),
    WorkflowState.BLOCKED: {
        WorkflowState.CANCELLED,
    },
    WorkflowState.FAILED: {
        WorkflowState.CANCELLED,
    },
    WorkflowState.CANCELLED: set(),
}

TERMINAL_STATES: Set[WorkflowState] = {
    WorkflowState.COMPLETED,
    WorkflowState.CANCELLED,
}


def is_terminal_state(state: WorkflowState) -> bool:
    """Return True if the workflow state is terminal (cannot progress further)."""
    return state in TERMINAL_STATES or state in (WorkflowState.BLOCKED, WorkflowState.FAILED)


def validate_transition(current_state: WorkflowState, target_state: WorkflowState) -> bool:
    """Validate that moving from current_state to target_state is explicitly allowed.

    Raises:
        TerminalStateError: If attempting to transition from a terminal state.
        IllegalStateTransitionError: If the transition is prohibited or illegal.
    """
    if current_state in TERMINAL_STATES:
        raise TerminalStateError(
            f"Cannot transition from terminal state '{current_state.value}' to '{target_state.value}'."
        )

    if current_state == target_state:
        raise IllegalStateTransitionError(
            f"Self-transition rejected: workflow is already in state '{current_state.value}'."
        )

    allowed = ALLOWED_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise IllegalStateTransitionError(
            f"Illegal workflow transition: cannot move from '{current_state.value}' to '{target_state.value}'. "
            f"Permitted targets: {sorted(s.value for s in allowed) if allowed else 'none (terminal state)'}."
        )
    return True
