"""Pydantic data models for benchmark cases, suites, executions, and metrics."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

BENCHMARK_SCHEMA_VERSION = "1.0.0"


class BenchmarkCategory(str, Enum):
    BOUNDARY_CONDITION = "boundary_condition"
    CALCULATION_ERROR = "calculation_error"
    INPUT_VALIDATION = "input_validation"
    UNSAFE_PATH = "unsafe_path"
    EXCEPTION_HANDLING = "exception_handling"
    CLEAN_CONTROL = "clean_control"
    PROMPT_INJECTION = "prompt_injection"


class ExpectedDefect(BaseModel):
    """Specification of the expected defect or clean status for a fixture."""
    model_config = ConfigDict(frozen=True)

    has_defect: bool = True
    category: Optional[str] = None
    severity: str = "medium"
    observable_condition: Optional[str] = None


class BenchmarkExecutionRequirements(BaseModel):
    """Execution rules and sandbox constraints for a benchmark case."""
    model_config = ConfigDict(frozen=True)

    requires_sandbox: bool = False
    test_command: Optional[List[str]] = None


class BenchmarkCase(BaseModel):
    """Validated metadata specification for an individual benchmark fixture."""
    model_config = ConfigDict(frozen=True)

    case_id: str
    name: str
    description: str
    category: BenchmarkCategory
    target_file: str
    target_symbol: Optional[str] = None
    expected_defect: ExpectedDefect
    execution_requirements: BenchmarkExecutionRequirements = Field(
        default_factory=BenchmarkExecutionRequirements
    )
    fixture_dir: Optional[Path] = None


class CaseOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED_UNAVAILABLE = "skipped_unavailable"


class CaseExecutionResult(BaseModel):
    """Outcome of running FORGE against a benchmark case."""
    case_id: str
    category: BenchmarkCategory
    outcome: CaseOutcome
    has_defect_expected: bool
    defect_detected: bool
    matched_target_file: bool = False
    matched_target_symbol: bool = False
    findings_count: int = 0
    duration_seconds: float = 0.0
    error_message: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkMetrics(BaseModel):
    """Statistical evaluation metrics for a benchmark suite execution."""
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    error_cases: int = 0
    skipped_unavailable_cases: int = 0

    # Binary classification counts for defect detection
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0

    # Derived metrics (None if denominator is 0)
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1_score: Optional[float] = None
    case_pass_rate: Optional[float] = None


class BenchmarkRunResult(BaseModel):
    """Complete summary of a benchmark suite run."""
    schema_version: str = BENCHMARK_SCHEMA_VERSION
    benchmark_run_id: str
    mode: str  # static or llm
    corpus_version: str = "1.0.0"
    started_at: str
    completed_at: str
    total_duration_seconds: float = 0.0

    system_environment: Dict[str, Any] = Field(default_factory=dict)
    metrics: BenchmarkMetrics
    case_results: List[CaseExecutionResult] = Field(default_factory=list)
