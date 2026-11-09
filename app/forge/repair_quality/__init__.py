"""Phase 12 M6: Repair Quality & Reliability Engine Package.

Provides comprehensive, evidence-driven, deterministic evaluation of
autonomous repair quality:
- Multi-dimensional quality analysis (correctness, completeness, maintainability, patch quality, reliability, security risk).
- Cryptographic evidence validation linking M2 diagnosis, M3 planning, M4 execution, and M5 verification.
- Fail-closed quality gate state machine.
- Bounded SQLite historical reliability tracking.
- ToolRegistry integration with 12 typed quality tools.
"""

from forge.repair_quality.schemas import (
    ConfidenceClassification,
    EvidenceCategory,
    EvidenceValidationStatus,
    QualityDimension,
    QualityDimensionScore,
    QualityGateDecision,
    QualityStatus,
    RepairQualityEvent,
    RepairQualityEventType,
    RepairQualityEvidence,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityRequest,
    RepairQualityResult,
    RiskSeverity,
)
from forge.repair_quality.evidence import RepairQualityEvidenceValidator
from forge.repair_quality.patch_analysis import RepairPatchAnalyzer
from forge.repair_quality.maintainability import RepairMaintainabilityAnalyzer
from forge.repair_quality.completeness import RepairCompletenessAnalyzer
from forge.repair_quality.correctness import RepairCorrectnessEvaluator
from forge.repair_quality.reliability import RepairReliabilityAnalyzer
from forge.repair_quality.risk import RepairRiskAnalyzer
from forge.repair_quality.scoring import RepairQualityScorer
from forge.repair_quality.gates import RepairQualityGateKeeper
from forge.repair_quality.history import HistoricalReliabilityStore
from forge.repair_quality.events import RepairQualityEventRecorder
from forge.repair_quality.executor import RepairQualityExecutor
from forge.repair_quality.tools import RepairQualityTools

__all__ = [
    # Schemas & Enums
    "ConfidenceClassification",
    "EvidenceCategory",
    "EvidenceValidationStatus",
    "QualityDimension",
    "QualityDimensionScore",
    "QualityGateDecision",
    "QualityStatus",
    "RepairQualityEvent",
    "RepairQualityEventType",
    "RepairQualityEvidence",
    "RepairQualityMetrics",
    "RepairQualityPolicy",
    "RepairQualityRequest",
    "RepairQualityResult",
    "RiskSeverity",
    # Engines & Analyzers
    "RepairQualityEvidenceValidator",
    "RepairPatchAnalyzer",
    "RepairMaintainabilityAnalyzer",
    "RepairCompletenessAnalyzer",
    "RepairCorrectnessEvaluator",
    "RepairReliabilityAnalyzer",
    "RepairRiskAnalyzer",
    "RepairQualityScorer",
    "RepairQualityGateKeeper",
    "HistoricalReliabilityStore",
    "RepairQualityEventRecorder",
    "RepairQualityExecutor",
    "RepairQualityTools",
]
