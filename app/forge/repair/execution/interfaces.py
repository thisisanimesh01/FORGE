"""Phase 8 Repair Execution Architecture — Core Component Interfaces.

Defines typed Protocols for RepairPlanner, RepairExecutor, RepairWorkspace,
PatchValidator, RepairVerifier, and RepairEventStore.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union, runtime_checkable

from forge.repair.execution.models import (
    ExecutionTestResult,
    PatchArtifact,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairRequest,
    ValidationResult,
)
from forge.repair.execution.policy import RepairExecutionPolicy


@runtime_checkable
class RepairWorkspaceProtocol(Protocol):
    """Protocol defining the isolated scratch workspace contract."""

    @property
    def workspace_id(self) -> str:
        """Unique identifier for this workspace instance."""
        ...

    @property
    def workspace_path(self) -> Path:
        """Absolute path to the isolated scratch working directory."""
        ...

    @property
    def target_repo_path(self) -> Path:
        """Absolute path to the original read-only target repository."""
        ...

    @property
    def base_commit_sha(self) -> str:
        """Pinned git commit SHA under test."""
        ...

    @property
    def is_isolated(self) -> bool:
        """Whether this workspace is guaranteed strictly isolated from target repository."""
        ...

    def prepare(self) -> None:
        """Set up the isolated disposable workspace without touching target repo."""
        ...

    def cleanup(self) -> None:
        """Safely destroy or reset the disposable workspace."""
        ...

    def validate_containment(self, file_path: Union[str, Path]) -> Path:
        """Resolve a path and ensure it resides strictly within workspace_path without symlink escape.

        Raises:
            ScopeContainmentViolationError: If path escapes workspace.
        """
        ...

    def check_target_immutability(self) -> bool:
        """Verify that the target repository remains unmodified."""
        ...


@runtime_checkable
class RepairPlannerProtocol(Protocol):
    """Protocol for formulating bounded repair plans from validated findings and evidence."""

    def formulate_plan(
        self,
        request: RepairRequest,
        policy: RepairExecutionPolicy,
    ) -> RepairExecutionPlan:
        """Formulate a grounded RepairExecutionPlan for a RepairRequest.

        Raises:
            RepairPolicyViolationError: If request violates policy.
        """
        ...


@runtime_checkable
class PatchValidatorProtocol(Protocol):
    """Protocol for validating candidate patch artifacts against boundaries and policies."""

    def validate_patch(
        self,
        patch: PatchArtifact,
        request: RepairRequest,
        policy: RepairExecutionPolicy,
        workspace: RepairWorkspaceProtocol,
    ) -> ValidationResult:
        """Verify that candidate patch respects scope, budget, and syntax rules."""
        ...


@runtime_checkable
class RepairExecutorProtocol(Protocol):
    """Protocol for executing patch candidate construction within isolated workspace."""

    def execute_repair(
        self,
        plan: RepairExecutionPlan,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
    ) -> PatchArtifact:
        """Generate a PatchArtifact inside isolated workspace based on the formulated plan."""
        ...


@runtime_checkable
class RepairVerifierProtocol(Protocol):
    """Protocol for isolated verification of candidate patches against reproduction oracle and test suites."""

    def verify_repair(
        self,
        patch: PatchArtifact,
        plan: RepairExecutionPlan,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
    ) -> Tuple[bool, List[ExecutionTestResult], Optional[str]]:
        """Apply patch in workspace, execute pre-approved verification checks, and report outcome.

        Returns:
            Tuple of (verification_passed, test_results, diagnostic_message)
        """
        ...


@runtime_checkable
class RepairEventStoreProtocol(Protocol):
    """Protocol for persistent storage and audit event ledger of repair executions."""

    def save_repair_request(self, request: RepairRequest) -> None:
        """Persist or update a RepairRequest."""
        ...

    def get_repair_request(self, repair_id: str) -> Optional[RepairRequest]:
        """Retrieve a RepairRequest by ID."""
        ...

    def save_patch_artifact(self, patch: PatchArtifact) -> None:
        """Persist an immutable candidate PatchArtifact."""
        ...

    def get_patch_artifact(self, artifact_id: str) -> Optional[PatchArtifact]:
        """Retrieve a PatchArtifact by ID."""
        ...

    def record_event(self, event: RepairExecutionEvent) -> None:
        """Record an immutable audit event in the repair ledger."""
        ...

    def get_events(self, repair_id: str) -> List[RepairExecutionEvent]:
        """Retrieve sequential audit events for a repair execution."""
        ...
