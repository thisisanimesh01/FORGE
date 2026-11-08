"""Phase 12 M4: Controlled Autonomous Repair Execution.

Provides safe, reviewable repair application inside isolated workspaces:
- Strict human approval validation and authorization invariants.
- Isolated workspace lifecycle management with baseline integrity tracking.
- Bounded unified diff synthesis and strict hunk validation.
- Pure in-memory patch application (zero host subprocess/shell execution).
- Workspace integrity verification and automatic rollback on breach.
- Structured audit event recording with secret redaction.
- Verification-pending handoff for Phase 12 M5.
"""

from forge.repair_execution.approval import (
    RepairApprovalValidator,
    ApprovalValidationResult,
    compute_plan_digest,
)
from forge.repair_execution.events import RepairEventRecorder
from forge.repair_execution.executor import ControlledRepairExecutor
from forge.repair_execution.integrity import (
    WorkspaceIntegrityChecker,
    WorkspaceIntegrityReport,
)
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.patch_applier import (
    ControlledPatchApplier,
    PatchApplicationOutcome,
)
from forge.repair_execution.patch_generator import RepairPatchGenerator
from forge.repair_execution.patch_validator import (
    RepairPatchValidator,
    PatchValidationResult,
    compute_patch_digest,
)
from forge.repair_execution.rollback import WorkspaceRollbackManager
from forge.repair_execution.schemas import (
    MAX_AFFECTED_FILES,
    MAX_DIFF_BYTES,
    MAX_EVENTS_PER_EXECUTION,
    ApprovalStatus,
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    PatchValidationStatus,
    RepairApproval,
    RepairEvent,
    RepairEventType,
    RepairExecutionRequest,
    RepairExecutionResult,
    RepairPatch,
    RepairWorkspace,
    RollbackStatus,
    WorkspaceStatus,
)
from forge.repair_execution.tools import RepairExecutionTools
from forge.repair_execution.workspace import (
    RepairWorkspaceManager,
    compute_tree_digest,
)

__all__ = [
    # Schemas & Enums
    "MAX_AFFECTED_FILES",
    "MAX_DIFF_BYTES",
    "MAX_EVENTS_PER_EXECUTION",
    "ApprovalStatus",
    "ExecutionLimits",
    "ExecutionStatus",
    "FailureClassification",
    "PatchValidationStatus",
    "RepairApproval",
    "RepairEvent",
    "RepairEventType",
    "RepairExecutionRequest",
    "RepairExecutionResult",
    "RepairPatch",
    "RepairWorkspace",
    "RollbackStatus",
    "WorkspaceStatus",
    # Approval & Ledger
    "RepairApprovalValidator",
    "ApprovalValidationResult",
    "compute_plan_digest",
    "ApprovalLedger",
    # Workspace & Rollback
    "RepairWorkspaceManager",
    "compute_tree_digest",
    "WorkspaceRollbackManager",
    "WorkspaceIntegrityChecker",
    "WorkspaceIntegrityReport",
    # Patch & Application
    "RepairPatchGenerator",
    "RepairPatchValidator",
    "PatchValidationResult",
    "compute_patch_digest",
    "ControlledPatchApplier",
    "PatchApplicationOutcome",
    # Event & Execution
    "RepairEventRecorder",
    "ControlledRepairExecutor",
    "RepairExecutionTools",
]
