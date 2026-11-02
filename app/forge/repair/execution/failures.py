"""Phase 8 Repair Execution Architecture — Workspace and Sandbox Failures.

Defines structured exception hierarchy for workspace provisioning, repository acquisition,
commit identity verification, workspace collision, integrity violations, and sandbox execution.
"""

from typing import Any, Dict, Optional

from forge.repair.execution.policy import (
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
)


class WorkspaceExecutionError(Exception):
    """Base exception for all repair workspace execution and sandbox failures."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class WorkspaceProvisioningError(WorkspaceExecutionError):
    """Raised when isolated workspace provisioning fails (e.g. disk full, permission denied)."""


class RepositoryAcquisitionError(WorkspaceExecutionError):
    """Raised when target repository acquisition or copying fails."""


class CommitMismatchError(WorkspaceExecutionError):
    """Raised when repository HEAD commit SHA does not match the pinned target commit SHA."""


class WorkspaceCollisionError(WorkspaceExecutionError, TargetRepoImmutabilityViolationError):
    """Raised when a workspace path collides with an existing active workspace or target repository."""


class WorkspaceReuseForbiddenError(WorkspaceExecutionError):
    """Raised when attempting to reuse or reprovision an already used or non-fresh workspace."""


class WorkspaceIntegrityViolationError(WorkspaceExecutionError):
    """Raised when unexpected modifications, additions, deletions, or symlink attacks are detected."""


class SandboxUnavailableError(WorkspaceExecutionError):
    """Raised when the Docker daemon or required sandbox runtime is unavailable (fail closed)."""


class SandboxInitializationError(WorkspaceExecutionError):
    """Raised when container initialization or configuration fails."""


class ResourceLimitViolationError(WorkspaceExecutionError):
    """Raised when a sandbox execution exceeds resource limits (CPU, memory, timeout, PIDs)."""


class WorkspaceCleanupError(WorkspaceExecutionError):
    """Raised when workspace teardown or directory removal encounters an unrecoverable error."""
