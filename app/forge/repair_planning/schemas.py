"""Phase 12 M3: Repair Planning Engine Data Contracts and Schemas.

Defines strict input, strategy, risk, verification, rollback, and reporting
contracts for autonomous defect repair planning:
- Structured candidate repair strategies with complexity, invasiveness, and reversibility metrics.
- Comprehensive risk assessment across dependency reach, API stability, security, and data integrity.
- Verification and rollback contracts distinguishing existing tests from proposed tests.
- Mandatory safety invariants: approval_required=True, execution_permitted=False.
- Tool input and output contracts for LLM and agent interaction.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.schemas import FindingConfidence, FindingSeverity


# ---------------------------------------------------------------------------
# Maximum Input Size Boundaries
# ---------------------------------------------------------------------------

MAX_SUMMARY_CHARS = 5_000
MAX_DESCRIPTION_CHARS = 10_000
MAX_LIMITATIONS_CHARS = 5_000
MAX_STRATEGIES_PER_PLAN = 20
MAX_AFFECTED_FILES = 100
MAX_AFFECTED_SYMBOLS = 200


# ---------------------------------------------------------------------------
# Repair Planning Enums
# ---------------------------------------------------------------------------

class RepairApproach(str, Enum):
    """Deterministic taxonomy of repair strategies."""
    LOCAL_LOGIC_CORRECTION = "local_logic_correction"
    INPUT_VALIDATION = "input_validation"
    ERROR_HANDLING = "error_handling"
    RESOURCE_MANAGEMENT = "resource_management"
    DEPENDENCY_INTEGRATION = "dependency_integration"
    CONCURRENCY_CORRECTION = "concurrency_correction"
    SECURITY_HARDENING = "security_hardening"
    CONFIGURATION_CORRECTION = "configuration_correction"
    TEST_COVERAGE_IMPROVEMENT = "test_coverage_improvement"
    UNKNOWN_OR_MANUAL_INVESTIGATION = "unknown_or_manual_investigation"


class StrategyComplexity(str, Enum):
    """Estimated implementation complexity of a repair strategy."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StrategyInvasiveness(str, Enum):
    """Extent of structural and syntactic changes proposed by a strategy."""
    MINIMAL = "minimal"        # Localized change within a single function/block
    MODERATE = "moderate"      # Multiple functions, methods, or internal interfaces
    EXTENSIVE = "extensive"    # Broad refactoring, multi-file architectural changes


class StrategyReversibility(str, Enum):
    """Ease with which a proposed repair can be rolled back safely."""
    HIGH = "high"              # Clean revert without external state or schema impacts
    MODERATE = "moderate"      # May require restoring configuration or cache invalidation
    LOW = "low"                # Complex migrations or potential data synchronization issues


class RiskSeverity(str, Enum):
    """Severity rating for repair side-effect risk."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskLikelihood(str, Enum):
    """Estimated probability of side effects or unintended regressions."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RepairPlanStatus(str, Enum):
    """Actionability and certainty status of a generated repair plan."""
    ACTIONABLE = "actionable"                            # High-confidence plan grounded in proven root cause
    CONDITIONAL = "conditional"                          # Plausible plan requiring explicit assumptions or conditions
    MULTIPLE_CANDIDATES = "multiple_candidates"          # Preserves competing candidate repair plans for ambiguous roots
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"      # Evidence missing, stale, or unreachable; non-actionable
    UNRESOLVED = "unresolved"                            # Contradictory evidence or unresolved defect; non-actionable


# ---------------------------------------------------------------------------
# Structured Change & Evidence Models
# ---------------------------------------------------------------------------

class RequiredChange(BaseModel):
    """An exact, localized change unit required by a repair strategy."""
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="Normalized repository-relative file path.")
    symbol_id: Optional[str] = Field(default=None, description="Optional qualified symbol identifier.")
    start_line: Optional[int] = Field(default=None, ge=1, description="1-indexed starting line number.")
    end_line: Optional[int] = Field(default=None, ge=1, description="1-indexed ending line number.")
    change_type: str = Field(description="Category of change (e.g. 'modify_condition', 'add_guard', 'handle_exception').")
    description: str = Field(description="Human-readable description of what code modification is required.")
    rationale: str = Field(description="Explanation of why this change addresses the verified root cause.")

    @model_validator(mode="after")
    def validate_line_bounds(self) -> "RequiredChange":
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError(f"end_line ({self.end_line}) cannot be less than start_line ({self.start_line})")
        return self


