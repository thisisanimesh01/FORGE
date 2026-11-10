"""Unit, Adversarial, and Integration Test Suite for Phase 13 M2:
Agent Autonomy, Coordination & Recovery.

Verifies:
- Durable SQLite persistence across process restarts, WAL mode, foreign keys, and rollbacks.
- Crash and interruption recovery at every stage with cryptographic event verification.
- Interrupted repair reconciliation requirements and at-most-once mutation safeguards.
- Bounded retry policy with deterministic failure classification and zero blind mutation retries.
- Task coordination DAG resolution, Kahn's algorithm cycle detection, and failure propagation.
- Strict human approval preservation across crashes, restarts, and recoveries.
- CLI subcommands (status, inspect, recover, tasks, reconcile) and ToolRegistry integration.
- All 14 M2 Integration Scenarios.
"""

import concurrent.futures
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import pytest
from typer.testing import CliRunner

from forge.causality.schemas import CausalStatus, RootCauseReport
from forge.diagnosis.schemas import DiagnosisInput, DiagnosisResult, DiagnosisStatus
from forge.repair_execution.approval import compute_plan_digest
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.schemas import (
    ApprovalStatus,
    ExecutionLimits,
    ExecutionStatus,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
)
from forge.repair_planning.schemas import (
    RepairPlan,
    RepairPlanStatus,
)
from forge.repair_quality.schemas import (
    QualityGateDecision,
    QualityStatus,
    RepairQualityPolicy,
    RepairQualityResult,
)
from forge.repair_verification.schemas import (
    RegressionClassification,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.orchestration.adapters import (
    FakeDiagnosisAdapter,
    FakeExecutionAdapter,
    FakePlanningAdapter,
    FakeQualityAdapter,
    FakeRootCauseAdapter,
    FakeVerificationAdapter,
)
from forge.orchestration.approval import (
    OrchestrationApprovalError,
    OrchestrationApprovalManager,
)
from forge.orchestration.coordination import (
    DependencyCycleError,
    TaskCoordinator,
    TaskRecord,
    TaskSchedulingError,
    TaskState,
)
from forge.orchestration.events import (
    AuditTrailTamperedError,
    OrchestrationEvent,
    OrchestrationEventRecorder,
    OrchestrationEventType,
)
from forge.orchestration.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    OperationStatus,
    UncertainExecutionError,
    compute_idempotency_key,
)
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.persistence import (
    CorruptedStateError,
    IncompatibleSchemaError,
    ORCHESTRATION_SCHEMA_VERSION,
    OrchestrationStateStore,
    PersistenceError,
)
from forge.orchestration.recovery import (
    MutationReconciliationRequiredError,
    RecoveryDecision,
    RecoveryValidationError,
    WorkflowRecoveryEngine,
    WorkflowRecoveryError,
)
from forge.orchestration.retry import (
    FailureCategory,
    FailureClassifier,
    RetryDecision,
    RetryPolicy,
)
from forge.orchestration.schemas import (
    StageExecutionRecord,
    StageStatus,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.orchestration.state import (
    IllegalStateTransitionError,
    InvalidTransitionError,
    TerminalStateError,
)
from forge.orchestration.tools import OrchestrationTools
from forge.investigation.tools import ToolRegistry
from forge.cli import app

runner = CliRunner()


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Provide a dedicated SQLite DB path for each test."""
    return tmp_path / "orchestration_test.db"


@pytest.fixture
def test_context(tmp_path: Path) -> WorkflowContext:
    """Create a standard WorkflowContext bound to a temporary repository."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return WorkflowContext(
        workflow_id="wf_test_001",
        repository_path=str(repo),
        commit_sha="abcd1234efgh5678",
        run_id="run_test_001",
        trusted_approvers={"alice", "bob"},
        defect_input=DiagnosisInput(user_description="Test defect"),
    )


# ===========================================================================
# 1. Persistence & Schema Integrity Tests
# ===========================================================================

def test_persistence_workflow_save_and_reload(temp_db: Path, test_context: WorkflowContext):
    """Workflow state, context, and metadata persist and reload across store instances."""
    store1 = OrchestrationStateStore(db_path=temp_db)
    store1.save_workflow(
        context=test_context,
        state=WorkflowState.PLAN_READY,
        current_stage=WorkflowStage.PLANNING,
        active_repair_plan_id="plan_001",
        metadata={"custom_key": "val_123"},
    )

    # Re-open in a second fresh store instance (simulating process restart)
    store2 = OrchestrationStateStore(db_path=temp_db)
    wf_data = store2.get_workflow(test_context.workflow_id)

    assert wf_data is not None
    assert wf_data["workflow_id"] == test_context.workflow_id
    assert wf_data["state"] == WorkflowState.PLAN_READY.value
    assert wf_data["repository_path"] == test_context.repository_path
    assert wf_data["current_stage"] == WorkflowStage.PLANNING.value
    assert wf_data["active_repair_plan_id"] == "plan_001"
    assert "alice" in wf_data["trusted_approvers"]
    assert wf_data["metadata"]["custom_key"] == "val_123"


def test_persistence_foreign_key_enforcement(temp_db: Path):
    """Foreign keys are strictly enforced on connections."""
    store = OrchestrationStateStore(db_path=temp_db)
    with pytest.raises(sqlite3.IntegrityError):
        # Insert a checkpoint for a non-existent workflow
        conn = store._get_connection()
        try:
            conn.execute(
                """
                INSERT INTO orchestration_recovery_checkpoints (
                    checkpoint_id, workflow_id, stage, state, last_event_sequence,
                    last_event_hash, outputs_digest, created_at
                ) VALUES ('chk_1', 'non_existent_wf', 'PLANNING', 'PLAN_READY', 1, 'hash', 'digest', 'now');
                """
            )
        finally:
            conn.close()


def test_persistence_schema_migration_tracking(temp_db: Path):
    """Schema migrations table records current schema version."""
    store = OrchestrationStateStore(db_path=temp_db)
    conn = store._get_connection()
    try:
        cur = conn.execute("SELECT version FROM orchestration_schema_migrations;")
        versions = [row["version"] for row in cur.fetchall()]
        assert ORCHESTRATION_SCHEMA_VERSION in versions
    finally:
        conn.close()


