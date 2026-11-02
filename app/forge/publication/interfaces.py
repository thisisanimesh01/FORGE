"""Phase 9 Git Publication Architecture — Interfaces and Protocols.

Defines runtime-checkable protocols separating Git integration contracts
from concrete implementations.
"""

from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

from forge.analysis.findings import Finding
from forge.publication.models import (
    BranchProposal,
    CommitProposal,
    PublicationAction,
    PublicationPlan,
    PublicationRequest,
    PublicationValidationResult,
    RepositoryIdentity,
)
from forge.repair.execution.models import (
    ApprovalDecisionRecord,
    PatchArtifact,
    ReviewDossier,
)


@runtime_checkable
class GitRepositoryInspectorProtocol(Protocol):
    """Protocol for safely inspecting target Git repository identity and status."""

    def inspect_repository(self, repository_path: Path) -> RepositoryIdentity:
        """Inspect and return verified identity snapshot of the target Git repository."""
        ...

    def check_working_tree_clean(self, repository_path: Path) -> bool:
        """Return True if working tree has zero uncommitted or untracked changes."""
        ...

    def resolve_head_commit(self, repository_path: Path) -> str:
        """Resolve current HEAD commit SHA."""
        ...

    def get_remote_url(self, repository_path: Path, remote_name: str = "origin") -> Optional[str]:
        """Fetch remote URL for configured remote name."""
        ...


@runtime_checkable
class GitBranchManagerProtocol(Protocol):
    """Protocol for branch planning and naming validation without mutation."""

    def plan_branch(
        self,
        base_commit_sha: str,
        finding_id: str,
        patch_sha256: str,
        custom_name: Optional[str] = None,
    ) -> BranchProposal:
        """Formulate a proposed branch name according to publication policy."""
        ...

    def validate_branch_name(self, branch_name: str) -> None:
        """Validate branch name against Git syntax and safety policies."""
        ...

    def branch_exists(self, repository_path: Path, branch_name: str) -> bool:
        """Check whether a branch already exists in local or remote references."""
        ...


@runtime_checkable
class GitCommitBuilderProtocol(Protocol):
    """Protocol for building structured, traceable commit proposals."""

    def plan_commit(
        self,
        finding: Finding,
        patch: PatchArtifact,
        dossier: ReviewDossier,
        approval: ApprovalDecisionRecord,
    ) -> CommitProposal:
        """Construct a structured, secret-scanned commit proposal."""
        ...

    def validate_commit_message(self, message: str) -> None:
        """Validate that commit message complies with policy and contains no secrets."""
        ...


@runtime_checkable
class GitPublicationValidatorProtocol(Protocol):
    """Protocol for validating publication requests against approvals, policy, and repository identity."""

    def validate_publication_request(
        self,
        request: PublicationRequest,
        repo_identity: RepositoryIdentity,
    ) -> PublicationValidationResult:
        """Validate authorization, binding, repository identity, and policy compliance."""
        ...


@runtime_checkable
class PullRequestProviderProtocol(Protocol):
    """Protocol for upstream pull request provider integration."""

    def get_provider_name(self) -> str:
        """Return provider name (e.g. 'github', 'gitlab')."""
        ...

    def validate_pr_prerequisites(self, plan: PublicationPlan) -> bool:
        """Validate whether prerequisites for PR creation are satisfied."""
        ...


@runtime_checkable
class PublicationAuditStoreProtocol(Protocol):
    """Protocol for persistent storage and audit logging of publication operations."""

    def save_publication_request(self, request: PublicationRequest) -> None:
        """Persist or update a PublicationRequest."""
        ...

    def get_publication_request(self, publication_id: str) -> Optional[PublicationRequest]:
        """Fetch a PublicationRequest by ID."""
        ...

    def save_publication_plan(self, plan: PublicationPlan) -> None:
        """Persist a PublicationPlan."""
        ...

    def get_publication_plan(self, plan_id: str) -> Optional[PublicationPlan]:
        """Fetch a PublicationPlan by ID."""
        ...

    def get_publication_plan_for_request(self, publication_id: str) -> Optional[PublicationPlan]:
        """Fetch the PublicationPlan associated with a publication_id."""
        ...

    def list_publication_requests_for_repair(self, repair_id: str) -> List[PublicationRequest]:
        """List all publication requests for a given repair_id."""
        ...
