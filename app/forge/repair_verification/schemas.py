"""Phase 12 M5: Repair Verification Schemas and Data Contracts.

Defines strict input, execution, planning, regression, baseline, evidence,
integrity, and tool contracts:
- Strict Pydantic models with ConfigDict(extra="forbid").
- Mandatory safety invariants: requires_human_review = True (cannot be disabled).
- Fail-closed verification gate semantics.
- Deterministic regression classifications and check outcomes.
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

MAX_OUTPUT_BYTES = 500_000
MAX_DESCRIPTION_CHARS = 10_000
MAX_LIMITATIONS_CHARS = 5_000
MAX_TESTS_PER_SUITE = 1_000
MAX_EVENTS_PER_VERIFICATION = 200


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VerificationStatus(str, Enum):
    """Lifecycle status of a repair verification run."""
    PENDING = "pending"
    PLANNED = "planned"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    REGRESSION_DETECTED = "regression_detected"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    RECOVERY_REQUIRED = "recovery_required"
    REQUIRES_HUMAN_REVIEW = "requires_human_review"


class RegressionClassification(str, Enum):
    """Classification of regression analysis outcome."""
    NO_REGRESSION_OBSERVED = "no_regression_observed"
    REGRESSION_DETECTED = "regression_detected"
    PRE_EXISTING_FAILURE = "pre_existing_failure"
    IMPROVED = "improved"
    INCONCLUSIVE = "inconclusive"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    VERIFICATION_INCOMPLETE = "verification_incomplete"


class TestScopeCategory(str, Enum):
    """Scope category of verification tests/checks."""
    __test__ = False
    TARGETED = "targeted"
    REGRESSION = "regression"
    FULL_SUITE = "full_suite"
    STATIC_ANALYSIS = "static_analysis"
    TYPE_CHECK = "type_check"
    INCOMPLETE = "incomplete"


class CheckOutcome(str, Enum):
    """Outcome of an individual test or static check."""
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    CRASH = "crash"
    XFAILED = "xfailed"
    XPASSED = "xpassed"
    COLLECTION_ERROR = "collection_error"


class VerificationEventType(str, Enum):
    """Category of audit events emitted during repair verification."""
    VERIFICATION_REQUESTED = "verification_requested"
    VERIFICATION_PLAN_GENERATED = "verification_plan_generated"
    SANDBOX_INSPECTED = "sandbox_inspected"
    BASELINE_EXECUTION_STARTED = "baseline_execution_started"
    BASELINE_EXECUTION_COMPLETED = "baseline_execution_completed"
    REPAIR_EXECUTION_STARTED = "repair_execution_started"
    REPAIR_EXECUTION_COMPLETED = "repair_execution_completed"
    RESULTS_PARSED = "results_parsed"
    REGRESSION_ANALYSIS_COMPLETED = "regression_analysis_completed"
    INTEGRITY_CHECK_COMPLETED = "integrity_check_completed"
    INTEGRITY_VIOLATION_DETECTED = "integrity_violation_detected"
    GATES_EVALUATED = "gates_evaluated"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"


# ---------------------------------------------------------------------------
# Verification Policy Contract
# ---------------------------------------------------------------------------

class VerificationPolicy(BaseModel):
    """Configuration governing boundaries, timeouts, and execution constraints."""
    model_config = ConfigDict(extra="forbid")

    allowed_executables: List[str] = Field(
        default=["pytest", "python", "python3", "flake8", "mypy"],
        description="Allowlist of executable binary names permitted for verification."
    )
    required_categories: List[TestScopeCategory] = Field(
        default=[TestScopeCategory.TARGETED],
        description="Test categories that must pass for verification to succeed."
    )
    optional_categories: List[TestScopeCategory] = Field(
        default=[TestScopeCategory.STATIC_ANALYSIS, TestScopeCategory.TYPE_CHECK],
        description="Optional test categories that are recorded but do not strictly fail verification."
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0.0,
        le=600.0,
        description="Per-command execution timeout in seconds."
    )
    memory_limit_mb: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description="Memory limit in megabytes."
    )
    cpu_quota: float = Field(
        default=2.0,
        gt=0.0,
        le=8.0,
        description="CPU quota limit."
    )
    pids_limit: int = Field(
        default=50,
        ge=10,
        le=200,
        description="Maximum process count."
    )
    max_output_bytes: int = Field(
        default=100_000,
        ge=1024,
        le=2_000_000,
        description="Maximum captured output bytes per command."
    )
    network_disabled: bool = Field(
        default=True,
        description="Whether network access is strictly disabled."
    )
    require_baseline_comparison: bool = Field(
        default=True,
        description="Whether verification requires running checks against baseline snapshot."
    )
    fail_on_regression: bool = Field(
        default=True,
        description="Whether any regression detected immediately fails verification."
    )
    fail_on_collection_error: bool = Field(
        default=True,
        description="Whether collection errors fail the check."
    )
    max_retries: int = Field(
        default=0,
        ge=0,
        le=2,
        description="Maximum command retries on transient errors."
    )

    @field_validator("network_disabled")
    @classmethod
    def enforce_network_disabled(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Verification policy strictly requires network_disabled=True")
        return v


# ---------------------------------------------------------------------------
# Test Selection & Planning Models
# ---------------------------------------------------------------------------

class TestIdentifier(BaseModel):
    """Structured identifier for a discovered or selected test."""
    __test__ = False
    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(description="Full pytest identifier, e.g. tests/test_calc.py::test_add")
    test_file: str = Field(description="Relative path to test file")
    test_name: str = Field(description="Name of test function or method")
    category: TestScopeCategory = Field(default=TestScopeCategory.TARGETED)


class SelectedCheck(BaseModel):
    """A concrete check or test command to be executed in the sandbox."""
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(description="Unique check identifier, e.g. chk_targeted_001")
    category: TestScopeCategory = Field(description="Scope category of this check")
    command: List[str] = Field(description="Structured argument array, e.g. ['pytest', '-v', 'tests/test_calc.py']")
    target_files: List[str] = Field(default_factory=list, description="Target files verified by this check")
    is_required: bool = Field(default=True, description="Whether this check is mandatory for verification")
    rationale: str = Field(description="Explanation for why this check was selected")


class VerificationPlan(BaseModel):
    """Structured, reproducible verification plan generated before execution."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Unique deterministic plan identifier")
    repair_plan_id: str = Field(description="Linked RepairPlan identifier")
    execution_id: str = Field(description="Linked RepairExecutionResult identifier")
    workspace_id: str = Field(description="Linked isolated workspace identifier")
    selected_checks: List[SelectedCheck] = Field(description="Ordered list of checks to execute")
    test_identifiers: List[TestIdentifier] = Field(default_factory=list, description="Discovered test items")
    selection_method: str = Field(description="Algorithm or strategy used to select tests")
    selection_rationale: str = Field(description="Detailed rationale for test selection")
    excluded_checks: Dict[str, str] = Field(default_factory=dict, description="Excluded checks and justification")
    is_partial_verification: bool = Field(default=False, description="True if selected tests are not full suite")
    policy: VerificationPolicy = Field(default_factory=VerificationPolicy)
    expected_baseline_digest: str = Field(description="SHA-256 tree digest of baseline snapshot")
    expected_patch_digest: str = Field(description="SHA-256 digest of applied patch")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Test Results & Parsing Models
