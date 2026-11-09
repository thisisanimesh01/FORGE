"""Phase 12 M5: Audit Event Recorder for Repair Verification.

Maintains an immutable, append-only cryptographic event trail:
- Sequential integer sequence numbers.
- SHA-256 cryptographic chaining (prev_event_hash -> event_hash).
- Chain rewrite and tampering detection via expected_head_hash validation.
- Automatic secret redaction for all recorded payloads and descriptions.
- Event count capping at MAX_EVENTS_PER_VERIFICATION (200).
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional

from forge.config import redact_secrets
from forge.repair_verification.schemas import (
    MAX_EVENTS_PER_VERIFICATION,
    VerificationEvent,
    VerificationEventType,
)


class VerificationEventRecorder:
    """Manages append-only cryptographic audit logging of verification events."""

    def __init__(self) -> None:
        self._events: List[VerificationEvent] = []

    def record_event(
        self,
        verification_id: str,
        event_type: VerificationEventType,
        action_description: str,
        outcome: str = "success",
        details: Optional[Dict[str, Any]] = None,
    ) -> VerificationEvent:
        """Append an audited event to the cryptographic event chain."""
        if len(self._events) >= MAX_EVENTS_PER_VERIFICATION:
            raise RuntimeError(
                f"Event capacity exceeded: cannot record more than {MAX_EVENTS_PER_VERIFICATION} events per verification"
            )

        seq_num = len(self._events) + 1
        prev_hash = self._events[-1].event_hash if self._events else None

        clean_desc = redact_secrets(action_description)
        payload_dict = {
            "seq": seq_num,
            "verification_id": verification_id,
            "event_type": event_type.value,
            "action": clean_desc,
            "outcome": outcome,
        }
        serialized = json.dumps(payload_dict, sort_keys=True)
        event_hash_input = f"{prev_hash or ''}:{serialized}"
        cur_hash = hashlib.sha256(event_hash_input.encode("utf-8")).hexdigest()

        event = VerificationEvent(
            event_id=f"vev_{verification_id[:8]}_{seq_num:03d}",
            verification_id=verification_id,
            sequence_number=seq_num,
            prev_event_hash=prev_hash,
            event_hash=cur_hash,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            actor="repair_verification_engine",
            action_description=clean_desc,
            outcome=outcome,
        )
        self._events.append(event)
        return event

    def get_events(self) -> List[VerificationEvent]:
        """Return shallow copy of recorded events."""
        return list(self._events)

    def verify_event_chain(self, expected_head_hash: Optional[str] = None) -> bool:
        """Verify unbroken cryptographic integrity of event log.

        Raises:
            ValueError: If chain rewriting, sequence break, or hash tampering is detected.
        """
        if not self._events:
            return True

        prev_hash: Optional[str] = None
        for i, ev in enumerate(self._events):
            if ev.sequence_number != i + 1:
                raise ValueError(
                    f"Integrity violation: non-consecutive sequence number at index {i}: {ev.sequence_number}"
                )
            if ev.prev_event_hash != prev_hash:
                raise ValueError(
                    f"Integrity violation: broken cryptographic link at event {ev.event_id}"
                )

            # Recompute expected hash from payload fields
            payload_dict = {
                "seq": ev.sequence_number,
                "verification_id": ev.verification_id,
                "event_type": ev.event_type.value,
                "action": ev.action_description,
                "outcome": ev.outcome,
            }
            serialized = json.dumps(payload_dict, sort_keys=True)
            expected_input = f"{prev_hash or ''}:{serialized}"
            expected_hash = hashlib.sha256(expected_input.encode("utf-8")).hexdigest()
            if ev.event_hash != expected_hash:
                raise ValueError(
                    f"Integrity violation: event payload tampering detected at event {ev.event_id}"
                )

            prev_hash = ev.event_hash

        if expected_head_hash is not None:
            actual_head = self._events[-1].event_hash
            if actual_head != expected_head_hash:
                raise ValueError(
                    f"Integrity violation: event chain head hash mismatch: expected '{expected_head_hash}', got '{actual_head}'"
                )

        return True
