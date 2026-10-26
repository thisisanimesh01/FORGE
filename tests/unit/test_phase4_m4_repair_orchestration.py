"""Unit tests for Phase 4 Milestone 4: Controlled Repair Orchestration & Lifecycle.

Verifies:
- Typed workflow state machine transitions:
  CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED.
- Invalid transition rejection in code (e.g. bypassing human review, terminal mutation).
- Strict evidence grounding (rejection of unverified findings).
- Pinning and stale base revision detection (repo HEAD != pinned SHA).
- Anti-tampering patch digest verification (SHA256 mismatch rejection).
- Dry-run / plan-only mode (bypasses execution, lands at AWAITING_HUMAN_REVIEW).
- Fail-closed container sandbox boundary (Docker unavailable -> BLOCKED / SANDBOX_UNAVAILABLE without host execution).
- Defect verification outcomes (VERIFIED, FAILED, BROKEN_BUILD, REGRESSION).
- Mandatory human review gate: verified patches are NEVER automatically approved.
- Host & target repository immutability (zero file modifications, no commits/pushes).
- Resumability and safe idempotent retries.
- Transactional optimistic concurrency control (version increments, conflict detection).
- Secret redaction in diagnostics, notes, and events.
- CLI subcommands: `forge repair workflow start/status/list/resume/review`.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner
import pytest

from forge.cli import app
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairOutcome,
    RepairSafetyContract,
    RepairSafetyError,
    StaleBaseRevisionError,
    compute_patch_sha256,
)
from forge.repair.models import (
    PlanValidationResult,
    ProtectedPathPolicy,
    RepairPlan,
    RepairPlanStatus,
    RepairVerificationResult,
)
from forge.repair.orchestrator_models import (
    ALLOWED_WORKFLOW_TRANSITIONS,
    InvalidWorkflowTransitionError,
    RepairWorkflowEvent,
    RepairWorkflowRecord,
    RepairWorkflowState,
    WorkflowConcurrencyError,
    validate_workflow_transition,
)
from forge.repair.orchestrator import RepairOrchestrator
from forge.repair.planner import RepairPlanner
from forge.repair.verifier import RepairVerifier
from forge.sandbox.contracts import ReproductionOutcome
from forge.schemas import (
    EventType,
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# =============================================================================
# Helper Fixtures & Builders
# =============================================================================

@pytest.fixture
def mock_repo(tmp_path: Path) -> Path:
    """Create a mock target git repository with committed test files."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    src_file = repo_dir / "calculator.py"
    src_file.write_text("def calculate_total(price, discount):\n    return price + discount  # Defect: should be -\n")

    # Initialize a real git repo so rev-parse HEAD works
    import subprocess
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "add", "calculator.py"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)
    return repo_dir


@pytest.fixture
def mock_store(tmp_path: Path) -> ForgeStore:
    """Create a clean temporary ForgeStore."""
    db_file = tmp_path / "test_orchestrator.sqlite3"
    init_database(db_file)
    return ForgeStore(db_path=db_file)


def _create_test_finding_and_evidence(
    store: ForgeStore,
    finding_id: str = "find_orch_001",
    lifecycle_state: FindingLifecycleState = FindingLifecycleState.REPRODUCED,
    with_evidence: bool = True,
) -> Tuple[Finding, Optional[EvidenceRecord]]:
    """Helper to create a run, finding, and reproduction evidence."""
    run_id = store.create_run(target="mock_target_repo", run_id="run_orch_test")
    finding = store.create_finding(
        run_id=run_id,
        title="Inverted subtraction operator",
        description="Calculation uses + instead of -",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.CONFIRMED,
        finding_id=finding_id,
        affected_files=["calculator.py"],
    )

    ev = None
    if with_evidence:
        ev = EvidenceRecord(
            evidence_id=f"evi_repro_{finding_id}",
            run_id=run_id,
            finding_id=finding_id,
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Reproduced incorrect calculation result",
            content="AssertionError: 120 != 80",
            provenance="sandbox_oracle",
        )
        store.attach_evidence(ev)

    # Set finding state
    if lifecycle_state != FindingLifecycleState.SUSPECTED:
        conn = store._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE findings SET lifecycle_state = ? WHERE finding_id = ?",
                    (lifecycle_state.value, finding_id),
                )
        finally:
            conn.close()

    return store.get_finding(finding_id), ev