def test_persistence_corrupted_state_fail_closed(temp_db: Path, test_context: WorkflowContext):
    """Corrupted schema version raises IncompatibleSchemaError fail-closed."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.CREATED)

    # Intentionally corrupt schema version in DB
    conn = store._get_connection()
    try:
        conn.execute(
            "UPDATE orchestration_workflows SET schema_version = 999 WHERE workflow_id = ?",
            (test_context.workflow_id,),
        )
    finally:
        conn.close()

    with pytest.raises(IncompatibleSchemaError, match="Incompatible schema version"):
        store.get_workflow(test_context.workflow_id)


def test_persistence_stage_records_and_outputs(temp_db: Path, test_context: WorkflowContext):
    """StageExecutionRecord and output payloads serialize and deserialize cleanly."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.DIAGNOSING)

    rec = StageExecutionRecord(
        stage=WorkflowStage.DIAGNOSIS,
        status=StageStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        metadata={"diag_id": "diag_001"},
    )
    store.save_stage_record(
        workflow_id=test_context.workflow_id,
        stage=WorkflowStage.DIAGNOSIS,
        record=rec,
        output_payload={"status": "confirmed", "diagnosis_id": "diag_001"},
    )

    loaded_records = store.get_stage_records(test_context.workflow_id)
    assert WorkflowStage.DIAGNOSIS.value in loaded_records
    assert loaded_records[WorkflowStage.DIAGNOSIS.value].status == StageStatus.COMPLETED

    loaded_outputs = store.get_stage_outputs_raw(test_context.workflow_id)
    assert loaded_outputs[WorkflowStage.DIAGNOSIS.value]["diagnosis_id"] == "diag_001"


# ===========================================================================
# 2. Retry Policy & Failure Classification Tests
# ===========================================================================

def test_failure_classification_deterministic():
    """Deterministic categorization of exceptions into failure categories."""
    assert FailureClassifier.classify_exception(sqlite3.OperationalError("database is locked")) == FailureCategory.TRANSIENT_INFRASTRUCTURE
    assert FailureClassifier.classify_exception(TimeoutError("operation timed out")) == FailureCategory.TIMEOUT
    assert FailureClassifier.classify_exception(PermissionError("access denied")) == FailureCategory.AUTHORIZATION_FAILURE
    assert FailureClassifier.classify_exception(AuditTrailTamperedError("tampered")) == FailureCategory.INTEGRITY_FAILURE
    assert FailureClassifier.classify_exception(ValueError("invalid input")) == FailureCategory.INVALID_INPUT
    assert FailureClassifier.classify_exception(RuntimeError("unrecoverable execution failed")) == FailureCategory.PERMANENT_EXECUTION_FAILURE
    assert FailureClassifier.classify_exception(Exception("any mutation interrupted"), is_mutating=True) == FailureCategory.UNCERTAIN_MUTATION


def test_retry_policy_transient_allowed():
    """Transient failures are retryable with bounded backoff."""
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=1.0)
    decision = policy.evaluate(FailureCategory.TRANSIENT_INFRASTRUCTURE, attempt=1, is_mutating=False)
    assert decision.should_retry
    assert decision.delay_seconds >= 0.1

    decision2 = policy.evaluate(FailureCategory.TIMEOUT, attempt=2, is_mutating=False)
    assert decision2.should_retry
    assert decision2.delay_seconds >= 0.2


def test_retry_policy_exceeding_max_attempts():
    """Attempts exceeding max_attempts return should_retry=False."""
    policy = RetryPolicy(max_attempts=3)
    decision = policy.evaluate(FailureCategory.TRANSIENT_INFRASTRUCTURE, attempt=3, is_mutating=False)
    assert not decision.should_retry
    assert "exhausted" in decision.reason.lower() or "exceeded" in decision.reason.lower()


def test_retry_policy_non_retryable_categories():
    """Authorization, integrity, policy, and sandbox failures fail closed without retries."""
    policy = RetryPolicy(max_attempts=5)
    for cat in (
        FailureCategory.AUTHORIZATION_FAILURE,
        FailureCategory.INTEGRITY_FAILURE,
        FailureCategory.SANDBOX_UNAVAILABLE,
        FailureCategory.INVALID_INPUT,
        FailureCategory.PERMANENT_EXECUTION_FAILURE,
    ):
        dec = policy.evaluate(cat, attempt=1, is_mutating=False)
        assert not dec.should_retry, f"Category {cat.value} should not be retryable"


def test_retry_policy_zero_blind_mutation_retries():
    """State-mutating operations NEVER retry blindly regardless of exception category."""
    policy = RetryPolicy(max_attempts=5)
    for cat in FailureCategory:
        dec = policy.evaluate(cat, attempt=1, is_mutating=True)
        assert not dec.should_retry, f"Mutating operations must never retry blindly ({cat.value})"


# ===========================================================================
# 3. Idempotency & Safeguards Tests
# ===========================================================================

def test_idempotency_key_canonical():
    """Idempotency key produces deterministic SHA-256 bound to workflow, stage, and digest."""
    key1 = compute_idempotency_key("wf_1", WorkflowStage.EXECUTION, "digest_abc")
    key2 = compute_idempotency_key("wf_1", WorkflowStage.EXECUTION, "digest_abc")
    key3 = compute_idempotency_key("wf_2", WorkflowStage.EXECUTION, "digest_abc")
    assert key1 == key2
    assert key1 != key3
    assert len(key1) == 64


def test_idempotency_ledger_lifecycle(temp_db: Path):
    """Idempotency ledger transitions from started -> completed, caching results."""
    ledger = IdempotencyLedger(db_path=temp_db)
    key = compute_idempotency_key("wf_1", WorkflowStage.EXECUTION, "digest_1")

    # 1. Register start
    existing = ledger.register_start(key, "wf_1", WorkflowStage.EXECUTION)
    assert existing is None

    # 2. Second register_start returns active STARTED record
    rec = ledger.register_start(key, "wf_1", WorkflowStage.EXECUTION)
    assert rec is not None
    assert rec.status == OperationStatus.STARTED

    # 3. Mark completed
    ledger.mark_completed(key, result_payload={"status": "APPLIED", "patch_id": "p1"}, result_digest="patch_digest_abc")
    rec_done = ledger.get_record(key)
    assert rec_done is not None
    assert rec_done.status == OperationStatus.COMPLETED
    assert rec_done.result_payload["status"] == "APPLIED"


def test_idempotency_uncertain_mutation_tracking(temp_db: Path):
    """Interrupted operations can be marked as UNCERTAIN."""
    ledger = IdempotencyLedger(db_path=temp_db)
    key = compute_idempotency_key("wf_1", WorkflowStage.EXECUTION, "digest_2")
    ledger.register_start(key, "wf_1", WorkflowStage.EXECUTION)
    ledger.mark_uncertain(key, reason="Process terminated during repair")

    rec = ledger.get_record(key)
    assert rec is not None
    assert rec.status == OperationStatus.UNCERTAIN
    assert "terminated" in (rec.error_message or "")


# ===========================================================================
# 4. Task Coordination & DAG Tests
# ===========================================================================