# ---------------------------------------------------------------------------

class TestExecutionResult(BaseModel):
    """Outcome of a single test item."""
    __test__ = False
    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(description="Pytest test identifier")
    outcome: CheckOutcome = Field(description="Individual test outcome")
    duration_ms: float = Field(default=0.0, ge=0.0)
    message: Optional[str] = Field(default=None, description="Failure or error message")
    output_snippet: Optional[str] = Field(default=None, description="Relevant excerpt of failure output")


class CheckExecutionResult(BaseModel):
    """Outcome of executing a single SelectedCheck."""
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(description="Linked check identifier")
    category: TestScopeCategory = Field(description="Scope category")
    command: List[str] = Field(description="Executed command array")
    exit_code: Optional[int] = Field(default=None, description="Process exit code")
    outcome: CheckOutcome = Field(description="Synthesized check outcome")
    tests_passed: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)
    tests_errored: int = Field(default=0, ge=0)
    tests_skipped: int = Field(default=0, ge=0)
    tests_xfailed: int = Field(default=0, ge=0)
    tests_xpassed: int = Field(default=0, ge=0)
    collection_errors: int = Field(default=0, ge=0)
    test_results: List[TestExecutionResult] = Field(default_factory=list)
    stdout_digest: str = Field(default="", description="SHA-256 digest of stdout")
    stderr_digest: str = Field(default="", description="SHA-256 digest of stderr")
    duration_ms: float = Field(default=0.0, ge=0.0)
    timeout: bool = Field(default=False)
    malformed_report: bool = Field(default=False)
    raw_output_snippet: str = Field(default="")


