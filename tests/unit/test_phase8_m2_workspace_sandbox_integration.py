"""Unit tests for Phase 8, Milestone 2: Isolated Workspace Management & Sandbox Integration.

Verifies:
1. Workspace provisioning, unique ID, and physical isolation outside target repo.
2. Exact Git commit identity verification and CommitMismatchError detection.
3. Target repository immutability guarantees.
4. Scope containment and symlink escape defenses.
5. Workspace reuse prevention.
6. Baseline file manifest hashing and filesystem integrity checker.
7. DockerSandbox integration (read-only vs writable, non-root, network none, limits).
8. Fail-closed behavior when Docker daemon is unavailable.
9. Idempotent workspace teardown and cleanup.
10. SQLite persistence of WorkspaceRecord and audit event ledger.
11. RepairWorkspaceManager end-to-end lifecycle orchestration.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from forge.repair.execution.failures import (
    CommitMismatchError,
    RepositoryAcquisitionError,
    ResourceLimitViolationError,
    SandboxUnavailableError,
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
    WorkspaceCleanupError,
    WorkspaceCollisionError,
    WorkspaceExecutionError,
    WorkspaceIntegrityViolationError,
    WorkspaceProvisioningError,
    WorkspaceReuseForbiddenError,
)
from forge.repair.execution.manager import RepairWorkspaceManager
from forge.repair.execution.models import (
    FailureCategory,
    FailureClassification,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionState,
    RepairRequest,
    WorkspaceRecord,
)
from forge.schemas import FindingSeverity
from forge.repair.execution.policy import (
    ArbitraryExecutionForbiddenError,
    RepairExecutionPolicy,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.workspace import RepairWorkspace
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    NetworkPolicy,
    SandboxRequest,
    SandboxResult,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore


# -----------------------------------------------------------------------------
# Fixtures & Helpers
# -----------------------------------------------------------------------------

@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_forge_m2.db"
    init_database(db_file)
    return db_file


@pytest.fixture
def seeded_db(test_db_path: Path) -> Path:
    forge_store = ForgeStore(db_path=test_db_path)
    forge_store.create_run(
        target="https://github.com/example/repo.git",
        run_id="run_m2_001",
    )
    forge_store.create_finding(
        run_id="run_m2_001",
        title="Null pointer dereference",
        description="Possible null dereference in parser",
        severity=FindingSeverity.HIGH,
        finding_id="find_m2_001",
    )
    return test_db_path


@pytest.fixture
def mock_target_git_repo(tmp_path: Path) -> Path:
    """Create a minimal real git repository fixture with a known commit SHA."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("def run():\n    return 42\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("# Target Repo\n", encoding="utf-8")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    return repo_dir


def _get_repo_head(repo_dir: Path) -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    return res.stdout.strip()


# -----------------------------------------------------------------------------
# 1. Provisioning & Unique ID
# -----------------------------------------------------------------------------

def test_workspace_provisioning_and_isolation(mock_target_git_repo: Path, tmp_path: Path):
    """Verify workspace provisions cleanly outside the target repo with baseline hashes."""
    head_sha = _get_repo_head(mock_target_git_repo)
    scratch_root = tmp_path / "scratch"

    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=scratch_root,
    )

    assert ws.workspace_id.startswith("ws_")
    assert not ws.is_prepared
    assert not ws.is_isolated

    ws.prepare()

    assert ws.is_prepared
    assert ws.is_isolated
    assert ws.workspace_path.exists()
    assert (ws.workspace_path / "src" / "app.py").exists()
    # .git should not be copied
    assert not (ws.workspace_path / ".git").exists()

    # Baseline hashes captured
    assert "src/app.py" in ws.baseline_hashes
    assert "README.md" in ws.baseline_hashes
    assert len(ws.baseline_hashes) == 2

    # Verify physical separation
    assert ws.workspace_path != mock_target_git_repo
    assert not ws.workspace_path.is_relative_to(mock_target_git_repo)
    assert not mock_target_git_repo.is_relative_to(ws.workspace_path)

    ws.cleanup()
    assert not ws.workspace_path.exists()
    assert ws.is_cleaned


# -----------------------------------------------------------------------------
# 2. Pinned Git Commit Verification
# -----------------------------------------------------------------------------

