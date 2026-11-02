"""Comprehensive unit and security test suite for Phase 9 Authorization Compatibility.

Focuses on:
- Phase 8 M6 vs Phase 9 M1 action model compatibility and semantic separation.
- Existing M6 approvals: EXPORT_PATCH, MANUAL_INTEGRATION, STAGE_FOR_PR.
- Ensuring MANUAL_INTEGRATION cannot authorize CREATE_PR or STAGE_BRANCH.
- Distinct semantics of STAGE_FOR_PR vs STAGE_BRANCH vs CREATE_PR.
- Default-deny behavior: STAGE_FOR_PR refuses CREATE_PR unless policy explicitly opts in.
- Policy opt-in: legacy_stage_for_pr_allows_create_pr.
- Action privilege escalation defenses (CLI aliases, string manipulation, DB tampering).
- Cross-repair approval replay defenses.
- Database backward compatibility and deserialization fidelity for historical rows.
"""

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.analysis.findings import Finding
from forge.cli import app
from forge.publication.authorization import (
    ACTION_AUTHORIZATION_MAP,
    get_permitted_publication_actions,
    is_action_authorized,
    validate_action_authorization,
)
from forge.publication.failures import (
    ArtifactBindingMismatchError,
    PublicationActionMismatchError,
    PublicationAuthorizationError,
)
from forge.publication.inspector import GitRepositoryInspector
from forge.publication.models import (
    PublicationAction,
    PublicationRequest,
    PublicationStatus,
    RepositoryIdentity,
)
from forge.publication.planner import GitPublicationPlanner
from forge.publication.policy import GitPublicationPolicy
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