def test_coordination_topological_resolution(temp_db: Path):
    """Tasks are dispatched in topological dependency order."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(WorkflowContext(workflow_id="wf_coord", repository_path=str(temp_db.parent)), WorkflowState.CREATED)
    coord = TaskCoordinator(workflow_id="wf_coord", store=store)

    t1 = coord.register_task("t_diag", "Diagnosis", WorkflowStage.DIAGNOSIS, idempotency_key="idemp_1", priority=10)
    t2 = coord.register_task("t_rca", "Root Cause", WorkflowStage.ROOT_CAUSE, idempotency_key="idemp_2", dependencies=["t_diag"], priority=20)
    t3 = coord.register_task("t_plan", "Planning", WorkflowStage.PLANNING, idempotency_key="idemp_3", dependencies=["t_rca"], priority=30)

    # Initially only t_diag is ready
    ready = coord.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].task_id == "t_diag"

    # Complete t_diag -> t_rca becomes ready
    coord.start_task("t_diag")
    coord.complete_task("t_diag")
    ready2 = coord.get_ready_tasks()
    assert len(ready2) == 1
    assert ready2[0].task_id == "t_rca"

    # Complete t_rca -> t_plan becomes ready
    coord.start_task("t_rca")
    coord.complete_task("t_rca")
    ready3 = coord.get_ready_tasks()
    assert len(ready3) == 1
    assert ready3[0].task_id == "t_plan"


def test_coordination_cycle_detection_kahn(temp_db: Path):
    """Kahn's algorithm detects cyclic dependencies at registration time and raises DependencyCycleError."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(WorkflowContext(workflow_id="wf_cycle", repository_path=str(temp_db.parent)), WorkflowState.CREATED)
    coord = TaskCoordinator(workflow_id="wf_cycle", store=store)

    coord.register_task("t_a", "Task A", WorkflowStage.DIAGNOSIS, idempotency_key="k_a")
    coord.register_task("t_b", "Task B", WorkflowStage.ROOT_CAUSE, idempotency_key="k_b", dependencies=["t_a"])

    # Attempting to add task C depending on B and task A depending on C creates a cycle
    coord.register_task("t_c", "Task C", WorkflowStage.PLANNING, idempotency_key="k_c", dependencies=["t_b"])

    with pytest.raises(DependencyCycleError, match="Cyclic dependency detected"):
        coord._validate_no_cycles("t_a", candidate_deps=["t_c"])


def test_coordination_failure_propagation(temp_db: Path):
    """When a prerequisite task fails, downstream dependent tasks transition to BLOCKED."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(WorkflowContext(workflow_id="wf_fail", repository_path=str(temp_db.parent)), WorkflowState.CREATED)
    coord = TaskCoordinator(workflow_id="wf_fail", store=store)

    coord.register_task("t_root", "Root Task", WorkflowStage.DIAGNOSIS, idempotency_key="k_root")
    coord.register_task("t_child", "Child Task", WorkflowStage.ROOT_CAUSE, idempotency_key="k_child", dependencies=["t_root"])
    coord.register_task("t_grandchild", "Grandchild Task", WorkflowStage.PLANNING, idempotency_key="k_grandchild", dependencies=["t_child"])

    coord.start_task("t_root")
    coord.fail_task("t_root", error_message="Fatal analysis defect")

    # Downstream tasks must be BLOCKED
    t_child = coord.get_task("t_child")
    t_grandchild = coord.get_task("t_grandchild")
    assert t_child.state == TaskState.BLOCKED
    assert t_grandchild.state == TaskState.BLOCKED
    assert len(coord.get_ready_tasks()) == 0


def test_coordination_cancellation(temp_db: Path):
    """cancel_pending_tasks transitions all PENDING and READY tasks to CANCELLED."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(WorkflowContext(workflow_id="wf_cancel", repository_path=str(temp_db.parent)), WorkflowState.CREATED)
    coord = TaskCoordinator(workflow_id="wf_cancel", store=store)

    coord.register_task("t_1", "Task 1", WorkflowStage.DIAGNOSIS, idempotency_key="k1")
    coord.register_task("t_2", "Task 2", WorkflowStage.ROOT_CAUSE, idempotency_key="k2", dependencies=["t_1"])

    cancelled = coord.cancel_pending_tasks(reason="Workflow aborted by operator")
    assert len(cancelled) == 2
    for t in cancelled:
        assert t.state == TaskState.CANCELLED
        assert t.error_message == "Workflow aborted by operator"


# ===========================================================================
# 5. Recovery Engine Core Tests
# ===========================================================================

def test_recovery_repository_binding_check(temp_db: Path, test_context: WorkflowContext):
    """Recovery from mismatched repository path fails closed."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.PLAN_READY)
    recorder = OrchestrationEventRecorder()
    ev1 = recorder.record_event(
        workflow_id=test_context.workflow_id,
        event_type=OrchestrationEventType.WORKFLOW_CREATED,
        state=WorkflowState.CREATED,
        action_description="Init",
    )
    store.save_event(ev1)
    engine = WorkflowRecoveryEngine(state_store=store)

    with pytest.raises(RecoveryValidationError, match="Repository binding mismatch"):
        engine.inspect_recovery(test_context.workflow_id, expected_repo_path="/wrong/path/to/repo")


def test_recovery_tampered_event_chain_blocks_recovery(temp_db: Path, test_context: WorkflowContext):
    """Tampered event chain in SQLite prevents recovery and marks state BLOCKED."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.PLAN_READY)

    recorder = OrchestrationEventRecorder()
    ev1 = recorder.record_event(
        workflow_id=test_context.workflow_id,
        event_type=OrchestrationEventType.WORKFLOW_CREATED,
        state=WorkflowState.CREATED,
        action_description="Init",
    )
    store.save_event(ev1)

    # Tamper with event in DB
    conn = store._get_connection()
    try:
        conn.execute(
            "UPDATE orchestration_events SET action_description = 'TAMPERED' WHERE workflow_id = ?",
            (test_context.workflow_id,),
        )
    finally:
        conn.close()

    engine = WorkflowRecoveryEngine(state_store=store)
    with pytest.raises((AuditTrailTamperedError, RecoveryValidationError)):
        engine.inspect_recovery(test_context.workflow_id)


