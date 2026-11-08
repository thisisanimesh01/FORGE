"""Phase 12 M4: Controlled Autonomous Repair Schemas and Contracts.

Defines strict input, execution, approval, workspace, patch, rollback, and event contracts:
- Strict Pydantic models with ConfigDict(extra="forbid").
- Immutable approval contracts bound to exact plan digest, strategy, file scope, and expiry.
- Isolated workspace lifecycle models tracking baseline commit and tree digests.
- Bounded repair patch representations with unified diff metrics.
- Comprehensive execution outcomes for handoff to Phase 12 M5 verification.
- Tool request and response schemas for ToolRegistry integration.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Maximum Input Size Boundaries
# ---------------------------------------------------------------------------

MAX_DIFF_BYTES = 500_000
MAX_DESCRIPTION_CHARS = 10_000
MAX_LIMITATIONS_CHARS = 5_000
MAX_AFFECTED_FILES = 50
MAX_EVENTS_PER_EXECUTION = 200


# ---------------------------------------------------------------------------
# Repair Execution Enums
# ---------------------------------------------------------------------------

class ExecutionStatus(str, Enum):
    """Lifecycle status of a controlled repair execution."""
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"
    BLOCKED = "blocked"


class ApprovalStatus(str, Enum):
    """Status of explicit human approval."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


class WorkspaceStatus(str, Enum):
    """Lifecycle status of an isolated repair workspace."""
    INITIALIZED = "initialized"
    READY = "ready"
    DIRTY = "dirty"
    CLEANED_UP = "cleaned_up"
    CORRUPTED = "corrupted"
    PRESERVED_FOR_INSPECTION = "preserved_for_inspection"


class PatchValidationStatus(str, Enum):
    """Outcome of patch syntax, target, and scope validation."""
    VALID = "valid"
    INVALID_SYNTAX = "invalid_syntax"
    SCOPE_VIOLATION = "scope_violation"
    PROTECTED_PATH_VIOLATION = "protected_path_violation"
    OVERSIZED = "oversized"
    DIGEST_MISMATCH = "digest_mismatch"
    STALE_BASE = "stale_base"


class RepairEventType(str, Enum):
    """Category of audit events emitted during controlled repair."""
    WORKSPACE_PREPARED = "workspace_prepared"
    APPROVAL_VERIFIED = "approval_verified"
    APPROVAL_REJECTED = "approval_rejected"
    PATCH_GENERATED = "patch_generated"
    PATCH_VALIDATED = "patch_validated"
    PATCH_APPLICATION_STARTED = "patch_application_started"
    PATCH_APPLICATION_SUCCEEDED = "patch_application_succeeded"
    PATCH_APPLICATION_FAILED = "patch_application_failed"
    DIFF_CAPTURED = "diff_captured"
    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_SUCCEEDED = "rollback_succeeded"
    ROLLBACK_FAILED = "rollback_failed"
    INTEGRITY_VERIFIED = "integrity_verified"
    INTEGRITY_VIOLATION_DETECTED = "integrity_violation_detected"


class FailureClassification(str, Enum):
    """Taxonomy of repair execution failures."""
    NONE = "none"
    UNAUTHORIZED_EXECUTION = "unauthorized_execution"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_REVOKED = "approval_revoked"
    SCOPE_BREACH = "scope_breach"
    DIFF_SYNTAX_ERROR = "diff_syntax_error"
    TARGET_NOT_FOUND = "target_not_found"
    PATCH_APPLICATION_FAILED = "patch_application_failed"
    UNEXPECTED_MODIFICATION = "unexpected_modification"
    TIMEOUT = "timeout"
    ROLLBACK_FAILED = "rollback_failed"
    WORKSPACE_ERROR = "workspace_error"


class RollbackStatus(str, Enum):
    """Status of workspace recovery / rollback."""
    NOT_TRIGGERED = "not_triggered"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PRESERVED = "preserved"


# ---------------------------------------------------------------------------
# Execution Limits Model
# ---------------------------------------------------------------------------

class ExecutionLimits(BaseModel):
    """Resource and size constraints enforced during repair execution."""
    model_config = ConfigDict(extra="forbid")

    max_patch_bytes: int = Field(default=100_000, ge=100, le=MAX_DIFF_BYTES)
    max_changed_lines: int = Field(default=500, ge=1, le=5_000)
    max_affected_files: int = Field(default=10, ge=1, le=MAX_AFFECTED_FILES)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    max_output_chars: int = Field(default=50_000, ge=1_000, le=200_000)


# ---------------------------------------------------------------------------
# Human Approval Model
# ---------------------------------------------------------------------------