@pytest.fixture
def test_env(tmp_path: Path):
    """Fixture initializing an approved repair environment."""
    test_db_path = tmp_path / "test_forge.db"
    init_database(test_db_path)

    forge_store = ForgeStore(db_path=test_db_path)
    exec_store = RepairExecutionStore(db_path=test_db_path)

    # 1. Target Git Repo
    mock_target_repo = tmp_path / "mock_repo"
    mock_target_repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(mock_target_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "FORGE Tester"], cwd=str(mock_target_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@forge.local"], cwd=str(mock_target_repo), check=True, capture_output=True)

    calc_file = mock_target_repo / "calc.py"
    calc_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=str(mock_target_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(mock_target_repo), check=True, capture_output=True)

    head_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(mock_target_repo), check=True, capture_output=True, text=True)
    head_sha = head_res.stdout.strip()

    # 2. Forge entities
    run_id = forge_store.create_run(target=str(mock_target_repo), run_id="run_auth_001")
    finding = Finding(
        finding_id="find_auth_001",
        title="Arithmetic Precision Bug",
        description="Addition overflow in edge cases",
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
    patch_diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n+    # verified fix\n     return a + b\n"
    import hashlib
    patch_digest = hashlib.sha256(patch_diff.encode("utf-8")).hexdigest()
    patch_id = f"patch_{patch_digest[:12]}"

    workspace = RepairWorkspace(
        target_repo_path=mock_target_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    workspace.prepare()

    repair_req = RepairRequest(
        repair_id="rep_auth_001",
        finding_id=finding.finding_id,
        run_id=run_id,
        target_repo=str(mock_target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_auth_001"],
        allowed_files=["calc.py"],
        workspace_id=workspace.workspace_id,
        state=RepairExecutionState.VERIFIED,
        patch_artifact_id=patch_id,
        created_at=now,
        updated_at=now,
    )
    exec_store.save_repair_request(repair_req)

    patch = PatchArtifact(
        artifact_id=patch_id,
        repair_id=repair_req.repair_id,
        finding_id=finding.finding_id,
        target_file="calc.py",
        affected_paths=["calc.py"],
        diff_content=patch_diff,
        content_hash=patch_digest,
        base_commit_sha=head_sha,
        lines_added=1,
        lines_deleted=0,
        hunks_count=1,
        created_at=now,
    )
    exec_store.save_patch_artifact(patch)

    val_res = IndependentValidationResult(
        validation_id="val_auth_001",
        repair_id=repair_req.repair_id,
        finding_id=finding.finding_id,
        repository_target=str(mock_target_repo),
        base_commit_sha=head_sha,
        workspace_id=workspace.workspace_id,
        patch_artifact_id=patch.artifact_id,
        patch_sha256=patch_digest,
        validated_file_paths=["calc.py"],
        validated_hunk_count=1,
        lines_added=1,
        lines_deleted=0,
        patch_bytes=len(patch_diff.encode("utf-8")),
        overall_status=ValidationOutcome.ACCEPTED,
        is_valid=True,
        created_at=now,
    )
    exec_store.save_validation_result(val_res)

    base_rec = TestExecutionRecord(
        execution_id="tex_base_001",
        verification_id="verif_auth_001",
        repair_id=repair_req.repair_id,
        workspace_id=workspace.workspace_id,
        target_type="baseline",
        test_name="test_calc.py::test_add",
        command=["pytest", "tests/test_calc.py"],
        exit_code=1,
        passed=False,
        output="AssertionError",
        duration_ms=100.0,
    )
    patch_rec = TestExecutionRecord(
        execution_id="tex_patch_001",
        verification_id="verif_auth_001",
        repair_id=repair_req.repair_id,
        workspace_id=workspace.workspace_id,
        target_type="patched",
        test_name="test_calc.py::test_add",
        command=["pytest", "tests/test_calc.py"],
        exit_code=0,
        passed=True,
        output="PASSED",
        duration_ms=100.0,
    )
    reg_analysis = RegressionAnalysisRecord(
        regression_detected=False,
        defect_resolved=True,
        comparisons=[
            TestComparisonResult(
                test_name="test_calc.py::test_add",
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
        plan_id="vplan_auth_001",
        repair_id=repair_req.repair_id,
        patch_artifact_id=patch.artifact_id,
        selected_tests=[
            SelectedTest(
                test_id="t_auth_01",
                test_name="test_calc.py::test_add",
                category=TestSelectionCategory.REPRODUCTION,
                command=["pytest", "tests/test_calc.py"],
            )
        ],
    )
    verif_run = VerificationRunRecord(
        verification_id="verif_auth_001",
        repair_id=repair_req.repair_id,
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

    dossier_builder = ReviewDossierBuilder(execution_store=exec_store, forge_store=forge_store)
    dossier = dossier_builder.build_dossier(repair_req.repair_id)

    approval_mgr = ApprovalManager(execution_store=exec_store, forge_store=forge_store)
    decision = approval_mgr.submit_decision(
        repair_id=repair_req.repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="lead_security@forge.local",
        reason="Verified defect fix with zero regressions.",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
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
# 1. Authorization Mapping Unit Tests
# =============================================================================

def test_action_authorization_matrix_completeness():
    """Every AuthorizedAction enum member is present in ACTION_AUTHORIZATION_MAP."""
    for action in AuthorizedAction:
        assert action in ACTION_AUTHORIZATION_MAP, f"Missing {action} in matrix"
        permitted = get_permitted_publication_actions(action)
        assert isinstance(permitted, set)
        assert len(permitted) >= 1


def test_export_patch_strictly_authorizes_export_patch_only():
    """EXPORT_PATCH authorizes ONLY PublicationAction.EXPORT_PATCH."""
    assert is_action_authorized(AuthorizedAction.EXPORT_PATCH, PublicationAction.EXPORT_PATCH)
    assert not is_action_authorized(AuthorizedAction.EXPORT_PATCH, PublicationAction.STAGE_BRANCH)
    assert not is_action_authorized(AuthorizedAction.EXPORT_PATCH, PublicationAction.CREATE_PR)


def test_manual_integration_strictly_authorizes_export_patch_only():
    """MANUAL_INTEGRATION authorizes ONLY PublicationAction.EXPORT_PATCH."""
    assert is_action_authorized(AuthorizedAction.MANUAL_INTEGRATION, PublicationAction.EXPORT_PATCH)
    assert not is_action_authorized(AuthorizedAction.MANUAL_INTEGRATION, PublicationAction.STAGE_BRANCH)
    assert not is_action_authorized(AuthorizedAction.MANUAL_INTEGRATION, PublicationAction.CREATE_PR)


def test_legacy_stage_for_pr_authorizes_stage_branch_and_export_patch():
    """STAGE_FOR_PR authorizes STAGE_BRANCH and EXPORT_PATCH, but denies CREATE_PR by default."""
    assert is_action_authorized(AuthorizedAction.STAGE_FOR_PR, PublicationAction.EXPORT_PATCH)
    assert is_action_authorized(AuthorizedAction.STAGE_FOR_PR, PublicationAction.STAGE_BRANCH)
    # Default-deny: denies CREATE_PR
    assert not is_action_authorized(AuthorizedAction.STAGE_FOR_PR, PublicationAction.CREATE_PR)


def test_legacy_stage_for_pr_with_policy_opt_in():
    """STAGE_FOR_PR authorizes CREATE_PR only when policy explicitly opts in."""
    policy_default = GitPublicationPolicy(legacy_stage_for_pr_allows_create_pr=False)
    assert not is_action_authorized(AuthorizedAction.STAGE_FOR_PR, PublicationAction.CREATE_PR, policy_default)

    policy_opt_in = GitPublicationPolicy(legacy_stage_for_pr_allows_create_pr=True)
    assert is_action_authorized(AuthorizedAction.STAGE_FOR_PR, PublicationAction.CREATE_PR, policy_opt_in)


def test_stage_branch_strictly_authorizes_stage_branch_and_export_patch():
    """STAGE_BRANCH authorizes STAGE_BRANCH and EXPORT_PATCH, but denies CREATE_PR."""
    assert is_action_authorized(AuthorizedAction.STAGE_BRANCH, PublicationAction.EXPORT_PATCH)
    assert is_action_authorized(AuthorizedAction.STAGE_BRANCH, PublicationAction.STAGE_BRANCH)
    assert not is_action_authorized(AuthorizedAction.STAGE_BRANCH, PublicationAction.CREATE_PR)


def test_create_pr_authorizes_all_downstream_actions():
    """CREATE_PR authorizes CREATE_PR, STAGE_BRANCH, and EXPORT_PATCH."""
    assert is_action_authorized(AuthorizedAction.CREATE_PR, PublicationAction.EXPORT_PATCH)
    assert is_action_authorized(AuthorizedAction.CREATE_PR, PublicationAction.STAGE_BRANCH)
    assert is_action_authorized(AuthorizedAction.CREATE_PR, PublicationAction.CREATE_PR)


# =============================================================================
# 2. Existing M6 Approvals & Backward Compatibility
# =============================================================================

def test_existing_m6_export_patch_approval_validation(test_env):
    """Existing M6 approval with EXPORT_PATCH allows EXPORT_PATCH and rejects STAGE_BRANCH / CREATE_PR."""
    env = test_env
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    # 1. EXPORT_PATCH succeeds
    pub_req_export = PublicationRequest(
        publication_id="pub_exp_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.EXPORT_PATCH,
    )
    res = validator.validate_publication_request(pub_req_export, repo_identity)
    assert res.is_valid is True

    # 2. STAGE_BRANCH rejected
    pub_req_stage = PublicationRequest(
        publication_id="pub_stg_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )
    with pytest.raises(PublicationActionMismatchError, match="requires explicit 'STAGE_FOR_PR' or 'STAGE_BRANCH'"):
        validator.validate_publication_request(pub_req_stage, repo_identity)

    # 3. CREATE_PR rejected
    pub_req_pr = PublicationRequest(
        publication_id="pub_pr_001",
        repair_id=env["request"].repair_id,
        decision_id=env["decision"].decision_id,
        action=PublicationAction.CREATE_PR,
    )
    with pytest.raises(PublicationActionMismatchError, match="requires explicit pull request creation authorization"):
        validator.validate_publication_request(pub_req_pr, repo_identity)


def test_existing_m6_manual_integration_approval_validation(test_env):
    """Existing M6 approval with MANUAL_INTEGRATION allows EXPORT_PATCH and rejects CREATE_PR."""
    env = test_env
    # Update decision to MANUAL_INTEGRATION
    mi_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.MANUAL_INTEGRATION})
    env["exec_store"].save_approval_decision(mi_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    # EXPORT_PATCH succeeds
    pub_req_export = PublicationRequest(
        publication_id="pub_exp_mi",
        repair_id=env["request"].repair_id,
        decision_id=mi_dec.decision_id,
        action=PublicationAction.EXPORT_PATCH,
    )
    res = validator.validate_publication_request(pub_req_export, repo_identity)
    assert res.is_valid is True

    # CREATE_PR strictly rejected
    pub_req_pr = PublicationRequest(
        publication_id="pub_pr_mi",
        repair_id=env["request"].repair_id,
        decision_id=mi_dec.decision_id,
        action=PublicationAction.CREATE_PR,
    )
    with pytest.raises(PublicationActionMismatchError, match="cannot be authorized by 'manual_integration'"):
        validator.validate_publication_request(pub_req_pr, repo_identity)

    # STAGE_BRANCH strictly rejected
    pub_req_stage = PublicationRequest(
        publication_id="pub_stg_mi",
        repair_id=env["request"].repair_id,
        decision_id=mi_dec.decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )
    with pytest.raises(PublicationActionMismatchError, match="requires explicit 'STAGE_FOR_PR' or 'STAGE_BRANCH'"):
        validator.validate_publication_request(pub_req_stage, repo_identity)


def test_existing_m6_stage_for_pr_approval_validation(test_env):
    """Existing M6 approval with STAGE_FOR_PR allows STAGE_BRANCH, denies CREATE_PR by default."""
    env = test_env
    sfp_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.STAGE_FOR_PR})
    env["exec_store"].save_approval_decision(sfp_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    # STAGE_BRANCH succeeds
    pub_req_stage = PublicationRequest(
        publication_id="pub_stg_sfp",
        repair_id=env["request"].repair_id,
        decision_id=sfp_dec.decision_id,
        action=PublicationAction.STAGE_BRANCH,
    )
    res = validator.validate_publication_request(pub_req_stage, repo_identity)
    assert res.is_valid is True

    # EXPORT_PATCH succeeds
    pub_req_exp = PublicationRequest(
        publication_id="pub_exp_sfp",
        repair_id=env["request"].repair_id,
        decision_id=sfp_dec.decision_id,
        action=PublicationAction.EXPORT_PATCH,
    )
    res_exp = validator.validate_publication_request(pub_req_exp, repo_identity)
    assert res_exp.is_valid is True

    # CREATE_PR denied by default
    pub_req_pr = PublicationRequest(
        publication_id="pub_pr_sfp",
        repair_id=env["request"].repair_id,
        decision_id=sfp_dec.decision_id,
        action=PublicationAction.CREATE_PR,
    )
    with pytest.raises(PublicationActionMismatchError, match="requires explicit 'create_pr' operator authorization; decision has 'stage_for_pr'"):
        validator.validate_publication_request(pub_req_pr, repo_identity)


def test_stage_for_pr_permits_create_pr_with_policy_opt_in(test_env):
    """When policy opt-in legacy_stage_for_pr_allows_create_pr=True, STAGE_FOR_PR permits CREATE_PR."""
    env = test_env
    sfp_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.STAGE_FOR_PR})
    env["exec_store"].save_approval_decision(sfp_dec)

    policy = GitPublicationPolicy(legacy_stage_for_pr_allows_create_pr=True)
    validator = GitPublicationValidator(execution_store=env["exec_store"], policy=policy)
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req_pr = PublicationRequest(
        publication_id="pub_pr_sfp_optin",
        repair_id=env["request"].repair_id,
        decision_id=sfp_dec.decision_id,
        action=PublicationAction.CREATE_PR,
    )
    res = validator.validate_publication_request(pub_req_pr, repo_identity)
    assert res.is_valid is True


# =============================================================================
# 3. Phase 9 STAGE_BRANCH vs CREATE_PR Distinction
# =============================================================================

def test_phase9_stage_branch_cannot_authorize_create_pr(test_env):
    """Phase 9 STAGE_BRANCH authorization strictly forbids CREATE_PR."""
    env = test_env
    stg_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.STAGE_BRANCH})
    env["exec_store"].save_approval_decision(stg_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    pub_req_pr = PublicationRequest(
        publication_id="pub_pr_stg",
        repair_id=env["request"].repair_id,
        decision_id=stg_dec.decision_id,
        action=PublicationAction.CREATE_PR,
    )
    with pytest.raises(PublicationActionMismatchError, match="requires explicit 'create_pr' operator authorization; decision has 'stage_branch'"):
        validator.validate_publication_request(pub_req_pr, repo_identity)


def test_phase9_create_pr_authorizes_all_actions(test_env):
    """Phase 9 CREATE_PR authorization permits CREATE_PR, STAGE_BRANCH, and EXPORT_PATCH."""
    env = test_env
    pr_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.CREATE_PR})
    env["exec_store"].save_approval_decision(pr_dec)

    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    for action in (PublicationAction.CREATE_PR, PublicationAction.STAGE_BRANCH, PublicationAction.EXPORT_PATCH):
        pub_req = PublicationRequest(
            publication_id=f"pub_{action.value}",
            repair_id=env["request"].repair_id,
            decision_id=pr_dec.decision_id,
            action=action,
        )
        res = validator.validate_publication_request(pub_req, repo_identity)
        assert res.is_valid is True


# =============================================================================
# 4. Anti-Escalation & Security Defenses
# =============================================================================

def test_cli_rejects_unauthorized_action_aliases():
    """CLI rejects invalid or shortcut aliases for publication actions."""
    # Attempting to pass 'pr' instead of 'create_pr'
    res = runner.invoke(app, ["repair", "publish", "plan", "rep_001", "--action", "pr"])
    assert res.exit_code != 0
    assert "Invalid publication action 'pr'" in res.output or "Permitted actions" in res.output

    # Attempting to pass 'stage' instead of 'stage_branch'
    res = runner.invoke(app, ["repair", "publish", "plan", "rep_001", "--action", "stage"])
    assert res.exit_code != 0
    assert "Invalid publication action 'stage'" in res.output or "Permitted actions" in res.output

    # Attempting to pass 'manual' instead of 'export_patch'
    res = runner.invoke(app, ["repair", "publish", "plan", "rep_001", "--action", "manual"])
    assert res.exit_code != 0


def test_database_tampering_of_publication_request_detected(test_env):
    """If a publication request is tampered in SQLite to 'create_pr', validator re-evaluates against the true approval decision and fails."""
    env = test_env
    # Decision is EXPORT_PATCH
    export_dec = env["decision"].model_copy(update={"authorized_action": AuthorizedAction.EXPORT_PATCH})
    env["exec_store"].save_approval_decision(export_dec)

    pub_store = env["pub_store"]
    # Create request with export_patch legitimately
    req = PublicationRequest(
        publication_id="pub_tamper_001",
        repair_id=env["request"].repair_id,
        decision_id=export_dec.decision_id,
        action=PublicationAction.EXPORT_PATCH,
    )
    pub_store.save_publication_request(req)

    # Attacker directly updates the SQLite row action to 'create_pr'
    conn = sqlite3.connect(env["db_path"])
    conn.execute("UPDATE publication_requests SET action = 'create_pr' WHERE publication_id = 'pub_tamper_001'")
    conn.commit()
    conn.close()

    # Re-fetch from store
    tampered_req = pub_store.get_publication_request("pub_tamper_001")
    assert tampered_req.action == PublicationAction.CREATE_PR

    # Validator checks against the authentic approval record and rejects
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    with pytest.raises(PublicationActionMismatchError):
        validator.validate_publication_request(tampered_req, repo_identity)


def test_approval_replay_across_repairs_rejected(test_env):
    """An operator approval for repair_1 cannot be replayed to authorize repair_2."""
    env = test_env
    validator = GitPublicationValidator(execution_store=env["exec_store"])
    inspector = GitRepositoryInspector()
    repo_identity = inspector.inspect_repository(env["target_repo"])

    # Create publication request claiming repair_2 with repair_1's approval
    replay_req = PublicationRequest(
        publication_id="pub_replay_001",
        repair_id="rep_auth_different_999",
        decision_id=env["decision"].decision_id,
        action=PublicationAction.EXPORT_PATCH,
    )

    with pytest.raises(PublicationAuthorizationError, match="belongs to repair"):
        validator.validate_publication_request(replay_req, repo_identity)


def test_backward_compatibility_db_deserialization_all_m6_actions(tmp_path: Path):
    """Historical SQLite records containing M6 authorized actions deserialize cleanly."""
    db_path = tmp_path / "compat.db"
    init_database(db_path)
    forge_store = ForgeStore(db_path=db_path)
    run_id = forge_store.create_run(target="/path/to/repo", run_id="run_hist")
    forge_store.create_finding(
        run_id=run_id,
        title="Bug",
        description="Bug",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        finding_id="find_001",
    )
    exec_store = RepairExecutionStore(db_path=db_path)

    m6_actions = ["export_patch", "stage_for_pr", "manual_integration"]

    for idx, act in enumerate(m6_actions):
        repair_id = f"rep_hist_{idx}"
        req = RepairRequest(
            repair_id=repair_id,
            finding_id="find_001",
            run_id="run_hist",
            target_repo="/path/to/repo",
            base_commit_sha="a" * 40,
            evidence_ids=["evi_001"],
            allowed_files=["file.py"],
            workspace_id="ws_001",
            state=RepairExecutionState.VERIFIED,
            patch_artifact_id="patch_001",
        )
        exec_store.save_repair_request(req)

        binding = ApprovalBinding(
            repair_id=repair_id,
            finding_id="find_001",
            repository_target="/path/to/repo",
            base_commit_sha="a" * 40,
            patch_artifact_id="patch_001",
            patch_sha256="b" * 64,
            validation_id="val_001",
            verification_id="ver_001",
            dossier_id="dos_001",
        )
        dec = ApprovalDecisionRecord(
            decision_id=f"dec_hist_{idx}",
            repair_id=f"rep_hist_{idx}",
            binding=binding,
            state=ApprovalState.APPROVED,
            decision=OperatorDecisionType.APPROVE,
            authorized_action=AuthorizedAction(act),
            actor="historical_operator@forge.local",
            reason="Historical approval",
            created_at=datetime.now(timezone.utc),
        )
        exec_store.save_approval_decision(dec)

    # Retrieve and verify deserialization
    for idx, act in enumerate(m6_actions):
        retrieved = exec_store.get_approval_decision(f"dec_hist_{idx}")
        assert retrieved is not None
        assert retrieved.authorized_action == AuthorizedAction(act)
        assert retrieved.authorized_action.value == act
