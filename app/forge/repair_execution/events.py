"""Phase 12 M4: Repair Execution Audit Event Recording.

Provides:
- Immutable audit event generation for all repair lifecycle actions.
- Automatic secret redaction for action descriptions and diagnostic output.
- Deterministic event identifiers and sequential ordering.
- Strict event collection boundaries per execution.
"""

from datetime import datetime, timezone
import hashlib
from typing import Dict, List, Optional, Tuple, Union
import uuid

from forge.config import redact_secrets
from forge.repair_execution.schemas import (
    MAX_EVENTS_PER_EXECUTION,
    RepairEvent,
    RepairEventType,
)


def compute_event_hash(
    prev_hash: str,
    seq: int,
    event_type: Union[RepairEventType, str],
    description: str,
    integrity_digest: Optional[str],
    outcome: str,
) -> str:
    """Compute canonical SHA-256 hash for tamper-evident event chain."""
    type_str = event_type.value if hasattr(event_type, "value") else str(event_type)
    payload = f"{prev_hash}:{seq}:{type_str}:{description}:{integrity_digest or ''}:{outcome}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepairEventRecorder:
    """Manages recording and retrieval of audit events during repair execution."""

    def __init__(self) -> None:
        self._events_by_execution: Dict[str, List[RepairEvent]] = {}

    def record_event(
        self,
        execution_id: str,
        event_type: Union[RepairEventType, str],
        action_description: str,
        actor: str = "repair_executor",
        related_evidence: Optional[List[str]] = None,
        integrity_digest: Optional[str] = None,
        outcome: str = "success",
    ) -> RepairEvent:
        """Create and record an audit event for a repair execution.

        Args:
            execution_id: Target execution identifier.
            event_type: Category of repair lifecycle event.
            action_description: Description of the action taken (secret-redacted).
            actor: Component or actor performing the action.
            related_evidence: Optional evidence IDs linked to the event.
            integrity_digest: Optional SHA-256 tree or patch digest.
            outcome: Outcome status string ('success', 'failure', etc.).

        Returns:
            The recorded immutable RepairEvent.
        """
        if execution_id not in self._events_by_execution:
            self._events_by_execution[execution_id] = []

        existing_events = self._events_by_execution[execution_id]
        if len(existing_events) >= MAX_EVENTS_PER_EXECUTION:
            # Strictly enforce cap to prevent memory exhaustion
            return existing_events[-1]

        sanitized_description = redact_secrets(action_description)
        seq = len(existing_events) + 1
        event_id = f"rev_{execution_id}_{seq:04d}"

        prev_hash = existing_events[-1].event_hash if existing_events and existing_events[-1].event_hash else "0" * 64
        ev_hash = compute_event_hash(
            prev_hash=prev_hash,
            seq=seq,
            event_type=event_type,
            description=sanitized_description,
            integrity_digest=integrity_digest,
            outcome=outcome,
        )

        event = RepairEvent(
            event_id=event_id,
            execution_id=execution_id,
            sequence_number=seq,
            prev_event_hash=prev_hash,
            event_hash=ev_hash,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            action_description=sanitized_description,
            related_evidence=related_evidence or [],
            integrity_digest=integrity_digest,
            outcome=outcome,
        )

        existing_events.append(event)
        return event

    def get_events(self, execution_id: str) -> List[RepairEvent]:
        """Retrieve all recorded events for an execution."""
        return list(self._events_by_execution.get(execution_id, []))

    def verify_event_chain(self, execution_id: str) -> Tuple[bool, Optional[str]]:
        """Verify the cryptographic hash chain and monotonic ordering of recorded events.

        Returns:
            Tuple of (is_valid, optional_error_message).
        """
        events = self._events_by_execution.get(execution_id, [])
        if not events:
            return True, None

        expected_prev = "0" * 64
        for idx, ev in enumerate(events):
            expected_seq = idx + 1
            if ev.sequence_number != expected_seq:
                return False, f"Event {ev.event_id} has sequence {ev.sequence_number}, expected {expected_seq}."

            if ev.prev_event_hash != expected_prev:
                return False, f"Event {ev.event_id} prev_event_hash mismatch: expected {expected_prev}, got {ev.prev_event_hash}."

            recomputed_hash = compute_event_hash(
                prev_hash=expected_prev,
                seq=expected_seq,
                event_type=ev.event_type,
                description=ev.action_description,
                integrity_digest=ev.integrity_digest,
                outcome=ev.outcome,
            )
            if ev.event_hash != recomputed_hash:
                return False, f"Event {ev.event_id} event_hash tampered: expected {recomputed_hash}, got {ev.event_hash}."

            expected_prev = ev.event_hash

        return True, None

    def clear(self, execution_id: Optional[str] = None) -> None:
        """Clear recorded events (used for testing and teardown)."""
        if execution_id:
            self._events_by_execution.pop(execution_id, None)
        else:
            self._events_by_execution.clear()
