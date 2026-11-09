"""Phase 13 M1: Cryptographic Audit Event Chaining for Orchestration.

Maintains an append-only, tamper-evident audit event log for the end-to-end
engineering lifecycle with SHA-256 hash chaining and secret redaction.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from forge.config import redact_secrets
from forge.orchestration.schemas import WorkflowStage, WorkflowState

GENESIS_HASH = "0" * 64
MAX_EVENTS_PER_WORKFLOW = 250


class AuditTrailTamperedError(Exception):
    """Exception raised when cryptographic event chain integrity verification fails."""


class OrchestrationEventType(str, Enum):
    """Types of audit events emitted during workflow orchestration."""

    WORKFLOW_CREATED = "WORKFLOW_CREATED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_FAILED = "STAGE_FAILED"
    STAGE_BLOCKED = "STAGE_BLOCKED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_SUBMITTED = "APPROVAL_SUBMITTED"
    APPROVAL_VERIFIED = "APPROVAL_VERIFIED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    WORKFLOW_BLOCKED = "WORKFLOW_BLOCKED"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"


class OrchestrationEvent(BaseModel):
    """Immutable, cryptographically chained event in the workflow audit trail."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    event_id: str
    workflow_id: str
    sequence: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: OrchestrationEventType
    stage: Optional[WorkflowStage] = None
    state: WorkflowState
    action_description: str
    details: Dict[str, Any] = Field(default_factory=dict)
    prev_event_hash: str
    event_hash: str


class OrchestrationEventRecorder:
    """Thread-safe recorder managing the append-only cryptographic event chain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[str, List[OrchestrationEvent]] = {}

    def record_event(
        self,
        workflow_id: str,
        event_type: OrchestrationEventType,
        state: WorkflowState,
        action_description: str,
        stage: Optional[WorkflowStage] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> OrchestrationEvent:
        """Record an event, redact secrets, calculate SHA-256 hash, and append to chain."""
        with self._lock:
            chain = self._events.setdefault(workflow_id, [])

            if len(chain) >= MAX_EVENTS_PER_WORKFLOW:
                raise RuntimeError(
                    f"Workflow '{workflow_id}' exceeded maximum event limit of {MAX_EVENTS_PER_WORKFLOW}."
                )

            sequence = len(chain)
            prev_hash = chain[-1].event_hash if chain else GENESIS_HASH

            # Sanitize and redact sensitive details
            raw_details = details or {}
            sanitized_details = self._sanitize_dict(raw_details)
            clean_action = redact_secrets(action_description)

            event_id = f"evt_wf_{workflow_id[-6:]}_{sequence:04d}"
            now = datetime.now(timezone.utc)

            event_hash = self.compute_event_hash(
                event_id=event_id,
                workflow_id=workflow_id,
                sequence=sequence,
                timestamp=now,
                event_type=event_type,
                stage=stage,
                state=state,
                action_description=clean_action,
                details=sanitized_details,
                prev_event_hash=prev_hash,
            )

            event = OrchestrationEvent(
                event_id=event_id,
                workflow_id=workflow_id,
                sequence=sequence,
                timestamp=now,
                event_type=event_type,
                stage=stage,
                state=state,
                action_description=clean_action,
                details=sanitized_details,
                prev_event_hash=prev_hash,
                event_hash=event_hash,
            )

            chain.append(event)
            return event

    def get_events(self, workflow_id: str) -> List[OrchestrationEvent]:
        """Return a copy of the event chain for a given workflow."""
        with self._lock:
            return list(self._events.get(workflow_id, []))

    @staticmethod
    def compute_event_hash(
        event_id: str,
        workflow_id: str,
        sequence: int,
        timestamp: datetime,
        event_type: OrchestrationEventType,
        stage: Optional[WorkflowStage],
        state: WorkflowState,
        action_description: str,
        details: Dict[str, Any],
        prev_event_hash: str,
    ) -> str:
        """Compute SHA-256 digest of canonical event attributes."""
        canonical = {
            "event_id": event_id,
            "workflow_id": workflow_id,
            "sequence": sequence,
            "timestamp": timestamp.isoformat(),
            "event_type": event_type.value,
            "stage": stage.value if stage else None,
            "state": state.value,
            "action_description": action_description,
            "details": details,
            "prev_event_hash": prev_event_hash,
        }
        serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def verify_event_chain(events: List[OrchestrationEvent]) -> Tuple[bool, Optional[str]]:
        """Verify the cryptographic hash-chain integrity of an event list.

        Returns:
            (True, None) if valid, or (False, error_reason) if tampered or invalid.
        """
        if not events:
            return True, None

        expected_prev_hash = GENESIS_HASH
        for idx, event in enumerate(events):
            if event.sequence != idx:
                return False, f"Sequence discontinuity: event at index {idx} has sequence {event.sequence}."

            if event.prev_event_hash != expected_prev_hash:
                return False, (
                    f"Hash chain broken at event {event.event_id} (seq {event.sequence}): "
                    f"prev_event_hash '{event.prev_event_hash}' != expected '{expected_prev_hash}'."
                )

            computed_hash = OrchestrationEventRecorder.compute_event_hash(
                event_id=event.event_id,
                workflow_id=event.workflow_id,
                sequence=event.sequence,
                timestamp=event.timestamp,
                event_type=event.event_type,
                stage=event.stage,
                state=event.state,
                action_description=event.action_description,
                details=event.details,
                prev_event_hash=event.prev_event_hash,
            )

            if event.event_hash != computed_hash:
                return False, (
                    f"Digest mismatch at event {event.event_id} (seq {event.sequence}): "
                    f"event_hash '{event.event_hash}' != computed '{computed_hash}'."
                )

            expected_prev_hash = event.event_hash

        return True, None

    def _sanitize_dict(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively redact secrets in dictionaries."""
        sanitized: Dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, str):
                sanitized[k] = redact_secrets(v)
            elif isinstance(v, dict):
                sanitized[k] = self._sanitize_dict(v)
            elif isinstance(v, list):
                sanitized[k] = [
                    redact_secrets(item) if isinstance(item, str) else item
                    for item in v
                ]
            else:
                sanitized[k] = v
        return sanitized