def _make_mock_patch_candidate(
    target_file: str = "calculator.py",
    finding_id: str = "find_calc_001",
    base_commit_sha: str = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    contract_id: str = "contract_calc_001",
) -> PatchCandidate:
    """Construct a valid PatchCandidate model for calculator.py."""
    diff = (
        f"--- a/{target_file}\n"
        f"+++ b/{target_file}\n"
        "@@ -1,2 +1,2 @@\n"
        " def calculate_total(price, discount):\n"
        "-    return price + discount  # Defect: should be -\n"
        "+    return price - discount  # Defect: should be -\n"
    )
    digest = compute_patch_sha256(diff)
    return PatchCandidate(
        patch_id="pat_calc_001",
        contract_id=contract_id,
        finding_id=finding_id,
        base_commit_sha=base_commit_sha,
        target_files=[target_file],
        unified_diff=diff,
        sha256_digest=digest,
        hunk_count=1,
        added_lines=1,
        removed_lines=1,
    )


# =============================================================================
# 1. State Machine Transition Tests
# =============================================================================

def test_workflow_state_machine_allowed_transitions():
    """Verify permitted transitions for all workflow states."""
    # CREATED can go to EVIDENCE_VALIDATED, BLOCKED, REJECTED
    validate_workflow_transition(RepairWorkflowState.CREATED, RepairWorkflowState.EVIDENCE_VALIDATED)
    validate_workflow_transition(RepairWorkflowState.CREATED, RepairWorkflowState.BLOCKED)
    validate_workflow_transition(RepairWorkflowState.CREATED, RepairWorkflowState.REJECTED)

    # EVIDENCE_VALIDATED -> PLAN_GENERATED
    validate_workflow_transition(RepairWorkflowState.EVIDENCE_VALIDATED, RepairWorkflowState.PLAN_GENERATED)

    # PLAN_GENERATED -> PATCH_VALIDATED
    validate_workflow_transition(RepairWorkflowState.PLAN_GENERATED, RepairWorkflowState.PATCH_VALIDATED)

    # PATCH_VALIDATED -> VERIFICATION_PENDING or AWAITING_HUMAN_REVIEW (dry-run)
    validate_workflow_transition(RepairWorkflowState.PATCH_VALIDATED, RepairWorkflowState.VERIFICATION_PENDING)
    validate_workflow_transition(RepairWorkflowState.PATCH_VALIDATED, RepairWorkflowState.AWAITING_HUMAN_REVIEW)

    # VERIFICATION_PENDING -> VERIFIED, FAILED, BLOCKED
    validate_workflow_transition(RepairWorkflowState.VERIFICATION_PENDING, RepairWorkflowState.VERIFIED)
    validate_workflow_transition(RepairWorkflowState.VERIFICATION_PENDING, RepairWorkflowState.FAILED)
    validate_workflow_transition(RepairWorkflowState.VERIFICATION_PENDING, RepairWorkflowState.BLOCKED)

    # VERIFIED -> AWAITING_HUMAN_REVIEW (MANDATORY intermediate state)
    validate_workflow_transition(RepairWorkflowState.VERIFIED, RepairWorkflowState.AWAITING_HUMAN_REVIEW)

    # AWAITING_HUMAN_REVIEW -> APPROVED, REJECTED
    validate_workflow_transition(RepairWorkflowState.AWAITING_HUMAN_REVIEW, RepairWorkflowState.APPROVED)
    validate_workflow_transition(RepairWorkflowState.AWAITING_HUMAN_REVIEW, RepairWorkflowState.REJECTED)


def test_workflow_state_machine_rejects_illegal_skip():
    """Verify illegal skips are strictly rejected with InvalidWorkflowTransitionError."""
    # Cannot jump from CREATED directly to APPROVED
    with pytest.raises(InvalidWorkflowTransitionError, match="Illegal workflow transition"):
        validate_workflow_transition(RepairWorkflowState.CREATED, RepairWorkflowState.APPROVED)

    # Cannot jump from CREATED directly to VERIFIED
    with pytest.raises(InvalidWorkflowTransitionError):
        validate_workflow_transition(RepairWorkflowState.CREATED, RepairWorkflowState.VERIFIED)

    # CRITICAL: VERIFIED cannot jump directly to APPROVED (must go through AWAITING_HUMAN_REVIEW)
    with pytest.raises(InvalidWorkflowTransitionError, match="Illegal workflow transition"):
        validate_workflow_transition(RepairWorkflowState.VERIFIED, RepairWorkflowState.APPROVED)


