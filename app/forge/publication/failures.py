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


class RemotePublicationError(PublicationError):
    """Base exception for all remote publication and pull request errors."""
    pass


class RemoteAuthenticationError(RemotePublicationError):
    """Raised when provider authentication fails or credentials are missing/invalid."""
    pass


class RemoteRepositoryVerificationError(RemotePublicationError):
    """Raised when remote repository identity, owner, name, or default branch does not match."""
    pass


class RemoteBranchPushError(RemotePublicationError):
    """Raised when pushing a branch to the remote repository fails."""
    pass


class RemoteBranchVerificationError(RemotePublicationError):
    """Raised when verifying the published remote branch or its commit SHA fails."""
    pass


class PullRequestCreationError(RemotePublicationError):
    """Raised when creating a pull request via the Git hosting provider fails."""
    pass


class PullRequestVerificationError(RemotePublicationError):
    """Raised when verifying a created pull request on the remote provider fails."""
    pass


class ProviderRateLimitError(RemotePublicationError):
    """Raised when Git hosting provider rate limits or throttling are encountered."""
    pass


class ProviderTimeoutError(RemotePublicationError):
    """Raised when communication with the Git hosting provider times out."""
    pass


class RemoteBranchCollisionError(RemotePublicationError):
    """Raised when an existing remote branch conflicts with the publication branch."""
    pass


class DuplicatePullRequestError(RemotePublicationError):
    """Raised when an open pull request already exists for the publication branch."""
    pass
