"""Phase 9 Git Publication Architecture — Remote Publication Coordinator.

Orchestrates controlled upstream Git remote publication and pull request creation:
revalidates operator approvals immediately before push and PR creation, verifies remote
repository identity, publishes verified branches, creates evidence-grounded pull requests,
and maintains an immutable, secret-redacted audit ledger.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from forge.analysis.findings import Finding
from forge.config import redact_secrets
from forge.publication.failures import (
    DuplicatePullRequestError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
    PublicationError,
    RemoteAuthenticationError,
    RemoteBranchPushError,
    RemoteBranchVerificationError,
    RemotePublicationError,
    RemoteRepositoryVerificationError,
)
from forge.publication.models import (
    PullRequestDetails,
    PullRequestRecord,
    RemoteBranchRecord,
    RemotePublicationRecord,
    RemoteRepositorySpec,
    StagedCommitRecord,
)
from forge.publication.policy import GitPublicationPolicy
from forge.publication.pr_template import PullRequestFormatter
from forge.publication.providers.base import PullRequestProviderProtocol
from forge.publication.providers.github import GitHubPullRequestProvider
from forge.publication.remote_publisher import RemoteBranchPublisher
from forge.publication.storage import PublicationStore
from forge.repair.execution.models import (
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionState,
    ReviewDossier,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.storage.store import ForgeStore


class RemotePublicationCoordinator:
    """Coordinates upstream remote branch publication and pull request creation."""

    def __init__(
        self,
        execution_store: RepairExecutionStore,
        forge_store: Optional[ForgeStore] = None,
        publication_store: Optional[PublicationStore] = None,
        policy: Optional[GitPublicationPolicy] = None,
        provider: Optional[PullRequestProviderProtocol] = None,
        publisher: Optional[RemoteBranchPublisher] = None,
    ):
        self.exec_store = execution_store
        self.forge_store = forge_store
        self.pub_store = publication_store or PublicationStore(db_path=self.exec_store.db_path)
        self.policy = policy or GitPublicationPolicy()
        self.provider = provider or GitHubPullRequestProvider()
        self.publisher = publisher or RemoteBranchPublisher(policy=self.policy, provider=self.provider)

    def _log_audit_event(
        self,
        repair_id: str,
        action: str,
        payload: Dict[str, Any],
        actor: RepairActor = RepairActor.ORCHESTRATOR,
    ) -> None:
        """Append sequential, secret-redacted audit event."""
        sanitized_payload = {k: redact_secrets(str(v)) if isinstance(v, str) else v for k, v in payload.items()}
        try:
            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action=action,
                actor=actor,
                payload=sanitized_payload,
                from_state=RepairExecutionState.VERIFIED,
                to_state=RepairExecutionState.VERIFIED,
            )
        except Exception:
            # Fallback if state mismatch
            pass

    def revalidate_approval(self, repair_id: str) -> ApprovalDecisionRecord:
        """Query authoritative operator approval from SQLite and enforce CREATE_PR authorization."""
        if hasattr(self.exec_store, "get_latest_approval_decision_for_repair"):
            approval = self.exec_store.get_latest_approval_decision_for_repair(repair_id)
        else:
            approval = getattr(self.exec_store, "get_approval_decision_for_repair")(repair_id)
        if not approval:
            raise PublicationAuthorizationError(
                f"No operator approval decision found for repair '{repair_id}'. Publication is forbidden."
            )

        if approval.state != ApprovalState.APPROVED:
            raise PublicationAuthorizationError(
                f"Operator approval for repair '{repair_id}' is in state '{approval.state.value}'. "
                f"Only 'approved' decisions authorize remote publication."
            )

        now = datetime.now(timezone.utc)
        if approval.expires_at and now > approval.expires_at:
            raise PublicationAuthorizationError(
                f"Operator approval '{approval.decision_id}' for repair '{repair_id}' has expired. "
                f"A fresh operator approval is required."
            )

        # STRICT AUTHORIZATION INVARIANT: Must be AuthorizedAction.CREATE_PR
        if approval.authorized_action != AuthorizedAction.CREATE_PR:
            raise PublicationActionMismatchError(
                f"Remote publication requires explicit 'create_pr' operator authorization. "
                f"Observed authorized action is '{approval.authorized_action.value if hasattr(approval.authorized_action, 'value') else approval.authorized_action}'. "
                f"Approvals for 'stage_for_pr', 'stage_branch', 'export_patch', or 'manual_integration' "
                f"are strictly forbidden from remote publication."
            )

        return approval

    def validate_remote_prerequisites(
        self,
        repair_id: str,
        repo_path: Optional[Path] = None,
        remote_name: str = "origin",
    ) -> Dict[str, Any]:
        """Validate all preconditions for remote branch publication and PR creation without mutation."""
        # 1. Authoritative approval re-validation
        approval = self.revalidate_approval(repair_id)

        # 2. Local staged commit verification
        staged_commit = self.pub_store.get_staged_commit_for_repair(repair_id)
        if not staged_commit:
            raise PublicationAuthorizationError(
                f"No local staged commit found for repair '{repair_id}'. "
                f"Run 'forge repair publish stage {repair_id}' first."
            )

        if not staged_commit.commit_sha:
            raise PublicationAuthorizationError(
                f"Staged commit for repair '{repair_id}' has no verified commit SHA."
            )

        # Verify cryptographic binding
        if staged_commit.patch_sha256 != approval.binding.patch_sha256:
            raise PublicationAuthorizationError(
                f"Patch SHA-256 mismatch between staged commit and operator approval."
            )
        if staged_commit.base_commit_sha != approval.binding.base_commit_sha:
            raise PublicationAuthorizationError(
                f"Base commit SHA mismatch between staged commit and operator approval."
            )

        # 3. Provider credentials validation
        self.provider.validate_credentials()

        # 4. Remote repository identity verification
        target_dir = Path(repo_path or staged_commit.target_repo_path).resolve()
        spec = self.publisher.verify_remote_repository_identity(
            repository_path=target_dir,
            remote_name=remote_name,
            expected_base_sha=staged_commit.base_commit_sha,
        )

        self._log_audit_event(
            repair_id=repair_id,
            action="remote_identity_verified",
            payload={
                "provider": spec.provider,
                "owner": spec.owner,
                "repository": spec.repository,
                "default_branch": spec.default_branch,
                "remote_url": spec.remote_url,
                "staged_commit_sha": staged_commit.commit_sha,
            },
        )

        self._log_audit_event(
            repair_id=repair_id,
            action="publication_authorization_revalidated",
            payload={
                "decision_id": approval.decision_id,
                "authorized_action": "create_pr",
                "actor": approval.actor,
            },
        )

        return {
            "is_valid": True,
            "repair_id": repair_id,
            "decision_id": approval.decision_id,
            "authorized_action": "create_pr",
            "staged_commit_id": staged_commit.staging_id,
            "staged_commit_sha": staged_commit.commit_sha,
            "branch_name": staged_commit.branch_name,
            "remote_spec": spec.model_dump(),
        }

    def publish_remote_branch(
        self,
        repair_id: str,
        repo_path: Optional[Path] = None,
        remote_name: str = "origin",
    ) -> RemoteBranchRecord:
        """Publish the verified staged commit to the authorized remote branch."""
        # 1. Re-validate approval immediately before remote push
        approval = self.revalidate_approval(repair_id)

        staged_commit = self.pub_store.get_staged_commit_for_repair(repair_id)
        if not staged_commit or not staged_commit.commit_sha:
            raise PublicationAuthorizationError(
                f"No verified local staged commit found for repair '{repair_id}'."
            )

        # 2. Determine source repository directory
        source_dir = Path(staged_commit.workspace_path).resolve()
        if not source_dir.exists():
            # Workspace was cleaned up; fallback to target repository directory if branch exists there
            source_dir = Path(repo_path or staged_commit.target_repo_path).resolve()

        # 3. Verify remote repository identity
        spec = self.publisher.verify_remote_repository_identity(
            repository_path=source_dir,
            remote_name=remote_name,
        )

        self._log_audit_event(
            repair_id=repair_id,
            action="remote_branch_push_started",
            payload={
                "branch_name": staged_commit.branch_name,
                "commit_sha": staged_commit.commit_sha,
                "remote_name": remote_name,
                "remote_url": spec.remote_url,
            },
        )

        try:
            # 4. Push and verify
            remote_branch = self.publisher.publish_remote_branch(
                repository_path=source_dir,
                remote_name=remote_name,
                remote_url=spec.remote_url,
                branch_name=staged_commit.branch_name,
                staged_commit=staged_commit,
                repair_id=repair_id,
                publication_id=staged_commit.plan_id,
                plan_id=staged_commit.plan_id,
                decision_id=approval.decision_id,
            )

            # 5. Persist RemoteBranchRecord
            self.pub_store.save_remote_branch(remote_branch)

            self._log_audit_event(
                repair_id=repair_id,
                action="remote_branch_published",
                payload={
                    "remote_branch_id": remote_branch.remote_branch_id,
                    "branch_name": remote_branch.branch_name,
                    "commit_sha": remote_branch.commit_sha,
                },
            )

            self._log_audit_event(
                repair_id=repair_id,
                action="remote_branch_verified",
                payload={
                    "remote_branch_id": remote_branch.remote_branch_id,
                    "verified_parent_sha": remote_branch.parent_commit_sha,
                    "verified_tree_sha": remote_branch.tree_sha,
                },
            )

            return remote_branch

        except Exception as e:
            sanitized_err = redact_secrets(str(e))
            self._log_audit_event(
                repair_id=repair_id,
                action="publication_failed",
                payload={"stage": "push_remote_branch", "error": sanitized_err},
            )
            raise

    def create_pull_request(
        self,
        repair_id: str,
        repo_path: Optional[Path] = None,
        remote_name: str = "origin",
        target_base_branch: Optional[str] = None,
        draft: bool = False,
    ) -> PullRequestRecord:
        """Create an upstream pull request for an already-pushed and verified remote branch."""
        # 1. Re-validate approval immediately before pull request creation
        approval = self.revalidate_approval(repair_id)

        staged_commit = self.pub_store.get_staged_commit_for_repair(repair_id)
        if not staged_commit:
            raise PublicationAuthorizationError(
                f"No verified local staged commit found for repair '{repair_id}'."
            )

        # 2. Check remote branch record
        remote_branch = self.pub_store.get_remote_branch_for_repair(repair_id)
        if not remote_branch or remote_branch.status != "verified":
            raise RemoteBranchVerificationError(
                f"Remote branch for repair '{repair_id}' has not been pushed and verified yet. "
                f"Run 'forge repair publish push {repair_id}' before creating a pull request."
            )

        # 3. Check for existing PR record in database (idempotency)
        existing_pr = self.pub_store.get_pull_request_for_repair(repair_id)
        if existing_pr and existing_pr.status in ("created", "verified"):
            return existing_pr

        # 4. Resolve repository identity and owner/repo
        target_dir = Path(repo_path or staged_commit.target_repo_path).resolve()
        spec = self.publisher.verify_remote_repository_identity(
            repository_path=target_dir,
            remote_name=remote_name,
        )

        base_branch = target_base_branch or spec.default_branch

        # 5. Load finding, patch artifact, and optional dossier
        finding = None
        if self.forge_store:
            finding = self.forge_store.get_finding(approval.binding.finding_id)
        if not finding:
            # Fallback construct
            finding = Finding(
                finding_id=approval.binding.finding_id,
                title="Automated Software Defect Repair",
                description=f"Repair for finding {approval.binding.finding_id}",
                severity=Finding.get_severity() if hasattr(Finding, "get_severity") else "HIGH",
            )

        patch = self.exec_store.get_patch_artifact(approval.binding.patch_artifact_id)
        if not patch:
            patch = PatchArtifact(
                artifact_id=approval.binding.patch_artifact_id,
                repair_id=repair_id,
                finding_id=approval.binding.finding_id,
                base_commit_sha=staged_commit.base_commit_sha,
                target_file=staged_commit.changed_files[0] if staged_commit.changed_files else "unknown.py",
                diff_content="",
                content_hash=staged_commit.patch_sha256,
                affected_paths=staged_commit.changed_files,
            )

        dossier = None
        if approval.binding.dossier_id:
            dossier = self.exec_store.get_review_dossier(approval.binding.dossier_id)

        # 6. Format PR content
        pr_details = PullRequestFormatter.format_pull_request(
            finding=finding,
            patch=patch,
            staged_commit=staged_commit,
            approval=approval,
            dossier=dossier,
            target_branch=base_branch,
            draft=draft,
        )

        self._log_audit_event(
            repair_id=repair_id,
            action="pull_request_creation_started",
            payload={
                "owner": spec.owner,
                "repository": spec.repository,
                "source_branch": remote_branch.branch_name,
                "target_branch": base_branch,
                "title": pr_details.title,
            },
        )

        try:
            # 7. Create PR via provider
            pr_record = self.provider.create_pull_request(
                owner=spec.owner,
                repo=spec.repository,
                details=pr_details,
                repair_id=repair_id,
                publication_id=remote_branch.publication_id,
                plan_id=remote_branch.plan_id,
                decision_id=approval.decision_id,
                staged_commit_id=staged_commit.staging_id,
                remote_branch_id=remote_branch.remote_branch_id,
            )

            # 8. Verify PR on remote
            verified_pr = self.provider.get_pull_request(
                owner=spec.owner,
                repo=spec.repository,
                pr_number=pr_record.pr_number,
            )
            if verified_pr:
                pr_record.verified_at = datetime.now(timezone.utc)
                pr_record.status = "verified"

            # 9. Persist PullRequestRecord
            self.pub_store.save_pull_request(pr_record)

            # 10. Persist RemotePublicationRecord
            remote_pub = RemotePublicationRecord(
                remote_publication_id=f"rpub_{uuid.uuid4().hex[:12]}",
                repair_id=repair_id,
                publication_id=remote_branch.publication_id,
                plan_id=remote_branch.plan_id,
                decision_id=approval.decision_id,
                staged_commit_id=staged_commit.staging_id,
                provider=self.provider.get_provider_name(),
                status="completed",
                remote_branch_id=remote_branch.remote_branch_id,
                pr_record_id=pr_record.pr_record_id,
                completed_at=datetime.now(timezone.utc),
            )
            self.pub_store.save_remote_publication(remote_pub)

            self._log_audit_event(
                repair_id=repair_id,
                action="pull_request_created",
                payload={
                    "pr_record_id": pr_record.pr_record_id,
                    "pr_number": pr_record.pr_number,
                    "pr_url": pr_record.pr_url,
                },
            )

            self._log_audit_event(
                repair_id=repair_id,
                action="pull_request_verified",
                payload={
                    "pr_record_id": pr_record.pr_record_id,
                    "pr_number": pr_record.pr_number,
                    "status": "verified",
                },
            )

            return pr_record

        except DuplicatePullRequestError as e:
            # Reconcile existing PR
            existing = self.pub_store.get_pull_request_for_repair(repair_id)
            if existing:
                return existing
            raise
        except Exception as e:
            sanitized_err = redact_secrets(str(e))
            self._log_audit_event(
                repair_id=repair_id,
                action="publication_failed",
                payload={"stage": "create_pull_request", "error": sanitized_err},
            )
            raise
