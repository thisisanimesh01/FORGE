"""Comprehensive Unit & Security Test Suite for Phase 9 Milestone 3:
Remote Pull Request Creation & Upstream Integration.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional
import uuid

import httpx
import pytest
from typer.testing import CliRunner

from forge.analysis.findings import Finding
from forge.cli import app
from forge.publication.authorization import is_action_authorized
from forge.publication.failures import (
    DuplicatePullRequestError,
    ProtectedBranchViolationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
    PullRequestCreationError,
    RemoteAuthenticationError,
    RemoteBranchCollisionError,
    RemoteBranchPushError,
    RemoteBranchVerificationError,
    RemotePublicationError,
    RemoteRepositoryVerificationError,
)
from forge.publication.models import (
    CommitAuthor,
    PublicationAction,
    PublicationExecutionState,
    PublicationPlan,
    PublicationRequest,
    PublicationStatus,
    PullRequestDetails,
    PullRequestRecord,
    RemoteBranchRecord,
    RemotePublicationRecord,
    RemoteRepositorySpec,
    StagedCommitRecord,
)
from forge.publication.policy import GitPublicationPolicy
from forge.publication.pr_template import PullRequestFormatter
from forge.publication.providers.base import retry_idempotent_operation
from forge.publication.providers.github import GitHubPullRequestProvider
from forge.publication.remote_coordinator import RemotePublicationCoordinator
from forge.publication.remote_publisher import (
    RemoteBranchPublisher,
    parse_repository_owner_and_name,
)
from forge.publication.storage import PublicationStore
from forge.repair.execution.models import (
    ApprovalBinding,
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    PatchArtifact,
    RepairActor,
    RepairExecutionState,
    RepairRequest,
    ReviewDossier,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.schemas import FindingSeverity
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# -----------------------------------------------------------------------------
# Fixtures: Local Git Target and Remote Repositories
# -----------------------------------------------------------------------------

@pytest.fixture
def git_repos(tmp_path: Path):
    """Sets up a local working repository and a bare upstream remote repository."""
    # 1. Create upstream bare remote repo
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)

    # 2. Create local repo
    local_dir = tmp_path / "local_repo"
    local_dir.mkdir()
    subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=str(local_dir), check=True)
    subprocess.run(["git", "config", "user.email", "committer@test.local"], cwd=str(local_dir), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(local_dir), check=True)

    # Add initial commit
    file1 = local_dir / "app.py"
    file1.write_text("def run():\n    return 42\n")
    subprocess.run(["git", "add", "app.py"], cwd=str(local_dir), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(local_dir), check=True)

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(local_dir), check=True, capture_output=True, text=True
    ).stdout.strip()

    # Link origin to bare remote
    remote_url = f"file://{remote_dir.resolve()}"
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=str(local_dir), check=True)
    subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=str(local_dir), check=True)

    return {
        "local_dir": local_dir,
        "remote_dir": remote_dir,
        "remote_url": remote_url,
        "base_sha": base_sha,
    }


@pytest.fixture
def m3_env(tmp_path: Path, git_repos):
    """Provisions complete M3 testing environment with SQLite stores and verified repair."""
    db_path = tmp_path / "test_m3.db"
    init_database(db_path)

    forge_store = ForgeStore(db_path=db_path)
    exec_store = RepairExecutionStore(db_path=db_path)
    pub_store = PublicationStore(db_path=db_path)

    run_id = forge_store.create_run(str(git_repos["local_dir"]))
    finding = forge_store.create_finding(
        run_id=run_id,
        title="Index out of bounds error",
        description="Index exceeds array bounds in app.py",
        severity=FindingSeverity.HIGH,
    )

    repair_id = f"rep_{uuid.uuid4().hex[:12]}"
    patch_sha = "a" * 64
    patch_id = f"patch_{patch_sha[:12]}"

    repair_req = RepairRequest(
        repair_id=repair_id,
        finding_id=finding.finding_id,
        run_id=run_id,
        target_repo=str(git_repos["local_dir"]),
        base_commit_sha=git_repos["base_sha"],
        evidence_ids=["evi_test"],
        allowed_files=["app.py"],
        workspace_id="ws_test",
        state=RepairExecutionState.VERIFIED,
        patch_artifact_id=patch_id,
    )
    exec_store.save_repair_request(repair_req)

    patch = PatchArtifact(
        artifact_id=patch_id,
        repair_id=repair_id,
        finding_id=finding.finding_id,
        base_commit_sha=git_repos["base_sha"],
        target_file="app.py",
        diff_content="--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def run():\n-    return 42\n+    return 43\n",
        content_hash=patch_sha,
        lines_added=1,
        lines_deleted=1,
        hunks_count=1,
        affected_paths=["app.py"],
    )
    exec_store.save_patch_artifact(patch)

    dossier_id = f"dos_{uuid.uuid4().hex[:12]}"
    dossier = ReviewDossier(
        dossier_id=dossier_id,
        repair_id=repair_id,
        finding_id=finding.finding_id,
        finding_title=finding.title,
        finding_severity="HIGH",
        repository_target=str(git_repos["local_dir"]),
        base_commit_sha=git_repos["base_sha"],
        patch_artifact_id=patch_id,
        patch_sha256=patch_sha,
        unified_diff=patch.diff_content,
        validation_id="val_test",
        verification_id="ver_test",
        validation_outcome="ACCEPTED",
        verification_outcome="VERIFIED",
        workspace_id="ws_test",
        regression_analysis_summary="Zero new regressions detected.",
        regression_detected=False,
    )
    exec_store.save_review_dossier(dossier)

    binding = ApprovalBinding(
        repair_id=repair_id,
        finding_id=finding.finding_id,
        repository_target=str(git_repos["local_dir"]),
        base_commit_sha=git_repos["base_sha"],
        patch_artifact_id=patch_id,
        patch_sha256=patch_sha,
        validation_id="val_test",
        verification_id="ver_test",
        dossier_id=dossier_id,
        dossier_version="1.0.0",
        policy_version="1.0.0",
    )

    decision_id = f"dec_{uuid.uuid4().hex[:12]}"
    approval = ApprovalDecisionRecord(
        decision_id=decision_id,
        repair_id=repair_id,
        state=ApprovalState.APPROVED,
        decision="approve",
        authorized_action=AuthorizedAction.CREATE_PR,
        actor="lead_operator",
        reason="Verified defect fix with zero regressions.",
        binding=binding,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    exec_store.save_approval_decision(approval)

    # Create local branch and commit in local repo
    branch_name = f"forge/fix-{finding.finding_id[:12]}-{patch_sha[:8]}"
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=str(git_repos["local_dir"]), check=True)
    file1 = git_repos["local_dir"] / "app.py"
    file1.write_text("def run():\n    return 43\n")
    subprocess.run(["git", "add", "app.py"], cwd=str(git_repos["local_dir"]), check=True)
    subprocess.run(["git", "commit", "-m", "Resolve Index out of bounds error"], cwd=str(git_repos["local_dir"]), check=True)

    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repos["local_dir"]), check=True, capture_output=True, text=True
    ).stdout.strip()
    tree_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=str(git_repos["local_dir"]), check=True, capture_output=True, text=True
    ).stdout.strip()

    pub_id = f"pub_{uuid.uuid4().hex[:12]}"
    pub_req = PublicationRequest(
        publication_id=pub_id,
        repair_id=repair_id,
        decision_id=decision_id,
        action=PublicationAction.CREATE_PR,
    )
    pub_store.save_publication_request(pub_req)

    staged_commit = StagedCommitRecord(
        staging_id=f"stg_{uuid.uuid4().hex[:12]}",
        workspace_id="ws_test",
        plan_id=pub_id,
        repair_id=repair_id,
        decision_id=decision_id,
        finding_id=finding.finding_id,
        branch_name=branch_name,
        base_commit_sha=git_repos["base_sha"],
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        parent_commit_sha=git_repos["base_sha"],
        patch_artifact_id=patch_id,
        patch_sha256=patch_sha,
        changed_files=["app.py"],
        author=CommitAuthor(name="FORGE Agent", email="forge@local.agent"),
        committer=CommitAuthor(name="FORGE Agent", email="forge@local.agent"),
        commit_message="Resolve Index out of bounds error",
        state=PublicationExecutionState.VERIFIED,
        workspace_path=str(git_repos["local_dir"]),
        target_repo_path=str(git_repos["local_dir"]),
    )
    pub_store.save_staged_commit(staged_commit)

    return {
        "db_path": db_path,
        "forge_store": forge_store,
        "exec_store": exec_store,
        "pub_store": pub_store,
        "repair_id": repair_id,
        "pub_id": pub_id,
        "finding": finding,
        "patch": patch,
        "approval": approval,
        "staged_commit": staged_commit,
        "git_repos": git_repos,
    }


def make_mock_github_client(
    pr_number: int = 101,
    pr_url: str = "https://github.com/owner/repo/pull/101",
    existing_prs: Optional[List[Dict[str, Any]]] = None,
    fail_auth: bool = False,
    fail_rate_limit: bool = False,
    fail_validation: bool = False,
) -> httpx.Client:
    """Creates a mock httpx.Client simulating GitHub REST API endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        if fail_auth:
            return httpx.Response(401, json={"message": "Bad credentials"})

        if fail_rate_limit:
            return httpx.Response(429, json={"message": "API rate limit exceeded"})

        # GET /user
        if "/user" in url_str and request.method == "GET":
            return httpx.Response(200, json={"login": "forge-bot", "id": 12345})

        # GET /repos/{owner}/{repo}
        if re.search(r"/repos/[^/]+/[^/]+$", url_str) and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "name": "repo",
                    "owner": {"login": "owner"},
                    "default_branch": "main",
                    "clone_url": "https://github.com/owner/repo.git",
                    "private": False,
                    "permissions": {"push": True, "pull": True},
                },
            )

        # GET /repos/{owner}/{repo}/branches/{branch}
        if "/branches/" in url_str and request.method == "GET":
            return httpx.Response(404, json={"message": "Branch not found"})

        # GET /repos/{owner}/{repo}/pulls/{number}
        if re.search(r"/pulls/\d+$", url_str) and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": pr_number,
                    "html_url": pr_url,
                    "title": "Resolve Index out of bounds error",
                    "body": "PR description",
                    "state": "open",
                    "draft": False,
                    "created_at": "2026-11-03T10:00:00Z",
                    "head": {"ref": "forge/fix-test"},
                    "base": {"ref": "main"},
                },
            )

        # GET /repos/{owner}/{repo}/pulls
        if "/pulls" in url_str and request.method == "GET":
            if existing_prs is not None:
                return httpx.Response(200, json=existing_prs)
            return httpx.Response(200, json=[])

        # POST /repos/{owner}/{repo}/pulls
        if "/pulls" in url_str and request.method == "POST":
            if fail_validation:
                return httpx.Response(422, json={"message": "Validation Failed", "errors": ["A pull request already exists"]})
            body = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                201,
                json={
                    "number": pr_number,
                    "html_url": pr_url,
                    "title": body.get("title", ""),
                    "body": body.get("body", ""),
                    "state": "open",
                    "draft": body.get("draft", False),
                    "created_at": "2026-11-03T10:00:00Z",
                    "head": {"ref": body.get("head", "")},
                    "base": {"ref": body.get("base", "main")},
                },
            )

        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


