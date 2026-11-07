"""Phase 12 M2: Root-Cause Analysis Data Contracts and Schemas.

Defines strict input, graph, and reporting contracts for causal defect analysis:
- Graph node, edge, and chain representation with evidence grounding.
- Semantic causal relationships (call, dependency, invalid data, state, exception).
- Provenance and certainty classifications (directly verified vs inferred).
- Comprehensive RootCauseReport and alternative explanation models.
- Tool input and output contracts for LLM and agent interaction.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.schemas import FindingConfidence, FindingSeverity
from forge.diagnosis.schemas import CandidateLocation, DefectCategory, DiagnosisResult, TestFailureReport


# ---------------------------------------------------------------------------
# Maximum Input Size Boundaries
# ---------------------------------------------------------------------------

MAX_CHAIN_DEPTH = 10
MAX_NODES_PER_REPORT = 200
MAX_EDGES_PER_REPORT = 300
MAX_EXPLANATION_CHARS = 5_000
MAX_LIMITATIONS_CHARS = 5_000


# ---------------------------------------------------------------------------
# Causal Enums
# ---------------------------------------------------------------------------

class CausalRelationship(str, Enum):
    """Semantic relationship between causal graph nodes."""
    CALLS = "calls"
    DEPENDS_ON = "depends_on"
    PASSES_INVALID_DATA = "passes_invalid_data"
    VIOLATES_PRECONDITION = "violates_precondition"
    MISUSES_RESOURCE = "misuses_resource"
    PROPAGATES_EXCEPTION = "propagates_exception"
    INVALIDATES_STATE = "invalidates_state"
    TRIGGERS_FAILURE = "triggers_failure"
    CONTRIBUTES_TO = "contributes_to"
    UNKNOWN = "unknown"


class CausalRole(str, Enum):
    """Functional role of a node within the causal graph or chain."""
    ROOT_CAUSE = "root_cause"
    CAUSAL_MECHANISM = "causal_mechanism"
    CONTRIBUTING_FACTOR = "contributing_factor"
    PROPAGATOR = "propagator"
    SYMPTOM = "symptom"


class CausalNodeType(str, Enum):
    """Syntactic or architectural classification of a causal node."""
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    MODULE = "module"
    STATEMENT = "statement"
    EXPRESSION = "expression"
    TEST = "test"
    EXTERNAL = "external"


class CausalStatus(str, Enum):
    """Overall status of root-cause reconstruction."""
    PROVEN = "proven"                                       # All links in primary chain directly verified with direct disk evidence
    PLAUSIBLE = "plausible"                                 # Plausible chain with grounded evidence but containing inferred links
    MULTIPLE_PLAUSIBLE_ROOT_CAUSES = "multiple_plausible_root_causes" # Competing plausible explanations without differentiating evidence
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"         # Key evidence missing, stale, or unreachable
    UNRESOLVED = "unresolved"                               # Contradictory evidence or unresolvable ambiguity


# ---------------------------------------------------------------------------
# Causal Graph Primitives
# ---------------------------------------------------------------------------

class CausalNode(BaseModel):
    """A node in the causal graph representing an entity, state, or event."""
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(description="Unique identifier for the node (e.g. 'cnode_func_xyz').")
    symbol_id: Optional[str] = Field(default=None, description="Symbol identifier if linked to a code symbol.")
    file_path: str = Field(description="Normalized repository-relative file path.")
    start_line: int = Field(ge=1, description="1-indexed starting line number.")
    end_line: int = Field(ge=1, description="1-indexed ending line number.")
    node_type: CausalNodeType = Field(description="Syntactic or architectural type.")
    role: CausalRole = Field(description="Role in the causal chain.")
    description: str = Field(description="Human-readable description of what this node represents.")
    supporting_evidence_ids: List[str] = Field(default_factory=list, description="IDs of supporting evidence records.")
    uncertainty: Optional[str] = Field(default=None, description="Explicit description of any uncertainty or assumptions.")
    is_verified_on_disk: bool = Field(default=False, description="True if verified against current disk content.")

    @model_validator(mode="after")
    def validate_line_bounds(self) -> "CausalNode":
        if self.end_line < self.start_line:
            raise ValueError(f"end_line ({self.end_line}) cannot be less than start_line ({self.start_line})")
        return self


class CausalEdge(BaseModel):
    """A directed edge representing a causal or propagation relationship."""
    model_config = ConfigDict(extra="forbid")

    edge_id: str = Field(description="Unique identifier for the edge (e.g. 'cedge_abc_to_def').")
    source_node_id: str = Field(description="Identifier of the upstream cause or trigger node.")
    target_node_id: str = Field(description="Identifier of the downstream effect or recipient node.")
    relationship: CausalRelationship = Field(description="Semantic type of relationship.")
    evidence_references: List[str] = Field(default_factory=list, description="Evidence IDs directly supporting this edge.")
    causal_explanation: str = Field(description="Explanation of how the source causes or propagates to the target.")
    confidence: FindingConfidence = Field(description="Confidence classification.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Deterministic heuristic confidence score.")
    is_directly_verified: bool = Field(default=False, description="True if proven by exact disk/graph facts, False if inferred.")


class CausalChain(BaseModel):
    """An ordered path of causal nodes and edges explaining a defect progression."""
    model_config = ConfigDict(extra="forbid")

    chain_id: str = Field(description="Unique identifier for the causal chain.")
    root_node_id: str = Field(description="ID of origin root-cause node.")
    symptom_node_id: str = Field(description="ID of failure symptom node.")
    node_ids: List[str] = Field(description="Ordered list of node IDs from root cause to symptom.")
    edge_ids: List[str] = Field(description="Ordered list of edge IDs connecting node_ids.")
    summary: str = Field(description="Summary narrative explaining the failure chain.")
    confidence: FindingConfidence = Field(description="Overall chain confidence.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Deterministic chain confidence score.")
    is_fully_verified: bool = Field(default=False, description="True if every edge in the chain is directly verified.")

    @model_validator(mode="after")
    def validate_chain_consistency(self) -> "CausalChain":
        if not self.node_ids:
            raise ValueError("CausalChain node_ids cannot be empty")
        if len(self.node_ids) < 2:
            raise ValueError("CausalChain requires at least two nodes (root and symptom)")
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError(f"edge_ids count ({len(self.edge_ids)}) must equal node_ids count minus 1 ({len(self.node_ids) - 1})")
        if self.node_ids[0] != self.root_node_id:
            raise ValueError("First node in node_ids must match root_node_id")
        if self.node_ids[-1] != self.symptom_node_id:
            raise ValueError("Last node in node_ids must match symptom_node_id")
        return self


class AlternativeExplanation(BaseModel):
    """A competing or alternative explanation for the defect."""
    model_config = ConfigDict(extra="forbid")

    explanation_id: str = Field(description="Unique identifier.")
    suspected_root_cause: str = Field(description="Alternative candidate cause.")
    rationale: str = Field(description="Why this alternative was considered.")
    confidence: FindingConfidence = Field(description="Confidence rating for this alternative.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Deterministic score.")
    contradicted_by: List[str] = Field(default_factory=list, description="Evidence IDs or reasons refuting this alternative.")
    is_refuted: bool = Field(default=False, description="True if evidence affirmatively refutes this alternative.")


# ---------------------------------------------------------------------------
# Root Cause Report
# ---------------------------------------------------------------------------

class RootCauseReport(BaseModel):
    """Comprehensive structured outcome produced by RootCauseAnalysisEngine."""
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(description="Unique deterministic report identifier.")
    diagnosis_id: Optional[str] = Field(default=None, description="Linked DiagnosisResult ID if available.")
    run_id: str = Field(description="FORGE run identifier.")
    status: CausalStatus = Field(description="Overall status of root-cause analysis.")
    primary_root_cause: Optional[CausalNode] = Field(default=None, description="Primary isolated root-cause node.")
    contributing_factors: List[CausalNode] = Field(default_factory=list, description="Secondary or contributing code conditions.")
    symptom_locations: List[CausalNode] = Field(default_factory=list, description="Immediate symptom / crash locations.")
    nodes: List[CausalNode] = Field(default_factory=list, description="All nodes in reconstructed causal subgraph.")
    edges: List[CausalEdge] = Field(default_factory=list, description="All edges in reconstructed causal subgraph.")
    causal_chains: List[CausalChain] = Field(default_factory=list, description="Synthesized causal progression paths.")
    evidence_references: List[str] = Field(default_factory=list, description="All grounded evidence IDs supporting the report.")
    alternative_explanations: List[AlternativeExplanation] = Field(default_factory=list, description="Alternative hypotheses evaluated.")
    unresolved_questions: List[str] = Field(default_factory=list, description="Uncertainties or unverified assumptions.")
    confidence: FindingConfidence = Field(description="Report confidence level.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Deterministic heuristic confidence score.")
    limitations: Optional[str] = Field(default=None, description="Explicit boundaries and missing context.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Execution and profiling metadata.")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="UTC ISO timestamp.")

    @field_validator("limitations")
    @classmethod
    def check_limitations_budget(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > MAX_LIMITATIONS_CHARS:
            return v[:MAX_LIMITATIONS_CHARS] + " [TRUNCATED]"
        return v


# ---------------------------------------------------------------------------
# Tool Request / Response Schemas
# ---------------------------------------------------------------------------

class AnalyzeRootCauseInput(BaseModel):
    """Input parameters for analyze_root_cause tool."""
    model_config = ConfigDict(extra="forbid")

    diagnosis_id: Optional[str] = Field(default=None, description="Optional diagnosis ID to analyze.")
    failing_symbol: Optional[str] = Field(default=None, description="Qualified symbol where failure was observed.")
    file_path: Optional[str] = Field(default=None, description="File path of observed failure.")
    line_number: Optional[int] = Field(default=None, ge=1, description="Line number of observed failure.")
    error_message: Optional[str] = Field(default=None, description="Failure or error message.")
    max_depth: int = Field(default=4, ge=1, le=8, description="Maximum backward traversal depth.")


class AnalyzeRootCauseOutput(BaseModel):
    """Output from analyze_root_cause tool."""
    model_config = ConfigDict(extra="forbid")

    status: CausalStatus = Field(description="Analysis outcome status.")
    primary_root_cause_symbol: Optional[str] = Field(default=None, description="Identified root cause symbol.")
    primary_root_cause_file: Optional[str] = Field(default=None, description="Root cause file path.")
    primary_root_cause_lines: Optional[str] = Field(default=None, description="Root cause line span.")
    chain_summary: str = Field(description="Summary of identified causal chain.")
    confidence: FindingConfidence = Field(description="Confidence rating.")
    confidence_score: float = Field(description="Deterministic confidence score.")
    is_directly_verified: bool = Field(description="True if proven by direct evidence.")
    contributing_factors_count: int = Field(default=0, description="Number of contributing factors found.")
    evidence_ids: List[str] = Field(default_factory=list, description="Associated evidence IDs.")
    report_id: str = Field(description="Full RootCauseReport ID.")


class TraceCausalChainInput(BaseModel):
    """Input for trace_causal_chain tool."""
    model_config = ConfigDict(extra="forbid")

    source_symbol: str = Field(description="Originating suspected symbol or caller.")
    target_symbol: str = Field(description="Target failing symbol or callee.")
    max_depth: int = Field(default=5, ge=1, le=8, description="Maximum traversal search depth.")


class TraceCausalChainOutput(BaseModel):
    """Output from trace_causal_chain tool."""
    model_config = ConfigDict(extra="forbid")

    path_found: bool = Field(description="True if a causal path connects source to target.")
    path_nodes: List[str] = Field(default_factory=list, description="Ordered symbols from source to target.")
    edge_relationships: List[CausalRelationship] = Field(default_factory=list, description="Relationships along path.")
    all_directly_verified: bool = Field(default=False, description="True if every step is directly verified.")
    explanation: str = Field(description="Narrative explanation of chain.")


class InspectContributingFactorsInput(BaseModel):
    """Input for inspect_contributing_factors tool."""
    model_config = ConfigDict(extra="forbid")

    symbol_id: str = Field(description="Symbol identifier to inspect for contributing factors.")
    include_dependencies: bool = Field(default=True, description="Whether to include module dependencies.")


class InspectContributingFactorsOutput(BaseModel):
    """Output from inspect_contributing_factors tool."""
    model_config = ConfigDict(extra="forbid")

    symbol_id: str = Field(description="Analyzed symbol.")
    contributing_symbols: List[str] = Field(default_factory=list, description="Symbols contributing state or inputs.")
    recent_changes: List[str] = Field(default_factory=list, description="Recently changed files impacting this symbol.")
    cycle_detected: bool = Field(default=False, description="True if involved in dependency cycle.")
    summary: str = Field(description="Summary of contributing factors.")


class CompareRootCauseHypothesesInput(BaseModel):
    """Input for compare_root_cause_hypotheses tool."""
    model_config = ConfigDict(extra="forbid")

    hypotheses: List[str] = Field(description="List of candidate hypothesis descriptions.")
    context_symbol: str = Field(description="Target symbol under analysis.")


class CompareRootCauseHypothesesOutput(BaseModel):
    """Output from compare_root_cause_hypotheses tool."""
    model_config = ConfigDict(extra="forbid")

    preferred_hypothesis_index: Optional[int] = Field(default=None, description="Index of best-supported hypothesis.")
    rationale: str = Field(description="Comparison rationale.")
    unresolved_ambiguity: bool = Field(default=False, description="True if multiple hypotheses remain equally plausible.")


class ExplainFailurePropagationInput(BaseModel):
    """Input for explain_failure_propagation tool."""
    model_config = ConfigDict(extra="forbid")

    root_symbol: str = Field(description="Suspected root cause symbol.")
    failure_symptom: str = Field(description="Observed symptom or error.")


class ExplainFailurePropagationOutput(BaseModel):
    """Output from explain_failure_propagation tool."""
    model_config = ConfigDict(extra="forbid")

    propagation_steps: List[str] = Field(default_factory=list, description="Step-by-step propagation narrative.")
    impacted_symbols: List[str] = Field(default_factory=list, description="Downstream symbols affected.")
    confidence: FindingConfidence = Field(description="Confidence rating.")
    confidence_score: float = Field(description="Deterministic score.")
