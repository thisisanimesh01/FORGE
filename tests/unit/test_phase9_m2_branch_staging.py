"""Comprehensive test suite for Phase 9 Milestone 2: Isolated Branch Staging & Commit Creation.

Covers:
- PublicationWorkspace isolation, provisioning, symlink guards, and cleanup.
- Target repository immutability verification and tamper detection.
- Authoritative operator authorization gate and re-verification.
- Deterministic branch planning, collision detection, and protected branch defenses.
- Approved patch application, scope containment, and binary diff rejection.
- GitCommitBuilder, normal descriptive messages without conventional prefixes, and post-commit verification.
- Sequential audit event persistence and StagedCommitRecord storage.
- CLI commands: stage, commit, status, inspect.
- End-to-end verified repair -> approval -> isolated staging -> verified local commit.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import pytest
from typer.testing import CliRunner

from forge.analysis.findings import Finding
from forge.cli import app
from forge.publication.branch import GitBranchManager
from forge.publication.commit import GitCommitBuilder
from forge.publication.failures import (
    BranchCollisionError,
    CommitCreationError,
    CommitVerificationError,
    PatchStagingError,
    ProtectedBranchViolationError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
    PublicationWorkspaceError,
    RepositoryDirtyError,
    SecretDetectedError,
    SymlinkEscapeError,
    TargetRepoMutationError,
    WorkspaceIntegrityError,
)
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
    PublicationAction,
    PublicationExecutionState,
    StagedCommitRecord,
)
from forge.publication.policy import GitPublicationPolicy
from forge.publication.staging import PublicationStagingCoordinator
from forge.publication.storage import PublicationStore
from forge.publication.workspace import PublicationWorkspace
from forge.repair.execution.models import (
    ApprovalBinding,
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    ComparisonStatus,
    IndependentValidationResult,
    OperatorDecisionType,
    PatchArtifact,
    RegressionAnalysisRecord,
    RepairExecutionPlan,
    RepairExecutionState,
    RepairRequest,
    ReviewDossier,
    SelectedTest,
    TestComparisonResult,
    TestExecutionRecord,
    TestSelectionCategory,
    ValidationOutcome,
    VerificationOutcome,
    VerificationPlan,
    VerificationRunRecord,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.schemas import (
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


@pytest.fixture
def target_git_repo(tmp_path: Path):
    """Fixture providing an initialized, clean target Git repository."""
    repo_dir = tmp_path / "target_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Target Committer"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "committer@target.local"], cwd=str(repo_dir), check=True, capture_output=True)

    # Initial file
    calc_file = repo_dir / "calc.py"
    calc_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    readme = repo_dir / "README.md"
    readme.write_text("# Target Repository\n", encoding="utf-8")

    subprocess.run(["git", "add", "calc.py", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    head_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    head_sha = head_res.stdout.strip().lower()

    return {
        "path": repo_dir,
        "head_sha": head_sha,
    }


@pytest.fixture
def m2_env(tmp_path: Path, target_git_repo):
    """Fixture setting up database, verified repair, patch artifact, and approval decision."""
    db_path = tmp_path / "forge_test.db"
    init_database(db_path)

    forge_store = ForgeStore(db_path=db_path)
    exec_store = RepairExecutionStore(db_path=db_path)
    pub_store = PublicationStore(db_path=db_path)

    target_repo = target_git_repo["path"]
    head_sha = target_git_repo["head_sha"]

    # 1. Run and Finding
    run_id = forge_store.create_run(target=str(target_repo), run_id="run_stage_01")
    finding = Finding(
        finding_id="find_stage_01",
        title="Arithmetic overflow defect in add function",
        description="add function fails on boundary precision inputs",
        lifecycle_state=FindingLifecycleState.SUSPECTED,
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        affected_files=["calc.py"],
    )
    forge_store.create_finding(
        run_id=run_id,
        title=finding.title,
        description=finding.description,
        severity=finding.severity,
        confidence=finding.confidence,
        affected_files=finding.affected_files,
        finding_id=finding.finding_id,
    )

    now = datetime.now(timezone.utc)
    repair_id = "rep_stage_01"
    diff_content = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n+    # overflow precision check\n     return a + b\n"
    patch_hash = hashlib.sha256(diff_content.encode("utf-8")).hexdigest()
    patch_id = f"patch_{patch_hash[:12]}"

    # 2. Repair Request
    repair_req = RepairRequest(
        repair_id=repair_id,
        finding_id=finding.finding_id,
        run_id=run_id,
        target_repo=str(target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_01"],
        allowed_files=["calc.py"],
        workspace_id="ws_01",
        state=RepairExecutionState.VERIFIED,
        patch_artifact_id=patch_id,
        created_at=now,
        updated_at=now,
    )
    exec_store.save_repair_request(repair_req)

    # 3. Patch Artifact
    patch = PatchArtifact(
        artifact_id=patch_id,
        repair_id=repair_id,
        finding_id=finding.finding_id,
        target_file="calc.py",
        affected_paths=["calc.py"],
        diff_content=diff_content,
        content_hash=patch_hash,
        base_commit_sha=head_sha,
        lines_added=1,
        lines_deleted=0,
        hunks_count=1,
        created_at=now,
    )
    exec_store.save_patch_artifact(patch)

    # 4. M4 Validation Result
    val_res = IndependentValidationResult(
        validation_id="val_stage_01",
        repair_id=repair_id,
        finding_id=finding.finding_id,
        repository_target=str(target_repo),
        base_commit_sha=head_sha,
        workspace_id="ws_01",
        patch_artifact_id=patch_id,
        patch_sha256=patch_hash,
        validated_file_paths=["calc.py"],
        validated_hunk_count=1,
        lines_added=1,
        lines_deleted=0,
        patch_bytes=len(diff_content.encode("utf-8")),
        overall_status=ValidationOutcome.ACCEPTED,
        is_valid=True,
        created_at=now,
    )
    exec_store.save_validation_result(val_res)

    # 5. M5 Verification Run
    vrun = VerificationRunRecord(
        verification_id="verif_stage_01",
        repair_id=repair_id,
        patch_artifact_id=patch_id,
        validation_id=val_res.validation_id,
        repository_target=str(target_repo),
        base_commit_sha=head_sha,
        workspace_id="ws_01",
        patch_sha256=patch_hash,
        plan=VerificationPlan(
            plan_id="vplan_01",
            repair_id=repair_id,
            patch_artifact_id=patch_id,
            selected_tests=[
                SelectedTest(test_id="t1", test_name="test_add", category=TestSelectionCategory.REPRODUCTION, command=["pytest"])
            ],
        ),
        baseline_executions=[],
        patched_executions=[],
        regression_analysis=RegressionAnalysisRecord(
            regression_detected=False,
            defect_resolved=True,
            comparisons=[],
            new_failures_count=0,
            fixed_tests_count=1,
            stable_tests_count=0,
            summary="Defect resolved with zero regressions",
        ),
        overall_outcome=VerificationOutcome.VERIFIED,
        started_at=now,
        completed_at=now,
    )
    exec_store.save_verification_run(vrun)

    # 6. Dossier
    dossier = ReviewDossier(
        dossier_id="dos_stage_01",
        repair_id=repair_id,
        finding_id=finding.finding_id,
        finding_title=finding.title,
        finding_severity=finding.severity.value,
        repository_target=str(target_repo),
        base_commit_sha=head_sha,
        patch_artifact_id=patch_id,
        patch_sha256=patch_hash,
        target_files=["calc.py"],
        unified_diff=diff_content,
        validation_id=val_res.validation_id,
        validation_outcome="accepted",
        verification_id=vrun.verification_id,
        verification_outcome="verified",
        workspace_id="ws_01",
        created_at=now,
    )
    exec_store.save_review_dossier(dossier)

    # 7. Operator Approval Decision
    binding = ApprovalBinding(
        repair_id=repair_id,
        finding_id=finding.finding_id,
        repository_target=str(target_repo),
        base_commit_sha=head_sha,
        patch_artifact_id=patch_id,
        patch_sha256=patch_hash,
        validation_id=val_res.validation_id,
        verification_id=vrun.verification_id,
        dossier_id=dossier.dossier_id,
    )
    decision = ApprovalDecisionRecord(
        decision_id="dec_stage_01",
        repair_id=repair_id,
        binding=binding,
        state=ApprovalState.APPROVED,
        decision=OperatorDecisionType.APPROVE,
        authorized_action=AuthorizedAction.STAGE_BRANCH,
        actor="operator_security@forge.local",
        reason="Verified defect repair with zero regressions.",
        ttl_hours=48.0,
        expires_at=now + timedelta(hours=48),
        created_at=now,
    )
    exec_store.save_approval_decision(decision)

    return {
        "db_path": db_path,
        "forge_store": forge_store,
        "exec_store": exec_store,
        "pub_store": pub_store,
        "target_repo": target_repo,
        "head_sha": head_sha,
        "finding": finding,
        "patch": patch,
        "repair_req": repair_req,
        "decision": decision,
        "dossier": dossier,
        "scratch_root": tmp_path / "scratch",
    }


# =============================================================================
# 1. PublicationWorkspace Tests
# =============================================================================

def test_workspace_isolation_and_base_commit_checkout(m2_env):
    """PublicationWorkspace clones target repo with --no-hardlinks and checks out base commit."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    try:
        ws.prepare()
        assert ws.is_prepared is True
        assert ws.workspace_path.exists()
        assert ws.workspace_path != env["target_repo"]

        # Check git rev-parse HEAD in workspace matches base_commit_sha
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ws.workspace_path), capture_output=True, text=True, check=True)
        assert res.stdout.strip().lower() == env["head_sha"]

        # Check baseline hashes exist
        assert len(ws.baseline_hashes) >= 2
        assert "calc.py" in ws.baseline_hashes
    finally:
        ws.cleanup()
        assert ws.is_cleaned is True
        assert not ws.workspace_path.exists()


