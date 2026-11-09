"""Phase 12 M6: Repair Quality & Reliability Schemas and Data Contracts.

Defines strict input, quality metrics, evidence, policy, scoring, gate,
and tool contracts:
- Strict Pydantic models with ConfigDict(extra="forbid", validate_assignment=True).
- Mandatory safety invariant: requires_human_review = True (cannot be disabled).
- Fail-closed quality gate semantics.
- Deterministic dimension scores and metrics.
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

MAX_FINDINGS_COUNT = 500
MAX_DESCRIPTION_CHARS = 10_000
MAX_LIMITATIONS_CHARS = 5_000
MAX_EVENTS_PER_ANALYSIS = 200
MAX_HISTORY_RECORDS = 1_000


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class QualityStatus(str, Enum):
    """Lifecycle status of a repair quality evaluation."""
    PENDING = "pending"
    ANALYZING = "analyzing"
    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REGRESSION_RISK = "regression_risk"
    HIGH_RISK = "high_risk"
    REQUIRES_HUMAN_REVIEW = "requires_human_review"
    BLOCKED = "blocked"


class QualityGateDecision(str, Enum):
    """Decision produced by the Quality Gatekeeper."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"
    BLOCKED = "BLOCKED"


class QualityDimension(str, Enum):
    """Core dimensions of repair engineering quality."""
    CORRECTNESS = "correctness"
    COMPLETENESS = "completeness"
    MAINTAINABILITY = "maintainability"
    PATCH_QUALITY = "patch_quality"
    RELIABILITY = "reliability"
    SECURITY_RISK = "security_risk"


class RiskSeverity(str, Enum):
    """Assessed risk severity level."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NEGLIGIBLE = "negligible"


class ConfidenceClassification(str, Enum):
    """Confidence classification based on evidence grounding and sample size."""
    VERY_HIGH = "very_high"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EvidenceCategory(str, Enum):
    """Categories of supporting evidence."""
    DIAGNOSIS = "diagnosis"
    REPAIR_PLAN = "repair_plan"
    REPAIR_EXECUTION = "repair_execution"
    REPAIR_PATCH = "repair_patch"
    VERIFICATION_RESULT = "verification_result"
    BASELINE_COMPARISON = "baseline_comparison"
    REGRESSION_ANALYSIS = "regression_analysis"
    STATIC_ANALYSIS = "static_analysis"
    HISTORICAL_RECORD = "historical_record"
    WORKSPACE_INTEGRITY = "workspace_integrity"


class EvidenceValidationStatus(str, Enum):
    """Validation outcome for a piece of evidence."""
    VALID = "valid"
    STALE = "stale"
    DIGEST_MISMATCH = "digest_mismatch"
    MISSING = "missing"
    FORGED_OR_CORRUPT = "forged_or_corrupt"
    UNVERIFIED = "unverified"


class RepairQualityEventType(str, Enum):
    """Audit events emitted during quality evaluation lifecycle."""
    QUALITY_ANALYSIS_STARTED = "quality_analysis_started"
    EVIDENCE_VALIDATED = "evidence_validated"
    PATCH_QUALITY_ANALYZED = "patch_quality_analyzed"
    MAINTAINABILITY_ANALYZED = "maintainability_analyzed"
    COMPLETENESS_ANALYZED = "completeness_analyzed"
    CORRECTNESS_EVALUATED = "correctness_evaluated"
    RELIABILITY_ANALYZED = "reliability_analyzed"
    RISK_ANALYZED = "risk_analyzed"
    QUALITY_SCORE_CALCULATED = "quality_score_calculated"
    QUALITY_GATE_EVALUATED = "quality_gate_evaluated"
    QUALITY_ANALYSIS_COMPLETED = "quality_analysis_completed"
    QUALITY_ANALYSIS_BLOCKED = "quality_analysis_blocked"


# ---------------------------------------------------------------------------
# Evidence & Event Models
# ---------------------------------------------------------------------------

class RepairQualityEvidence(BaseModel):
    """Cryptographically anchored evidence item used in quality evaluation."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(description="Unique identifier for evidence item")
    source_milestone: str = Field(description="Milestone providing this evidence (e.g. M2, M3, M4, M5)")
    source_record_id: str = Field(description="Original ID of the source record")
    source_digest: str = Field(description="Cryptographic SHA-256 digest of source content")
    evidence_category: EvidenceCategory = Field(description="Category of evidence")
    observation: str = Field(description="Observation or claim supported by evidence")
    measurement_method: str = Field(description="Method used to derive observation (e.g. ast_analysis, diff_parser)")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    provenance: str = Field(default="forge:repair_quality")
    freshness_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    validation_status: EvidenceValidationStatus = Field(default=EvidenceValidationStatus.VALID)


