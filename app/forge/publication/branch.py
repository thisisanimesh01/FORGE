"""Phase 9 Git Publication Architecture — Branch Manager.

Handles deterministic branch planning, reference validation, collision detection,
and safe branch creation exclusively within isolated publication workspaces.
"""

from pathlib import Path
import subprocess
from typing import Optional

from forge.publication.failures import (
    BranchCollisionError,
    BranchCreationError,
    ProtectedBranchViolationError,
    PublicationPolicyViolationError,
)
from forge.publication.interfaces import GitBranchManagerProtocol
from forge.publication.models import BranchProposal
from forge.publication.policy import (
    GitPublicationPolicy,
    is_protected_branch,
    validate_branch_name,
)
from forge.publication.workspace import PublicationWorkspace


class GitBranchManager(GitBranchManagerProtocol):
    """Manages Git branch proposals and isolated branch creation."""

    def __init__(self, policy: Optional[GitPublicationPolicy] = None):
        self.policy = policy or GitPublicationPolicy()

    def plan_branch(
        self,
        base_commit_sha: str,
        finding_id: str,
        patch_sha256: str,
        custom_name: Optional[str] = None,
    ) -> BranchProposal:
        """Formulate a deterministic, policy-compliant branch proposal."""
        if custom_name:
            branch_name = custom_name.strip()
        else:
            clean_finding = finding_id.replace("find_", "")
            short_hash = patch_sha256[:8]
            branch_name = f"{self.policy.permitted_branch_prefix}fix-{clean_finding}-{short_hash}"

        self.validate_branch_name(branch_name)

        return BranchProposal(
            branch_name=branch_name,
            source_commit_sha=base_commit_sha,
            target_branch="main",
            description=f"Isolated repair branch for finding {finding_id}",
        )

    def validate_branch_name(self, branch_name: str) -> None:
        """Validate branch name against Git syntax and safety policies."""
        validate_branch_name(branch_name, self.policy)
        if is_protected_branch(branch_name, self.policy):
            raise ProtectedBranchViolationError(
                f"Branch name '{branch_name}' violates protected branch policy."
            )

    def branch_exists(self, repository_path: Path, branch_name: str) -> bool:
        """Check whether a branch reference already exists in a given repository."""
        clean_name = branch_name.removeprefix("refs/heads/")
        try:
            res = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "show-ref",
                    "--verify",
                    f"refs/heads/{clean_name}",
                ],
                cwd=str(repository_path),
                capture_output=True,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return False

    def create_branch(
        self,
        workspace: PublicationWorkspace,
        branch_name: str,
        base_commit_sha: str,
    ) -> str:
        """Create a new local branch inside the isolated publication workspace.

        Guarantees:
        - Target repository is never checked out or modified.
        - Branch name must not collide with existing target or workspace branches.
        - Base commit is strictly verified.
        - Target repository immutability is validated before and after creation.
        """
        # 1. Syntax and protected branch validation
        self.validate_branch_name(branch_name)

        # 2. Collision detection in target repository
        if self.branch_exists(workspace.target_repo_path, branch_name):
            raise BranchCollisionError(
                f"Branch '{branch_name}' already exists in target repository."
            )

        # 3. Collision detection in publication workspace
        if self.branch_exists(workspace.workspace_path, branch_name):
            raise BranchCollisionError(
                f"Branch '{branch_name}' already exists in publication workspace."
            )

        # 4. Assert target repository immutability prior to branch creation
        workspace.verify_target_immutability()

        # 5. Create and switch to new branch in isolated workspace
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "checkout",
                    "-b",
                    branch_name,
                    base_commit_sha,
                ],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise BranchCreationError(
                f"Failed to create branch '{branch_name}' in workspace: {e.stderr}"
            ) from e

        # 6. Verify current branch and revision in workspace
        try:
            res_branch = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            active_branch = res_branch.stdout.strip()
            if active_branch != branch_name:
                raise BranchCreationError(
                    f"Expected active branch '{branch_name}', found '{active_branch}'."
                )

            res_head = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD"],
                cwd=str(workspace.workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            head_sha = res_head.stdout.strip().lower()
            if not (head_sha.startswith(base_commit_sha) or base_commit_sha.startswith(head_sha)):
                raise BranchCreationError(
                    f"Branch '{branch_name}' HEAD '{head_sha}' does not match expected base '{base_commit_sha}'."
                )
        except subprocess.CalledProcessError as e:
            raise BranchCreationError(
                f"Failed to verify active branch in publication workspace: {e.stderr}"
            ) from e

        # 7. Post-creation target repository immutability check
        workspace.verify_target_immutability()

        return branch_name
