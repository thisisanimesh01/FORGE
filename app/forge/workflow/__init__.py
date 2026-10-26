"""FORGE Phase 5: Supervised End-to-End Engineering Workflow package.

Milestone 1: Unified Task Intake & Repository Context Pipeline.
"""

from forge.workflow.context import (
    assemble_repository_context,
    extract_source_excerpts,
    filter_and_rank_symbols,
    render_context_prompt,
)
from forge.workflow.intake import (
    compute_intake_id,
    create_intake_from_diff,
    create_intake_from_finding,
    create_intake_from_issue,
    create_intake_from_test_spec,
    resolve_and_pin_revision,
    sanitize_affected_paths,
    validate_intake_spec,
)
from forge.workflow.models import (
    ContextBudget,
    ContextBudgetExceededError,
    ContextBudgetSummary,
    DiffIntakePayload,
    DirtyRepositoryError,
    FindingIntakePayload,
    IntakeSourceType,
    IntakeValidationError,
    IssueIntakePayload,
    RepositoryContext,
    RepositoryRevisionInfo,
    RevisionDriftError,
    SourceExcerpt,
    TargetImmutabilityViolationError,
    TestSpecIntakePayload,
    WorkflowError,
    WorkflowIntakeSpec,
)

__all__ = [
    # Errors
    "WorkflowError",
    "IntakeValidationError",
    "RevisionDriftError",
    "DirtyRepositoryError",
    "ContextBudgetExceededError",
    "TargetImmutabilityViolationError",
    # Enums & Models
    "IntakeSourceType",
    "RepositoryRevisionInfo",
    "FindingIntakePayload",
    "DiffIntakePayload",
    "IssueIntakePayload",
    "TestSpecIntakePayload",
    "WorkflowIntakeSpec",
    "ContextBudget",
    "SourceExcerpt",
    "ContextBudgetSummary",
    "RepositoryContext",
    # Intake Functions
    "resolve_and_pin_revision",
    "compute_intake_id",
    "sanitize_affected_paths",
    "create_intake_from_finding",
    "create_intake_from_diff",
    "create_intake_from_issue",
    "create_intake_from_test_spec",
    "validate_intake_spec",
    # Context Pipeline Functions
    "assemble_repository_context",
    "filter_and_rank_symbols",
    "extract_source_excerpts",
    "render_context_prompt",
]