# -----------------------------------------------------------------------------
# 1. Authorization Security Tests
# -----------------------------------------------------------------------------

def test_missing_approval_rejected(m3_env):
    """Missing approval decision raises PublicationAuthorizationError."""
    coordinator = RemotePublicationCoordinator(
        execution_store=m3_env["exec_store"],
        forge_store=m3_env["forge_store"],
        publication_store=m3_env["pub_store"],
    )
    with pytest.raises(PublicationAuthorizationError, match="No operator approval decision found"):
        coordinator.validate_remote_prerequisites("non_existent_repair")


@pytest.mark.parametrize("disallowed_action", [
    AuthorizedAction.EXPORT_PATCH,
    AuthorizedAction.MANUAL_INTEGRATION,
    AuthorizedAction.STAGE_FOR_PR,
    AuthorizedAction.STAGE_BRANCH,
])
def test_disallowed_actions_strictly_denied_for_remote_publication(m3_env, disallowed_action):
    """Historical or non-CREATE_PR actions are strictly rejected for remote publication."""
    # Modify approval to disallowed action
    approval = m3_env["approval"].model_copy(update={"authorized_action": disallowed_action})
    m3_env["exec_store"].save_approval_decision(approval)

    coordinator = RemotePublicationCoordinator(
        execution_store=m3_env["exec_store"],
        forge_store=m3_env["forge_store"],
        publication_store=m3_env["pub_store"],
    )

    with pytest.raises(PublicationActionMismatchError, match="Remote publication requires explicit 'create_pr'"):
        coordinator.validate_remote_prerequisites(m3_env["repair_id"])


