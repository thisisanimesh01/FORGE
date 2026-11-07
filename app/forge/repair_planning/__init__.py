"""FORGE Phase 12 M3: Repair Planning Package.

Transforms verified defect diagnoses and causal explanations into structured,
minimal, risk-aware repair plans without modifying repository source files.
"""

from forge.repair_planning.schemas import (
    AssessRepairRiskInput,
    AssessRepairRiskOutput,
    CompareRepairStrategiesInput,
    CompareRepairStrategiesOutput,
    CreateRepairPlanInput,
    CreateRepairPlanOutput,
    ExplainRepairPlanInput,
    ExplainRepairPlanOutput,
    GenerateVerificationPlanInput,
    GenerateVerificationPlanOutput,
    RepairApproach,
    RepairEvidence,
    RepairPlan,
    RepairPlanStatus,
    RepairRiskAssessment,
    RepairStrategy,
    RequiredChange,
    RiskLikelihood,
    RiskSeverity,
    RollbackPlan,
    StrategyComplexity,
    StrategyInvasiveness,
    StrategyReversibility,
    ValidateRepairPlanInput,
    ValidateRepairPlanOutput,
    VerificationPlan,
)
from forge.repair_planning.evidence import RepairEvidenceCollector
from forge.repair_planning.strategies import RepairStrategyGenerator
from forge.repair_planning.risk import RepairRiskAnalyzer
from forge.repair_planning.rollback import RollbackPlanBuilder
from forge.repair_planning.validation import RepairPlanValidator
from forge.repair_planning.planner import RepairPlanningEngine
from forge.repair_planning.tools import RepairPlanningTools

__all__ = [
    "AssessRepairRiskInput",
    "AssessRepairRiskOutput",
    "CompareRepairStrategiesInput",
    "CompareRepairStrategiesOutput",
    "CreateRepairPlanInput",
    "CreateRepairPlanOutput",
    "ExplainRepairPlanInput",
    "ExplainRepairPlanOutput",
    "GenerateVerificationPlanInput",
    "GenerateVerificationPlanOutput",
    "RepairApproach",
    "RepairEvidence",
    "RepairEvidenceCollector",
    "RepairPlan",
    "RepairPlanStatus",
    "RepairPlanValidator",
    "RepairPlanningEngine",
    "RepairPlanningTools",
    "RepairRiskAnalyzer",
    "RepairRiskAssessment",
    "RepairStrategy",
    "RepairStrategyGenerator",
    "RequiredChange",
    "RiskLikelihood",
    "RiskSeverity",
    "RollbackPlan",
    "RollbackPlanBuilder",
    "StrategyComplexity",
    "StrategyInvasiveness",
    "StrategyReversibility",
    "ValidateRepairPlanInput",
    "ValidateRepairPlanOutput",
    "VerificationPlan",
]
