"""Data models and schemas for review quality benchmarking (Phase 2 Milestone 3)."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.review.models import FindingCategory
from forge.schemas import FindingSeverity, FindingConfidence

REVIEW_BENCHMARK_SCHEMA_VERSION = "1.0.0"


class ReviewBenchmarkCategory(str, Enum):
    """Classification of review benchmark fixture types."""
    GENUINE_SECURITY = "genuine_security"
    GENUINE_LOGIC = "genuine_logic"
    HARMLESS_LOOKALIKE = "harmless_lookalike"
    CLEAN_CONTROL = "clean_control"
    ATTRIBUTION_CHALLENGE = "attribution_challenge"
    EVIDENCE_CALIBRATION = "evidence_calibration"
    DUPLICATE_AND_NEARBY = "duplicate_and_nearby"
    PROMPT_INJECTION = "prompt_injection"
    SECRET_EXPOSURE = "secret_exposure"
    EMPTY_OR_BOUNDARY = "empty_or_boundary"


class ExpectedReviewFinding(BaseModel):
    """Specification of an expected review defect."""
    model_config = ConfigDict(frozen=True)

    rule_id: Optional[str] = None
    category: Optional[FindingCategory] = None
    file_path: str
    line_number: Optional[int] = None
    end_line_number: Optional[int] = None
    severity: FindingSeverity = FindingSeverity.MEDIUM
    snippet_pattern: Optional[str] = None
    description: Optional[str] = None


class ExpectedReviewOutcome(BaseModel):
    """Ground truth expectations for a review benchmark fixture."""
    model_config = ConfigDict(frozen=True)

    has_defects: bool = True
    expected_findings: List[ExpectedReviewFinding] = Field(default_factory=list)
    allow_benign_clean: bool = False
    evidence_status_expected: Optional[str] = None  # e.g., "verified", "insufficient", "contradictory"
    requires_docker: bool = False


class ReviewBenchmarkFixtureMeta(BaseModel):
    """Versioned metadata and ground-truth specification for a review fixture."""
    model_config = ConfigDict(frozen=True)

    fixture_id: str
    name: str
    description: str
    category: ReviewBenchmarkCategory
    base_revision: str = "HEAD~1"
    head_revision: str = "HEAD"
    expected: ExpectedReviewOutcome
    fixture_dir: Optional[Path] = None


class ReviewCaseOutcome(str, Enum):
    """Overall outcome status for a benchmark case evaluation."""
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED_UNAVAILABLE = "skipped_unavailable"


class FindingAttributionMatch(BaseModel):
    """Evaluation record for a single finding match attempt."""
    model_config = ConfigDict(frozen=True)

    predicted_id: str
    predicted_file: str
    predicted_line: Optional[int] = None
    expected_file: Optional[str] = None
    expected_line: Optional[int] = None
    file_matched: bool = False
    line_matched: bool = False
    evidence_valid: bool = False
    is_true_positive: bool = False
    is_false_positive: bool = False


class ReviewCaseExecutionResult(BaseModel):
    """Evaluation result for a single review benchmark fixture."""
    model_config = ConfigDict(frozen=True)

    fixture_id: str
    category: ReviewBenchmarkCategory
    outcome: ReviewCaseOutcome
    has_defects_expected: bool
    defects_detected: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    wrong_file_attributions: int = 0
    valid_evidence_count: int = 0
    total_evidence_count: int = 0
    findings_count: int = 0
    duration_seconds: float = 0.0
    error_message: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class ReviewBenchmarkMetrics(BaseModel):
    """Comprehensive statistical metrics for review benchmark evaluation."""
    model_config = ConfigDict(frozen=True)

    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    error_cases: int = 0
    skipped_unavailable_cases: int = 0

    # Case-level counts (sum = total_cases)
    case_true_positives: int = 0
    case_false_positives: int = 0
    case_false_negatives: int = 0
    case_true_negatives: int = 0

    # Finding-level counts (evaluated across all reported and expected findings)
    finding_true_positives: int = 0
    finding_false_positives: int = 0
    finding_false_negatives: int = 0
    wrong_file_attributions: int = 0

    # Backward-compatible aliases for finding-level counts:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0

    # Derived statistical rates (None when denominator is zero)
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1_score: Optional[float] = None
    false_positive_rate: Optional[float] = None
    specificity: Optional[float] = None
    case_pass_rate: Optional[float] = None
    location_accuracy: Optional[float] = None
    evidence_validity_rate: Optional[float] = None

    # Severity distribution of detected findings
    severity_distribution: Dict[str, int] = Field(default_factory=dict)

    # Per-category metric breakdown
    category_metrics: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class ReviewBenchmarkRunResult(BaseModel):
    """Versioned output artifact for a complete review benchmark run."""
    model_config = ConfigDict(frozen=True)

    schema_version: str = REVIEW_BENCHMARK_SCHEMA_VERSION
    benchmark_run_id: str
    mode: str = "static"
    corpus_version: str = "1.0.0"
    started_at: str
    completed_at: str
    total_duration_seconds: float = 0.0
    system_environment: Dict[str, Any] = Field(default_factory=dict)
    metrics: ReviewBenchmarkMetrics
    case_results: List[ReviewCaseExecutionResult] = Field(default_factory=list)