def test_expired_approval_rejected_for_remote(m3_env):
    """Expired operator approval fails closed."""
    approval = m3_env["approval"].model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(minutes=5)})
    m3_env["exec_store"].save_approval_decision(approval)

    coordinator = RemotePublicationCoordinator(
        execution_store=m3_env["exec_store"],
        forge_store=m3_env["forge_store"],
        publication_store=m3_env["pub_store"],
    )

    with pytest.raises(PublicationAuthorizationError, match="has expired"):
        coordinator.validate_remote_prerequisites(m3_env["repair_id"])


def test_invalidated_approval_rejected_for_remote(m3_env):
    """Invalidated operator approval fails closed."""
    approval = m3_env["approval"].model_copy(update={"state": ApprovalState.INVALIDATED})
    m3_env["exec_store"].save_approval_decision(approval)

    coordinator = RemotePublicationCoordinator(
        execution_store=m3_env["exec_store"],
        forge_store=m3_env["forge_store"],
        publication_store=m3_env["pub_store"],
    )

    with pytest.raises(PublicationAuthorizationError, match="state 'invalidated'"):
        coordinator.validate_remote_prerequisites(m3_env["repair_id"])


def test_approval_invalidated_between_push_and_pr_creation(m3_env):
    """If approval is invalidated after push but before PR creation, PR creation is blocked."""
    # Simulate existing verified remote branch
    remote_branch = RemoteBranchRecord(
        remote_branch_id="rb_test",
        repair_id=m3_env["repair_id"],
        publication_id=m3_env["pub_id"],
        plan_id=m3_env["pub_id"],
        staged_commit_id=m3_env["staged_commit"].staging_id,
        decision_id=m3_env["approval"].decision_id,
        remote_name="origin",
        remote_url=m3_env["git_repos"]["remote_url"],
        branch_name=m3_env["staged_commit"].branch_name,
        commit_sha=m3_env["staged_commit"].commit_sha,
        parent_commit_sha=m3_env["staged_commit"].base_commit_sha,
        tree_sha=m3_env["staged_commit"].tree_sha,
        patch_sha256=m3_env["staged_commit"].patch_sha256,
        status="verified",
    )
    m3_env["pub_store"].save_remote_branch(remote_branch)

    # Invalidate approval before PR creation
    approval = m3_env["approval"].model_copy(update={"state": ApprovalState.INVALIDATED})
    m3_env["exec_store"].save_approval_decision(approval)

    coordinator = RemotePublicationCoordinator(
        execution_store=m3_env["exec_store"],
        forge_store=m3_env["forge_store"],
        publication_store=m3_env["pub_store"],
    )

    with pytest.raises(PublicationAuthorizationError, match="state 'invalidated'"):
        coordinator.create_pull_request(m3_env["repair_id"])


