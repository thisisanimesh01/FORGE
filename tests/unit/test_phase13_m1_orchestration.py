"""Phase 13 Milestone 1: Unit, Adversarial, and Integration Test Suite.

Tests the End-to-End Engineering Orchestration Engine including:
- Workflow state machine transitions and terminal state invariants
- Cryptographic event chain integrity and tamper detection
- Non-negotiable human approval boundary (digest, strategy, scope, replay, expiry)
- Verification & quality fail-closed lifecycles
- ToolRegistry exposure and CLI invocation
- 10 required end-to-end integration scenarios
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import pytest
from typer.testing import CliRunner

from forge.causality.schemas import (
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.cli import app
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
)
from forge.investigation.tools import ToolRegistry
from forge.orchestration.adapters import (
    FakeDiagnosisAdapter,
    FakeExecutionAdapter,
    FakePlanningAdapter,
    FakeQualityAdapter,
    FakeRootCauseAdapter,
    FakeVerificationAdapter,
)
from forge.orchestration.approval import ApprovalValidationError, OrchestrationApprovalManager
from forge.orchestration.events import OrchestrationEvent, OrchestrationEventRecorder, OrchestrationEventType
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.report import EngineeringReportGenerator
from forge.orchestration.schemas import (
    EngineeringWorkflowResult,
    StageExecutionRecord,
    StageStatus,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.orchestration.state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalStateTransitionError,
    TerminalStateError,
    is_terminal_state,
    validate_transition,
)
from forge.orchestration.tools import OrchestrationTools
from forge.repair_execution.approval import compute_plan_digest
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.schemas import (
    ApprovalStatus,
    ExecutionStatus,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
    RollbackStatus,
    WorkspaceStatus,
)
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairPlan,
    RepairPlanStatus,
    RepairRiskAssessment,
    RepairStrategy,
    RequiredChange,
    RiskLikelihood,
    RiskSeverity,
    RollbackPlan,
    StrategyComplexity,
    StrategyInvasiveness,
    StrategyReversibility,
    VerificationPlan,
)
from forge.repair_quality.schemas import (
    ConfidenceClassification,
    QualityGateDecision,
    QualityStatus,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityResult,
)
from forge.repair_verification.schemas import (
    RegressionClassification,
    VerificationResult,
    VerificationStatus,
)
from forge.schemas import FindingConfidence, FindingSeverity

runner = CliRunner()


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "logic.py").write_text("def calc(n):\n    return n * 2\n")
    return repo


def _create_sample_plan(
    plan_id: str = "plan_001",
    affected_files: Optional[List[str]] = None,
    status: RepairPlanStatus = RepairPlanStatus.ACTIONABLE,
) -> RepairPlan:
    files = affected_files or ["src/logic.py"]
    strat = RepairStrategy(
        strategy_id="strat_001",
        title="Fix logic boundary",
        description="Correct boundary condition in logic.py",
        approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
        complexity=StrategyComplexity.LOW,
        invasiveness=StrategyInvasiveness.MINIMAL,
        reversibility=StrategyReversibility.HIGH,
        affected_files=files,
    )
    change = RequiredChange(
        change_type="modify_condition",
        file_path="src/logic.py",
        description="Fix boundary condition",
        rationale="Prevents off-by-one error",
    )
    risk = RepairRiskAssessment(
        overall_risk=RiskSeverity.LOW,
        likelihood=RiskLikelihood.LOW,
        risk_score=0.10,
        explanation="Low risk of regression",
    )
    verif = VerificationPlan(
        existing_targeted_tests=["tests/test_logic.py"],
    )
    rollback = RollbackPlan(
        changes_to_reverse=["Revert changes in src/logic.py"],
    )
    return RepairPlan(
        plan_id=plan_id,
        summary="Repair plan for logic error in logic.py",
        problem_statement="Off-by-one bug in logic.py",
        repair_objective="Ensure boundary safety",
        status=status,
        confidence=FindingConfidence.HIGH,
        selected_strategy_id="strat_001",
        candidate_strategies=[strat],
        required_changes=[change],
        risk_assessment=risk,
        verification_plan=verif,
        rollback_plan=rollback,
        affected_files=files,
    )


def _create_sample_approval(
    plan: RepairPlan,
    approval_id: str = "appr_001",
    strategy_id: str = "strat_001",
    scope: Optional[List[str]] = None,
    expired: bool = False,
    approver: str = "security-lead@example.com",
) -> RepairApproval:
    digest = compute_plan_digest(plan)
    files = scope or plan.affected_files
    now = datetime.now(timezone.utc)
    expiry = (now - timedelta(hours=1)) if expired else (now + timedelta(hours=24))
    return RepairApproval(
        approval_id=approval_id,
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approved_strategy_id=strategy_id,
        approved_file_paths=files,
        approval_timestamp=now.isoformat(),
        approval_expiry=expiry.isoformat(),
        approver_reference=approver,
        approval_status=ApprovalStatus.APPROVED,
    )


# ---------------------------------------------------------------------------
# 1. Workflow State Machine Tests
# ---------------------------------------------------------------------------

def test_valid_state_transitions():
    """Verify all valid forward and branch transitions validate successfully."""
    assert validate_transition(WorkflowState.CREATED, WorkflowState.DIAGNOSING)
    assert validate_transition(WorkflowState.DIAGNOSING, WorkflowState.DIAGNOSIS_COMPLETED)
    assert validate_transition(WorkflowState.DIAGNOSIS_COMPLETED, WorkflowState.ROOT_CAUSE_ANALYSIS)
    assert validate_transition(WorkflowState.ROOT_CAUSE_ANALYSIS, WorkflowState.ROOT_CAUSE_COMPLETED)
    assert validate_transition(WorkflowState.ROOT_CAUSE_COMPLETED, WorkflowState.PLANNING)
    assert validate_transition(WorkflowState.PLANNING, WorkflowState.PLAN_READY)
    assert validate_transition(WorkflowState.PLAN_READY, WorkflowState.AWAITING_HUMAN_APPROVAL)
    assert validate_transition(WorkflowState.AWAITING_HUMAN_APPROVAL, WorkflowState.APPROVED)
    assert validate_transition(WorkflowState.APPROVED, WorkflowState.REPAIR_EXECUTING)
    assert validate_transition(WorkflowState.REPAIR_EXECUTING, WorkflowState.REPAIR_COMPLETED)
    assert validate_transition(WorkflowState.REPAIR_COMPLETED, WorkflowState.VERIFICATION_RUNNING)
    assert validate_transition(WorkflowState.VERIFICATION_RUNNING, WorkflowState.VERIFICATION_COMPLETED)
    assert validate_transition(WorkflowState.VERIFICATION_COMPLETED, WorkflowState.QUALITY_EVALUATION)
    assert validate_transition(WorkflowState.QUALITY_EVALUATION, WorkflowState.COMPLETED)


def test_invalid_state_transitions():
    """Verify illegal jumps and backwards transitions are strictly rejected."""
    with pytest.raises(IllegalStateTransitionError):
        validate_transition(WorkflowState.CREATED, WorkflowState.REPAIR_EXECUTING)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(WorkflowState.PLAN_READY, WorkflowState.REPAIR_EXECUTING)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(WorkflowState.DIAGNOSIS_COMPLETED, WorkflowState.COMPLETED)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(WorkflowState.VERIFICATION_COMPLETED, WorkflowState.COMPLETED)


def test_terminal_state_invariants():
    """Verify COMPLETED, BLOCKED, FAILED, and CANCELLED are strictly terminal."""
    assert is_terminal_state(WorkflowState.COMPLETED)
    assert is_terminal_state(WorkflowState.BLOCKED)
    assert is_terminal_state(WorkflowState.FAILED)
    assert is_terminal_state(WorkflowState.CANCELLED)

    for terminal in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[terminal] == set()
        with pytest.raises(TerminalStateError):
            validate_transition(terminal, WorkflowState.DIAGNOSING)
        with pytest.raises(TerminalStateError):
            validate_transition(terminal, WorkflowState.CREATED)


# ---------------------------------------------------------------------------
# 2. Cryptographic Event Chain & Integrity Tests
# ---------------------------------------------------------------------------

def test_event_recorder_chaining():
    """Verify cryptographic SHA-256 event chaining and verify_event_chain."""
    recorder = OrchestrationEventRecorder()
    wf_id = "wf_test_001"
    e1 = recorder.record_event(
        workflow_id=wf_id,
        event_type=OrchestrationEventType.STAGE_STARTED,
        state=WorkflowState.DIAGNOSING,
        stage=WorkflowStage.DIAGNOSIS,
        action_description="Started diagnosis",
    )
    e2 = recorder.record_event(
        workflow_id=wf_id,
        event_type=OrchestrationEventType.STAGE_COMPLETED,
        state=WorkflowState.DIAGNOSIS_COMPLETED,
        stage=WorkflowStage.DIAGNOSIS,
        action_description="Diagnosis completed",
    )
    assert e1.sequence == 0
    assert e2.sequence == 1
    assert e1.prev_event_hash == "0" * 64
    assert e2.prev_event_hash == e1.event_hash
    events = recorder.get_events(wf_id)
    is_valid, err = OrchestrationEventRecorder.verify_event_chain(events)
    assert is_valid
    assert err is None


def test_event_recorder_tamper_detection():
    """Verify tampered events cause verify_event_chain to fail."""
    recorder = OrchestrationEventRecorder()
    wf_id = "wf_test_002"
    recorder.record_event(
        workflow_id=wf_id,
        event_type=OrchestrationEventType.STAGE_STARTED,
        state=WorkflowState.DIAGNOSING,
        action_description="Step 1",
    )
    recorder.record_event(
        workflow_id=wf_id,
        event_type=OrchestrationEventType.STAGE_COMPLETED,
        state=WorkflowState.DIAGNOSIS_COMPLETED,
        action_description="Step 2",
    )

    events = recorder.get_events(wf_id)
    is_valid, _ = OrchestrationEventRecorder.verify_event_chain(events)
    assert is_valid

    # Tamper with event sequence
    tampered_event = OrchestrationEvent(
        event_id=events[1].event_id,
        workflow_id=events[1].workflow_id,
        sequence=999,
        timestamp=events[1].timestamp,
        event_type=events[1].event_type,
        stage=events[1].stage,
        state=events[1].state,
        action_description=events[1].action_description,
        details=events[1].details,
        prev_event_hash=events[1].prev_event_hash,
        event_hash=events[1].event_hash,
    )
    tampered_events = [events[0], tampered_event]
    is_valid, err = OrchestrationEventRecorder.verify_event_chain(tampered_events)
    assert not is_valid


def test_event_secret_redaction():
    """Verify sensitive tokens and paths in action/details are redacted."""
    recorder = OrchestrationEventRecorder()
    wf_id = "wf_test_003"
    secret_text = "Found key sk-1234567890abcdef1234567890abcdef"
    event = recorder.record_event(
        workflow_id=wf_id,
        event_type=OrchestrationEventType.STAGE_STARTED,
        state=WorkflowState.DIAGNOSING,
        action_description=f"Analysis with {secret_text}",
        details={"token": secret_text},
    )
    assert "sk-" not in event.action_description or "[REDACTED" in event.action_description
    assert "[REDACTED" in str(event.details)


# ---------------------------------------------------------------------------
# 3. Non-Negotiable Human Approval Boundary Tests
# ---------------------------------------------------------------------------

def test_approval_manager_valid(tmp_path: Path):
    """Valid approval passes all checks and is recorded in M4 ledger."""
    db_file = tmp_path / "ledger.db"
    ledger = ApprovalLedger(db_path=db_file)
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan()
    approval = _create_sample_approval(plan)

    mgr.validate_and_record_approval(approval, plan)
    assert mgr.is_approved(plan.plan_id)
    assert mgr.get_approval(plan.plan_id) == approval


def test_approval_manager_digest_mismatch(tmp_path: Path):
    """Mismatched plan digest is strictly rejected."""
    ledger = ApprovalLedger(db_path=tmp_path / "ledger.db")
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan()
    bad_approval = RepairApproval(
        approval_id="appr_bad_digest",
        repair_plan_id=plan.plan_id,
        plan_digest="a" * 64,
        approved_strategy_id="strat_001",
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        approver_reference="lead@example.com",
    )
    with pytest.raises(ApprovalValidationError, match="Plan digest mismatch"):
        mgr.validate_and_record_approval(bad_approval, plan)


def test_approval_manager_strategy_mismatch(tmp_path: Path):
    """Approval specifying an unselected strategy is strictly rejected."""
    ledger = ApprovalLedger(db_path=tmp_path / "ledger.db")
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan()
    bad_approval = _create_sample_approval(plan, strategy_id="strat_other")
    with pytest.raises(ApprovalValidationError, match="Strategy mismatch"):
        mgr.validate_and_record_approval(bad_approval, plan)


def test_approval_manager_scope_expansion(tmp_path: Path):
    """Approval granting files outside plan.affected_files is strictly rejected."""
    ledger = ApprovalLedger(db_path=tmp_path / "ledger.db")
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan(affected_files=["src/logic.py"])
    expanded_scope = ["src/logic.py", "etc/shadow"]
    bad_approval = _create_sample_approval(plan, scope=expanded_scope)
    with pytest.raises(ApprovalValidationError, match="Scope expansion"):
        mgr.validate_and_record_approval(bad_approval, plan)


def test_approval_manager_expired(tmp_path: Path):
    """Expired approval is strictly rejected."""
    ledger = ApprovalLedger(db_path=tmp_path / "ledger.db")
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan()
    expired_approval = _create_sample_approval(plan, expired=True)
    with pytest.raises(ApprovalValidationError, match="expired"):
        mgr.validate_and_record_approval(expired_approval, plan)


def test_approval_manager_replay_protection(tmp_path: Path):
    """Replaying an already-consumed approval is strictly rejected."""
    db_file = tmp_path / "ledger.db"
    ledger = ApprovalLedger(db_path=db_file)
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan()
    approval = _create_sample_approval(plan)

    mgr.validate_and_record_approval(approval, plan)

    # Consume in ledger
    ledger.record_consumed(approval.approval_id, "exec_001")

    # Attempt replay
    with pytest.raises(ApprovalValidationError, match="already"):
        mgr.validate_and_record_approval(approval, plan)


def test_approval_manager_revocation(tmp_path: Path):
    """Revoking an approval prevents subsequent verification and execution."""
    ledger = ApprovalLedger(db_path=tmp_path / "ledger.db")
    mgr = OrchestrationApprovalManager(ledger=ledger)
    plan = _create_sample_plan()
    approval = _create_sample_approval(plan)

    mgr.validate_and_record_approval(approval, plan)
    mgr.revoke_approval(plan.plan_id, "Security alert issued")

    assert not mgr.is_approved(plan.plan_id)
    with pytest.raises(ApprovalValidationError, match="revoked"):
        mgr.validate_and_record_approval(approval, plan)


# ---------------------------------------------------------------------------
# 4. Invariant Enforcement & Pydantic Safety Tests
# ---------------------------------------------------------------------------

def test_workflow_result_requires_human_review_invariant():
    """requires_human_review cannot be False or modified on EngineeringWorkflowResult."""
    with pytest.raises(ValueError, match="Safety invariant violated"):
        EngineeringWorkflowResult(
            workflow_id="wf_bad",
            state=WorkflowState.COMPLETED,
            repository_path="/repo",
            created_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            requires_human_review=False,
        )


def test_workflow_result_completion_integrity():
    """COMPLETED state requires plan, approval, execution, passing verification & quality."""
    with pytest.raises(ValueError, match="without a verified repair plan"):
        EngineeringWorkflowResult(
            workflow_id="wf_incomplete",
            state=WorkflowState.COMPLETED,
            repository_path="/repo",
            created_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            repair_plan_id=None,
        )

    with pytest.raises(ValueError, match="verification status"):
        EngineeringWorkflowResult(
            workflow_id="wf_failed_verif",
            state=WorkflowState.COMPLETED,
            repository_path="/repo",
            created_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            repair_plan_id="plan_001",
            approval_id="appr_001",
            execution_id="exec_001",
            verification_status="FAILED",
            quality_gate_decision="PASSED",
        )


# ---------------------------------------------------------------------------
# 5. 10 Mandatory End-to-End Integration Scenarios
# ---------------------------------------------------------------------------

def test_scenario_1_clean_end_to_end_orchestration(tmp_repo: Path, tmp_path: Path):
    """Scenario 1: Clean E2E run from CREATED through all 6 stages to COMPLETED."""
    db_file = tmp_path / "ledger.db"
    approval_mgr = OrchestrationApprovalManager(ledger=ApprovalLedger(db_path=db_file))
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
        verification_adapter=FakeVerificationAdapter(),
        quality_adapter=FakeQualityAdapter(),
    )

    state = orchestrator.run_until_approval_or_completion()
    assert state == WorkflowState.AWAITING_HUMAN_APPROVAL
    assert orchestrator.stage_outputs.repair_plan is not None

    plan = orchestrator.stage_outputs.repair_plan
    approval = _create_sample_approval(plan)
    outcome = orchestrator.submit_approval(approval)
    assert outcome.is_valid
    assert orchestrator.state == WorkflowState.APPROVED

    final_state = orchestrator.run_until_approval_or_completion()
    assert final_state == WorkflowState.COMPLETED
    assert orchestrator.is_completed()

    res = orchestrator.get_result()
    assert res.state == WorkflowState.COMPLETED
    assert res.requires_human_review is True
    assert res.repair_plan_id == plan.plan_id
    assert res.approval_id == approval.approval_id
    assert res.verification_status == "passed"
    assert res.quality_gate_decision == "PASSED"
    assert orchestrator.verify_audit_trail()


def test_scenario_2_insufficient_evidence_diagnosis(tmp_repo: Path):
    """Scenario 2: Insufficient diagnosis evidence transitions to BLOCKED."""
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        diagnosis_adapter=FakeDiagnosisAdapter(status=DiagnosisStatus.UNRESOLVED),
    )
    final_state = orchestrator.run_until_approval_or_completion()
    assert final_state == WorkflowState.BLOCKED
    assert orchestrator.stage_records[WorkflowStage.DIAGNOSIS.value].status == StageStatus.BLOCKED
    assert orchestrator.stage_outputs.root_cause_report is None


def test_scenario_3_missing_or_invalid_approval_blocks_execution(tmp_repo: Path):
    """Scenario 3: Executing repair without approval raises and remains paused."""
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    state = orchestrator.run_until_approval_or_completion()
    assert state == WorkflowState.AWAITING_HUMAN_APPROVAL

    with pytest.raises(IllegalStateTransitionError):
        orchestrator.run_repair()

    assert orchestrator.state == WorkflowState.AWAITING_HUMAN_APPROVAL


def test_scenario_4_execution_failure_blocks_workflow(tmp_repo: Path, tmp_path: Path):
    """Scenario 4: Repair execution failure transitions to BLOCKED."""
    approval_mgr = OrchestrationApprovalManager(ledger=ApprovalLedger(db_path=tmp_path / "ledger.db"))
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(status=ExecutionStatus.FAILED),
    )
    orchestrator.run_until_approval_or_completion()
    plan = orchestrator.stage_outputs.repair_plan
    orchestrator.submit_approval(_create_sample_approval(plan))

    final_state = orchestrator.run_until_approval_or_completion()
    assert final_state == WorkflowState.BLOCKED
    assert orchestrator.stage_records[WorkflowStage.EXECUTION.value].status == StageStatus.BLOCKED
    assert orchestrator.stage_outputs.verification_result is None


def test_scenario_5_verification_regression_blocks_workflow(tmp_repo: Path, tmp_path: Path):
    """Scenario 5: Verification regression detection transitions to BLOCKED, skipping quality."""
    approval_mgr = OrchestrationApprovalManager(ledger=ApprovalLedger(db_path=tmp_path / "ledger.db"))
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
        verification_adapter=FakeVerificationAdapter(
            status=VerificationStatus.REGRESSION_DETECTED,
            regression=RegressionClassification.REGRESSION_DETECTED,
            gate_decision="BLOCKED",
        ),
    )
    orchestrator.run_until_approval_or_completion()
    plan = orchestrator.stage_outputs.repair_plan
    orchestrator.submit_approval(_create_sample_approval(plan))

    final_state = orchestrator.run_until_approval_or_completion()
    assert final_state == WorkflowState.BLOCKED
    assert orchestrator.stage_records[WorkflowStage.VERIFICATION.value].status == StageStatus.BLOCKED
    assert orchestrator.stage_outputs.quality_result is None


def test_scenario_6_quality_gate_rejection_blocks_workflow(tmp_repo: Path, tmp_path: Path):
    """Scenario 6: Quality gatekeeper rejection transitions to BLOCKED, preventing COMPLETED."""
    approval_mgr = OrchestrationApprovalManager(ledger=ApprovalLedger(db_path=tmp_path / "ledger.db"))
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
        verification_adapter=FakeVerificationAdapter(),
        quality_adapter=FakeQualityAdapter(
            status=QualityStatus.FAILED,
            gate_decision=QualityGateDecision.FAILED,
            overall_score=0.45,
            policy_violations=["Maintainability score below 0.50 threshold"],
        ),
    )
    orchestrator.run_until_approval_or_completion()
    plan = orchestrator.stage_outputs.repair_plan
    orchestrator.submit_approval(_create_sample_approval(plan))

    final_state = orchestrator.run_until_approval_or_completion()
    assert final_state == WorkflowState.BLOCKED
    assert orchestrator.stage_records[WorkflowStage.QUALITY.value].status == StageStatus.BLOCKED
    assert orchestrator.stage_outputs.quality_result.gate_decision == QualityGateDecision.FAILED


def test_scenario_7_stage_exception_fail_closed(tmp_repo: Path):
    """Scenario 7: Unexpected crash in a stage fails closed and moves workflow to FAILED."""
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        diagnosis_adapter=FakeDiagnosisAdapter(should_raise=RuntimeError("Database lock acquisition timeout")),
    )
    with pytest.raises(RuntimeError):
        orchestrator.run_until_approval_or_completion()
    assert orchestrator.state == WorkflowState.FAILED
    assert orchestrator.stage_records[WorkflowStage.DIAGNOSIS.value].status == StageStatus.FAILED
    assert "Database lock" in orchestrator.stage_records[WorkflowStage.DIAGNOSIS.value].error_message


def test_scenario_8_replayed_approval_rejection_in_orchestrator(tmp_repo: Path, tmp_path: Path):
    """Scenario 8: Submitting a previously consumed approval is rejected."""
    db_file = tmp_path / "ledger.db"
    ledger = ApprovalLedger(db_path=db_file)
    approval_mgr = OrchestrationApprovalManager(ledger=ledger)
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orchestrator.run_until_approval_or_completion()
    plan = orchestrator.stage_outputs.repair_plan
    approval = _create_sample_approval(plan)

    ledger.record_consumed(approval.approval_id, "prior_exec")

    outcome = orchestrator.submit_approval(approval)
    assert not outcome.is_valid
    assert any("already been consumed" in e for e in outcome.errors)
    assert orchestrator.state == WorkflowState.BLOCKED


def test_scenario_9_workflow_cancellation(tmp_repo: Path):
    """Scenario 9: Cancellation at any non-terminal state transitions cleanly to CANCELLED."""
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        diagnosis_adapter=FakeDiagnosisAdapter(),
    )
    orchestrator.run_diagnosis()
    assert orchestrator.state == WorkflowState.DIAGNOSIS_COMPLETED

    orchestrator.cancel("User aborted run")
    assert orchestrator.state == WorkflowState.CANCELLED
    assert is_terminal_state(orchestrator.state)

    with pytest.raises(TerminalStateError):
        orchestrator.execute_next_stage()


def test_scenario_10_tampered_event_chain_verification_failure(tmp_repo: Path, tmp_path: Path):
    """Scenario 10: Event recorder detects any broken chain links or altered hashes."""
    ctx = WorkflowContext(repository_path=str(tmp_repo))
    orchestrator = EngineeringOrchestrator(
        context=ctx,
        approval_manager=OrchestrationApprovalManager(ledger=ApprovalLedger(db_path=tmp_path / "ledger.db")),
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
        verification_adapter=FakeVerificationAdapter(),
        quality_adapter=FakeQualityAdapter(),
    )
    orchestrator.run_until_approval_or_completion()
    plan = orchestrator.stage_outputs.repair_plan
    orchestrator.submit_approval(_create_sample_approval(plan))
    orchestrator.run_until_approval_or_completion()

    assert orchestrator.verify_audit_trail()

    events = orchestrator.event_recorder.get_events(orchestrator.workflow_id)
    assert len(events) >= 5
    tampered = OrchestrationEvent(
        event_id=events[3].event_id,
        workflow_id=events[3].workflow_id,
        sequence=events[3].sequence,
        timestamp=events[3].timestamp,
        event_type=events[3].event_type,
        stage=events[3].stage,
        state=events[3].state,
        action_description="Tampered action text",
        details=events[3].details,
        prev_event_hash=events[3].prev_event_hash,
        event_hash=events[3].event_hash,
    )
    orchestrator.event_recorder._events[orchestrator.workflow_id][3] = tampered

    assert not orchestrator.verify_audit_trail()


# ---------------------------------------------------------------------------
# 6. ToolRegistry & CLI Integration Tests
# ---------------------------------------------------------------------------

def test_tool_registry_orchestration_tools(tmp_repo: Path):
    """Verify all 6 orchestration tools are registered in ToolRegistry."""
    registry = ToolRegistry(workspace_path=tmp_repo)
    registered_names = [d.name for d in registry.get_definitions()]
    assert len(registered_names) >= 65

    expected_tools = [
        "create_orchestration_workflow",
        "execute_orchestration_stage",
        "inspect_orchestration_status",
        "inspect_stage_result",
        "submit_orchestration_approval",
        "get_engineering_report",
    ]
    for tool_name in expected_tools:
        assert tool_name in registered_names, f"Missing tool {tool_name} in ToolRegistry"


def test_cli_orchestrate_run_and_status(tmp_repo: Path):
    """Test CLI commands: forge orchestrate run and forge orchestrate status."""
    result = runner.invoke(app, ["orchestrate", "run", "--repo", str(tmp_repo), "--desc", "Test bug in logic.py"])
    assert result.exit_code == 0
    assert "Initialized workflow" in result.output
    assert "Workflow State:" in result.output