class RepairEvidence(BaseModel):
    """An audit-grounded evidence record supporting a repair plan."""
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(description="Unique identifier for the evidence record.")
    evidence_source: str = Field(description="Source origin (e.g. 'root_cause_analysis', 'diagnosis', 'disk_inspection').")
    file_path: str = Field(description="Normalized repository-relative file path.")
    symbol_reference: Optional[str] = Field(default=None, description="Optional symbol identifier.")
    start_line: Optional[int] = Field(default=None, ge=1, description="1-indexed start line.")
    end_line: Optional[int] = Field(default=None, ge=1, description="1-indexed end line.")
    evidence_type: str = Field(description="Type of evidence (e.g. 'source_excerpt', 'call_edge', 'stack_trace').")
    source_hash: Optional[str] = Field(default=None, description="SHA-256 digest of source excerpt when available.")
    is_fresh: bool = Field(default=True, description="True if verified against current workspace disk state.")
    relationship_to_defect: str = Field(description="Explanation of how this evidence supports the repair.")

    @model_validator(mode="after")
    def validate_line_bounds(self) -> "RepairEvidence":
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError(f"end_line ({self.end_line}) cannot be less than start_line ({self.start_line})")
        return self


# ---------------------------------------------------------------------------
# Candidate Strategy & Risk Models
# ---------------------------------------------------------------------------

class RepairStrategy(BaseModel):
    """A concrete candidate repair strategy addressing a diagnosed defect."""
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(description="Unique deterministic strategy identifier (e.g. 'strat_logic_01').")
    title: str = Field(description="Concise summary title of the strategy.")
    description: str = Field(description="Detailed narrative describing the proposed approach.")
    approach: RepairApproach = Field(description="Taxonomy classification of the repair approach.")
    affected_files: List[str] = Field(default_factory=list, description="List of repository-relative file paths.")
    affected_symbols: List[str] = Field(default_factory=list, description="List of affected qualified symbols.")
    supporting_evidence: List[str] = Field(default_factory=list, description="IDs of supporting evidence records.")
    expected_benefits: List[str] = Field(default_factory=list, description="Observed or expected positive outcomes.")
    risks: List[str] = Field(default_factory=list, description="Specific risks associated with this strategy.")
    complexity: StrategyComplexity = Field(description="Estimated complexity.")
    invasiveness: StrategyInvasiveness = Field(description="Estimated invasiveness.")
    reversibility: StrategyReversibility = Field(description="Estimated reversibility.")
    test_requirements: List[str] = Field(default_factory=list, description="Tests required to validate this strategy.")
    rejection_reasons: List[str] = Field(default_factory=list, description="Reasons why this strategy was rejected or disfavored.")
    is_recommended: bool = Field(default=False, description="True if selected as the primary recommended strategy.")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="Deterministic multi-criteria ranking score.")


class RepairRiskAssessment(BaseModel):
    """Comprehensive risk analysis for a proposed repair."""
    model_config = ConfigDict(extra="forbid")

    overall_risk: RiskSeverity = Field(description="Aggregated risk severity rating.")
    likelihood: RiskLikelihood = Field(description="Probability of unintended side effects.")
    risk_score: float = Field(ge=0.0, le=1.0, description="Deterministic heuristic risk score in [0.0, 1.0].")
    affected_components: List[str] = Field(default_factory=list, description="Modules, packages, or services impacted.")
    potential_regressions: List[str] = Field(default_factory=list, description="Potential regressions or breaking changes.")
    security_implications: List[str] = Field(default_factory=list, description="Security boundaries, auth, or input risks.")
    data_integrity_implications: List[str] = Field(default_factory=list, description="State, persistence, or data integrity risks.")
    dependency_implications: List[str] = Field(default_factory=list, description="External or internal dependency consequences.")
    explanation: str = Field(description="Detailed narrative explaining the risk evaluation.")
    supporting_evidence: List[str] = Field(default_factory=list, description="Evidence IDs or call graph references.")