def test_workspace_rejects_dirty_target_repository(m2_env):
    """PublicationWorkspace rejects initialization if target repository has uncommitted changes."""
    env = m2_env
    dirty_file = env["target_repo"] / "dirty.txt"
    dirty_file.write_text("uncommitted change")

    with pytest.raises(RepositoryDirtyError, match="has uncommitted changes"):
        PublicationWorkspace(
            target_repo_path=env["target_repo"],
            base_commit_sha=env["head_sha"],
            scratch_root=env["scratch_root"],
        )


def test_workspace_symlink_escape_detection(m2_env, tmp_path: Path):
    """PublicationWorkspace rejects symlinks pointing outside workspace root."""
    env = m2_env
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret outside")

    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        # Create an escaping symlink inside workspace
        escaping_link = ws.workspace_path / "escape_link.txt"
        escaping_link.symlink_to(outside_file)

        with pytest.raises(SymlinkEscapeError, match="points outside workspace"):
            ws.check_integrity()
    finally:
        ws.cleanup()


def test_workspace_path_containment_validation(m2_env):
    """PublicationWorkspace validate_containment rejects path traversal attempts."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        # Valid path
        valid_path = ws.validate_containment("calc.py")
        assert valid_path.exists()

        # Path traversal
        with pytest.raises(SymlinkEscapeError, match="Path traversal"):
            ws.validate_containment("../outside.py")

        with pytest.raises(SymlinkEscapeError, match="Absolute path.*escapes workspace"):
            ws.validate_containment("/etc/passwd")
    finally:
        ws.cleanup()


def test_workspace_reuse_forbidden_after_cleanup(m2_env):
    """PublicationWorkspace cannot be reused once cleanup has run."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    ws.cleanup()

    with pytest.raises(PublicationWorkspaceError, match="already cleaned up"):
        ws.prepare()


