"""Phase 8 Repair Execution Architecture — Isolated Workspace Implementation.

Manages isolated scratch workspaces, guarantees target repository immutability,
enforces path containment, and prevents symlink escapes.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Optional, Union
import uuid

from forge.repair.execution.policy import (
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
)


class RepairWorkspace:
    """Isolated, disposable scratch workspace for safe repair execution and verification."""

    def __init__(
        self,
        target_repo_path: Union[str, Path],
        base_commit_sha: str,
        workspace_id: Optional[str] = None,
        scratch_root: Optional[Union[str, Path]] = None,
    ):
        self._target_repo_path = Path(target_repo_path).resolve()
        if not self._target_repo_path.exists():
            raise TargetRepoImmutabilityViolationError(
                f"Target repository path '{self._target_repo_path}' does not exist"
            )

        self._base_commit_sha = base_commit_sha.strip().lower()
        self._workspace_id = workspace_id or f"ws_{uuid.uuid4().hex[:12]}"

        root = Path(scratch_root).resolve() if scratch_root else Path(tempfile.gettempdir()).resolve() / "forge_repair_workspaces"
        self._workspace_path = root / self._workspace_id

        # Guarantee immutability boundary: workspace must never collide with target repo
        if self._workspace_path == self._target_repo_path or self._target_repo_path.is_relative_to(self._workspace_path):
            raise TargetRepoImmutabilityViolationError(
                "Workspace path cannot match or contain target repository path"
            )

        self._initial_target_git_status: Optional[str] = self._capture_target_git_status()
        self._prepared = False

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def workspace_path(self) -> Path:
        return self._workspace_path

    @property
    def target_repo_path(self) -> Path:
        return self._target_repo_path

    @property
    def base_commit_sha(self) -> str:
        return self._base_commit_sha

    @property
    def is_isolated(self) -> bool:
        return self._prepared and self._workspace_path != self._target_repo_path

    def prepare(self) -> None:
        """Create the isolated scratch workspace and copy repository contents for isolated execution."""
        if self._prepared:
            return

        self._workspace_path.parent.mkdir(parents=True, exist_ok=True)
        if self._workspace_path.exists():
            shutil.rmtree(self._workspace_path)
        self._workspace_path.mkdir(parents=True, exist_ok=False)

        # Copy repository contents into isolated workspace (excluding VCS hooks and caches)
        def _ignore_patterns(path: str, names: list[str]) -> set[str]:
            ignored = set()
            for n in names:
                if n in (".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"):
                    ignored.add(n)
            return ignored

        shutil.copytree(
            self._target_repo_path,
            self._workspace_path,
            dirs_exist_ok=True,
            ignore=_ignore_patterns,
            symlinks=False,
        )

        self._prepared = True

    def cleanup(self) -> None:
        """Safely remove the scratch workspace directory."""
        if self._workspace_path.exists():
            shutil.rmtree(self._workspace_path, ignore_errors=True)
        self._prepared = False

    def validate_containment(self, file_path: Union[str, Path]) -> Path:
        """Resolve a file path and verify that it remains strictly within the workspace boundary.

        Raises:
            ScopeContainmentViolationError: If path attempts directory traversal or escapes via symlink.
        """
        raw = str(file_path).strip()
        if not raw:
            raise ScopeContainmentViolationError("Path cannot be empty")
        if ".." in raw.split("/"):
            raise ScopeContainmentViolationError(f"Path traversal ('..') forbidden in '{file_path}'")

        # Resolve relative to workspace root
        target = (self._workspace_path / raw).resolve()
        try:
            target.relative_to(self._workspace_path.resolve())
        except ValueError:
            raise ScopeContainmentViolationError(
                f"Path '{file_path}' escapes workspace boundary '{self._workspace_path}'"
            )

        # Check symlink escapes
        if os.path.islink(target):
            link_target = Path(os.path.realpath(target))
            try:
                link_target.relative_to(self._workspace_path.resolve())
            except ValueError:
                raise ScopeContainmentViolationError(
                    f"Symlink '{file_path}' points outside workspace: '{link_target}'"
                )

        return target

    def _capture_target_git_status(self) -> Optional[str]:
        """Capture git status of target repository if it is a git repository."""
        git_dir = self._target_repo_path / ".git"
        if not git_dir.exists():
            return None
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self._target_repo_path),
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            return res.stdout.strip()
        except Exception:
            return None

    def check_target_immutability(self) -> bool:
        """Confirm that target repository git status has not changed."""
        current = self._capture_target_git_status()
        if self._initial_target_git_status is None:
            return True
        return current == self._initial_target_git_status
