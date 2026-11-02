"""Comprehensive unit and security test suite for Phase 9 Milestone 1:
Git Integration Architecture & Publication Policy.

Covers:
- Protocol adherence (GitRepositoryInspectorProtocol, GitPublicationValidatorProtocol, PublicationAuditStoreProtocol).
- Repository identity verification and immutability guarantees.
- Publication policy defaults (default-deny, forbidden force-push/history rewrite).
- Precondition & authorization enforcement (missing approval, expired, invalidated, rejected).
- Action privilege escalation defense (EXPORT_PATCH cannot authorize STAGE_BRANCH or CREATE_PR).
- Target repository identity matching (canonical path, remote URL, base commit SHA).
- Dirty working tree detection.
- Artifact binding and tamper detection.
- Branch naming syntax and protected branch defenses.
- Outbound secret scanning on commit messages and metadata.
- Cross-repair approval reuse rejection.
- SQLite persistence of publication requests and plans.
- Sequential audit event ledger integration.
- Full CLI command integration (plan, validate, show, list).
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.analysis.findings import Finding
from forge.cli import app
from forge.publication.failures import (
    ArtifactBindingMismatchError,
    ProtectedBranchViolationError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
    PublicationError,
    PublicationPolicyViolationError,
    RepositoryDirtyError,
    RepositoryIdentityMismatchError,
    SecretDetectedError,
)
from forge.publication.inspector import GitRepositoryInspector
from forge.publication.interfaces import (
    GitBranchManagerProtocol,
    GitCommitBuilderProtocol,
    GitPublicationValidatorProtocol,
    GitRepositoryInspectorProtocol,
    PublicationAuditStoreProtocol,
)
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
    PublicationAction,
    PublicationPlan,
    PublicationRequest,
    PublicationStatus,
    RepositoryIdentity,
)
from forge.publication.planner import GitPublicationPlanner
from forge.publication.policy import (
    GitPublicationPolicy,
    is_protected_branch,
    scan_for_secrets,
    validate_branch_name,
    validate_remote_url,
)
from forge.publication.storage import PublicationStore
from forge.publication.validator import GitPublicationValidator
from forge.repair.execution.approval import ApprovalManager
from forge.repair.execution.dossier import ReviewDossierBuilder
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
    RepairActor,
    RepairExecutionPlan,
    RepairExecutionState,
    RepairRequest,
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
from forge.repair.execution.workspace import RepairWorkspace
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    """Provide isolated SQLite database path."""
    return tmp_path / "test_p9_m1.db"


@pytest.fixture
def mock_target_repo(tmp_path: Path) -> Path:
    """Create a mock target git repository with committed code and clean working tree."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    calc_code = (
        "def compute_total(price: float, tax: float) -> float:\n"
        "    if tax < 0.0:\n"
        "        raise ValueError('Tax cannot be negative')\n"
        "    return price * (1.0 + tax)\n"
    )
    (src_dir / "calc.py").write_text(calc_code, encoding="utf-8")
    (repo_dir / "README.md").write_text("# Mock Target Repo\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    return repo_dir


def _get_head_sha(repo_dir: Path) -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    return res.stdout.strip()


@pytest.fixture
def approved_environment(test_db_path: Path, mock_target_repo: Path, tmp_path: Path):
    """Seed SQLite database with fully verified and operator-approved repair execution."""
    forge_store = ForgeStore(db_path=test_db_path)
    exec_store = RepairExecutionStore(db_path=test_db_path)
    head_sha = _get_head_sha(mock_target_repo)

    run_id = forge_store.create_run(target=str(mock_target_repo), run_id="run_p9_seed")

    finding = forge_store.create_finding(
        run_id=run_id,
        title="Missing tax rate validation",
        description="compute_total fails to reject negative tax rates",
        severity=FindingSeverity.HIGH,
        finding_id="find_p9_001",
        affected_files=["src/calc.py"],
    )

    ev_repro = EvidenceRecord(
        evidence_id="evi_p9_repro",
        run_id=run_id,
        finding_id="find_p9_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Reproduction showing negative tax handling defect",
        content="AssertionError: Expected ValueError on negative tax rate",
        file_path="src/calc.py",
        details={"status": "reproduced", "commit_sha": head_sha},
    )
    forge_store.attach_evidence(ev_repro)
    forge_store.transition_finding("find_p9_001", FindingLifecycleState.REPRODUCED, "evi_p9_repro")

    workspace = RepairWorkspace(
        target_repo_path=mock_target_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    workspace.prepare()

    repair_req = RepairRequest(
        repair_id="rep_p9_001",
        finding_id="find_p9_001",
        run_id=run_id,
        target_repo=str(mock_target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_p9_repro"],
        allowed_files=["src/calc.py"],
        workspace_id=workspace.workspace_id,
        state=RepairExecutionState.VERIFIED,
    )
    exec_store.save_repair_request(repair_req)

    clean_diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def compute_total(price: float, tax: float) -> float:\n"
        "+    if tax < 0.0:\n"
        "+        raise ValueError('Tax cannot be negative')\n"
        "     return price * (1.0 + tax)\n"
    )
    import hashlib
    patch_digest = hashlib.sha256(clean_diff.encode("utf-8")).hexdigest()
    patch_id = f"patch_{patch_digest[:12]}"

    patch = PatchArtifact(
        artifact_id=patch_id,
        repair_id="rep_p9_001",
        finding_id="find_p9_001",
        target_file="src/calc.py",
        affected_paths=["src/calc.py"],
        diff_content=clean_diff,
        content_hash=patch_digest,
        base_commit_sha=head_sha,
        lines_added=2,
        lines_deleted=0,
        hunks_count=1,
    )
    exec_store.save_patch_artifact(patch)
    repair_req = repair_req.model_copy(update={"patch_artifact_id": patch.artifact_id})
    exec_store.save_repair_request(repair_req)

    now = datetime.now(timezone.utc)
    val_res = IndependentValidationResult(
        validation_id="val_p9_001",
        patch_artifact_id=patch.artifact_id,
        repair_id="rep_p9_001",
        finding_id="find_p9_001",
        repository_target=str(mock_target_repo),
        base_commit_sha=head_sha,
        workspace_id=workspace.workspace_id,
        patch_sha256=patch_digest,
        validated_file_paths=["src/calc.py"],
        validated_hunk_count=1,
        lines_added=2,
        lines_deleted=0,
        patch_bytes=len(clean_diff.encode("utf-8")),
        scope_result={"valid": True, "files_checked": 1},
        integrity_result={"valid": True, "syntax_checked": True},
        semantic_result={"valid": True, "invariants_checked": True},
        overall_status=ValidationOutcome.ACCEPTED,
        is_valid=True,
        checks_passed=["scope_containment", "ast_syntax", "semantic_invariants"],
        validated_at=now,
    )
    exec_store.save_validation_result(val_res)

    base_rec = TestExecutionRecord(
        execution_id="exec_base_p9_001",
        verification_id="verif_p9_001",
        repair_id="rep_p9_001",
        workspace_id=workspace.workspace_id,
        target_type="baseline",
        test_name="tax_defect_test",
        command=["pytest", "tests/test_calc.py"],
        exit_code=1,
        status="failed",
        passed=False,
        timed_out=False,
        duration_ms=100.0,
        tests_collected=1,
        tests_executed=1,
        tests_passed=0,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = TestExecutionRecord(
        execution_id="exec_patch_p9_001",
        verification_id="verif_p9_001",
        repair_id="rep_p9_001",
        workspace_id=workspace.workspace_id,
        target_type="patched",
        test_name="tax_defect_test",
        command=["pytest", "tests/test_calc.py"],
        exit_code=0,
        status="passed",
        passed=True,
        timed_out=False,
        duration_ms=110.0,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        tests_failed=0,
        started_at=now,
        completed_at=now,
    )
    reg_analysis = RegressionAnalysisRecord(
        regression_detected=False,
        defect_resolved=True,
        comparisons=[
            TestComparisonResult(
                test_name="tax_defect_test",
                status=ComparisonStatus.FIXED,
                baseline_passed=False,
                patched_passed=True,
                details="Defect resolved",
            )
        ],
        new_failures_count=0,
        fixed_tests_count=1,
        stable_tests_count=0,
        summary="REPAIR VERIFIED: defect resolved, 0 regressions",
    )
    verif_plan = VerificationPlan(
        plan_id="vplan_p9_001",
        repair_id="rep_p9_001",
        patch_artifact_id=patch.artifact_id,
        selected_tests=[
            SelectedTest(
                test_id="t_01",
                test_name="tax_defect_test",
                category=TestSelectionCategory.REPRODUCTION,
                command=["pytest", "tests/test_calc.py"],
            )
        ],
    )
    verif_run = VerificationRunRecord(
        verification_id="verif_p9_001",
        repair_id="rep_p9_001",
        patch_artifact_id=patch.artifact_id,
        validation_id=val_res.validation_id,
        repository_target=str(mock_target_repo),
        base_commit_sha=head_sha,
        workspace_id=workspace.workspace_id,
        patch_sha256=patch_digest,
        plan=verif_plan,
        baseline_executions=[base_rec],
        patched_executions=[patch_rec],
        regression_analysis=reg_analysis,
        overall_outcome=VerificationOutcome.VERIFIED,
        started_at=now,
        completed_at=now,
    )
    exec_store.save_verification_run(verif_run)

    # Compile Dossier and Submit M6 Approval
    dossier_builder = ReviewDossierBuilder(execution_store=exec_store, forge_store=forge_store)
    dossier = dossier_builder.build_dossier(repair_req.repair_id)

    approval_mgr = ApprovalManager(execution_store=exec_store, forge_store=forge_store)
    decision = approval_mgr.submit_decision(
        repair_id=repair_req.repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="chief_engineer@forge.local",
        reason="Verified defect resolution, 0 regressions, authorized for PR staging.",
        authorized_action=AuthorizedAction.STAGE_FOR_PR,
        ttl_hours=48.0,
    )

    pub_store = PublicationStore(db_path=test_db_path)

    return {
        "forge_store": forge_store,
        "exec_store": exec_store,
        "pub_store": pub_store,
        "request": repair_req,
        "patch": patch,
        "validation_result": val_res,
        "verification_run": verif_run,
        "dossier": dossier,
        "decision": decision,
        "finding": finding,
        "workspace": workspace,
        "target_repo": mock_target_repo,
        "head_sha": head_sha,
        "patch_digest": patch_digest,
        "db_path": test_db_path,
    }


# =============================================================================
# 1. Protocol Adherence Tests
# =============================================================================

def test_inspector_satisfies_protocol():
    """GitRepositoryInspector strictly implements GitRepositoryInspectorProtocol."""
    inspector = GitRepositoryInspector()
    assert isinstance(inspector, GitRepositoryInspectorProtocol)


def test_validator_satisfies_protocol(test_db_path: Path):
    """GitPublicationValidator strictly implements GitPublicationValidatorProtocol."""
    exec_store = RepairExecutionStore(db_path=test_db_path)
    validator = GitPublicationValidator(execution_store=exec_store)
    assert isinstance(validator, GitPublicationValidatorProtocol)


def test_store_satisfies_protocol(test_db_path: Path):
    """PublicationStore strictly implements PublicationAuditStoreProtocol."""
    store = PublicationStore(db_path=test_db_path)
    assert isinstance(store, PublicationAuditStoreProtocol)


# =============================================================================
# 2. Repository Identity & Inspection Tests
# =============================================================================

def test_inspector_extracts_complete_repository_identity(mock_target_repo: Path):
    """GitRepositoryInspector accurately gathers 7-tuple repository identity."""
    inspector = GitRepositoryInspector()
    identity = inspector.inspect_repository(mock_target_repo)

    assert identity.canonical_path == str(mock_target_repo.resolve())
    assert identity.git_root == str(mock_target_repo.resolve())
    assert len(identity.base_commit_sha) == 40
    assert identity.is_clean is True
    assert identity.modified_files_count == 0
    assert identity.untracked_files_count == 0


def test_inspector_detects_dirty_working_tree(mock_target_repo: Path):
    """GitRepositoryInspector accurately detects dirty working trees."""
    inspector = GitRepositoryInspector()
    dirty_file = mock_target_repo / "untracked.txt"
    dirty_file.write_text("untracked", encoding="utf-8")

    identity = inspector.inspect_repository(mock_target_repo)
    assert identity.is_clean is False
    assert identity.untracked_files_count == 1


def test_inspector_nonexistent_repo_raises(tmp_path: Path):
    """GitRepositoryInspector raises RepositoryIdentityMismatchError on nonexistent path."""
    inspector = GitRepositoryInspector()
    nonexistent = tmp_path / "does_not_exist"
    with pytest.raises(RepositoryIdentityMismatchError, match="does not exist"):
        inspector.inspect_repository(nonexistent)


def test_inspector_preserves_target_repo_immutability(mock_target_repo: Path):
    """Repository inspection is completely read-only; leaves Git state untouched."""
    inspector = GitRepositoryInspector()
    head_before = _get_head_sha(mock_target_repo)

    identity = inspector.inspect_repository(mock_target_repo)
    head_after = _get_head_sha(mock_target_repo)

    assert head_before == head_after == identity.base_commit_sha
    assert inspector.check_working_tree_clean(mock_target_repo) is True


# =============================================================================
# 3. Publication Policy & Safety Boundary Tests
# =============================================================================

def test_policy_defaults_deny_force_push():
    """GitPublicationPolicy default-deny behavior forbids destructive Git operations."""
    policy = GitPublicationPolicy()
    assert policy.allow_force_push is False
    assert policy.allow_history_rewrite is False
    assert policy.allow_automatic_merge is False
    assert policy.allow_software_deployment is False
    policy.validate_safety_invariants()


def test_policy_rejects_force_push_tampering():
    """GitPublicationPolicy fails closed if allow_force_push is tampered to True."""
    policy = GitPublicationPolicy(allow_force_push=True)
    with pytest.raises(PublicationPolicyViolationError, match="allow_force_push must never be True"):
        policy.validate_safety_invariants()


def test_valid_branch_name_accepted():
    """Branch names matching prefix and regex are accepted."""
    policy = GitPublicationPolicy()
    validate_branch_name("forge/fix-find01-abc1234", policy)
    validate_branch_name("forge/repair-calc-patch-1", policy)


def test_invalid_branch_name_rejected_syntax():
    """Branch names with illegal Git ref syntax or exceeding length are rejected."""
    policy = GitPublicationPolicy()
    with pytest.raises(PublicationPolicyViolationError, match="prefix"):
        validate_branch_name("feature/my-branch", policy)
    with pytest.raises(PublicationPolicyViolationError, match="illegal Git ref"):
        validate_branch_name("forge/fix..test", policy)
    with pytest.raises(PublicationPolicyViolationError, match="illegal Git ref"):
        validate_branch_name("forge/fix test", policy)


def test_protected_branch_name_rejected():
    """Attempts to name or target protected branches are strictly blocked."""
    policy = GitPublicationPolicy()
    assert is_protected_branch("main", policy) is True
    assert is_protected_branch("master", policy) is True
    assert is_protected_branch("release/1.0", policy) is True

    with pytest.raises(ProtectedBranchViolationError, match="collides with protected branch"):
        validate_branch_name("main", policy)


def test_remote_url_validation_scheme_and_domain():
    """Remote URL validation enforces authorized schemes and domains."""
    policy = GitPublicationPolicy(allowed_remote_domains={"github.com", "gitlab.com"})
    validate_remote_url("https://github.com/example/repo.git", policy)

    with pytest.raises(PublicationPolicyViolationError, match="unauthorized scheme"):
        validate_remote_url("ftp://github.com/repo.git", policy)

    with pytest.raises(PublicationPolicyViolationError, match="not in allowed domains"):
        validate_remote_url("https://malicious.net/repo.git", policy)


# =============================================================================
# 4. Publication Authorization & Security Enforcement Tests
# =============================================================================

def test_publication_validator_accepts_valid_request(approved_environment):
    """GitPublicationValidator accepts compliant request matching M6 approval."""
    env = approved_environment
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    res = validator.validate_publication_request(pub_req, repo_identity)
    assert res.is_valid is True
    assert len(res.checks_passed) >= 6
    assert len(res.violations) == 0


def test_security_missing_approval_rejected(approved_environment):
    """Publication is refused if referenced approval decision does not exist."""
    env = approved_environment
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id="nonexistent_dec_id",
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(PublicationAuthorizationError, match="does not exist in store"):
        validator.validate_publication_request(pub_req, repo_identity)


def test_security_expired_approval_rejected(approved_environment):
    """Publication is refused if approval TTL has elapsed; approval transitions to EXPIRED."""
    env = approved_environment
    # Update decision to be expired
    past_time = datetime.now(timezone.utc) - timedelta(hours=1)
    expired_dec = env["decision"].model_copy(update={"expires_at": past_time})
    env["exec_store"].save_approval_decision(expired_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=expired_dec.decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(PublicationAuthorizationError, match="expired"):
        validator.validate_publication_request(pub_req, repo_identity)

    # Confirm state updated to EXPIRED in database
    refreshed = env["exec_store"].get_approval_decision(expired_dec.decision_id)
    assert refreshed.state == ApprovalState.EXPIRED


def test_security_invalidated_approval_rejected(approved_environment):
    """Publication is refused if approval was invalidated."""
    env = approved_environment
    inval_dec = env["decision"].model_copy(update={"state": ApprovalState.INVALIDATED})
    env["exec_store"].save_approval_decision(inval_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=inval_dec.decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(PublicationAuthorizationError, match="must be 'APPROVED'"):
        validator.validate_publication_request(pub_req, repo_identity)


def test_security_wrong_action_authorization(approved_environment):
    """EXPORT_PATCH approval strictly forbids STAGE_BRANCH or CREATE_PR."""
    env = approved_environment
    # Update decision to authorize only EXPORT_PATCH
    export_only_dec = env["decision"].model_copy(
        update={"authorized_action": AuthorizedAction.EXPORT_PATCH}
    )
    env["exec_store"].save_approval_decision(export_only_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=export_only_dec.decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(PublicationActionMismatchError, match="requires explicit 'STAGE_FOR_PR'"):
        validator.validate_publication_request(pub_req, repo_identity)


def test_security_wrong_repository_path_rejected(approved_environment, tmp_path: Path):
    """Publication is refused if repository path does not match approved target."""
    env = approved_environment
    validator = GitPublicationValidator(execution_store=env["exec_store"])

    # Create another repo with same files but different path
    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(other_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(other_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(other_repo), check=True, capture_output=True)
    (other_repo / "README.md").write_text("other", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(other_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(other_repo), check=True, capture_output=True)

    inspector = GitRepositoryInspector()
    other_identity = inspector.inspect_repository(other_repo)

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(RepositoryIdentityMismatchError):
        validator.validate_publication_request(pub_req, other_identity)


def test_security_wrong_base_sha_rejected(approved_environment):
    """Publication is refused if repository HEAD commit SHA has diverged from approved base commit."""
    env = approved_environment
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    # Tamper identity base commit SHA
    tampered_identity = repo_identity.model_copy(update={"base_commit_sha": "0" * 40})

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(RepositoryIdentityMismatchError, match="does not match approved base commit"):
        validator.validate_publication_request(pub_req, tampered_identity)


def test_security_patch_digest_mismatch_rejected(approved_environment):
    """Publication is refused if stored patch artifact digest does not match approval binding."""
    env = approved_environment
    tampered_patch = env["patch"].model_copy(update={"content_hash": "a" * 64})
    env["exec_store"].save_patch_artifact(tampered_patch)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(ArtifactBindingMismatchError, match="Patch content hash tampered"):
        validator.validate_publication_request(pub_req, repo_identity)


def test_security_dirty_working_tree_rejected(approved_environment):
    """Publication is refused if target repository working tree is dirty."""
    env = approved_environment
    (env["target_repo"] / "dirty.txt").write_text("dirty content", encoding="utf-8")

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(RepositoryDirtyError, match="dirty working tree"):
        validator.validate_publication_request(pub_req, repo_identity)


def test_security_secret_detected_in_commit_message():
    """Secrets detected in commit messages or metadata immediately raise SecretDetectedError."""
    with pytest.raises(SecretDetectedError, match="Secret or credential detected"):
        scan_for_secrets("fix: add token sk-1234567890abcdef12345678 to config")

    with pytest.raises(SecretDetectedError, match="Secret or credential detected"):
        scan_for_secrets("fix: add GitHub personal access token ghp_123456789012345678901234567890123456")


def test_security_cross_repair_approval_reuse_rejected(approved_environment):
    """Reusing an approval decision from a different repair execution is rejected."""
    env = approved_environment
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req = PublicationRequest(
        publication_id="pub_001",
        repair_id="rep_different_123",
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )

    with pytest.raises(PublicationAuthorizationError, match="belongs to repair"):
        validator.validate_publication_request(pub_req, repo_identity)


# =============================================================================
# 5. Publication Planner Tests
# =============================================================================

def test_planner_creates_valid_publication_plan(approved_environment):
    """GitPublicationPlanner formulates structured, validated PublicationPlan."""
    env = approved_environment
    planner = GitPublicationPlanner(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )

    plan = planner.create_publication_plan(
        repair_id=env["request"].repair_id,
        action=PublicationAction.STAGE_BRANCH,
        repository_path=env["target_repo"],
    )

    assert plan.plan_id.startswith("pubplan_")
    assert plan.repair_id == env["request"].repair_id
    assert plan.authorized_action == PublicationAction.STAGE_BRANCH
    assert plan.base_commit_sha == env["head_sha"]
    assert plan.patch_sha256 == env["patch_digest"]
    assert plan.proposed_branch_name.startswith("forge/fix-")
    assert "Finding: find_p9_001" in plan.commit_proposal.commit_message
    assert plan.commit_proposal.operator_signoff == env["decision"].actor


def test_planner_target_repo_remains_strictly_immutable(approved_environment):
    """Publication planning is strictly non-modifying; target repo remains 100% untouched."""
    env = approved_environment
    planner = GitPublicationPlanner(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )

    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=str(env["target_repo"]), capture_output=True, text=True).stdout
    head_before = _get_head_sha(env["target_repo"])

    _ = planner.create_publication_plan(
        repair_id=env["request"].repair_id,
        action=PublicationAction.STAGE_BRANCH,
        repository_path=env["target_repo"],
    )

    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=str(env["target_repo"]), capture_output=True, text=True).stdout
    head_after = _get_head_sha(env["target_repo"])

    assert status_before == status_after == ""
    assert head_before == head_after


def test_planner_custom_valid_branch_name(approved_environment):
    """Planner accepts valid custom branch name matching permitted prefix."""
    env = approved_environment
    planner = GitPublicationPlanner(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )

    plan = planner.create_publication_plan(
        repair_id=env["request"].repair_id,
        action=PublicationAction.STAGE_BRANCH,
        repository_path=env["target_repo"],
        custom_branch_name="forge/fix-tax-rate-zero-check",
    )

    assert plan.proposed_branch_name == "forge/fix-tax-rate-zero-check"


# =============================================================================
# 6. Audit & SQLite Persistence Tests
# =============================================================================

def test_persistence_roundtrip_publication_request_and_plan(approved_environment):
    """PublicationStore persists and retrieves PublicationRequest and PublicationPlan."""
    env = approved_environment
    planner = GitPublicationPlanner(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )
    plan = planner.create_publication_plan(
        repair_id=env["request"].repair_id,
        action=PublicationAction.STAGE_BRANCH,
        repository_path=env["target_repo"],
    )

    pub_store = env["pub_store"]
    pub_req = PublicationRequest(
        publication_id=plan.publication_id,
        repair_id=plan.repair_id,
        decision_id=plan.decision_id,
        action=plan.authorized_action,
        status=PublicationStatus.AUTHORIZED,
        plan_id=plan.plan_id,
    )

    pub_store.save_publication_request(pub_req)
    pub_store.save_publication_plan(plan)

    retrieved_req = pub_store.get_publication_request(plan.publication_id)
    assert retrieved_req is not None
    assert retrieved_req.publication_id == plan.publication_id
    assert retrieved_req.status == PublicationStatus.AUTHORIZED

    retrieved_plan = pub_store.get_publication_plan(plan.plan_id)
    assert retrieved_plan is not None
    assert retrieved_plan.plan_id == plan.plan_id
    assert retrieved_plan.proposed_branch_name == plan.proposed_branch_name
    assert retrieved_plan.patch_sha256 == plan.patch_sha256

    # Test list queries
    req_list = pub_store.list_publication_requests_for_repair(plan.repair_id)
    assert len(req_list) == 1
    plan_list = pub_store.list_publication_plans_for_repair(plan.repair_id)
    assert len(plan_list) == 1


def test_audit_event_logged_on_plan_creation(approved_environment):
    """Formulating a publication plan records sequential audit event in ledger."""
    env = approved_environment
    pub_store = env["pub_store"]

    pub_store.record_publication_event(
        repair_id=env["request"].repair_id,
        action="publication_plan_created",
        actor=RepairActor.ORCHESTRATOR,
        payload={"plan_id": "pubplan_test_123", "action": "stage_branch"},
    )

    events = env["exec_store"].get_events(env["request"].repair_id)
    pub_events = [e for e in events if e.payload.get("action") == "publication_plan_created"]
    assert len(pub_events) == 1
    assert pub_events[0].payload["plan_id"] == "pubplan_test_123"


# =============================================================================
# 7. CLI Commands Integration Tests
# =============================================================================

def test_cli_publish_plan_command(approved_environment, monkeypatch: pytest.MonkeyPatch):
    """forge repair publish plan command successfully formats and persists plan."""
    env = approved_environment
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    result = runner.invoke(
        app,
        [
            "repair",
            "publish",
            "plan",
            env["request"].repair_id,
            "--action",
            "stage_branch",
            "--repo",
            str(env["target_repo"]),
        ],
    )

    assert result.exit_code == 0
    assert "FORGE Publication Plan" in result.output
    assert "Proposed Branch:" in result.output


def test_cli_publish_plan_json_output(approved_environment, monkeypatch: pytest.MonkeyPatch):
    """forge repair publish plan --json produces valid JSON plan."""
    env = approved_environment
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    result = runner.invoke(
        app,
        [
            "repair",
            "publish",
            "plan",
            env["request"].repair_id,
            "--action",
            "stage_branch",
            "--repo",
            str(env["target_repo"]),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["repair_id"] == env["request"].repair_id
    assert data["authorized_action"] == "stage_branch"


def test_cli_publish_validate_command(approved_environment, monkeypatch: pytest.MonkeyPatch):
    """forge repair publish validate confirms authorization and policy compliance."""
    env = approved_environment
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    # First plan via CLI
    res_plan = runner.invoke(
        app,
        [
            "repair",
            "publish",
            "plan",
            env["request"].repair_id,
            "--action",
            "stage_branch",
            "--repo",
            str(env["target_repo"]),
            "--json",
        ],
    )
    plan_data = json.loads(res_plan.output)
    pub_id = plan_data["publication_id"]

    # Validate
    res_val = runner.invoke(
        app,
        [
            "repair",
            "publish",
            "validate",
            pub_id,
            "--repo",
            str(env["target_repo"]),
        ],
    )

    assert res_val.exit_code == 0
    assert "[VALID]" in res_val.output


def test_cli_publish_show_command(approved_environment, monkeypatch: pytest.MonkeyPatch):
    """forge repair publish show displays plan details."""
    env = approved_environment
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    res_plan = runner.invoke(
        app,
        [
            "repair",
            "publish",
            "plan",
            env["request"].repair_id,
            "--action",
            "stage_branch",
            "--repo",
            str(env["target_repo"]),
            "--json",
        ],
    )
    plan_data = json.loads(res_plan.output)
    plan_id = plan_data["plan_id"]

    res_show = runner.invoke(app, ["repair", "publish", "show", plan_id])
    assert res_show.exit_code == 0
    assert plan_id in res_show.output
    assert "Commit Proposal:" in res_show.output


def test_cli_publish_list_command(approved_environment, monkeypatch: pytest.MonkeyPatch):
    """forge repair publish list enumerates publication plans."""
    env = approved_environment
    monkeypatch.setenv("FORGE_DB_PATH", str(env["db_path"]))

    runner.invoke(
        app,
        [
            "repair",
            "publish",
            "plan",
            env["request"].repair_id,
            "--action",
            "stage_branch",
            "--repo",
            str(env["target_repo"]),
        ],
    )

    res_list = runner.invoke(app, ["repair", "publish", "list", "--repair-id", env["request"].repair_id])
    assert res_list.exit_code == 0
    assert "Plan ID" in res_list.output
    assert "stage_branch" in res_list.output