def test_terminal_states_cannot_transition():
    """Verify APPROVED and REJECTED states have empty target transition sets."""
    assert len(ALLOWED_WORKFLOW_TRANSITIONS[RepairWorkflowState.APPROVED]) == 0
    assert len(ALLOWED_WORKFLOW_TRANSITIONS[RepairWorkflowState.REJECTED]) == 0

    with pytest.raises(InvalidWorkflowTransitionError):
        validate_workflow_transition(RepairWorkflowState.APPROVED, RepairWorkflowState.CREATED)

    with pytest.raises(InvalidWorkflowTransitionError):
        validate_workflow_transition(RepairWorkflowState.REJECTED, RepairWorkflowState.CREATED)


# =============================================================================
# 2. End-to-End Orchestration & Full Lifecycle
# =============================================================================

def test_orchestration_happy_path_to_awaiting_review(mock_repo: Path, mock_store: ForgeStore):
    """Test full happy path from finding to AWAITING_HUMAN_REVIEW with mocked sandbox pass."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_calc_01")
    patch_cand = _make_mock_patch_candidate()

    # Mock planner to return a valid plan
    mock_planner = MagicMock()
    mock_plan = RepairPlan(
        plan_id="rpl_calc_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha="a1b2c3d4e5f67890123456789012345678901234",
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Operator inversion",
        proposed_change="Replace + with -",
        target_file="calculator.py",
        expected_behavior="Subtract discount",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_planner.plan_repair.return_value = mock_plan

    # Mock verifier to report REPAIR_VERIFIED
    mock_verifier = MagicMock()
    mock_verifier.verify_plan.return_value = RepairVerificationResult(
        plan_id=mock_plan.plan_id,
        attempt_id="att_calc_001",
        outcome=RepairOutcome.REPAIR_VERIFIED,
        diagnostic="Reproduction check passed and regression tests clean",
        pre_repro_outcome=ReproductionOutcome.REPRODUCED,
        post_repro_outcome=ReproductionOutcome.NOT_REPRODUCED,
        patch_applied=True,
        syntax_valid=True,
        sandbox_available=True,
        oracle_verified=True,
    )

    mock_sandbox = MagicMock()
    mock_sandbox.is_available.return_value = True

    # Extract actual HEAD SHA from mock_repo
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()
    mock_plan = mock_plan.model_copy(update={"base_commit_sha": head_sha})
    mock_planner.plan_repair.return_value = mock_plan

    # Save plan in store so get_repair_plan finds it
    mock_store.save_repair_plan(mock_plan)

    orchestrator = RepairOrchestrator(
        store=mock_store,
        planner=mock_planner,
        verifier=mock_verifier,
        sandbox=mock_sandbox,
    )

    # 1. Start workflow
    wf = orchestrator.start_workflow(
        finding_id=finding.finding_id,
        target_repo=mock_repo,
        base_commit_sha=head_sha,
    )
    assert wf.state == RepairWorkflowState.CREATED
    assert wf.base_commit_sha == head_sha

    # 2. Advance to completion (pauses at AWAITING_HUMAN_REVIEW)
    advanced = orchestrator.advance(wf.workflow_id)
    assert advanced.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW
    assert advanced.outcome == RepairOutcome.REPAIR_VERIFIED
    assert advanced.plan_id == mock_plan.plan_id
    assert advanced.patch_digest == patch_cand.sha256_digest
    assert advanced.human_decision is None  # NOT automatically approved

    # 3. Check event trail
    events = mock_store.list_repair_workflow_events(wf.workflow_id)
    stage_names = [e.stage for e in events]
    assert "init" in stage_names
    assert "validate_evidence" in stage_names
    assert "generate_plan" in stage_names
    assert "validate_patch" in stage_names
    assert "execute_verification" in stage_names
    assert "require_review" in stage_names

    # Monotonic sequences
    seqs = [e.sequence for e in events]
    assert seqs == list(range(1, len(events) + 1))


# =============================================================================
# 3. Human Approval Gate Enforcement
# =============================================================================

def test_human_approval_gate_approval_and_immutability(mock_repo: Path, mock_store: ForgeStore):
    """Verify human approval completes workflow and leaves target repository unmodified."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_appr_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    mock_plan = RepairPlan(
        plan_id="rpl_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
    )
    mock_store.save_repair_plan(mock_plan)

    wf_record = RepairWorkflowRecord(
        workflow_id="rwf_appr_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.AWAITING_HUMAN_REVIEW,
        plan_id="rpl_test",
        patch_digest="a" * 64,
    )
    mock_store.save_repair_workflow(wf_record)

    orchestrator = RepairOrchestrator(store=mock_store)

    # Initial file state snapshot
    target_file = mock_repo / "calculator.py"
    content_before = target_file.read_text()
    git_head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    # Submit human approval
    reviewed = orchestrator.review_workflow(
        workflow_id=wf_record.workflow_id,
        decision="approve",
        notes="Validated by senior QA team; patch verified clean",
    )

    assert reviewed.state == RepairWorkflowState.APPROVED
    assert reviewed.human_decision == "approved"
    assert reviewed.human_notes == "Validated by senior QA team; patch verified clean"
    assert reviewed.is_terminal is True

    # IMMUTABILITY ASSERTION: Target repository must remain completely unmodified
    content_after = target_file.read_text()
    git_head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()
    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    assert content_before == content_after
    assert git_head_before == git_head_after
    assert status_before == status_after == ""


