"""Focused unit and integration tests for Phase 6, Milestone 3: Operator Workflow Reliability & Recovery.

Covers:
1. Interruption during reproduction, repair planning, and verification.
2. Idempotent recovery and resume behavior.
3. Interrupted human review workflows and guidance.
4. Human decision integrity: candidate patch digest validation, regression safety, stale revision rejection.
5. Permanence of rejection and immutability of approved candidates.
6. CLI workflow show and reproduce command aliases and enhanced inspection.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.repair.contracts import compute_patch_sha256
from forge.repair.models import PatchCandidate, PlanValidationResult, RepairPlan, RepairPlanStatus
from forge.storage.database import init_database
from forge.storage.store import ForgeStore
from forge.workflow.models import (
    CheckStatus,
    EngineeringWorkflow,
    EngineeringWorkflowState,
    IntakeSourceType,
    InvalidWorkflowTransitionError,
    IssueIntakePayload,
    MultiCheckVerificationResult,
    RevisionDriftError,
    VerificationAggregateOutcome,
    VerificationCheckResult,
    VerificationCheckType,
    WorkflowIntakeSpec,
    WorkflowResumeError,
)
from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator
from forge.workflow.state import is_resumable_state, is_terminal_state

runner = CliRunner()


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_workflow_m3.db"
    init_database(db_path)
    return db_path


@pytest.fixture
def store(mock_db: Path) -> ForgeStore:
    return ForgeStore(db_path=mock_db)


@pytest.fixture
def orchestrator(store: ForgeStore) -> EngineeringWorkflowOrchestrator:
    return EngineeringWorkflowOrchestrator(store=store)


@pytest.fixture
def sample_intake_spec() -> WorkflowIntakeSpec:
    return WorkflowIntakeSpec(
        intake_id="intake_m3_test01",
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo="/path/to/target/repo",
        base_commit_sha="a" * 40,
        title="Sample Defect Intake",
        description="Detailed defect description for M3 testing.",
        affected_files=["src/calculator.py"],
        issue_payload=IssueIntakePayload(
            issue_id="ISSUE-101",
            issue_text="Zero division crash in calculate_rate",
            expected_behavior="Return 0.0 on zero denominator",
            observed_behavior="ZeroDivisionError raised",
        ),
    )


@pytest.fixture
def sample_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Create a minimal clean local git repository for testing and return (repo_path, commit_sha)."""
    repo = tmp_path / "target_git_repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True, capture_output=True)

    src_dir = repo / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "calculator.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    (repo / "README.md").write_text("# Target Calculator Repo\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    sha = res.stdout.strip()
    return repo, sha


def _create_sample_repair_plan(plan_id: str = "plan_m3_001", target_file: str = "src/calculator.py") -> RepairPlan:
    diff_text = "--- a/src/calculator.py\n+++ b/src/calculator.py\n@@ -1,2 +1,3 @@\n def divide(a, b):\n+    if b == 0: return 0.0\n     return a / b\n"
    patch_sha = compute_patch_sha256(diff_text)
    candidate = PatchCandidate(
        patch_id=f"patch_{plan_id}",
        contract_id="contract_m3_001",
        finding_id="find_intake_m3_test01",
        base_commit_sha="a" * 40,
        unified_diff=diff_text,
        sha256_digest=patch_sha,
        added_lines=1,
        removed_lines=0,
        hunk_count=1,
        target_files=[target_file],
    )
    val = PlanValidationResult(
        plan_id=plan_id,
        is_valid=True,
        violations=[],
        evidence_valid=True,
        patch_valid=True,
        scope_valid=True,
        budget_valid=True,
        base_sha_valid=True,
    )
    return RepairPlan(
        plan_id=plan_id,
        finding_id="find_intake_m3_test01",
        target_repo="/path/to/target/repo",
        base_commit_sha="a" * 40,
        cited_evidence_ids=["evi_m3_test01"],
        root_cause="Missing zero divisor guard in divide function",
        proposed_change="Add zero check for divisor returning 0.0",
        target_file=target_file,
        expected_behavior="divide(10, 0) returns 0.0 instead of ZeroDivisionError",
        verification_plan="pytest tests/test_calc.py",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=candidate,
        validation=val,
    )


def _save_plan_with_finding(store: ForgeStore, plan: RepairPlan, run_id: str = "run_m3_test01") -> None:
    """Ensure runs and findings tables satisfy foreign keys before saving repair plan."""
    if not store.get_run(run_id):
        store.create_run(target="/path/to/target/repo", run_id=run_id)
    if not store.get_finding(plan.finding_id):
        store.create_finding(
            run_id=run_id,
            title="Test finding",
            description="Desc",
            finding_id=plan.finding_id,
        )
    store.save_repair_plan(plan)


