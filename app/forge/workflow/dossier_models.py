"""Pydantic schemas and exceptions for Phase 5 Milestone 5 Certified Engineering Dossiers."""

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.workflow.models import WorkflowError


# =============================================================================
# Exceptions
# =============================================================================

class DossierError(WorkflowError):
    """Base exception for all dossier export and validation operations."""


class DossierWorkflowNotApprovedError(DossierError):
    """Raised when an export is attempted on a workflow that is not in APPROVED state."""


class DossierExportRefusedError(DossierError):
    """Raised when an export is refused due to missing or invalid prerequisite workflow data."""


class DossierSecurityError(DossierError):
    """Raised when symlinks, path traversal, or out-of-containment paths are detected."""


class DossierExportDirectoryError(DossierError):
    """Raised when the target export directory is invalid or contains unexpected existing files."""


class DossierIntegrityError(DossierError):
    """Raised when an artifact checksum does not match its manifest digest or patch SHA256."""


class DossierSignatureError(DossierError):
    """Raised when cryptographic signature generation or verification fails."""


class DossierValidationError(DossierError):
    """Raised when a dossier package fails structural or integrity validation."""


# =============================================================================
# Package Models
# =============================================================================

class DossierFileEntry(BaseModel):
    """Metadata entry for a single file within the certified engineering dossier."""
    model_config = ConfigDict(frozen=True)

    relative_path: str = Field(description="Normalized relative path of artifact within package")
    sha256: str = Field(description="Hexadecimal SHA-256 digest of artifact contents")
    byte_size: int = Field(ge=0, description="Size of artifact file in bytes")


class DossierSignature(BaseModel):
    """Cryptographic authentication signature record for the dossier manifest."""
    model_config = ConfigDict(frozen=True)

    algorithm: str = Field(default="HMAC-SHA256", description="Cryptographic signing algorithm")
    key_id: str = Field(description="Identifier or fingerprint of signing key")
    signature_hex: str = Field(description="Hexadecimal signature digest")
    signed_at: str = Field(description="ISO 8601 timestamp when signature was generated")


class DossierManifest(BaseModel):
    """Canonical integrity manifest for a certified engineering dossier."""
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default="1.0.0", description="Manifest schema version")
    package_type: str = Field(default="forge_engineering_dossier", description="Package type identifier")
    dossier_id: str = Field(description="Unique deterministic dossier identifier (dos_<hex12>)")
    workflow_id: str = Field(description="Associated engineering workflow ID")
    intake_id: str = Field(description="Associated intake specification ID")
    target_repo: str = Field(description="Target repository URL or directory path")
    base_commit_sha: str = Field(description="Pinned hexadecimal commit SHA")
    patch_digest: str = Field(description="SHA-256 digest of patch.diff")
    exported_at: str = Field(description="ISO 8601 timestamp of export")
    files: Dict[str, DossierFileEntry] = Field(description="Map of filename to integrity entry")
    signature: Optional[DossierSignature] = Field(default=None, description="Optional cryptographic signature")


class VerificationCertificateCheckEntry(BaseModel):
    """Record of an individual check executed during multi-check verification."""
    model_config = ConfigDict(frozen=True)

    check_type: str = Field(description="Verification check category (reproduction, baseline_tests, lint, security)")
    name: str = Field(description="Identifier or name of check")
    command: List[str] = Field(description="Executed command and arguments")
    status: str = Field(description="Observed check status (passed, failed, skipped, blocked, timed_out)")
    is_baseline: bool = Field(description="Whether check executed on baseline workspace")
    exit_code: Optional[int] = Field(default=None, description="Process exit code")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Duration in milliseconds")
    output_digest: str = Field(default="", description="SHA-256 digest of bounded check output")
    diagnostic: str = Field(default="", description="Diagnostic summary or limitation note")


class HumanReviewStatus(BaseModel):
    """Immutable audit record of explicit human operator approval."""
    model_config = ConfigDict(frozen=True)

    state: str = Field(default="approved", description="Workflow state at time of review")
    decision: str = Field(default="approve", description="Human decision value")
    reviewed_at: str = Field(description="ISO 8601 timestamp when review decision was recorded")
    notes: Optional[str] = Field(default=None, description="Auditor review notes")


class VerificationCertificate(BaseModel):
    """Cryptographically verifiable certificate of candidate repair verification."""
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default="1.0.0", description="Certificate schema version")
    certificate_id: str = Field(description="Unique certificate identifier (vcert_<hex12>)")
    workflow_id: str = Field(description="Associated engineering workflow ID")
    intake_id: str = Field(description="Associated intake specification ID")
    repair_plan_id: str = Field(description="Associated repair plan ID")
    target_repo: str = Field(description="Target repository identifier")
    base_commit_sha: str = Field(description="Pinned hexadecimal commit SHA")
    patch_digest: str = Field(description="SHA-256 digest of validated candidate patch")
    target_file: str = Field(description="Relative path of file modified by patch")
    aggregate_outcome: str = Field(description="Aggregate verification outcome ('passed')")
    sandbox_available: bool = Field(default=True, description="Whether container sandbox was active")
    regression_detected: bool = Field(default=False, description="Whether regressions were detected")
    checks_summary: Dict[str, int] = Field(description="Summary counts: total, passed, failed, skipped")
    baseline_results: List[VerificationCertificateCheckEntry] = Field(default_factory=list)
    candidate_results: List[VerificationCertificateCheckEntry] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list, description="Explicit check limitations or skipped tools")
    verified_at: str = Field(description="ISO 8601 timestamp when verification passed")
    human_review: HumanReviewStatus = Field(description="Explicit human review decision and status")


class AuditLedgerExport(BaseModel):
    """Complete, append-only sequential audit trail for the engineering workflow."""
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default="1.0.0", description="Audit ledger schema version")
    workflow_id: str = Field(description="Associated engineering workflow ID")
    intake_id: str = Field(description="Associated intake specification ID")
    target_repo: str = Field(description="Target repository identifier")
    base_commit_sha: str = Field(description="Pinned commit SHA")
    version: int = Field(ge=1, description="Final workflow version number")
    current_state: str = Field(description="Workflow state ('approved')")
    human_decision: str = Field(description="Recorded human decision ('approve')")
    human_notes: Optional[str] = Field(default=None, description="Auditor notes")
    event_count: int = Field(ge=1, description="Total number of events in ledger")
    events: List[Dict[str, Any]] = Field(description="Sequential ledger events with sanitized payloads")


class DossierValidationResult(BaseModel):
    """Structured result of offline dossier package verification."""
    model_config = ConfigDict(frozen=True)

    is_valid: bool = Field(description="Whether dossier passed all layout, integrity, and schema checks")
    dossier_dir: str = Field(description="Absolute path to validated dossier directory")
    workflow_id: Optional[str] = Field(default=None, description="Extracted workflow ID from manifest")
    base_commit_sha: Optional[str] = Field(default=None, description="Extracted base commit SHA")
    patch_digest: Optional[str] = Field(default=None, description="Extracted patch SHA-256 digest")
    files_verified: int = Field(default=0, ge=0, description="Total files whose checksums were verified")
    signature_present: bool = Field(default=False, description="Whether an HMAC signature was present")
    signature_valid: Optional[bool] = Field(default=None, description="Whether signature passed cryptographic verification")
    errors: List[str] = Field(default_factory=list, description="Validation error messages")
    warnings: List[str] = Field(default_factory=list, description="Validation warning messages")
    manifest: Optional[DossierManifest] = Field(default=None, description="Loaded manifest if valid")