def test_workspace_verifies_matching_git_commit(mock_target_git_repo: Path, tmp_path: Path):
    """Workspace preparation succeeds when base_commit_sha matches target repo HEAD."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()
    assert ws.is_prepared
    ws.cleanup()


def test_workspace_accepts_short_commit_sha_prefix(mock_target_git_repo: Path, tmp_path: Path):
    """Workspace accepts prefix-matching short commit SHA."""
    head_sha = _get_repo_head(mock_target_git_repo)
    short_sha = head_sha[:10]

    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=short_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()
    assert ws.is_prepared
    ws.cleanup()


def test_workspace_rejects_commit_mismatch(mock_target_git_repo: Path, tmp_path: Path):
    """Workspace raises CommitMismatchError when commit SHA does not match repo HEAD."""
    wrong_sha = "0000000000000000000000000000000000000000"

    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=wrong_sha,
        scratch_root=tmp_path / "scratch",
    )
    with pytest.raises(CommitMismatchError, match="does not match expected base_commit_sha"):
        ws.prepare()


def test_workspace_rejects_nonexistent_target_repo(tmp_path: Path):
    """Attempting to create a workspace for a non-existent repo raises RepositoryAcquisitionError."""
    with pytest.raises(RepositoryAcquisitionError, match="does not exist"):
        RepairWorkspace(
            target_repo_path=tmp_path / "nonexistent_repo",
            base_commit_sha="abcdef123456",
        )


def test_workspace_rejects_empty_commit_sha(mock_target_git_repo: Path):
    """Attempting to create a workspace with an empty commit SHA raises RepositoryAcquisitionError."""
    with pytest.raises(RepositoryAcquisitionError, match="base_commit_sha cannot be empty"):
        RepairWorkspace(
            target_repo_path=mock_target_git_repo,
            base_commit_sha="   ",
        )


# -----------------------------------------------------------------------------
# 3. Collision Prevention & Target Repo Immutability
# -----------------------------------------------------------------------------

def test_workspace_rejects_collision_with_target_repo(mock_target_git_repo: Path, tmp_path: Path):
    """Attempting to place workspace at target repo path raises WorkspaceCollisionError."""
    head_sha = _get_repo_head(mock_target_git_repo)
    with pytest.raises(WorkspaceCollisionError):
        RepairWorkspace(
            target_repo_path=mock_target_git_repo,
            base_commit_sha=head_sha,
            workspace_id=mock_target_git_repo.name,
            scratch_root=mock_target_git_repo.parent,
        )


def test_workspace_rejects_placement_inside_target_repo(mock_target_git_repo: Path):
    """Attempting to place workspace inside target repo directory raises WorkspaceCollisionError."""
    head_sha = _get_repo_head(mock_target_git_repo)
    with pytest.raises(WorkspaceCollisionError):
        RepairWorkspace(
            target_repo_path=mock_target_git_repo,
            base_commit_sha=head_sha,
            workspace_id="nested_ws",
            scratch_root=mock_target_git_repo / "subfolder",
        )


def test_workspace_detects_target_repo_mutation(mock_target_git_repo: Path, tmp_path: Path):
    """TargetRepoImmutabilityViolationError is raised if the target repo is modified."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Mutate the target repository out of band
    (mock_target_git_repo / "src" / "tampered.py").write_text("malicious = True\n")

    assert not ws.check_target_immutability()
    with pytest.raises(TargetRepoImmutabilityViolationError):
        ws.verify_isolation()

    with pytest.raises(TargetRepoImmutabilityViolationError):
        ws.check_integrity()

    ws.cleanup()


# -----------------------------------------------------------------------------
# 4. Scope Containment & Symlink Escape Defense
# -----------------------------------------------------------------------------

def test_validate_containment_valid_paths(mock_target_git_repo: Path, tmp_path: Path):
    """Containment validation allows legitimate workspace-relative paths."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    valid_path = ws.validate_containment("src/app.py")
    assert valid_path == ws.workspace_path / "src" / "app.py"

    valid_dir = ws.validate_containment("src")
    assert valid_dir == ws.workspace_path / "src"
    ws.cleanup()


def test_validate_containment_rejects_directory_traversal(mock_target_git_repo: Path, tmp_path: Path):
    """Containment validation strictly forbids path traversal ('..')."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    with pytest.raises(ScopeContainmentViolationError, match="Path traversal"):
        ws.validate_containment("../etc/passwd")

    with pytest.raises(ScopeContainmentViolationError, match="Path traversal"):
        ws.validate_containment("src/../../secret.txt")

    ws.cleanup()