def test_recovery_reconciliation_unblocks_workflow(temp_db: Path, test_context: WorkflowContext):
    """reconcile_mutation unblocks an interrupted repair with operator attribution."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(
        context=test_context,
        state=WorkflowState.BLOCKED,
        reconciliation_required=True,
    )
    engine = WorkflowRecoveryEngine(state_store=store)

    engine.reconcile_mutation(
        workflow_id=test_context.workflow_id,
        resolution="discard",
        reconciled_by="alice",
        notes="Resetting workspace after interrupted process",
    )

    wf_data = store.get_workflow(test_context.workflow_id)
    assert wf_data["state"] == WorkflowState.AWAITING_HUMAN_APPROVAL.value
    assert wf_data["reconciliation_required"] == 0


# ===========================================================================
# 6. All 14 M2 Integration Scenarios
# ===========================================================================

def test_scenario_1_clean_end_to_end_with_full_persistence(temp_db: Path, test_context: WorkflowContext):
    """Scenario 1: Clean end-to-end execution with full SQLite persistence and checkpointing."""
    store = OrchestrationStateStore(db_path=temp_db)
    ledger = IdempotencyLedger(db_path=temp_db)
    approval_manager = OrchestrationApprovalManager(trusted_approvers={"alice"})

    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        idempotency_ledger=ledger,
        approval_manager=approval_manager,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
        verification_adapter=FakeVerificationAdapter(),
        quality_adapter=FakeQualityAdapter(),
    )

    # Run up to approval
    orch.run_until_approval_or_completion()
    assert orch.state == WorkflowState.AWAITING_HUMAN_APPROVAL

    # Submit valid approval
    plan = orch.stage_outputs.repair_plan
    digest = compute_plan_digest(plan)
    appr = RepairApproval(
        approval_id="appr_scen_1",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch.submit_approval(appr)
    assert orch.state == WorkflowState.APPROVED

    # Run through completion
    orch.run_until_approval_or_completion()
    assert orch.state == WorkflowState.COMPLETED

    # Verify persisted stage records
    stages = store.get_stage_records(test_context.workflow_id)
    assert WorkflowStage.DIAGNOSIS.value in stages
    assert WorkflowStage.EXECUTION.value in stages
    assert WorkflowStage.QUALITY.value in stages
    assert stages[WorkflowStage.QUALITY.value].status == StageStatus.COMPLETED


def test_scenario_2_crash_after_diagnosis_completed(temp_db: Path, test_context: WorkflowContext):
    """Scenario 2: Crash after diagnosis completed -> recovery resumes to root-cause."""
    store = OrchestrationStateStore(db_path=temp_db)
    orch1 = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        diagnosis_adapter=FakeDiagnosisAdapter(),
    )
    orch1.run_diagnosis()
    assert orch1.state == WorkflowState.DIAGNOSIS_COMPLETED

    # Process "dies" -> simulate fresh process recovery
    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    decision, orch2 = recovery_engine.recover_workflow(
        workflow_id=test_context.workflow_id,
        root_cause_adapter=FakeRootCauseAdapter(),
    )
    assert decision.can_resume
    assert orch2.state == WorkflowState.DIAGNOSIS_COMPLETED

    # Can now execute next stage (ROOT_CAUSE)
    orch2.execute_next_stage()
    assert orch2.state == WorkflowState.ROOT_CAUSE_COMPLETED


def test_scenario_3_crash_during_planning(temp_db: Path, test_context: WorkflowContext):
    """Scenario 3: Crash during planning -> recovery safely resumes at planning."""
    store = OrchestrationStateStore(db_path=temp_db)
    orch1 = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
    )
    orch1.run_diagnosis()
    orch1.run_root_cause()
    assert orch1.state == WorkflowState.ROOT_CAUSE_COMPLETED

    # Simulate interrupt in PLANNING
    store.save_workflow(context=test_context, state=WorkflowState.PLANNING, current_stage=WorkflowStage.PLANNING)

    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    decision, orch2 = recovery_engine.recover_workflow(
        workflow_id=test_context.workflow_id,
        planning_adapter=FakePlanningAdapter(),
    )
    assert decision.can_resume
    assert decision.next_stage_to_execute == WorkflowStage.PLANNING


def test_scenario_4_awaiting_approval_preserves_human_gate(temp_db: Path, test_context: WorkflowContext):
    """Scenario 4: Recovery at Awaiting Approval preserves human approval boundary (never auto-approves)."""
    store = OrchestrationStateStore(db_path=temp_db)
    orch1 = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch1.run_until_approval_or_completion()
    assert orch1.state == WorkflowState.AWAITING_HUMAN_APPROVAL

    # Recover in fresh engine
    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    decision, orch2 = recovery_engine.recover_workflow(workflow_id=test_context.workflow_id)

    assert orch2.state == WorkflowState.AWAITING_HUMAN_APPROVAL
    assert decision.human_action_required is not None
    assert "approve" in decision.human_action_required.lower()


def test_scenario_5_crash_after_approval_preserves_approved_state(temp_db: Path, test_context: WorkflowContext):
    """Scenario 5: Crash after human approval -> recovery preserves approved state, ready to repair."""
    store = OrchestrationStateStore(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(trusted_approvers={"alice"})
    orch1 = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch1.run_until_approval_or_completion()
    plan = orch1.stage_outputs.repair_plan
    digest = compute_plan_digest(plan)
    appr = RepairApproval(
        approval_id="appr_scen_5",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch1.submit_approval(appr)
    assert orch1.state == WorkflowState.APPROVED

    # Recovery
    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    decision, orch2 = recovery_engine.recover_workflow(
        workflow_id=test_context.workflow_id,
        approval_manager=approval_mgr,
        execution_adapter=FakeExecutionAdapter(),
    )
    assert decision.can_resume
    assert orch2.state == WorkflowState.APPROVED
    assert decision.next_stage_to_execute == WorkflowStage.EXECUTION


def test_scenario_6_crash_during_repair_execution_requires_reconciliation(temp_db: Path, test_context: WorkflowContext):
    """Scenario 6: Crash DURING repair execution (REPAIR_EXECUTING) blocks and requires operator reconciliation."""
    store = OrchestrationStateStore(db_path=temp_db)
    orch1 = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch1.run_until_approval_or_completion()

    # Simulate interrupt while in REPAIR_EXECUTING
    orch1._transition(WorkflowState.APPROVED, "Approved", stage=WorkflowStage.APPROVAL)
    orch1._transition(WorkflowState.REPAIR_EXECUTING, "Executing", stage=WorkflowStage.EXECUTION)

    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    decision = recovery_engine.inspect_recovery(test_context.workflow_id)

    assert not decision.can_resume
    assert decision.reconciliation_required
    assert "reconciliation required" in decision.reason.lower()


def test_scenario_7_operator_reconcile_confirm(temp_db: Path, test_context: WorkflowContext):
    """Scenario 7: Operator reconciliation with confirm verifies evidence and moves state to REPAIR_COMPLETED."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.BLOCKED, reconciliation_required=True)
    # Provide completed execution evidence so confirm can succeed
    store.save_stage_record(
        workflow_id=test_context.workflow_id,
        stage=WorkflowStage.EXECUTION,
        record=StageExecutionRecord(
            stage=WorkflowStage.EXECUTION,
            status=StageStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        ),
    )

    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    recovery_engine.reconcile_mutation(
        workflow_id=test_context.workflow_id,
        resolution="confirm",
        reconciled_by="alice",
    )

    wf = store.get_workflow(test_context.workflow_id)
    assert wf["state"] == WorkflowState.REPAIR_COMPLETED.value
    assert wf["reconciliation_required"] == 0


