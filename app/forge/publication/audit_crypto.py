"""Cryptographic hash chaining and canonical serialization for audit ledgers."""

from enum import Enum
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple


class AuditIntegrityStatus(str, Enum):
    """Integrity verification status for sequential audit events and event chains."""
    VERIFIED = "verified"
    LEGACY_UNSEALED = "legacy_unsealed"
    TAMPERED = "tampered"
    BROKEN_CHAIN = "broken_chain"
    MISSING_EVENTS = "missing_events"
    DUPLICATE_SEQUENCE = "duplicate_sequence"
    INCONCLUSIVE = "inconclusive"


def canonical_event_bytes(prev_hash: Optional[str], event_dict: Dict[str, Any]) -> bytes:
    """Deterministically serializes event parameters into canonical JSON bytes for hashing.
    
    Keys are strictly sorted, separators are normalized with no superfluous whitespace,
    and string encoding is UTF-8.
    """
    payload = event_dict.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {"raw": payload}

    canonical = {
        "actor": str(event_dict.get("actor") or ""),
        "event_id": str(event_dict.get("event_id") or ""),
        "from_state": str(event_dict.get("from_state") or ""),
        "payload": payload,
        "prev_event_hash": str(prev_hash or "GENESIS"),
        "repair_id": str(event_dict.get("repair_id") or ""),
        "sequence": int(event_dict.get("sequence") or 0),
        "timestamp": str(event_dict.get("timestamp") or ""),
        "to_state": str(event_dict.get("to_state") or ""),
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def compute_event_hash(prev_event_hash: Optional[str], event_dict: Dict[str, Any]) -> str:
    """Computes SHA-256 hash chaining the previous event hash to the canonical event data."""
    data = canonical_event_bytes(prev_event_hash, event_dict)
    return hashlib.sha256(data).hexdigest()


def verify_event_hash(
    prev_event_hash: Optional[str],
    event_dict: Dict[str, Any],
    expected_hash: Optional[str],
) -> bool:
    """Checks whether the event's data matches its recorded cryptographic hash."""
    if not expected_hash:
        return False
    computed = compute_event_hash(prev_event_hash, event_dict)
    return computed == expected_hash