class RepairQualityEvent(BaseModel):
    """Append-only SHA-256 chained audit event."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(description="Deterministic event identifier")
    analysis_id: str = Field(description="Linked quality analysis identifier")
    sequence_number: int = Field(default=1, ge=1)
    prev_event_hash: Optional[str] = Field(default=None)
    event_hash: Optional[str] = Field(default=None)
    event_type: RepairQualityEventType = Field(description="Type of quality event")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str = Field(default="quality_engine")
    action_description: str = Field(description="Sanitized action description")
    outcome: str = Field(default="success")


# ---------------------------------------------------------------------------
# Policy & Configuration
# ---------------------------------------------------------------------------

class RepairQualityPolicy(BaseModel):
    """Configurable thresholds and weights for quality scoring and gating."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = Field(default="1.0.0")
    max_patch_lines: int = Field(default=500, ge=1)
    max_files_changed: int = Field(default=10, ge=1)
    max_complexity_increase: int = Field(default=5, ge=0)
    max_duplication_increase_lines: int = Field(default=20, ge=0)
    max_scope_expansion_files: int = Field(default=0, ge=0)
    allow_regressions: bool = Field(default=False)
    min_verification_completeness: float = Field(default=1.0, ge=0.0, le=1.0)
    min_overall_score: float = Field(default=0.70, ge=0.0, le=1.0)
    min_dimension_score: float = Field(default=0.50, ge=0.0, le=1.0)
    min_statistical_sample_size: int = Field(default=5, ge=1)
    dimension_weights: Dict[QualityDimension, float] = Field(
        default_factory=lambda: {
            QualityDimension.CORRECTNESS: 0.25,
            QualityDimension.COMPLETENESS: 0.20,
            QualityDimension.MAINTAINABILITY: 0.15,
            QualityDimension.PATCH_QUALITY: 0.15,
            QualityDimension.RELIABILITY: 0.15,
            QualityDimension.SECURITY_RISK: 0.10,
        }
    )
    require_all_evidence: bool = Field(default=True)

    @field_validator("allow_regressions")
    @classmethod
    def enforce_allow_regressions_false(cls, v: bool) -> bool:
        if v is not False:
            raise ValueError("Safety invariant breach: allow_regressions cannot be True")
        return False

    @field_validator("dimension_weights")
    @classmethod
    def validate_weights(cls, weights: Dict[QualityDimension, float]) -> Dict[QualityDimension, float]:
        for dim, w in weights.items():
            if w < 0.0:
                raise ValueError(f"Weight for dimension '{dim.value}' cannot be negative: {w}")
        if sum(weights.values()) <= 0.0:
            raise ValueError("Sum of dimension weights must be greater than zero")
        return weights


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class RepairQualityMetrics(BaseModel):
    """Comprehensive evidence-backed metrics calculated for a repair."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Patch metrics
    patch_lines_added: int = Field(default=0, ge=0)
    patch_lines_removed: int = Field(default=0, ge=0)
    patch_total_lines: int = Field(default=0, ge=0)
    files_changed_count: int = Field(default=0, ge=0)
    change_concentration: float = Field(default=1.0, ge=0.0, le=1.0)
    unnecessary_change_indicators: List[str] = Field(default_factory=list)

    # Maintainability & Complexity
    cyclomatic_complexity_delta: Optional[int] = Field(default=None)
    function_length_delta: Optional[int] = Field(default=None)
    duplication_indicators: List[str] = Field(default_factory=list)
    dependency_changes: List[str] = Field(default_factory=list)
    public_api_changes: List[str] = Field(default_factory=list)
    type_lint_findings: List[str] = Field(default_factory=list)
    dead_code_indicators: List[str] = Field(default_factory=list)

    # Verification & Regressions
    verification_completeness_ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    regressions_detected_count: int = Field(default=0, ge=0)
    pre_existing_failures_count: int = Field(default=0, ge=0)
    improvements_count: int = Field(default=0, ge=0)

    # Measurement flags
    complexity_measured: bool = Field(default=False)
    coverage_measured: bool = Field(default=False)
    historical_data_available: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Dimension Scores & Request
# ---------------------------------------------------------------------------

class QualityDimensionScore(BaseModel):
    """Score, weight, and explanation for an individual quality dimension."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: QualityDimension
    score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0)
    status: QualityStatus
    explanation: str
    evidence_ids: List[str] = Field(default_factory=list)
    findings: List[str] = Field(default_factory=list)
    is_measured: bool = Field(default=True)


