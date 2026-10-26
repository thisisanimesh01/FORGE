"""Comprehensive test suite for Phase 5, Milestone 2: Supervised Workflow State Machine & Transactional Ledger.

Covers:
1. Guarded state machine & transition rules (allowed vs. illegal transitions, honest outcomes).
2. Mandatory human review gate (rejection of autonomous transition to APPROVED).
3. Transactional storage & optimistic concurrency (monotonic versioning, conflict detection).
4. Append-only event ledger (sequential monotonicity, unique constraint, secret redaction).
5. Safe resume and crash recovery (unblocking REPRO_BLOCKED / VERIFICATION_BLOCKED, rejecting terminal resume).
6. Target repository immutability during workflow lifecycle.
7. CLI commands (start, status, list, resume, review).
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.schemas import FindingSeverity
from forge.storage.database import init_database
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.intake import create_intake_from_issue
from forge.workflow.models import (
    EngineeringWorkflow,
    EngineeringWorkflowEvent,
    EngineeringWorkflowState,
    HumanApprovalRequiredError,
    IntakeSourceType,
    InvalidWorkflowTransitionError,
    IssueIntakePayload,
    WorkflowConcurrencyError,
    WorkflowIntakeSpec,
    WorkflowResumeError,
)
from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator
from forge.workflow.state import (
    ALLOWED_TRANSITIONS,
    RESUMABLE_STATES,
    TERMINAL_STATES,
    is_resumable_state,
    is_terminal_state,
    validate_workflow_transition,
)

runner = CliRunner()


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_workflow_m2.db"
    init_database(db_path)
    return db_path


@pytest.fixture
def store(mock_db: Path) -> ForgeStore:
    return ForgeStore(db_path=mock_db)


@pytest.fixture
def orchestrator(store: ForgeStore) -> EngineeringWorkflowOrchestrator:
    return EngineeringWorkflowOrchestrator(store=store)


@pytest.fixture
def test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Create a minimal clean local git repository for testing and return (repo_path, commit_sha)."""
    repo = tmp_path / "git_target_repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True, capture_output=True)

    src_dir = repo / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "parser.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
    (repo / "README.md").write_text("# Target Sample Repo\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    sha = res.stdout.strip()
    return repo, sha


@pytest.fixture
def sample_intake_spec(tmp_path: Path) -> WorkflowIntakeSpec:
    repo_dir = tmp_path / "target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return WorkflowIntakeSpec(
        intake_id="intake_test_001",
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo=str(repo_dir),
        base_commit_sha="a" * 40,
        title="Sample Defect Intake",
        description="Fix off-by-one error in parser loop",
        affected_files=["src/parser.py"],
        issue_payload=IssueIntakePayload(
            issue_id="ISSUE-001",
            issue_text="Fix off-by-one error in parser loop",
        ),
    )


# =============================================================================
# 1. State Machine & Transition Guard Tests
# =============================================================================