# ---------------------------------------------------------------------------
# Verification & Rollback Models
# ---------------------------------------------------------------------------

class VerificationPlan(BaseModel):
    """Specification of verification tests and checks to validate a repair."""
    model_config = ConfigDict(extra="forbid")

    existing_targeted_tests: List[str] = Field(
        default_factory=list,
        description="Targeted test files or test functions verified to exist on disk.",
    )
    recommended_new_tests: List[str] = Field(
        default_factory=list,
        description="Explicitly proposed new test cases (not yet existing in the repository).",
    )
    regression_test_scope: List[str] = Field(
        default_factory=list,
        description="Broader regression test suites or packages to execute.",
    )
    security_checks: List[str] = Field(
        default_factory=list,
        description="Static security or boundary inspection checks (e.g. bandit, sanitization).",
    )
    static_analysis_requirements: List[str] = Field(
        default_factory=list,
        description="Linters and type checks required (e.g. mypy, flake8).",
    )
    expected_verification_criteria: List[str] = Field(
        default_factory=list,
        description="Concrete criteria that must be satisfied for the repair to be verified.",
    )


class RollbackPlan(BaseModel):
    """Concrete instructions and limitations for reversing a repair."""
    model_config = ConfigDict(extra="forbid")

    changes_to_reverse: List[str] = Field(
        default_factory=list,
        description="Precise list of modifications to undo if the repair fails.",
    )
    files_and_symbols_involved: List[str] = Field(
        default_factory=list,
        description="All files and symbols that would be reverted.",
    )
    dependencies_to_restore: List[str] = Field(
        default_factory=list,
        description="Dependencies or package pins to restore if altered.",
    )
    data_configuration_considerations: List[str] = Field(
        default_factory=list,
        description="Cache invalidations, configs, or stored state to reset.",
    )
    rollback_triggers: List[str] = Field(
        default_factory=list,
        description="Conditions or test failures that mandate an immediate rollback.",
    )
    known_limitations: List[str] = Field(
        default_factory=list,
        description="Acknowledged limitations where automated rollback cannot guarantee original state.",
    )


# ---------------------------------------------------------------------------
# Comprehensive Repair Plan Model
# ---------------------------------------------------------------------------