class SuiteExecutionSummary(BaseModel):
    """Summary of executing a verification plan against a workspace (baseline or repaired)."""
    model_config = ConfigDict(extra="forbid")

    execution_type: str = Field(description="'baseline' or 'repaired'")
    workspace_id: str = Field(description="Workspace identifier")
    tree_digest: str = Field(description="SHA-256 workspace tree digest at execution time")
    checks: List[CheckExecutionResult] = Field(default_factory=list)
    all_passed: bool = Field(default=False)
    completed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sandbox_available: bool = Field(default=True)
    sandbox_details: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Regression Analysis Models
# ---------------------------------------------------------------------------

class RegressionFinding(BaseModel):
    """Specific regression finding comparing baseline and repaired outcomes."""
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(description="Check identifier")
    test_id: Optional[str] = Field(default=None, description="Specific test ID if applicable")
    classification: RegressionClassification = Field(description="Finding classification")
    baseline_outcome: Optional[CheckOutcome] = Field(default=None)
    repaired_outcome: Optional[CheckOutcome] = Field(default=None)
    explanation: str = Field(description="Human-readable explanation of comparison")
    evidence_digest: Optional[str] = Field(default=None, description="SHA-256 evidence digest")


# ---------------------------------------------------------------------------
# Verification Evidence & Events
# ---------------------------------------------------------------------------