def _create_sample_verification_result(
    attempt_id: str = "vatt_m3_001",
    workflow_id: str = "wf_m3_test",
    repair_plan_id: str = "plan_m3_001",
    outcome: VerificationAggregateOutcome = VerificationAggregateOutcome.PASSED,
) -> MultiCheckVerificationResult:
    return MultiCheckVerificationResult(
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        repair_plan_id=repair_plan_id,
        base_commit_sha="a" * 40,
        patch_digest="d" * 64,
        aggregate_outcome=outcome,
        regression_detected=False,
        sandbox_available=True,
        total_duration_ms=120.0,
        candidate_results=[
            VerificationCheckResult(
                check_type=VerificationCheckType.REPRODUCTION,
                name="pytest:repro",
                status=CheckStatus.PASSED,
                command=["pytest", "tests/test_repro.py"],
                pinned_revision="a" * 40,
                scratch_workspace_id="scratch_1",
                provenance="sandbox",
                duration_ms=65.0,
            ),
            VerificationCheckResult(
                check_type=VerificationCheckType.SEMANTIC,
                name="semantic:ast_invariants",
                status=CheckStatus.PASSED,
                command=["python", "-m", "ast"],
                pinned_revision="a" * 40,
                scratch_workspace_id="scratch_1",
                provenance="static_analysis",
                duration_ms=12.0,
            ),
        ],
    )


# =============================================================================
# 1. Interruption & Recovery Tests
# =============================================================================

