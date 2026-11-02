"""Phase 9 Git Publication Architecture — Isolated Publication Workspace.

Provides physically isolated, disposable scratch workspaces for safe Git branch
staging and commit creation without mutating the original target repository.
"""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from forge.config import redact_secrets
from forge.publication.failures import (
    PublicationWorkspaceError,
    RepositoryDirtyError,
    SymlinkEscapeError,
    TargetRepoMutationError,
    WorkspaceIntegrityError,
)
from forge.publication.models import WorkspaceIntegritySnapshot
from forge.publication.policy import GitPublicationPolicy


class PublicationWorkspace:
    """Isolated Git scratch workspace for staging branches and creating verified commits."""

    def __init__(
        self,
        target_repo_path: Union[str, Path],
        base_commit_sha: str,
        workspace_id: Optional[str] = None,
        scratch_root: Optional[Union[str, Path]] = None,
        policy: Optional[GitPublicationPolicy] = None,
    ):
        self._target_repo_path = Path(target_repo_path).resolve()
        if not self._target_repo_path.exists():
            raise PublicationWorkspaceError(
                f"Target repository path '{self._target_repo_path}' does not exist."
            )
        git_dir = self._target_repo_path / ".git"
        if not git_dir.exists():
            raise PublicationWorkspaceError(
                f"Target path '{self._target_repo_path}' is not a valid Git repository (missing .git)."
            )

        self._base_commit_sha = base_commit_sha.strip().lower()
        if not self._base_commit_sha:
            raise PublicationWorkspaceError("base_commit_sha cannot be empty.")

        self._workspace_id = workspace_id or f"pubws_{uuid.uuid4().hex[:12]}"
        self._policy = policy or GitPublicationPolicy()

        root = (
            Path(scratch_root).resolve()
            if scratch_root
            else Path(tempfile.gettempdir()).resolve() / "forge_publication_workspaces"
        )
        self._workspace_path = root / self._workspace_id

        # Boundary checks: workspace must not equal or be inside target repository
        if self._workspace_path == self._target_repo_path or self._target_repo_path.is_relative_to(
            self._workspace_path
        ):
            raise PublicationWorkspaceError(
                "Publication workspace path cannot match or contain target repository."
            )
        try:
            if self._workspace_path.is_relative_to(self._target_repo_path):
                raise PublicationWorkspaceError(
                    "Publication workspace path cannot be inside target repository."
                )
        except ValueError:
            pass

        # Capture initial target repository identity snapshot for strict immutability verification
        self._initial_target_snapshot = self._capture_target_snapshot()

        # Enforce target repository cleanliness upon intake
        if self._policy.require_clean_working_tree and self._initial_target_snapshot["status"]:
            raise RepositoryDirtyError(
                f"Target repository '{self._target_repo_path}' has uncommitted changes:\n"
                f"{self._initial_target_snapshot['status']}"
            )

        self._baseline_hashes: Dict[str, str] = {}
        self._prepared: bool = False
        self._cleaned: bool = False

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
    def is_prepared(self) -> bool:
        return self._prepared

    @property
    def is_cleaned(self) -> bool:
        return self._cleaned

    @property
    def baseline_hashes(self) -> Dict[str, str]:
        return dict(self._baseline_hashes)

    def _capture_target_snapshot(self) -> Dict[str, str]:
        """Capture immutable cryptographic snapshot of target repository."""
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        try:
            head_sha = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD"],
                cwd=str(self._target_repo_path),
                capture_output=True,
                text=True,
                check=True,
                env=env,
            ).stdout.strip().lower()

            branch_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self._target_repo_path),
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            branch = branch_res.stdout.strip()

            tree_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD^{tree}"],
                cwd=str(self._target_repo_path),
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            tree_sha = tree_res.stdout.strip().lower()

            status_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
                cwd=str(self._target_repo_path),
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            status = status_res.stdout.strip()

            return {
                "head_sha": head_sha,
                "branch": branch,
                "tree_sha": tree_sha,
                "status": status,
            }
        except subprocess.CalledProcessError as e:
            raise PublicationWorkspaceError(
                f"Failed to query target repository at '{self._target_repo_path}': {e.stderr}"
            ) from e

    def check_target_immutability(self) -> bool:
        """Verify whether target repository remains 100% unmutated."""
        try:
            current = self._capture_target_snapshot()
            return (
                current["head_sha"] == self._initial_target_snapshot["head_sha"]
                and current["branch"] == self._initial_target_snapshot["branch"]
                and current["tree_sha"] == self._initial_target_snapshot["tree_sha"]
                and current["status"] == self._initial_target_snapshot["status"]
            )
        except Exception:
            return False

    def verify_target_immutability(self) -> None:
        """Assert target repository immutability, raising TargetRepoMutationError if altered."""
        if not self.check_target_immutability():
            raise TargetRepoMutationError(
                f"Non-negotiable immutability violation: target repository at '{self._target_repo_path}' "
                "was mutated during publication operations."
            )

    def prepare(self) -> None:
        """Provision the isolated scratch Git repository clone and checkout base commit."""
        if self._cleaned:
            raise PublicationWorkspaceError(
                f"Workspace '{self._workspace_id}' was already cleaned up and cannot be reused."
            )
        if self._prepared:
            return

        # 1. Assert initial target immutability
        self.verify_target_immutability()

        # 2. Check workspace path collision
        if self._workspace_path.exists():
            if any(self._workspace_path.iterdir()):
                raise PublicationWorkspaceError(
                    f"Workspace path '{self._workspace_path}' already exists and is not empty."
                )
        else:
            self._workspace_path.parent.mkdir(parents=True, exist_ok=True)

        # 3. Clone repository with --no-hardlinks to guarantee physical file separation
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "clone",
                    "--no-hardlinks",
                    str(self._target_repo_path),
                    str(self._workspace_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise PublicationWorkspaceError(
                f"Failed to clone target repository into publication workspace: {e.stderr}"
            ) from e

        # 4. Disable remote pushing and hooks in workspace configuration
        try:
            subprocess.run(
                ["git", "config", "core.hooksPath", "/dev/null"],
                cwd=str(self._workspace_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "set-url", "--push", "origin", "DISABLED_FOR_SAFETY"],
                cwd=str(self._workspace_path),
                check=False,
                capture_output=True,
            )
        except Exception as e:
            raise PublicationWorkspaceError(
                f"Failed to secure workspace Git configuration: {e}"
            ) from e

        # 5. Checkout exact base commit in detached HEAD
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "checkout",
                    "--detach",
                    self._base_commit_sha,
                ],
                cwd=str(self._workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise PublicationWorkspaceError(
                f"Failed to checkout approved base commit '{self._base_commit_sha}': {e.stderr}"
            ) from e

        # 6. Verify checked out revision matches expected base commit SHA
        try:
            res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", "HEAD"],
                cwd=str(self._workspace_path),
                check=True,
                capture_output=True,
                text=True,
            )
            actual_sha = res.stdout.strip().lower()
            if not (actual_sha.startswith(self._base_commit_sha) or self._base_commit_sha.startswith(actual_sha)):
                raise PublicationWorkspaceError(
                    f"Workspace HEAD '{actual_sha}' does not match expected base SHA '{self._base_commit_sha}'."
                )
        except subprocess.CalledProcessError as e:
            raise PublicationWorkspaceError(
                f"Failed to verify workspace HEAD revision: {e.stderr}"
            ) from e

        # 7. Compute baseline file manifest and enforce symlink containment
        self._baseline_hashes = self._hash_workspace_files()
        self._prepared = True

        # 8. Final pre-operation target repository immutability check
        self.verify_target_immutability()

    def _hash_workspace_files(self) -> Dict[str, str]:
        """Compute SHA-256 for all working tree files (excluding .git) and enforce symlink containment."""
        hashes: Dict[str, str] = {}
        ws_root = self._workspace_path.resolve()

        for root, dirs, files in os.walk(self._workspace_path, followlinks=False):
            if ".git" in dirs:
                dirs.remove(".git")
            dirs.sort()
            for fname in sorted(files):
                full_path = Path(root) / fname
                rel_path = full_path.relative_to(self._workspace_path).as_posix()

                if full_path.is_symlink():
                    target_real = full_path.resolve()
                    if not target_real.is_relative_to(ws_root):
                        raise SymlinkEscapeError(
                            f"Symlink at '{rel_path}' points outside workspace: '{target_real}'"
                        )
                    hashes[rel_path] = f"symlink:{target_real}"
                else:
                    try:
                        hasher = hashlib.sha256()
                        with open(full_path, "rb") as f:
                            while chunk := f.read(65536):
                                hasher.update(chunk)
                        hashes[rel_path] = hasher.hexdigest()
                    except (OSError, IOError) as e:
                        raise WorkspaceIntegrityError(
                            f"Failed to hash workspace file '{rel_path}': {e}"
                        ) from e
        return hashes

    def validate_containment(self, file_path: Union[str, Path]) -> Path:
        """Validate that a target file path resolves strictly within the publication workspace."""
        path_obj = Path(file_path)
        if path_obj.is_absolute():
            # If absolute, verify it's inside workspace
            try:
                resolved = path_obj.resolve()
                if not resolved.is_relative_to(self._workspace_path.resolve()):
                    raise SymlinkEscapeError(
                        f"Absolute path '{file_path}' escapes workspace boundary."
                    )
                return resolved
            except ValueError:
                raise SymlinkEscapeError(
                    f"Absolute path '{file_path}' escapes workspace boundary."
                )

        # Disallow path traversal patterns
        parts = path_obj.parts
        if ".." in parts:
            raise SymlinkEscapeError(f"Path traversal '..' detected in '{file_path}'.")

        resolved = (self._workspace_path / path_obj).resolve()
        if not resolved.is_relative_to(self._workspace_path.resolve()):
            raise SymlinkEscapeError(
                f"Resolved path '{resolved}' escapes workspace root '{self._workspace_path}'."
            )
        return resolved

    def check_integrity(
        self,
        allowed_files: Optional[List[str]] = None,
    ) -> WorkspaceIntegritySnapshot:
        """Verify workspace integrity and ensure only approved files were modified."""
        if not self._prepared:
            raise WorkspaceIntegrityError("Cannot check integrity of an unprepared workspace.")

        self.verify_target_immutability()

        current_hashes = self._hash_workspace_files()
        allowed_set = {Path(p).as_posix() for p in (allowed_files or [])}

        modified_files: List[str] = []
        added_files: List[str] = []
        removed_files: List[str] = []

        # Check modified or removed files
        for rel_path, baseline_hash in self._baseline_hashes.items():
            if rel_path not in current_hashes:
                removed_files.append(rel_path)
            elif current_hashes[rel_path] != baseline_hash:
                modified_files.append(rel_path)

        # Check added files
        for rel_path in current_hashes:
            if rel_path not in self._baseline_hashes:
                added_files.append(rel_path)

        # Assert containment against allowed modification scope
        unexpected_modified = [f for f in modified_files if f not in allowed_set]
        unexpected_added = [f for f in added_files if f not in allowed_set]
        unexpected_removed = [f for f in removed_files if f not in allowed_set]

        if unexpected_modified or unexpected_added or unexpected_removed:
            violations = []
            if unexpected_modified:
                violations.append(f"Unexpected modifications: {unexpected_modified}")
            if unexpected_added:
                violations.append(f"Unexpected new files: {unexpected_added}")
            if unexpected_removed:
                violations.append(f"Unexpected deletions: {unexpected_removed}")
            raise WorkspaceIntegrityError(
                f"Publication workspace integrity check failed: {'; '.join(violations)}"
            )

        return WorkspaceIntegritySnapshot(
            baseline_file_count=len(self._baseline_hashes),
            modified_files=modified_files,
            added_files=added_files,
            removed_files=removed_files,
            is_contained=True,
            target_repo_immutable=True,
        )

    def cleanup(self) -> None:
        """Safely destroy scratch publication workspace directory."""
        if self._cleaned:
            return
        if self._workspace_path.exists():
            try:
                shutil.rmtree(self._workspace_path, ignore_errors=True)
            except Exception as e:
                raise PublicationWorkspaceError(
                    f"Failed to cleanup workspace directory '{self._workspace_path}': {e}"
                ) from e
        self._cleaned = True

    def __enter__(self) -> "PublicationWorkspace":
        self.prepare()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()
