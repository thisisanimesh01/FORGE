"""Finding lifecycle state machine and transition enforcement."""

from typing import List, Optional
from forge.schemas import (
    Finding,
    FindingLifecycleState,
    EvidenceRecord,
    EvidenceType,
)


class InvalidTransitionError(Exception):
    """Raised when an illegal finding lifecycle transition is requested or required evidence is missing."""
    pass


# Canonical lifecycle transitions
# suspected -> reproduced -> root_caused -> fixed -> verified
ALLOWED_TRANSITIONS = {
    FindingLifecycleState.SUSPECTED: [FindingLifecycleState.REPRODUCED],
    FindingLifecycleState.REPRODUCED: [FindingLifecycleState.ROOT_CAUSED],
    FindingLifecycleState.ROOT_CAUSED: [FindingLifecycleState.FIXED],
    FindingLifecycleState.FIXED: [FindingLifecycleState.VERIFIED],
    FindingLifecycleState.VERIFIED: [],
}

# Required evidence types per target state
REQUIRED_EVIDENCE_TYPE = {
    FindingLifecycleState.REPRODUCED: EvidenceType.REPRODUCTION_RESULT,
    FindingLifecycleState.ROOT_CAUSED: EvidenceType.ROOT_CAUSE_ANALYSIS,
    FindingLifecycleState.FIXED: EvidenceType.PATCH_REFERENCE,
    FindingLifecycleState.VERIFIED: EvidenceType.VERIFICATION_RESULT,
}


def validate_transition(
    finding: Finding,
    target_state: FindingLifecycleState | str,
    evidence: EvidenceRecord,
) -> None:
    """Validate that finding can legally transition to target_state backed by evidence.

    Raises:
        InvalidTransitionError: If the transition is illegal or evidence is mismatched/insufficient.
    """
    if isinstance(target_state, str):
        try:
            to_state = FindingLifecycleState(target_state)
        except ValueError:
            raise InvalidTransitionError(f"Unknown finding lifecycle state: '{target_state}'")
    else:
        to_state = target_state

    current_state = finding.lifecycle_state

    # Idempotent attempt check
    if current_state == to_state:
        raise InvalidTransitionError(f"Finding '{finding.finding_id}' is already in state '{current_state.value}'")

    # Check legal sequence
    allowed_next = ALLOWED_TRANSITIONS.get(current_state, [])
    if to_state not in allowed_next:
        raise InvalidTransitionError(
            f"Illegal transition: Cannot transition finding '{finding.finding_id}' from "
            f"'{current_state.value}' to '{to_state.value}'. Permitted transitions: {[s.value for s in allowed_next]}"
        )

    # Validate evidence association: finding_id must match if specified
    if evidence.finding_id and evidence.finding_id != finding.finding_id:
        raise InvalidTransitionError(
            f"Evidence '{evidence.evidence_id}' belongs to finding '{evidence.finding_id}', "
            f"not target finding '{finding.finding_id}'"
        )

    # Validate run association: evidence run_id must match finding run_id
    if finding.run_id and evidence.run_id and finding.run_id != evidence.run_id:
        raise InvalidTransitionError(
            f"Evidence '{evidence.evidence_id}' belongs to run '{evidence.run_id}', "
            f"which does not match finding '{finding.finding_id}' run '{finding.run_id}'"
        )


    # Validate required evidence type
    expected_type = REQUIRED_EVIDENCE_TYPE.get(to_state)
    if expected_type and evidence.evidence_type != expected_type:
        raise InvalidTransitionError(
            f"Transition to '{to_state.value}' requires evidence of type '{expected_type.value}', "
            f"got '{evidence.evidence_type.value}'"
        )

    # Evidence cannot be truncated when authorizing reproduction or verification
    if "[TRUNCATED:" in evidence.content:
        if to_state in [FindingLifecycleState.REPRODUCED, FindingLifecycleState.VERIFIED]:
            raise InvalidTransitionError(
                f"Cannot authorize transition to '{to_state.value}' using truncated evidence"
            )

    # Specific evidence outcome validation
    if to_state == FindingLifecycleState.REPRODUCED:
        # Check details or content indicate successful reproduction
        status = evidence.details.get("status") or evidence.details.get("outcome")
        if status and str(status).lower() in ["failed", "not_reproduced", "skipped", "error"]:
            raise InvalidTransitionError(
                f"Cannot mark finding as reproduced with failed reproduction status: '{status}'"
            )

    elif to_state == FindingLifecycleState.VERIFIED:
        # Verification must prove fix succeeded (e.g. tests passed)
        status = evidence.details.get("status") or evidence.details.get("outcome")
        if status and str(status).lower() in ["failed", "failing", "error", "rejected"]:
            raise InvalidTransitionError(
                f"Cannot mark finding as verified with failed verification status: '{status}'"
            )
        # Content check: cannot contain failure markers
        if "FAILED" in evidence.content and "PASSED" not in evidence.content:
            raise InvalidTransitionError("Cannot mark finding as verified when evidence contains failures")
        # In audit-only Phase 1, sandbox is unavailable. Verification must come from trusted forge provenance,
        # not arbitrary caller strings or untrusted claim.
        trusted_provenance_prefixes = ["forge:sandbox", "forge:verifier", "forge:system"]
        if not any(evidence.provenance.startswith(p) for p in trusted_provenance_prefixes):
            # If the evidence provenance is untrusted, fail closed.
            raise InvalidTransitionError(
                f"Untrusted verification evidence provenance '{evidence.provenance}'. "
                "Phase 1 requires verified provenance from trusted execution."
            )

