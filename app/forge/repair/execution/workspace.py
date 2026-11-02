"""Phase 8 Repair Execution Architecture — Isolated Workspace Implementation.

Manages isolated scratch workspaces, guarantees target repository immutability,
enforces git commit identity verification, computes file baseline manifests,
checks filesystem integrity, and executes vetted commands inside Docker sandboxes.
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
from forge.repair.execution.failures import (
    CommitMismatchError,
    RepositoryAcquisitionError,
    ResourceLimitViolationError,
    SandboxUnavailableError,
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
    WorkspaceCleanupError,
    WorkspaceCollisionError,
    WorkspaceIntegrityViolationError,
    WorkspaceProvisioningError,
    WorkspaceReuseForbiddenError,
)
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    NetworkPolicy,
    SandboxRequest,
    SandboxResult,
)


class RepairWorkspace:
    """Isolated, disposable scratch workspace for safe repair execution and verification."""

    def __init__(
        self,
        target_repo_path: Union[str, Path],
        base_commit_sha: str,
        workspace_id: Optional[str] = None,
        scratch_root: Optional[Union[str, Path]] = None,
        policy: Optional[RepairExecutionPolicy] = None,
    ):
        self._target_repo_path = Path(target_repo_path).resolve()
        if not self._target_repo_path.exists():
            raise RepositoryAcquisitionError(
                f"Target repository path '{self._target_repo_path}' does not exist"
            )

        self._base_commit_sha = base_commit_sha.strip().lower()
        if not self._base_commit_sha:
            raise RepositoryAcquisitionError("base_commit_sha cannot be empty")

        self._workspace_id = workspace_id or f"ws_{uuid.uuid4().hex[:12]}"
        self._policy = policy or RepairExecutionPolicy()

        root = (
            Path(scratch_root).resolve()
            if scratch_root
            else Path(tempfile.gettempdir()).resolve() / "forge_repair_workspaces"
        )
        self._workspace_path = root / self._workspace_id

        # Guarantee immutability boundary: workspace must never collide with or be inside target repo
        if self._workspace_path == self._target_repo_path or self._target_repo_path.is_relative_to(
            self._workspace_path
        ):
            raise WorkspaceCollisionError(
                "Workspace path cannot match or contain target repository path"
            )
        try:
            if self._workspace_path.is_relative_to(self._target_repo_path):
                raise WorkspaceCollisionError(
                    "Workspace path cannot be inside target repository directory"
                )
        except ValueError:
            pass

        self._initial_target_git_status: Optional[str] = self._capture_target_git_status()
        self._baseline_hashes: Dict[str, str] = {}
        self._prepared: bool = False
        self._cleaned: bool = False
        self._isolation_verified: bool = False

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

    @property
    def is_prepared(self) -> bool:
        return self._prepared

    @property
    def is_cleaned(self) -> bool:
        return self._cleaned

    @property
    def baseline_hashes(self) -> Dict[str, str]:
        return dict(self._baseline_hashes)

    @property
    def baseline_manifest(self) -> Dict[str, str]:
        return dict(self._baseline_hashes)

    def prepare(self) -> None:
        """Create the isolated scratch workspace, verify git commit, and capture baseline hashes."""
        if self._cleaned:
            raise WorkspaceReuseForbiddenError(
                f"Workspace '{self._workspace_id}' was already cleaned up and cannot be reused"
            )
        if self._prepared:
            return

        # 1. Verify Git commit on target repository if .git exists
        self._verify_target_git_commit()

        # 2. Check collision
        if self._workspace_path.exists() and any(self._workspace_path.iterdir()):
            raise WorkspaceCollisionError(
                f"Workspace path '{self._workspace_path}' already exists and is non-empty"
            )

        self._workspace_path.parent.mkdir(parents=True, exist_ok=True)
        self._workspace_path.mkdir(parents=True, exist_ok=True)

        # 3. Copy repository contents into isolated workspace (excluding VCS hooks and caches)
        def _ignore_patterns(path: str, names: list[str]) -> set[str]:
            ignored = set()
            for n in names:
                if n in (".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"):
                    ignored.add(n)
            return ignored

        try:
            shutil.copytree(
                self._target_repo_path,
                self._workspace_path,
                dirs_exist_ok=True,
                ignore=_ignore_patterns,
                symlinks=False,
            )
        except Exception as e:
            raise WorkspaceProvisioningError(
                f"Failed to copy target repository to workspace '{self._workspace_path}': {e}"
            ) from e

        # 4. Capture baseline file hashes for integrity tracking
        self._baseline_hashes = self._hash_workspace_files()
        self._prepared = True

    def _verify_target_git_commit(self) -> None:
        """Inspect HEAD commit of target repo and verify it matches base_commit_sha."""
        git_dir = self._target_repo_path / ".git"
        if not git_dir.exists():
            return

        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._target_repo_path),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except Exception as e:
            raise RepositoryAcquisitionError(
                f"Failed to query git HEAD in target repository '{self._target_repo_path}': {e}"
            ) from e

        if res.returncode != 0:
            raise RepositoryAcquisitionError(
                f"git rev-parse HEAD failed with exit code {res.returncode}: {res.stderr.strip()}"
            )

        head_sha = res.stdout.strip().lower()
        # Verify SHA match (exact or prefix match if short SHA provided)
        if not (head_sha.startswith(self._base_commit_sha) or self._base_commit_sha.startswith(head_sha)):
            raise CommitMismatchError(
                f"Target repository HEAD '{head_sha}' does not match expected base_commit_sha '{self._base_commit_sha}'"
            )

    def _hash_workspace_files(self) -> Dict[str, str]:
        """Compute SHA256 hashes for all non-directory files currently in workspace."""
        hashes: Dict[str, str] = {}
        ws_root = self._workspace_path.resolve()
        for root, dirs, files in os.walk(self._workspace_path, followlinks=False):
            dirs.sort()
            for fname in sorted(files):
                full_path = Path(root) / fname
                rel_path = full_path.relative_to(self._workspace_path).as_posix()
                if os.path.islink(full_path):
                    # Record symlink destination
                    target_real = os.path.realpath(full_path)
                    hashes[rel_path] = f"symlink:{target_real}"
                else:
                    try:
                        hasher = hashlib.sha256()
                        with open(full_path, "rb") as f:
                            while chunk := f.read(65536):
                                hasher.update(chunk)
                        hashes[rel_path] = hasher.hexdigest()
                    except (OSError, IOError) as e:
                        hashes[rel_path] = f"error:{e}"
        return hashes

    def verify_isolation(self) -> bool:
        """Verify repository immutability, boundary containment, and Docker readiness."""
        if not self._prepared:
            raise WorkspaceIntegrityViolationError("Workspace has not been prepared")

        # 1. Verify target repository immutability
        if not self.check_target_immutability():
            raise TargetRepoImmutabilityViolationError(
                f"Target repository '{self._target_repo_path}' has been mutated"
            )

        # 2. Verify physical path separation
        if self._workspace_path == self._target_repo_path:
            raise TargetRepoImmutabilityViolationError(
                "Workspace path must not equal target repository path"
            )

        # 3. Verify Docker runtime status
        sandbox = DockerSandbox()
        docker_available = sandbox.is_available()

        self._isolation_verified = True
        return docker_available

    def validate_containment(self, file_path: Union[str, Path]) -> Path:
        """Resolve a file path and verify that it remains strictly within the workspace boundary.

        Raises:
            ScopeContainmentViolationError: If path attempts directory traversal or escapes via symlink.
        """
        raw = str(file_path).strip()
        if not raw:
            raise ScopeContainmentViolationError("Path cannot be empty")
        if ".." in raw.split("/") or ".." in raw.split("\\"):
            raise ScopeContainmentViolationError(f"Path traversal ('..') forbidden in '{file_path}'")

        ws_resolved = self._workspace_path.resolve()
        raw_target = ws_resolved / raw

        # Check symlink escapes on unresolved path
        if os.path.islink(raw_target):
            link_target = Path(os.path.realpath(raw_target))
            try:
                link_target.relative_to(ws_resolved)
            except ValueError:
                raise ScopeContainmentViolationError(
                    f"Symlink '{file_path}' points outside workspace: '{link_target}'"
                )

        target = raw_target.resolve()
        try:
            target.relative_to(ws_resolved)
        except ValueError:
            raise ScopeContainmentViolationError(
                f"Path '{file_path}' escapes workspace boundary '{self._workspace_path}'"
            )

        return target

    def check_integrity(self, allowed_files: Optional[List[str]] = None) -> Dict[str, Any]:
        """Verify filesystem integrity against baseline hashes, enforcing allowed write boundaries.

        Raises:
            WorkspaceIntegrityViolationError: If unexpected files were modified, added, deleted, or symlinked.
            TargetRepoImmutabilityViolationError: If target repository was touched.
        """
        if not self._prepared:
            raise WorkspaceIntegrityViolationError("Workspace is not prepared")

        # Confirm target immutability
        if not self.check_target_immutability():
            raise TargetRepoImmutabilityViolationError("Target repository was modified during execution")

        current_hashes = self._hash_workspace_files()

        baseline_set = set(self._baseline_hashes.keys())
        current_set = set(current_hashes.keys())

        added = sorted(list(current_set - baseline_set))
        deleted = sorted(list(baseline_set - current_set))
        modified = sorted([p for p in (baseline_set & current_set) if self._baseline_hashes[p] != current_hashes[p]])

        # Detect symlink escapes in any current file
        ws_resolved = self._workspace_path.resolve()
        for p in current_set:
            if current_hashes[p].startswith("symlink:"):
                target_str = current_hashes[p].removeprefix("symlink:")
                try:
                    Path(target_str).relative_to(ws_resolved)
                except ValueError:
                    raise WorkspaceIntegrityViolationError(
                        f"Symlink escape detected at '{p}' pointing to external path '{target_str}'"
                    )

        if allowed_files is not None:
            norm_allowed = {Path(f).as_posix().strip() for f in allowed_files}
            unauthorized = []
            for m in modified:
                if m not in norm_allowed:
                    unauthorized.append(f"modified: {m}")
            for a in added:
                if a not in norm_allowed:
                    unauthorized.append(f"added: {a}")
            for d in deleted:
                if d not in norm_allowed:
                    unauthorized.append(f"deleted: {d}")

            if unauthorized:
                raise WorkspaceIntegrityViolationError(
                    f"Workspace integrity violation: unauthorized changes detected: {unauthorized}"
                )

        return {
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "valid": True,
        }

    def run_command_in_sandbox(
        self,
        command: Union[str, List[str]],
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 30.0,
        read_only: bool = False,
        limits: Optional[ExecutionLimits] = None,
        run_id: Optional[str] = None,
    ) -> SandboxResult:
        """Execute a vetted command inside DockerSandbox.

        Raises:
            SandboxUnavailableError: If Docker runtime is unavailable (fail-closed).
            ResourceLimitViolationError: If timeout or process limit exceeded.
        """
        sandbox = DockerSandbox()
        if not sandbox.is_available():
            raise SandboxUnavailableError(
                "Docker sandbox runtime is not available; repository execution refused (fail closed)"
            )

        # Normalize command arguments
        if isinstance(command, str):
            cmd_list = [command] + (args or [])
        else:
            cmd_list = list(command) + (args or [])

        # Validate command against execution policy
        validated_cmd = self._policy.validate_command(cmd_list)
        sanitized_env = self._policy.sanitize_environment(env)

        # Validate cwd containment
        if cwd and cwd != ".":
            self.validate_containment(cwd)

        exec_limits = limits or ExecutionLimits(timeout_seconds=min(timeout_seconds, 300.0))

        req = SandboxRequest(
            request_id=f"sbr_{uuid.uuid4().hex[:12]}",
            run_id=run_id or f"repair_{self._workspace_id}",
            workspace_path=self._workspace_path,
            command=validated_cmd,
            relative_cwd=cwd or ".",
            limits=exec_limits,
            env_vars=sanitized_env,
            network_policy=NetworkPolicy.DISABLED,
            read_only_workspace=read_only,
            provenance="forge:repair_workspace",
        )

        result = sandbox.run(req)

        if result.timed_out:
            raise ResourceLimitViolationError(
                f"Command '{validated_cmd[0]}' timed out after {exec_limits.timeout_seconds}s"
            )

        return result

    def cleanup(self) -> None:
        """Safely remove the scratch workspace directory."""
        if self._cleaned:
            return
        if self._workspace_path.exists():
            try:
                shutil.rmtree(self._workspace_path, ignore_errors=False)
            except Exception as e:
                raise WorkspaceCleanupError(
                    f"Failed to delete scratch workspace '{self._workspace_path}': {e}"
                ) from e

        self._prepared = False
        self._cleaned = True

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
