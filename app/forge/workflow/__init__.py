"""FORGE Phase 5: Supervised End-to-End Engineering Workflow package.

Milestone 1: Unified Task Intake & Repository Context Pipeline.
Milestone 2: Supervised Workflow State Machine & Transactional Ledger.
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
    EngineeringWorkflow,
    EngineeringWorkflowEvent,
    EngineeringWorkflowState,
    FindingIntakePayload,
    HumanApprovalRequiredError,
    IntakeSourceType,
    IntakeValidationError,
    InvalidWorkflowTransitionError,
    IssueIntakePayload,
    RepositoryContext,
    RepositoryRevisionInfo,
    RevisionDriftError,
    SourceExcerpt,
    TargetImmutabilityViolationError,
    TestSpecIntakePayload,
    WorkflowConcurrencyError,
    WorkflowError,
    WorkflowIntakeSpec,
    WorkflowResumeError,
)
from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator
from forge.workflow.state import (
    ALLOWED_TRANSITIONS,
    RESUMABLE_STATES,
    TERMINAL_STATES,
    is_resumable_state,
    is_terminal_state,
    validate_workflow_transition,
)

__all__ = [
    # Errors
    "WorkflowError",
    "IntakeValidationError",
    "RevisionDriftError",
    "DirtyRepositoryError",
    "ContextBudgetExceededError",
    "TargetImmutabilityViolationError",
    "InvalidWorkflowTransitionError",
    "WorkflowConcurrencyError",
    "WorkflowResumeError",
    "HumanApprovalRequiredError",
    # Enums & Models
    "IntakeSourceType",
    "EngineeringWorkflowState",
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
    "EngineeringWorkflow",
    "EngineeringWorkflowEvent",
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
    # State Machine & Transition Rules
    "ALLOWED_TRANSITIONS",
    "RESUMABLE_STATES",
    "TERMINAL_STATES",
    "validate_workflow_transition",
    "is_resumable_state",
    "is_terminal_state",
    # Orchestrator
    "EngineeringWorkflowOrchestrator",
]