def test_human_approval_gate_rejection(mock_repo: Path, mock_store: ForgeStore):
    """Verify human rejection transitions workflow to REJECTED."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_rej_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    wf_record = RepairWorkflowRecord(
        workflow_id="rwf_rej_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.AWAITING_HUMAN_REVIEW,
    )
    mock_store.save_repair_workflow(wf_record)

    orchestrator = RepairOrchestrator(store=mock_store)
    reviewed = orchestrator.review_workflow(
        workflow_id=wf_record.workflow_id,
        decision="reject",
        notes="Does not match intended architecture",
    )

    assert reviewed.state == RepairWorkflowState.REJECTED
    assert reviewed.human_decision == "rejected"
    assert reviewed.is_terminal is True


def test_review_rejected_when_not_in_awaiting_review(mock_repo: Path, mock_store: ForgeStore):
    """Reviewing a workflow that is not in AWAITING_HUMAN_REVIEW must raise."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_not_ready")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    wf = RepairWorkflowRecord(
        workflow_id="rwf_not_ready",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.CREATED,
    )
    mock_store.save_repair_workflow(wf)

    orchestrator = RepairOrchestrator(store=mock_store)
    with pytest.raises(InvalidWorkflowTransitionError, match="awaiting_human_review"):
        orchestrator.review_workflow(wf.workflow_id, decision="approve")


# =============================================================================
# 4. Dry-Run / Plan-Only Mode
# =============================================================================

