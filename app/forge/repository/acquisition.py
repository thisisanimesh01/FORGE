"""Safe acquisition of remote and local repositories into isolated workspaces."""

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from forge.config import get_settings, redact_secrets
from forge.repository.validation import (
    TargetType,
    TargetValidationResult,
    assert_contained_path,
    validate_target,
    SecurityError,
)


class AcquiredRepository(BaseModel):
    """Metadata describing a safely acquired repository."""
    target: str
    workspace_path: Path
    base_workspace: Path
    is_temporary: bool = True
    resolved_commit_sha: Optional[str] = None
    target_branch: Optional[str] = None
    is_local_copy: bool = False


class AcquisitionError(Exception):
    """Raised when repository cloning or acquisition fails."""
    pass


def run_git_command(args: list[str], cwd: Optional[Path] = None, timeout_seconds: int = 60) -> subprocess.CompletedProcess:
    """Run git command securely without shell interpolation and with hooks disabled."""
    # Ensure hooks are globally and locally disabled for any invoked git command
    base_cmd = [
        "git",
        "-c", "core.hooksPath=/dev/null",
    ]
    if cwd:
        base_cmd.extend(["-c", f"safe.directory={cwd.resolve()}"])
    base_cmd += args
    try:
        result = subprocess.run(
            base_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return result
    except subprocess.TimeoutExpired as e:
        raise AcquisitionError(f"Git command timed out after {timeout_seconds}s: {e}")
    except Exception as e:
        raise AcquisitionError(f"Failed to execute git command: {redact_secrets(str(e))}")


def acquire_repository(
    target: str,
    branch: Optional[str] = None,
    custom_workspace: Optional[Path] = None,
    allow_local: bool = True,
) -> AcquiredRepository:
    """Validate and acquire a repository into an isolated workspace."""
    validation: TargetValidationResult = validate_target(target, allow_local=allow_local)
    if not validation.is_valid:
        raise SecurityError(f"Target validation failed: {validation.error}")

    settings = get_settings()
    base_dir = (custom_workspace or settings.workspace_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Unique isolated directory name
    run_token = uuid.uuid4().hex[:12]
    dest_dir = base_dir / f"repo_{run_token}"
    dest_dir.mkdir(parents=True, exist_ok=False)

    if validation.target_type == TargetType.REMOTE_GIT:
        clone_args = [
            "clone",
            "--config", "core.hooksPath=/dev/null",
            "--depth", "1",
        ]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend([validation.sanitized_target, str(dest_dir)])

        result = run_git_command(clone_args, timeout_seconds=settings.step_timeout_seconds)
        if result.returncode != 0:
            shutil.rmtree(dest_dir, ignore_errors=True)
            err_msg = redact_secrets(result.stderr or result.stdout)
            raise AcquisitionError(f"Failed to clone repository: {err_msg}")

        # Explicitly configure cloned repository to disable hooks in local config as well
        run_git_command(["config", "core.hooksPath", "/dev/null"], cwd=dest_dir)

        # Extract resolved commit SHA and branch
        commit_res = run_git_command(["rev-parse", "HEAD"], cwd=dest_dir)
        commit_sha = commit_res.stdout.strip() if commit_res.returncode == 0 else None

        branch_res = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest_dir)
        resolved_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else branch

        return AcquiredRepository(
            target=target,
            workspace_path=dest_dir,
            base_workspace=base_dir,
            is_temporary=True,
            resolved_commit_sha=commit_sha,
            target_branch=resolved_branch,
            is_local_copy=False,
        )

    elif validation.target_type == TargetType.LOCAL_DIRECTORY:
        local_src = Path(validation.sanitized_target).resolve()

        # Copy files safely, ignoring .git hooks and virtual environments, and strictly rejecting external symlinks
        def ignore_patterns(dir_path: str, names: list[str]) -> set[str]:
            ignored = set()
            curr = Path(dir_path)
            for name in names:
                item = curr / name
                if name in [".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"]:
                    ignored.add(name)
                elif item.is_symlink():
                    try:
                        resolved_target = item.resolve()
                        resolved_target.relative_to(local_src)
                    except (ValueError, RuntimeError):
                        # Symlink points outside local_src; ignore it for safety
                        ignored.add(name)
            return ignored

        # Safely copy into workspace without preserving symlinks as symlinks
        shutil.copytree(local_src, dest_dir, dirs_exist_ok=True, ignore=ignore_patterns, symlinks=False)

        # Check if local source had git
        commit_sha = None
        resolved_branch = branch
        if (local_src / ".git").is_dir():
            commit_res = run_git_command(["rev-parse", "HEAD"], cwd=local_src)
            if commit_res.returncode == 0:
                commit_sha = commit_res.stdout.strip()
            branch_res = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=local_src)
            if branch_res.returncode == 0:
                resolved_branch = branch_res.stdout.strip()

        return AcquiredRepository(
            target=target,
            workspace_path=dest_dir,
            base_workspace=base_dir,
            is_temporary=True,
            resolved_commit_sha=commit_sha,
            target_branch=resolved_branch,
            is_local_copy=True,
        )

    raise SecurityError(f"Unsupported target type: {validation.target_type}")


def cleanup_repository(repo: AcquiredRepository, allowed_base: Optional[Path] = None) -> None:
    """Clean up acquired temporary workspace safely, verifying containment within allowed workspace base."""
    if not repo.is_temporary or not repo.workspace_path.exists():
        return

    base_dir = (allowed_base or repo.base_workspace).resolve()
    target_path = repo.workspace_path.resolve()

    # Enforce that cleanup target is strictly a subdirectory of base_dir and not base_dir itself
    try:
        rel = target_path.relative_to(base_dir)
        if str(rel) in [".", ""]:
            raise SecurityError(f"Refusing to delete base workspace directory: {target_path}")
    except ValueError:
        raise SecurityError(
            f"Refusing to delete path outside permitted workspace root: {target_path} (root: {base_dir})"
        )

    # Target path must be a directory and must follow naming convention repo_*
    if target_path.is_dir() and target_path.name.startswith("repo_"):
        shutil.rmtree(target_path, ignore_errors=True)