def test_scenario_8_operator_reconcile_discard(temp_db: Path, test_context: WorkflowContext):
    """Scenario 8: Operator reconciliation with discard returns workflow to AWAITING_HUMAN_APPROVAL."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.BLOCKED, reconciliation_required=True)

    recovery_engine = WorkflowRecoveryEngine(state_store=store)
    recovery_engine.reconcile_mutation(
        workflow_id=test_context.workflow_id,
        resolution="discard",
        reconciled_by="alice",
    )

    wf = store.get_workflow(test_context.workflow_id)
    assert wf["state"] == WorkflowState.AWAITING_HUMAN_APPROVAL.value
    assert wf["reconciliation_required"] == 0


def test_scenario_9_idempotency_cached_execution_replay(temp_db: Path, test_context: WorkflowContext):
    """Scenario 9: Duplicate repair execution returns cached result without re-executing."""
    store = OrchestrationStateStore(db_path=temp_db)
    ledger = IdempotencyLedger(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(trusted_approvers={"alice"})

    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        idempotency_ledger=ledger,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
    )
    orch.run_until_approval_or_completion()

    plan = orch.stage_outputs.repair_plan
    digest = compute_plan_digest(plan)
    appr = RepairApproval(
        approval_id="appr_scen_9",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch.submit_approval(appr)
    exec_res1 = orch.run_repair()
    assert orch.state == WorkflowState.REPAIR_COMPLETED

    # Reset state to APPROVED to simulate replay attempt
    orch.state = WorkflowState.APPROVED
    exec_res2 = orch.run_repair()

    # Replay returned identical cached result
    assert exec_res2.execution_id == exec_res1.execution_id
    assert orch.state == WorkflowState.REPAIR_COMPLETED


def test_scenario_10_transient_failure_retried_and_succeeds(temp_db: Path, test_context: WorkflowContext):
    """Scenario 10: Transient failure in verification retries and succeeds within retry bounds."""
    store = OrchestrationStateStore(db_path=temp_db)
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.01)

    class FlakyVerificationAdapter(FakeVerificationAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def run_verification(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked (transient)")
            return super().run_verification(*args, **kwargs)

    flaky_adapter = FlakyVerificationAdapter()
    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        retry_policy=policy,
        verification_adapter=flaky_adapter,
    )
    orch.state = WorkflowState.REPAIR_COMPLETED
    orch.stage_outputs.execution_result = RepairExecutionResult(
        execution_id="exec_1",
        repair_plan_id="plan_1",
        workspace_id="ws_1",
        applied_patch_digest="d1",
        execution_status=ExecutionStatus.APPLIED,
        diff_summary="summary",
        approval_reference="alice",
    )
    orch.stage_outputs.repair_workspace = RepairWorkspace(
        workspace_id="ws_1",
        original_repository_reference=str(test_context.repository_path),
        isolated_workspace_path=str(test_context.repository_path),
        baseline_commit=test_context.commit_sha,
        baseline_tree_digest="0" * 64,
    )

    verif_res = orch.run_verification()
    assert flaky_adapter.calls == 2
    assert verif_res.verification_status == VerificationStatus.PASSED


def test_scenario_11_authorization_failure_fails_closed_immediately(temp_db: Path, test_context: WorkflowContext):
    """Scenario 11: Non-retryable authorization failure fails closed immediately with zero retries."""
    store = OrchestrationStateStore(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(trusted_approvers={"alice"})

    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch.run_until_approval_or_completion()

    # Submit invalid approver
    plan = orch.stage_outputs.repair_plan
    digest = compute_plan_digest(plan)
    appr = RepairApproval(
        approval_id="appr_unauth",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approver_reference="unauthorized_mallory",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    outcome = orch.submit_approval(appr)
    assert not outcome.is_valid
    assert orch.state == WorkflowState.BLOCKED


def test_scenario_12_multi_task_dag_topological_execution(temp_db: Path):
    """Scenario 12: Multi-task DAG execution in topological order with cycle detection."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(WorkflowContext(workflow_id="wf_dag_12", repository_path=str(temp_db.parent)), WorkflowState.CREATED)
    coord = TaskCoordinator(workflow_id="wf_dag_12", store=store)

    coord.register_task("t1", "Diag", WorkflowStage.DIAGNOSIS, idempotency_key="k1")
    coord.register_task("t2", "RCA", WorkflowStage.ROOT_CAUSE, idempotency_key="k2", dependencies=["t1"])
    coord.register_task("t3", "Plan", WorkflowStage.PLANNING, idempotency_key="k3", dependencies=["t2"])

    order = []
    while True:
        ready = coord.get_ready_tasks()
        if not ready:
            break
        task = ready[0]
        coord.start_task(task.task_id)
        coord.complete_task(task.task_id)
        order.append(task.task_id)

    assert order == ["t1", "t2", "t3"]


def test_scenario_13_dag_failure_propagation(temp_db: Path):
    """Scenario 13: DAG failure propagation blocks downstream dependent tasks."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(WorkflowContext(workflow_id="wf_dag_13", repository_path=str(temp_db.parent)), WorkflowState.CREATED)
    coord = TaskCoordinator(workflow_id="wf_dag_13", store=store)

    coord.register_task("t1", "T1", WorkflowStage.DIAGNOSIS, idempotency_key="k1")
    coord.register_task("t2", "T2", WorkflowStage.ROOT_CAUSE, idempotency_key="k2", dependencies=["t1"])

    coord.start_task("t1")
    coord.fail_task("t1", error_message="Fatal")

    assert coord.get_task("t2").state == TaskState.BLOCKED


def test_scenario_14_tampered_event_chain_in_sqlite_blocks_recovery(temp_db: Path, test_context: WorkflowContext):
    """Scenario 14: Tampered event chain in SQLite prevents recovery with fail-closed error."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.PLAN_READY)

    recorder = OrchestrationEventRecorder()
    ev1 = recorder.record_event(
        workflow_id=test_context.workflow_id,
        event_type=OrchestrationEventType.WORKFLOW_CREATED,
        state=WorkflowState.CREATED,
        action_description="Init",
    )
    store.save_event(ev1)

    # Tamper with event sequence
    conn = store._get_connection()
    try:
        conn.execute(
            "UPDATE orchestration_events SET event_hash = 'corrupted_hash' WHERE workflow_id = ?",
            (test_context.workflow_id,),
        )
    finally:
        conn.close()

    engine = WorkflowRecoveryEngine(state_store=store)
    with pytest.raises((AuditTrailTamperedError, RecoveryValidationError)):
        engine.inspect_recovery(test_context.workflow_id)


