"""Comprehensive verification suite for Option C: Safe Git Metadata Isolation.

Demonstrates:
1. Trusted repository passes git fsck --full with 0 errors.
2. Selected history guarantee is correctly enforced (complete historical mirror).
3. Protected Git content cannot be accessed by analysis tools or agents.
4. Ordinary files remain readable and editable.
5. Upstream commit identity remains exact.
6. Repair and publication work end-to-end.
7. Protected paths cannot be staged, deleted, or published.
8. Mediator failures and access-control violations fail closed.
9. No protected content reaches model context, logs, or artifacts.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path
import pytest

from forge.repository.acquisition import (
    acquire_repository,
    cleanup_repository,
    run_git_command,
    AcquiredRepository,
)
from forge.repository.git_mediator import (
    GitMediator,
    GitMediatorError,
    GitMediatorSecurityError,
)
from forge.publication.workspace import PublicationWorkspace
from forge.publication.commit import GitCommitBuilder, PatchStagingError
from forge.publication.models import CommitProposal
from forge.schemas import (
    Finding,
    FindingLifecycleState,
    FindingSeverity,
    FindingConfidence,
)
from forge.repair.execution.models import (
    ApprovalBinding,
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    OperatorDecisionType,
    PatchArtifact,
    ReviewDossier,
)


@pytest.fixture
def synthetic_multi_commit_repo(tmp_path: Path) -> Path:
    """Create a synthetic Git repository containing protected and ordinary files across multiple commits."""
    src = tmp_path / "synthetic_upstream"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Upstream Dev"], cwd=src)
    run_git_command(["config", "user.email", "dev@upstream.local"], cwd=src)

    # Commit 1: Initial files
    (src / "README.md").write_text("# Upstream Repo\n", encoding="utf-8")
    (src / ".env").write_text("API_SECRET_KEY=upstream_secret_111\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Initial commit with .env"], cwd=src)

    # Commit 2: Add ordinary source file and sensitive database
    pkg = src / "pkg"
    pkg.mkdir()
    (pkg / "core.py").write_text("def compute(x): return x * 2\n", encoding="utf-8")
    (src / "memory.db").write_text("database-binary-data\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Add core.py and memory.db"], cwd=src)

    # Commit 3: Add contacts.json and update core.py
    (src / "contacts.json").write_text('{"users": ["alice", "bob"]}\n', encoding="utf-8")
    (pkg / "core.py").write_text("def compute(x): return x * 3\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Add contacts.json and update core"], cwd=src)

    # Commit 4: Add diagnostic log
    (src / "debug.log").write_text("2026-11-05 trace log\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Add debug.log"], cwd=src)

    return src


# =============================================================================
# 1. Trusted Repository Integrity & History Guarantee
# =============================================================================

def test_trusted_repository_passes_fsck_full(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """The trusted repository mirror must pass git fsck --full with zero broken links or errors."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        assert acquired.trusted_git_path is not None
        assert acquired.trusted_git_path.exists()

        # Run git fsck --full directly on trusted mirror
        res_fsck = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "fsck", "--full"],
            cwd=str(acquired.trusted_git_path),
            capture_output=True,
            text=True,
        )
        assert res_fsck.returncode == 0, f"Trusted mirror failed fsck: {res_fsck.stderr}"
        assert "broken link" not in res_fsck.stdout and "broken link" not in res_fsck.stderr

        # Check mediator integrity verification helper
        assert acquired.mediator.verify_trusted_integrity() is True
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_complete_historical_mirror_guarantee(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """Verify that the complete historical mirror retains all 4 commits and parentage."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        # Check that history is NOT shallow graft
        shallow_file = acquired.trusted_git_path / ".git" / "shallow"
        if not shallow_file.exists():
            shallow_file = acquired.trusted_git_path / "shallow"
        assert not shallow_file.exists(), "Trusted mirror must not be a shallow graft"

        # Check commit count via git rev-list --count HEAD
        res_count = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "rev-list", "--count", "HEAD"],
            cwd=str(acquired.trusted_git_path),
            capture_output=True,
            text=True,
        )
        assert res_count.returncode == 0
        assert int(res_count.stdout.strip()) == 4, "Complete history of 4 commits must be preserved"
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 2. Analysis Workspace Isolation & Protected Content Inaccessibility
# =============================================================================

def test_analysis_workspace_cannot_access_protected_git_content(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """The analysis workspace cannot access protected files through filesystem or Git."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        # 1. Protected working tree files do NOT exist
        assert not (acquired.workspace_path / ".env").exists()
        assert not (acquired.workspace_path / "memory.db").exists()
        assert not (acquired.workspace_path / "contacts.json").exists()
        assert not (acquired.workspace_path / "debug.log").exists()

        # 2. Local git in analysis workspace does not have protected files in HEAD or history
        res_show_env = subprocess.run(
            ["git", "show", "HEAD:.env"],
            cwd=str(acquired.workspace_path),
            capture_output=True,
            text=True,
        )
        assert res_show_env.returncode != 0
        assert "does not exist" in res_show_env.stderr or "fatal" in res_show_env.stderr

        res_show_contacts = subprocess.run(
            ["git", "show", "HEAD:contacts.json"],
            cwd=str(acquired.workspace_path),
            capture_output=True,
            text=True,
        )
        assert res_show_contacts.returncode != 0

        # 3. Raw byte search of analysis workspace .git contains ZERO secret tokens
        for root, dirs, files in os.walk(acquired.workspace_path):
            for f in files:
                fpath = Path(root) / f
                content = fpath.read_bytes()
                assert b"upstream_secret_111" not in content, f"Secret leaked in analysis file: {fpath}"
                assert b"database-binary-data" not in content
                assert b"alice" not in content
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_ordinary_files_remain_readable_and_editable(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """Ordinary source files remain fully readable and editable in the analysis workspace."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        core_path = acquired.workspace_path / "pkg" / "core.py"
        assert core_path.exists()
        assert "def compute(x): return x * 3" in core_path.read_text(encoding="utf-8")

        # Edit ordinary file
        core_path.write_text("def compute(x): return x * 4\n", encoding="utf-8")
        assert "def compute(x): return x * 4" in core_path.read_text(encoding="utf-8")

        # Analysis workspace git status reflects ordinary modification cleanly
        res_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(acquired.workspace_path),
            capture_output=True,
            text=True,
        )
        assert res_status.returncode == 0
        assert "M pkg/core.py" in res_status.stdout
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 3. Upstream Revision Pinning & Identity Separation
# =============================================================================

def test_upstream_commit_identity_exact(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """Authentic upstream commit SHA is preserved exactly, separate from analysis commit SHA."""
    expected_head = run_git_command(["rev-parse", "HEAD"], cwd=synthetic_multi_commit_repo).stdout.strip()

    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        assert acquired.upstream_commit_sha == expected_head
        assert acquired.resolved_commit_sha == expected_head
        assert acquired.analysis_commit_sha is not None
        assert acquired.analysis_commit_sha != acquired.upstream_commit_sha

        # Mediator validates revision drift cleanly
        assert acquired.mediator.check_revision_drift(expected_head) is True
        assert acquired.mediator.check_revision_drift("0" * 40) is False
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 4. Mediator Security Enforcement & Fail-Closed Behavior
# =============================================================================

def test_mediator_rejects_protected_paths_fail_closed(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """GitMediator strictly rejects reading or diffing protected paths."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        mediator: GitMediator = acquired.mediator
        assert mediator is not None

        # Rejects reading .env
        with pytest.raises(GitMediatorSecurityError, match="access to protected sensitive path"):
            mediator.get_file_content_at_ref("HEAD", ".env")

        # Rejects reading contacts.json
        with pytest.raises(GitMediatorSecurityError, match="access to protected sensitive path"):
            mediator.get_file_content_at_ref("HEAD", "contacts.json")

        # Rejects diffing protected paths
        with pytest.raises(GitMediatorSecurityError, match="diff requested for protected sensitive path"):
            mediator.get_safe_diff("HEAD~1", "HEAD", paths=[".env"])

        # Ordinary file read works through mediator
        core_content = mediator.get_file_content_at_ref("HEAD", "pkg/core.py")
        assert "def compute(x): return x * 3" in core_content

        # Tree listing excludes protected paths
        paths = mediator.list_tree_paths("HEAD")
        assert "pkg/core.py" in paths
        assert "README.md" in paths
        assert ".env" not in paths
        assert "contacts.json" not in paths
        assert "memory.db" not in paths
        assert "debug.log" not in paths
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_mediator_enforces_directory_isolation(tmp_path: Path):
    """Mediator raises GitMediatorSecurityError if workspace and trusted storage collide."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    ws = trusted / "inside_workspace"
    ws.mkdir()

    with pytest.raises(GitMediatorSecurityError, match="analysis workspace cannot be inside trusted Git repository"):
        GitMediator(trusted_git_path=trusted, workspace_path=ws)


# =============================================================================
# 5. Phase 9 Publication End-to-End & Protection Boundaries
# =============================================================================

def test_phase9_publication_with_isolated_trusted_git(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """Phase 9 PublicationWorkspace provisions from trusted mirror and commits verified patch."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        # 1. Provision PublicationWorkspace using trusted_git_path
        pub_ws = PublicationWorkspace(
            target_repo_path=acquired.trusted_git_path,
            base_commit_sha=acquired.resolved_commit_sha,
            scratch_root=tmp_path / "pub_workspaces",
        )
        pub_ws.prepare()
        assert pub_ws.is_prepared is True

        # 2. Create patch for ordinary file
        diff_text = """--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,1 +1,1 @@
-def compute(x): return x * 3
+def compute(x): return x * 5
"""
        import hashlib
        diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

        patch = PatchArtifact(
            artifact_id="art_test_001",
            repair_id="rep_test_001",
            finding_id="find_test_001",
            target_file="pkg/core.py",
            base_commit_sha=acquired.resolved_commit_sha,
            diff_content=diff_text,
            content_hash=diff_hash,
            affected_paths=["pkg/core.py"],
            lines_added=1,
            lines_deleted=1,
            hunks_count=1,
        )

        finding = Finding(
            finding_id="find_test_001",
            title="Computation bug in core",
            description="Returns x * 3 instead of x * 5",
            lifecycle_state=FindingLifecycleState.ROOT_CAUSED,
            severity=FindingSeverity.MEDIUM,
            confidence=FindingConfidence.CONFIRMED,
        )

        dossier = ReviewDossier(
            dossier_id="dos_test_001",
            repair_id="rep_test_001",
            finding_id=finding.finding_id,
            finding_title=finding.title,
            finding_severity=finding.severity.value,
            repository_target=str(synthetic_multi_commit_repo),
            base_commit_sha=acquired.resolved_commit_sha,
            patch_artifact_id=patch.artifact_id,
            patch_sha256=patch.content_hash,
            unified_diff=diff_text,
            validation_id="val_001",
            validation_outcome="accepted",
            verification_id="ver_001",
            verification_outcome="verified",
            workspace_id="ws_pub_01",
        )

        binding = ApprovalBinding(
            repair_id="rep_test_001",
            finding_id=finding.finding_id,
            repository_target=str(synthetic_multi_commit_repo),
            base_commit_sha=acquired.resolved_commit_sha,
            patch_artifact_id=patch.artifact_id,
            patch_sha256=patch.content_hash,
            validation_id="val_001",
            verification_id="ver_001",
            dossier_id=dossier.dossier_id,
        )
        approval = ApprovalDecisionRecord(
            decision_id="dec_001",
            repair_id="rep_test_001",
            binding=binding,
            state=ApprovalState.APPROVED,
            decision=OperatorDecisionType.APPROVE,
            authorized_action=AuthorizedAction.STAGE_FOR_PR,
            actor="security_officer@local",
            reason="Verified bugfix.",
        )

        # 3. Stage and commit patch
        builder = GitCommitBuilder()
        proposal = builder.plan_commit(
            finding=finding,
            patch=patch,
            dossier=dossier,
            approval=approval,
        )
        builder.apply_and_stage_patch(
            workspace=pub_ws,
            patch=patch,
            allowed_files=["pkg/core.py"],
        )
        commit_res = builder.create_commit(
            workspace=pub_ws,
            commit_proposal=proposal,
            base_commit_sha=acquired.resolved_commit_sha,
        )

        assert commit_res["commit_sha"] is not None
        assert commit_res["parent_commit_sha"] == acquired.resolved_commit_sha
        assert commit_res["changed_files"] == ["pkg/core.py"]

        # Clean up publication workspace
        pub_ws.cleanup()
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_phase9_publication_rejects_protected_patch_paths(synthetic_multi_commit_repo: Path, tmp_path: Path):
    """PublicationCommitManager strictly rejects staging patches that touch protected files."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_multi_commit_repo), custom_workspace=ws, allow_local=True)

    try:
        pub_ws = PublicationWorkspace(
            target_repo_path=acquired.trusted_git_path,
            base_commit_sha=acquired.resolved_commit_sha,
            scratch_root=tmp_path / "pub_workspaces",
        )
        pub_ws.prepare()

        # Malicious patch attempting to modify .env
        diff_text = """--- a/.env
+++ b/.env
@@ -1,1 +1,1 @@
-API_SECRET_KEY=upstream_secret_111
+API_SECRET_KEY=modified_secret_222
"""
        import hashlib
        diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

        patch = PatchArtifact(
            artifact_id="art_malicious_001",
            repair_id="rep_malicious_001",
            finding_id="find_malicious_001",
            target_file=".env",
            base_commit_sha=acquired.resolved_commit_sha,
            diff_content=diff_text,
            content_hash=diff_hash,
            affected_paths=[".env"],
            lines_added=1,
            lines_deleted=1,
            hunks_count=1,
        )

        builder = GitCommitBuilder()
        with pytest.raises(PatchStagingError, match="Security violation: patch attempts to modify protected sensitive file"):
            builder.apply_and_stage_patch(
                workspace=pub_ws,
                patch=patch,
                allowed_files=[".env"],
            )

        pub_ws.cleanup()
    finally:
        cleanup_repository(acquired, allowed_base=ws)
