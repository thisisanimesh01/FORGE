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