# -----------------------------------------------------------------------------
# 2. Credential Security Tests
# -----------------------------------------------------------------------------

def test_missing_token_raises_authentication_error(monkeypatch):
    """Missing token raises RemoteAuthenticationError without leaking environment info."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_GITHUB_TOKEN", raising=False)

    provider = GitHubPullRequestProvider()
    with pytest.raises(RemoteAuthenticationError, match="Missing GitHub access credentials"):
        provider.validate_credentials()


def test_token_never_leaked_in_repr_or_str():
    """Secrets never appear in repr(provider) or str(provider)."""
    secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    provider = GitHubPullRequestProvider(token=secret)

    repr_str = repr(provider)
    assert secret not in repr_str
    assert "token='present'" in repr_str


def test_token_redacted_in_exceptions():
    """If provider returns an error mentioning token, it is redacted."""
    secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    client = make_mock_github_client(fail_auth=True)
    provider = GitHubPullRequestProvider(token=secret, client=client)

    with pytest.raises(RemoteAuthenticationError) as exc_info:
        provider.validate_credentials()

    assert secret not in str(exc_info.value)


# -----------------------------------------------------------------------------
# 3. URL Parsing & Repository Identity Tests
# -----------------------------------------------------------------------------

def test_parse_repository_owner_and_name():
    """Verify owner and repo extraction across URL formats."""
    assert parse_repository_owner_and_name("https://github.com/myorg/myrepo.git") == ("myorg", "myrepo")
    assert parse_repository_owner_and_name("git@github.com:myorg/myrepo.git") == ("myorg", "myrepo")
    assert parse_repository_owner_and_name("ssh://git@github.com/myorg/myrepo") == ("myorg", "myrepo")
    assert parse_repository_owner_and_name("file:///tmp/repo.git") == ("tmp", "repo")


def test_forbidden_remote_url_scheme_rejected(tmp_path: Path):
    """Remote URL with disallowed scheme (e.g. ftp://) is rejected."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "ftp://evil.com/repo.git"], cwd=str(repo_dir), check=True)

    publisher = RemoteBranchPublisher(policy=GitPublicationPolicy())
    with pytest.raises(RemoteRepositoryVerificationError, match="Remote URL scheme 'ftp' is forbidden"):
        publisher.verify_remote_repository_identity(repo_dir)


def test_owner_and_repo_mismatch_rejected(tmp_path: Path):
    """Mismatch in expected repository owner or name raises RemoteRepositoryVerificationError."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/actual_owner/actual_repo.git"], cwd=str(repo_dir), check=True)

    publisher = RemoteBranchPublisher(policy=GitPublicationPolicy())
    with pytest.raises(RemoteRepositoryVerificationError, match="owner mismatch"):
        publisher.verify_remote_repository_identity(repo_dir, expected_owner="expected_owner")

    with pytest.raises(RemoteRepositoryVerificationError, match="name mismatch"):
        publisher.verify_remote_repository_identity(repo_dir, expected_repo="expected_repo")


# -----------------------------------------------------------------------------
# 4. Safe Remote Branch Publication Tests
# -----------------------------------------------------------------------------

def test_push_remote_branch_success(m3_env):
    """Normal push to remote publishes branch and verifies parent and tree."""
    publisher = RemoteBranchPublisher(policy=GitPublicationPolicy())

    record = publisher.publish_remote_branch(
        repository_path=m3_env["git_repos"]["local_dir"],
        remote_name="origin",
        remote_url=m3_env["git_repos"]["remote_url"],
        branch_name=m3_env["staged_commit"].branch_name,
        staged_commit=m3_env["staged_commit"],
        repair_id=m3_env["repair_id"],
        publication_id="pub_test",
        plan_id="plan_test",
        decision_id=m3_env["approval"].decision_id,
    )

    assert record.status == "verified"
    assert record.commit_sha == m3_env["staged_commit"].commit_sha
    assert record.parent_commit_sha == m3_env["staged_commit"].base_commit_sha

    # Verify branch exists on remote
    remote_sha = publisher.check_remote_branch_collision(
        m3_env["git_repos"]["local_dir"], "origin", m3_env["staged_commit"].branch_name
    )
    assert remote_sha == m3_env["staged_commit"].commit_sha


def test_push_to_protected_branch_rejected(m3_env):
    """Pushing directly to a protected branch (e.g. main) is strictly forbidden."""
    publisher = RemoteBranchPublisher(policy=GitPublicationPolicy())

    with pytest.raises(ProtectedBranchViolationError, match="protected branch 'main'"):
        publisher.publish_remote_branch(
            repository_path=m3_env["git_repos"]["local_dir"],
            remote_name="origin",
            remote_url=m3_env["git_repos"]["remote_url"],
            branch_name="main",
            staged_commit=m3_env["staged_commit"],
            repair_id=m3_env["repair_id"],
            publication_id="pub_test",
            plan_id="plan_test",
            decision_id=m3_env["approval"].decision_id,
        )


def test_push_conflicting_remote_branch_rejected(m3_env):
    """If remote branch already exists with a different commit SHA, overwrite is rejected."""
    branch = m3_env["staged_commit"].branch_name

    # Push a different commit on that branch to remote first
    subprocess.run(
        ["git", "push", "origin", f"{m3_env['git_repos']['base_sha']}:refs/heads/{branch}"],
        cwd=str(m3_env["git_repos"]["local_dir"]),
        check=True,
    )

    publisher = RemoteBranchPublisher(policy=GitPublicationPolicy())

    with pytest.raises(RemoteBranchCollisionError, match="differing commit SHA"):
        publisher.publish_remote_branch(
            repository_path=m3_env["git_repos"]["local_dir"],
            remote_name="origin",
            remote_url=m3_env["git_repos"]["remote_url"],
            branch_name=branch,
            staged_commit=m3_env["staged_commit"],
            repair_id=m3_env["repair_id"],
            publication_id="pub_test",
            plan_id="plan_test",
            decision_id=m3_env["approval"].decision_id,
        )


def test_push_identical_remote_branch_reconciles_safely(m3_env):
    """If remote branch already exists with identical commit SHA, it reconciles safely."""
    publisher = RemoteBranchPublisher(policy=GitPublicationPolicy())

    # Publish once
    rec1 = publisher.publish_remote_branch(
        repository_path=m3_env["git_repos"]["local_dir"],
        remote_name="origin",
        remote_url=m3_env["git_repos"]["remote_url"],
        branch_name=m3_env["staged_commit"].branch_name,
        staged_commit=m3_env["staged_commit"],
        repair_id=m3_env["repair_id"],
        publication_id="pub_test",
        plan_id="plan_test",
        decision_id=m3_env["approval"].decision_id,
    )
    assert rec1.status == "verified"

    # Publish again (idempotent reconciliation)
    rec2 = publisher.publish_remote_branch(
        repository_path=m3_env["git_repos"]["local_dir"],
        remote_name="origin",
        remote_url=m3_env["git_repos"]["remote_url"],
        branch_name=m3_env["staged_commit"].branch_name,
        staged_commit=m3_env["staged_commit"],
        repair_id=m3_env["repair_id"],
        publication_id="pub_test",
        plan_id="plan_test",
        decision_id=m3_env["approval"].decision_id,
    )
    assert rec2.status == "verified"
    assert rec2.verification_details.get("reconciled") is True


# -----------------------------------------------------------------------------
# 5. Pull Request Content & Provider Tests
# -----------------------------------------------------------------------------

def test_pr_formatter_distinguishes_facts_from_ai(m3_env):
    """PR description clearly distinguishes verified empirical facts from AI hypotheses."""
    details = PullRequestFormatter.format_pull_request(
        finding=m3_env["finding"],
        patch=m3_env["patch"],
        staged_commit=m3_env["staged_commit"],
        approval=m3_env["approval"],
        target_branch="main",
    )

    assert details.title.startswith("Resolve Index out of bounds error")
    assert "## Verified Empirical Facts" in details.body
    assert m3_env["staged_commit"].commit_sha in details.body
    assert m3_env["staged_commit"].patch_sha256 in details.body
    assert "## Root Cause & Repair Approach *(Automated Analysis)*" in details.body
    assert "## Operator Authorization" in details.body
    assert "lead_operator" in details.body


def test_github_provider_create_pull_request_success():
    """Mocked GitHub provider successfully creates and verifies pull request."""
    client = make_mock_github_client(pr_number=42, pr_url="https://github.com/owner/repo/pull/42")
    provider = GitHubPullRequestProvider(token="test_token", client=client)

    details = PullRequestDetails(
        title="Resolve defect",
        body="PR body",
        target_branch="main",
        source_branch="forge/fix-123",
    )

    record = provider.create_pull_request(
        owner="owner",
        repo="repo",
        details=details,
        repair_id="rep_1",
        publication_id="pub_1",
        plan_id="plan_1",
        decision_id="dec_1",
        staged_commit_id="stg_1",
        remote_branch_id="rb_1",
    )

    assert record.pr_number == 42
    assert record.pr_url == "https://github.com/owner/repo/pull/42"
    assert record.status == "created"


def test_github_provider_detects_duplicate_pr():
    """If an open PR already exists for head/base branch, DuplicatePullRequestError is raised."""
    existing = [{
        "number": 99,
        "html_url": "https://github.com/owner/repo/pull/99",
        "title": "Existing PR",
        "body": "Existing body",
        "state": "open",
        "draft": False,
        "created_at": "2026-11-03T10:00:00Z",
    }]
    client = make_mock_github_client(existing_prs=existing)
    provider = GitHubPullRequestProvider(token="test_token", client=client)

    details = PullRequestDetails(
        title="Resolve defect",
        body="PR body",
        target_branch="main",
        source_branch="forge/fix-123",
    )

    with pytest.raises(DuplicatePullRequestError, match="PR #99"):
        provider.create_pull_request(
            owner="owner",
            repo="repo",
            details=details,
            repair_id="rep_1",
            publication_id="pub_1",
            plan_id="plan_1",
            decision_id="dec_1",
            staged_commit_id="stg_1",
            remote_branch_id="rb_1",
        )


def test_retry_idempotent_operation_recovers_from_rate_limit():
    """Retry logic handles transient rate limit then succeeds."""
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ProviderRateLimitError("Rate limit exceeded")
        return "success"

    result = retry_idempotent_operation(flaky, max_retries=3, initial_delay=0.01)
    assert result == "success"
    assert calls == 2


# -----------------------------------------------------------------------------
# 6. End-to-End Remote Publication Workflow
# -----------------------------------------------------------------------------

def test_coordinator_end_to_end_remote_publication(m3_env):
    """Verified repair -> CREATE_PR approval -> staged commit -> remote push -> PR creation -> SQLite audit."""
    client = make_mock_github_client(pr_number=77, pr_url="https://github.com/owner/repo/pull/77")
    provider = GitHubPullRequestProvider(token="test_token", client=client)

    coordinator = RemotePublicationCoordinator(
        execution_store=m3_env["exec_store"],
        forge_store=m3_env["forge_store"],
        publication_store=m3_env["pub_store"],
        provider=provider,
    )

    # 1. Validate preconditions (read-only)
    val_res = coordinator.validate_remote_prerequisites(m3_env["repair_id"])
    assert val_res["is_valid"] is True
    assert val_res["authorized_action"] == "create_pr"

    # 2. Push remote branch
    remote_branch = coordinator.publish_remote_branch(m3_env["repair_id"])
    assert remote_branch.status == "verified"
    assert remote_branch.commit_sha == m3_env["staged_commit"].commit_sha

    # Verify persisted in SQLite
    saved_branch = m3_env["pub_store"].get_remote_branch(remote_branch.remote_branch_id)
    assert saved_branch is not None
    assert saved_branch.branch_name == m3_env["staged_commit"].branch_name

    # 3. Create Pull Request
    pr = coordinator.create_pull_request(m3_env["repair_id"])
    assert pr.pr_number == 77
    assert pr.status == "verified"

    # Verify persisted in SQLite
    saved_pr = m3_env["pub_store"].get_pull_request_for_repair(m3_env["repair_id"])
    assert saved_pr is not None
    assert saved_pr.pr_number == 77

    # Verify RemotePublicationRecord
    remote_pub = m3_env["pub_store"].get_remote_publication_for_repair(m3_env["repair_id"])
    assert remote_pub is not None
    assert remote_pub.status == "completed"

    # Verify Audit Events in repair_execution_events
    events = m3_env["exec_store"].get_events_for_repair(m3_env["repair_id"])
    actions = [e.payload.get("action") for e in events]
    assert "remote_identity_verified" in actions
    assert "publication_authorization_revalidated" in actions
    assert "remote_branch_push_started" in actions
    assert "remote_branch_published" in actions
    assert "remote_branch_verified" in actions
    assert "pull_request_creation_started" in actions
    assert "pull_request_created" in actions
    assert "pull_request_verified" in actions


# -----------------------------------------------------------------------------
# 7. CLI Commands Tests
# -----------------------------------------------------------------------------

def test_cli_remote_validate_command(m3_env, monkeypatch):
    """Test 'forge repair publish remote-validate' CLI command."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m3_env["db_path"]))
    monkeypatch.setenv("GITHUB_TOKEN", "mock_gh_token_for_cli")
    monkeypatch.setattr(
        "forge.publication.providers.github.GitHubPullRequestProvider._get_client",
        lambda self: make_mock_github_client(),
    )

    result = runner.invoke(app, ["repair", "publish", "remote-validate", m3_env["repair_id"]])
    assert result.exit_code == 0
    assert "[VALID] Remote publication authorized" in result.output
    assert "create_pr" in result.output


def test_cli_remote_status_and_inspect_commands(m3_env, monkeypatch):
    """Test 'forge repair publish remote-status' and 'remote-inspect' CLI commands."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m3_env["db_path"]))
    monkeypatch.setenv("GITHUB_TOKEN", "mock_gh_token_for_cli")

    # 1. Status before push
    res_status = runner.invoke(app, ["repair", "publish", "remote-status", m3_env["repair_id"]])
    assert res_status.exit_code == 0
    assert "Not pushed" in res_status.output
    assert "Not created" in res_status.output

    # 2. Push via CLI
    res_push = runner.invoke(app, ["repair", "publish", "push", m3_env["repair_id"]])
    assert res_push.exit_code == 0
    assert "[PUBLISHED]" in res_push.output

    # 3. Status after push
    res_status2 = runner.invoke(app, ["repair", "publish", "remote-status", m3_env["repair_id"]])
    assert res_status2.exit_code == 0
    assert "Branch Status:     verified" in res_status2.output

    # 4. Inspect
    res_inspect = runner.invoke(app, ["repair", "publish", "remote-inspect", m3_env["repair_id"]])
    assert res_inspect.exit_code == 0
    assert "Remote Branch ID:" in res_inspect.output


def test_cli_top_level_alias(m3_env, monkeypatch):
    """Top-level 'forge publish remote-validate' alias works identically."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m3_env["db_path"]))
    monkeypatch.setenv("GITHUB_TOKEN", "mock_gh_token_for_cli")
    monkeypatch.setattr(
        "forge.publication.providers.github.GitHubPullRequestProvider._get_client",
        lambda self: make_mock_github_client(),
    )

    result = runner.invoke(app, ["publish", "remote-validate", m3_env["repair_id"]])
    assert result.exit_code == 0
    assert "[VALID]" in result.output