class RepairApproval(BaseModel):
    """Explicit human approval contract authorizing repair patch application."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    approval_id: str = Field(description="Unique deterministic approval identifier.")
    repair_plan_id: str = Field(description="Exact RepairPlan ID approved.")
    plan_digest: str = Field(description="SHA-256 digest of the approved RepairPlan content.")
    approved_strategy_id: str = Field(description="Exact RepairStrategy ID approved.")
    approved_file_paths: List[str] = Field(description="Exact repository-relative file paths authorized.")
    approval_timestamp: str = Field(description="UTC ISO 8601 timestamp when approval was granted.")
    approval_expiry: str = Field(description="UTC ISO 8601 timestamp after which approval is invalid.")
    approver_reference: str = Field(description="Identity or reference of human approver (e.g. 'engineer@example.com').")
    approval_status: ApprovalStatus = Field(default=ApprovalStatus.APPROVED, description="Status of approval.")

    @model_validator(mode="after")
    def validate_approval_fields(self) -> "RepairApproval":
        if not self.approval_id:
            raise ValueError("approval_id cannot be empty")
        if not self.repair_plan_id:
            raise ValueError("repair_plan_id cannot be empty")
        if not self.plan_digest or len(self.plan_digest) != 64:
            raise ValueError("plan_digest must be a valid 64-char SHA-256 hex digest")
        if not self.approved_strategy_id:
            raise ValueError("approved_strategy_id cannot be empty")
        if not self.approved_file_paths:
            raise ValueError("approved_file_paths cannot be empty")
        if not self.approver_reference:
            raise ValueError("approver_reference cannot be empty")
        return self

    def is_expired(self, current_time: Optional[datetime] = None) -> bool:
        """Check whether the approval has expired against current UTC time."""
        now = current_time or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.approval_expiry)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return now > exp
        except Exception:
            return True


# ---------------------------------------------------------------------------
# Workspace & Request Models
# ---------------------------------------------------------------------------

class RepairWorkspace(BaseModel):
    """Metadata describing an isolated workspace allocated for repair application."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(description="Unique workspace identifier.")
    original_repository_reference: str = Field(description="Path or URL to original repository.")
    isolated_workspace_path: str = Field(description="Absolute path to the isolated repair workspace.")
    baseline_commit: str = Field(description="Commit SHA recorded at workspace initialization.")
    baseline_tree_digest: str = Field(description="SHA-256 digest of clean baseline workspace tree.")
    baseline_snapshot_path: Optional[str] = Field(default=None, description="Path to immutable baseline snapshot taken at workspace creation.")
    allowed_paths: List[str] = Field(default_factory=list, description="Approved file paths allowlisted for modification.")
    workspace_status: WorkspaceStatus = Field(default=WorkspaceStatus.INITIALIZED)
    creation_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cleanup_status: str = Field(default="active")


class RepairExecutionRequest(BaseModel):
    """Request to apply an approved repair plan inside an isolated workspace."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Unique execution identifier.")
    repair_plan_id: str = Field(description="Target RepairPlan ID.")
    repository_id: str = Field(description="Target repository reference or path.")
    approval: RepairApproval = Field(description="Validated human approval contract.")
    approved_plan_digest: str = Field(description="Digest of plan to be verified against approval.")
    requested_strategy_id: str = Field(description="Strategy ID requested.")
    requested_scope: List[str] = Field(description="Target file paths requested for modification.")
    execution_limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    workspace_policy: str = Field(default="isolated_worktree", description="Isolation strategy.")


# ---------------------------------------------------------------------------
# Patch Model
# ---------------------------------------------------------------------------

class RepairPatch(BaseModel):
    """Structured representation of a validated unified diff patch."""
    model_config = ConfigDict(extra="forbid")

    patch_id: str = Field(description="Unique deterministic patch identifier.")
    repair_plan_id: str = Field(description="Linked RepairPlan identifier.")
    strategy_id: str = Field(description="Linked RepairStrategy identifier.")
    unified_diff: str = Field(description="Standard unified diff content.")
    affected_files: List[str] = Field(default_factory=list, description="Files targeted by the patch.")
    added_lines: int = Field(default=0, ge=0)
    removed_lines: int = Field(default=0, ge=0)
    patch_digest: str = Field(description="SHA-256 digest of unified_diff text.")
    evidence_references: List[str] = Field(default_factory=list)
    validation_status: PatchValidationStatus = Field(default=PatchValidationStatus.VALID)

    @field_validator("unified_diff")
    @classmethod
    def check_diff_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_DIFF_BYTES:
            raise ValueError(f"unified_diff exceeds maximum allowed size of {MAX_DIFF_BYTES} bytes")
        return v


# ---------------------------------------------------------------------------
# Audit Event Model
# ---------------------------------------------------------------------------

class RepairEvent(BaseModel):
    """Immutable audit record tracking execution lifecycle events."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(description="Unique deterministic event identifier.")
    execution_id: str = Field(description="Linked execution identifier.")
    sequence_number: int = Field(default=1, ge=1, description="Monotonically increasing sequence number.")
    prev_event_hash: Optional[str] = Field(default=None, description="Cryptographic SHA-256 hash of previous event.")
    event_hash: Optional[str] = Field(default=None, description="Cryptographic SHA-256 hash of current event payload.")
    event_type: RepairEventType = Field(description="Type of lifecycle event.")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str = Field(description="Actor or component triggering event (e.g. 'repair_executor').")
    action_description: str = Field(description="Human-readable description of the action taken.")
    related_evidence: List[str] = Field(default_factory=list)
    integrity_digest: Optional[str] = Field(default=None, description="SHA-256 tree or patch digest at event time.")
    outcome: str = Field(default="success")


