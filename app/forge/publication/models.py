"""Phase 9 Git Publication Architecture — Domain Models.

Defines typed domain entities for publication requests, repository identity,
commit and branch proposals, publication plans, and validation results.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PublicationAction(str, Enum):
    """Permitted publication actions authorized by operators."""
    EXPORT_PATCH = "export_patch"
    STAGE_BRANCH = "stage_branch"
    CREATE_PR = "create_pr"


class PublicationStatus(str, Enum):
    """Lifecycle state of a publication request."""
    PLANNED = "planned"
    VALIDATING = "validating"
    AUTHORIZED = "authorized"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class RepositoryIdentity(BaseModel):
    """Cryptographic and contextual identity snapshot of a target Git repository."""
    canonical_path: str = Field(..., description="Absolute canonical resolved filesystem path.")
    git_root: str = Field(..., description="Top-level root directory reported by git rev-parse.")
    remote_url: Optional[str] = Field(None, description="Remote origin URL if configured.")
    current_branch: Optional[str] = Field(None, description="Active branch name or HEAD reference.")
    base_commit_sha: str = Field(..., description="Full 40-character commit SHA at repository HEAD.")
    is_clean: bool = Field(..., description="True if working tree and index have zero uncommitted changes.")
    remote_tracking: Optional[str] = Field(None, description="Upstream remote tracking branch ref if configured.")
    untracked_files_count: int = Field(0, description="Count of untracked files in working tree.")
    modified_files_count: int = Field(0, description="Count of modified/staged files in working tree.")
    snapshot_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when repository identity was verified."
    )


class CommitAuthor(BaseModel):
    """Author and committer identity for Git commit records."""
    name: str = Field(..., min_length=1, description="Author full name.")
    email: str = Field(..., min_length=3, description="Author email address.")


class BranchProposal(BaseModel):
    """Proposed branch specification for patch staging."""
    branch_name: str = Field(..., min_length=1, description="Proposed Git branch name.")
    source_commit_sha: str = Field(..., description="Commit SHA from which the branch will be created.")
    target_branch: Optional[str] = Field("main", description="Target base branch intended for future integration.")
    description: Optional[str] = Field(None, description="Human-readable branch purpose.")


class CommitProposal(BaseModel):
    """Proposed commit metadata for patch staging."""
    commit_message: str = Field(..., min_length=1, description="Structured Git commit message.")
    author: CommitAuthor = Field(..., description="Author identity.")
    committer: CommitAuthor = Field(..., description="Committer identity.")
    touched_files: List[str] = Field(default_factory=list, description="List of repository files touched by commit.")
    patch_sha256: str = Field(..., description="Cryptographic SHA-256 digest of the candidate patch.")
    dossier_id: Optional[str] = Field(None, description="Linked Phase 8 Review Dossier identifier.")
    operator_signoff: Optional[str] = Field(None, description="Operator identity who authorized this publication.")


class PublicationPlan(BaseModel):
    """Structured plan for future Git publication operations."""
    plan_id: str = Field(..., description="Unique publication plan ID.")
    publication_id: str = Field(..., description="Linked publication request ID.")
    repair_id: str = Field(..., description="Linked repair execution ID.")
    decision_id: str = Field(..., description="Linked M6 operator approval decision ID.")
    finding_id: str = Field(..., description="Linked defect finding ID.")
    repository_target: str = Field(..., description="Target repository path or URL.")
    base_commit_sha: str = Field(..., description="Pinned base commit SHA.")
    proposed_branch_name: str = Field(..., description="Proposed Git branch name.")
    commit_proposal: CommitProposal = Field(..., description="Proposed commit metadata.")
    patch_artifact_id: str = Field(..., description="Linked patch artifact ID.")
    patch_sha256: str = Field(..., description="Expected patch content SHA-256.")
    authorized_action: PublicationAction = Field(..., description="Operator-authorized action.")
    policy_version: str = Field("1.0.0", description="Active Git publication policy version.")
    repository_identity: RepositoryIdentity = Field(..., description="Verified target repository identity.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Plan formulation timestamp."
    )


class PublicationRequest(BaseModel):
    """Tracked publication request entity."""
    publication_id: str = Field(..., description="Unique publication request ID.")
    repair_id: str = Field(..., description="Target repair execution ID.")
    decision_id: str = Field(..., description="Authorized approval decision ID.")
    action: PublicationAction = Field(..., description="Requested publication action.")
    status: PublicationStatus = Field(default=PublicationStatus.PLANNED, description="Publication request status.")
    plan_id: Optional[str] = Field(None, description="Associated publication plan ID once formulated.")
    error_message: Optional[str] = Field(None, description="Failure diagnostic message if rejected/failed.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Creation timestamp."
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last status update timestamp."
    )


class PublicationValidationResult(BaseModel):
    """Audit result of publication authorization and policy validation."""
    is_valid: bool = Field(..., description="True if publication request is fully authorized and compliant.")
    action: PublicationAction = Field(..., description="Validated publication action.")
    checks_passed: List[str] = Field(default_factory=list, description="List of passed validation checks.")
    violations: List[str] = Field(default_factory=list, description="List of observed policy/authorization violations.")
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Validation evaluation timestamp."
    )


class PublicationExecutionState(str, Enum):
    """Lifecycle state of an isolated branch staging and commit workflow."""
    INITIALIZED = "initialized"
    WORKSPACE_PROVISIONED = "workspace_provisioned"
    BRANCH_CREATED = "branch_created"
    PATCH_APPLIED = "patch_applied"
    COMMITTED = "committed"
    VERIFIED = "verified"
    FAILED = "failed"
    ABORTED = "aborted"
    CLEANED = "cleaned"


class WorkspaceIntegritySnapshot(BaseModel):
    """Integrity assessment of publication workspace against baseline."""
    baseline_file_count: int = Field(0, description="Total baseline files fingerprinted.")
    modified_files: List[str] = Field(default_factory=list, description="Files with modified content.")
    added_files: List[str] = Field(default_factory=list, description="Unexpected added files.")
    removed_files: List[str] = Field(default_factory=list, description="Unexpected removed files.")
    is_contained: bool = Field(True, description="True if all operations were contained inside workspace.")
    target_repo_immutable: bool = Field(True, description="True if target repo remained unmutated.")


class StagedCommitRecord(BaseModel):
    """Persisted record of an approved, locally staged Git commit in an isolated workspace."""
    staging_id: str = Field(..., description="Unique staging identifier.")
    workspace_id: str = Field(..., description="Isolated publication workspace identifier.")
    plan_id: str = Field(..., description="Associated publication plan ID.")
    repair_id: str = Field(..., description="Associated repair ID.")
    decision_id: str = Field(..., description="Authorizing operator decision ID.")
    finding_id: str = Field(..., description="Associated defect finding ID.")
    branch_name: str = Field(..., description="Staged Git branch name.")
    base_commit_sha: str = Field(..., description="Parent base commit revision.")
    commit_sha: Optional[str] = Field(None, description="SHA of created commit.")
    tree_sha: Optional[str] = Field(None, description="SHA of created commit tree.")
    parent_commit_sha: Optional[str] = Field(None, description="Verified parent commit SHA.")
    patch_artifact_id: str = Field(..., description="Applied patch artifact ID.")
    patch_sha256: str = Field(..., description="SHA-256 digest of applied patch.")
    changed_files: List[str] = Field(default_factory=list, description="Exact files modified in commit.")
    author: CommitAuthor = Field(..., description="Commit author.")
    committer: CommitAuthor = Field(..., description="Commit committer.")
    commit_message: str = Field(..., description="Exact commit message.")
    state: PublicationExecutionState = Field(
        default=PublicationExecutionState.INITIALIZED,
        description="Current execution state."
    )
    failure_reason: Optional[str] = Field(None, description="Diagnostic error message if failed.")
    workspace_path: str = Field(..., description="Filesystem path of publication workspace.")
    target_repo_path: str = Field(..., description="Original target repository path.")
    workspace_integrity: Optional[WorkspaceIntegritySnapshot] = Field(
        None,
        description="Integrity snapshot recorded after commit creation."
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Record creation timestamp."
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last update timestamp."
    )


class RemoteRepositorySpec(BaseModel):
    """Verified specification of an upstream remote Git repository."""
    provider: str = Field(..., description="Hosting provider name (e.g. 'github').")
    owner: str = Field(..., description="Repository owner or organization account.")
    repository: str = Field(..., description="Repository project name.")
    default_branch: str = Field(default="main", description="Target base branch on remote.")
    remote_url: str = Field(..., description="Sanitized remote clone/push URL.")
    is_private: bool = Field(False, description="True if repository is private.")
    can_push: bool = Field(True, description="True if push access is permitted with current credentials.")


class PullRequestDetails(BaseModel):
    """Detailed payload specification for creating a pull request."""
    title: str = Field(..., min_length=1, description="Pull request title.")
    body: str = Field(..., min_length=1, description="Pull request markdown body description.")
    target_branch: str = Field(..., description="Upstream base branch to merge into.")
    source_branch: str = Field(..., description="Head branch containing the staged commit.")
    draft: bool = Field(False, description="Whether to open as draft pull request.")


class RemoteBranchRecord(BaseModel):
    """Persisted record of an approved, published and verified remote Git branch."""
    remote_branch_id: str = Field(..., description="Unique remote branch record identifier.")
    repair_id: str = Field(..., description="Associated repair execution ID.")
    publication_id: str = Field(..., description="Associated publication request ID.")
    plan_id: str = Field(..., description="Associated publication plan ID.")
    staged_commit_id: str = Field(..., description="Associated local staged commit ID.")
    decision_id: str = Field(..., description="Authorizing operator decision ID.")
    remote_name: str = Field("origin", description="Git remote name used for push.")
    remote_url: str = Field(..., description="Sanitized remote repository URL.")
    branch_name: str = Field(..., description="Remote branch name.")
    commit_sha: str = Field(..., description="Published commit SHA on remote.")
    parent_commit_sha: str = Field(..., description="Verified parent commit SHA on remote.")
    tree_sha: str = Field(..., description="Verified tree SHA on remote.")
    patch_sha256: str = Field(..., description="SHA-256 digest of patch published in commit.")
    pushed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when branch was pushed."
    )
    verified_at: Optional[datetime] = Field(None, description="Timestamp when remote branch was verified.")
    status: str = Field(default="published", description="Status ('published', 'verified', 'failed').")
    verification_details: Dict[str, Any] = Field(default_factory=dict, description="Verification audit facts.")


class PullRequestRecord(BaseModel):
    """Persisted record of an opened and verified upstream pull request."""
    pr_record_id: str = Field(..., description="Unique pull request record identifier.")
    repair_id: str = Field(..., description="Associated repair execution ID.")
    publication_id: str = Field(..., description="Associated publication request ID.")
    plan_id: str = Field(..., description="Associated publication plan ID.")
    decision_id: str = Field(..., description="Authorizing operator decision ID.")
    staged_commit_id: str = Field(..., description="Associated local staged commit ID.")
    remote_branch_id: str = Field(..., description="Associated remote branch record ID.")
    provider: str = Field("github", description="Hosting provider name.")
    repository_owner: str = Field(..., description="Target repository owner.")
    repository_name: str = Field(..., description="Target repository name.")
    target_branch: str = Field(..., description="Base branch on upstream repository.")
    source_branch: str = Field(..., description="Head branch published to upstream repository.")
    pr_number: int = Field(..., description="Provider-assigned pull request number.")
    pr_url: str = Field(..., description="Canonical web URL of created pull request.")
    pr_title: str = Field(..., description="Title of created pull request.")
    pr_body: str = Field(..., description="Body of created pull request.")
    pr_state: str = Field(default="open", description="State on provider ('open', 'closed', 'merged').")
    is_draft: bool = Field(False, description="Whether opened as draft.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when pull request was created."
    )
    verified_at: Optional[datetime] = Field(None, description="Timestamp when pull request was verified on remote.")
    status: str = Field(default="created", description="Record status ('created', 'verified', 'failed').")


class ProviderOperationRecord(BaseModel):
    """Audit record of an individual operation performed against a Git hosting provider."""
    operation_id: str = Field(..., description="Unique provider operation ID.")
    repair_id: str = Field(..., description="Associated repair execution ID.")
    provider: str = Field(..., description="Target provider ('github', etc.).")
    operation_type: str = Field(..., description="Type of operation ('verify_remote', 'push', 'create_pr', etc.).")
    idempotency_key: str = Field(..., description="Deterministic key preventing duplicate remote operations.")
    status: str = Field(default="pending", description="Operation status ('pending', 'success', 'failed').")
    request_payload: Dict[str, Any] = Field(default_factory=dict, description="Sanitized request payload.")
    response_payload: Dict[str, Any] = Field(default_factory=dict, description="Sanitized provider response.")
    error_message: Optional[str] = Field(None, description="Diagnostic error message if failed.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when operation was performed."
    )


class RemotePublicationRecord(BaseModel):
    """High-level lifecycle record tracking end-to-end remote publication for a repair."""
    remote_publication_id: str = Field(..., description="Unique remote publication lifecycle ID.")
    repair_id: str = Field(..., description="Associated repair execution ID.")
    publication_id: str = Field(..., description="Associated publication request ID.")
    plan_id: str = Field(..., description="Associated publication plan ID.")
    decision_id: str = Field(..., description="Authorizing operator decision ID.")
    staged_commit_id: str = Field(..., description="Associated staged commit ID.")
    provider: str = Field(default="github", description="Target provider name.")
    status: str = Field(
        default="pending",
        description="Status ('pending', 'branch_pushed', 'pr_created', 'completed', 'failed', 'aborted')."
    )
    remote_branch_id: Optional[str] = Field(None, description="Linked RemoteBranchRecord ID once pushed.")
    pr_record_id: Optional[str] = Field(None, description="Linked PullRequestRecord ID once created.")
    failure_classification: Optional[str] = Field(None, description="Categorized failure classification if failed.")
    failure_reason: Optional[str] = Field(None, description="Detailed diagnostic reason if failed.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Publication initiation timestamp."
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last status update timestamp."
    )
    completed_at: Optional[datetime] = Field(None, description="Completion timestamp.")