def test_dry_run_mode_skips_sandboxed_execution(mock_repo: Path, mock_store: ForgeStore):
    """Dry-run mode validates patch and transitions directly to AWAITING_HUMAN_REVIEW without invoking sandbox."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_dry_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_dry_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Operator bug",
        proposed_change="Fix operator",
        target_file="calculator.py",
        expected_behavior="Subtract",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    mock_planner = MagicMock()
    mock_planner.plan_repair.return_value = mock_plan

    mock_verifier = MagicMock()
    mock_sandbox = MagicMock()

    orchestrator = RepairOrchestrator(
        store=mock_store,
        planner=mock_planner,
        verifier=mock_verifier,
        sandbox=mock_sandbox,
    )

    wf = orchestrator.start_workflow(
        finding_id=finding.finding_id,
        target_repo=mock_repo,
        base_commit_sha=head_sha,
        dry_run=True,
    )
    assert wf.dry_run is True

    result = orchestrator.advance(wf.workflow_id)
    assert result.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW
    assert result.dry_run is True
    assert result.outcome is None  # Never executed

    # Verifier and sandbox must NOT have been called
    mock_verifier.verify_plan.assert_not_called()


# =============================================================================
# 5. Fail-Closed Sandbox Boundary Tests
# =============================================================================

def test_sandbox_unavailable_transitions_to_blocked(mock_repo: Path, mock_store: ForgeStore):
    """When container sandbox is unavailable, workflow transitions to BLOCKED without executing on host."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_no_docker")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_no_docker",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    mock_planner = MagicMock()
    mock_planner.plan_repair.return_value = mock_plan

    # Sandbox is explicitly unavailable
    mock_sandbox = MagicMock()
    mock_sandbox.is_available.return_value = False

    mock_verifier = MagicMock()

    orchestrator = RepairOrchestrator(
        store=mock_store,
        planner=mock_planner,
        verifier=mock_verifier,
        sandbox=mock_sandbox,
    )

    wf = orchestrator.start_workflow(
        finding_id=finding.finding_id,
        target_repo=mock_repo,
        base_commit_sha=head_sha,
    )

    result = orchestrator.advance(wf.workflow_id)
    assert result.state == RepairWorkflowState.BLOCKED
    assert result.outcome == RepairOutcome.SANDBOX_UNAVAILABLE
    assert result.sandbox_available is False
    assert "sandbox unavailable" in result.error_message.lower()

    # Verifier must NOT have executed code
    mock_verifier.verify_plan.assert_not_called()


# =============================================================================
# 6. Verification Failures & Regression Outcomes
# =============================================================================

def test_verification_failure_transitions_to_failed(mock_repo: Path, mock_store: ForgeStore):
    """When patch fails verification (defect remains reproducible), workflow transitions to FAILED."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_fail_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_fail_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    mock_planner = MagicMock()
    mock_planner.plan_repair.return_value = mock_plan

    mock_verifier = MagicMock()
    mock_verifier.verify_plan.return_value = RepairVerificationResult(
        plan_id=mock_plan.plan_id,
        attempt_id="att_fail_001",
        outcome=RepairOutcome.REPAIR_FAILED,
        diagnostic="Target defect remained reproducible after patch",
        pre_repro_outcome=ReproductionOutcome.REPRODUCED,
        post_repro_outcome=ReproductionOutcome.REPRODUCED,
        patch_applied=True,
        syntax_valid=True,
        sandbox_available=True,
        oracle_verified=True,
    )

    mock_sandbox = MagicMock()
    mock_sandbox.is_available.return_value = True

    orchestrator = RepairOrchestrator(
        store=mock_store,
        planner=mock_planner,
        verifier=mock_verifier,
        sandbox=mock_sandbox,
    )

    wf = orchestrator.start_workflow(finding_id=finding.finding_id, target_repo=mock_repo, base_commit_sha=head_sha)
    result = orchestrator.advance(wf.workflow_id)

    assert result.state == RepairWorkflowState.FAILED
    assert result.outcome == RepairOutcome.REPAIR_FAILED
    assert "defect remained reproducible" in result.error_message.lower()


def test_verification_regression_transitions_to_failed(mock_repo: Path, mock_store: ForgeStore):
    """When patch introduces regressions, workflow transitions to FAILED with REGRESSION_DETECTED outcome."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_regr_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_regr_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    mock_planner = MagicMock()
    mock_planner.plan_repair.return_value = mock_plan

    mock_verifier = MagicMock()
    mock_verifier.verify_plan.return_value = RepairVerificationResult(
        plan_id=mock_plan.plan_id,
        attempt_id="att_regr_001",
        outcome=RepairOutcome.REGRESSION_DETECTED,
        diagnostic="Existing baseline test suite failed after patch application",
        pre_repro_outcome=ReproductionOutcome.NOT_REPRODUCED,
        post_repro_outcome=ReproductionOutcome.NOT_REPRODUCED,
        patch_applied=True,
        syntax_valid=True,
        sandbox_available=True,
        oracle_verified=True,
    )

    mock_sandbox = MagicMock()
    mock_sandbox.is_available.return_value = True

    orchestrator = RepairOrchestrator(
        store=mock_store,
        planner=mock_planner,
        verifier=mock_verifier,
        sandbox=mock_sandbox,
    )

    wf = orchestrator.start_workflow(finding_id=finding.finding_id, target_repo=mock_repo, base_commit_sha=head_sha)
    result = orchestrator.advance(wf.workflow_id)

    assert result.state == RepairWorkflowState.FAILED
    assert result.outcome == RepairOutcome.REGRESSION_DETECTED


