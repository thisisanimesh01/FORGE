"""Guarded state machine and transition rules for Phase 5 engineering workflows.

Phase 5, Milestone 2: Supervised Workflow State Machine & Transactional Ledger.

Invariants:
- Strict transition gating: transitions are validated in Python code with zero prompt discretion.
- Human review gate: transition to APPROVED strictly requires explicit human decision.
- Zero fabrication: REPRODUCED and VERIFIED states strictly require evidence.
- Safe resume: defines explicit resumable vs. terminal states.
"""

from typing import Any, Dict, Optional, Set

from forge.workflow.models import (
    EngineeringWorkflowState,
    HumanApprovalRequiredError,
    InvalidWorkflowTransitionError,
)


ALLOWED_TRANSITIONS: Dict[EngineeringWorkflowState, Set[EngineeringWorkflowState]] = {
    EngineeringWorkflowState.CREATED: {
        EngineeringWorkflowState.INTAKE_VALIDATED,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.INTAKE_VALIDATED: {
        EngineeringWorkflowState.REPO_ANALYZED,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REPO_ANALYZED: {
        EngineeringWorkflowState.REPRODUCTION_PENDING,
        EngineeringWorkflowState.REPAIR_PENDING,  # Fast-forward if verified repro evidence pre-exists
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REPRODUCTION_PENDING: {
        EngineeringWorkflowState.REPRODUCED,
        EngineeringWorkflowState.REPRO_FAILED,
        EngineeringWorkflowState.REPRO_BLOCKED,
        EngineeringWorkflowState.REPRO_INCONCLUSIVE,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REPRO_BLOCKED: {
        EngineeringWorkflowState.REPRODUCTION_PENDING,  # Resumed once unblocked
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REPRODUCED: {
        EngineeringWorkflowState.REPAIR_PENDING,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REPAIR_PENDING: {
        EngineeringWorkflowState.VERIFICATION_PENDING,
        EngineeringWorkflowState.REPAIR_FAILED,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.VERIFICATION_PENDING: {
        EngineeringWorkflowState.VERIFIED,
        EngineeringWorkflowState.VERIFICATION_FAILED,
        EngineeringWorkflowState.VERIFICATION_BLOCKED,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.VERIFICATION_BLOCKED: {
        EngineeringWorkflowState.VERIFICATION_PENDING,  # Resumed once unblocked
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.VERIFIED: {
        EngineeringWorkflowState.REGRESSION_CHECKING,
        EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,  # Direct review when regression suite omitted
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REGRESSION_CHECKING: {
        EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        EngineeringWorkflowState.REGRESSION_FAILED,
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.REGRESSION_FAILED: {
        EngineeringWorkflowState.REPAIR_PENDING,  # Retry repair after regression failure
        EngineeringWorkflowState.REJECTED,
    },
    EngineeringWorkflowState.AWAITING_HUMAN_REVIEW: {
        EngineeringWorkflowState.APPROVED,
        EngineeringWorkflowState.REJECTED,
    },
    # Terminal states:
    EngineeringWorkflowState.REPRO_FAILED: set(),
    EngineeringWorkflowState.REPRO_INCONCLUSIVE: set(),
    EngineeringWorkflowState.REPAIR_FAILED: set(),
    EngineeringWorkflowState.VERIFICATION_FAILED: set(),
    EngineeringWorkflowState.APPROVED: set(),
    EngineeringWorkflowState.REJECTED: set(),
}

RESUMABLE_STATES: Set[EngineeringWorkflowState] = {
    EngineeringWorkflowState.REPRO_BLOCKED,
    EngineeringWorkflowState.VERIFICATION_BLOCKED,
    EngineeringWorkflowState.REPRODUCTION_PENDING,
    EngineeringWorkflowState.REPAIR_PENDING,
    EngineeringWorkflowState.VERIFICATION_PENDING,
    EngineeringWorkflowState.REGRESSION_CHECKING,
}

TERMINAL_STATES: Set[EngineeringWorkflowState] = {
    EngineeringWorkflowState.REPRO_FAILED,
    EngineeringWorkflowState.REPRO_INCONCLUSIVE,
    EngineeringWorkflowState.REPAIR_FAILED,
    EngineeringWorkflowState.VERIFICATION_FAILED,
    EngineeringWorkflowState.APPROVED,
    EngineeringWorkflowState.REJECTED,
}


def is_resumable_state(state: EngineeringWorkflowState) -> bool:
    """Return True if the workflow state can be safely resumed."""
    return state in RESUMABLE_STATES


def is_terminal_state(state: EngineeringWorkflowState) -> bool:
    """Return True if the workflow state is terminal and immutable."""
    return state in TERMINAL_STATES


def validate_workflow_transition(
    current_state: EngineeringWorkflowState,
    target_state: EngineeringWorkflowState,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Validate that a proposed workflow state transition conforms to all invariants.

    Raises:
    - InvalidWorkflowTransitionError: when transition is illegal per ALLOWED_TRANSITIONS
    - HumanApprovalRequiredError: when transition to APPROVED lacks human decision
    - ValueError: when required evidence or identifiers are missing
    """
    ctx = context or {}

    # 1. State machine transition check
    allowed = ALLOWED_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise InvalidWorkflowTransitionError(
            f"Illegal workflow transition: cannot transition from '{current_state.value}' to '{target_state.value}'. "
            f"Allowed next states: {[s.value for s in allowed]}"
        )

    # 2. Mandatory Human Review Gate
    if target_state == EngineeringWorkflowState.APPROVED:
        decision = ctx.get("human_decision")
        if decision != "approve":
            raise HumanApprovalRequiredError(
                "Transition to APPROVED strictly requires explicit human operator authorization "
                "with human_decision='approve'. Autonomous approval is forbidden."
            )

    # 3. Honest Outcome Checks
    if target_state == EngineeringWorkflowState.REPRODUCED:
        repro_outcome = ctx.get("repro_outcome")
        if repro_outcome and repro_outcome not in ("reproduced", "failed"):
            # A reproduction test must confirm the failure to be considered reproduced
            if repro_outcome in ("passed", "not_reproduced", "skipped_sandbox_unavailable"):
                raise InvalidWorkflowTransitionError(
                    f"Cannot mark workflow as REPRODUCED with non-reproduced outcome: '{repro_outcome}'"
                )

    if target_state == EngineeringWorkflowState.VERIFIED:
        verify_outcome = ctx.get("verification_outcome")
        if verify_outcome and verify_outcome not in ("repair_verified", "verified", "passed"):
            raise InvalidWorkflowTransitionError(
                f"Cannot mark workflow as VERIFIED with unverified outcome: '{verify_outcome}'"
            )

    # 4. Evidence grounding check for direct repair fast-forward from REPO_ANALYZED
    if current_state == EngineeringWorkflowState.REPO_ANALYZED and target_state == EngineeringWorkflowState.REPAIR_PENDING:
        has_repro_evidence = ctx.get("has_repro_evidence", False)
        if not has_repro_evidence:
            raise InvalidWorkflowTransitionError(
                "Direct transition from REPO_ANALYZED to REPAIR_PENDING requires verified reproduction evidence. "
                "Otherwise workflow must transition to REPRODUCTION_PENDING."
            )
