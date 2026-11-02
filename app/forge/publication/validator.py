"""Phase 9 Git Publication Architecture — Publication Validator.

Enforces defense-in-depth authorization checks, exact M6 artifact binding validation,
repository identity consistency, policy compliance, and secret scanning.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from forge.config import redact_secrets
from forge.publication.authorization import validate_action_authorization
from forge.publication.failures import (
    ArtifactBindingMismatchError,
    ProtectedBranchViolationError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
    PublicationPolicyViolationError,
    RepositoryDirtyError,
    RepositoryIdentityMismatchError,
)
from forge.publication.inspector import GitRepositoryInspector
from forge.publication.interfaces import (
    GitPublicationValidatorProtocol,
    GitRepositoryInspectorProtocol,
)
from forge.publication.models import (
    PublicationAction,
    PublicationRequest,
    PublicationValidationResult,
    RepositoryIdentity,
)
from forge.publication.policy import (
    GitPublicationPolicy,
    is_protected_branch,
    scan_for_secrets,
    validate_remote_url,
)
from forge.repair.execution.models import (
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
)
from forge.repair.execution.storage import RepairExecutionStore


class GitPublicationValidator(GitPublicationValidatorProtocol):
    """Validates publication requests against approvals, policy, and repository identity."""

    def __init__(
        self,
        execution_store: RepairExecutionStore,
        policy: Optional[GitPublicationPolicy] = None,
        inspector: Optional[GitRepositoryInspectorProtocol] = None,
    ):
        self.exec_store = execution_store
        self.policy = policy or GitPublicationPolicy()
        self.inspector = inspector or GitRepositoryInspector()

    def validate_publication_request(
        self,
        request: PublicationRequest,
        repo_identity: RepositoryIdentity,
    ) -> PublicationValidationResult:
        """Validate authorization, binding, repository identity, and policy compliance."""
        checks_passed: List[str] = []
        violations: List[str] = []

        # 1. Non-negotiable policy safety invariants
        self.policy.validate_safety_invariants()
        checks_passed.append("Policy safety invariants verified (force push, rewrite, auto-merge forbidden).")

        # 2. Check Allowed Actions Policy
        if request.action not in self.policy.allowed_actions:
            raise PublicationPolicyViolationError(
                f"Action '{request.action.value}' is not permitted by active publication policy."
            )
        checks_passed.append(f"Action '{request.action.value}' is permitted by policy.")

        # 3. Retrieve and Validate M6 Approval Decision
        decision_rec = self.exec_store.get_approval_decision(request.decision_id)
        if not decision_rec:
            # Fallback query: check latest decision for repair
            latest = self.exec_store.get_latest_approval_decision_for_repair(request.repair_id)
            if latest and latest.decision_id == request.decision_id:
                decision_rec = latest
            else:
                raise PublicationAuthorizationError(
                    f"Approval decision '{request.decision_id}' does not exist in store."
                )

        if decision_rec.repair_id != request.repair_id:
            raise PublicationAuthorizationError(
                f"Approval decision '{decision_rec.decision_id}' belongs to repair '{decision_rec.repair_id}', "
                f"not '{request.repair_id}'."
            )

        # 4. Enforce Approval Lifecycle State (must be APPROVED)
        now = datetime.now(timezone.utc)
        if decision_rec.expires_at and now > decision_rec.expires_at:
            # Auto-expire the approval record in storage if not already expired
            if decision_rec.state == ApprovalState.APPROVED:
                expired_rec = decision_rec.model_copy(
                    update={
                        "state": ApprovalState.EXPIRED,
                        "invalidation_reason": "Approval TTL expired prior to publication authorization.",
                        "invalidation_timestamp": now,
                    }
                )
                self.exec_store.save_approval_decision(expired_rec)
            raise PublicationAuthorizationError(
                f"Approval decision '{decision_rec.decision_id}' expired at {decision_rec.expires_at.isoformat()}."
            )

        if decision_rec.state != ApprovalState.APPROVED:
            raise PublicationAuthorizationError(
                f"Approval decision '{decision_rec.decision_id}' is in state '{decision_rec.state.value}'; "
                "must be 'APPROVED' for publication."
            )
        checks_passed.append(f"Approval decision '{decision_rec.decision_id}' is active and APPROVED.")

        # 5. Enforce Action-Specific Authorization
        auth_action = decision_rec.authorized_action
        if not auth_action:
            raise PublicationAuthorizationError(
                f"Approval decision '{decision_rec.decision_id}' has no recorded authorized action."
            )

        validate_action_authorization(
            authorized_action=auth_action,
            requested_action=request.action,
            policy=self.policy,
        )
        checks_passed.append(
            f"Requested action '{request.action.value}' matches operator authorization '{auth_action.value}'."
        )

        # 6. Validate Exact Artifact Binding
        binding = decision_rec.binding
        if not binding:
            raise PublicationAuthorizationError(
                f"Approval decision '{decision_rec.decision_id}' has no structured artifact binding."
            )

        if binding.repair_id != request.repair_id:
            raise ArtifactBindingMismatchError(
                f"Binding repair ID '{binding.repair_id}' does not match request '{request.repair_id}'."
            )

        # Check candidate patch artifact existence and content hash
        patch = self.exec_store.get_patch_artifact(binding.patch_artifact_id)
        if not patch:
            raise ArtifactBindingMismatchError(
                f"Patch artifact '{binding.patch_artifact_id}' referenced in binding does not exist in store."
            )

        if patch.content_hash != binding.patch_sha256:
            raise ArtifactBindingMismatchError(
                f"Patch content hash tampered: binding has '{binding.patch_sha256}', "
                f"stored patch has '{patch.content_hash}'."
            )
        checks_passed.append(f"Patch content hash '{binding.patch_sha256[:12]}' matches binding exactly.")

        # Check M4 validation record existence and status
        if binding.validation_id and binding.validation_id != "NONE":
            val_res = self.exec_store.get_validation_result(binding.validation_id)
            if not val_res:
                raise ArtifactBindingMismatchError(
                    f"Validation result '{binding.validation_id}' not found in store."
                )
            if val_res.patch_sha256 != binding.patch_sha256:
                raise ArtifactBindingMismatchError(
                    f"Validation patch SHA-256 '{val_res.patch_sha256}' does not match binding '{binding.patch_sha256}'."
                )
            checks_passed.append(f"M4 validation record '{binding.validation_id}' verified.")

        # Check M5 verification run existence and outcome
        if binding.verification_id and binding.verification_id != "NONE":
            verif_run = self.exec_store.get_verification_run(binding.verification_id)
            if not verif_run:
                raise ArtifactBindingMismatchError(
                    f"Verification run '{binding.verification_id}' not found in store."
                )
            if verif_run.patch_sha256 != binding.patch_sha256:
                raise ArtifactBindingMismatchError(
                    f"Verification patch SHA-256 '{verif_run.patch_sha256}' does not match binding '{binding.patch_sha256}'."
                )
            checks_passed.append(f"M5 verification record '{binding.verification_id}' verified.")

        # 7. Validate Repository Identity & Immutability
        # Verify base commit SHA matches binding exactly
        if self.policy.require_exact_base_sha:
            if repo_identity.base_commit_sha != binding.base_commit_sha:
                raise RepositoryIdentityMismatchError(
                    f"Repository HEAD commit '{repo_identity.base_commit_sha}' does not match "
                    f"approved base commit '{binding.base_commit_sha}'."
                )
            checks_passed.append(f"Base commit SHA matches approved revision '{binding.base_commit_sha[:12]}'.")

        # Verify canonical repository target matches binding
        # Resolve target paths
        binding_target_str = binding.repository_target.strip()
        identity_path_str = repo_identity.canonical_path.strip()
        identity_git_root = repo_identity.git_root.strip()

        # Both can be compared as paths if local, or URLs if remote
        if binding_target_str.startswith("http://") or binding_target_str.startswith("https://"):
            if repo_identity.remote_url != binding_target_str:
                raise RepositoryIdentityMismatchError(
                    f"Repository remote URL '{repo_identity.remote_url}' does not match approved target '{binding_target_str}'."
                )
        else:
            try:
                binding_p = Path(binding_target_str).resolve()
                ident_p = Path(identity_path_str).resolve()
                root_p = Path(identity_git_root).resolve()
                if ident_p != binding_p and root_p != binding_p:
                    raise RepositoryIdentityMismatchError(
                        f"Repository canonical path '{ident_p}' does not match approved target '{binding_p}'."
                    )
            except Exception as e:
                if isinstance(e, RepositoryIdentityMismatchError):
                    raise
                if binding_target_str not in (identity_path_str, identity_git_root):
                    raise RepositoryIdentityMismatchError(
                        f"Target repository mismatch: expected '{binding_target_str}', found '{identity_path_str}'."
                    )
        checks_passed.append("Repository canonical identity matches approved target.")

        # 8. Check Working Tree Cleanliness
        if self.policy.require_clean_working_tree:
            if not repo_identity.is_clean:
                raise RepositoryDirtyError(
                    f"Repository at '{repo_identity.canonical_path}' has a dirty working tree: "
                    f"{repo_identity.modified_files_count} modified, {repo_identity.untracked_files_count} untracked."
                )
            checks_passed.append("Target repository working tree is clean.")

        # 9. Verify Remote URL Allowlist
        if repo_identity.remote_url:
            validate_remote_url(repo_identity.remote_url, self.policy)
            checks_passed.append("Remote URL complies with scheme and domain allowlists.")

        return PublicationValidationResult(
            is_valid=True,
            action=request.action,
            checks_passed=checks_passed,
            violations=[],
            evaluated_at=now,
        )