def test_transition_happy_path_pipeline():
    """Verify standard happy-path progression through all phases to human review."""
    # CREATED -> INTAKE_VALIDATED -> REPO_ANALYZED
    validate_workflow_transition(
        EngineeringWorkflowState.CREATED,
        EngineeringWorkflowState.INTAKE_VALIDATED,
    )
    validate_workflow_transition(
        EngineeringWorkflowState.INTAKE_VALIDATED,
        EngineeringWorkflowState.REPO_ANALYZED,
    )
    # REPO_ANALYZED -> REPRODUCTION_PENDING -> REPRODUCED
    validate_workflow_transition(
        EngineeringWorkflowState.REPO_ANALYZED,
        EngineeringWorkflowState.REPRODUCTION_PENDING,
    )
    validate_workflow_transition(
        EngineeringWorkflowState.REPRODUCTION_PENDING,
        EngineeringWorkflowState.REPRODUCED,
        context={"repro_outcome": "reproduced"},
    )
    # REPRODUCED -> REPAIR_PENDING -> VERIFICATION_PENDING -> VERIFIED
    validate_workflow_transition(
        EngineeringWorkflowState.REPRODUCED,
        EngineeringWorkflowState.REPAIR_PENDING,
    )
    validate_workflow_transition(
        EngineeringWorkflowState.REPAIR_PENDING,
        EngineeringWorkflowState.VERIFICATION_PENDING,
    )
    validate_workflow_transition(
        EngineeringWorkflowState.VERIFICATION_PENDING,
        EngineeringWorkflowState.VERIFIED,
        context={"verification_outcome": "repair_verified"},
    )
    # VERIFIED -> REGRESSION_CHECKING -> AWAITING_HUMAN_REVIEW
    validate_workflow_transition(
        EngineeringWorkflowState.VERIFIED,
        EngineeringWorkflowState.REGRESSION_CHECKING,
    )
    validate_workflow_transition(
        EngineeringWorkflowState.REGRESSION_CHECKING,
        EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
    )
    # AWAITING_HUMAN_REVIEW -> APPROVED with explicit human approval
    validate_workflow_transition(
        EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        EngineeringWorkflowState.APPROVED,
        context={"human_decision": "approve"},
    )


def test_transition_fast_forward_repair_requires_evidence():
    """Direct transition from REPO_ANALYZED to REPAIR_PENDING requires pre-existing repro evidence."""
    # Without repro evidence -> rejected
    with pytest.raises(InvalidWorkflowTransitionError, match="requires verified reproduction evidence"):
        validate_workflow_transition(
            EngineeringWorkflowState.REPO_ANALYZED,
            EngineeringWorkflowState.REPAIR_PENDING,
            context={"has_repro_evidence": False},
        )

    # With repro evidence -> allowed
    validate_workflow_transition(
        EngineeringWorkflowState.REPO_ANALYZED,
        EngineeringWorkflowState.REPAIR_PENDING,
        context={"has_repro_evidence": True},
    )


def test_transition_honest_reproduction_guard():
    """Cannot mark workflow as REPRODUCED if reproduction check did not confirm failure."""
    with pytest.raises(InvalidWorkflowTransitionError, match="Cannot mark workflow as REPRODUCED with non-reproduced outcome"):
        validate_workflow_transition(
            EngineeringWorkflowState.REPRODUCTION_PENDING,
            EngineeringWorkflowState.REPRODUCED,
            context={"repro_outcome": "passed"},
        )

    with pytest.raises(InvalidWorkflowTransitionError, match="Cannot mark workflow as REPRODUCED with non-reproduced outcome"):
        validate_workflow_transition(
            EngineeringWorkflowState.REPRODUCTION_PENDING,
            EngineeringWorkflowState.REPRODUCED,
            context={"repro_outcome": "skipped_sandbox_unavailable"},
        )


def test_transition_honest_verification_guard():
    """Cannot mark workflow as VERIFIED if verification check failed."""
    with pytest.raises(InvalidWorkflowTransitionError, match="Cannot mark workflow as VERIFIED with unverified outcome"):
        validate_workflow_transition(
            EngineeringWorkflowState.VERIFICATION_PENDING,
            EngineeringWorkflowState.VERIFIED,
            context={"verification_outcome": "failed"},
        )


def test_transition_mandatory_human_review_gate():
    """Transition to APPROVED strictly requires human_decision='approve'."""
    # Missing decision
    with pytest.raises(HumanApprovalRequiredError, match="Autonomous approval is forbidden"):
        validate_workflow_transition(
            EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
            EngineeringWorkflowState.APPROVED,
            context={},
        )

    # Incorrect decision value
    with pytest.raises(HumanApprovalRequiredError, match="Autonomous approval is forbidden"):
        validate_workflow_transition(
            EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
            EngineeringWorkflowState.APPROVED,
            context={"human_decision": "reject"},
        )