class VerificationEvidenceRecord(BaseModel):
    """Verifiable evidence linked to a verification check."""
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(description="Unique evidence identifier, e.g. vev_001")
    verification_id: str = Field(description="Linked verification run identifier")
    source_command: List[str] = Field(description="Command that produced this evidence")
    category: str = Field(description="Category of evidence (e.g. test_output, static_check)")
    target_scope: str = Field(description="Target file or test scope")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    classification: str = Field(description="Classification of check outcome")
    output_digest: str = Field(description="SHA-256 digest of verified output")
    provenance: str = Field(default="forge:repair_verification")
    freshness_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class VerificationEvent(BaseModel):
    """Immutable audit event recorded during verification lifecycle."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(description="Unique deterministic event identifier")
    verification_id: str = Field(description="Linked verification identifier")
    sequence_number: int = Field(default=1, ge=1)
    prev_event_hash: Optional[str] = Field(default=None)
    event_hash: Optional[str] = Field(default=None)
    event_type: VerificationEventType = Field(description="Type of verification event")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str = Field(default="verification_engine")
    action_description: str = Field(description="Action description with secrets redacted")
    outcome: str = Field(default="success")


# ---------------------------------------------------------------------------
# Final Verification Result
# ---------------------------------------------------------------------------

class VerificationResult(BaseModel):
    """Complete, evidence-backed outcome produced by RepairVerificationEngine."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    verification_id: str = Field(description="Unique verification identifier")
    execution_id: str = Field(description="Linked M4 execution identifier")
    workspace_id: str = Field(description="Linked workspace identifier")
    repair_plan_id: str = Field(description="Linked RepairPlan identifier")
    repair_plan_digest: str = Field(description="Verified plan SHA-256 digest")
    applied_patch_digest: str = Field(description="Verified patch SHA-256 digest")
    baseline_tree_digest: str = Field(description="Verified baseline tree SHA-256 digest")
    verification_status: VerificationStatus = Field(description="Final verification lifecycle status")
    regression_classification: RegressionClassification = Field(description="Synthesized regression outcome")
    required_checks_completed: List[str] = Field(default_factory=list)
    required_checks_failed: List[str] = Field(default_factory=list)
    optional_checks_failed: List[str] = Field(default_factory=list)
    skipped_checks: Dict[str, str] = Field(default_factory=dict)
    test_counts: Dict[str, int] = Field(default_factory=dict)
    baseline_summary: Optional[SuiteExecutionSummary] = Field(default=None)
    repaired_summary: Optional[SuiteExecutionSummary] = Field(default=None)
    regression_findings: List[RegressionFinding] = Field(default_factory=list)
    evidence_records: List[VerificationEvidenceRecord] = Field(default_factory=list)
    gate_decision: str = Field(description="'PASSED', 'FAILED', 'BLOCKED', etc.")
    requires_human_review: bool = Field(default=True, description="Strictly True. Cannot be disabled.")
    limitations: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("requires_human_review")
    @classmethod
    def enforce_human_review_required(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Safety invariant breach: requires_human_review must strictly be True")
        return True

    @model_validator(mode="after")
    def validate_invariants(self) -> "VerificationResult":
        # Invariant 1: requires_human_review is always True
        if self.requires_human_review is not True:
            raise ValueError("Safety invariant breach: requires_human_review must strictly be True")

        # Invariant 2: Cannot mark PASSED if any required checks failed
        if self.verification_status == VerificationStatus.PASSED:
            if self.required_checks_failed:
                raise ValueError(
                    f"Integrity violation: cannot mark verification as PASSED when required checks failed: {self.required_checks_failed}"
                )
            if self.regression_classification == RegressionClassification.REGRESSION_DETECTED:
                raise ValueError(
                    "Integrity violation: cannot mark verification as PASSED when regression is detected"
                )
            if self.gate_decision != "PASSED":
                raise ValueError(
                    f"Integrity violation: verification_status is PASSED but gate_decision is {self.gate_decision}"
                )
        return self

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None, deep: bool = False) -> "VerificationResult":
        """Create a copy of the model, revalidating all invariants if updates are provided."""
        copied = super().model_copy(update=update, deep=deep)
        if update:
            return self.model_validate(copied.model_dump())
        return copied


# ---------------------------------------------------------------------------
# Tool Request & Response Schemas
# ---------------------------------------------------------------------------

class CreateRepairVerificationPlanInput(BaseModel):
    """Input for create_repair_verification_plan tool."""
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(description="M4 execution identifier")
    workspace_id: str = Field(description="Workspace identifier")
    repair_plan_id: str = Field(description="Repair plan identifier")
    policy_overrides: Optional[Dict[str, Any]] = Field(default=None, description="Optional policy overrides")


class CreateRepairVerificationPlanOutput(BaseModel):
    """Output from create_repair_verification_plan tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Created plan identifier")
    selected_checks_count: int = Field(description="Number of selected checks")
    is_partial_verification: bool = Field(description="Whether selection is partial")
    selection_rationale: str = Field(description="Rationale for check selection")
    status: str = Field(default="created")


class DiscoverRepairTestsInput(BaseModel):
    """Input for discover_repair_tests tool."""
    model_config = ConfigDict(extra="forbid")

    workspace_path: str = Field(description="Path to repository or isolated workspace")
    modified_files: List[str] = Field(default_factory=list, description="List of modified files to map tests for")


class DiscoverRepairTestsOutput(BaseModel):
    """Output from discover_repair_tests tool."""
    model_config = ConfigDict(extra="forbid")

    discovered_test_files: List[str] = Field(description="List of discovered test files")
    targeted_tests: List[str] = Field(description="Tests mapped directly to modified files")
    regression_tests: List[str] = Field(description="Tests mapped to dependent modules")
    total_discovered: int = Field(description="Total count of discovered test items")


class InspectVerificationEnvironmentInput(BaseModel):
    """Input for inspect_verification_environment tool."""
    model_config = ConfigDict(extra="forbid")


class InspectVerificationEnvironmentOutput(BaseModel):
    """Output from inspect_verification_environment tool."""
    model_config = ConfigDict(extra="forbid")

    sandbox_available: bool = Field(description="Whether isolated container sandbox is available")
    sandbox_healthy: bool = Field(description="Whether container sandbox passed health checks")
    diagnostic_message: str = Field(description="Detailed diagnostic or error message")
    docker_image: Optional[str] = Field(default=None, description="Configured docker image")
    allowed_executables: List[str] = Field(description="Permitted executables")


class RunRepairVerificationInput(BaseModel):
    """Input for run_repair_verification tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Verification plan identifier")
    workspace_id: str = Field(description="Workspace identifier")


