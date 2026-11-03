"""Phase 9 Git Publication Architecture — Publication Planner.

Formulates deterministic PublicationPlans for future Git operations without
mutating target repositories or creating remote branches.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from forge.repair.execution.storage import RepairExecutionStore
    from forge.storage.store import ForgeStore

from forge.config import redact_secrets
from forge.publication.failures import (
    ProtectedBranchViolationError,
    PublicationError,
)
from forge.publication.inspector import GitRepositoryInspector
from forge.publication.interfaces import (
    GitPublicationValidatorProtocol,
    GitRepositoryInspectorProtocol,
)
from forge.publication.models import (
    BranchProposal,
    CommitAuthor,
    CommitProposal,
    PublicationAction,
    PublicationPlan,
    PublicationRequest,
    RepositoryIdentity,
)
from forge.publication.policy import (
    GitPublicationPolicy,
    is_protected_branch,
    scan_for_secrets,
    validate_branch_name,
)
from forge.publication.validator import GitPublicationValidator


class GitPublicationPlanner:
    """Formulates and validates publication plans for operator-approved candidate repairs."""

    def __init__(
        self,
        execution_store: "RepairExecutionStore",
        forge_store: Optional["ForgeStore"] = None,
        policy: Optional[GitPublicationPolicy] = None,
        validator: Optional[GitPublicationValidatorProtocol] = None,
        inspector: Optional[GitRepositoryInspectorProtocol] = None,
    ):
        self.exec_store = execution_store
        self.forge_store = forge_store
        self.policy = policy or GitPublicationPolicy()
        self.inspector = inspector or GitRepositoryInspector()
        self.validator = validator or GitPublicationValidator(
            execution_store=self.exec_store,
            policy=self.policy,
            inspector=self.inspector,
        )

    def create_publication_plan(
        self,
        repair_id: str,
        action: PublicationAction,
        repository_path: Path,
        custom_branch_name: Optional[str] = None,
        author_name: str = "FORGE Autonomous Agent",
        author_email: str = "forge@local.agent",
    ) -> PublicationPlan:
        """Formulate a validated PublicationPlan without mutating the target repository."""
        # 1. Fetch Latest Approval Decision
        decision_rec = self.exec_store.get_latest_approval_decision_for_repair(repair_id)
        if not decision_rec:
            raise PublicationError(f"No operator approval decision found for repair '{repair_id}'.")

        # 2. Inspect Target Repository
        repo_identity = self.inspector.inspect_repository(repository_path)

        # 3. Formulate Publication Request and Validate
        pub_request = PublicationRequest(
            publication_id=f"pub_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            decision_id=decision_rec.decision_id,
            action=action,
        )

        validation_result = self.validator.validate_publication_request(
            request=pub_request,
            repo_identity=repo_identity,
        )
        if not validation_result.is_valid:
            raise PublicationError(
                f"Publication validation failed: {'; '.join(validation_result.violations)}"
            )

        # 4. Resolve Linked Entities
        binding = decision_rec.binding
        finding_id = binding.finding_id
        finding_title = "Defect Repair"
        if self.forge_store:
            finding_obj = self.forge_store.get_finding(finding_id)
            if finding_obj:
                finding_title = finding_obj.title

        patch = self.exec_store.get_patch_artifact(binding.patch_artifact_id)
        touched_files = patch.affected_paths if patch else []

        # 5. Formulate and Validate Proposed Branch Name
        if custom_branch_name:
            proposed_branch = custom_branch_name.strip()
        else:
            clean_finding = finding_id.replace("find_", "")
            short_hash = binding.patch_sha256[:8]
            proposed_branch = f"{self.policy.permitted_branch_prefix}fix-{clean_finding}-{short_hash}"

        validate_branch_name(proposed_branch, self.policy)
        if is_protected_branch(proposed_branch, self.policy):
            raise ProtectedBranchViolationError(
                f"Proposed branch name '{proposed_branch}' matches protected branch policy."
            )

        # 6. Formulate Structured Commit Message
        commit_msg_lines = [
            f"fix({finding_id}): resolve defect verified by FORGE",
            "",
            f"Finding: {finding_id} - {finding_title}",
            f"Repair ID: {repair_id}",
            f"Patch SHA-256: {binding.patch_sha256}",
            f"M4 Validation ID: {binding.validation_id}",
            f"M5 Verification ID: {binding.verification_id}",
            f"Review Dossier: {binding.dossier_id} (v{binding.dossier_version})",
            f"Authorized Action: {action.value}",
            f"Operator Sign-off: {decision_rec.actor}",
            f"Justification: {decision_rec.reason}",
        ]
        commit_msg = "\n".join(commit_msg_lines)

        # 7. Scan Commit Message and Branch for Secrets
        if self.policy.scan_secrets:
            scan_for_secrets(commit_msg)
            scan_for_secrets(proposed_branch)

        commit_proposal = CommitProposal(
            commit_message=commit_msg,
            author=CommitAuthor(name=author_name, email=author_email),
            committer=CommitAuthor(name=decision_rec.actor, email=author_email),
            touched_files=touched_files,
            patch_sha256=binding.patch_sha256,
            dossier_id=binding.dossier_id,
            operator_signoff=decision_rec.actor,
        )

        plan_id = f"pubplan_{uuid.uuid4().hex[:12]}"

        return PublicationPlan(
            plan_id=plan_id,
            publication_id=pub_request.publication_id,
            repair_id=repair_id,
            decision_id=decision_rec.decision_id,
            finding_id=finding_id,
            repository_target=str(repository_path.resolve()),
            base_commit_sha=binding.base_commit_sha,
            proposed_branch_name=proposed_branch,
            commit_proposal=commit_proposal,
            patch_artifact_id=binding.patch_artifact_id,
            patch_sha256=binding.patch_sha256,
            authorized_action=action,
            policy_version=self.policy.policy_version,
            repository_identity=repo_identity,
            created_at=datetime.now(timezone.utc),
        )