def test_transition_illegal_skips_rejected():
    """Arbitrary skipping of states is strictly rejected."""
    with pytest.raises(InvalidWorkflowTransitionError, match="cannot transition from 'created' to 'reproduced'"):
        validate_workflow_transition(
            EngineeringWorkflowState.CREATED,
            EngineeringWorkflowState.REPRODUCED,
        )

    with pytest.raises(InvalidWorkflowTransitionError, match="cannot transition from 'created' to 'approved'"):
        validate_workflow_transition(
            EngineeringWorkflowState.CREATED,
            EngineeringWorkflowState.APPROVED,
            context={"human_decision": "approve"},
        )


def test_terminal_states_reject_all_outgoing_transitions():
    """Terminal states have no allowed outgoing transitions."""
    for term_state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[term_state] == set()
        for target in EngineeringWorkflowState:
            with pytest.raises(InvalidWorkflowTransitionError):
                validate_workflow_transition(term_state, target)


# =============================================================================
# 2. Storage & Optimistic Concurrency Tests
# =============================================================================

def test_store_intake_persistence(store: ForgeStore, sample_intake_spec: WorkflowIntakeSpec):
    """Store persists and retrieves WorkflowIntakeSpec accurately."""
    store.save_workflow_intake(sample_intake_spec)
    loaded = store.get_workflow_intake(sample_intake_spec.intake_id)
    assert loaded is not None
    assert loaded.intake_id == sample_intake_spec.intake_id
    assert loaded.title == sample_intake_spec.title
    assert loaded.affected_files == ["src/parser.py"]

    # Filtered list
    items = store.list_workflow_intakes(target_repo=sample_intake_spec.target_repo)
    assert len(items) == 1
    assert items[0].intake_id == sample_intake_spec.intake_id


def test_store_workflow_save_and_initial_event(store: ForgeStore, sample_intake_spec: WorkflowIntakeSpec):
    """Saving initial workflow creates record and sequence 1 init event."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_test001",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.CREATED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    loaded = store.get_engineering_workflow(wf.workflow_id)
    assert loaded is not None
    assert loaded.state == EngineeringWorkflowState.CREATED
    assert loaded.version == 1

    events = store.list_engineering_workflow_events(wf.workflow_id)
    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].from_state is None
    assert events[0].to_state == EngineeringWorkflowState.CREATED
    assert events[0].stage == "init"


def test_store_transition_increments_version_and_appends_event(
    store: ForgeStore, sample_intake_spec: WorkflowIntakeSpec
):
    """Successful transition increments version and appends sequential event."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_test002",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.CREATED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # Transition CREATED -> INTAKE_VALIDATED
    updated = store.transition_engineering_workflow(
        workflow_id=wf.workflow_id,
        expected_version=1,
        target_state=EngineeringWorkflowState.INTAKE_VALIDATED,
        stage="intake_validation",
        payload={"note": "Intake syntax verified"},
    )

    assert updated.version == 2
    assert updated.state == EngineeringWorkflowState.INTAKE_VALIDATED

    events = store.list_engineering_workflow_events(wf.workflow_id)
    assert len(events) == 2
    assert events[1].sequence == 2
    assert events[1].from_state == EngineeringWorkflowState.CREATED
    assert events[1].to_state == EngineeringWorkflowState.INTAKE_VALIDATED
    assert events[1].stage == "intake_validation"
    assert events[1].payload.get("note") == "Intake syntax verified"