class RunRepairVerificationOutput(BaseModel):
    """Output from run_repair_verification tool."""
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(description="Verification run identifier")
    status: VerificationStatus = Field(description="Verification outcome status")
    all_passed: bool = Field(description="Whether all required checks passed")
    tests_passed: int = Field(default=0)
    tests_failed: int = Field(default=0)
    collection_errors: int = Field(default=0)
    diagnostic_message: Optional[str] = Field(default=None)


class RunBaselineVerificationInput(BaseModel):
    """Input for run_baseline_verification tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Verification plan identifier")
    workspace_id: str = Field(description="Workspace identifier")


class RunBaselineVerificationOutput(BaseModel):
    """Output from run_baseline_verification tool."""
    model_config = ConfigDict(extra="forbid")

    execution_type: str = Field(default="baseline")
    baseline_digest_verified: bool = Field(description="Whether baseline snapshot digest was verified")
    all_passed: bool = Field(description="Whether all baseline checks passed")
    tests_passed: int = Field(default=0)
    tests_failed: int = Field(default=0)
    diagnostic_message: Optional[str] = Field(default=None)


class CompareRepairVerificationInput(BaseModel):
    """Input for compare_repair_verification tool."""
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(description="Verification run identifier")


class CompareRepairVerificationOutput(BaseModel):
    """Output from compare_repair_verification tool."""
    model_config = ConfigDict(extra="forbid")

    classification: RegressionClassification = Field(description="Regression analysis classification")
    regressions_count: int = Field(default=0)
    improvements_count: int = Field(default=0)
    pre_existing_failures_count: int = Field(default=0)
    explanation: str = Field(description="Comparison summary")


class InspectVerificationEvidenceInput(BaseModel):
    """Input for inspect_verification_evidence tool."""
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(description="Verification run identifier")
    evidence_id: Optional[str] = Field(default=None, description="Optional specific evidence identifier")


class InspectVerificationEvidenceOutput(BaseModel):
    """Output from inspect_verification_evidence tool."""
    model_config = ConfigDict(extra="forbid")

    evidence_records: List[VerificationEvidenceRecord] = Field(default_factory=list)
    total_records: int = Field(default=0)


class ValidateVerificationIntegrityInput(BaseModel):
    """Input for validate_verification_integrity tool."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(description="Workspace identifier to validate")


class ValidateVerificationIntegrityOutput(BaseModel):
    """Output from validate_verification_integrity tool."""
    model_config = ConfigDict(extra="forbid")

    original_repo_unmodified: bool = Field(description="True if original repository was untouched")
    baseline_snapshot_intact: bool = Field(description="True if baseline snapshot digest is intact")
    repaired_workspace_valid: bool = Field(description="True if repaired workspace has no unauthorized mutations")
    integrity_status: str = Field(description="'VALID' or 'VIOLATION_DETECTED'")
    violations: List[str] = Field(default_factory=list)


class GetRepairVerificationSummaryInput(BaseModel):
    """Input for get_repair_verification_summary tool."""
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(description="Verification run identifier")


class GetRepairVerificationSummaryOutput(BaseModel):
    """Output from get_repair_verification_summary tool."""
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(description="Verification identifier")
    status: VerificationStatus = Field(description="Verification status")
    gate_decision: str = Field(description="Gate decision ('PASSED', 'FAILED', 'BLOCKED')")
    regression_classification: RegressionClassification = Field(description="Regression classification")
    requires_human_review: bool = Field(default=True)
    summary_markdown: str = Field(description="Formatted human-readable markdown summary")
