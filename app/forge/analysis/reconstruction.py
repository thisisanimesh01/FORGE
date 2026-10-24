"""Deterministic finding lifecycle reconstruction and event verification."""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

from forge.schemas import Finding, FindingLifecycleState, EventType

if TYPE_CHECKING:
    from forge.storage.store import ForgeStore



class ReconstructionError(Exception):
    """Raised when an event log contains contradictory or unevidenced transitions."""
    pass


ALLOWED_TRANSITIONS = {
    FindingLifecycleState.SUSPECTED: [FindingLifecycleState.REPRODUCED],
    FindingLifecycleState.REPRODUCED: [FindingLifecycleState.ROOT_CAUSED],
    FindingLifecycleState.ROOT_CAUSED: [FindingLifecycleState.FIXED],
    FindingLifecycleState.FIXED: [FindingLifecycleState.VERIFIED],
}


def reconstruct_finding_lifecycle(finding_id: str, store: ForgeStore) -> FindingLifecycleState:
    """Replay finding lifecycle events in order and verify against persisted state.

    Ensures:
    1. Finding was created in 'suspected'.
    2. Every transition event follows canonical state ordering.
    3. Every transition event links to an existing valid evidence record.
    4. Reconstructed end state strictly agrees with stored finding state.
    """
    finding = store.get_finding(finding_id)
    if not finding:
        raise ReconstructionError(f"Finding '{finding_id}' not found in store")

    # Fetch transitions from transition table
    conn = store._conn()
    try:
        cur = conn.execute(
            """
            SELECT * FROM finding_transitions
            WHERE finding_id = ?
            ORDER BY transition_id ASC
            """,
            (finding_id,),
        )
        transitions = cur.fetchall()
    finally:
        conn.close()

    current_state = FindingLifecycleState.SUSPECTED

    for trans in transitions:
        from_st = FindingLifecycleState(trans["from_state"])
        to_st = FindingLifecycleState(trans["to_state"])
        evidence_id = trans["evidence_id"]

        # Validate step continuity
        if from_st != current_state:
            raise ReconstructionError(
                f"Transition step mismatch for '{finding_id}': expected from '{current_state.value}', got '{from_st.value}'"
            )

        # Validate allowed progression
        allowed = ALLOWED_TRANSITIONS.get(from_st, [])
        if to_st not in allowed:
            raise ReconstructionError(
                f"Contradictory transition from '{from_st.value}' to '{to_st.value}' for '{finding_id}'"
            )

        # Verify evidence exists in store
        evidence = store.get_evidence(evidence_id)
        if not evidence:
            raise ReconstructionError(
                f"Unevidenced transition: Evidence '{evidence_id}' not found for finding '{finding_id}'"
            )

        current_state = to_st

    # Cross-check against append-only run_events table to ensure tamper-detection
    # Every finding transition must have a corresponding FINDING_STATE_TRANSITIONED event
    run_id = finding.run_id
    if run_id:
        events = store.list_run_events(run_id)
        transition_events = [
            e for e in events
            if e.event_type == EventType.FINDING_STATE_TRANSITIONED
            and e.payload.get("finding_id") == finding_id
        ]
        if len(transition_events) != len(transitions):
            raise ReconstructionError(
                f"Event log mismatch for finding '{finding_id}': found {len(transitions)} transition records "
                f"but {len(transition_events)} transition events in event stream."
            )
        for i, (trans, ev) in enumerate(zip(transitions, transition_events)):
            if trans["from_state"] != ev.payload.get("from_state") or trans["to_state"] != ev.payload.get("to_state"):
                raise ReconstructionError(
                    f"Contradictory event record at step {i} for finding '{finding_id}': "
                    f"transition row shows {trans['from_state']}->{trans['to_state']}, "
                    f"event stream shows {ev.payload.get('from_state')}->{ev.payload.get('to_state')}"
                )

    # Final check: reconstructed state must match current finding state
    if current_state != finding.lifecycle_state:
        raise ReconstructionError(
            f"State divergence: Reconstructed state '{current_state.value}' does not match "
            f"persisted state '{finding.lifecycle_state.value}' for finding '{finding_id}'"
        )

    return current_state