class RepairPlan(BaseModel):
    """Complete structured repair plan generated by RepairPlanningEngine."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Unique deterministic plan identifier.")
    finding_id: Optional[str] = Field(default=None, description="Linked Finding identifier if available.")
    root_cause_reference: Optional[str] = Field(default=None, description="Linked RootCauseReport or root node ID.")
    status: RepairPlanStatus = Field(description="Actionability status of this plan.")
    summary: str = Field(description="High-level narrative summary of the proposed repair.")
    problem_statement: str = Field(description="Precise statement of the defect being addressed.")
    repair_objective: str = Field(description="Observable condition defining successful resolution.")
    selected_strategy_id: Optional[str] = Field(default=None, description="ID of recommended strategy.")
    candidate_strategies: List[RepairStrategy] = Field(default_factory=list, description="Evaluated candidate strategies.")
    affected_files: List[str] = Field(default_factory=list, description="All files required to be modified.")
    affected_symbols: List[str] = Field(default_factory=list, description="All symbols directly modified or impacted.")
    required_changes: List[RequiredChange] = Field(default_factory=list, description="Exact localized changes.")
    preconditions: List[str] = Field(default_factory=list, description="Conditions that must hold before repair.")
    dependencies: List[str] = Field(default_factory=list, description="Required dependencies, libraries, or modules.")
    risk_assessment: RepairRiskAssessment = Field(description="Comprehensive risk assessment.")
    expected_side_effects: List[str] = Field(default_factory=list, description="Expected secondary effects.")
    verification_plan: VerificationPlan = Field(description="Verification test plan.")
    rollback_plan: RollbackPlan = Field(description="Rollback procedure and triggers.")
    supporting_evidence: List[RepairEvidence] = Field(default_factory=list, description="Grounded supporting evidence.")
    confidence: FindingConfidence = Field(description="Overall confidence in this repair plan.")
    limitations: Optional[str] = Field(default=None, description="Explicit boundaries and uncertainties.")
    approval_required: bool = Field(default=True, description="Human approval gate (must always be True in M3).")
    execution_permitted: bool = Field(default=False, description="Execution permission (must always be False in M3).")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="UTC ISO timestamp.")

    @field_validator("summary")
    @classmethod
    def check_summary_budget(cls, v: str) -> str:
        if len(v) > MAX_SUMMARY_CHARS:
            return v[:MAX_SUMMARY_CHARS] + " [TRUNCATED]"
        return v

    @field_validator("limitations")
    @classmethod
    def check_limitations_budget(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_LIMITATIONS_CHARS:
            return v[:MAX_LIMITATIONS_CHARS] + " [TRUNCATED]"
        return v

    @model_validator(mode="after")
    def validate_safety_invariants(self) -> "RepairPlan":
        """Enforce non-negotiable safety and execution boundaries in code."""
        if not self.approval_required:
            raise ValueError("M3 safety invariant violation: approval_required must always be True.")
        if self.execution_permitted:
            raise ValueError("M3 safety invariant violation: execution_permitted must always be False in Phase 12 M3.")
        return self


# ---------------------------------------------------------------------------
# Tool Request / Response Schemas
# ---------------------------------------------------------------------------

class CreateRepairPlanInput(BaseModel):
    """Input parameters for create_repair_plan tool."""
    model_config = ConfigDict(extra="forbid")

    root_cause_report_id: Optional[str] = Field(default=None, description="Linked RootCauseReport ID if available.")
    diagnosis_id: Optional[str] = Field(default=None, description="Linked DiagnosisResult ID if available.")
    failing_symbol: Optional[str] = Field(default=None, description="Suspected or failing symbol name.")
    file_path: Optional[str] = Field(default=None, description="Suspected file path.")
    line_number: Optional[int] = Field(default=None, ge=1, description="Line number of failure or defect.")
    error_message: Optional[str] = Field(default=None, description="Observed failure or error message.")


class CreateRepairPlanOutput(BaseModel):
    """Output from create_repair_plan tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Deterministic repair plan identifier.")
    status: RepairPlanStatus = Field(description="Plan status (actionable, conditional, etc.).")
    selected_strategy_id: Optional[str] = Field(default=None, description="Selected strategy ID.")
    selected_strategy_title: Optional[str] = Field(default=None, description="Selected strategy title.")
    affected_files: List[str] = Field(default_factory=list, description="Target files for the repair.")
    affected_symbols: List[str] = Field(default_factory=list, description="Target symbols for the repair.")
    risk_level: RiskSeverity = Field(description="Overall risk severity.")
    confidence: FindingConfidence = Field(description="Confidence rating.")
    execution_permitted: bool = Field(default=False, description="Strictly False for M3.")
    approval_required: bool = Field(default=True, description="Strictly True for M3.")
    summary: str = Field(description="Summary narrative of the repair plan.")
    candidate_strategy_count: int = Field(default=0, description="Total candidate strategies evaluated.")


class CompareRepairStrategiesInput(BaseModel):
    """Input parameters for compare_repair_strategies tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: Optional[str] = Field(default=None, description="Plan ID to compare strategies from.")
    file_path: Optional[str] = Field(default=None, description="Optional target file path.")
    failing_symbol: Optional[str] = Field(default=None, description="Optional failing symbol.")


class CompareRepairStrategiesOutput(BaseModel):
    """Output from compare_repair_strategies tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: Optional[str] = Field(default=None, description="Plan ID evaluated.")
    strategies: List[Dict[str, Any]] = Field(default_factory=list, description="Evaluated candidate strategies with trade-offs.")
    recommended_strategy_id: Optional[str] = Field(default=None, description="ID of recommended strategy.")
    tradeoff_analysis: str = Field(description="Comparative trade-off explanation.")


