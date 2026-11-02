"""FORGE Phase 8 Repair Execution Architecture Package.

Provides the foundational architecture, domain models, guarded state machine,
strict safety boundaries, component interfaces, isolated workspace management,
and audit event ledger for evidence-grounded software repair execution.
"""

from forge.repair.execution.applier import SafePatchApplier
from forge.repair.execution.coordinator import RepairExecutionCoordinator
from forge.repair.execution.failures import (
    CommitMismatchError,
    InsufficientEvidenceError,
    PatchApplicationError,
    PatchGenerationError,
    PatchValidationError,
    RepairRequestValidationError,
    RepositoryAcquisitionError,
    ResourceLimitViolationError,
    SandboxInitializationError,
    SandboxUnavailableError,
    ScopeContainmentViolationError,
    StaleBaseRevisionError,
    TargetRepoImmutabilityViolationError,
    WorkspaceCleanupError,
    WorkspaceCollisionError,
    WorkspaceExecutionError,
    WorkspaceIntegrityViolationError,
    WorkspaceProvisioningError,
    WorkspaceReuseForbiddenError,
)
from forge.repair.execution.generator import RepairPatchGenerator
from forge.repair.execution.interfaces import (
    PatchValidatorProtocol,
    RepairEventStoreProtocol,
    RepairExecutorProtocol,
    RepairPlannerProtocol,
    RepairVerifierProtocol,
    RepairWorkspaceProtocol,
)
from forge.repair.execution.manager import RepairWorkspaceManager
from forge.repair.execution.models import (
    ExecutionTestResult,
    FailureCategory,
    FailureClassification,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairExecutionRecord,
    RepairExecutionState,
    RepairRequest,
    ValidationResult,
    WorkspaceRecord,
)
from forge.repair.execution.planner import RepairExecutionPlanner
from forge.repair.execution.policy import (
    ArbitraryExecutionForbiddenError,
    BudgetExceededViolationError,
    DEFAULT_ALLOWED_COMMAND_PREFIXES,
    DEFAULT_ALLOWED_ENV_VARS,
    NetworkAccessForbiddenError,
    PROTECTED_EXACT_FILENAMES,
    PROTECTED_PATH_PREFIXES,
    ProtectedPathViolationError,
    RepairExecutionPolicy,
    RepairPolicyViolationError,
)
from forge.repair.execution.state import (
    ACTOR_TRANSITION_PERMISSIONS,
    ALLOWED_REPAIR_TRANSITIONS,
    IllegalStateModificationError,
    InvalidRepairTransitionError,
    RepairExecutionStateError,
    RepairExecutionStateMachine,
    RESUMABLE_STATES,
    TERMINAL_STATES,
    UnauthorizedTransitionActorError,
    validate_repair_transition,
)
from forge.repair.execution.storage import (
    RepairExecutionStore,
    RepairExecutionStoreError,
)
from forge.repair.execution.validator import (
    PatchValidator,
    RepairRequestValidator,
)
from forge.repair.execution.workspace import RepairWorkspace

__all__ = [
    # Enums & Models
    "RepairExecutionState",
    "RepairActor",
    "FailureCategory",
    "FailureClassification",
    "ExecutionTestResult",
    "ValidationResult",
    "PatchArtifact",
    "RepairExecutionPlan",
    "RepairRequest",
    "RepairExecutionEvent",
    "RepairExecutionRecord",
    "WorkspaceRecord",
    # State Machine & Transitions
    "ALLOWED_REPAIR_TRANSITIONS",
    "ACTOR_TRANSITION_PERMISSIONS",
    "TERMINAL_STATES",
    "RESUMABLE_STATES",
    "validate_repair_transition",
    "RepairExecutionStateMachine",
    "RepairExecutionStateError",
    "InvalidRepairTransitionError",
    "UnauthorizedTransitionActorError",
    "IllegalStateModificationError",
    # Safety Policies & Boundaries
    "RepairExecutionPolicy",
    "RepairPolicyViolationError",
    "TargetRepoImmutabilityViolationError",
    "ScopeContainmentViolationError",
    "ArbitraryExecutionForbiddenError",
    "NetworkAccessForbiddenError",
    "BudgetExceededViolationError",
    "ProtectedPathViolationError",
    "DEFAULT_ALLOWED_COMMAND_PREFIXES",
    "DEFAULT_ALLOWED_ENV_VARS",
    "PROTECTED_EXACT_FILENAMES",
    "PROTECTED_PATH_PREFIXES",
    # Workspace & Execution Failures
    "WorkspaceExecutionError",
    "WorkspaceProvisioningError",
    "RepositoryAcquisitionError",
    "CommitMismatchError",
    "WorkspaceCollisionError",
    "WorkspaceReuseForbiddenError",
    "WorkspaceIntegrityViolationError",
    "SandboxUnavailableError",
    "SandboxInitializationError",
    "ResourceLimitViolationError",
    "WorkspaceCleanupError",
    "RepairRequestValidationError",
    "InsufficientEvidenceError",
    "StaleBaseRevisionError",
    "PatchGenerationError",
    "PatchValidationError",
    "PatchApplicationError",
    # Interfaces
    "RepairWorkspaceProtocol",
    "RepairPlannerProtocol",
    "PatchValidatorProtocol",
    "RepairExecutorProtocol",
    "RepairVerifierProtocol",
    "RepairEventStoreProtocol",
    # Workspace & Storage Implementations
    "RepairWorkspace",
    "RepairWorkspaceManager",
    "RepairExecutionStore",
    "RepairExecutionStoreError",
    # M3 Candidate Patch Generation Components
    "RepairRequestValidator",
    "PatchValidator",
    "RepairExecutionPlanner",
    "RepairPatchGenerator",
    "SafePatchApplier",
    "RepairExecutionCoordinator",
]