class RepairQualityRequest(BaseModel):
    """Cryptographically bound input contract requesting quality analysis."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    request_id: str = Field(description="Unique request identifier")
    repair_plan_id: str = Field(description="Linked M3 repair plan ID")
    repair_plan_digest: str = Field(description="SHA-256 digest of repair plan")
    execution_id: str = Field(description="Linked M4 execution ID")
    applied_patch_digest: str = Field(description="SHA-256 digest of applied patch")
    baseline_tree_digest: str = Field(description="SHA-256 digest of baseline tree")
    verification_id: str = Field(description="Linked M5 verification ID")
    verification_status: str = Field(description="Reported status from M5 verification")
    workspace_id: str = Field(description="Linked repair workspace ID")
    policy_version: str = Field(default="1.0.0")
    requested_scope: List[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Final Quality Result
# ---------------------------------------------------------------------------

class RepairQualityResult(BaseModel):
    """Immutable, complete outcome produced by RepairQualityEngine."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    result_id: str = Field(description="Unique quality evaluation identifier")
    request_id: str = Field(description="Linked request identifier")
    quality_status: QualityStatus = Field(description="Overall quality status")
    overall_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    dimension_scores: Dict[QualityDimension, QualityDimensionScore] = Field(default_factory=dict)
    metrics: RepairQualityMetrics = Field(default_factory=RepairQualityMetrics)
    evidence: List[RepairQualityEvidence] = Field(default_factory=list)
    findings: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    policy_violations: List[str] = Field(default_factory=list)
    risk_level: RiskSeverity = Field(default=RiskSeverity.LOW)
    confidence: ConfidenceClassification = Field(default=ConfidenceClassification.MEDIUM)
    gate_decision: QualityGateDecision = Field(description="Quality gatekeeper decision")
    policy_version: str = Field(default="1.0.0", description="Policy version bound to this evaluation")
    requires_human_review: bool = Field(default=True, description="Strictly True. Cannot be disabled.")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("requires_human_review")
    @classmethod
    def enforce_human_review_required(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Safety invariant breach: requires_human_review must strictly be True")
        return True

    @model_validator(mode="after")
    def validate_invariants(self) -> "RepairQualityResult":
        # Invariant 1: requires_human_review is always True
        if self.requires_human_review is not True:
            raise ValueError("Safety invariant breach: requires_human_review must strictly be True")

        # Invariant 2: Cannot mark PASSED if gate_decision is not PASSED
        if self.quality_status == QualityStatus.PASSED:
            if self.gate_decision != QualityGateDecision.PASSED:
                raise ValueError(
                    f"Integrity violation: quality_status is PASSED but gate_decision is {self.gate_decision}"
                )
            if self.policy_violations:
                raise ValueError(
                    f"Integrity violation: cannot mark PASSED with policy violations: {self.policy_violations}"
                )
            if self.missing_evidence:
                raise ValueError(
                    f"Integrity violation: cannot mark PASSED with missing evidence: {self.missing_evidence}"
                )
            if self.metrics.regressions_detected_count > 0:
                raise ValueError(
                    "Integrity violation: cannot mark PASSED when regressions were detected"
                )
            if self.risk_level in (RiskSeverity.CRITICAL, RiskSeverity.HIGH):
                raise ValueError(
                    f"Integrity violation: cannot mark PASSED with {self.risk_level.value} risk level"
                )
            if self.overall_score is None:
                raise ValueError(
                    "Integrity violation: cannot mark PASSED without a valid overall score"
                )

        return self

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None, deep: bool = False) -> "RepairQualityResult":
        """Create a copy of the model, revalidating all invariants if updates are provided."""
        copied = super().model_copy(update=update, deep=deep)
        if update:
            return self.model_validate(copied.model_dump())
        return copied


# ---------------------------------------------------------------------------
# Tool Request & Response Schemas
# ---------------------------------------------------------------------------

class CreateRepairQualityRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repair_plan_id: str
    repair_plan_digest: str
    execution_id: str
    applied_patch_digest: str
    baseline_tree_digest: str
    verification_id: str
    verification_status: str
    workspace_id: str
    policy_version: str = "1.0.0"
    requested_scope: List[str] = Field(default_factory=list)


class ValidateRepairQualityEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    repair_plan: Dict[str, Any]
    execution_result: Dict[str, Any]
    verification_result: Dict[str, Any]
    diagnosis_result: Optional[Dict[str, Any]] = None


class AnalyzeRepairPatchQualityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch_content: str
    approved_files: List[str]
    repair_plan_id: Optional[str] = None


class AnalyzeRepairMaintainabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_path: str
    baseline_path: Optional[str] = None
    modified_files: List[str]


class AnalyzeRepairCompletenessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repair_plan: Dict[str, Any]
    modified_files: List[str]
    verification_result: Optional[Dict[str, Any]] = None


class EvaluateRepairCorrectnessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repair_plan: Dict[str, Any]
    verification_result: Dict[str, Any]
    modified_files: List[str]
    root_cause_locations: Optional[List[str]] = None


class AnalyzeRepairReliabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verification_result: Dict[str, Any]
    strategy_id: Optional[str] = None
    target_module: Optional[str] = None
    db_path: Optional[str] = None


class AnalyzeRepairRiskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch_content: str
    modified_files: List[str]
    metrics: Optional[Dict[str, Any]] = None


class CalculateRepairQualityScoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension_scores: Dict[str, Dict[str, Any]]
    policy: Optional[Dict[str, Any]] = None


class EvaluateRepairQualityGatesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall_score: Optional[float] = None
    dimension_scores: Dict[str, Dict[str, Any]]
    metrics: Dict[str, Any]
    policy_violations: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    risk_level: str = "low"
    verification_status: str = "passed"
    policy: Optional[Dict[str, Any]] = None


class InspectRepairQualityHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: Optional[str] = None
    target_file: Optional[str] = None
    limit: int = 50
    db_path: Optional[str] = None


class GetRepairQualitySummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str
    db_path: Optional[str] = None