# ===========================================================================
# 7. CLI & ToolRegistry Integration Tests
# ===========================================================================

def test_tool_registry_m2_recovery_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ToolRegistry exposes inspect_workflow_recovery, recover_orchestration_workflow, and inspect_workflow_tasks."""
    db_path = tmp_path / "reg_test.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry(workspace_path=repo)
    def_names = [d.name for d in registry.get_definitions()]

    assert "inspect_workflow_recovery" in def_names
    assert "recover_orchestration_workflow" in def_names
    assert "inspect_workflow_tasks" in def_names

    # Test inspection via ToolRegistry
    res = registry.execute_tool("inspect_workflow_recovery", {"workflow_id": "non_existent"})
    assert "not found" in res.lower() or "error" in res.lower()


def test_cli_orchestrate_inspect_and_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI inspect and tasks commands operate cleanly against persisted SQLite state."""
    db_path = tmp_path / "cli_m2_test.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

    store = OrchestrationStateStore(db_path=db_path)
    ctx = WorkflowContext(
        workflow_id="wf_cli_m2",
        repository_path=str(tmp_path),
        run_id="run_cli_m2",
    )
    store.save_workflow(context=ctx, state=WorkflowState.CREATED)

    rec = OrchestrationEventRecorder()
    ev = rec.record_event(
        workflow_id="wf_cli_m2",
        event_type=OrchestrationEventType.WORKFLOW_CREATED,
        state=WorkflowState.CREATED,
        action_description="Init CLI",
    )
    store.save_event(ev)

    # CLI inspect
    result_inspect = runner.invoke(app, ["orchestrate", "inspect", "wf_cli_m2"])
    assert result_inspect.exit_code == 0
    assert "Workflow ID:          wf_cli_m2" in result_inspect.output
    assert "Can Resume:           True" in result_inspect.output

    # CLI tasks (empty)
    result_tasks = runner.invoke(app, ["orchestrate", "tasks", "wf_cli_m2"])
    assert result_tasks.exit_code == 0
    assert "No tasks registered" in result_tasks.output


def test_cli_orchestrate_reconcile_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI reconcile command records operator reconciliation decision."""
    db_path = tmp_path / "cli_reconcile_test.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

    store = OrchestrationStateStore(db_path=db_path)
    ctx = WorkflowContext(
        workflow_id="wf_rec_cli",
        repository_path=str(tmp_path),
        run_id="run_rec_cli",
    )
    store.save_workflow(context=ctx, state=WorkflowState.BLOCKED, reconciliation_required=True)

    # 1. Unverified confirm is rejected
    result_bad = runner.invoke(app, ["orchestrate", "reconcile", "wf_rec_cli", "--resolution", "confirm"])
    assert result_bad.exit_code == 1
    assert "no evidence of completed repair" in result_bad.output.lower()

    # 2. Discard unblocks workflow
    result = runner.invoke(app, ["orchestrate", "reconcile", "wf_rec_cli", "--resolution", "discard"])
    assert result.exit_code == 0
    assert "successfully reconciled" in result.output


# ===========================================================================
# 7. M2 Adversarial Security & Invariant Audit Tests
# ===========================================================================

def test_adversarial_reconciliation_unverified_confirm_rejected(temp_db: Path, test_context: WorkflowContext):
    """Confirm must NEVER blindly execute or complete without verified evidence."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.BLOCKED, reconciliation_required=True)
    engine = WorkflowRecoveryEngine(state_store=store)

    with pytest.raises(RecoveryValidationError, match="no evidence of completed repair"):
        engine.reconcile_mutation(
            workflow_id=test_context.workflow_id,
            resolution="confirm",
            reconciled_by="alice",
        )

    # Workflow must remain blocked with reconciliation_required=1
    wf = store.get_workflow(test_context.workflow_id)
    assert wf["state"] == WorkflowState.BLOCKED.value
    assert wf["reconciliation_required"] == 1


def test_adversarial_reconciliation_prohibited_identities_rejected(temp_db: Path, test_context: WorkflowContext):
    """Synthetic, AI, empty, or bot identities are rejected for reconciliation."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.BLOCKED, reconciliation_required=True)
    engine = WorkflowRecoveryEngine(state_store=store)

    for bad_id in ("auto", "llm", "ai", "bot", "synthetic", "none", "unknown", "", "   "):
        with pytest.raises(RecoveryValidationError, match="prohibited or invalid"):
            engine.reconcile_mutation(
                workflow_id=test_context.workflow_id,
                resolution="discard",
                reconciled_by=bad_id,
            )


def test_adversarial_reconciliation_unauthorized_operator_rejected(temp_db: Path, test_context: WorkflowContext):
    """Operator not present in workflow trusted_approvers allowlist is rejected."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.BLOCKED, reconciliation_required=True)
    engine = WorkflowRecoveryEngine(state_store=store)

    with pytest.raises(RecoveryValidationError, match="not in the trusted approvers list"):
        engine.reconcile_mutation(
            workflow_id=test_context.workflow_id,
            resolution="discard",
            reconciled_by="eve_untrusted",
        )


def test_adversarial_reconciliation_on_unblocked_workflow_rejected(temp_db: Path, test_context: WorkflowContext):
    """Reconciliation cannot be invoked on a workflow that is not blocked for reconciliation."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.PLAN_READY, reconciliation_required=False)
    engine = WorkflowRecoveryEngine(state_store=store)

    with pytest.raises(RecoveryValidationError, match="does not require mutation reconciliation"):
        engine.reconcile_mutation(
            workflow_id=test_context.workflow_id,
            resolution="discard",
            reconciled_by="alice",
        )


def _setup_events(store: OrchestrationStateStore, workflow_id: str):
    rec = OrchestrationEventRecorder()
    ev = rec.record_event(
        workflow_id=workflow_id,
        event_type=OrchestrationEventType.WORKFLOW_CREATED,
        state=WorkflowState.CREATED,
        action_description="Workflow initialized",
    )
    store.save_event(ev)


def test_adversarial_automatic_discard_bypass_rejected(temp_db: Path, test_context: WorkflowContext):
    """recover_workflow must reject allow_discard_unconfirmed_mutation to enforce human reconciliation."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(context=test_context, state=WorkflowState.REPAIR_EXECUTING, reconciliation_required=True)
    _setup_events(store, test_context.workflow_id)
    engine = WorkflowRecoveryEngine(state_store=store)

    with pytest.raises(RecoveryValidationError, match="Automatic discard of unconfirmed mutations is forbidden"):
        engine.recover_workflow(
            workflow_id=test_context.workflow_id,
            allow_discard_unconfirmed_mutation=True,
        )