def test_validate_containment_rejects_symlink_escape(mock_target_git_repo: Path, tmp_path: Path):
    """Containment validation detects and blocks symlinks pointing outside workspace."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Create external target file and symlink pointing to it
    external_file = tmp_path / "host_secret.txt"
    external_file.write_text("TOP_SECRET")
    symlink_path = ws.workspace_path / "symlink_leak.txt"
    os.symlink(external_file, symlink_path)

    with pytest.raises(ScopeContainmentViolationError, match="points outside workspace"):
        ws.validate_containment("symlink_leak.txt")

    ws.cleanup()


# -----------------------------------------------------------------------------
# 5. Workspace Reuse Forbidden
# -----------------------------------------------------------------------------

def test_workspace_reuse_after_cleanup_forbidden(mock_target_git_repo: Path, tmp_path: Path):
    """Calling prepare() on a cleaned workspace raises WorkspaceReuseForbiddenError."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()
    ws.cleanup()

    with pytest.raises(WorkspaceReuseForbiddenError, match="cannot be reused"):
        ws.prepare()


# -----------------------------------------------------------------------------
# 6. Filesystem Integrity Checking
# -----------------------------------------------------------------------------

def test_integrity_check_authorized_modification(mock_target_git_repo: Path, tmp_path: Path):
    """Integrity check passes when only allowed files are modified."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Modify allowed file
    (ws.workspace_path / "src" / "app.py").write_text("def run():\n    return 100\n", encoding="utf-8")

    diff = ws.check_integrity(allowed_files=["src/app.py"])
    assert diff["valid"] is True
    assert diff["modified"] == ["src/app.py"]
    assert diff["added"] == []
    assert diff["deleted"] == []

    ws.cleanup()


def test_integrity_check_rejects_unauthorized_modification(mock_target_git_repo: Path, tmp_path: Path):
    """Integrity check raises WorkspaceIntegrityViolationError when unapproved file is modified."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Modify unapproved file
    (ws.workspace_path / "README.md").write_text("# Hacked README\n", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityViolationError, match="unauthorized changes detected"):
        ws.check_integrity(allowed_files=["src/app.py"])

    ws.cleanup()


def test_integrity_check_rejects_unauthorized_addition(mock_target_git_repo: Path, tmp_path: Path):
    """Integrity check raises WorkspaceIntegrityViolationError when an unexpected file is added."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Add unauthorized file
    (ws.workspace_path / "src" / "extra.py").write_text("# extra file\n")

    with pytest.raises(WorkspaceIntegrityViolationError, match="added: src/extra.py"):
        ws.check_integrity(allowed_files=["src/app.py"])

    ws.cleanup()


def test_integrity_check_rejects_unauthorized_deletion(mock_target_git_repo: Path, tmp_path: Path):
    """Integrity check raises WorkspaceIntegrityViolationError when an unexpected file is deleted."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Delete README.md
    (ws.workspace_path / "README.md").unlink()

    with pytest.raises(WorkspaceIntegrityViolationError, match="deleted: README.md"):
        ws.check_integrity(allowed_files=["src/app.py"])

    ws.cleanup()


def test_integrity_check_detects_symlink_attack(mock_target_git_repo: Path, tmp_path: Path):
    """Integrity check raises WorkspaceIntegrityViolationError if external symlink was introduced."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    # Add external symlink
    external_target = tmp_path / "external.txt"
    external_target.write_text("external")
    os.symlink(external_target, ws.workspace_path / "src" / "symlink_leak.txt")

    with pytest.raises(WorkspaceIntegrityViolationError, match="Symlink escape detected"):
        ws.check_integrity(allowed_files=["src/app.py", "src/symlink_leak.txt"])

    ws.cleanup()


# -----------------------------------------------------------------------------
# 7. Docker Sandbox Integration & Fail-Closed Behavior
# -----------------------------------------------------------------------------

def test_sandbox_execution_fail_closed_when_docker_unavailable(mock_target_git_repo: Path, tmp_path: Path):
    """When Docker daemon is unavailable, run_command_in_sandbox raises SandboxUnavailableError."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    with patch.object(DockerSandbox, "is_available", return_value=False):
        with pytest.raises(SandboxUnavailableError, match="fail closed"):
            ws.run_command_in_sandbox(command=["pytest", "tests/"])

    ws.cleanup()


