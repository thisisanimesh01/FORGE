"""Phase 12 M1: Defect Diagnosis Data Contracts and Schemas.

Defines strict input and output contracts for evidence-driven defect diagnosis:
- Input models (user descriptions, test failures, static findings, changesets, stack traces).
- Defect taxonomy and classification categories.
- Candidate failure locations and localization provenance.
- Hypotheses, evaluations, and structured diagnosis results.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.schemas import FindingSeverity, FindingConfidence


# ---------------------------------------------------------------------------
# Maximum Input Size Boundaries
# ---------------------------------------------------------------------------

MAX_DESCRIPTION_CHARS = 10_000
MAX_STACK_TRACE_CHARS = 15_000
MAX_DIFF_CHARS = 50_000
MAX_ERROR_MESSAGE_CHARS = 5_000
MAX_EXPECTED_ACTUAL_CHARS = 5_000
MAX_REPRODUCTION_CHARS = 10_000


# ---------------------------------------------------------------------------
# Classification & Status Enums
# ---------------------------------------------------------------------------

class DefectCategory(str, Enum):
    """Deterministic taxonomy of software defects."""
    LOGIC_ERROR = "logic_error"
    INCORRECT_DATA_HANDLING = "incorrect_data_handling"
    INPUT_VALIDATION_FAILURE = "input_validation_failure"
    EXCEPTION_HANDLING_DEFECT = "exception_handling_defect"
    RESOURCE_MANAGEMENT_ISSUE = "resource_management_issue"
    DEPENDENCY_INTEGRATION_DEFECT = "dependency_integration_defect"
    CONCURRENCY_ISSUE = "concurrency_issue"
    SECURITY_VULNERABILITY = "security_vulnerability"
    PERFORMANCE_DEFECT = "performance_defect"
    CONFIGURATION_DEFECT = "configuration_defect"
    TEST_BUILD_FAILURE = "test_build_failure"
    UNKNOWN = "unknown"


class DiagnosisStatus(str, Enum):
    """Lifecycle status of a defect diagnosis."""
    CONFIRMED = "confirmed"      # Verified by direct disk facts and failure reproduction
    SUSPECTED = "suspected"      # Plausible hypothesis backed by partial evidence
    UNRESOLVED = "unresolved"    # Inconclusive, competing unrefuted hypotheses, or missing context


class LocationSource(str, Enum):
    """Provenance mechanism that identified a candidate failure location."""
    STACK_TRACE = "stack_trace"
    ERROR_MESSAGE = "error_message"
    TEST_FAILURE = "test_failure"
    SYMBOL_RELATIONSHIP = "symbol_relationship"
    CHANGE_IMPACT = "change_impact"
    STATIC_ANALYSIS = "static_analysis"
    USER_SPECIFIED = "user_specified"


# ---------------------------------------------------------------------------
# Candidate Location
# ---------------------------------------------------------------------------

class CandidateLocation(BaseModel):
    """A localized candidate code location relevant to a failure or defect."""
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="Normalized relative path to source file.")
    start_line: int = Field(ge=1, description="1-indexed starting line number.")
    end_line: int = Field(ge=1, description="1-indexed ending line number.")
    symbol_id: Optional[str] = Field(default=None, description="Optional qualified symbol identifier.")
    source: LocationSource = Field(description="Mechanism that identified this location.")
    confidence_weight: float = Field(ge=0.0, le=1.0, description="Heuristic localization confidence weight.")
    reason: str = Field(description="Explanation of why this location was identified.")
    is_verified_on_disk: bool = Field(default=False, description="True if verified against current disk content.")

    @model_validator(mode="after")
    def validate_line_bounds(self) -> "CandidateLocation":
        if self.end_line < self.start_line:
            raise ValueError(f"end_line ({self.end_line}) cannot be less than start_line ({self.start_line})")
        return self


# ---------------------------------------------------------------------------
# Specialized Input Reports
# ---------------------------------------------------------------------------

class TestFailureReport(BaseModel):
    """Details of a failed test execution."""
    __test__ = False
    model_config = ConfigDict(extra="forbid")

    test_name: str = Field(description="Identifier or path of failing test.")
    test_file: Optional[str] = Field(default=None, description="Repository-relative file path of test.")
    failure_message: str = Field(description="Direct failure or assertion error message.")
    stack_trace: Optional[str] = Field(default=None, description="Formatted Python stack trace if available.")
    line_number: Optional[int] = Field(default=None, ge=1, description="Line number of failure in test file.")

    @field_validator("failure_message")
    @classmethod
    def bound_failure_message(cls, v: str) -> str:
        if len(v) > MAX_ERROR_MESSAGE_CHARS:
            return v[:MAX_ERROR_MESSAGE_CHARS] + " [TRUNCATED]"
        return v

    @field_validator("stack_trace")
    @classmethod
    def bound_stack_trace(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_STACK_TRACE_CHARS:
            return v[:MAX_STACK_TRACE_CHARS] + " [TRUNCATED]"
        return v


class StaticFindingReport(BaseModel):
    """Details of a static-analysis or linter finding."""
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="Linter or rule identifier.")
    file_path: str = Field(description="Repository-relative path of affected file.")
    line_number: int = Field(ge=1, description="Line number flagged by rule.")
    message: str = Field(description="Diagnostic message.")
    severity: Optional[FindingSeverity] = Field(default=None, description="Linter-reported severity.")


# ---------------------------------------------------------------------------
# Diagnosis Input Contract
# ---------------------------------------------------------------------------

class DiagnosisInput(BaseModel):
    """Structured input parameters for defect diagnosis."""
    model_config = ConfigDict(extra="forbid")

    user_description: Optional[str] = Field(default=None, description="User-reported defect description.")
    test_failure: Optional[TestFailureReport] = Field(default=None, description="Associated test failure report.")
    finding_id: Optional[str] = Field(default=None, description="Existing FORGE finding identifier to diagnose.")
    static_finding: Optional[StaticFindingReport] = Field(default=None, description="Static-analysis finding.")
    target_files: List[str] = Field(default_factory=list, description="Explicit repository files to focus on.")
    target_symbols: List[str] = Field(default_factory=list, description="Explicit symbols to focus on.")
    diff: Optional[str] = Field(default=None, description="Unified git diff to correlate with failure.")
    modified_files: List[str] = Field(default_factory=list, description="Files modified in recent changes.")
    error_message: Optional[str] = Field(default=None, description="Direct error string or exception message.")
    stack_trace: Optional[str] = Field(default=None, description="Python traceback string.")
    expected_behavior: Optional[str] = Field(default=None, description="Description of expected outcome.")
    actual_behavior: Optional[str] = Field(default=None, description="Description of observed outcome.")
    reproduction_steps: Optional[str] = Field(default=None, description="Steps or command to reproduce.")
    relevant_test_names: List[str] = Field(default_factory=list, description="Names of relevant test functions.")

    @field_validator("user_description")
    @classmethod
    def check_description_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"user_description exceeds {MAX_DESCRIPTION_CHARS} character budget ({len(v)} chars)")
        return v

    @field_validator("stack_trace")
    @classmethod
    def check_stack_trace_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_STACK_TRACE_CHARS:
            raise ValueError(f"stack_trace exceeds {MAX_STACK_TRACE_CHARS} character budget ({len(v)} chars)")
        return v

    @field_validator("diff")
    @classmethod
    def check_diff_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_DIFF_CHARS:
            raise ValueError(f"diff exceeds {MAX_DIFF_CHARS} character budget ({len(v)} chars)")
        return v

    @field_validator("error_message")
    @classmethod
    def check_error_message_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_ERROR_MESSAGE_CHARS:
            raise ValueError(f"error_message exceeds {MAX_ERROR_MESSAGE_CHARS} character budget ({len(v)} chars)")
        return v

    @field_validator("expected_behavior", "actual_behavior")
    @classmethod
    def check_behavior_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_EXPECTED_ACTUAL_CHARS:
            raise ValueError(f"behavior field exceeds {MAX_EXPECTED_ACTUAL_CHARS} character budget ({len(v)} chars)")
        return v

    @field_validator("reproduction_steps")
    @classmethod
    def check_repro_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_REPRODUCTION_CHARS:
            raise ValueError(f"reproduction_steps exceeds {MAX_REPRODUCTION_CHARS} character budget ({len(v)} chars)")
        return v

    @model_validator(mode="after")
    def validate_has_signal(self) -> "DiagnosisInput":
        has_signal = any([
            bool(self.user_description and self.user_description.strip()),
            self.test_failure is not None,
            bool(self.finding_id and self.finding_id.strip()),
            self.static_finding is not None,
            bool(self.target_files),
            bool(self.target_symbols),
            bool(self.diff and self.diff.strip()),
            bool(self.modified_files),
            bool(self.error_message and self.error_message.strip()),
            bool(self.stack_trace and self.stack_trace.strip()),
        ])
        if not has_signal:
            raise ValueError("DiagnosisInput requires at least one diagnostic input signal (e.g. description, test failure, finding_id, error message, or stack trace).")
        return self


# ---------------------------------------------------------------------------
# Hypothesis Models
# ---------------------------------------------------------------------------

class DiagnosisHypothesis(BaseModel):
    """A candidate explanation for a diagnosed defect."""
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(description="Unique identifier for hypothesis (e.g. 'hyp_a1b2c3d4').")
    description: str = Field(description="Clear explanation of the suspected failure mechanism.")
    suspected_location: CandidateLocation = Field(description="Code location suspected of harboring the defect.")
    defect_category: DefectCategory = Field(description="Classified category of this suspected defect.")
    supporting_evidence_ids: List[str] = Field(default_factory=list, description="IDs of evidence records supporting this hypothesis.")
    contradicting_evidence_ids: List[str] = Field(default_factory=list, description="IDs of evidence records contradicting this hypothesis.")
    confidence: FindingConfidence = Field(description="Confidence rating for this hypothesis.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Deterministic numeric score in [0.0, 1.0].")
    missing_information: List[str] = Field(default_factory=list, description="Uncertainties or unverified assumptions.")
    suggested_verification_method: str = Field(description="Recommended test or inspection to verify this hypothesis.")


# ---------------------------------------------------------------------------
# Diagnosis Result
# ---------------------------------------------------------------------------

class DiagnosisResult(BaseModel):
    """Structured outcome produced by the Evidence-Driven Defect Diagnosis Engine."""
    model_config = ConfigDict(extra="forbid")

    diagnosis_id: str = Field(description="Unique deterministic identifier for this diagnosis.")
    run_id: str = Field(description="FORGE run identifier.")
    input_summary: str = Field(description="Summary of provided input signals and symptoms.")
    failing_behavior: str = Field(description="Description of what behavior is failing.")
    defect_category: DefectCategory = Field(description="Primary classified defect category.")
    severity: FindingSeverity = Field(description="Estimated impact severity (independent from confidence).")
    status: DiagnosisStatus = Field(description="Diagnosis status: confirmed, suspected, or unresolved.")
    confidence: FindingConfidence = Field(description="Independent confidence level in this diagnosis.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Deterministic heuristic confidence score.")
    root_cause_locations: List[CandidateLocation] = Field(default_factory=list, description="Candidate root-cause locations ordered by likelihood.")
    hypotheses: List[DiagnosisHypothesis] = Field(default_factory=list, description="Evaluated diagnostic hypotheses.")
    supporting_evidence_ids: List[str] = Field(default_factory=list, description="Grounded supporting evidence records.")
    contradicting_evidence_ids: List[str] = Field(default_factory=list, description="Grounded contradicting evidence records.")
    confidence_explanation: str = Field(description="Detailed rationale for the assigned confidence score and status.")
    verification_requirements: List[str] = Field(default_factory=list, description="Specific tests or inspections required to confirm or refute.")
    recommended_next_step: str = Field(description="Actionable next step for investigation or repair planning.")
    limitations: Optional[str] = Field(default=None, description="Explicit boundaries, assumptions, or missing context.")
    associated_finding_id: Optional[str] = Field(default=None, description="Linked Finding row in storage if created.")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="UTC ISO timestamp.")
