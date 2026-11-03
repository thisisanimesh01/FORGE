"""Phase 9 Git Publication Architecture — Remote Branch Publisher.

Implements safe, verified Git remote branch publication enforcing strict preconditions,
zero force-pushes, protected branch defenses, collision avoidance, and post-push
cryptographic commit and tree verification.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import uuid

from forge.config import redact_secrets
from forge.publication.failures import (
    ProtectedBranchViolationError,
    RemoteAuthenticationError,
    RemoteBranchCollisionError,
    RemoteBranchPushError,
    RemoteBranchVerificationError,
    RemotePublicationError,
    RemoteRepositoryVerificationError,
)
from forge.publication.models import (
    RemoteBranchRecord,
    RemoteRepositorySpec,
    StagedCommitRecord,
)
from forge.publication.policy import GitPublicationPolicy, is_protected_branch
from forge.publication.providers.base import PullRequestProviderProtocol


def parse_repository_owner_and_name(remote_url: str) -> Tuple[str, str]:
    """Parse owner and repository name from SSH or HTTPS Git URLs."""
    clean_url = remote_url.strip()
    if clean_url.endswith(".git"):
        clean_url = clean_url[:-4]

    # Handle SCP-like SSH syntax: git@github.com:owner/repo
    if "@" in clean_url and ":" in clean_url and not clean_url.startswith(("http://", "https://", "ssh://", "file://")):
        path_part = clean_url.split(":", 1)[1]
        parts = [p for p in path_part.split("/") if p]
        if len(parts) >= 2:
            return parts[-2], parts[-1]

    # Handle standard URL schemes
    parsed = urllib.parse.urlparse(clean_url)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if len(parts) == 1:
        return "local", parts[0]

    return "unknown", "unknown"


class RemoteBranchPublisher:
    """Publishes verified local staged commits to authorized Git remotes with defense-in-depth."""

    def __init__(
        self,
        policy: Optional[GitPublicationPolicy] = None,
        provider: Optional[PullRequestProviderProtocol] = None,
    ):
        self.policy = policy or GitPublicationPolicy()
        self.provider = provider

    def verify_remote_repository_identity(
        self,
        repository_path: Path,
        remote_name: str = "origin",
        expected_owner: Optional[str] = None,
        expected_repo: Optional[str] = None,
        expected_base_sha: Optional[str] = None,
    ) -> RemoteRepositorySpec:
        """Verify remote repository identity, URL scheme, owner/name, and accessibility."""
        repo_dir = repository_path.resolve()
        if not repo_dir.exists():
            raise RemoteRepositoryVerificationError(f"Repository directory does not exist: {repo_dir}")

        # 1. Fetch configured remote URL
        try:
            res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "remote", "get-url", remote_name],
                cwd=str(repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            raw_url = res.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RemoteRepositoryVerificationError(
                f"Failed to query remote '{remote_name}' in repository '{repo_dir}': {redact_secrets(e.stderr)}"
            ) from e

        if not raw_url:
            raise RemoteRepositoryVerificationError(f"Remote '{remote_name}' has an empty or unconfigured URL.")

        # 2. Validate URL scheme against policy
        parsed = urllib.parse.urlparse(raw_url)
        scheme = parsed.scheme.lower()
        if not scheme and "@" in raw_url and ":" in raw_url:
            scheme = "ssh"
        elif not scheme and raw_url.startswith("/"):
            scheme = "file"

        if scheme and scheme not in self.policy.allowed_remote_schemes:
            raise RemoteRepositoryVerificationError(
                f"Remote URL scheme '{scheme}' is forbidden by publication policy. Allowed: {self.policy.allowed_remote_schemes}"
            )

        # 3. Extract owner and repository name
        owner, repo_name = parse_repository_owner_and_name(raw_url)

        if expected_owner and owner.lower() != expected_owner.lower():
            raise RemoteRepositoryVerificationError(
                f"Remote repository owner mismatch: expected '{expected_owner}', observed '{owner}'."
            )
        if expected_repo and repo_name.lower() != expected_repo.lower():
            raise RemoteRepositoryVerificationError(
                f"Remote repository name mismatch: expected '{expected_repo}', observed '{repo_name}'."
            )

        # 4. Provider-side verification if provider is configured and remote is a provider host
        provider_name = self.provider.get_provider_name() if self.provider else "git"
        can_push = True
        default_branch = "main"
        is_private = False

        if self.provider and ("github.com" in raw_url or provider_name == "github") and scheme in ("https", "ssh"):
            spec = self.provider.verify_repository(owner, repo_name)
            default_branch = spec.default_branch
            is_private = spec.is_private
            can_push = spec.can_push
            if not can_push:
                raise RemoteAuthenticationError(
                    f"Authenticated credentials do not have write (push) permission for repository '{owner}/{repo_name}'."
                )

        return RemoteRepositorySpec(
            provider=provider_name,
            owner=owner,
            repository=repo_name,
            default_branch=default_branch,
            remote_url=redact_secrets(raw_url),
            is_private=is_private,
            can_push=can_push,
        )

    def check_remote_branch_collision(
        self,
        repository_path: Path,
        remote_name: str,
        branch_name: str,
    ) -> Optional[str]:
        """Check if branch exists on remote. Returns commit SHA if exists, None otherwise."""
        try:
            res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "ls-remote", "--heads", remote_name, branch_name],
                cwd=str(repository_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            output = res.stdout.strip()
            if not output:
                return None
            # Format: <sha>\trefs/heads/<branch>
            for line in output.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == f"refs/heads/{branch_name}":
                    return parts[0]
            return None
        except subprocess.CalledProcessError as e:
            # If remote is unreachable or network error, raise RemotePublicationError
            raise RemotePublicationError(
                f"Failed to query remote references for '{remote_name}': {redact_secrets(e.stderr)}"
            ) from e

    def publish_remote_branch(
        self,
        repository_path: Path,
        remote_name: str,
        remote_url: str,
        branch_name: str,
        staged_commit: StagedCommitRecord,
        repair_id: str,
        publication_id: str,
        plan_id: str,
        decision_id: str,
    ) -> RemoteBranchRecord:
        """Safely push the verified staged branch to the exact remote, and cryptographically verify."""
        repo_dir = repository_path.resolve()

        # 1. Enforce safety invariants on branch name
        if is_protected_branch(branch_name, self.policy):
            raise ProtectedBranchViolationError(
                f"Refusing to push to protected branch '{branch_name}'. Publication must target a dedicated 'forge/' branch."
            )

        if not branch_name.startswith(self.policy.permitted_branch_prefix):
            raise ProtectedBranchViolationError(
                f"Branch name '{branch_name}' does not start with required prefix '{self.policy.permitted_branch_prefix}'."
            )

        # 2. Verify local HEAD commit matches staged commit SHA
        try:
            head_sha = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", branch_name],
                cwd=str(repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RemoteBranchPushError(
                f"Local branch '{branch_name}' not found in workspace: {redact_secrets(e.stderr)}"
            ) from e

        if head_sha != staged_commit.commit_sha:
            raise RemoteBranchPushError(
                f"Local branch HEAD '{head_sha}' does not match approved staged commit SHA '{staged_commit.commit_sha}'."
            )

        # 3. Collision check on remote
        existing_sha = self.check_remote_branch_collision(repo_dir, remote_name, branch_name)
        if existing_sha:
            if existing_sha == staged_commit.commit_sha:
                # Already published with identical commit SHA — reconcile safely
                return RemoteBranchRecord(
                    remote_branch_id=f"rb_{uuid.uuid4().hex[:12]}",
                    repair_id=repair_id,
                    publication_id=publication_id,
                    plan_id=plan_id,
                    staged_commit_id=staged_commit.staging_id,
                    decision_id=decision_id,
                    remote_name=remote_name,
                    remote_url=redact_secrets(remote_url),
                    branch_name=branch_name,
                    commit_sha=staged_commit.commit_sha,
                    parent_commit_sha=staged_commit.parent_commit_sha or staged_commit.base_commit_sha,
                    tree_sha=staged_commit.tree_sha or "",
                    patch_sha256=staged_commit.patch_sha256,
                    pushed_at=datetime.now(timezone.utc),
                    verified_at=datetime.now(timezone.utc),
                    status="verified",
                    verification_details={"reconciled": True, "remote_sha": existing_sha},
                )
            raise RemoteBranchCollisionError(
                f"Remote branch '{branch_name}' already exists on '{remote_name}' with differing commit SHA '{existing_sha}'. "
                f"Overwriting remote history is strictly forbidden by publication policy."
            )

        # 4. Execute normal Git push without force flags
        push_cmd = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            remote_name,
            f"{branch_name}:{branch_name}",
        ]

        try:
            push_res = subprocess.run(
                push_cmd,
                cwd=str(repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            sanitized_err = redact_secrets(e.stderr)
            if "permission denied" in sanitized_err.lower() or "authentication failed" in sanitized_err.lower():
                raise RemoteAuthenticationError(
                    f"Authentication failed pushing branch '{branch_name}' to '{remote_name}': {sanitized_err}"
                ) from e
            raise RemoteBranchPushError(
                f"Failed to push branch '{branch_name}' to remote '{remote_name}': {sanitized_err}"
            ) from e

        # 5. Independent remote post-push verification
        remote_sha = self.check_remote_branch_collision(repo_dir, remote_name, branch_name)
        if not remote_sha:
            raise RemoteBranchVerificationError(
                f"Remote branch '{branch_name}' was not observed on '{remote_name}' following push."
            )

        if remote_sha != staged_commit.commit_sha:
            raise RemoteBranchVerificationError(
                f"Remote branch commit SHA mismatch: expected '{staged_commit.commit_sha}', observed '{remote_sha}'."
            )

        # 6. Verify remote commit parent and tree
        try:
            # Query parent commit
            parent_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", f"{staged_commit.commit_sha}^"],
                cwd=str(repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            parent_sha = parent_res.stdout.strip()
            if parent_sha != staged_commit.base_commit_sha:
                raise RemoteBranchVerificationError(
                    f"Published commit parent mismatch: expected '{staged_commit.base_commit_sha}', observed '{parent_sha}'."
                )

            # Query tree
            tree_res = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "rev-parse", f"{staged_commit.commit_sha}^{{tree}}"],
                cwd=str(repo_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            tree_sha = tree_res.stdout.strip()
            if staged_commit.tree_sha and tree_sha != staged_commit.tree_sha:
                raise RemoteBranchVerificationError(
                    f"Published commit tree mismatch: expected '{staged_commit.tree_sha}', observed '{tree_sha}'."
                )
        except subprocess.CalledProcessError as e:
            raise RemoteBranchVerificationError(
                f"Failed to inspect cryptographic properties of published commit '{staged_commit.commit_sha}': {redact_secrets(e.stderr)}"
            ) from e

        return RemoteBranchRecord(
            remote_branch_id=f"rb_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            publication_id=publication_id,
            plan_id=plan_id,
            staged_commit_id=staged_commit.staging_id,
            decision_id=decision_id,
            remote_name=remote_name,
            remote_url=redact_secrets(remote_url),
            branch_name=branch_name,
            commit_sha=staged_commit.commit_sha,
            parent_commit_sha=parent_sha,
            tree_sha=tree_sha,
            patch_sha256=staged_commit.patch_sha256,
            pushed_at=datetime.now(timezone.utc),
            verified_at=datetime.now(timezone.utc),
            status="verified",
            verification_details={
                "verified_remote_sha": remote_sha,
                "verified_parent_sha": parent_sha,
                "verified_tree_sha": tree_sha,
            },
        )
