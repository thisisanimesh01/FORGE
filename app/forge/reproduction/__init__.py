"""FORGE reproduction package: case management, evidence capture, and execution coordination."""

from forge.reproduction.models import (
    CaseStatus,
    FieldProvenance,
    ReproductionAttempt,
    ReproductionCase,
    RetryPolicy,
    generate_case_id,
)
from forge.reproduction.manager import (
    ReproductionCaseManager,
    CaseManagementError,
    StaleRevisionError,
    NonIdempotentRetryError,
)
from forge.reproduction.planner import (
    ReproductionStrategy,
    ReproductionPlan,
    PlanProvenance,
    PlanValidationResult,
    ReproductionPlanner,
    generate_plan_id,
)
from forge.reproduction.validator import ReproductionPlanValidator
from forge.reproduction.classifier import (
    ClassificationOutcome,
    ClassificationReasonCode,
    ReproductionClassification,
    ReproductionOutcomeClassifier,
    CLASSIFIER_VERSION,
    generate_classification_id,
)

__all__ = [
    "CaseStatus",
    "FieldProvenance",
    "ReproductionAttempt",
    "ReproductionCase",
    "RetryPolicy",
    "generate_case_id",
    "ReproductionCaseManager",
    "CaseManagementError",
    "StaleRevisionError",
    "NonIdempotentRetryError",
    # Planner
    "ReproductionStrategy",
    "ReproductionPlan",
    "PlanProvenance",
    "PlanValidationResult",
    "ReproductionPlanner",
    "generate_plan_id",
    # Validator
    "ReproductionPlanValidator",
    # Classifier
    "ClassificationOutcome",
    "ClassificationReasonCode",
    "ReproductionClassification",
    "ReproductionOutcomeClassifier",
    "CLASSIFIER_VERSION",
    "generate_classification_id",
]