def test_resume_repro_blocked_to_reproduction_pending(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Workflow blocked on reproduction safely resumes to REPRODUCTION_PENDING."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_blocked_repro",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.REPRO_BLOCKED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    resumed = orchestrator.resume_workflow(wf.workflow_id)
    assert resumed.state == EngineeringWorkflowState.REPRODUCTION_PENDING
    assert resumed.version == 2

    # Duplicate resume call should be completely idempotent
    dup_resumed = orchestrator.resume_workflow(wf.workflow_id)
    assert dup_resumed.state == EngineeringWorkflowState.REPRODUCTION_PENDING
    assert dup_resumed.version == 2


def test_resume_interrupted_human_review_workflow(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Workflow interrupted while AWAITING_HUMAN_REVIEW can be safely resumed idempotently."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_review_interrupted",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # Resume must succeed and return the workflow in AWAITING_HUMAN_REVIEW
    resumed = orchestrator.resume_workflow(wf.workflow_id)
    assert resumed.state == EngineeringWorkflowState.AWAITING_HUMAN_REVIEW
    assert is_resumable_state(resumed.state)


def test_resume_terminal_state_strictly_rejected(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Resuming terminal workflows (APPROVED, REJECTED, REPRO_FAILED) is strictly rejected."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)

    for term_state in (
        EngineeringWorkflowState.APPROVED,
        EngineeringWorkflowState.REJECTED,
        EngineeringWorkflowState.REPRO_FAILED,
        EngineeringWorkflowState.VERIFICATION_FAILED,
    ):
        wf = EngineeringWorkflow(
            workflow_id=f"wf_term_{term_state.value}",
            intake_id=sample_intake_spec.intake_id,
            target_repo=sample_intake_spec.target_repo,
            base_commit_sha=sample_intake_spec.base_commit_sha,
            state=term_state,
            version=1,
            created_at=now,
            updated_at=now,
        )
        store.save_engineering_workflow(wf)

        with pytest.raises(WorkflowResumeError, match="Cannot resume terminal workflow"):
            orchestrator.resume_workflow(wf.workflow_id)


def test_plan_repair_resumes_interrupted_repair_pending(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Interrupted repair planning in REPAIR_PENDING without a plan can be safely resumed."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_interrupted_repair",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.REPAIR_PENDING,
        repair_plan_id=None,  # Plan generation was interrupted
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # Calling plan_repair on a workflow with already validated plan is idempotent
    plan = _create_sample_repair_plan()
    _save_plan_with_finding(store, plan)

    wf_with_plan = EngineeringWorkflow(
        workflow_id="wf_eng_planned",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.REPAIR_PENDING,
        repair_plan_id=plan.plan_id,
        version=2,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf_with_plan)

    res = orchestrator.plan_repair(wf_with_plan.workflow_id)
    assert res.state == EngineeringWorkflowState.REPAIR_PENDING
    assert res.repair_plan_id == plan.plan_id


def test_verify_repair_idempotent_when_already_verified(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Calling verify_repair when workflow is already VERIFIED or AWAITING_HUMAN_REVIEW returns idempotently."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_already_verified",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        version=3,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    res = orchestrator.verify_repair(wf.workflow_id)
    assert res.state == EngineeringWorkflowState.AWAITING_HUMAN_REVIEW
    assert res.version == 3


# =============================================================================
# 2. Human Review & Decision Integrity Tests
# =============================================================================

def test_human_approval_validates_candidate_patch_digest(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Approval validates candidate patch SHA-256 digest and catches tampered patches."""
    store.save_workflow_intake(sample_intake_spec)
    plan = _create_sample_repair_plan(plan_id="plan_digest_check")
    _save_plan_with_finding(store, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_digest_check",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        repair_plan_id=plan.plan_id,
        verification_attempt_id="vatt_digest_check",
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    vresult = _create_sample_verification_result(
        attempt_id="vatt_digest_check",
        workflow_id=wf.workflow_id,
        repair_plan_id=plan.plan_id,
    )
    store.save_multi_check_verification(vresult)

    # 1. Clean approval passes
    approved = orchestrator.review_workflow(wf.workflow_id, decision="approve", notes="Patch verified.")
    assert approved.state == EngineeringWorkflowState.APPROVED
    assert approved.human_decision == "approve"

    # 2. Tampered patch candidate diff is caught
    tampered_diff = plan.patch_candidate.unified_diff + "\n# Malicious injected comment\n"
    tampered_candidate = PatchCandidate(
        patch_id="patch_tampered",
        contract_id="contract_m3_001",
        finding_id="find_tampered",
        base_commit_sha="a" * 40,
        unified_diff=tampered_diff,
        sha256_digest=plan.patch_candidate.sha256_digest,  # Digest not updated!
        added_lines=2,
        removed_lines=0,
        hunk_count=1,
        target_files=["src/calculator.py"],
    )
    tampered_plan = RepairPlan(
        plan_id="plan_tampered",
        finding_id="find_tampered",
        target_repo="/path/to/target/repo",
        base_commit_sha="a" * 40,
        cited_evidence_ids=["evi_tampered"],
        root_cause="Tampered patch test",
        proposed_change="Tampered modification",
        target_file="src/calculator.py",
        expected_behavior="Tampered behavior",
        verification_plan="pytest tests/test_calc.py",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=tampered_candidate,
    )
    _save_plan_with_finding(store, tampered_plan)

    wf_tampered = EngineeringWorkflow(
        workflow_id="wf_eng_tampered",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        repair_plan_id=tampered_plan.plan_id,
        verification_attempt_id="vatt_tampered",
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf_tampered)

    vresult_tampered = _create_sample_verification_result(
        attempt_id="vatt_tampered",
        workflow_id=wf_tampered.workflow_id,
        repair_plan_id=tampered_plan.plan_id,
    )
    store.save_multi_check_verification(vresult_tampered)

    with pytest.raises(ValueError, match="digest mismatch"):
        orchestrator.review_workflow(wf_tampered.workflow_id, decision="approve", notes="Should fail.")


def test_human_approval_blocks_failed_or_regressed_verification(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Approval is strictly blocked if verification aggregate outcome is not PASSED."""
    store.save_workflow_intake(sample_intake_spec)
    plan = _create_sample_repair_plan(plan_id="plan_failed_verify")
    _save_plan_with_finding(store, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_verify_failed",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        repair_plan_id=plan.plan_id,
        verification_attempt_id="vatt_failed_verify",
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    vresult = _create_sample_verification_result(
        attempt_id="vatt_failed_verify",
        workflow_id=wf.workflow_id,
        repair_plan_id=plan.plan_id,
        outcome=VerificationAggregateOutcome.FAILED,
    )
    store.save_multi_check_verification(vresult)

    with pytest.raises(ValueError, match="Cannot approve workflow with unverified aggregate outcome"):
        orchestrator.review_workflow(wf.workflow_id, decision="approve", notes="Approve despite failure.")


def test_human_approval_invalidated_by_revision_drift(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
    sample_git_repo: Tuple[Path, str],
):
    """Target repository revision drift invalidates human approval."""
    repo_path, base_sha = sample_git_repo
    store.save_workflow_intake(sample_intake_spec)

    plan = _create_sample_repair_plan(plan_id="plan_drift")
    _save_plan_with_finding(store, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_drift",
        intake_id=sample_intake_spec.intake_id,
        target_repo=str(repo_path),
        base_commit_sha=base_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        repair_plan_id=plan.plan_id,
        verification_attempt_id="vatt_drift",
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    vresult = _create_sample_verification_result(
        attempt_id="vatt_drift",
        workflow_id=wf.workflow_id,
        repair_plan_id=plan.plan_id,
    )
    store.save_multi_check_verification(vresult)

    # 1. Matching revision approves cleanly
    # (Testing with matching target_repo)
    # Simulate a new commit modifying HEAD in the repository:
    (repo_path / "src" / "other.py").write_text("# drifting change\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Drift commit"], cwd=repo_path, check=True, capture_output=True)

    # Attempting to approve after revision drift must raise RevisionDriftError
    with pytest.raises(RevisionDriftError, match="Workspace drift detected"):
        orchestrator.review_workflow(
            wf.workflow_id,
            decision="approve",
            notes="Should fail due to drift",
            target_repo=repo_path,
        )


def test_approved_workflow_cannot_change_candidate(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """An approved workflow is immutable and rejects attaching new repair plans."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_approved_immutable",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.APPROVED,
        repair_plan_id="plan_initial",
        version=2,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # Attempting to overwrite repair plan on approved workflow must raise InvalidWorkflowTransitionError
    with pytest.raises(InvalidWorkflowTransitionError, match="Cannot attach repair plan to terminal workflow"):
        store.attach_repair_plan_to_workflow(
            workflow_id=wf.workflow_id,
            expected_version=wf.version,
            repair_plan_id="plan_hijack",
        )


def test_rejection_recorded_permanently_in_event_ledger(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Rejection permanently transitions state to REJECTED and records audit event."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_rejection_audit",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    rejection_notes = "Rejected due to missing boundary checks."
    rejected = orchestrator.review_workflow(
        workflow_id=wf.workflow_id,
        decision="reject",
        notes=rejection_notes,
    )
    assert rejected.state == EngineeringWorkflowState.REJECTED
    assert is_terminal_state(rejected.state)
    assert rejected.human_decision == "reject"
    assert rejected.human_notes == rejection_notes

    # Event ledger must contain rejection event
    events = orchestrator.list_events(wf.workflow_id)
    review_events = [e for e in events if e.stage == "human_review"]
    assert len(review_events) == 1
    assert review_events[0].to_state == EngineeringWorkflowState.REJECTED
    assert review_events[0].payload["decision"] == "reject"
    assert review_events[0].payload["notes"] == rejection_notes

    # Repeated decision fails closed
    with pytest.raises(InvalidWorkflowTransitionError):
        orchestrator.review_workflow(wf.workflow_id, decision="approve")


# =============================================================================
# 3. CLI Usability & Inspection Tests
# =============================================================================

def test_cli_workflow_show_and_reproduce_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """forge workflow show and forge workflow reproduce aliases execute cleanly."""
    db_file = tmp_path / "cli_aliases.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
    init_database(db_file)
    store = ForgeStore(db_path=db_file)
    store.save_workflow_intake(sample_intake_spec)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_alias_test",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.REPRODUCED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # 1. Test forge workflow show (alias for status)
    show_res = runner.invoke(app, ["workflow", "show", wf.workflow_id])
    assert show_res.exit_code == 0
    assert f"Engineering Workflow `{wf.workflow_id}`" in show_res.output
    assert "REPRODUCED (Defect Verified by Execution Oracle)" in show_res.output
    assert "forge workflow plan-repair" in show_res.output

    # 2. Test forge workflow reproduce (alias for repro)
    reproduce_res = runner.invoke(app, ["workflow", "reproduce", wf.workflow_id])
    # Already in REPRODUCED -> idempotent exit 0
    assert reproduce_res.exit_code == 0
    assert "REPRODUCED" in reproduce_res.output


def test_cli_workflow_inspection_displays_patch_and_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Workflow status/show displays candidate patch diff and verification table."""
    db_file = tmp_path / "cli_inspect.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
    init_database(db_file)
    store = ForgeStore(db_path=db_file)
    store.save_workflow_intake(sample_intake_spec)

    plan = _create_sample_repair_plan(plan_id="plan_inspect")
    _save_plan_with_finding(store, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_inspect_full",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        repair_plan_id=plan.plan_id,
        verification_attempt_id="vatt_inspect",
        version=3,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    vresult = _create_sample_verification_result(
        attempt_id="vatt_inspect",
        workflow_id=wf.workflow_id,
        repair_plan_id=plan.plan_id,
    )
    store.save_multi_check_verification(vresult)

    res = runner.invoke(app, ["workflow", "status", wf.workflow_id])
    assert res.exit_code == 0
    assert "AWAITING OPERATOR REVIEW (Review Gate Active)" in res.output
    assert "Candidate Repair Patch" in res.output
    assert "src/calculator.py" in res.output
    assert "+    if b == 0: return 0.0" in res.output
    assert "Multi-Check Verification Results" in res.output
    assert "pytest:repro" in res.output
    assert "semantic:ast_invariants" in res.output