def test_sandbox_execution_rejects_unapproved_command(mock_target_git_repo: Path, tmp_path: Path):
    """Commands outside policy whitelist are rejected before container dispatch."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    with patch.object(DockerSandbox, "is_available", return_value=True):
        with pytest.raises(ArbitraryExecutionForbiddenError, match="pre-approved command whitelist"):
            ws.run_command_in_sandbox(command=["curl", "https://malicious.site"])

    ws.cleanup()


def test_sandbox_execution_dispatches_with_writable_mount(mock_target_git_repo: Path, tmp_path: Path):
    """Sandbox execution builds SandboxRequest respecting read_only=False for writable scratch workspaces."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    mock_result = SandboxResult(
        request_id="sbr_test123",
        run_id="run_test",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="1 passed in 0.05s",
        stderr="",
        isolation_established=True,
    )

    with patch.object(DockerSandbox, "is_available", return_value=True), \
         patch.object(DockerSandbox, "run", return_value=mock_result) as mock_run:

        result = ws.run_command_in_sandbox(
            command=["pytest"],
            args=["tests/"],
            read_only=False,
        )

        assert result.status == ExecutionStatus.SUCCEEDED
        assert result.exit_code == 0
        mock_run.assert_called_once()
        req: SandboxRequest = mock_run.call_args[0][0]
        assert req.read_only_workspace is False
        assert req.command == ["pytest", "tests/"]
        assert req.network_policy == NetworkPolicy.DISABLED

    ws.cleanup()