def test_target_repository_immutability_monitoring(m2_env):
    """Detects target repository mutation and raises TargetRepoMutationError."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        assert ws.check_target_immutability() is True

        # Mutate target repository by touching a file
        mutated_file = env["target_repo"] / "calc.py"
        mutated_file.write_text("TAMPERED")

        assert ws.check_target_immutability() is False
        with pytest.raises(TargetRepoMutationError, match="was mutated"):
            ws.verify_target_immutability()
    finally:
        ws.cleanup()


# =============================================================================
# 2. GitBranchManager Tests
# =============================================================================

def test_branch_planning_and_naming_rules():
    """GitBranchManager plans deterministic branches with mandatory forge/ prefix."""
    mgr = GitBranchManager()
    proposal = mgr.plan_branch(
        base_commit_sha="a" * 40,
        finding_id="find_auth_01",
        patch_sha256="b" * 64,
    )
    assert proposal.branch_name == f"forge/fix-auth_01-{'b' * 8}"
    assert proposal.branch_name.startswith("forge/")


def test_branch_creation_in_workspace(m2_env):
    """GitBranchManager creates branch in workspace without touching target repo."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        mgr = GitBranchManager()
        branch_name = "forge/fix-calc-001"
        created_name = mgr.create_branch(ws, branch_name, env["head_sha"])
        assert created_name == branch_name

        # Verify active branch in workspace
        res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ws.workspace_path), capture_output=True, text=True, check=True)
        assert res.stdout.strip() == branch_name

        # Verify target repo still on initial branch
        assert not mgr.branch_exists(env["target_repo"], branch_name)
    finally:
        ws.cleanup()


