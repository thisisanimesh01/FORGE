"""Phase 9 Git Publication Architecture — Commit Builder and Patch Staging.

Constructs descriptive, secret-scanned commit proposals, applies vetted candidate patches
with strict boundary checks, and creates verified local commits inside publication workspaces.
"""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from forge.analysis.findings import Finding
from forge.config import redact_secrets
from forge.publication.failures import (
    CommitCreationError,
    CommitVerificationError,
    PatchStagingError,
    SecretDetectedError,
    WorkspaceIntegrityError,
)
from forge.publication.interfaces import GitCommitBuilderProtocol
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
)
from forge.publication.policy import GitPublicationPolicy, scan_for_secrets
from forge.publication.workspace import PublicationWorkspace
from forge.repair.execution.models import (
    ApprovalDecisionRecord,
    PatchArtifact,
    ReviewDossier,
)
from forge.repair.verifier import apply_unified_diff_to_file


class GitCommitBuilder(GitCommitBuilderProtocol):
    """Builds descriptive commit proposals, stages approved patches, and creates verified commits."""

    def __init__(self, policy: Optional[GitPublicationPolicy] = None):
        self.policy = policy or GitPublicationPolicy()

    def plan_commit(
        self,
        finding: Finding,
        patch: PatchArtifact,
        dossier: ReviewDossier,
        approval: ApprovalDecisionRecord,
        author_name: str = "FORGE Autonomous Agent",
        author_email: str = "forge@local.agent",
    ) -> CommitProposal:
        """Construct a structured, normal descriptive commit proposal without conventional prefixes."""
        # Non-negotiable requirement: Normal descriptive message without feat:, fix:, docs: prefixes
        subject = f"Resolve defect in {finding.title} ({finding.finding_id})"
        clean_subject = re.sub(
            r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^\)]+\))?:\s*",
            "",
            subject,
            flags=re.IGNORECASE,
        ).strip()

        commit_lines = [
            clean_subject,
            "",
            f"Finding: {finding.finding_id} - {finding.title}",
            f"Repair ID: {patch.repair_id}",
            f"Base Commit: {patch.base_commit_sha}",
            f"Patch SHA-256: {patch.content_hash}",
            f"M4 Validation ID: {dossier.validation_id}",
            f"M5 Verification ID: {dossier.verification_id}",
            f"Review Dossier: {dossier.dossier_id}",
            f"Authorized Action: {approval.authorized_action.value if approval.authorized_action else 'stage_branch'}",
            f"Operator Sign-off: {approval.actor}",
            f"Operator Reason: {approval.reason}",
        ]
        commit_message = "\n".join(commit_lines)

        self.validate_commit_message(commit_message)

        author = CommitAuthor(name=author_name, email=author_email)
        committer = CommitAuthor(
            name=approval.actor if "@" not in approval.actor else approval.actor.split("@")[0],
            email=approval.actor if "@" in approval.actor else author_email,
        )

        return CommitProposal(
            commit_message=commit_message,
            author=author,
            committer=committer,
            touched_files=patch.affected_paths,
            patch_sha256=patch.content_hash,
            dossier_id=dossier.dossier_id,
            operator_signoff=approval.actor,
        )

    def validate_commit_message(self, message: str) -> None:
        """Validate commit message against secrets and security policies."""
        if not message or not message.strip():
            raise CommitCreationError("Commit message cannot be empty.")
        if self.policy.scan_secrets:
            scan_for_secrets(message)

    def apply_and_stage_patch(
        self,
        workspace: PublicationWorkspace,
        patch: PatchArtifact,
        allowed_files: List[str],
    ) -> List[str]:
        """Apply approved candidate patch to the workspace working tree and stage it."""
        # 1. Digest verification
        calculated_sha = hashlib.sha256(patch.diff_content.encode("utf-8")).hexdigest()
        if calculated_sha != patch.content_hash:
            raise PatchStagingError(
                f"Patch integrity check failed: expected hash '{patch.content_hash}', computed '{calculated_sha}'."
            )

        # 2. Reject binary or forbidden changes
        if "GIT binary patch" in patch.diff_content or "Binary files" in patch.diff_content:
            raise PatchStagingError("Binary patch changes are strictly rejected by publication policy.")

        # 3. Scope validation: all affected paths must be in allowed_files
        allowed_set = {Path(f).as_posix() for f in allowed_files}
        affected_set = {Path(f).as_posix() for f in patch.affected_paths}
        unauthorized = affected_set - allowed_set
        if unauthorized:
            raise PatchStagingError(
                f"Patch attempts to modify unauthorized files: {sorted(unauthorized)}."
            )

        # 3b. Protected path validation: strictly forbid staging changes to protected files
        from forge.repository.protection import is_protected_name, is_protected_path
        for rel_file in patch.affected_paths:
            if is_protected_name(Path(rel_file).name) or is_protected_path(rel_file, base_dir=workspace.workspace_path):
                raise PatchStagingError(
                    f"Security violation: patch attempts to modify protected sensitive file: '{rel_file}'."
                )

        # 4. Containment and existence check
        for rel_file in patch.affected_paths:
            target_disk = workspace.validate_containment(rel_file)
            if not target_disk.is_file():
                raise PatchStagingError(
                    f"Target file '{rel_file}' does not exist in publication workspace."
                )

        # 5. Assert baseline integrity prior to application
        workspace.verify_target_immutability()
        workspace.check_integrity(allowed_files=[])

        # 6. Apply unified diff to workspace working tree
        # First attempt native git apply inside workspace
        applied_via_git = False
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as tf:
            tf.write(patch.diff_content)
            temp_patch_path = tf.name

        try:
            res_git_apply = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "apply",
                    "--whitespace=nowarn",
                    temp_patch_path,
                ],
                cwd=str(workspace.workspace_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_git_apply.returncode == 0:
                applied_via_git = True
        finally:
            if os.path.exists(temp_patch_path):
                os.remove(temp_patch_path)

        if not applied_via_git:
            # Fallback to internal python diff application
            try:
                for rel_file in patch.affected_paths:
                    target_disk = workspace.validate_containment(rel_file)
                    apply_unified_diff_to_file(
                        target_disk,
                        patch.diff_content,
                        check_semantics=False,
                    )
            except Exception as e:
                raise PatchStagingError(
                    f"Failed to apply approved patch diff to publication workspace: {e}"
                ) from e

        # 7. Post-application workspace integrity verification
        integrity = workspace.check_integrity(allowed_files=patch.affected_paths)
        if set(integrity.modified_files) != affected_set:
            raise PatchStagingError(
                f"Modified files {integrity.modified_files} do not match expected affected files {patch.affected_paths}."
            )
        if integrity.added_files or integrity.removed_files:
            raise PatchStagingError(
                f"Unexpected workspace additions ({integrity.added_files}) or deletions ({integrity.removed_files})."
            )

        # 8. Stage exactly the affected files into Git index
        try:
            for rel_file in patch.affected_paths:
                subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "add", rel_file],
                    cwd=str(workspace.workspace_path),
                    check=True,
                    capture_output=True,
                    text=True,
                )
        except subprocess.CalledProcessError as e:
            raise PatchStagingError(f"Failed to git add touched files: {e.stderr}") from e

        # 9. Verify staged index contains ONLY expected files
        try:
            staged_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "diff", "--cached", "--name-only"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            staged_files = set(staged_res.stdout.strip().splitlines())
            if staged_files != affected_set:
                raise PatchStagingError(
                    f"Staged index files {staged_files} do not match approved files {affected_set}."
                )

            # Ensure zero untracked or unstaged changes remain
            status_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            status_lines = status_res.stdout.strip().splitlines()
            for line in status_lines:
                if not line.startswith("M  "):
                    raise PatchStagingError(
                        f"Unexpected workspace state after patch staging: '{line}'"
                    )
        except subprocess.CalledProcessError as e:
            raise PatchStagingError(f"Failed to verify Git staging index: {e.stderr}") from e

        # 10. Verify target repository immutability
        workspace.verify_target_immutability()

        return patch.affected_paths

    def create_commit(
        self,
        workspace: PublicationWorkspace,
        commit_proposal: CommitProposal,
        base_commit_sha: str,
    ) -> Dict[str, Any]:
        """Create and strictly verify a Git commit in the publication workspace."""
        # 1. Validate commit message
        self.validate_commit_message(commit_proposal.commit_message)

        # 2. Assert target repository immutability prior to commit creation
        workspace.verify_target_immutability()

        # 3. Verify that staged files match proposal exactly
        try:
            staged_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "diff", "--cached", "--name-only"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            staged_files = set(staged_res.stdout.strip().splitlines())
            expected_files = set(commit_proposal.touched_files)
            if staged_files != expected_files:
                raise CommitCreationError(
                    f"Staged files {staged_files} do not match commit proposal files {expected_files}."
                )
        except subprocess.CalledProcessError as e:
            raise CommitCreationError(f"Failed to query staged files: {e.stderr}") from e

        # 4. Formulate deterministic commit environment
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": commit_proposal.author.name,
            "GIT_AUTHOR_EMAIL": commit_proposal.author.email,
            "GIT_COMMITTER_NAME": commit_proposal.committer.name,
            "GIT_COMMITTER_EMAIL": commit_proposal.committer.email,
        }

        # 5. Execute commit
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    commit_proposal.commit_message,
                ],
                cwd=str(workspace.workspace_path),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise CommitCreationError(
                f"Failed to execute git commit in publication workspace: {e.stderr}"
            ) from e

        # 6. Post-commit verification
        try:
            # Check commit SHA
            commit_sha = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip().lower()

            # Check parent commit SHA equals base_commit_sha
            parent_sha = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD^1"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip().lower()

            if not (parent_sha.startswith(base_commit_sha) or base_commit_sha.startswith(parent_sha)):
                raise CommitVerificationError(
                    f"Commit parent '{parent_sha}' does not match expected base SHA '{base_commit_sha}'."
                )

            # Check tree SHA
            tree_sha = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD^{tree}"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip().lower()

            # Check diff-tree touched files
            diff_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            committed_files = diff_res.stdout.strip().splitlines()
            if set(committed_files) != expected_files:
                raise CommitVerificationError(
                    f"Committed files {committed_files} do not match expected touched files {expected_files}."
                )

            # Check working tree is completely clean
            status_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            if status_res.stdout.strip():
                raise CommitVerificationError(
                    f"Working tree is not clean after commit: {status_res.stdout.strip()}"
                )
        except subprocess.CalledProcessError as e:
            raise CommitVerificationError(f"Post-commit inspection failed: {e.stderr}") from e

        # 7. Post-commit target repository immutability verification
        workspace.verify_target_immutability()

        return {
            "commit_sha": commit_sha,
            "parent_commit_sha": parent_sha,
            "tree_sha": tree_sha,
            "changed_files": committed_files,
        }