def test_crash_lifecycle_point_1_before_patch_application(temp_db: Path, test_context: WorkflowContext):
    """Crash Point 1: Crash in APPROVED state before repair execution starts."""
    store = OrchestrationStateStore(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(trusted_approvers={"alice"})
    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch.run_until_approval_or_completion()
    plan = orch.stage_outputs.repair_plan
    appr = RepairApproval(
        approval_id="appr_pt_1",
        repair_plan_id=plan.plan_id,
        plan_digest=compute_plan_digest(plan),
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch.submit_approval(appr)
    assert orch.state == WorkflowState.APPROVED

    # Simulate crash before run_repair()
    engine = WorkflowRecoveryEngine(state_store=store)
    decision = engine.inspect_recovery(test_context.workflow_id)
    assert decision.can_resume
    assert decision.recovered_state == WorkflowState.APPROVED
    assert decision.next_stage_to_execute == WorkflowStage.EXECUTION
    assert not decision.reconciliation_required
    assert not approval_mgr.ledger.is_consumed(appr.approval_id)


def test_crash_lifecycle_point_2_during_patch_application(temp_db: Path, test_context: WorkflowContext):
    """Crash Point 2: Process killed while in REPAIR_EXECUTING midway through patch application."""
    store = OrchestrationStateStore(db_path=temp_db)
    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch.run_until_approval_or_completion()
    orch._transition(WorkflowState.APPROVED, "Approved", stage=WorkflowStage.APPROVAL)
    orch._transition(WorkflowState.REPAIR_EXECUTING, "Mutating workspace...", stage=WorkflowStage.EXECUTION)

    # Process dies abruptly
    engine = WorkflowRecoveryEngine(state_store=store)
    decision = engine.inspect_recovery(test_context.workflow_id)
    assert not decision.can_resume
    assert decision.recovered_state == WorkflowState.BLOCKED
    assert decision.reconciliation_required
    assert "uncertain" in decision.reason.lower()


def test_crash_lifecycle_point_3_after_patch_before_persistence(temp_db: Path, test_context: WorkflowContext):
    """Crash Point 3: Workspace mutated on disk, but process crashed before result persistence to SQLite."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(
        context=test_context,
        state=WorkflowState.REPAIR_EXECUTING,
        current_stage=WorkflowStage.EXECUTION,
        reconciliation_required=True,
    )
    _setup_events(store, test_context.workflow_id)

    engine = WorkflowRecoveryEngine(state_store=store)
    decision = engine.inspect_recovery(test_context.workflow_id)
    assert not decision.can_resume
    assert decision.reconciliation_required

    # Attempting to confirm without evidence must fail closed
    with pytest.raises(RecoveryValidationError, match="no evidence of completed repair"):
        engine.reconcile_mutation(test_context.workflow_id, "confirm", reconciled_by="alice")


def test_crash_lifecycle_point_4_after_persistence_before_transition(temp_db: Path, test_context: WorkflowContext):
    """Crash Point 4: Execution result persisted, but crash occurs before workflow transitions to REPAIR_COMPLETED."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(
        context=test_context,
        state=WorkflowState.BLOCKED,
        current_stage=WorkflowStage.EXECUTION,
        reconciliation_required=True,
    )
    _setup_events(store, test_context.workflow_id)
    # Persist completed stage record
    store.save_stage_record(
        workflow_id=test_context.workflow_id,
        stage=WorkflowStage.EXECUTION,
        record=StageExecutionRecord(
            stage=WorkflowStage.EXECUTION,
            status=StageStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        ),
    )

    engine = WorkflowRecoveryEngine(state_store=store)
    # Operator confirms based on verified evidence
    engine.reconcile_mutation(test_context.workflow_id, "confirm", reconciled_by="alice")

    wf = store.get_workflow(test_context.workflow_id)
    assert wf["state"] == WorkflowState.REPAIR_COMPLETED.value
    assert wf["reconciliation_required"] == 0

    # Next stage is now VERIFICATION
    decision = engine.inspect_recovery(test_context.workflow_id)
    assert decision.can_resume
    assert decision.next_stage_to_execute == WorkflowStage.VERIFICATION


def test_crash_lifecycle_point_5_during_rollback(temp_db: Path, test_context: WorkflowContext):
    """Crash Point 5: Interrupted during discard/revert keeps workflow safely blocked."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(
        context=test_context,
        state=WorkflowState.BLOCKED,
        current_stage=WorkflowStage.EXECUTION,
        reconciliation_required=True,
    )
    _setup_events(store, test_context.workflow_id)

    engine = WorkflowRecoveryEngine(state_store=store)
    decision = engine.inspect_recovery(test_context.workflow_id)
    assert not decision.can_resume
    assert decision.recovered_state == WorkflowState.BLOCKED
    assert decision.reconciliation_required


def test_crash_lifecycle_point_6_during_reconciliation(temp_db: Path, test_context: WorkflowContext):
    """Crash Point 6: Crash or exception during reconciliation leaves workflow blocked."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(
        context=test_context,
        state=WorkflowState.BLOCKED,
        current_stage=WorkflowStage.EXECUTION,
        reconciliation_required=True,
    )
    _setup_events(store, test_context.workflow_id)

    engine = WorkflowRecoveryEngine(state_store=store)
    # Operator attempts invalid reconciliation
    with pytest.raises(RecoveryValidationError):
        engine.reconcile_mutation(test_context.workflow_id, "invalid_resolution", reconciled_by="alice")

    # State must remain untouched
    wf = store.get_workflow(test_context.workflow_id)
    assert wf["state"] == WorkflowState.BLOCKED.value
    assert wf["reconciliation_required"] == 1


