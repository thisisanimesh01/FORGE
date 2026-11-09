"""Phase 13 M1: Engineering Orchestration Microbenchmark Suite.

Measures latency and memory footprint across the core orchestration components:
- Workflow initialization and state transitions.
- Append-only SHA-256 cryptographic event chaining and verification.
- Human approval boundary validation against canonical plan digests.
- End-to-end simulated workflow execution through all 7 stages.
- Final engineering report generation.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import time
import tracemalloc
from typing import Any, Dict

from forge.diagnosis.schemas import DiagnosisInput
from forge.repair_execution.approval import compute_plan_digest
from forge.repair_execution.schemas import RepairApproval
from forge.orchestration.adapters import (
    FakeDiagnosisAdapter,
    FakeExecutionAdapter,
    FakePlanningAdapter,
    FakeQualityAdapter,
    FakeRootCauseAdapter,
    FakeVerificationAdapter,
)
from forge.orchestration.approval import OrchestrationApprovalManager
from forge.orchestration.events import OrchestrationEventRecorder, OrchestrationEventType
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.report import EngineeringReportGenerator
from forge.orchestration.schemas import WorkflowContext, WorkflowStage, WorkflowState
from forge.orchestration.state import validate_transition


def benchmark_orchestration() -> Dict[str, Any]:
    """Run comprehensive performance benchmarks for Phase 13 M1 Orchestration."""
    tracemalloc.start()
    t_start = time.perf_counter()

    results: Dict[str, Any] = {}

    # 1. State Transition Validation Benchmark (1,000 ops)
    t0 = time.perf_counter()
    for _ in range(1000):
        validate_transition(WorkflowState.CREATED, WorkflowState.DIAGNOSING)
        validate_transition(WorkflowState.DIAGNOSIS_COMPLETED, WorkflowState.ROOT_CAUSE_ANALYSIS)
        validate_transition(WorkflowState.ROOT_CAUSE_COMPLETED, WorkflowState.PLANNING)
        validate_transition(WorkflowState.APPROVED, WorkflowState.REPAIR_EXECUTING)
        validate_transition(WorkflowState.REPAIR_COMPLETED, WorkflowState.VERIFICATION_RUNNING)
        validate_transition(WorkflowState.VERIFICATION_COMPLETED, WorkflowState.QUALITY_EVALUATION)
        validate_transition(WorkflowState.QUALITY_EVALUATION, WorkflowState.COMPLETED)
    t_transitions = (time.perf_counter() - t0) * 1000
    results["transition_validation_7000_ops_ms"] = round(t_transitions, 3)

    # 2. Cryptographic Event Recording & Chaining (200 events)
    recorder = OrchestrationEventRecorder()
    t0 = time.perf_counter()
    for seq in range(200):
        recorder.record_event(
            workflow_id="wf_benchmark_001",
            event_type=OrchestrationEventType.STAGE_COMPLETED,
            state=WorkflowState.REPAIR_COMPLETED,
            action_description=f"Action step {seq}",
            stage=WorkflowStage.EXECUTION,
            details={"step": seq, "status": "ok"},
        )
    t_events = (time.perf_counter() - t0) * 1000
    results["event_recording_200_events_ms"] = round(t_events, 3)

    # Verify event chain
    events = recorder.get_events("wf_benchmark_001")
    t0 = time.perf_counter()
    is_valid, err = recorder.verify_event_chain(events)
    t_verify = (time.perf_counter() - t0) * 1000
    assert is_valid, f"Event chain verification failed: {err}"
    results["event_chain_verification_ms"] = round(t_verify, 3)

    # 3. Approval Boundary Validation (500 ops)
    approval_mgr = OrchestrationApprovalManager()
    plan_adapter = FakePlanningAdapter()
    ctx = WorkflowContext(repository_path=str(Path(".").resolve()))
    plan = plan_adapter.run_planning(ctx)
    plan_digest = compute_plan_digest(plan)

    now_iso = datetime.now(timezone.utc).isoformat()
    expiry_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    approval = RepairApproval(
        approval_id="appr_benchmark_001",
        repair_plan_id=plan.plan_id,
        plan_digest=plan_digest,
        approver_reference="security_lead",
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=plan.affected_files,
        approval_timestamp=now_iso,
        approval_expiry=expiry_iso,
    )

    t0 = time.perf_counter()
    for _ in range(500):
        outcome = approval_mgr.verify_approval(approval, plan)
        assert outcome.is_valid
    t_approval = (time.perf_counter() - t0) * 1000
    results["approval_verification_500_ops_ms"] = round(t_approval, 3)

    # 4. End-to-End Orchestration (50 complete workflows through all 7 stages)
    t0 = time.perf_counter()
    for _ in range(50):
        orchestrator = EngineeringOrchestrator(
            context=ctx,
            diagnosis_adapter=FakeDiagnosisAdapter(),
            root_cause_adapter=FakeRootCauseAdapter(),
            planning_adapter=FakePlanningAdapter(),
            execution_adapter=FakeExecutionAdapter(),
            verification_adapter=FakeVerificationAdapter(),
            quality_adapter=FakeQualityAdapter(),
        )
        orchestrator.run_until_approval_or_completion()
        assert orchestrator.state == WorkflowState.AWAITING_HUMAN_APPROVAL

        active_plan = orchestrator.stage_outputs.repair_plan
        assert active_plan is not None
        active_digest = compute_plan_digest(active_plan)
        valid_appr = RepairApproval(
            approval_id=f"appr_benchmark_{_}",
            repair_plan_id=active_plan.plan_id,
            plan_digest=active_digest,
            approver_reference="benchmark_approver",
            approved_strategy_id=active_plan.selected_strategy_id,
            approved_file_paths=active_plan.affected_files,
            approval_timestamp=now_iso,
            approval_expiry=expiry_iso,
        )
        orchestrator.submit_approval(valid_appr)
        assert orchestrator.state == WorkflowState.APPROVED

        orchestrator.run_until_approval_or_completion()
        assert orchestrator.state == WorkflowState.COMPLETED
        result = orchestrator.get_result()
        assert result.state == WorkflowState.COMPLETED
        assert result.requires_human_review is True

    t_e2e = (time.perf_counter() - t0) * 1000
    results["e2e_50_workflows_ms"] = round(t_e2e, 3)
    results["e2e_per_workflow_latency_ms"] = round(t_e2e / 50, 3)

    # 5. Report Generation (200 ops)
    sample_result = orchestrator.get_result()
    t0 = time.perf_counter()
    for _ in range(200):
        EngineeringReportGenerator.generate_markdown(sample_result)
    t_report = (time.perf_counter() - t0) * 1000
    results["report_generation_200_ops_ms"] = round(t_report, 3)

    # Memory Footprint
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    results["peak_memory_kb"] = round(peak_mem / 1024, 2)
    results["total_benchmark_time_ms"] = round((time.perf_counter() - t_start) * 1000, 2)

    return results


if __name__ == "__main__":
    res = benchmark_orchestration()
    print("=" * 60)
    print("FORGE Phase 13 M1 Orchestration Microbenchmark Results")
    print("=" * 60)
    for k, v in res.items():
        print(f"  {k:<40}: {v}")
    print("=" * 60)