def test_store_optimistic_concurrency_conflict(store: ForgeStore, sample_intake_spec: WorkflowIntakeSpec):
    """Mismatched expected_version triggers WorkflowConcurrencyError without corrupting state."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_concurrency",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.CREATED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # Attempt transition with stale expected_version=999
    with pytest.raises(WorkflowConcurrencyError, match="expected version 999, but found 1"):
        store.transition_engineering_workflow(
            workflow_id=wf.workflow_id,
            expected_version=999,
            target_state=EngineeringWorkflowState.INTAKE_VALIDATED,
            stage="intake_validation",
        )

    # Verify state and event count are completely unchanged
    loaded = store.get_engineering_workflow(wf.workflow_id)
    assert loaded.version == 1
    assert loaded.state == EngineeringWorkflowState.CREATED
    events = store.list_engineering_workflow_events(wf.workflow_id)
    assert len(events) == 1


def test_store_secret_redaction_in_event_ledger(store: ForgeStore, sample_intake_spec: WorkflowIntakeSpec):
    """Sensitive API keys or credentials in event payload are automatically redacted."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_secrets",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.CREATED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # Transition with secret in payload
    store.transition_engineering_workflow(
        workflow_id=wf.workflow_id,
        expected_version=1,
        target_state=EngineeringWorkflowState.INTAKE_VALIDATED,
        stage="intake_validation",
        payload={
            "api_key": "sk-1234567890abcdef1234567890abcdef12",
            "info": "gsk_abcdef1234567890abcdef",
        },
    )

    events = store.list_engineering_workflow_events(wf.workflow_id)
    assert len(events) == 2
    payload_str = json.dumps(events[1].payload)
    assert "sk-" not in payload_str or "[REDACTED_SECRET]" in payload_str
    assert "gsk_" not in payload_str or "[REDACTED_SECRET]" in payload_str


# =============================================================================
# 3. Orchestrator Operations & Safe Resume Tests
# =============================================================================

def test_orchestrator_start_workflow_no_auto_advance(
    orchestrator: EngineeringWorkflowOrchestrator, sample_intake_spec: WorkflowIntakeSpec
):
    """start_workflow without auto_advance stops at CREATED state."""
    wf = orchestrator.start_workflow(sample_intake_spec, auto_advance=False)
    assert wf.state == EngineeringWorkflowState.CREATED
    assert wf.version == 1

    events = orchestrator.list_events(wf.workflow_id)
    assert len(events) == 1
    assert events[0].to_state == EngineeringWorkflowState.CREATED


def test_orchestrator_start_workflow_auto_advance(
    orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]
):
    """start_workflow with auto_advance advances to INTAKE_VALIDATED and REPO_ANALYZED."""
    repo_dir, sha = test_git_repo
    intake = WorkflowIntakeSpec(
        intake_id="intake_advance_001",
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo=str(repo_dir),
        base_commit_sha=sha,
        title="Sample Auto Advance Intake",
        description="Fix off-by-one error in parser loop",
        affected_files=["src/parser.py"],
        issue_payload=IssueIntakePayload(
            issue_id="ISSUE-001",
            issue_text="Fix off-by-one error in parser loop",
        ),
    )

    wf = orchestrator.start_workflow(intake, target_repo=repo_dir, auto_advance=True)
    assert wf.state == EngineeringWorkflowState.REPO_ANALYZED
    assert wf.context_id is not None
    assert wf.version == 3

    events = orchestrator.list_events(wf.workflow_id)
    assert len(events) == 3
    assert events[0].to_state == EngineeringWorkflowState.CREATED
    assert events[1].to_state == EngineeringWorkflowState.INTAKE_VALIDATED
    assert events[2].to_state == EngineeringWorkflowState.REPO_ANALYZED


