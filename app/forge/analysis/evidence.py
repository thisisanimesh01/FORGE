"""Evidence model utilities, normalization, and validation rules."""

from pathlib import Path
from typing import Any, Dict, Optional
import uuid

from forge.config import redact_secrets
from forge.schemas import EvidenceRecord, EvidenceType


class EvidenceValidationError(Exception):
    """Raised when an evidence record fails validation or containment checks."""
    pass


MAX_CONTENT_CHARS = 50_000
MAX_DESCRIPTION_CHARS = 1_000


def normalize_relative_path(path_str: Optional[str]) -> Optional[str]:
    """Validate and normalize a file path relative to repository root.

    Rejects path traversal attempts (e.g. '../', absolute paths pointing outside).
    """
    if not path_str:
        return None

    cleaned = path_str.strip().replace("\\", "/")
    if cleaned.startswith("/") or cleaned.startswith("../") or "/../" in cleaned or cleaned == "..":
        raise EvidenceValidationError(f"Invalid repository-relative path or path traversal: {path_str}")

    try:
        p = Path(cleaned)
        # Check components
        for part in p.parts:
            if part in ["..", "."]:
                raise EvidenceValidationError(f"Path contains forbidden traversal component: {path_str}")
        return p.as_posix()
    except Exception as e:
        raise EvidenceValidationError(f"Malformed file path '{path_str}': {e}")


def create_evidence(
    run_id: str,
    evidence_type: EvidenceType | str,
    description: str,
    content: str,
    finding_id: Optional[str] = None,
    file_path: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    provenance: str = "forge:investigator",
    evidence_id: Optional[str] = None,
) -> EvidenceRecord:
    """Create a validated, bounded, and secret-redacted EvidenceRecord."""
    if not run_id or not run_id.strip():
        raise EvidenceValidationError("run_id cannot be empty")

    if not description or not description.strip():
        raise EvidenceValidationError("description cannot be empty")

    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]

    # Convert string to EvidenceType if necessary
    if isinstance(evidence_type, str):
        try:
            ev_type = EvidenceType(evidence_type)
        except ValueError:
            raise EvidenceValidationError(f"Invalid evidence_type: {evidence_type}")
    else:
        ev_type = evidence_type

    # Validate lines
    if start_line is not None and start_line < 1:
        raise EvidenceValidationError(f"start_line must be >= 1, got {start_line}")

    if end_line is not None and end_line < 1:
        raise EvidenceValidationError(f"end_line must be >= 1, got {end_line}")

    if start_line is not None and end_line is not None and end_line < start_line:
        raise EvidenceValidationError(f"end_line ({end_line}) cannot be less than start_line ({start_line})")

    # Validate path
    norm_path = normalize_relative_path(file_path)

    # Redact secrets from content and description
    sanitized_content = redact_secrets(content or "")
    if len(sanitized_content) > MAX_CONTENT_CHARS:
        sanitized_content = (
            sanitized_content[:MAX_CONTENT_CHARS]
            + f"\n\n[TRUNCATED: Evidence content exceeded limit of {MAX_CONTENT_CHARS} characters]"
        )

    sanitized_description = redact_secrets(description)

    # Sanitize and validate details payload size
    safe_details = details or {}
    import json
    try:
        details_str = json.dumps(safe_details)
        if len(details_str) > MAX_CONTENT_CHARS:
            raise EvidenceValidationError(f"Details dictionary exceeds maximum allowed size ({MAX_CONTENT_CHARS} chars)")
    except (TypeError, ValueError) as e:
        raise EvidenceValidationError(f"Details must be JSON-serializable: {e}")

    # Sanitize provenance
    if not provenance or not provenance.strip():
        raise EvidenceValidationError("provenance cannot be empty")
    safe_provenance = redact_secrets(provenance.strip())[:255]

    eid = evidence_id or f"evi_{uuid.uuid4().hex[:12]}"

    return EvidenceRecord(
        evidence_id=eid,
        run_id=run_id,
        finding_id=finding_id,
        evidence_type=ev_type,
        description=sanitized_description,
        file_path=norm_path,
        start_line=start_line,
        end_line=end_line,
        content=sanitized_content,
        details=safe_details,
        provenance=safe_provenance,
    )
