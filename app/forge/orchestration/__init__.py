"""Phase 13 M1: End-to-End Engineering Orchestration Package.

Integrates defect diagnosis (M1), root-cause analysis (M2), repair planning (M3),
controlled repair execution (M4), repair verification (M5), and repair quality
evaluation (M6) into a coherent, deterministic, reviewable engineering workflow.
"""

from forge.orchestration.schemas import (
    CreateOrchestrationWorkflowInput,
    CreateOrchestrationWorkflowOutput,
    EngineeringWorkflowResult,
    ExecuteOrchestrationStageInput,
    ExecuteOrchestrationStageOutput,
    GetEngineeringReportInput,
    GetEngineeringReportOutput,
    InspectOrchestrationStatusInput,
    InspectOrchestrationStatusOutput,
    InspectStageResultInput,
    InspectStageResultOutput,
    StageExecutionRecord,
    StageOutputs,
    StageStatus,
    SubmitOrchestrationApprovalInput,
    SubmitOrchestrationApprovalOutput,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.orchestration.state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalStateTransitionError,
    InvalidTransitionError,
    TerminalStateError,
    WorkflowStateError,
    is_terminal_state,
    validate_transition,
)
from forge.orchestration.events import (
    AuditTrailTamperedError,
    OrchestrationEvent,
    OrchestrationEventRecorder,
    OrchestrationEventType,
)
from forge.orchestration.approval import (
    ApprovalValidationError,
    ApprovalVerificationOutcome,
    OrchestrationApprovalError,
    OrchestrationApprovalManager,
)
from forge.orchestration.report import EngineeringReportGenerator
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.tools import OrchestrationTools
from forge.orchestration.persistence import (
    CorruptedStateError,
    IncompatibleSchemaError,
    ORCHESTRATION_SCHEMA_VERSION,
    OrchestrationStateStore,
    PersistenceError,
)
from forge.orchestration.retry import (
    FailureCategory,
    FailureClassifier,
    RetryDecision,
    RetryPolicy,
)
from forge.orchestration.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyRecord,
    OperationStatus,
    UncertainExecutionError,
    compute_idempotency_key,
)
from forge.orchestration.coordination import (
    DependencyCycleError,
    TaskCoordinator,
    TaskDependencyError,
    TaskRecord,
    TaskState,
)
from forge.orchestration.recovery import (
    RecoveryDecision,
    RecoveryStatus,
    WorkflowRecoveryEngine,
    WorkflowRecoveryError,
)
from forge.orchestration.adapters import (
    DefaultDiagnosisAdapter,
    DefaultExecutionAdapter,
    DefaultPlanningAdapter,
    DefaultQualityAdapter,
    DefaultRootCauseAdapter,
    DefaultVerificationAdapter,
    DiagnosisAdapter,
    ExecutionAdapter,
    FakeDiagnosisAdapter,
    FakeExecutionAdapter,
    FakePlanningAdapter,
    FakeQualityAdapter,
    FakeRootCauseAdapter,
    FakeVerificationAdapter,
    PlanningAdapter,
    QualityAdapter,
    RootCauseAdapter,
    VerificationAdapter,
)

__all__ = [
    # Schemas & Enums
    "WorkflowState",
    "WorkflowStage",
    "StageStatus",
    "StageExecutionRecord",
    "WorkflowContext",
    "StageOutputs",
    "EngineeringWorkflowResult",
    "CreateOrchestrationWorkflowInput",
    "CreateOrchestrationWorkflowOutput",
    "ExecuteOrchestrationStageInput",
    "ExecuteOrchestrationStageOutput",
    "InspectOrchestrationStatusInput",
    "InspectOrchestrationStatusOutput",
    "InspectStageResultInput",
    "InspectStageResultOutput",
    "SubmitOrchestrationApprovalInput",
    "SubmitOrchestrationApprovalOutput",
    "GetEngineeringReportInput",
    "GetEngineeringReportOutput",
    # State Machine
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "InvalidTransitionError",
    "WorkflowStateError",
    "is_terminal_state",
    "validate_transition",
    # Events & Auditing
    "OrchestrationEvent",
    "OrchestrationEventRecorder",
    "OrchestrationEventType",
    # Approval
    "ApprovalVerificationOutcome",
    "OrchestrationApprovalError",
    "OrchestrationApprovalManager",
    # Persistence & Checkpointing (M2)
    "OrchestrationStateStore",
    "PersistenceError",
    "CorruptedStateError",
    "IncompatibleSchemaError",
    "ORCHESTRATION_SCHEMA_VERSION",
    # Retry & Failure Classification (M2)
    "FailureCategory",
    "FailureClassifier",
    "RetryDecision",
    "RetryPolicy",
    # Idempotency & Mutation Safeguards (M2)
    "IdempotencyConflictError",
    "IdempotencyLedger",
    "IdempotencyRecord",
    "OperationStatus",
    "UncertainExecutionError",
    "compute_idempotency_key",
    # Task Coordination & DAG Management (M2)
    "DependencyCycleError",
    "TaskCoordinator",
    "TaskDependencyError",
    "TaskRecord",
    "TaskState",
    # Recovery Engine (M2)
    "RecoveryDecision",
    "RecoveryStatus",
    "WorkflowRecoveryEngine",
    "WorkflowRecoveryError",
    # Reporting
    "EngineeringReportGenerator",
    # Core Orchestration
    "EngineeringOrchestrator",
    "OrchestrationTools",
    # Adapters
    "DiagnosisAdapter",
    "RootCauseAdapter",
    "PlanningAdapter",
    "ExecutionAdapter",
    "VerificationAdapter",
    "QualityAdapter",
    "DefaultDiagnosisAdapter",
    "DefaultRootCauseAdapter",
    "DefaultPlanningAdapter",
    "DefaultExecutionAdapter",
    "DefaultVerificationAdapter",
    "DefaultQualityAdapter",
    "FakeDiagnosisAdapter",
    "FakeRootCauseAdapter",
    "FakePlanningAdapter",
    "FakeExecutionAdapter",
    "FakeVerificationAdapter",
    "FakeQualityAdapter",
]