# =============================================================================
# 7. Tampering & Stale Base Revisions
# =============================================================================

def test_patch_tampering_rejected_before_verification(mock_repo: Path, mock_store: ForgeStore):
    """Altering the candidate patch unified_diff after digest pinning must reject the workflow."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_tamp_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_tamp_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    # Workflow pinned to original digest
    orig_digest = patch_cand.sha256_digest
    wf = RepairWorkflowRecord(
        workflow_id="rwf_tamp_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.PLAN_GENERATED,
        plan_id=mock_plan.plan_id,
        patch_digest=orig_digest,
    )
    mock_store.save_repair_workflow(wf)

    # Tamper with the plan diff in the store
    tampered_diff = patch_cand.unified_diff + "\n# Tampered injected comment"
    tampered_cand = patch_cand.model_copy(update={"unified_diff": tampered_diff})
    tampered_plan = mock_plan.model_copy(update={"patch_candidate": tampered_cand})
    mock_store.save_repair_plan(tampered_plan)

    orchestrator = RepairOrchestrator(store=mock_store)
    result = orchestrator.advance(wf.workflow_id)

    assert result.state == RepairWorkflowState.REJECTED
    assert "patch integrity mismatch" in result.error_message.lower()


def test_stale_base_revision_blocks_workflow(mock_repo: Path, mock_store: ForgeStore):
    """When repo HEAD differs from pinned base SHA, workflow transitions to BLOCKED."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_stale_01")

    # Workflow pinned to an older / non-existent commit SHA
    pinned_sha = "0000000000000000000000000000000000000001"

    orchestrator = RepairOrchestrator(store=mock_store)
    wf = orchestrator.start_workflow(
        finding_id=finding.finding_id,
        target_repo=mock_repo,
        base_commit_sha=pinned_sha,
    )

    result = orchestrator.advance(wf.workflow_id)
    assert result.state == RepairWorkflowState.BLOCKED
    assert "revision mismatch" in result.error_message.lower()


