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
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_INVALIDATED = "approval_invalidated"
    APPROVAL_EXPIRED = "approval_expired"


class ValidationOutcome(str, Enum):
    """Explicit outcomes for independent patch validation."""
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


class VerificationOutcome(str, Enum):
    """Explicit outcomes for sandbox repair verification."""
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class TestSelectionCategory(str, Enum):
    """Categorization of test commands selected for repair verification."""
    __test__ = False

    REPRODUCTION = "reproduction"
    MODULE = "module"
    UNIT = "unit"
    INTEGRATION = "integration"
    REGRESSION = "regression"


class ComparisonStatus(str, Enum):
    """Outcome of comparing baseline vs patched execution for a specific test."""
    FIXED = "fixed"
    STABLE_PASS = "stable_pass"
    PRE_EXISTING_FAILURE = "pre_existing_failure"
    NEW_REGRESSION = "new_regression"
    NEW_ERROR = "new_error"
    INCONCLUSIVE = "inconclusive"


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
    prev_event_hash: Optional[str] = Field(default=None, description="SHA-256 hash of previous event in chain")
    event_hash: Optional[str] = Field(default=None, description="SHA-256 hash of this event and chain")


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


class SelectedTest(BaseModel):
    """Specific test command selected for sandbox verification execution."""
    model_config = ConfigDict(frozen=True)

    test_id: str = Field(description="Deterministic identifier for the selected test")
    test_name: str = Field(description="Human-readable identifier or path of the test")
    category: TestSelectionCategory = Field(description="Selection category (reproduction, module, unit, etc.)")
    command: List[str] = Field(description="Vetted executable command line array")
    relative_cwd: str = Field(default=".", description="Working directory relative to workspace root")
    target_file: Optional[str] = Field(default=None, description="Primary source or test file targeted")
    rationale: str = Field(default="", description="Audit explanation for why this test was selected")
    expected_to_fail_on_baseline: bool = Field(default=False, description="True if this test reproduces the reported defect")


class VerificationPlan(BaseModel):
    """Structured plan defining tests to execute for patch verification."""
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(description="Unique verification plan identifier")
    repair_id: str = Field(description="Associated repair request identifier")
    patch_artifact_id: str = Field(description="Target patch artifact identifier")
    selected_tests: List[SelectedTest] = Field(default_factory=list, description="Ordered list of tests to run")
    timeout_seconds: float = Field(default=60.0, ge=1.0, description="Total timeout cap for verification")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Plan creation timestamp")


class TestExecutionRecord(BaseModel):
    """Structured record of an individual test command execution inside the sandbox."""
    __test__ = False
    model_config = ConfigDict(frozen=True)

    execution_id: str = Field(description="Unique test execution identifier")
    verification_id: str = Field(description="Associated verification run identifier")
    repair_id: str = Field(description="Associated repair request identifier")
    workspace_id: str = Field(description="Workspace identifier where test executed")
    target_type: str = Field(description="Target environment: 'baseline' or 'patched'")
    test_name: str = Field(description="Test name or command identifier")
    command: List[str] = Field(description="Exact command executed")
    selection_rationale: str = Field(default="", description="Why this test was run")
    exit_code: Optional[int] = Field(default=None, description="Process exit code")
    status: str = Field(default="passed", description="Execution status: passed, failed, timeout, inconclusive, error")
    passed: bool = Field(default=False, description="Whether test passed completely with observed assertions")
    timed_out: bool = Field(default=False, description="Whether execution timed out")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Duration in milliseconds")
    stdout_excerpt: str = Field(default="", description="Sanitized and bounded standard output")
    stderr_excerpt: str = Field(default="", description="Sanitized and bounded standard error")
    tests_collected: int = Field(default=0, ge=0, description="Number of tests collected by runner")
    tests_executed: int = Field(default=0, ge=0, description="Number of tests executed by runner")
    tests_passed: int = Field(default=0, ge=0, description="Number of tests passed")
    tests_failed: int = Field(default=0, ge=0, description="Number of tests failed")
    tests_skipped: int = Field(default=0, ge=0, description="Number of tests skipped")
    tests_errored: int = Field(default=0, ge=0, description="Number of tests that encountered errors")
    failure_message: Optional[str] = Field(default=None, description="Failure diagnostic or error message")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Start timestamp")
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Completion timestamp")