class AssessRepairRiskInput(BaseModel):
    """Input parameters for assess_repair_risk tool."""
    model_config = ConfigDict(extra="forbid")

    affected_files: List[str] = Field(description="Files proposed to be modified.")
    affected_symbols: List[str] = Field(default_factory=list, description="Symbols proposed to be modified.")
    proposed_approach: Optional[RepairApproach] = Field(default=None, description="Proposed repair approach.")


class AssessRepairRiskOutput(BaseModel):
    """Output from assess_repair_risk tool."""
    model_config = ConfigDict(extra="forbid")

    overall_risk: RiskSeverity = Field(description="Overall risk rating.")
    likelihood: RiskLikelihood = Field(description="Likelihood of regressions.")
    risk_score: float = Field(description="Heuristic risk score in [0.0, 1.0].")
    affected_components: List[str] = Field(default_factory=list, description="Identified affected components.")
    potential_regressions: List[str] = Field(default_factory=list, description="Identified potential regressions.")
    security_implications: List[str] = Field(default_factory=list, description="Security boundaries touched.")
    data_integrity_implications: List[str] = Field(default_factory=list, description="Data integrity implications.")
    dependency_implications: List[str] = Field(default_factory=list, description="Dependency consequences.")
    explanation: str = Field(description="Detailed narrative explaining the risk score.")


class ExplainRepairPlanInput(BaseModel):
    """Input parameters for explain_repair_plan tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Identifier of the repair plan to explain.")


class ExplainRepairPlanOutput(BaseModel):
    """Output from explain_repair_plan tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Plan ID.")
    status: RepairPlanStatus = Field(description="Plan actionability status.")
    summary: str = Field(description="Executive summary.")
    problem_statement: str = Field(description="Problem statement.")
    repair_objective: str = Field(description="Repair objective.")
    selected_strategy: Optional[Dict[str, Any]] = Field(default=None, description="Selected strategy details.")
    risk_explanation: str = Field(description="Summary of risks.")
    verification_summary: str = Field(description="Verification overview.")
    rollback_summary: str = Field(description="Rollback overview.")
    approval_required: bool = Field(default=True, description="Strictly True.")
    execution_permitted: bool = Field(default=False, description="Strictly False.")


class ValidateRepairPlanInput(BaseModel):
    """Input parameters for validate_repair_plan tool."""
    model_config = ConfigDict(extra="forbid")

    plan_id: Optional[str] = Field(default=None, description="Cached plan ID to validate.")
    plan_json: Optional[str] = Field(default=None, description="Serialized RepairPlan JSON to validate.")


class ValidateRepairPlanOutput(BaseModel):
    """Output from validate_repair_plan tool."""
    model_config = ConfigDict(extra="forbid")

    is_valid: bool = Field(description="True if plan satisfies all M3 safety and certainty invariants.")
    validation_errors: List[str] = Field(default_factory=list, description="List of invariant violations found.")
    checked_invariants: List[str] = Field(default_factory=list, description="All invariants verified.")
    execution_permitted_guarantee: bool = Field(default=False, description="Guarantee that execution is blocked.")
    approval_required_guarantee: bool = Field(default=True, description="Guarantee that approval is mandated.")


class GenerateVerificationPlanInput(BaseModel):
    """Input parameters for generate_verification_plan tool."""
    model_config = ConfigDict(extra="forbid")

    affected_files: List[str] = Field(description="Files targeted for modification.")
    affected_symbols: List[str] = Field(default_factory=list, description="Symbols targeted for modification.")


class GenerateVerificationPlanOutput(BaseModel):
    """Output from generate_verification_plan tool."""
    model_config = ConfigDict(extra="forbid")

    existing_targeted_tests: List[str] = Field(default_factory=list, description="Tests verified on disk.")
    recommended_new_tests: List[str] = Field(default_factory=list, description="Explicitly proposed new tests.")
    regression_test_scope: List[str] = Field(default_factory=list, description="Broader regression test scope.")
    security_checks: List[str] = Field(default_factory=list, description="Security check recommendations.")
    static_analysis_requirements: List[str] = Field(default_factory=list, description="Static analysis requirements.")
    expected_verification_criteria: List[str] = Field(default_factory=list, description="Expected criteria.")