# ---------------------------------------------------------------------------
# Execution Result Model
# ---------------------------------------------------------------------------

class RepairExecutionResult(BaseModel):
    """Complete structured outcome produced by RepairExecutionEngine."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Unique execution identifier.")
    repair_plan_id: str = Field(description="Target RepairPlan ID.")
    execution_status: ExecutionStatus = Field(description="Final execution outcome status.")
    workspace_id: str = Field(description="Isolated workspace identifier.")
    applied_patch_digest: Optional[str] = Field(default=None, description="SHA-256 of successfully applied patch.")
    modified_files: List[str] = Field(default_factory=list, description="List of files verified to have changed.")
    diff_summary: str = Field(description="Captured unified diff or execution outcome summary.")
    execution_events: List[RepairEvent] = Field(default_factory=list, description="Ordered audit event trail.")
    rollback_status: RollbackStatus = Field(default=RollbackStatus.NOT_TRIGGERED)
    failure_classification: FailureClassification = Field(default=FailureClassification.NONE)
    limitations: List[str] = Field(default_factory=list, description="Acknowledged limitations and edge cases.")
    verification_pending: bool = Field(default=True, description="Strictly True for handoff to Phase 12 M5.")
    approval_reference: str = Field(description="Reference of approval that authorized execution.")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("diff_summary")
    @classmethod
    def check_diff_summary_size(cls, v: str) -> str:
        if len(v) > MAX_DIFF_BYTES:
            return v[:MAX_DIFF_BYTES] + "\n\n[TRUNCATED: Diff exceeded size cap]"
        return v


# ---------------------------------------------------------------------------
# Tool Request / Response Schemas
# ---------------------------------------------------------------------------

class PrepareRepairWorkspaceInput(BaseModel):
    """Input for prepare_repair_workspace tool."""
    model_config = ConfigDict(extra="forbid")

    repository_path: str = Field(description="Path to repository to prepare isolated workspace for.")
    plan_id: str = Field(description="Repair plan ID to be executed.")
    allowed_file_paths: List[str] = Field(description="Explicit list of files approved for modification.")


class PrepareRepairWorkspaceOutput(BaseModel):
    """Output from prepare_repair_workspace tool."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(description="Allocated isolated workspace ID.")
    isolated_path: str = Field(description="Path to isolated workspace.")
    baseline_commit: str = Field(description="Baseline commit SHA.")
    baseline_tree_digest: str = Field(description="SHA-256 baseline tree digest.")
    status: WorkspaceStatus = Field(description="Status of workspace.")


class ValidateRepairApprovalInput(BaseModel):
    """Input for validate_repair_approval tool."""
    model_config = ConfigDict(extra="forbid")

    approval_json: str = Field(description="JSON serialized RepairApproval model.")
    plan_json: Optional[str] = Field(default=None, description="Optional plan JSON to verify against.")
    plan_id: Optional[str] = Field(default=None, description="Optional plan ID.")


class ValidateRepairApprovalOutput(BaseModel):
    """Output from validate_repair_approval tool."""
    model_config = ConfigDict(extra="forbid")

    is_valid: bool = Field(description="True if approval satisfies all mandatory authorization invariants.")
    approval_status: ApprovalStatus = Field(description="Status of approval.")
    validation_errors: List[str] = Field(default_factory=list, description="List of authorization errors found.")
    approved_plan_id: Optional[str] = Field(default=None)
    approved_strategy_id: Optional[str] = Field(default=None)
    approved_file_paths: List[str] = Field(default_factory=list)


