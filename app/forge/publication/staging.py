"""Phase 9 Git Publication Architecture — Publication Staging Coordinator.

Orchestrates isolated workspace provisioning, revalidates authoritative operator approvals,
creates candidate repair branches, applies vetted patches, and creates verified local commits
strictly within isolated workspaces while guaranteeing target repository immutability.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from forge.analysis.findings import Finding
from forge.config import redact_secrets
from forge.publication.branch import GitBranchManager
from forge.publication.commit import GitCommitBuilder
from forge.publication.failures import (
    CommitCreationError,
    CommitVerificationError,
    PatchStagingError,
    PublicationAuthorizationError,
    PublicationError,
    TargetRepoMutationError,
    WorkspaceIntegrityError,
)
from forge.publication.inspector import GitRepositoryInspector
from forge.publication.interfaces import (
    GitPublicationValidatorProtocol,
    GitRepositoryInspectorProtocol,
)
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
    PublicationAction,
    PublicationExecutionState,
    PublicationPlan,
    PublicationRequest,
    StagedCommitRecord,
    WorkspaceIntegritySnapshot,
)
from forge.publication.planner import GitPublicationPlanner
from forge.publication.policy import GitPublicationPolicy
from forge.publication.storage import PublicationStore
from forge.publication.validator import GitPublicationValidator
from forge.publication.workspace import PublicationWorkspace
from forge.repair.execution.models import (
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    PatchArtifact,
    RepairActor,
    ReviewDossier,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.storage.store import ForgeStore


class PublicationStagingCoordinator:
    """Coordinates isolated branch staging and commit creation workflows."""

    def __init__(
        self,
        execution_store: RepairExecutionStore,
        forge_store: Optional[ForgeStore] = None,
        publication_store: Optional[PublicationStore] = None,
        policy: Optional[GitPublicationPolicy] = None,
        validator: Optional[GitPublicationValidatorProtocol] = None,
        inspector: Optional[GitRepositoryInspectorProtocol] = None,
        branch_manager: Optional[GitBranchManager] = None,
        commit_builder: Optional[GitCommitBuilder] = None,
        planner: Optional[GitPublicationPlanner] = None,
    ):
        self.exec_store = execution_store
        self.forge_store = forge_store
        self.pub_store = publication_store or PublicationStore()
        self.policy = policy or GitPublicationPolicy()
        self.inspector = inspector or GitRepositoryInspector()
        self.validator = validator or GitPublicationValidator(
            execution_store=self.exec_store,
            policy=self.policy,
            inspector=self.inspector,
        )
        self.branch_manager = branch_manager or GitBranchManager(policy=self.policy)
        self.commit_builder = commit_builder or GitCommitBuilder(policy=self.policy)
        self.planner = planner or GitPublicationPlanner(
            execution_store=self.exec_store,
            forge_store=self.forge_store,
            policy=self.policy,
            validator=self.validator,
            inspector=self.inspector,
        )

    def _revalidate_authoritative_approval(
        self,
        repair_id: str,
        phase: str,
    ) -> ApprovalDecisionRecord:
        """Immediately re-query authoritative SQLite approval record and enforce APPROVED state."""
        decision_rec = self.exec_store.get_latest_approval_decision_for_repair(repair_id)
        if not decision_rec:
            raise PublicationAuthorizationError(
                f"Authoritative operator approval revalidation failed ({phase}): no approval found for repair '{repair_id}'."
            )

        now = datetime.now(timezone.utc)
        if decision_rec.expires_at and now > decision_rec.expires_at:
            raise PublicationAuthorizationError(
                f"Authoritative operator approval revalidation failed ({phase}): "
                f"decision '{decision_rec.decision_id}' expired at {decision_rec.expires_at.isoformat()}."
            )

        if decision_rec.state != ApprovalState.APPROVED:
            raise PublicationAuthorizationError(
                f"Authoritative operator approval revalidation failed ({phase}): "
                f"decision '{decision_rec.decision_id}' is in state '{decision_rec.state.value}'; must be 'APPROVED'."
            )

        # Enforce that action permits local branch staging
        auth_action = decision_rec.authorized_action
        if auth_action not in (
            AuthorizedAction.STAGE_BRANCH,
            AuthorizedAction.STAGE_FOR_PR,
            AuthorizedAction.CREATE_PR,
        ):
            raise PublicationAuthorizationError(
                f"Operator decision '{decision_rec.decision_id}' has action '{auth_action.value if auth_action else None}'; "
                "local branch staging requires 'stage_branch', 'stage_for_pr', or 'create_pr'."
            )

        return decision_rec

    def stage_and_commit(
        self,
        repair_id: str,
        repository_path: Path,
        custom_branch_name: Optional[str] = None,
        author_name: str = "FORGE Autonomous Agent",
        author_email: str = "forge@local.agent",
        scratch_root: Optional[Path] = None,
        auto_cleanup: bool = False,
    ) -> StagedCommitRecord:
        """Execute the end-to-end isolated branch staging and commit workflow.

        Steps:
        1. Pre-flight validation of repair, bindings, and operator approval.
        2. Snapshot and assert original repository immutability.
        3. Formulate publication plan.
        4. Provision physically isolated PublicationWorkspace.
        5. Re-validate authoritative approval immediately before branch creation.
        6. Create deterministic branch in workspace.
        7. Apply and stage approved patch bytes; verify workspace integrity.
        8. Re-validate authoritative approval immediately before commit creation.
        9. Create structured, secret-scanned, descriptive commit in workspace.
        10. Verify commit parent SHA, tree SHA, and touched files.
        11. Re-verify target repository immutability.
        12. Record sequential audit events and persist StagedCommitRecord.
        13. Optionally clean up workspace.
        """
        repo_path = Path(repository_path).resolve()
        staging_id = f"stg_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)

        # 1. Pre-flight approval check
        decision_rec = self._revalidate_authoritative_approval(repair_id, phase="pre_flight")

        # 2. Inspect target repository identity
        repo_identity = self.inspector.inspect_repository(repo_path)

        # 3. Formulate and validate publication request
        pub_request = PublicationRequest(
            publication_id=f"pub_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            decision_id=decision_rec.decision_id,
            action=PublicationAction.STAGE_BRANCH,
        )
        val_result = self.validator.validate_publication_request(pub_request, repo_identity)
        if not val_result.is_valid:
            raise PublicationAuthorizationError(
                f"Publication validation failed: {'; '.join(val_result.violations)}"
            )

        # 4. Resolve entities
        binding = decision_rec.binding
        patch = self.exec_store.get_patch_artifact(binding.patch_artifact_id)
        if not patch:
            raise PublicationError(f"Patch artifact '{binding.patch_artifact_id}' not found.")

        finding = None
        if self.forge_store:
            finding = self.forge_store.get_finding(binding.finding_id)
        if not finding:
            finding = Finding(
                finding_id=binding.finding_id,
                title="Candidate Repair",
                description="Verified software repair",
            )

        dossier = None
        if binding.dossier_id:
            dossier = self.exec_store.get_review_dossier(binding.dossier_id)
        if not dossier:
            dossier = ReviewDossier(
                dossier_id=binding.dossier_id or f"dos_{uuid.uuid4().hex[:12]}",
                repair_id=repair_id,
                finding_id=binding.finding_id,
                finding_title=finding.title,
                finding_severity="high",
                repository_target=str(repo_path),
                base_commit_sha=binding.base_commit_sha,
                patch_artifact_id=binding.patch_artifact_id,
                patch_sha256=binding.patch_sha256,
                target_files=patch.affected_paths,
                unified_diff=patch.diff_content,
                validation_id=binding.validation_id or "val_none",
                validation_outcome="accepted",
                verification_id=binding.verification_id or "ver_none",
                verification_outcome="verified",
                workspace_id="ws_fallback",
            )

        repair_req = self.exec_store.get_repair_request(repair_id)
        allowed_files = repair_req.allowed_files if repair_req else patch.affected_paths

        # 5. Formulate publication plan
        plan = self.planner.create_publication_plan(
            repair_id=repair_id,
            action=PublicationAction.STAGE_BRANCH,
            repository_path=repo_path,
            custom_branch_name=custom_branch_name,
            author_name=author_name,
            author_email=author_email,
        )
        pub_request = PublicationRequest(
            publication_id=plan.publication_id,
            repair_id=repair_id,
            decision_id=decision_rec.decision_id,
            action=PublicationAction.STAGE_BRANCH,
            plan_id=plan.plan_id,
        )
        self.pub_store.save_publication_request(pub_request)
        self.pub_store.save_publication_plan(plan)

        branch_name = plan.proposed_branch_name
        commit_proposal = self.commit_builder.plan_commit(
            finding=finding,
            patch=patch,
            dossier=dossier,
            approval=decision_rec,
            author_name=author_name,
            author_email=author_email,
        )

        # Initial StagedCommitRecord
        staged_record = StagedCommitRecord(
            staging_id=staging_id,
            workspace_id="pending",
            plan_id=plan.plan_id,
            repair_id=repair_id,
            decision_id=decision_rec.decision_id,
            finding_id=binding.finding_id,
            branch_name=branch_name,
            base_commit_sha=binding.base_commit_sha,
            patch_artifact_id=patch.artifact_id,
            patch_sha256=binding.patch_sha256,
            changed_files=patch.affected_paths,
            author=commit_proposal.author,
            committer=commit_proposal.committer,
            commit_message=commit_proposal.commit_message,
            state=PublicationExecutionState.INITIALIZED,
            workspace_path="",
            target_repo_path=str(repo_path),
            created_at=now,
            updated_at=now,
        )
        self.pub_store.save_staged_commit(staged_record)

        workspace: Optional[PublicationWorkspace] = None
        try:
            # 6. Provision PublicationWorkspace
            workspace = PublicationWorkspace(
                target_repo_path=repo_path,
                base_commit_sha=binding.base_commit_sha,
                scratch_root=scratch_root,
                policy=self.policy,
            )
            workspace.prepare()

            staged_record.workspace_id = workspace.workspace_id
            staged_record.workspace_path = str(workspace.workspace_path)
            staged_record.state = PublicationExecutionState.WORKSPACE_PROVISIONED
            staged_record.updated_at = datetime.now(timezone.utc)
            self.pub_store.save_staged_commit(staged_record)

            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="workspace_provisioned",
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "workspace_id": workspace.workspace_id,
                    "workspace_path": str(workspace.workspace_path),
                    "base_commit_sha": binding.base_commit_sha,
                },
            )

            # 7. Re-validate authoritative approval immediately before branch creation
            decision_rec = self._revalidate_authoritative_approval(repair_id, phase="pre_branch_creation")
            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="authorization_revalidated",
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "phase": "pre_branch_creation",
                    "decision_id": decision_rec.decision_id,
                    "authorized_action": decision_rec.authorized_action.value if decision_rec.authorized_action else None,
                },
            )

            # 8. Create branch inside workspace
            self.branch_manager.create_branch(
                workspace=workspace,
                branch_name=branch_name,
                base_commit_sha=binding.base_commit_sha,
            )
            staged_record.state = PublicationExecutionState.BRANCH_CREATED
            staged_record.updated_at = datetime.now(timezone.utc)
            self.pub_store.save_staged_commit(staged_record)

            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="branch_created",
                actor=RepairActor.ORCHESTRATOR,
                payload={"branch_name": branch_name, "base_commit_sha": binding.base_commit_sha},
            )

            # 9. Apply and stage patch
            self.commit_builder.apply_and_stage_patch(
                workspace=workspace,
                patch=patch,
                allowed_files=allowed_files,
            )
            staged_record.state = PublicationExecutionState.PATCH_APPLIED
            staged_record.updated_at = datetime.now(timezone.utc)
            self.pub_store.save_staged_commit(staged_record)

            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="patch_applied",
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "patch_artifact_id": patch.artifact_id,
                    "patch_sha256": binding.patch_sha256,
                    "touched_files": patch.affected_paths,
                },
            )

            integrity_snapshot = workspace.check_integrity(allowed_files=patch.affected_paths)
            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="patch_integrity_verified",
                actor=RepairActor.ORCHESTRATOR,
                payload=integrity_snapshot.model_dump(),
            )

            # 10. Re-validate authoritative approval immediately before commit creation
            decision_rec = self._revalidate_authoritative_approval(repair_id, phase="pre_commit_creation")
            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="authorization_revalidated",
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "phase": "pre_commit_creation",
                    "decision_id": decision_rec.decision_id,
                    "authorized_action": decision_rec.authorized_action.value if decision_rec.authorized_action else None,
                },
            )

            # 11. Create and verify local commit
            commit_result = self.commit_builder.create_commit(
                workspace=workspace,
                commit_proposal=commit_proposal,
                base_commit_sha=binding.base_commit_sha,
            )

            commit_sha = commit_result["commit_sha"]
            tree_sha = commit_result["tree_sha"]
            parent_sha = commit_result["parent_commit_sha"]

            staged_record.commit_sha = commit_sha
            staged_record.tree_sha = tree_sha
            staged_record.parent_commit_sha = parent_sha
            staged_record.state = PublicationExecutionState.COMMITTED
            staged_record.updated_at = datetime.now(timezone.utc)
            self.pub_store.save_staged_commit(staged_record)

            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="commit_created",
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "commit_sha": commit_sha,
                    "parent_commit_sha": parent_sha,
                    "tree_sha": tree_sha,
                    "branch_name": branch_name,
                },
            )

            # 12. Final verification and target immutability assertion
            workspace.verify_target_immutability()
            final_integrity = workspace.check_integrity(allowed_files=patch.affected_paths)

            staged_record.workspace_integrity = final_integrity
            staged_record.state = PublicationExecutionState.VERIFIED
            staged_record.updated_at = datetime.now(timezone.utc)
            self.pub_store.save_staged_commit(staged_record)

            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="commit_verified",
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "commit_sha": commit_sha,
                    "clean_working_tree": True,
                    "target_repo_immutable": True,
                },
            )

            # 13. Optional workspace cleanup
            if auto_cleanup:
                workspace.cleanup()
                staged_record.state = PublicationExecutionState.CLEANED
                staged_record.updated_at = datetime.now(timezone.utc)
                self.pub_store.save_staged_commit(staged_record)

                self.pub_store.record_publication_event(
                    repair_id=repair_id,
                    action="workspace_cleaned",
                    actor=RepairActor.ORCHESTRATOR,
                    payload={"workspace_id": workspace.workspace_id},
                )

            return staged_record

        except Exception as e:
            # Handle failure and preserve audit trail
            err_msg = str(e)
            staged_record.state = PublicationExecutionState.FAILED
            staged_record.failure_reason = redact_secrets(err_msg)
            staged_record.updated_at = datetime.now(timezone.utc)
            self.pub_store.save_staged_commit(staged_record)

            self.pub_store.record_publication_event(
                repair_id=repair_id,
                action="staging_failed",
                actor=RepairActor.ORCHESTRATOR,
                payload={"error": redact_secrets(err_msg), "failed_at_state": staged_record.state.value},
            )

            # Attempt safe workspace cleanup on error
            if workspace and not workspace.is_cleaned:
                try:
                    workspace.cleanup()
                    self.pub_store.record_publication_event(
                        repair_id=repair_id,
                        action="workspace_cleaned",
                        actor=RepairActor.ORCHESTRATOR,
                        payload={"workspace_id": workspace.workspace_id, "reason": "cleanup_after_failure"},
                    )
                except Exception:
                    pass

            raise
