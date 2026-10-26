"""Data models, schemas, and metrics structures for Phase 4 Repair Benchmarking."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.repair.contracts import RepairOutcome
from forge.schemas import FindingSeverity

REPAIR_BENCHMARK_SCHEMA_VERSION = "1.0.0"


class RepairBenchmarkCategory(str, Enum):
    """Categorization of repair benchmark defect cases."""
    SYNTAX_ERROR = "syntax_error"
    INCORRECT_LOGIC = "incorrect_logic"
    BOUNDARY_CONDITION = "boundary_condition"
    MISSING_VALIDATION = "missing_validation"
    EXCEPTION_HANDLING = "exception_handling"
    SECURITY_SENSITIVE = "security_sensitive"
    CLEAN_CONTROL = "clean_control"
    AMBIGUOUS_FINDING = "ambiguous_finding"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ADVERSARIAL_INSTRUCTION = "adversarial_instruction"
    REGRESSION_INDUCING = "regression_inducing"


class ExpectedPatchConstraints(BaseModel):
    """Allowlist and budget boundaries for acceptable repair patches."""
    model_config = ConfigDict(frozen=True)

    allowed_files: List[str]
    max_lines: int = Field(default=50, ge=1, le=500)
    max_hunks: int = Field(default=5, ge=1, le=20)
    max_bytes: int = Field(default=10_000, ge=100, le=100_000)
    forbidden_tokens: List[str] = Field(default_factory=list)
    required_tokens: List[str] = Field(default_factory=list)


class ExpectedRepairOutcomeSpec(BaseModel):
    """Ground truth expectations for a repair benchmark fixture."""
    model_config = ConfigDict(frozen=True)

    has_defect: bool = True
    expected_outcome: RepairOutcome = RepairOutcome.REPAIR_VERIFIED
    expected_plan_valid: bool = True
    requires_evidence: bool = True
    allow_no_fix: bool = False
    test_oracle_command: List[str] = Field(default_factory=lambda: ["pytest"])


class RepairBenchmarkFixtureMeta(BaseModel):
    """Versioned metadata and ground-truth specification for a repair fixture."""
    model_config = ConfigDict(frozen=True)

    fixture_id: str
    name: str
    description: str
    category: RepairBenchmarkCategory
    target_file: str
    base_commit_sha: str = "a1b2c3d4e5f67890"
    severity: FindingSeverity = FindingSeverity.HIGH
    expected: ExpectedRepairOutcomeSpec
    constraints: ExpectedPatchConstraints
    scripted_response: Optional[Dict[str, Any]] = None
    scripted_diff: Optional[str] = None
    fixture_dir: Optional[Path] = None


class RepairBenchmarkCaseResult(BaseModel):
    """Detailed evaluation result for an individual repair benchmark fixture."""
    model_config = ConfigDict(frozen=True)

    fixture_id: str
    category: RepairBenchmarkCategory
    has_defect_expected: bool
    plan_generated: bool
    plan_valid: bool
    patch_generated: bool
    patch_accepted: bool
    scope_compliant: bool
    evidence_grounded: bool
    lines_changed: int = 0
    diff_bytes: int = 0
    expected_outcome: RepairOutcome
    observed_outcome: RepairOutcome
    outcome_matched: bool
    is_regression: bool = False
    is_false_fix: bool = False
    is_clean_pass: bool = False
    duration_ms: float = 0.0
    diagnostic: str = ""


class RepairBenchmarkMetrics(BaseModel):
    """Comprehensive metric report for the repair benchmark suite."""
    model_config = ConfigDict(frozen=True)

    total_cases: int
    passed_cases: int
    failed_cases: int
    inconclusive_cases: int
    skipped_cases: int

    plan_generation_rate: Optional[float] = None
    valid_plan_rate: Optional[float] = None
    evidence_grounding_rate: Optional[float] = None
    patch_acceptance_rate: Optional[float] = None
    scope_compliance_rate: Optional[float] = None
    outcome_accuracy: Optional[float] = None
    regression_rate: Optional[float] = None
    false_fix_rate: Optional[float] = None

    average_lines_changed: float = 0.0
    average_diff_bytes: float = 0.0

    # Defect identification metrics where ground-truth binary labels apply
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1_score: Optional[float] = None
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0


class RepairBenchmarkRunResult(BaseModel):
    """Complete serialized outcome of a repair benchmark evaluation run."""
    model_config = ConfigDict(frozen=True)

    benchmark_id: str
    schema_version: str = REPAIR_BENCHMARK_SCHEMA_VERSION
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    mode: str
    provider: str
    model: str
    sandbox_available: bool
    metrics: RepairBenchmarkMetrics
    case_results: List[RepairBenchmarkCaseResult]
    disclaimer: str = (
        "Notice: Synthetic benchmark metrics measure adherence to deterministic specification fixtures "
        "and security boundaries in offline test environments. They do not constitute a guarantee of "
        "real-world defect repair efficacy or production software safety."
    )