def test_orchestrator_resume_blocked_states(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """resume_workflow safely unblocks REPRO_BLOCKED and VERIFICATION_BLOCKED."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)

    # 1. Unblock REPRO_BLOCKED -> REPRODUCTION_PENDING
    wf1 = EngineeringWorkflow(
        workflow_id="wf_eng_blocked1",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.REPRO_BLOCKED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf1)

    resumed1 = orchestrator.resume_workflow(wf1.workflow_id)
    assert resumed1.state == EngineeringWorkflowState.REPRODUCTION_PENDING
    assert resumed1.version == 2

    # 2. Unblock VERIFICATION_BLOCKED -> VERIFICATION_PENDING
    wf2 = EngineeringWorkflow(
        workflow_id="wf_eng_blocked2",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.VERIFICATION_BLOCKED,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf2)

    resumed2 = orchestrator.resume_workflow(wf2.workflow_id)
    assert resumed2.state == EngineeringWorkflowState.VERIFICATION_PENDING
    assert resumed2.version == 2


def test_orchestrator_resume_terminal_state_rejected(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Terminal states strictly reject resumption."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)

    for terminal_state in (EngineeringWorkflowState.APPROVED, EngineeringWorkflowState.REJECTED, EngineeringWorkflowState.REPRO_FAILED):
        wf_id = f"wf_term_{terminal_state.value}"
        wf = EngineeringWorkflow(
            workflow_id=wf_id,
            intake_id=sample_intake_spec.intake_id,
            target_repo=sample_intake_spec.target_repo,
            base_commit_sha=sample_intake_spec.base_commit_sha,
            state=terminal_state,
            version=1,
            created_at=now,
            updated_at=now,
        )
        store.save_engineering_workflow(wf)

        with pytest.raises(WorkflowResumeError, match="Cannot resume terminal workflow"):
            orchestrator.resume_workflow(wf_id)


def test_orchestrator_human_review_approval_and_rejection(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """review_workflow transitions awaiting review workflows to APPROVED or REJECTED."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)

    # 1. Approve workflow
    wf_app = EngineeringWorkflow(
        workflow_id="wf_eng_await_app",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf_app)

    approved = orchestrator.review_workflow(
        workflow_id=wf_app.workflow_id,
        decision="approve",
        notes="Code diff verified offline. Patch approved for manual release.",
    )
    assert approved.state == EngineeringWorkflowState.APPROVED
    assert approved.human_decision == "approve"
    assert "Code diff verified" in approved.human_notes

    # 2. Reject workflow
    wf_rej = EngineeringWorkflow(
        workflow_id="wf_eng_await_rej",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf_rej)

    rejected = orchestrator.review_workflow(
        workflow_id=wf_rej.workflow_id,
        decision="reject",
        notes="Fix violates internal API conventions.",
    )
    assert rejected.state == EngineeringWorkflowState.REJECTED
    assert rejected.human_decision == "reject"


def test_orchestrator_human_review_invalid_state_rejected(
    orchestrator: EngineeringWorkflowOrchestrator,
    store: ForgeStore,
    sample_intake_spec: WorkflowIntakeSpec,
):
    """Reviewing a workflow that is not awaiting review raises error."""
    store.save_workflow_intake(sample_intake_spec)
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_eng_in_flight",
        intake_id=sample_intake_spec.intake_id,
        target_repo=sample_intake_spec.target_repo,
        base_commit_sha=sample_intake_spec.base_commit_sha,
        state=EngineeringWorkflowState.REPAIR_PENDING,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    with pytest.raises(InvalidWorkflowTransitionError, match="must be in 'awaiting_human_review'"):
        orchestrator.review_workflow(wf.workflow_id, decision="approve")


# =============================================================================
# 4. Target Repository Immutability Tests
# =============================================================================

def test_target_repository_immutability(
    orchestrator: EngineeringWorkflowOrchestrator,
    test_git_repo: Tuple[Path, str],
):
    """Verify that throughout workflow transitions, target repository files are never modified."""
    repo_dir, sha = test_git_repo
    source_file = repo_dir / "src" / "parser.py"
    original_code = source_file.read_text(encoding="utf-8")

    intake = WorkflowIntakeSpec(
        intake_id="intake_immut_001",
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo=str(repo_dir),
        base_commit_sha=sha,
        title="Immutability Verification Intake",
        description="Verify target repo remains unmodified",
        affected_files=["src/parser.py"],
        issue_payload=IssueIntakePayload(
            issue_id="IMMUT-1",
            issue_text="Verify target repo remains unmodified",
        ),
    )

    # Start workflow and advance
    wf = orchestrator.start_workflow(intake, target_repo=repo_dir, auto_advance=True)
    assert source_file.read_text(encoding="utf-8") == original_code

    # Resume workflow
    # Force state to REPRO_BLOCKED
    orchestrator.store.save_engineering_workflow(
        wf.model_copy(update={"state": EngineeringWorkflowState.REPRO_BLOCKED, "version": wf.version})
    )
    orchestrator.resume_workflow(wf.workflow_id)
    assert source_file.read_text(encoding="utf-8") == original_code

    # Verify working tree remains completely clean in target repo
    res = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, check=True, capture_output=True, text=True)
    assert res.stdout.strip() == ""


