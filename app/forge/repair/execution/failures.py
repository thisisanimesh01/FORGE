"""Phase 8 Repair Execution Architecture — Workspace and Sandbox Failures.

Defines structured exception hierarchy for workspace provisioning, repository acquisition,
commit identity verification, workspace collision, integrity violations, and sandbox execution.
"""

from typing import Any, Dict, Optional

from forge.repair.execution.policy import (
    ArbitraryExecutionForbiddenError,
    RepairPolicyViolationError,
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


class RepairRequestValidationError(WorkspaceExecutionError):
    """Raised when a repair request fails validation (missing finding, mismatched repo/commit, missing evidence)."""


class InsufficientEvidenceError(WorkspaceExecutionError):
    """Raised when finding lacks verified supporting source evidence required for grounded repair."""


class StaleBaseRevisionError(WorkspaceExecutionError):
    """Raised when workspace or target repo commit revision does not match finding base commit."""


class PatchGenerationError(WorkspaceExecutionError):
    """Raised when patch generation fails, times out, or produces malformed output."""


class PatchValidationError(WorkspaceExecutionError):
    """Raised when a candidate patch violates syntax, scope, budget, AST, or semantic invariants."""


class PatchIntegrityError(PatchValidationError):
    """Raised when patch digest, artifact identity, or cryptographic chain of custody verification fails."""


class PatchScopeViolationError(PatchValidationError):
    """Raised when patch touches unauthorized files, protected paths, or exceeds budget constraints."""


class PatchSemanticInvariantError(PatchValidationError):
    """Raised when patch breaks semantic invariants (e.g. stripped guards, dead code, auth bypass)."""


class PatchAdversarialDefectError(PatchValidationError):
    """Raised when patch contains adversarial content (null bytes, duplicate/overlapping hunks, mode tampering, secrets)."""


class PatchApplicationError(WorkspaceExecutionError):
    """Raised when a candidate patch fails to apply cleanly to the workspace files."""


class VerificationPreconditionError(WorkspaceExecutionError):
    """Raised when repair verification preconditions fail (unvalidated patch, hash mismatch, etc.)."""


class VerificationExecutionError(WorkspaceExecutionError):
    """Raised when test execution or verification analysis fails unrecoverably."""
