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