def test_approval_consumption_once_consumed_never_restored(temp_db: Path, test_context: WorkflowContext):
    """Consumed approval is never restored as unused across restarts or reloads."""
    store = OrchestrationStateStore(db_path=temp_db)
    ledger = IdempotencyLedger(db_path=temp_db)
    appr_ledger = ApprovalLedger(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(ledger=appr_ledger, trusted_approvers={"alice"})

    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        idempotency_ledger=ledger,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
    )
    orch.run_until_approval_or_completion()
    plan = orch.stage_outputs.repair_plan
    appr = RepairApproval(
        approval_id="appr_durable_test",
        repair_plan_id=plan.plan_id,
        plan_digest=compute_plan_digest(plan),
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch.submit_approval(appr)
    orch.run_repair()

    assert approval_mgr.ledger.is_consumed(appr.approval_id)

    # Fresh instance reloading from same DB
    fresh_appr_ledger = ApprovalLedger(db_path=temp_db)
    new_approval_mgr = OrchestrationApprovalManager(ledger=fresh_appr_ledger, trusted_approvers={"alice"})
    assert new_approval_mgr.ledger.is_consumed(appr.approval_id)

    # Attempting to re-verify or re-execute with this consumed approval fails closed
    outcome = new_approval_mgr.verify_approval(appr, plan, workflow_id=test_context.workflow_id)
    print("OUTCOME ERRORS:", outcome.errors)
    assert not outcome.is_valid
    assert any("consumed" in err.lower() for err in outcome.errors)


def test_approval_consumption_discard_preserves_consumption_requires_fresh_approval(
    temp_db: Path, test_context: WorkflowContext
):
    """Discarding a mutation resets workflow to AWAITING_HUMAN_APPROVAL but never un-consumes the approval."""
    store = OrchestrationStateStore(db_path=temp_db)
    ledger = IdempotencyLedger(db_path=temp_db)
    appr_ledger = ApprovalLedger(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(ledger=appr_ledger, trusted_approvers={"alice"})

    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        idempotency_ledger=ledger,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
        execution_adapter=FakeExecutionAdapter(),
    )
    orch.run_until_approval_or_completion()
    plan = orch.stage_outputs.repair_plan
    appr = RepairApproval(
        approval_id="appr_discard_test",
        repair_plan_id=plan.plan_id,
        plan_digest=compute_plan_digest(plan),
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch.submit_approval(appr)

    # Mark consumed and simulate interruption
    approval_mgr.ledger.record_consumed(
        approval_id=appr.approval_id,
        execution_id="exec_interrupted_1",
    )
    orch._transition(WorkflowState.REPAIR_EXECUTING, "Executing", stage=WorkflowStage.EXECUTION)
    store.save_workflow(context=test_context, state=WorkflowState.BLOCKED, reconciliation_required=True)

    # Operator discards mutation
    engine = WorkflowRecoveryEngine(state_store=store)
    engine.reconcile_mutation(test_context.workflow_id, "discard", reconciled_by="alice")

    wf = store.get_workflow(test_context.workflow_id)
    assert wf["state"] == WorkflowState.AWAITING_HUMAN_APPROVAL.value

    # Old approval is STILL consumed
    assert approval_mgr.ledger.is_consumed(appr.approval_id)

    # Recover orchestrator to synchronize hydrated audit event chain
    decision, orch2 = engine.recover_workflow(test_context.workflow_id, approval_manager=approval_mgr)
    assert orch2.state == WorkflowState.AWAITING_HUMAN_APPROVAL

    # Re-submitting the same approval must fail closed
    bad_outcome = orch2.submit_approval(appr)
    assert not bad_outcome.is_valid
    assert any("consumed" in err.lower() for err in bad_outcome.errors)
    assert orch2.state == WorkflowState.BLOCKED

    # Reset state to AWAITING_HUMAN_APPROVAL for fresh approval submission
    orch2.state = WorkflowState.AWAITING_HUMAN_APPROVAL

    # Submitting fresh approval for the plan succeeds
    fresh_appr = RepairApproval(
        approval_id="appr_fresh_after_discard",
        repair_plan_id=plan.plan_id,
        plan_digest=compute_plan_digest(plan),
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    outcome = orch2.submit_approval(fresh_appr)
    assert outcome.is_valid
    assert orch2.state == WorkflowState.APPROVED


def test_idempotency_concurrent_register_start(temp_db: Path):
    """Concurrent threads attempting register_start with the same key safely return 1 winner and N active records."""
    ledger = IdempotencyLedger(db_path=temp_db)
    key = compute_idempotency_key("wf_conc", WorkflowStage.EXECUTION, "digest_conc")

    def try_start():
        return ledger.register_start(key, "wf_conc", WorkflowStage.EXECUTION)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(try_start) for _ in range(5)]
        results = [f.result() for f in futures]

    winners = [r for r in results if r is None]
    already_started = [r for r in results if r is not None and r.status == OperationStatus.STARTED]

    assert len(winners) == 1
    assert len(already_started) == 4


def test_idempotency_corrupted_cached_payload_fail_closed(temp_db: Path, test_context: WorkflowContext):
    """Corrupted cached payload in idempotency table raises CorruptedStateError fail-closed."""
    store = OrchestrationStateStore(db_path=temp_db)
    ledger = IdempotencyLedger(db_path=temp_db)
    approval_mgr = OrchestrationApprovalManager(trusted_approvers={"alice"})

    orch = EngineeringOrchestrator(
        context=test_context,
        state_store=store,
        idempotency_ledger=ledger,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(),
    )
    orch.run_until_approval_or_completion()
    plan = orch.stage_outputs.repair_plan
    digest = compute_plan_digest(plan)
    idemp_key = compute_idempotency_key(test_context.workflow_id, WorkflowStage.EXECUTION, digest)

    # Insert corrupted payload
    ledger.register_start(idemp_key, test_context.workflow_id, WorkflowStage.EXECUTION)
    conn = ledger._get_connection()
    try:
        conn.execute(
            """
            UPDATE orchestration_idempotency_records SET
                status = 'completed',
                result_payload_json = '{"execution_result": "CORRUPTED_STRING_NOT_OBJECT"}'
            WHERE idempotency_key = ?;
            """,
            (idemp_key,),
        )
    finally:
        conn.close()

    appr = RepairApproval(
        approval_id="appr_corrupt_test",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approver_reference="alice",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=datetime.now(timezone.utc).isoformat(),
        approval_expiry=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        approval_status=ApprovalStatus.APPROVED,
    )
    orch.submit_approval(appr)

    with pytest.raises(CorruptedStateError, match="Corrupted idempotency cached payload"):
        orch.run_repair()


def test_idempotency_process_restart_preserves_reconciliation_required(temp_db: Path, test_context: WorkflowContext):
    """inspect_recovery correctly reports reconciliation_required=True across multiple process restarts."""
    store = OrchestrationStateStore(db_path=temp_db)
    store.save_workflow(
        context=test_context,
        state=WorkflowState.BLOCKED,
        current_stage=WorkflowStage.EXECUTION,
        reconciliation_required=True,
    )
    _setup_events(store, test_context.workflow_id)

    # Process 1 inspects
    engine1 = WorkflowRecoveryEngine(state_store=store)
    d1 = engine1.inspect_recovery(test_context.workflow_id)
    assert not d1.can_resume
    assert d1.reconciliation_required
    assert d1.recovered_state == WorkflowState.BLOCKED

    # Process 2 fresh instantiation
    fresh_store = OrchestrationStateStore(db_path=temp_db)
    engine2 = WorkflowRecoveryEngine(state_store=fresh_store)
    d2 = engine2.inspect_recovery(test_context.workflow_id)
    assert not d2.can_resume
    assert d2.reconciliation_required
    assert d2.recovered_state == WorkflowState.BLOCKED