def test_sandbox_execution_timeout_raises_resource_limit_error(mock_target_git_repo: Path, tmp_path: Path):
    """When container command times out, ResourceLimitViolationError is raised."""
    head_sha = _get_repo_head(mock_target_git_repo)
    ws = RepairWorkspace(
        target_repo_path=mock_target_git_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    ws.prepare()

    mock_timeout_result = SandboxResult(
        request_id="sbr_timeout",
        run_id="run_test",
        status=ExecutionStatus.TIMED_OUT,
        exit_code=None,
        stdout="",
        stderr="Execution exceeded 5.0 seconds",
        timed_out=True,
        isolation_established=True,
    )

    with patch.object(DockerSandbox, "is_available", return_value=True), \
         patch.object(DockerSandbox, "run", return_value=mock_timeout_result):

        with pytest.raises(ResourceLimitViolationError, match="timed out"):
            ws.run_command_in_sandbox(
                command=["python", "script.py"],
                timeout_seconds=5.0,
            )

    ws.cleanup()


# -----------------------------------------------------------------------------
# 8. Storage Persistence: WorkspaceRecord
# -----------------------------------------------------------------------------

def test_store_workspace_record_lifecycle(seeded_db: Path):
    """Verify WorkspaceRecord can be saved, retrieved, and updated in SQLite."""
    store = RepairExecutionStore(db_path=seeded_db)

    # First create parent repair execution
    req = RepairRequest(
        repair_id="rep_m2_store",
        finding_id="find_m2_001",
        run_id="run_m2_001",
        target_repo="/tmp/mock_repo",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_m2_001"],
        allowed_files=["src/app.py"],
    )
    store.save_repair_request(req)

    now = datetime.now(timezone.utc)
    rec = WorkspaceRecord(
        workspace_id="ws_store_001",
        repair_id="rep_m2_store",
        target_repo="/tmp/mock_repo",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        workspace_path="/tmp/forge_repair_workspaces/ws_store_001",
        provisioning_status="provisioned",
        isolation_verified=True,
        cleanup_status="active",
        metadata={"baseline_files_count": 5},
        created_at=now,
        updated_at=now,
    )
    store.save_workspace_record(rec)

    fetched = store.get_workspace_record("ws_store_001")
    assert fetched is not None
    assert fetched.workspace_id == "ws_store_001"
    assert fetched.isolation_verified is True
    assert fetched.cleanup_status == "active"
    assert fetched.metadata["baseline_files_count"] == 5

    by_repair = store.get_workspace_record_for_repair("rep_m2_store")
    assert by_repair is not None
    assert by_repair.workspace_id == "ws_store_001"

    # Update cleanup status
    store.update_workspace_record("ws_store_001", {"cleanup_status": "cleaned"})
    updated = store.get_workspace_record("ws_store_001")
    assert updated.cleanup_status == "cleaned"


# -----------------------------------------------------------------------------
# 9. RepairWorkspaceManager End-to-End Orchestration
# -----------------------------------------------------------------------------

def test_manager_end_to_end_lifecycle(
    seeded_db: Path,
    mock_target_git_repo: Path,
    tmp_path: Path,
):
    """RepairWorkspaceManager coordinates provisioning, integrity audit, execution, and cleanup."""
    store = RepairExecutionStore(db_path=seeded_db)
    head_sha = _get_repo_head(mock_target_git_repo)

    req = RepairRequest(
        repair_id="rep_m2_mgr",
        finding_id="find_m2_001",
        run_id="run_m2_001",
        target_repo=str(mock_target_git_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_m2_001"],
        allowed_files=["src/app.py"],
    )
    store.save_repair_request(req)

    manager = RepairWorkspaceManager(store=store)

    # 1. Provision workspace
    with patch.object(DockerSandbox, "is_available", return_value=True):
        ws = manager.provision_workspace(
            request=req,
            scratch_root=tmp_path / "scratch",
        )

    assert ws.is_prepared
    assert ws.workspace_path.exists()

    # Verify record in store
    rec = store.get_workspace_record(ws.workspace_id)
    assert rec is not None
    assert rec.provisioning_status == "provisioned"
    assert rec.isolation_verified is True
    assert rec.cleanup_status == "active"

    # 2. Modify within allowed scope & validate integrity
    (ws.workspace_path / "src" / "app.py").write_text("def run():\n    return 420\n", encoding="utf-8")
    diff = manager.validate_workspace_integrity(ws, req)
    assert diff["modified"] == ["src/app.py"]

    # 3. Execute approved command inside sandbox
    mock_sandbox_result = SandboxResult(
        request_id="sbr_mgr_run",
        run_id="run_m2_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="tests passed",
        stderr="",
        isolation_established=True,
    )
    with patch.object(DockerSandbox, "is_available", return_value=True), \
         patch.object(DockerSandbox, "run", return_value=mock_sandbox_result):

        exec_res = manager.execute_in_workspace(
            workspace=ws,
            request=req,
            command=["pytest"],
            args=["tests/"],
        )
        assert exec_res.status == ExecutionStatus.SUCCEEDED

    # 4. Cleanup workspace
    manager.cleanup_workspace(ws, repair_id=req.repair_id)
    assert not ws.workspace_path.exists()
    assert ws.is_cleaned

    # Verify record updated to cleaned
    rec_after = store.get_workspace_record(ws.workspace_id)
    assert rec_after.cleanup_status == "cleaned"

    # Verify audit event trail
    events = store.get_events(req.repair_id)
    event_actions = [e.payload.get("action") for e in events if "action" in e.payload]
    assert "workspace_provisioned" in event_actions
    assert "workspace_integrity_verified" in event_actions
    assert "sandbox_command_executed" in event_actions
    assert "workspace_cleaned" in event_actions


def test_manager_rejects_duplicate_active_workspace(
    seeded_db: Path,
    mock_target_git_repo: Path,
    tmp_path: Path,
):
    """RepairWorkspaceManager rejects provisioning duplicate active workspace for the same repair."""
    store = RepairExecutionStore(db_path=seeded_db)
    head_sha = _get_repo_head(mock_target_git_repo)

    req = RepairRequest(
        repair_id="rep_m2_dup",
        finding_id="find_m2_001",
        run_id="run_m2_001",
        target_repo=str(mock_target_git_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_m2_001"],
        allowed_files=["src/app.py"],
    )
    store.save_repair_request(req)

    manager = RepairWorkspaceManager(store=store)

    with patch.object(DockerSandbox, "is_available", return_value=True):
        ws = manager.provision_workspace(
            request=req,
            scratch_root=tmp_path / "scratch",
        )

        with pytest.raises(WorkspaceReuseForbiddenError, match="Active workspace.*already exists"):
            manager.provision_workspace(
                request=req,
                scratch_root=tmp_path / "scratch",
            )

        ws.cleanup()