def test_branch_collision_rejected(m2_env):
    """GitBranchManager rejects creating a branch that already exists."""
    env = m2_env
    # Create branch in target repo
    subprocess.run(["git", "branch", "forge/existing-branch"], cwd=str(env["target_repo"]), check=True, capture_output=True)

    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        mgr = GitBranchManager()
        with pytest.raises(BranchCollisionError, match="already exists"):
            mgr.create_branch(ws, "forge/existing-branch", env["head_sha"])
    finally:
        ws.cleanup()


def test_protected_branch_rejected_for_creation():
    """GitBranchManager rejects protected branch names."""
    mgr = GitBranchManager()
    with pytest.raises(ProtectedBranchViolationError):
        mgr.validate_branch_name("main")

    with pytest.raises(ProtectedBranchViolationError):
        mgr.validate_branch_name("release/1.0.0")


# =============================================================================
# 3. GitCommitBuilder & Patch Application Tests
# =============================================================================

def test_plan_commit_creates_normal_descriptive_message(m2_env):
    """GitCommitBuilder creates normal descriptive commit message with NO conventional prefixes."""
    env = m2_env
    builder = GitCommitBuilder()
    proposal = builder.plan_commit(
        finding=env["finding"],
        patch=env["patch"],
        dossier=env["dossier"],
        approval=env["decision"],
    )

    first_line = proposal.commit_message.splitlines()[0]
    assert not first_line.startswith(("feat:", "fix:", "docs:", "chore:", "refactor:"))
    assert "Arithmetic overflow defect" in first_line
    assert f"Finding: {env['finding'].finding_id}" in proposal.commit_message
    assert f"Patch SHA-256: {env['patch'].content_hash}" in proposal.commit_message


def test_secret_in_commit_message_rejected(m2_env):
    """GitCommitBuilder rejects commit message containing sensitive tokens."""
    env = m2_env
    builder = GitCommitBuilder()
    with pytest.raises(SecretDetectedError):
        builder.validate_commit_message("Fix bug with sk-1234567890123456789012")


