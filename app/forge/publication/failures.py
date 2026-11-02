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
