"""FORGE Phase 12 M2: Root-Cause Analysis Package.

Provides production-grade causal reconstruction from verified evidence,
execution failures, cross-file call graphs, module dependencies, and
AST contradiction analysis.
"""

from forge.causality.schemas import (
    AlternativeExplanation,
    AnalyzeRootCauseInput,
    AnalyzeRootCauseOutput,
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    CompareRootCauseHypothesesInput,
    CompareRootCauseHypothesesOutput,
    ExplainFailurePropagationInput,
    ExplainFailurePropagationOutput,
    InspectContributingFactorsInput,
    InspectContributingFactorsOutput,
    RootCauseReport,
    TraceCausalChainInput,
    TraceCausalChainOutput,
)
from forge.causality.contradiction import ASTContradictionInspector
from forge.causality.tracer import CausalTracer
from forge.causality.engine import RootCauseAnalysisEngine
from forge.causality.tools import CausalityTools

__all__ = [
    "AlternativeExplanation",
    "AnalyzeRootCauseInput",
    "AnalyzeRootCauseOutput",
    "ASTContradictionInspector",
    "CausalChain",
    "CausalEdge",
    "CausalNode",
    "CausalNodeType",
    "CausalRelationship",
    "CausalRole",
    "CausalStatus",
    "CausalTracer",
    "CausalityTools",
    "CompareRootCauseHypothesesInput",
    "CompareRootCauseHypothesesOutput",
    "ExplainFailurePropagationInput",
    "ExplainFailurePropagationOutput",
    "InspectContributingFactorsInput",
    "InspectContributingFactorsOutput",
    "RootCauseAnalysisEngine",
    "RootCauseReport",
    "TraceCausalChainInput",
    "TraceCausalChainOutput",
]
