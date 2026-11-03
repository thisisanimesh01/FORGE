"""Phase 9 Git Publication Architecture Package.

Exposes domain models, protocols, policies, inspectors, validators, planners,
and persistent stores for safe Git publication of operator-approved repairs.
"""

from forge.publication.authorization import (
    ACTION_AUTHORIZATION_MAP,
    get_permitted_publication_actions,
    is_action_authorized,
    validate_action_authorization,
)
from forge.publication.branch import GitBranchManager
from forge.publication.commit import GitCommitBuilder
from forge.publication.failures import (
    ArtifactBindingMismatchError,
    BranchCollisionError,
    BranchCreationError,
    CommitCreationError,
    CommitVerificationError,
    DuplicatePullRequestError,
    DuplicatePublicationError,
    PatchStagingError,
    ProtectedBranchViolationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
    PublicationError,
    PublicationPolicyViolationError,
    PublicationWorkspaceError,
    PullRequestCreationError,
    PullRequestVerificationError,
    RemoteAuthenticationError,
    RemoteBranchCollisionError,
    RemoteBranchPushError,
    RemoteBranchVerificationError,
    RemotePublicationError,
    RemoteRepositoryVerificationError,
    RepositoryDirtyError,
    RepositoryIdentityMismatchError,
    SecretDetectedError,
    SymlinkEscapeError,
    TargetRepoMutationError,
    WorkspaceIntegrityError,
)
from forge.publication.inspector import GitRepositoryInspector
from forge.publication.interfaces import (
    GitBranchManagerProtocol,
    GitCommitBuilderProtocol,
    GitPublicationValidatorProtocol,
    GitRepositoryInspectorProtocol,
    PublicationAuditStoreProtocol,
    PullRequestProviderProtocol,
)
from forge.publication.models import (
    BranchProposal,
    CommitAuthor,
    CommitProposal,
    ProviderOperationRecord,
    PublicationAction,
    PublicationExecutionState,
    PublicationPlan,
    PublicationRequest,
    PublicationStatus,
    PublicationValidationResult,
    PullRequestDetails,
    PullRequestRecord,
    RemoteBranchRecord,
    RemotePublicationRecord,
    RemoteRepositorySpec,
    StagedCommitRecord,
    WorkspaceIntegritySnapshot,
)
from forge.publication.planner import GitPublicationPlanner
from forge.publication.policy import (
    GitPublicationPolicy,
    is_protected_branch,
    scan_for_secrets,
    validate_branch_name,
    validate_remote_url,
)
from forge.publication.pr_template import PullRequestFormatter
from forge.publication.providers import (
    GitHubPullRequestProvider,
    retry_idempotent_operation,
)
from forge.publication.remote_coordinator import RemotePublicationCoordinator
from forge.publication.remote_publisher import (
    RemoteBranchPublisher,
    parse_repository_owner_and_name,
)
from forge.publication.staging import PublicationStagingCoordinator
from forge.publication.storage import PublicationStore
from forge.publication.validator import GitPublicationValidator
from forge.publication.workspace import PublicationWorkspace

__all__ = [
    # Models
    "PublicationAction",
    "PublicationExecutionState",
    "PublicationStatus",
    "RepositoryIdentity",
    "CommitAuthor",
    "BranchProposal",
    "CommitProposal",
    "PublicationPlan",
    "PublicationRequest",
    "PublicationValidationResult",
    "StagedCommitRecord",
    "WorkspaceIntegritySnapshot",
    "RemoteRepositorySpec",
    "PullRequestDetails",
    "RemoteBranchRecord",
    "PullRequestRecord",
    "ProviderOperationRecord",
    "RemotePublicationRecord",
    # Protocols
    "GitRepositoryInspectorProtocol",
    "GitBranchManagerProtocol",
    "GitCommitBuilderProtocol",
    "GitPublicationValidatorProtocol",
    "PullRequestProviderProtocol",
    "PublicationAuditStoreProtocol",
    # Failures
    "PublicationError",
    "PublicationAuthorizationError",
    "PublicationActionMismatchError",
    "RepositoryIdentityMismatchError",
    "RepositoryDirtyError",
    "ProtectedBranchViolationError",
    "PublicationPolicyViolationError",
    "ArtifactBindingMismatchError",
    "DuplicatePublicationError",
    "SecretDetectedError",
    "PublicationWorkspaceError",
    "TargetRepoMutationError",
    "BranchCreationError",
    "BranchCollisionError",
    "PatchStagingError",
    "CommitCreationError",
    "CommitVerificationError",
    "WorkspaceIntegrityError",
    "SymlinkEscapeError",
    "RemotePublicationError",
    "RemoteAuthenticationError",
    "RemoteRepositoryVerificationError",
    "RemoteBranchPushError",
    "RemoteBranchVerificationError",
    "PullRequestCreationError",
    "PullRequestVerificationError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "RemoteBranchCollisionError",
    "DuplicatePullRequestError",
    # Policy & Authorization
    "GitPublicationPolicy",
    "is_protected_branch",
    "validate_branch_name",
    "validate_remote_url",
    "scan_for_secrets",
    "ACTION_AUTHORIZATION_MAP",
    "get_permitted_publication_actions",
    "is_action_authorized",
    "validate_action_authorization",
    # Components & Providers
    "GitRepositoryInspector",
    "GitPublicationValidator",
    "GitPublicationPlanner",
    "PublicationStore",
    "PublicationWorkspace",
    "GitBranchManager",
    "GitCommitBuilder",
    "PublicationStagingCoordinator",
    "PullRequestFormatter",
    "RemoteBranchPublisher",
    "RemotePublicationCoordinator",
    "GitHubPullRequestProvider",
    "retry_idempotent_operation",
    "parse_repository_owner_and_name",
]