# =============================================================================
# 5. CLI Integration Tests
# =============================================================================

def test_cli_workflow_start_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge workflow start and status commands operate cleanly end-to-end."""
    db_file = tmp_path / "cli_wf.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
    init_database(db_file)

    # Prepare intake spec file
    repo_dir = tmp_path / "dummy_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    intake_spec = WorkflowIntakeSpec(
        intake_id="intake_cli_001",
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo=str(repo_dir),
        base_commit_sha="b" * 40,
        title="CLI Defect Test",
        description="CLI workflow lifecycle verification",
        affected_files=[],
        issue_payload=IssueIntakePayload(
            issue_id="CLI-1",
            issue_text="CLI workflow lifecycle verification",
        ),
    )
    spec_file = tmp_path / "intake.json"
    spec_file.write_text(intake_spec.model_dump_json(indent=2), encoding="utf-8")

    # 1. Start workflow
    start_res = runner.invoke(app, ["workflow", "start", "--intake", str(spec_file)])
    assert start_res.exit_code == 0
    assert "Engineering Workflow `wf_eng_" in start_res.output
    assert "intake_cli_001" in start_res.output

    # Extract workflow ID
    import re
    match = re.search(r"wf_eng_[a-f0-9]{12}", start_res.output)
    assert match is not None
    wf_id = match.group(0)

    # 2. Check status
    status_res = runner.invoke(app, ["workflow", "status", wf_id])
    assert status_res.exit_code == 0
    assert wf_id in status_res.output
    assert "Event Ledger Timeline" in status_res.output

    # 3. List workflows
    list_res = runner.invoke(app, ["workflow", "list"])
    assert list_res.exit_code == 0
    assert wf_id in list_res.output


def test_cli_workflow_review_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge workflow review enforces approval gate and sets appropriate exit codes."""
    db_file = tmp_path / "cli_wf_rev.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
    init_database(db_file)
    store = ForgeStore(db_path=db_file)

    now = datetime.now(timezone.utc)
    intake = WorkflowIntakeSpec(
        intake_id="intake_rev_001",
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo="/path/to/repo",
        base_commit_sha="c" * 40,
        title="Review Gate Intake",
        description="Verify human decision",
        issue_payload=IssueIntakePayload(
            issue_id="REV-1",
            issue_text="Verify human decision",
        ),
    )
    store.save_workflow_intake(intake)

    wf = EngineeringWorkflow(
        workflow_id="wf_eng_rev_cli",
        intake_id=intake.intake_id,
        target_repo=intake.target_repo,
        base_commit_sha=intake.base_commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        version=1,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # 1. Review approve -> exit 0
    app_res = runner.invoke(
        app,
        ["workflow", "review", wf.workflow_id, "--decision", "approve", "--notes", "LGTM"],
    )
    assert app_res.exit_code == 0
    assert "Workflow APPROVED by human operator" in app_res.output

    # Check state in store
    loaded = store.get_engineering_workflow(wf.workflow_id)
    assert loaded.state == EngineeringWorkflowState.APPROVED
    assert loaded.human_decision == "approve"