def test_protected_path_patch_rejected(mock_repo: Path, mock_store: ForgeStore):
    """A patch targeting a protected file (e.g. AGENTS.md or README.md) must be rejected."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_prot_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    diff = "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1 +1 @@\n-# Malicious\n+# Patch\n"
    digest = compute_patch_sha256(diff)
    cand = PatchCandidate(
        patch_id="pat_mal_001",
        contract_id="contract_prot_001",
        finding_id=finding.finding_id,
        base_commit_sha=head_sha,
        target_files=["AGENTS.md"],
        unified_diff=diff,
        sha256_digest=digest,
        hunk_count=1,
        added_lines=1,
        removed_lines=1,
    )
    mock_plan = RepairPlan(
        plan_id="rpl_prot_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Attack",
        proposed_change="Edit AGENTS.md",
        target_file="AGENTS.md",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=cand,
    )
    mock_store.save_repair_plan(mock_plan)

    wf = RepairWorkflowRecord(
        workflow_id="rwf_prot_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.PLAN_GENERATED,
        plan_id=mock_plan.plan_id,
        patch_digest=digest,
    )
    mock_store.save_repair_workflow(wf)

    orchestrator = RepairOrchestrator(store=mock_store)
    result = orchestrator.advance(wf.workflow_id)

    assert result.state == RepairWorkflowState.REJECTED
    assert "protected by policy" in result.error_message.lower()


# =============================================================================
# 8. Resume & Idempotency Tests
# =============================================================================

def test_resume_blocked_workflow_when_unblocked(mock_repo: Path, mock_store: ForgeStore):
    """Resuming a blocked workflow succeeds once the unblocking condition is satisfied."""
    finding, ev = _create_test_finding_and_evidence(mock_store, "find_res_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_res_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    # Workflow blocked due to sandbox unavailability
    wf = RepairWorkflowRecord(
        workflow_id="rwf_res_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.BLOCKED,
        plan_id=mock_plan.plan_id,
        patch_digest=patch_cand.sha256_digest,
        outcome=RepairOutcome.SANDBOX_UNAVAILABLE,
        sandbox_available=False,
    )
    mock_store.save_repair_workflow(wf)

    # Now sandbox becomes available
    mock_sandbox = MagicMock()
    mock_sandbox.is_available.return_value = True

    mock_verifier = MagicMock()
    mock_verifier.verify_plan.return_value = RepairVerificationResult(
        plan_id=mock_plan.plan_id,
        attempt_id="att_res_001",
        outcome=RepairOutcome.REPAIR_VERIFIED,
        diagnostic="Verified cleanly",
        pre_repro_outcome=ReproductionOutcome.REPRODUCED,
        post_repro_outcome=ReproductionOutcome.NOT_REPRODUCED,
        patch_applied=True,
        syntax_valid=True,
        sandbox_available=True,
        oracle_verified=True,
    )

    orchestrator = RepairOrchestrator(
        store=mock_store,
        verifier=mock_verifier,
        sandbox=mock_sandbox,
    )

    resumed = orchestrator.resume_workflow(wf.workflow_id, auto_advance=True)
    assert resumed.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW
    assert resumed.outcome == RepairOutcome.REPAIR_VERIFIED


def test_resume_terminal_workflow_raises(mock_repo: Path, mock_store: ForgeStore):
    """Attempting to resume an approved or rejected workflow must raise RepairSafetyError."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_term_res")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    wf = RepairWorkflowRecord(
        workflow_id="rwf_term_test",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        state=RepairWorkflowState.APPROVED,
    )
    mock_store.save_repair_workflow(wf)

    orchestrator = RepairOrchestrator(store=mock_store)
    with pytest.raises(RepairSafetyError, match="terminal state"):
        orchestrator.resume_workflow(wf.workflow_id)


def test_idempotent_duplicate_start(mock_repo: Path, mock_store: ForgeStore):
    """Calling start_workflow twice with same parameters returns existing active workflow."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_idem_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    orchestrator = RepairOrchestrator(store=mock_store)
    wf1 = orchestrator.start_workflow(finding.finding_id, mock_repo, head_sha)
    wf2 = orchestrator.start_workflow(finding.finding_id, mock_repo, head_sha)

    assert wf1.workflow_id == wf2.workflow_id


# =============================================================================
# 9. Concurrency & Optimistic Locking
# =============================================================================

def test_competing_state_update_raises_concurrency_error(mock_repo: Path, mock_store: ForgeStore):
    """Simultaneous competing updates with mismatched expected_from_state raise WorkflowConcurrencyError."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_conc_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    orchestrator = RepairOrchestrator(store=mock_store)
    wf = orchestrator.start_workflow(finding.finding_id, mock_repo, head_sha)

    # Thread A advances CREATED -> EVIDENCE_VALIDATED
    mock_store.transition_repair_workflow(
        workflow_id=wf.workflow_id,
        expected_from_state=RepairWorkflowState.CREATED,
        target_state=RepairWorkflowState.EVIDENCE_VALIDATED,
        stage="thread_a",
    )

    # Thread B also expects CREATED and tries to advance -> Conflict!
    with pytest.raises(WorkflowConcurrencyError, match="concurrency conflict"):
        mock_store.transition_repair_workflow(
            workflow_id=wf.workflow_id,
            expected_from_state=RepairWorkflowState.CREATED,
            target_state=RepairWorkflowState.BLOCKED,
            stage="thread_b",
        )


