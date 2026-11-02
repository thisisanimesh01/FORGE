"""Phase 9 Git Publication Architecture — Repository Inspector.

Implements read-only Git repository inspection, capturing verifiable
RepositoryIdentity snapshots while preserving strict target repository immutability.
"""

from pathlib import Path
from typing import Optional

from forge.config import redact_secrets
from forge.publication.failures import (
    RepositoryIdentityMismatchError,
)
from forge.publication.interfaces import GitRepositoryInspectorProtocol
from forge.publication.models import RepositoryIdentity
from forge.repository.acquisition import run_git_command


class GitRepositoryInspector(GitRepositoryInspectorProtocol):
    """Safely inspects target Git repositories using read-only queries with hooks disabled."""

    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds

    def inspect_repository(self, repository_path: Path) -> RepositoryIdentity:
        """Inspect and return verified identity snapshot of the target Git repository."""
        path = repository_path.resolve()
        if not path.exists() or not path.is_dir():
            raise RepositoryIdentityMismatchError(
                f"Repository path does not exist or is not a directory: '{path}'"
            )

        # 1. Resolve Git root
        git_root = self._get_git_root(path)

        # 2. Resolve HEAD commit SHA
        base_commit_sha = self.resolve_head_commit(path)

        # 3. Resolve Current Branch or ref
        current_branch = self._get_current_branch(path)

        # 4. Resolve Remote URL (origin)
        remote_url = self.get_remote_url(path)

        # 5. Resolve Remote Tracking Configuration
        remote_tracking = self._get_remote_tracking(path)

        # 6. Check Working Tree Cleanliness
        is_clean, untracked_count, modified_count = self._get_status_metrics(path)

        return RepositoryIdentity(
            canonical_path=str(path),
            git_root=str(git_root),
            remote_url=remote_url,
            current_branch=current_branch,
            base_commit_sha=base_commit_sha,
            is_clean=is_clean,
            remote_tracking=remote_tracking,
            untracked_files_count=untracked_count,
            modified_files_count=modified_count,
        )

    def check_working_tree_clean(self, repository_path: Path) -> bool:
        """Return True if working tree has zero uncommitted or untracked changes."""
        path = repository_path.resolve()
        is_clean, _, _ = self._get_status_metrics(path)
        return is_clean

    def resolve_head_commit(self, repository_path: Path) -> str:
        """Resolve full 40-character commit SHA at HEAD."""
        path = repository_path.resolve()
        res = run_git_command(
            ["rev-parse", "HEAD"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if res.returncode != 0:
            raise RepositoryIdentityMismatchError(
                f"Failed to resolve HEAD commit for '{path}': {redact_secrets(res.stderr)}"
            )
        sha = res.stdout.strip()
        if len(sha) != 40:
            raise RepositoryIdentityMismatchError(
                f"Invalid commit SHA '{sha}' resolved for '{path}'."
            )
        return sha

    def get_remote_url(self, repository_path: Path, remote_name: str = "origin") -> Optional[str]:
        """Fetch remote URL for configured remote name."""
        path = repository_path.resolve()
        res = run_git_command(
            ["remote", "get-url", remote_name],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

        # Fallback to config query
        cfg_res = run_git_command(
            ["config", "--get", f"remote.{remote_name}.url"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if cfg_res.returncode == 0 and cfg_res.stdout.strip():
            return cfg_res.stdout.strip()

        return None

    def _get_git_root(self, path: Path) -> Path:
        res = run_git_command(
            ["rev-parse", "--show-toplevel"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if res.returncode != 0:
            raise RepositoryIdentityMismatchError(
                f"Directory '{path}' is not inside a valid Git repository: {redact_secrets(res.stderr)}"
            )
        return Path(res.stdout.strip()).resolve()

    def _get_current_branch(self, path: Path) -> Optional[str]:
        res = run_git_command(
            ["symbolic-ref", "--short", "HEAD"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

        # Detached HEAD fallback
        abbrev_res = run_git_command(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if abbrev_res.returncode == 0 and abbrev_res.stdout.strip():
            val = abbrev_res.stdout.strip()
            return None if val == "HEAD" else val

        return None

    def _get_remote_tracking(self, path: Path) -> Optional[str]:
        res = run_git_command(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        return None

    def _get_status_metrics(self, path: Path) -> tuple[bool, int, int]:
        res = run_git_command(
            ["status", "--porcelain"],
            cwd=path,
            timeout_seconds=self.timeout_seconds,
        )
        if res.returncode != 0:
            raise RepositoryIdentityMismatchError(
                f"Failed to check git status on '{path}': {redact_secrets(res.stderr)}"
            )

        lines = [line for line in res.stdout.splitlines() if line.strip()]
        untracked = 0
        modified = 0
        for line in lines:
            prefix = line[:2]
            if "??" in prefix:
                untracked += 1
            else:
                modified += 1

        is_clean = len(lines) == 0
        return is_clean, untracked, modified
