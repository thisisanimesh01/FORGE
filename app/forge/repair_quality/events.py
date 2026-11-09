"""Phase 12 M6: Quality Audit Event Recorder.

Maintains an immutable append-only SHA-256 cryptographic hash chain
for audit events emitted throughout repair quality evaluation.
Automatically sanitizes secrets from event action descriptions.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional

from forge.config import redact_secrets
from forge.repair_quality.schemas import (
    MAX_EVENTS_PER_ANALYSIS,
    RepairQualityEvent,
    RepairQualityEventType,
)


class RepairQualityEventRecorder:
    """Manages append-only cryptographic event audit chains for quality evaluations."""

    def __init__(self, analysis_id: str):
        self.analysis_id = analysis_id
        self._events: List[RepairQualityEvent] = []
        self._last_event_hash: Optional[str] = None

    def record_event(
        self,
        event_type: RepairQualityEventType,
        action_description: str,
        actor: str = "quality_engine",
        outcome: str = "success",
    ) -> RepairQualityEvent:
        """Record a new sanitized audit event linked into the cryptographic chain."""
        if len(self._events) >= MAX_EVENTS_PER_ANALYSIS:
            return self._events[-1]

        seq = len(self._events) + 1
        ts = datetime.now(timezone.utc).isoformat()
        sanitized_action = redact_secrets(action_description)
        event_id = f"qev_{self.analysis_id[:8]}_{seq:03d}"

        # Compute SHA-256 event hash chaining previous hash
        prev_hash = self._last_event_hash
        hash_payload = f"{event_id}|{self.analysis_id}|{seq}|{prev_hash or 'GENESIS'}|{event_type.value}|{ts}|{sanitized_action}|{outcome}"
        current_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
        self._last_event_hash = current_hash

        event = RepairQualityEvent(
            event_id=event_id,
            analysis_id=self.analysis_id,
            sequence_number=seq,
            prev_event_hash=prev_hash,
            event_hash=current_hash,
            event_type=event_type,
            timestamp=ts,
            actor=actor,
            action_description=sanitized_action,
            outcome=outcome,
        )
        self._events.append(event)
        return event

    def get_events(self) -> List[RepairQualityEvent]:
        """Return full event audit history."""
        return list(self._events)

    @staticmethod
    def validate_event_chain(events: List[RepairQualityEvent]) -> bool:
        """Verify cryptographic integrity of an event hash chain."""
        if not events:
            return True

        last_hash: Optional[str] = None
        for idx, event in enumerate(events):
            expected_seq = idx + 1
            if event.sequence_number != expected_seq:
                return False
            if event.prev_event_hash != last_hash:
                return False

            payload = (
                f"{event.event_id}|{event.analysis_id}|{event.sequence_number}|"
                f"{event.prev_event_hash or 'GENESIS'}|{event.event_type.value}|"
                f"{event.timestamp}|{event.action_description}|{event.outcome}"
            )
            computed_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if computed_hash != event.event_hash:
                return False

            last_hash = computed_hash

        return True
