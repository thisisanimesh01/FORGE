"""Phase 8 Milestone 6 — Operator Review, Approval & Authorization Workflows Test Suite.

Verifies Review Dossier assembly, artifact-bound approvals, invalidation on tampering,
operator decisions (APPROVE, REJECT, REQUEST_CHANGES, WITHDRAW), TTL expiration,
security replay defenses, SQLite persistence, and CLI integration.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from typing import Any, Dict, List
import unittest.mock as mock
from unittest.mock import patch
import uuid

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.repair.execution.approval import (
    ALLOWED_APPROVAL_TRANSITIONS,
    ApprovalManager,
    ApprovalStateMachine,
)
from forge.repair.execution.coordinator import RepairExecutionCoordinator
from forge.repair.execution.dossier import ReviewDossierBuilder
from forge.repair.execution.failures import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalInvalidatedError,
    ApprovalPreconditionError,
    ArtifactBindingMismatchError,
    DuplicateDecisionError,
    InvalidApprovalTransitionError,
    UnauthorizedOperatorError,
)
from forge.repair.execution.models import (
    ApprovalBinding,
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    ComparisonStatus,
    FailureCategory,
    FailureClassification,
    IndependentValidationResult,
    OperatorDecisionType,
    PatchArtifact,
    RegressionAnalysisRecord,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairExecutionRecord,
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
from forge.repair.execution.workspace import RepairWorkspace
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore

runner = CliRunner()


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    """Provide isolated SQLite database path."""
    return tmp_path / "test_m6.db"


@pytest.fixture
def mock_target_repo(tmp_path: Path) -> Path:
    """Create a mock target git repository with a source module and test suite."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()

    calc_code = (
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    if rate > 1.0:\n"
        "        raise ValueError('Rate too high')\n"
        "    return price * (1.0 - rate)\n"
    )
    (src_dir / "calculator.py").write_text(calc_code, encoding="utf-8")

    test_code = (
        "from src.calculator import calculate_discount\n"
        "import pytest\n\n"
        "def test_normal_discount():\n"
        "    assert calculate_discount(100.0, 0.2) == 80.0\n\n"
        "def test_negative_rate_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        calculate_discount(100.0, -0.5)\n"
    )
    (tests_dir / "test_calculator.py").write_text(test_code, encoding="utf-8")
    (repo_dir / "README.md").write_text("# Target Repo M6\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    return repo_dir


def _get_head(repo_dir: Path) -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    return res.stdout.strip()


@pytest.fixture
def verified_environment(test_db_path: Path, mock_target_repo: Path, tmp_path: Path):
    """Seed ForgeStore and RepairExecutionStore with a fully VERIFIED repair execution."""
    forge_store = ForgeStore(db_path=test_db_path)
    exec_store = RepairExecutionStore(db_path=test_db_path)
    head_sha = _get_head(mock_target_repo)

    run_id = forge_store.create_run(target=str(mock_target_repo), run_id="run_m6_seed")

    finding = forge_store.create_finding(
        run_id=run_id,
        title="Negative discount rate allowed",
        description="calculate_discount does not check that rate >= 0.0",
        severity=FindingSeverity.HIGH,
        finding_id="find_m6_001",
        affected_files=["src/calculator.py"],
    )

    ev_repro = EvidenceRecord(
        evidence_id="evi_m6_repro",
        run_id=run_id,
        finding_id="find_m6_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Reproduction result showing failure on negative discount rate",
        content="pytest tests/test_calculator.py::test_negative_rate_raises failed",
        file_path="src/calculator.py",
        details={
            "status": "reproduced",
            "commit_sha": head_sha,
            "reproduction_command": "pytest -v tests/test_calculator.py",
        },
    )
    forge_store.attach_evidence(ev_repro)
    forge_store.transition_finding("find_m6_001", FindingLifecycleState.REPRODUCED, "evi_m6_repro")

    workspace = RepairWorkspace(
        target_repo_path=mock_target_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    workspace.prepare()

    repair_req = RepairRequest(
        repair_id="rep_m6_001",
        finding_id="find_m6_001",
        run_id=run_id,
        target_repo=str(mock_target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_m6_repro"],
        allowed_files=["src/calculator.py"],
        workspace_id=workspace.workspace_id,
        state=RepairExecutionState.VERIFIED,
    )
    exec_store.save_repair_request(repair_req)

    clean_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    import hashlib
    patch_digest = hashlib.sha256(clean_diff.encode("utf-8")).hexdigest()
    patch_id = f"patch_{patch_digest[:12]}"

    patch = PatchArtifact(
        artifact_id=patch_id,
        repair_id="rep_m6_001",
        finding_id="find_m6_001",
        target_file="src/calculator.py",
        affected_paths=["src/calculator.py"],
        diff_content=clean_diff,
        content_hash=patch_digest,
        base_commit_sha=head_sha,
        lines_added=1,
        lines_deleted=1,
        hunks_count=1,
    )
    exec_store.save_patch_artifact(patch)
    repair_req = repair_req.model_copy(update={"patch_artifact_id": patch.artifact_id})
    exec_store.save_repair_request(repair_req)

    now = datetime.now(timezone.utc)
    validation_res = IndependentValidationResult(
        validation_id="val_indep_m6_001",
        repair_id="rep_m6_001",
        patch_artifact_id=patch.artifact_id,
        finding_id="find_m6_001",
        repository_target=str(mock_target_repo),
        base_commit_sha=head_sha,
        workspace_id=workspace.workspace_id,
        patch_sha256=patch_digest,
        is_valid=True,
        overall_status=ValidationOutcome.ACCEPTED,
        checks_performed=["digest_integrity", "scope_containment", "context_match", "python_ast"],
        scope_result={"allowed_files": ["src/calculator.py"], "valid": True},
        integrity_result={"digest_valid": True, "artifact_id_valid": True},
        semantic_result={"ast_valid": True, "guards_preserved": True},
        summary="Independent validation passed: patch satisfies all integrity, scope, and semantic invariants",
        validated_at=now,
    )
    exec_store.save_validation_result(validation_res)

    base_rec = TestExecutionRecord(
        execution_id="e_base_01",
        verification_id="verif_m6_001",
        repair_id="rep_m6_001",
        workspace_id="ws_base",
        target_type="baseline",
        test_name="evidence_reproduction_test",
        command=["pytest", "-v", "tests/test_calculator.py"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=2,
        tests_executed=2,
        tests_passed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = TestExecutionRecord(
        execution_id="e_patch_01",
        verification_id="verif_m6_001",
        repair_id="rep_m6_001",
        workspace_id=workspace.workspace_id,
        target_type="patched",
        test_name="evidence_reproduction_test",
        command=["pytest", "-v", "tests/test_calculator.py"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=2,
        tests_executed=2,
        tests_passed=2,
        started_at=now,
        completed_at=now,
    )
    reg_analysis = RegressionAnalysisRecord(
        regression_detected=False,
        defect_resolved=True,
        comparisons=[
            TestComparisonResult(
                test_name="evidence_reproduction_test",
                status=ComparisonStatus.FIXED,
                baseline_passed=False,
                patched_passed=True,
                details="Defect resolved",
            )
        ],
        new_failures_count=0,
        fixed_tests_count=1,
        stable_tests_count=1,
        summary="REPAIR VERIFIED: defect resolved (1 fixed test(s), 1 stable passes, 0 regressions)",
    )
    verif_plan = VerificationPlan(
        plan_id="vplan_m6_001",
        repair_id="rep_m6_001",
        patch_artifact_id=patch.artifact_id,
        selected_tests=[
            SelectedTest(
                test_id="t_01",
                test_name="evidence_reproduction_test",
                category=TestSelectionCategory.REPRODUCTION,
                command=["pytest", "-v", "tests/test_calculator.py"],
            )
        ],
    )
    verif_run = VerificationRunRecord(
        verification_id="verif_m6_001",
        repair_id="rep_m6_001",
        patch_artifact_id=patch.artifact_id,
        validation_id=validation_res.validation_id,
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

    return {
        "forge_store": forge_store,
        "exec_store": exec_store,
        "request": repair_req,
        "patch": patch,
        "validation_result": validation_res,
        "verification_run": verif_run,
        "finding": finding,
        "workspace": workspace,
        "target_repo": mock_target_repo,
        "head_sha": head_sha,
        "patch_digest": patch_digest,
    }


# =============================================================================
# 1. Review Dossier Builder Tests
# =============================================================================

def test_dossier_builder_compiles_complete_factual_dossier(verified_environment):
    """ReviewDossierBuilder compiles facts strictly from stored audit records."""
    env = verified_environment
    builder = ReviewDossierBuilder(execution_store=env["exec_store"], forge_store=env["forge_store"])

    dossier = builder.build_dossier(repair_id=env["request"].repair_id)

    assert dossier.repair_id == env["request"].repair_id
    assert dossier.finding_id == env["finding"].finding_id
    assert dossier.finding_title == env["finding"].title
    assert dossier.patch_artifact_id == env["patch"].artifact_id
    assert dossier.patch_sha256 == env["patch_digest"]
    assert dossier.validation_outcome == "ACCEPTED"
    assert dossier.verification_outcome == "VERIFIED"
    assert dossier.defect_resolved is True
    assert dossier.regression_detected is False
    assert dossier.recommended_decision == "APPROVE"
    assert len(dossier.facts) >= 6
    assert any("Pinned base commit" in f for f in dossier.facts)


def test_dossier_builder_recommends_reject_on_regression(verified_environment):
    """ReviewDossierBuilder recommends REJECT if regressions are present in verification."""
    env = verified_environment
    reg_verif = env["verification_run"].model_copy(
        update={
            "overall_outcome": VerificationOutcome.FAILED,
            "regression_analysis": env["verification_run"].regression_analysis.model_copy(
                update={"regression_detected": True, "new_failures_count": 1}
            ),
        }
    )
    env["exec_store"].save_verification_run(reg_verif)

    builder = ReviewDossierBuilder(execution_store=env["exec_store"], forge_store=env["forge_store"])
    dossier = builder.build_dossier(repair_id=env["request"].repair_id)

    assert dossier.recommended_decision == "REJECT"
    assert dossier.regression_detected is True


def test_dossier_builder_recommends_reject_on_validation_failure(verified_environment):
    """ReviewDossierBuilder recommends REJECT if M4 validation failed."""
    env = verified_environment
    rej_val = env["validation_result"].model_copy(update={"overall_status": ValidationOutcome.REJECTED})
    env["exec_store"].save_validation_result(rej_val)

    builder = ReviewDossierBuilder(execution_store=env["exec_store"], forge_store=env["forge_store"])
    dossier = builder.build_dossier(repair_id=env["request"].repair_id)

    assert dossier.recommended_decision == "REJECT"
    assert dossier.validation_outcome == "REJECTED"


def test_dossier_render_markdown_contains_required_sections(verified_environment):
    """ReviewDossierBuilder.render_markdown produces structured Markdown with diff and facts."""
    env = verified_environment
    builder = ReviewDossierBuilder(execution_store=env["exec_store"], forge_store=env["forge_store"])
    dossier = builder.build_dossier(repair_id=env["request"].repair_id)

    md = ReviewDossierBuilder.render_markdown(dossier)
    assert "# FORGE Repair Review Dossier" in md
    assert "Unified Diff" in md
    assert "```diff" in md
    assert "Verified Facts (Traceable to Storage)" in md
    assert "Recommended Decision:" in md


# =============================================================================
# 2. Approval Preconditions & State Machine Tests
# =============================================================================

def test_approval_manager_submits_clean_approval(verified_environment):
    """ApprovalManager successfully records explicit operator approval with artifact binding."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Verified fix for negative rate handling with zero regressions.",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    assert decision.state == ApprovalState.APPROVED
    assert decision.decision == OperatorDecisionType.APPROVE
    assert decision.authorized_action == AuthorizedAction.EXPORT_PATCH
    assert decision.actor == "operator@forge.local"
    assert decision.binding.patch_sha256 == env["patch_digest"]
    assert decision.binding.base_commit_sha == env["head_sha"]


def test_approval_rejected_if_m4_validation_missing(verified_environment):
    """Approval is strictly refused if M4 independent validation is missing."""
    env = verified_environment
    # Save a repair without validation
    conn = env["exec_store"]._conn()
    conn.execute("DELETE FROM repair_validation_results WHERE repair_id = ?", (env["request"].repair_id,))
    conn.commit()
    conn.close()

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ApprovalPreconditionError, match="validation result is missing"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_m4_validation_not_accepted(verified_environment):
    """Approval is strictly refused if M4 validation status is not ACCEPTED."""
    env = verified_environment
    rej_val = env["validation_result"].model_copy(update={"overall_status": ValidationOutcome.REJECTED})
    env["exec_store"].save_validation_result(rej_val)

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ApprovalPreconditionError, match="not 'ACCEPTED'"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_m5_verification_missing(verified_environment):
    """Approval is strictly refused if M5 verification run is missing."""
    env = verified_environment
    conn = env["exec_store"]._conn()
    conn.execute("DELETE FROM repair_test_executions WHERE repair_id = ?", (env["request"].repair_id,))
    conn.execute("DELETE FROM repair_verification_runs WHERE repair_id = ?", (env["request"].repair_id,))
    conn.commit()
    conn.close()

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ApprovalPreconditionError, match="verification run is missing"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_m5_verification_not_verified(verified_environment):
    """Approval is strictly refused if M5 verification outcome is FAILED."""
    env = verified_environment
    fail_verif = env["verification_run"].model_copy(update={"overall_outcome": VerificationOutcome.FAILED})
    env["exec_store"].save_verification_run(fail_verif)

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ApprovalPreconditionError, match="not 'VERIFIED'"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_regressions_detected(verified_environment):
    """Approval is strictly refused if verification recorded regressions."""
    env = verified_environment
    reg_verif = env["verification_run"].model_copy(
        update={
            "regression_analysis": env["verification_run"].regression_analysis.model_copy(
                update={"regression_detected": True}
            )
        }
    )
    env["exec_store"].save_verification_run(reg_verif)

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ApprovalPreconditionError, match="detected regressions"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_patch_digest_tampered(verified_environment):
    """Approval is strictly refused if validation or verification digest does not match patch."""
    env = verified_environment
    tampered_val = env["validation_result"].model_copy(update={"patch_sha256": "00000000000000000000000000000000"})
    env["exec_store"].save_validation_result(tampered_val)

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ArtifactBindingMismatchError, match="content hash mismatch"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_base_commit_mismatched(verified_environment):
    """Approval is strictly refused if request and patch base commit SHA do not match."""
    env = verified_environment
    tampered_req = env["request"].model_copy(update={"base_commit_sha": "a" * 40})
    env["exec_store"].save_repair_request(tampered_req)

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(ArtifactBindingMismatchError, match="Base commit mismatch"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="operator@forge.local",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


def test_approval_rejected_if_empty_operator_identity(verified_environment):
    """Approval is strictly refused if operator actor identity is empty or whitespace."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with pytest.raises(UnauthorizedOperatorError, match="Operator identity cannot be empty"):
        mgr.submit_decision(
            repair_id=env["request"].repair_id,
            decision=OperatorDecisionType.APPROVE,
            actor="   ",
            reason="Approved",
            authorized_action=AuthorizedAction.EXPORT_PATCH,
        )


# =============================================================================
# 3. Operator Decisions (Reject, Request Changes, Withdraw, Expire)
# =============================================================================

def test_operator_reject_decision(verified_environment):
    """Operator can explicitly reject a repair, recording immutable rejection history."""
    env = verified_environment
    # Set request to resumable state (PATCH_VALIDATING) so state machine legally transitions to REJECTED
    req = env["request"].model_copy(update={"state": RepairExecutionState.PATCH_VALIDATING})
    env["exec_store"].save_repair_request(req)

    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.REJECT,
        actor="operator@forge.local",
        reason="Patch changes logic in an undesirable way.",
    )

    assert decision.state == ApprovalState.REJECTED
    assert decision.decision == OperatorDecisionType.REJECT
    assert decision.reason == "Patch changes logic in an undesirable way."

    # Check that repair request is also transitioned to REJECTED
    saved_req = env["exec_store"].get_repair_request(env["request"].repair_id)
    assert saved_req.state == RepairExecutionState.REJECTED


def test_operator_reject_decision_on_verified_request(verified_environment):
    """Operator can reject an already VERIFIED repair; decision records REJECTED while request preserves terminal state."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.REJECT,
        actor="operator@forge.local",
        reason="Verified fix introduces undesirable architectural side effects.",
    )

    assert decision.state == ApprovalState.REJECTED
    assert decision.decision == OperatorDecisionType.REJECT

    # Approval decision is recorded as REJECTED in approval history
    latest = env["exec_store"].get_latest_approval_decision_for_repair(env["request"].repair_id)
    assert latest is not None
    assert latest.state == ApprovalState.REJECTED

    # Terminal state of VERIFIED request remains immutable per M1 invariant
    saved_req = env["exec_store"].get_repair_request(env["request"].repair_id)
    assert saved_req.state == RepairExecutionState.VERIFIED


def test_operator_request_changes_decision(verified_environment):
    """Operator can request changes with actionable feedback."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.REQUEST_CHANGES,
        actor="operator@forge.local",
        reason="Need clearer exception message.",
        feedback="Change 'Rate too high' to include the invalid rate value.",
    )

    assert decision.state == ApprovalState.CHANGES_REQUESTED
    assert decision.decision == OperatorDecisionType.REQUEST_CHANGES
    assert decision.feedback == "Change 'Rate too high' to include the invalid rate value."


def test_operator_withdraw_pending_decision(verified_environment):
    """Operator can withdraw an approval decision."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    # First approve
    mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Looks good.",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    # Then withdraw
    withdrawn = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.WITHDRAW,
        actor="operator@forge.local",
        reason="Withdrawing approval due to upstream priority change.",
    )

    assert withdrawn.state == ApprovalState.WITHDRAWN
    assert withdrawn.decision == OperatorDecisionType.WITHDRAW


def test_state_machine_blocks_illegal_transition_from_rejected():
    """ApprovalStateMachine blocks illegal transition from terminal REJECTED state."""
    with pytest.raises(InvalidApprovalTransitionError, match="Illegal approval transition"):
        ApprovalStateMachine.validate_transition(ApprovalState.REJECTED, ApprovalState.APPROVED)


def test_state_machine_blocks_duplicate_decision_in_same_state():
    """ApprovalStateMachine blocks duplicate decision in identical state."""
    with pytest.raises(DuplicateDecisionError, match="already in state"):
        ApprovalStateMachine.validate_transition(ApprovalState.APPROVED, ApprovalState.APPROVED)


# =============================================================================
# 4. Artifact Binding & Dynamic Invalidation Tests
# =============================================================================

def test_approval_binding_valid_when_unmodified(verified_environment):
    """Approval binding is verified valid when all bound artifacts remain unchanged."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Approved",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    is_valid, reason = mgr.validate_approval_binding(repair_id=env["request"].repair_id)
    assert is_valid is True
    assert reason is None


def test_approval_binding_invalidated_when_patch_content_tampered(verified_environment):
    """Approval is immediately INVALIDATED in persistent storage if patch content is tampered with."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Approved",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    # Tamper with patch artifact in database
    tampered_patch = env["patch"].model_copy(update={"content_hash": "deadbeef" * 8})
    env["exec_store"].save_patch_artifact(tampered_patch)

    # Validate binding: must detect mismatch and invalidate decision
    is_valid, reason = mgr.validate_approval_binding(repair_id=env["request"].repair_id)
    assert is_valid is False
    assert "tampered or modified" in reason

    # Verify decision state in store was updated to INVALIDATED
    stored_dec = env["exec_store"].get_approval_decision(decision.decision_id)
    assert stored_dec.state == ApprovalState.INVALIDATED
    assert "tampered or modified" in stored_dec.invalidation_reason


def test_approval_binding_invalidated_when_commit_sha_changes(verified_environment):
    """Approval is immediately INVALIDATED if repository base commit changes."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Approved",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    # Change base commit on repair request
    tampered_req = env["request"].model_copy(update={"base_commit_sha": "f" * 40})
    env["exec_store"].save_repair_request(tampered_req)

    is_valid, reason = mgr.validate_approval_binding(repair_id=env["request"].repair_id)
    assert is_valid is False
    assert "base commit SHA changed" in reason

    stored_dec = env["exec_store"].get_approval_decision(decision.decision_id)
    assert stored_dec.state == ApprovalState.INVALIDATED


def test_approval_binding_invalidated_when_ttl_expires(verified_environment):
    """Approval transitions to EXPIRED when configured TTL window expires."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    # Submit decision with negative TTL (already expired)
    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Approved",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
        ttl_hours=-1.0,
    )

    is_valid, reason = mgr.validate_approval_binding(repair_id=env["request"].repair_id)
    assert is_valid is False
    assert "expired" in reason.lower()

    stored_dec = env["exec_store"].get_approval_decision(decision.decision_id)
    assert stored_dec.state == ApprovalState.EXPIRED


def test_cross_repair_approval_reuse_rejected(verified_environment):
    """Approval records cannot be reused or applied across different repair IDs."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="operator@forge.local",
        reason="Approved",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    # Attempt to validate this decision against a different repair ID
    is_valid, reason = mgr.validate_approval_binding(repair_id="rep_different_999")
    assert is_valid is False
    assert "No approval decision found" in reason


# =============================================================================
# 5. Persistence & Audit Ledger Tests
# =============================================================================

def test_persistence_roundtrip_dossier_and_approval(verified_environment):
    """ReviewDossier and ApprovalDecisionRecord persist and restore identically in SQLite."""
    env = verified_environment
    exec_store = env["exec_store"]
    mgr = ApprovalManager(execution_store=exec_store, forge_store=env["forge_store"])

    dossier = mgr.get_or_create_dossier(env["request"].repair_id)
    decision = mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="lead_maintainer",
        reason="Production fix reviewed and validated.",
        authorized_action=AuthorizedAction.STAGE_FOR_PR,
    )

    # Restore dossier
    restored_dossier = exec_store.get_review_dossier(dossier.dossier_id)
    assert restored_dossier is not None
    assert restored_dossier.repair_id == dossier.repair_id
    assert restored_dossier.patch_sha256 == dossier.patch_sha256
    assert restored_dossier.facts == dossier.facts

    # Restore decision
    restored_dec = exec_store.get_approval_decision(decision.decision_id)
    assert restored_dec is not None
    assert restored_dec.state == ApprovalState.APPROVED
    assert restored_dec.decision == OperatorDecisionType.APPROVE
    assert restored_dec.authorized_action == AuthorizedAction.STAGE_FOR_PR
    assert restored_dec.actor == "lead_maintainer"
    assert restored_dec.binding.patch_sha256 == env["patch_digest"]


def test_audit_event_logged_on_decision(verified_environment):
    """Every operator decision emits a secret-redacted sequential audit event."""
    env = verified_environment
    mgr = ApprovalManager(execution_store=env["exec_store"], forge_store=env["forge_store"])

    mgr.submit_decision(
        repair_id=env["request"].repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="sec_officer",
        reason="Auth token sk-test1234567890abcdef checked.",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )

    events = env["exec_store"].get_events_for_repair(env["request"].repair_id)
    appr_events = [e for e in events if e.payload.get("action") == "operator_decision_submitted"]
    assert len(appr_events) >= 1
    ev = appr_events[-1]
    assert ev.payload["decision"] == "approve"
    assert ev.payload["actor"] == "sec_officer"
    assert "sk-" not in str(ev.payload) or "REDACTED" in str(ev.payload)


# =============================================================================
# 6. CLI Integration Tests
# =============================================================================

def test_cli_repair_review_command(verified_environment, monkeypatch: pytest.MonkeyPatch, test_db_path: Path):
    """forge repair review displays formatted Markdown review dossier."""
    monkeypatch.setenv("FORGE_DB_PATH", str(test_db_path))

    result = runner.invoke(app, ["repair", "review", verified_environment["request"].repair_id])
    assert result.exit_code == 0
    assert "FORGE Repair Review Dossier" in result.output
    assert "Candidate Patch Details" in result.output
    assert verified_environment["patch_digest"] in result.output


def test_cli_repair_review_json_flag(verified_environment, monkeypatch: pytest.MonkeyPatch, test_db_path: Path):
    """forge repair review --json outputs parseable JSON."""
    monkeypatch.setenv("FORGE_DB_PATH", str(test_db_path))

    result = runner.invoke(app, ["repair", "review", verified_environment["request"].repair_id, "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.output)
    assert data["repair_id"] == verified_environment["request"].repair_id
    assert data["patch_sha256"] == verified_environment["patch_digest"]


def test_cli_repair_approve_command(verified_environment, monkeypatch: pytest.MonkeyPatch, test_db_path: Path):
    """forge repair approve records approval decision with --yes flag."""
    monkeypatch.setenv("FORGE_DB_PATH", str(test_db_path))

    result = runner.invoke(
        app,
        [
            "repair",
            "approve",
            verified_environment["request"].repair_id,
            "--actor",
            "cli_operator",
            "--reason",
            "Tested and approved",
            "--action",
            "export_patch",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert "Approval recorded successfully" in result.output
    assert "export_patch" in result.output


def test_cli_repair_reject_command(verified_environment, monkeypatch: pytest.MonkeyPatch, test_db_path: Path):
    """forge repair reject records rejection decision."""
    monkeypatch.setenv("FORGE_DB_PATH", str(test_db_path))

    result = runner.invoke(
        app,
        [
            "repair",
            "reject",
            verified_environment["request"].repair_id,
            "--actor",
            "cli_operator",
            "--reason",
            "Incorrect approach",
        ],
    )
    assert result.exit_code == 0
    assert "rejected" in result.output.lower()


def test_cli_repair_request_changes_command(verified_environment, monkeypatch: pytest.MonkeyPatch, test_db_path: Path):
    """forge repair request-changes records actionable feedback."""
    monkeypatch.setenv("FORGE_DB_PATH", str(test_db_path))

    result = runner.invoke(
        app,
        [
            "repair",
            "request-changes",
            verified_environment["request"].repair_id,
            "--actor",
            "cli_operator",
            "--feedback",
            "Please add docstring",
        ],
    )
    assert result.exit_code == 0
    assert "Changes requested" in result.output


def test_cli_repair_approval_history_command(verified_environment, monkeypatch: pytest.MonkeyPatch, test_db_path: Path):
    """forge repair approval-history displays historical decisions."""
    monkeypatch.setenv("FORGE_DB_PATH", str(test_db_path))

    # Approve first
    runner.invoke(
        app,
        [
            "repair",
            "approve",
            verified_environment["request"].repair_id,
            "--actor",
            "auditor_1",
            "--reason",
            "Good",
            "--yes",
        ],
    )

    result = runner.invoke(app, ["repair", "approval-history", verified_environment["request"].repair_id])
    assert result.exit_code == 0
    assert "auditor_1" in result.output
    assert "approved" in result.output


# =============================================================================
# 7. End-to-End Workflow Demonstration
# =============================================================================

def test_end_to_end_repair_approval_and_tamper_invalidation(verified_environment):
    """Complete workflow demonstration:

    1. Verify repair request is in VERIFIED state.
    2. Generate Review Dossier with traceable evidence.
    3. Operator explicitly approves candidate patch for EXPORT_PATCH.
    4. Confirm approval state is APPROVED and binding is valid.
    5. Attempt to tamper with patch artifact.
    6. Confirm approval is immediately INVALIDATED upon audit.
    7. Confirm target repository was NEVER modified, committed, or pushed.
    """
    env = verified_environment
    coordinator = RepairExecutionCoordinator(execution_store=env["exec_store"], forge_store=env["forge_store"])

    # Step 1: Confirm verified status
    req = env["exec_store"].get_repair_request(env["request"].repair_id)
    assert req.state == RepairExecutionState.VERIFIED

    # Step 2: Prepare review dossier
    dossier = coordinator.prepare_review_dossier(repair_id=req.repair_id)
    assert dossier.recommended_decision == "APPROVE"
    assert dossier.defect_resolved is True
    assert dossier.regression_detected is False

    # Step 3: Operator approves patch
    decision, _ = coordinator.process_operator_decision(
        repair_id=req.repair_id,
        decision=OperatorDecisionType.APPROVE,
        actor="chief_engineer",
        reason="Verified defect resolution, 0 regressions, authorized for patch export.",
        authorized_action=AuthorizedAction.EXPORT_PATCH,
    )
    assert decision.state == ApprovalState.APPROVED
    assert decision.authorized_action == AuthorizedAction.EXPORT_PATCH

    # Step 4: Confirm binding integrity
    is_valid, err = coordinator.audit_approval_integrity(repair_id=req.repair_id)
    assert is_valid is True
    assert err is None

    # Step 5: Tamper with candidate patch
    tampered_patch = env["patch"].model_copy(
        update={"content_hash": "1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff"}
    )
    env["exec_store"].save_patch_artifact(tampered_patch)

    # Step 6: Audit approval integrity -> must detect tampering and invalidate
    is_valid, err = coordinator.audit_approval_integrity(repair_id=req.repair_id)
    assert is_valid is False
    assert "tampered or modified" in err

    dec_after = env["exec_store"].get_approval_decision(decision.decision_id)
    assert dec_after.state == ApprovalState.INVALIDATED
    assert "tampered or modified" in dec_after.invalidation_reason

    # Step 7: Verify target repository remained strictly unchanged
    current_head = _get_head(env["target_repo"])
    assert current_head == env["head_sha"]
    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=str(env["target_repo"]), capture_output=True, text=True)
    assert status_res.stdout.strip() == ""