class GenerateRepairPatchInput(BaseModel):
    """Input for generate_repair_patch tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="ID of plan to synthesize patch for.")
    strategy_id: Optional[str] = Field(default=None, description="Target strategy ID.")
    plan_json: Optional[str] = Field(default=None, description="Optional serialized RepairPlan JSON.")


class GenerateRepairPatchOutput(BaseModel):
    """Output from generate_repair_patch tool."""
    model_config = ConfigDict(extra="forbid")

    patch_id: str = Field(description="Generated patch ID.")
    unified_diff: str = Field(description="Synthesized unified diff.")
    affected_files: List[str] = Field(default_factory=list)
    patch_digest: str = Field(description="SHA-256 digest of patch.")
    added_lines: int = Field(default=0)
    removed_lines: int = Field(default=0)
    validation_status: PatchValidationStatus = Field(default=PatchValidationStatus.VALID)


class ValidateRepairPatchInput(BaseModel):
    """Input for validate_repair_patch tool."""
    model_config = ConfigDict(extra="forbid")

    unified_diff: str = Field(description="Unified diff text to validate.")
    allowed_file_paths: List[str] = Field(description="Allowlisted file paths.")
    expected_patch_digest: Optional[str] = Field(default=None, description="Optional digest check.")


class ValidateRepairPatchOutput(BaseModel):
    """Output from validate_repair_patch tool."""
    model_config = ConfigDict(extra="forbid")

    is_valid: bool = Field(description="True if patch is syntactically and structurally valid.")
    validation_status: PatchValidationStatus = Field(description="Classification status.")
    validation_errors: List[str] = Field(default_factory=list)
    affected_files: List[str] = Field(default_factory=list)
    added_lines: int = Field(default=0)
    removed_lines: int = Field(default=0)
    patch_digest: str = Field(description="SHA-256 digest of validated diff.")


class ApplyApprovedRepairInput(BaseModel):
    """Input for apply_approved_repair tool."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(description="Allocated workspace ID.")
    plan_id: str = Field(description="Repair plan ID.")
    approval_json: str = Field(description="Serialized RepairApproval JSON.")
    plan_json: Optional[str] = Field(default=None, description="Optional serialized RepairPlan JSON.")
    patch_id: Optional[str] = Field(default=None, description="Pre-generated patch ID if available.")
    unified_diff: Optional[str] = Field(default=None, description="Optional unified diff string.")


class ApplyApprovedRepairOutput(BaseModel):
    """Output from apply_approved_repair tool."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Execution identifier.")
    execution_status: ExecutionStatus = Field(description="Result of execution.")
    modified_files: List[str] = Field(default_factory=list)
    applied_patch_digest: Optional[str] = Field(default=None)
    diff_summary: str = Field(description="Captured resulting diff.")
    rollback_status: RollbackStatus = Field(default=RollbackStatus.NOT_TRIGGERED)
    failure_classification: FailureClassification = Field(default=FailureClassification.NONE)
    verification_pending: bool = Field(default=True)


class InspectRepairExecutionInput(BaseModel):
    """Input for inspect_repair_execution tool."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Execution ID to inspect.")


class InspectRepairExecutionOutput(BaseModel):
    """Output from inspect_repair_execution tool."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Execution ID.")
    repair_plan_id: str = Field(description="Target RepairPlan ID.")
    execution_status: ExecutionStatus = Field(description="Execution status.")
    modified_files: List[str] = Field(default_factory=list)
    applied_patch_digest: Optional[str] = Field(default=None)
    rollback_status: RollbackStatus = Field(default=RollbackStatus.NOT_TRIGGERED)
    failure_classification: FailureClassification = Field(default=FailureClassification.NONE)
    event_count: int = Field(default=0)
    verification_pending: bool = Field(default=True)


class RollbackRepairWorkspaceInput(BaseModel):
    """Input for rollback_repair_workspace tool."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(description="Workspace ID to roll back.")
    execution_id: Optional[str] = Field(default=None, description="Optional execution ID.")


class RollbackRepairWorkspaceOutput(BaseModel):
    """Output from rollback_repair_workspace tool."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(description="Workspace ID.")
    rollback_status: RollbackStatus = Field(description="Rollback outcome.")
    restored_files: List[str] = Field(default_factory=list)
    workspace_tree_digest: str = Field(description="Digest of workspace after rollback.")
    message: str = Field(description="Summary message.")


class GetRepairExecutionHistoryInput(BaseModel):
    """Input for get_repair_execution_history tool."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Execution ID to retrieve events for.")


class GetRepairExecutionHistoryOutput(BaseModel):
    """Output from get_repair_execution_history tool."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="Execution ID.")
    events: List[Dict[str, Any]] = Field(default_factory=list)
    event_count: int = Field(default=0)
