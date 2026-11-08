"""Phase 12 M4: Repair Execution Audit Event Recording.

Provides:
- Immutable audit event generation for all repair lifecycle actions.
- Automatic secret redaction for action descriptions and diagnostic output.
- Deterministic event identifiers and sequential ordering.
- Strict event collection boundaries per execution.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional
import uuid

from forge.config import redact_secrets
from forge.repair_execution.schemas import (
    MAX_EVENTS_PER_EXECUTION,
    RepairEvent,
    RepairEventType,
)


class RepairEventRecorder:
    """Manages recording and retrieval of audit events during repair execution."""

    def __init__(self) -> None:
        self._events_by_execution: Dict[str, List[RepairEvent]] = {}

    def record_event(
        self,
        execution_id: str,
        event_type: RepairEventType,
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
            # Enforce event cap
            action_description = "[CAP REACHED] " + action_description

        sanitized_description = redact_secrets(action_description)
        seq = len(existing_events) + 1
        event_id = f"rev_{execution_id}_{seq:04d}"

        event = RepairEvent(
            event_id=event_id,
            execution_id=execution_id,
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

    def clear(self, execution_id: Optional[str] = None) -> None:
        """Clear recorded events (used for testing and teardown)."""
        if execution_id:
            self._events_by_execution.pop(execution_id, None)
        else:
            self._events_by_execution.clear()