def test_patch_application_and_staging(m2_env):
    """GitCommitBuilder applies approved patch and stages exactly the affected file."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        builder = GitCommitBuilder()
        touched = builder.apply_and_stage_patch(
            workspace=ws,
            patch=env["patch"],
            allowed_files=env["repair_req"].allowed_files,
        )
        assert touched == ["calc.py"]

        # Check git status in workspace has staged calc.py
        res = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(ws.workspace_path), capture_output=True, text=True, check=True)
        assert res.stdout.strip().splitlines() == ["calc.py"]
    finally:
        ws.cleanup()


def test_tampered_patch_content_hash_rejected(m2_env):
    """GitCommitBuilder rejects patch whose content does not match its SHA-256 digest."""
    env = m2_env
    tampered_patch = env["patch"].model_copy(update={"content_hash": "0" * 64})

    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        builder = GitCommitBuilder()
        with pytest.raises(PatchStagingError, match="integrity check failed"):
            builder.apply_and_stage_patch(
                workspace=ws,
                patch=tampered_patch,
                allowed_files=["calc.py"],
            )
    finally:
        ws.cleanup()


def test_binary_patch_rejected(m2_env):
    """GitCommitBuilder rejects binary patch diffs."""
    env = m2_env
    binary_diff = "GIT binary patch\nliteral 1234\n"
    binary_hash = hashlib.sha256(binary_diff.encode("utf-8")).hexdigest()
    binary_patch = env["patch"].model_copy(update={"diff_content": binary_diff, "content_hash": binary_hash})

    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        builder = GitCommitBuilder()
        with pytest.raises(PatchStagingError, match="Binary patch"):
            builder.apply_and_stage_patch(
                workspace=ws,
                patch=binary_patch,
                allowed_files=["calc.py"],
            )
    finally:
        ws.cleanup()


def test_unauthorized_file_modification_rejected(m2_env):
    """GitCommitBuilder rejects patch targeting files not in allowed_files scope."""
    env = m2_env
    unauthorized_patch = env["patch"].model_copy(update={"affected_paths": ["calc.py", "unauthorized.py"]})

    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        builder = GitCommitBuilder()
        with pytest.raises(PatchStagingError, match="unauthorized files"):
            builder.apply_and_stage_patch(
                workspace=ws,
                patch=unauthorized_patch,
                allowed_files=["calc.py"],
            )
    finally:
        ws.cleanup()


def test_commit_creation_and_parent_verification(m2_env):
    """GitCommitBuilder creates commit in workspace and verifies parent SHA equals base_commit_sha."""
    env = m2_env
    ws = PublicationWorkspace(
        target_repo_path=env["target_repo"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["scratch_root"],
    )
    ws.prepare()
    try:
        # Create branch
        branch_mgr = GitBranchManager()
        branch_mgr.create_branch(ws, "forge/fix-test", env["head_sha"])

        # Stage patch
        builder = GitCommitBuilder()
        builder.apply_and_stage_patch(ws, env["patch"], env["repair_req"].allowed_files)

        proposal = builder.plan_commit(env["finding"], env["patch"], env["dossier"], env["decision"])
        result = builder.create_commit(ws, proposal, env["head_sha"])

        assert result["commit_sha"] is not None
        assert result["parent_commit_sha"] == env["head_sha"]
        assert result["tree_sha"] is not None
        assert result["changed_files"] == ["calc.py"]

        # Ensure working tree is clean in workspace
        status_res = subprocess.run(["git", "status", "--porcelain"], cwd=str(ws.workspace_path), capture_output=True, text=True, check=True)
        assert status_res.stdout.strip() == ""

        # Assert target repository has ZERO new commits
        target_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(env["target_repo"]), capture_output=True, text=True, check=True)
        assert target_head.stdout.strip().lower() == env["head_sha"]
    finally:
        ws.cleanup()


# =============================================================================
# 4. Authorization Re-Validation & Anti-Escalation Tests
# =============================================================================

def test_export_patch_authorization_rejected_for_staging(m2_env):
    """Operator approval of EXPORT_PATCH strictly rejects branch staging."""
    env = m2_env
    exp_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.EXPORT_PATCH})
    env["exec_store"].save_approval_decision(exp_dec)

    coord = PublicationStagingCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        publication_store=env["pub_store"],
    )

    with pytest.raises(PublicationAuthorizationError, match="local branch staging requires"):
        coord.stage_and_commit(env["repair_req"].repair_id, env["target_repo"])


def test_manual_integration_authorization_rejected_for_staging(m2_env):
    """Operator approval of MANUAL_INTEGRATION strictly rejects branch staging."""
    env = m2_env
    mi_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.MANUAL_INTEGRATION})
    env["exec_store"].save_approval_decision(mi_dec)

    coord = PublicationStagingCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        publication_store=env["pub_store"],
    )

    with pytest.raises(PublicationAuthorizationError, match="local branch staging requires"):
        coord.stage_and_commit(env["repair_req"].repair_id, env["target_repo"])


def test_stage_for_pr_authorized_for_local_staging(m2_env):
    """Operator approval of STAGE_FOR_PR permits local branch staging."""
    env = m2_env
    sfp_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.STAGE_FOR_PR})
    env["exec_store"].save_approval_decision(sfp_dec)

    coord = PublicationStagingCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        publication_store=env["pub_store"],
    )

    record = coord.stage_and_commit(
        env["repair_req"].repair_id,
        env["target_repo"],
        scratch_root=env["scratch_root"],
        auto_cleanup=True,
    )
    assert record.state == PublicationExecutionState.CLEANED
    assert record.commit_sha is not None


def test_create_pr_authorized_for_local_staging(m2_env):
    """Operator approval of CREATE_PR authorizes prerequisite local branch staging."""
    env = m2_env
    cpr_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.CREATE_PR})
    env["exec_store"].save_approval_decision(cpr_dec)

    coord = PublicationStagingCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        publication_store=env["pub_store"],
    )

    record = coord.stage_and_commit(
        env["repair_req"].repair_id,
        env["target_repo"],
        scratch_root=env["scratch_root"],
        auto_cleanup=True,
    )
    assert record.state == PublicationExecutionState.CLEANED
    assert record.commit_sha is not None


def test_expired_approval_rejected_at_staging_gate(m2_env):
    """Expired operator approval is rejected."""
    env = m2_env
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    exp_dec = env["decision"].model_copy(update={"expires_at": past})
    env["exec_store"].save_approval_decision(exp_dec)

    coord = PublicationStagingCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        publication_store=env["pub_store"],
    )

    with pytest.raises(PublicationAuthorizationError, match="expired"):
        coord.stage_and_commit(env["repair_req"].repair_id, env["target_repo"])


# =============================================================================
# 5. End-to-End Staging Coordinator & Persistence Tests
# =============================================================================

def test_end_to_end_staging_and_commit_workflow(m2_env):
    """Full workflow: approved repair -> isolated staging -> verified commit -> audit trail."""
    env = m2_env
    coord = PublicationStagingCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        publication_store=env["pub_store"],
    )

    record = coord.stage_and_commit(
        repair_id=env["repair_req"].repair_id,
        repository_path=env["target_repo"],
        scratch_root=env["scratch_root"],
        auto_cleanup=False,
    )

    # 1. Assert record attributes
    assert record.state == PublicationExecutionState.VERIFIED
    assert record.commit_sha is not None
    assert record.tree_sha is not None
    assert record.parent_commit_sha == env["head_sha"]
    assert record.changed_files == ["calc.py"]
    assert record.workspace_integrity is not None
    assert record.workspace_integrity.target_repo_immutable is True

    # 2. Assert target repository remains untouched
    target_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(env["target_repo"]), capture_output=True, text=True, check=True)
    assert target_head.stdout.strip().lower() == env["head_sha"]

    target_branch = subprocess.run(["git", "branch"], cwd=str(env["target_repo"]), capture_output=True, text=True, check=True)
    assert record.branch_name not in target_branch.stdout

    # 3. Assert SQLite persistence in publication_staged_commits
    persisted = env["pub_store"].get_staged_commit(record.staging_id)
    assert persisted is not None
    assert persisted.commit_sha == record.commit_sha

    by_repair = env["pub_store"].get_staged_commit_for_repair(env["repair_req"].repair_id)
    assert by_repair is not None
    assert by_repair.staging_id == record.staging_id

    # 4. Assert sequential audit events recorded in repair_execution_events
    conn = sqlite3.connect(env["db_path"])
    cur = conn.execute(
        "SELECT sequence, actor, payload_json FROM repair_execution_events WHERE repair_id = ? ORDER BY sequence ASC",
        (env["repair_req"].repair_id,),
    )
    rows = cur.fetchall()
    conn.close()

    actions = [json.loads(r[2]).get("action") for r in rows if "action" in json.loads(r[2])]
    assert "workspace_provisioned" in actions
    assert "authorization_revalidated" in actions
    assert "branch_created" in actions
    assert "patch_applied" in actions
    assert "patch_integrity_verified" in actions
    assert "commit_created" in actions
    assert "commit_verified" in actions

    # Monotonic sequence numbers
    sequences = [r[0] for r in rows]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


# =============================================================================
# 6. CLI Integration Tests
# =============================================================================

def test_cli_publish_stage_command(m2_env, monkeypatch):
    """Test 'forge repair publish stage' command."""
    env = m2_env
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    result = runner.invoke(
        app,
        [
            "repair",
            "publish",
            "stage",
            env["repair_req"].repair_id,
            "--repo",
            str(env["target_repo"]),
            "--clean",
        ],
    )
    assert result.exit_code == 0
    assert "[STAGED]" in result.output
    assert "Branch Name:" in result.output
    assert "Commit SHA:" in result.output
    assert "Target Repo:" in result.output
    assert "IMMUTABLE" in result.output


def test_cli_publish_status_and_inspect_commands(m2_env, monkeypatch):
    """Test 'forge publish status' and 'forge publish inspect' commands."""
    env = m2_env
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    # 1. Run stage
    runner.invoke(
        app,
        [
            "publish",
            "stage",
            env["repair_req"].repair_id,
            "--repo",
            str(env["target_repo"]),
        ],
    )

    # 2. Check status
    res_status = runner.invoke(app, ["publish", "status", env["repair_req"].repair_id])
    assert res_status.exit_code == 0
    assert "Publication Staging Status" in res_status.output
    assert "Commit SHA:" in res_status.output

    # 3. Check inspect
    res_inspect = runner.invoke(app, ["publish", "inspect", env["repair_req"].repair_id])
    assert res_inspect.exit_code == 0
    assert "Detailed Publication Inspection" in res_inspect.output
    assert "Audit Trail" in res_inspect.output
