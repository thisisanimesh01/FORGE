"""Phase 8 Repair Execution Architecture — Core Domain Models.

Establishes typed, explicit domain models for software repair execution,
patch artifacts, validation results, failure classifications, and audit events.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =============================================================================
# Lifecycle and Actor Enums
# =============================================================================

class RepairExecutionState(str, Enum):
    """Explicit lifecycle states for a Phase 8 repair execution."""
    CREATED = "created"
    PLANNED = "planned"
    VALIDATING = "validating"
    READY = "ready"
    EXECUTING = "executing"
    PATCH_GENERATED = "patch_generated"
    PATCH_VALIDATING = "patch_validating"
    VERIFIED = "verified"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RepairActor(str, Enum):
    """Authorized components/actors permitted to perform specific lifecycle transitions."""
    PLANNER = "repair_planner"
    VALIDATOR = "patch_validator"
    EXECUTOR = "repair_executor"
    VERIFIER = "repair_verifier"
    ORCHESTRATOR = "repair_orchestrator"
    OPERATOR = "human_operator"


class FailureCategory(str, Enum):
    """Fine-grained classification of repair and verification failures."""
    SYNTAX_ERROR = "syntax_error"
    BUILD_BROKEN = "build_broken"
    TEST_FAILED = "test_failed"
    REGRESSION_DETECTED = "regression_detected"
    BUDGET_EXCEEDED = "budget_exceeded"
    SCOPE_VIOLATION = "scope_violation"
    PROTECTED_PATH = "protected_path"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    TIMEOUT = "timeout"
    WORKSPACE_ERROR = "workspace_error"
    INTERNAL_ERROR = "internal_error"
    INTEGRITY_ERROR = "integrity_error"
    ADVERSARIAL_DEFECT = "adversarial_defect"
    SEMANTIC_VIOLATION = "semantic_violation"


class ValidationOutcome(str, Enum):
    """Explicit outcomes for independent patch validation."""
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


# =============================================================================
# Value and Entity Models
# =============================================================================

class FailureClassification(BaseModel):
    """Structured and classified failure details."""
    model_config = ConfigDict(frozen=True)

    category: FailureCategory = Field(description="Deterministic failure category")
    message: str = Field(description="Sanitized failure message")
    details: Dict[str, Any] = Field(default_factory=dict, description="Structured diagnostics")
    retryable: bool = Field(default=False, description="Whether this failure mode allows retry")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Failure timestamp")


class ExecutionTestResult(BaseModel):
    """Individual test case outcome observed during isolated verification."""
    model_config = ConfigDict(frozen=True)

    test_name: str = Field(description="Identifier or name of the executed test")
    passed: bool = Field(description="Whether the test passed")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Execution duration in milliseconds")
    output_excerpt: str = Field(default="", description="Sanitized and bounded test output excerpt")
    failure_message: Optional[str] = Field(default=None, description="Failure diagnostic if failed")


class ValidationResult(BaseModel):
    """Deterministic validation outcome for a repair plan or patch artifact."""
    model_config = ConfigDict(frozen=True)

    is_valid: bool = Field(description="Whether validation passed completely")
    violations: List[str] = Field(default_factory=list, description="List of policy or safety violations")
    checks_passed: List[str] = Field(default_factory=list, description="List of validation checks that passed")
    validated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Timestamp of validation")


class IndependentValidationResult(ValidationResult):
    """Defense-in-depth independent validation result for a candidate patch artifact."""
    model_config = ConfigDict(frozen=True)

    validation_id: str = Field(description="Deterministic validation identifier (val_indep_...)")
    patch_artifact_id: str = Field(description="Target patch artifact ID")
    repair_id: str = Field(description="Associated repair request identifier")
    finding_id: str = Field(description="Associated finding identifier")
    repository_target: str = Field(description="Target repository identifier or URL")
    base_commit_sha: str = Field(description="Pinned repository base commit revision")
    workspace_id: str = Field(description="Validated workspace identifier")
    patch_sha256: str = Field(description="SHA-256 digest of exact validated patch bytes")
    validated_file_paths: List[str] = Field(default_factory=list, description="Files modified by patch")
    validated_hunk_count: int = Field(default=0, ge=0, description="Hunks count verified from raw diff")
    lines_added: int = Field(default=0, ge=0, description="Recalculated added lines count")
    lines_deleted: int = Field(default=0, ge=0, description="Recalculated deleted lines count")
    patch_bytes: int = Field(default=0, ge=0, description="Raw patch byte size")
    scope_result: Dict[str, Any] = Field(default_factory=dict, description="Detailed scope validation findings")
    integrity_result: Dict[str, Any] = Field(default_factory=dict, description="Detailed cryptographic & baseline integrity findings")
    semantic_result: Dict[str, Any] = Field(default_factory=dict, description="Detailed AST & semantic analysis findings")
    overall_status: ValidationOutcome = Field(default=ValidationOutcome.REJECTED, description="Final independent validation outcome")
    rejection_reasons: List[str] = Field(default_factory=list, description="Concrete rejection rationales if not accepted")
    policy_version: str = Field(default="1.0", description="Security policy version applied")


class PatchArtifact(BaseModel):
    """Immutable representation of a candidate code patch artifact."""
    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(description="Deterministic artifact identifier (e.g. patch_<sha256[:12]>)")
    repair_id: str = Field(description="Associated repair request identifier")
    finding_id: str = Field(description="Associated finding identifier")
    base_commit_sha: str = Field(description="Pinned repository base commit revision")
    target_file: str = Field(description="Relative path of primary target file")
    diff_content: str = Field(description="Unified diff format patch content")
    content_hash: str = Field(description="SHA-256 digest of normalized diff_content")
    lines_added: int = Field(default=0, ge=0, description="Total count of added lines")
    lines_deleted: int = Field(default=0, ge=0, description="Total count of deleted lines")
    hunks_count: int = Field(default=0, ge=0, description="Total count of diff hunks")
    affected_paths: List[str] = Field(default_factory=list, description="All relative paths modified by the patch")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")

    @field_validator("base_commit_sha")
    @classmethod
    def validate_commit_sha(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not re.match(r"^[0-9a-f]{7,40}$", cleaned):
            raise ValueError(f"Invalid git commit SHA: '{v}' (must be 7-40 hex chars)")
        return cleaned

    @field_validator("diff_content")
    @classmethod
    def validate_diff_content(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("diff_content cannot be empty")
        return v

    @classmethod
    def create(
        cls,
        repair_id: str,
        finding_id: str,
        base_commit_sha: str,
        target_file: str,
        diff_content: str,
        affected_paths: Optional[List[str]] = None,
    ) -> "PatchArtifact":
        """Factory creating a PatchArtifact with automatic SHA-256 hash and metric computation."""
        cleaned_diff = diff_content.strip() + "\n"
        digest = hashlib.sha256(cleaned_diff.encode("utf-8")).hexdigest()
        artifact_id = f"patch_{digest[:12]}"

        # Compute line and hunk metrics from unified diff
        added = 0
        deleted = 0
        hunks = 0
        for line in cleaned_diff.splitlines():
            if line.startswith("@@") and line.endswith("@@"):
                hunks += 1
            elif line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1

        paths = affected_paths or [target_file]
        return cls(
            artifact_id=artifact_id,
            repair_id=repair_id,
            finding_id=finding_id,
            base_commit_sha=base_commit_sha,
            target_file=target_file,
            diff_content=cleaned_diff,
            content_hash=digest,
            lines_added=added,
            lines_deleted=deleted,
            hunks_count=hunks,
            affected_paths=paths,
        )


class RepairExecutionPlan(BaseModel):
    """Bounded, evidence-grounded repair plan formulated by a RepairPlanner."""
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(description="Unique deterministic plan identifier")
    repair_id: str = Field(description="Associated repair request identifier")
    target_file: str = Field(description="Relative path of file to modify")
    root_cause: str = Field(description="Identified root cause of the defect")
    proposed_change: str = Field(description="High-level description of proposed fix")
    expected_behavior: str = Field(description="Expected behavior after repair")
    verification_command: str = Field(description="Pre-approved command to verify repair in sandbox")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score")
    rationale: Optional[str] = Field(default=None, description="Concise justification for proposed repair approach")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")


class RepairRequest(BaseModel):
    """Core domain entity tracking a software repair request and its lifecycle."""
    model_config = ConfigDict(frozen=False)

    repair_id: str = Field(description="Unique identifier for the repair request")
    finding_id: str = Field(description="Associated validated finding identifier")
    target_repo: str = Field(description="Repository URL or absolute path")
    base_commit_sha: str = Field(description="Pinned immutable git commit SHA")
    evidence_ids: List[str] = Field(description="Evidence record IDs supporting the finding")
    allowed_files: List[str] = Field(description="Explicit list of relative file paths permitted for modification")
    run_id: Optional[str] = Field(default=None, description="Associated investigation run ID if applicable")
    workspace_id: Optional[str] = Field(default=None, description="Assigned isolated workspace identifier")
    patch_artifact_id: Optional[str] = Field(default=None, description="Assigned patch artifact identifier")
    state: RepairExecutionState = Field(default=RepairExecutionState.CREATED, description="Current lifecycle state")
    validation_status: Optional[str] = Field(default=None, description="Status of plan/patch validation")
    verification_status: Optional[str] = Field(default=None, description="Status of isolated patch verification")
    failure: Optional[FailureClassification] = Field(default=None, description="Failure details if in FAILED state")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Last state update timestamp")

    @field_validator("base_commit_sha")
    @classmethod
    def validate_commit_sha(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not re.match(r"^[0-9a-f]{7,40}$", cleaned):
            raise ValueError(f"Invalid git commit SHA: '{v}' (must be 7-40 hex chars)")
        return cleaned

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("evidence_ids cannot be empty; repair request must be grounded in verified evidence")
        return v

    @field_validator("allowed_files")
    @classmethod
    def validate_allowed_files(cls, files: List[str]) -> List[str]:
        if not files:
            raise ValueError("allowed_files cannot be empty; scope must be explicitly bounded")
        cleaned = []
        for f in files:
            norm = Path(f).as_posix().strip()
            if not norm or norm == ".":
                raise ValueError("allowed_files cannot contain empty paths or '.'")
            if norm.startswith("/") or norm.startswith("\\"):
                raise ValueError(f"allowed_files must be relative paths: '{f}'")
            if ".." in norm.split("/"):
                raise ValueError(f"Path traversal ('..') forbidden in allowed_files: '{f}'")
            cleaned.append(norm)
        return cleaned


class RepairExecutionEvent(BaseModel):
    """Immutable audit event recorded on each repair state change or significant operation."""
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(description="Unique event identifier")
    repair_id: str = Field(description="Associated repair request identifier")
    sequence: int = Field(ge=1, description="Monotonically increasing sequence number")
    from_state: Optional[RepairExecutionState] = Field(default=None, description="Previous lifecycle state")
    to_state: RepairExecutionState = Field(description="Target lifecycle state")
    actor: RepairActor = Field(description="Component or human actor triggering the transition")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Sanitized event parameters")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Event UTC timestamp")


class RepairExecutionRecord(BaseModel):
    """Complete serialized snapshot of a repair execution including history and artifacts."""
    model_config = ConfigDict(frozen=True)

    request: RepairRequest = Field(description="Base repair request")
    plan: Optional[RepairExecutionPlan] = Field(default=None, description="Repair plan if formulated")
    patch: Optional[PatchArtifact] = Field(default=None, description="Patch artifact if generated")
    validation: Optional[ValidationResult] = Field(default=None, description="Validation result if validated")
    test_results: List[ExecutionTestResult] = Field(default_factory=list, description="Observed verification test outcomes")
    failure: Optional[FailureClassification] = Field(default=None, description="Failure diagnostic if failed")
    events: List[RepairExecutionEvent] = Field(default_factory=list, description="Ordered audit event trail")


class WorkspaceRecord(BaseModel):
    """Persistent audit record of an isolated repair scratch workspace."""
    model_config = ConfigDict(frozen=True)

    workspace_id: str = Field(description="Unique workspace identifier")
    repair_id: str = Field(description="Associated repair request identifier")
    target_repo: str = Field(description="Target repository identifier or URL")
    base_commit_sha: str = Field(description="Pinned git commit SHA")
    workspace_path: str = Field(description="Absolute path to workspace directory")
    provisioning_status: str = Field(default="provisioned", description="Provisioning status: provisioned, failed, cleaned")
    isolation_verified: bool = Field(default=False, description="Whether workspace isolation was successfully verified")
    cleanup_status: str = Field(default="active", description="Cleanup status: active, cleaned, failed")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata such as baseline hash count, sandbox mode")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Last update timestamp")


# Compatibility alias
RepairExecutionRequest = RepairRequest
