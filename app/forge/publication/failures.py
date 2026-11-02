"""Phase 9 Git Publication Architecture — Failure Hierarchy.

Defines typed exception classes for publication authorization, identity verification,
policy violations, and security boundary enforcement.
"""


class PublicationError(Exception):
    """Base exception for all publication-related errors."""
    pass


class PublicationAuthorizationError(PublicationError):
    """Raised when publication is attempted without a valid, approved operator decision."""
    pass


class PublicationActionMismatchError(PublicationError):
    """Raised when requested publication action was not authorized by the operator approval."""
    pass


class RepositoryIdentityMismatchError(PublicationError):
    """Raised when repository path, remote URL, or base commit SHA does not match expected identity."""
    pass


class RepositoryDirtyError(PublicationError):
    """Raised when target repository working tree contains uncommitted or untracked changes."""
    pass


class ProtectedBranchViolationError(PublicationError):
    """Raised when a publication action attempts to target or overwrite a protected branch."""
    pass


class PublicationPolicyViolationError(PublicationError):
    """Raised when a publication request violates configured Git publication policies."""
    pass


class ArtifactBindingMismatchError(PublicationError):
    """Raised when candidate patch digest, base SHA, or verification records do not match approval binding."""
    pass


class DuplicatePublicationError(PublicationError):
    """Raised when an active publication request already exists for the same repair and action."""
    pass


class SecretDetectedError(PublicationError):
    """Raised when credentials or secrets are detected in commit messages, branch names, or metadata."""
    pass


class PublicationWorkspaceError(PublicationError):
    """Raised when provisioning, configuring, or cleaning an isolated publication workspace fails."""
    pass


class TargetRepoMutationError(PublicationError):
    """Raised when the original target repository is detected to have been mutated during publication."""
    pass


class BranchCreationError(PublicationError):
    """Raised when branch planning or creation fails."""
    pass


class BranchCollisionError(PublicationError):
    """Raised when proposed branch name already exists in target repository or workspace."""
    pass


class PatchStagingError(PublicationError):
    """Raised when applying or staging approved patch in publication workspace fails."""
    pass


class CommitCreationError(PublicationError):
    """Raised when creating a commit in the isolated publication workspace fails."""
    pass


class CommitVerificationError(PublicationError):
    """Raised when post-commit verification checks (parent, tree, diff, author) fail."""
    pass


class WorkspaceIntegrityError(PublicationError):
    """Raised when publication workspace filesystem integrity is violated."""
    pass


class SymlinkEscapeError(WorkspaceIntegrityError):
    """Raised when a symlink escapes the publication workspace root."""
    pass