class TestComparisonResult(BaseModel):
    """Comparative outcome between baseline and patched execution for a specific test."""
    __test__ = False
    model_config = ConfigDict(frozen=True)

    test_name: str = Field(description="Name or command of test compared")
    status: ComparisonStatus = Field(description="Outcome status (fixed, stable_pass, new_regression, etc.)")
    baseline_passed: Optional[bool] = Field(default=None, description="Baseline pass status")
    patched_passed: Optional[bool] = Field(default=None, description="Patched pass status")
    details: str = Field(default="", description="Diagnostic details or explanation")


class RegressionAnalysisRecord(BaseModel):
    """Detailed regression analysis comparing baseline and patched test suites."""
    model_config = ConfigDict(frozen=True)

    regression_detected: bool = Field(default=False, description="True if newly introduced failures or errors detected")
    defect_resolved: bool = Field(default=False, description="True if defect reproduction test is confirmed fixed")
    comparisons: List[TestComparisonResult] = Field(default_factory=list, description="Itemized test comparisons")
    new_failures_count: int = Field(default=0, ge=0, description="Number of newly failing tests")
    fixed_tests_count: int = Field(default=0, ge=0, description="Number of tests that changed from fail to pass")
    stable_tests_count: int = Field(default=0, ge=0, description="Number of tests passing on both")
    pre_existing_failures_count: int = Field(default=0, ge=0, description="Number of tests failing on both")
    summary: str = Field(default="", description="High-level regression analysis summary")


class VerificationRunRecord(BaseModel):
    """Complete, immutable audit record of a sandbox patch verification run."""
    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(description="Unique verification run identifier (verif_...)")
    repair_id: str = Field(description="Associated repair request identifier")
    patch_artifact_id: str = Field(description="Target patch artifact identifier")
    validation_id: str = Field(description="Associated M4 validation identifier")
    repository_target: str = Field(description="Target repository identifier or URL")
    base_commit_sha: str = Field(description="Pinned repository base commit revision")
    workspace_id: str = Field(description="Patched workspace identifier")
    baseline_workspace_id: Optional[str] = Field(default=None, description="Baseline workspace identifier if used")
    patch_sha256: str = Field(description="SHA-256 digest of exact verified patch bytes")
    plan: VerificationPlan = Field(description="Verification plan with selected tests")
    baseline_executions: List[TestExecutionRecord] = Field(default_factory=list, description="Baseline execution records")
    patched_executions: List[TestExecutionRecord] = Field(default_factory=list, description="Patched execution records")
    regression_analysis: RegressionAnalysisRecord = Field(description="Comparative regression analysis")
    overall_outcome: VerificationOutcome = Field(default=VerificationOutcome.FAILED, description="Final verification outcome")
    failure_category: Optional[FailureCategory] = Field(default=None, description="Failure category if not verified")
    failure_message: Optional[str] = Field(default=None, description="Detailed failure diagnostic message")
    evidence_ids: List[str] = Field(default_factory=list, description="Evidence records created during verification")
    logs_excerpt: str = Field(default="", description="Sanitized excerpt of execution logs")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Verification start timestamp")
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Verification completion timestamp")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Total verification duration in milliseconds")


# Compatibility alias
RepairExecutionRequest = RepairRequest


# =============================================================================
# Milestone 6: Operator Review, Approval & Authorization Models
# =============================================================================

class ApprovalState(str, Enum):
    """Guarded lifecycle states for repair review and approval."""
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class OperatorDecisionType(str, Enum):
    """Explicit decision choices submitted by an authorized operator."""
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    WITHDRAW = "withdraw"


class AuthorizedAction(str, Enum):
    """Specific downstream publication actions that can be authorized by operators."""
    EXPORT_PATCH = "export_patch"
    STAGE_FOR_PR = "stage_for_pr"
    MANUAL_INTEGRATION = "manual_integration"
    STAGE_BRANCH = "stage_branch"
    CREATE_PR = "create_pr"


class ApprovalBinding(BaseModel):
    """Cryptographic and contextual binding tuple tying an approval to exact artifacts."""
    model_config = ConfigDict(frozen=True)

    repair_id: str = Field(description="Unique repair request identifier")
    finding_id: str = Field(description="Associated finding identifier")
    repository_target: str = Field(description="Target repository identifier or URL")
    base_commit_sha: str = Field(description="Exact base commit revision SHA")
    patch_artifact_id: str = Field(description="Bound patch artifact identifier")
    patch_sha256: str = Field(description="Exact SHA-256 digest of verified patch bytes")
    validation_id: str = Field(description="Bound M4 independent validation result identifier")
    verification_id: str = Field(description="Bound M5 sandbox verification run identifier")
    dossier_id: str = Field(description="Associated review dossier identifier")
    dossier_version: str = Field(default="1.0.0", description="Review dossier schema version")
    policy_version: str = Field(default="1.0.0", description="Approval policy version")


