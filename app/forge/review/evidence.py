"""Evidence models, validation, and calibration for code review."""

from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.analysis.evidence import normalize_relative_path, MAX_CONTENT_CHARS, MAX_DESCRIPTION_CHARS
from forge.config import redact_secrets
from forge.schemas import EvidenceType


class EvidenceCalibrationStatus(str, Enum):
    """Validation and calibration status of an evidence item."""
    VERIFIED = "verified"
    INSUFFICIENT = "insufficient"
    MISMATCHED = "mismatched"
    CONTRADICTORY = "contradictory"
    REJECTED = "rejected"


class CalibratedEvidence(BaseModel):
    """Structured evidence item calibrated to diff hunks and repository artifacts."""
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_type: EvidenceType = EvidenceType.STATIC_ANALYSIS
    status: EvidenceCalibrationStatus = EvidenceCalibrationStatus.VERIFIED
    provenance: str = "forge:review:static"
    description: str
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    content_excerpt: str
    content_hash: str
    details: Dict[str, Any] = Field(default_factory=dict)
    contradiction_reason: Optional[str] = None


class EvidenceValidationError(Exception):
    """Raised when evidence validation fails."""
    pass


def compute_content_hash(content: str) -> str:
    """Compute deterministic SHA-256 hash of evidence content."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


def create_calibrated_evidence(
    evidence_type: EvidenceType | str,
    description: str,
    content: str,
    provenance: str = "forge:review:static",
    file_path: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    evidence_id: Optional[str] = None,
    status: EvidenceCalibrationStatus = EvidenceCalibrationStatus.VERIFIED,
    contradiction_reason: Optional[str] = None,
) -> CalibratedEvidence:
    """Create a validated, secret-redacted, and hashed CalibratedEvidence item."""
    if not description or not description.strip():
        raise EvidenceValidationError("Evidence description cannot be empty.")

    if isinstance(evidence_type, str):
        try:
            ev_type = EvidenceType(evidence_type)
        except ValueError:
            raise EvidenceValidationError(f"Invalid evidence_type: {evidence_type}")
    else:
        ev_type = evidence_type

    # Line bounds validation
    if start_line is not None and start_line < 1:
        raise EvidenceValidationError(f"start_line must be >= 1, got {start_line}")
    if end_line is not None and end_line < 1:
        raise EvidenceValidationError(f"end_line must be >= 1, got {end_line}")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise EvidenceValidationError(f"end_line ({end_line}) cannot be less than start_line ({start_line})")

    norm_path = normalize_relative_path(file_path) if file_path else None

    # Secret redaction
    sanitized_content = redact_secrets(content or "")
    if len(sanitized_content) > MAX_CONTENT_CHARS:
        sanitized_content = (
            sanitized_content[:MAX_CONTENT_CHARS]
            + f"\n\n[TRUNCATED: Evidence content exceeded limit of {MAX_CONTENT_CHARS} characters]"
        )

    sanitized_desc = redact_secrets(description)[:MAX_DESCRIPTION_CHARS]
    sanitized_prov = redact_secrets(provenance or "forge:review")[:255]
    content_hash = compute_content_hash(sanitized_content)

    eid = evidence_id or f"evi_{content_hash}_{start_line or 0}"

    return CalibratedEvidence(
        evidence_id=eid,
        evidence_type=ev_type,
        status=status,
        provenance=sanitized_prov,
        description=sanitized_desc,
        file_path=norm_path,
        start_line=start_line,
        end_line=end_line,
        content_excerpt=sanitized_content,
        content_hash=content_hash,
        details=details or {},
        contradiction_reason=redact_secrets(contradiction_reason) if contradiction_reason else None,
    )