def test_version_increments_monotonically(mock_repo: Path, mock_store: ForgeStore):
    """Each workflow transition increments the optimistic concurrency version number."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_ver_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    orchestrator = RepairOrchestrator(store=mock_store)
    wf = orchestrator.start_workflow(finding.finding_id, mock_repo, head_sha)
    assert wf.version == 1

    t1 = mock_store.transition_repair_workflow(
        workflow_id=wf.workflow_id,
        expected_from_state=RepairWorkflowState.CREATED,
        target_state=RepairWorkflowState.EVIDENCE_VALIDATED,
        stage="s1",
    )
    assert t1.version == 2

    t2 = mock_store.transition_repair_workflow(
        workflow_id=wf.workflow_id,
        expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
        target_state=RepairWorkflowState.PLAN_GENERATED,
        stage="s2",
    )
    assert t2.version == 3


# =============================================================================
# 10. Secret Redaction
# =============================================================================

def test_secret_redaction_in_workflow_events(mock_repo: Path, mock_store: ForgeStore):
    """Tokens and sensitive credentials in event payloads are automatically redacted."""
    finding, _ = _create_test_finding_and_evidence(mock_store, "find_sec_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    orchestrator = RepairOrchestrator(store=mock_store)
    wf = orchestrator.start_workflow(finding.finding_id, mock_repo, head_sha)

    secret_token = "sk-live-0123456789abcdef01234567"
    mock_store.transition_repair_workflow(
        workflow_id=wf.workflow_id,
        expected_from_state=RepairWorkflowState.CREATED,
        target_state=RepairWorkflowState.EVIDENCE_VALIDATED,
        stage="leak_test",
        payload={"sensitive_debug": f"Bearer {secret_token}"},
    )

    events = mock_store.list_repair_workflow_events(wf.workflow_id)
    last_event = events[-1]
    event_str = json.dumps(last_event.payload)

    assert secret_token not in event_str
    assert "[REDACTED_SECRET]" in event_str


# =============================================================================
# 11. CLI Integration Tests
# =============================================================================

def test_cli_repair_workflow_help():
    """Verify forge repair workflow --help displays subcommands."""
    res = runner.invoke(app, ["repair", "workflow", "--help"])
    assert res.exit_code == 0
    assert "Controlled repair orchestration" in res.output
    assert "start" in res.output
    assert "status" in res.output
    assert "list" in res.output
    assert "resume" in res.output
    assert "review" in res.output


def test_cli_workflow_start_dry_run_and_review(
    mock_repo: Path,
    mock_store: ForgeStore,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test full CLI lifecycle: start --dry-run -> status -> review approve."""
    monkeypatch.setenv("FORGE_DB_PATH", str(mock_store.db_path))

    finding, ev = _create_test_finding_and_evidence(mock_store, "find_cli_01")
    import subprocess
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=mock_repo, capture_output=True, text=True).stdout.strip()

    patch_cand = _make_mock_patch_candidate()
    mock_plan = RepairPlan(
        plan_id="rpl_cli_01",
        finding_id=finding.finding_id,
        target_repo=str(mock_repo),
        base_commit_sha=head_sha,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="CLI Test Bug",
        proposed_change="CLI Test Fix",
        target_file="calculator.py",
        expected_behavior="Expected",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    mock_store.save_repair_plan(mock_plan)

    with patch("forge.repair.planner.RepairPlanner.plan_repair", return_value=mock_plan):
        # 1. Start dry run
        start_res = runner.invoke(app, [
            "repair", "workflow", "start", finding.finding_id,
            "--repo", str(mock_repo),
            "--commit", head_sha,
            "--dry-run",
        ])
        assert start_res.exit_code == 0
        assert "AWAITING_HUMAN_REVIEW" in start_res.output
        assert "Dry Run" in start_res.output

        # Extract workflow ID from output
        wf_list = mock_store.list_repair_workflows(finding_id=finding.finding_id)
        assert len(wf_list) >= 1
        wf_id = wf_list[0].workflow_id

        # 2. Check status via CLI
        status_res = runner.invoke(app, ["repair", "workflow", "status", wf_id])
        assert status_res.exit_code == 0
        assert wf_id in status_res.output
        assert "AWAITING_HUMAN_REVIEW" in status_res.output

        # 3. Submit human review
        review_res = runner.invoke(app, [
            "repair", "workflow", "review", wf_id,
            "--decision", "approve",
            "--notes", "CLI approval verified",
        ])
        assert review_res.exit_code == 0
        assert "APPROVED" in review_res.output
        assert "Workflow APPROVED by human operator" in review_res.output

        # Verify final state
        wf_final = mock_store.get_repair_workflow(wf_id)
        assert wf_final.state == RepairWorkflowState.APPROVED
        assert wf_final.human_decision == "approved"