class ReviewDossier(BaseModel):
    """Structured, factual dossier consolidating complete audit evidence for operator review."""
    model_config = ConfigDict(frozen=True)

    dossier_id: str = Field(description="Unique review dossier identifier (dossier_...)")
    dossier_version: str = Field(default="1.0.0", description="Review dossier schema version")
    repair_id: str = Field(description="Associated repair request identifier")
    finding_id: str = Field(description="Associated finding identifier")
    finding_title: str = Field(description="Title of original finding under repair")
    finding_severity: str = Field(description="Severity of original finding")
    repository_target: str = Field(description="Target repository identifier or URL")
    base_commit_sha: str = Field(description="Exact pinned base commit SHA")
    evidence_ids: List[str] = Field(default_factory=list, description="Associated evidence record IDs")
    patch_artifact_id: str = Field(description="Candidate patch artifact identifier")
    patch_sha256: str = Field(description="SHA-256 digest of exact candidate patch bytes")
    target_files: List[str] = Field(default_factory=list, description="Target files touched by patch")
    lines_added: int = Field(default=0, ge=0, description="Total inserted lines in patch")
    lines_removed: int = Field(default=0, ge=0, description="Total deleted lines in patch")
    hunk_count: int = Field(default=0, ge=0, description="Total diff hunks in patch")
    unified_diff: str = Field(description="Full unified diff of candidate patch")
    validation_id: str = Field(description="M4 independent validation result identifier")
    validation_outcome: str = Field(description="Outcome of independent M4 validation")
    verification_id: str = Field(description="M5 sandbox verification run identifier")
    verification_outcome: str = Field(description="Outcome of M5 sandbox verification")
    baseline_test_summary: Dict[str, Any] = Field(default_factory=dict, description="Summary of baseline test run")
    patched_test_summary: Dict[str, Any] = Field(default_factory=dict, description="Summary of patched test run")
    regression_analysis_summary: str = Field(default="", description="Summary of comparative regression analysis")
    regression_detected: bool = Field(default=False, description="Whether regressions were detected")
    defect_resolved: bool = Field(default=False, description="Whether original defect was demonstrably resolved")
    workspace_integrity_confirmed: bool = Field(default=True, description="Whether workspace file integrity is confirmed")
    workspace_id: str = Field(description="Workspace identifier where verification occurred")
    security_findings: List[str] = Field(default_factory=list, description="Security invariant checks or notices")
    limitations: List[str] = Field(default_factory=list, description="Known operational or verification limitations")
    recommended_decision: str = Field(default="MANUAL_REVIEW", description="Deterministic recommendation based on verification facts")
    facts: List[str] = Field(default_factory=list, description="Factual statements directly traceable to stored records")
    model_explanations: List[str] = Field(default_factory=list, description="Model-generated explanations labeled as AI text")
    unresolved_uncertainties: List[str] = Field(default_factory=list, description="Identified uncertainties or open questions")
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Dossier generation timestamp")


class ApprovalDecisionRecord(BaseModel):
    """Immutable audit record of an operator review decision."""
    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(description="Unique decision record identifier (dec_...)")
    repair_id: str = Field(description="Associated repair request identifier")
    binding: ApprovalBinding = Field(description="Artifact and context binding")
    state: ApprovalState = Field(description="Resulting approval lifecycle state")
    decision: OperatorDecisionType = Field(description="Operator decision choice")
    authorized_action: Optional[AuthorizedAction] = Field(default=None, description="Authorized action if approved")
    actor: str = Field(description="Operator or agent identifier who recorded the decision")
    reason: str = Field(description="Mandatory justification or rationale for the decision")
    feedback: Optional[str] = Field(default=None, description="Actionable feedback if changes were requested")
    expires_at: Optional[datetime] = Field(default=None, description="Approval expiration timestamp if TTL configured")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Decision creation timestamp")
    invalidation_reason: Optional[str] = Field(default=None, description="Reason for invalidation if invalidated")
    invalidation_timestamp: Optional[datetime] = Field(default=None, description="Timestamp of invalidation if invalidated")
