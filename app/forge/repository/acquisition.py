"""Safe acquisition of remote and local repositories into isolated workspaces."""

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.config import get_settings, redact_secrets
from forge.repository.git_mediator import GitMediator
from forge.repository.protection import (
    create_copytree_ignore,
    purge_protected_paths_from_workspace,
    purge_protected_git_metadata,
    GitMetadataProtectionError,
)
from forge.repository.validation import (
    TargetType,
    TargetValidationResult,
    assert_contained_path,
    validate_target,
    SecurityError,
)


class AcquiredRepository(BaseModel):
    """Metadata describing a safely acquired repository."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    target: str
    workspace_path: Path
    base_workspace: Path
    is_temporary: bool = True
    resolved_commit_sha: Optional[str] = None
    target_branch: Optional[str] = None
    is_local_copy: bool = False
    trusted_git_path: Optional[Path] = None
    upstream_commit_sha: Optional[str] = None
    analysis_commit_sha: Optional[str] = None
    mediator: Optional[Any] = None


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
    commit_sha: Optional[str] = None,
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
    trusted_dir = base_dir / f"trusted_{run_token}.git"

    target_commit = (commit_sha or "").strip() or None
    if not target_commit and branch and re.match(r"^[0-9a-fA-F]{40}$", branch.strip()):
        target_commit = branch.strip()

    if validation.target_type == TargetType.REMOTE_GIT:
        # Clone into isolated trusted repository mirror outside analysis workspace
        clone_args = [
            "clone",
            "--config", "core.hooksPath=/dev/null",
        ]
        if branch and not target_commit:
            clone_args.extend(["--branch", branch])
        clone_args.extend([validation.sanitized_target, str(trusted_dir)])

        result = run_git_command(clone_args, timeout_seconds=settings.step_timeout_seconds)
        if result.returncode != 0:
            shutil.rmtree(dest_dir, ignore_errors=True)
            shutil.rmtree(trusted_dir, ignore_errors=True)
            err_msg = redact_secrets(result.stderr or result.stdout)
            raise AcquisitionError(f"Failed to clone repository: {err_msg}")

        # Secure permissions on trusted repository (user-only, mode 0700)
        try:
            os.chmod(trusted_dir, 0o700)
        except OSError:
            pass

        # Explicitly configure cloned repository to disable hooks in local config
        run_git_command(["config", "core.hooksPath", "/dev/null"], cwd=trusted_dir)

        if target_commit:
            run_git_command(["checkout", target_commit], cwd=trusted_dir)

        # Extract authentic upstream commit SHA and branch from trusted mirror
        commit_res = run_git_command(["rev-parse", "HEAD"], cwd=trusted_dir)
        upstream_commit_sha = commit_res.stdout.strip() if commit_res.returncode == 0 else target_commit

        branch_res = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=trusted_dir)
        resolved_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else branch

        # Instantiate mediator between trusted repository and analysis workspace
        mediator = GitMediator(trusted_git_path=trusted_dir, workspace_path=dest_dir)

        try:
            # Export sanitized working tree into dest_dir (excluding all protected paths)
            mediator.export_sanitized_workspace(dest_dir)
            purge_protected_paths_from_workspace(dest_dir)

            # Initialize clean local Git repository in analysis workspace containing only sanitized files
            run_git_command(["init"], cwd=dest_dir)
            run_git_command(["config", "user.name", "FORGE Analysis Agent"], cwd=dest_dir)
            run_git_command(["config", "user.email", "agent@forge.local"], cwd=dest_dir)
            run_git_command(["config", "core.hooksPath", "/dev/null"], cwd=dest_dir)
            run_git_command(["add", "."], cwd=dest_dir)
            run_git_command(["commit", "-m", "Sanitized analysis workspace snapshot"], cwd=dest_dir)
            analysis_commit_res = run_git_command(["rev-parse", "HEAD"], cwd=dest_dir)
            analysis_commit_sha = analysis_commit_res.stdout.strip() if analysis_commit_res.returncode == 0 else None
        except Exception as e:
            shutil.rmtree(dest_dir, ignore_errors=True)
            shutil.rmtree(trusted_dir, ignore_errors=True)
            raise AcquisitionError(f"Failed to export sanitized repository workspace: {e}") from e

        return AcquiredRepository(
            target=target,
            workspace_path=dest_dir,
            base_workspace=base_dir,
            is_temporary=True,
            resolved_commit_sha=upstream_commit_sha,
            target_branch=resolved_branch,
            is_local_copy=False,
            trusted_git_path=trusted_dir,
            upstream_commit_sha=upstream_commit_sha,
            analysis_commit_sha=analysis_commit_sha,
            mediator=mediator,
        )

    elif validation.target_type == TargetType.LOCAL_DIRECTORY:
        local_src = Path(validation.sanitized_target).resolve()
        has_git = (local_src / ".git").is_dir()
        has_commits = False
        if has_git:
            commit_chk = run_git_command(["rev-parse", "HEAD"], cwd=local_src)
            has_commits = (commit_chk.returncode == 0)

        if has_git and has_commits:
            # Clone from local source into isolated trusted repository mirror outside analysis workspace
            clone_args = [
                "clone",
                "--no-hardlinks",
                "--config", "core.hooksPath=/dev/null",
                str(local_src),
                str(trusted_dir),
            ]
            res_clone = run_git_command(clone_args, timeout_seconds=settings.step_timeout_seconds)
            if res_clone.returncode != 0:
                shutil.rmtree(dest_dir, ignore_errors=True)
                shutil.rmtree(trusted_dir, ignore_errors=True)
                err_msg = redact_secrets(res_clone.stderr or res_clone.stdout)
                raise AcquisitionError(f"Failed to clone local repository: {err_msg}")

            try:
                os.chmod(trusted_dir, 0o700)
            except OSError:
                pass

            run_git_command(["config", "core.hooksPath", "/dev/null"], cwd=trusted_dir)

            if target_commit:
                run_git_command(["checkout", target_commit], cwd=trusted_dir)
            elif branch:
                run_git_command(["checkout", branch], cwd=trusted_dir)

            commit_res = run_git_command(["rev-parse", "HEAD"], cwd=trusted_dir)
            upstream_commit_sha = commit_res.stdout.strip() if commit_res.returncode == 0 else target_commit

            branch_res = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=trusted_dir)
            resolved_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else branch

            mediator = GitMediator(trusted_git_path=trusted_dir, workspace_path=dest_dir)

            try:
                mediator.export_sanitized_workspace(dest_dir)
                purge_protected_paths_from_workspace(dest_dir)

                # Initialize clean local Git repository in analysis workspace containing only sanitized files
                run_git_command(["init"], cwd=dest_dir)
                run_git_command(["config", "user.name", "FORGE Analysis Agent"], cwd=dest_dir)
                run_git_command(["config", "user.email", "agent@forge.local"], cwd=dest_dir)
                run_git_command(["config", "core.hooksPath", "/dev/null"], cwd=dest_dir)
                run_git_command(["add", "."], cwd=dest_dir)
                run_git_command(["commit", "-m", "Sanitized analysis workspace snapshot"], cwd=dest_dir)
                analysis_commit_res = run_git_command(["rev-parse", "HEAD"], cwd=dest_dir)
                analysis_commit_sha = analysis_commit_res.stdout.strip() if analysis_commit_res.returncode == 0 else None
            except Exception as e:
                shutil.rmtree(dest_dir, ignore_errors=True)
                shutil.rmtree(trusted_dir, ignore_errors=True)
                raise AcquisitionError(f"Failed to export sanitized repository workspace: {e}") from e

            return AcquiredRepository(
                target=target,
                workspace_path=dest_dir,
                base_workspace=base_dir,
                is_temporary=True,
                resolved_commit_sha=upstream_commit_sha,
                target_branch=resolved_branch,
                is_local_copy=True,
                trusted_git_path=trusted_dir,
                upstream_commit_sha=upstream_commit_sha,
                analysis_commit_sha=analysis_commit_sha,
                mediator=mediator,
            )
        else:
            # Non-git directory or uncommitted git repo: copy unprotected files
            ignore_patterns = create_copytree_ignore(local_src)
            shutil.copytree(local_src, dest_dir, dirs_exist_ok=True, ignore=ignore_patterns, symlinks=False)
            purge_protected_paths_from_workspace(dest_dir)

            # Strip any uncommitted .git directory copied from source to eliminate hooks
            ws_git = dest_dir / ".git"
            if ws_git.exists():
                shutil.rmtree(ws_git, ignore_errors=True)

            # Initialize clean git repository in analysis workspace
            run_git_command(["init"], cwd=dest_dir)
            run_git_command(["config", "user.name", "FORGE Analysis Agent"], cwd=dest_dir)
            run_git_command(["config", "user.email", "agent@forge.local"], cwd=dest_dir)
            run_git_command(["config", "core.hooksPath", "/dev/null"], cwd=dest_dir)
            run_git_command(["add", "."], cwd=dest_dir)
            run_git_command(["commit", "-m", "Sanitized analysis workspace snapshot"], cwd=dest_dir)
            analysis_commit_res = run_git_command(["rev-parse", "HEAD"], cwd=dest_dir)
            analysis_commit_sha = analysis_commit_res.stdout.strip() if analysis_commit_res.returncode == 0 else None

            return AcquiredRepository(
                target=target,
                workspace_path=dest_dir,
                base_workspace=base_dir,
                is_temporary=True,
                resolved_commit_sha=None,
                target_branch=branch,
                is_local_copy=True,
                trusted_git_path=None,
                upstream_commit_sha=None,
                analysis_commit_sha=analysis_commit_sha,
                mediator=None,
            )

    raise SecurityError(f"Unsupported target type: {validation.target_type}")


def cleanup_repository(repo: AcquiredRepository, allowed_base: Optional[Path] = None) -> None:
    """Clean up acquired temporary workspace and trusted mirror safely, verifying containment."""
    if not repo.is_temporary:
        return

    base_dir = (allowed_base or repo.base_workspace).resolve()

    paths_to_clean = []
    if repo.workspace_path and repo.workspace_path.exists():
        paths_to_clean.append(repo.workspace_path)
    if repo.trusted_git_path and repo.trusted_git_path.exists():
        paths_to_clean.append(repo.trusted_git_path)

    for p in paths_to_clean:
        target_path = p.resolve()
        try:
            rel = target_path.relative_to(base_dir)
            if str(rel) in [".", ""]:
                raise SecurityError(f"Refusing to delete base workspace directory: {target_path}")
        except ValueError:
            raise SecurityError(
                f"Refusing to delete path outside permitted workspace root: {target_path} (root: {base_dir})"
            )
        if target_path.is_dir() and (target_path.name.startswith("repo_") or target_path.name.startswith("trusted_")):
            shutil.rmtree(target_path, ignore_errors=True)
