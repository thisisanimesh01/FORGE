"""Phase 12 M4: Controlled Autonomous Repair Benchmark Suite.

Benchmarks:
1. Cold vs warm isolated workspace allocation.
2. Human approval validation against all mandatory invariants.
3. Patch generation from RepairPlan contracts.
4. Unified diff validation (syntax, hunk headers, scope, boundaries).
5. Pure in-memory patch application (zero subprocess).
6. Post-application workspace integrity verification.
7. Baseline tree digest computation.
8. Workspace rollback and baseline recovery.
9. Audit event recording with secret redaction.
10. Full end-to-end controlled repair execution latency.
11. Peak memory consumption via tracemalloc.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc
from typing import List, Optional, Tuple

from forge.repair_execution.approval import (
    RepairApprovalValidator,
    compute_plan_digest,
)
from forge.repair_execution.events import RepairEventRecorder
from forge.repair_execution.executor import ControlledRepairExecutor
from forge.repair_execution.integrity import WorkspaceIntegrityChecker
from forge.repair_execution.patch_applier import ControlledPatchApplier
from forge.repair_execution.patch_generator import RepairPatchGenerator
from forge.repair_execution.patch_validator import (
    RepairPatchValidator,
    compute_patch_digest,
)
from forge.repair_execution.rollback import WorkspaceRollbackManager
from forge.repair_execution.schemas import (
    ApprovalStatus,
    RepairApproval,
    RepairPatch,
)
from forge.repair_execution.workspace import (
    RepairWorkspaceManager,
    compute_tree_digest,
)
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairEvidence,
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
from forge.schemas import FindingConfidence


def _create_synthetic_repo(base_dir: Path) -> Path:
    """Create a realistic benchmark repository fixture."""
    repo = base_dir / "bench_repo"
    repo.mkdir(parents=True, exist_ok=True)
    pkg = repo / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    calc_lines = [
        "def compute_interest(principal, rate, time):\n",
        "    if principal < 0:\n",
        "        raise ValueError('Negative principal')\n",
        "    if rate < 0:\n",
        "        raise ValueError('Negative rate')\n",
        "    return principal * rate * time\n\n",
        "def calculate_ratio(numerator, denominator):\n",
        "    return numerator / denominator\n",
    ]
    (pkg / "calculator.py").write_text("".join(calc_lines), encoding="utf-8")

    tests = repo / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_calc.py").write_text("def test_dummy(): pass\n", encoding="utf-8")
    return repo


def _create_benchmark_plan(repo: Path) -> Tuple[RepairPlan, RepairApproval]:
    """Create a valid RepairPlan and matching RepairApproval for benchmarks."""
    strategy = RepairStrategy(
        strategy_id="strat_zero_check",
        title="Zero denominator guard",
        description="Add guard before division",
        approach=RepairApproach.INPUT_VALIDATION,
        affected_files=["pkg/calculator.py"],
        affected_symbols=["calculate_ratio"],
        supporting_evidence=["evi_01"],
        expected_benefits=["Prevents ZeroDivisionError"],
        risks=[],
        complexity=StrategyComplexity.LOW,
        invasiveness=StrategyInvasiveness.MINIMAL,
        reversibility=StrategyReversibility.HIGH,
        test_requirements=["tests/test_calc.py"],
        is_recommended=True,
        score=0.95,
    )

    req_change = RequiredChange(
        file_path="pkg/calculator.py",
        symbol_id="calculate_ratio",
        start_line=7,
        end_line=9,
        change_type="add_guard",
        description="Add guard against denominator == 0",
        rationale="Prevents ZeroDivisionError",
    )

    risk = RepairRiskAssessment(
        overall_risk=RiskSeverity.LOW,
        likelihood=RiskLikelihood.LOW,
        risk_score=0.1,
        affected_components=["pkg.calculator"],
        potential_regressions=[],
        security_implications=[],
        data_integrity_implications=[],
        dependency_implications=[],
        explanation="Low risk check",
        supporting_evidence=["evi_01"],
    )

    verif = VerificationPlan(
        existing_targeted_tests=["tests/test_calc.py"],
        recommended_new_tests=[],
        regression_test_scope=["tests/"],
        security_checks=[],
        static_analysis_requirements=[],
        expected_verification_criteria=["Passes all tests"],
    )

    rollback = RollbackPlan(
        changes_to_reverse=["Revert guard in pkg/calculator.py"],
        files_and_symbols_involved=["pkg/calculator.py"],
        dependencies_to_restore=[],
        data_configuration_considerations=[],
        rollback_triggers=[],
        known_limitations=[],
    )

    evidence = [
        RepairEvidence(
            evidence_id="evi_01",
            evidence_source="diagnosis",
            file_path="pkg/calculator.py",
            symbol_reference="calculate_ratio",
            start_line=7,
            end_line=9,
            evidence_type="source_excerpt",
            is_fresh=True,
            relationship_to_defect="Division without guard",
        )
    ]

    plan = RepairPlan(
        plan_id="plan_bench_001",
        finding_id="find_bench",
        root_cause_reference="node_bench",
        status=RepairPlanStatus.ACTIONABLE,
        summary="Add check for zero division",
        problem_statement="calculate_ratio division by zero",
        repair_objective="Raise ValueError on zero denominator",
        selected_strategy_id="strat_zero_check",
        candidate_strategies=[strategy],
        affected_files=["pkg/calculator.py"],
        affected_symbols=["calculate_ratio"],
        required_changes=[req_change],
        preconditions=[],
        dependencies=[],
        risk_assessment=risk,
        expected_side_effects=[],
        verification_plan=verif,
        rollback_plan=rollback,
        supporting_evidence=evidence,
        confidence=FindingConfidence.HIGH,
        limitations="Guards ratio function only",
        approval_required=True,
        execution_permitted=False,
    )

    digest = compute_plan_digest(plan)
    now = datetime.now(timezone.utc)
    approval = RepairApproval(
        approval_id="appr_bench_001",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=["pkg/calculator.py"],
        approval_timestamp=now.isoformat(),
        approval_expiry=(now + timedelta(hours=24)).isoformat(),
        approver_reference="benchmark_engineer@example.com",
        approval_status=ApprovalStatus.APPROVED,
    )

    return plan, approval


def run_benchmark() -> None:
    print("=== FORGE Phase 12 M4 Controlled Repair Execution Benchmark Suite ===")
    tracemalloc.start()

    with tempfile.TemporaryDirectory() as td:
        base_dir = Path(td)
        repo = _create_synthetic_repo(base_dir)
        plan, approval = _create_benchmark_plan(repo)

        ws_dir = base_dir / "workspaces"
        ws_mgr = RepairWorkspaceManager(original_repo_path=repo, base_workspaces_dir=ws_dir)

        # 1. Cold workspace allocation
        t0 = time.perf_counter()
        ws = ws_mgr.allocate_workspace(allowed_paths=["pkg/calculator.py"], plan_id=plan.plan_id)
        cold_ws_ms = (time.perf_counter() - t0) * 1000.0
        print(f"1. Cold workspace allocation: {cold_ws_ms:.2f} ms")

        # 2. Warm workspace allocation
        warm_times = []
        for i in range(5):
            t0 = time.perf_counter()
            temp_ws = ws_mgr.allocate_workspace(allowed_paths=["pkg/calculator.py"], plan_id=f"plan_warm_{i}")
            warm_times.append((time.perf_counter() - t0) * 1000.0)
            ws_mgr.cleanup_workspace(temp_ws.workspace_id)
        warm_ws_mean = statistics.mean(warm_times)
        print(f"2. Warm workspace allocation: mean={warm_ws_mean:.2f} ms")

        # 3. Human approval validation
        appr_val = RepairApprovalValidator()
        appr_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            res = appr_val.validate_approval(approval, plan)
            appr_times.append((time.perf_counter() - t0) * 1000.0)
        appr_mean = statistics.mean(appr_times)
        print(f"3. Approval validation latency: mean={appr_mean:.3f} ms, median={statistics.median(appr_times):.3f} ms")

        # 4. Patch generation
        patch_gen = RepairPatchGenerator(workspace_path=Path(ws.isolated_workspace_path))
        patch_times = []
        generated_patch = None
        for _ in range(20):
            t0 = time.perf_counter()
            generated_patch = patch_gen.generate_patch(plan)
            patch_times.append((time.perf_counter() - t0) * 1000.0)
        patch_mean = statistics.mean(patch_times)
        print(f"4. Patch generation latency: mean={patch_mean:.2f} ms, lines_added={generated_patch.added_lines}")

        # 5. Patch validation
        patch_val = RepairPatchValidator(workspace_path=Path(ws.isolated_workspace_path))
        val_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            val_res = patch_val.validate_patch(
                unified_diff=generated_patch.unified_diff,
                allowed_paths=["pkg/calculator.py"],
                expected_digest=generated_patch.patch_digest,
            )
            val_times.append((time.perf_counter() - t0) * 1000.0)
        val_mean = statistics.mean(val_times)
        print(f"5. Patch validation latency: mean={val_mean:.3f} ms, median={statistics.median(val_times):.3f} ms")

        # 6. In-memory patch application
        applier = ControlledPatchApplier(workspace_path=Path(ws.isolated_workspace_path))
        diff = (
            "--- a/pkg/calculator.py\n"
            "+++ b/pkg/calculator.py\n"
            "@@ -8,2 +8,4 @@\n"
            " def calculate_ratio(numerator, denominator):\n"
            "+    if denominator == 0:\n"
            "+        raise ValueError('division by zero')\n"
            "     return numerator / denominator\n"
        )
        custom_patch = RepairPatch(
            patch_id="pat_bench",
            repair_plan_id=plan.plan_id,
            strategy_id=plan.selected_strategy_id,
            unified_diff=diff,
            affected_files=["pkg/calculator.py"],
            patch_digest=compute_patch_digest(diff),
        )
        t0 = time.perf_counter()
        app_outcome = applier.apply_patch(
            workspace_path=Path(ws.isolated_workspace_path),
            patch=custom_patch,
            allowed_files=["pkg/calculator.py"],
        )
        app_ms = (time.perf_counter() - t0) * 1000.0
        print(f"6. In-memory patch application: {app_ms:.2f} ms (success={app_outcome.success})")

        # 7. Post-application workspace integrity verification
        checker = WorkspaceIntegrityChecker()
        t0 = time.perf_counter()
        int_report = checker.verify_integrity(ws, allowed_paths=["pkg/calculator.py"])
        int_ms = (time.perf_counter() - t0) * 1000.0
        print(f"7. Workspace integrity verification: {int_ms:.2f} ms (valid={int_report.is_valid})")

        # 8. Rollback and baseline recovery
        rollback_mgr = WorkspaceRollbackManager()
        t0 = time.perf_counter()
        rb_status, restored, post_digest = rollback_mgr.rollback(
            workspace=ws,
            modified_files=["pkg/calculator.py"],
            backups=app_outcome.backups,
        )
        rb_ms = (time.perf_counter() - t0) * 1000.0
        print(f"8. Rollback and recovery: {rb_ms:.2f} ms (status={rb_status.value}, restored={len(restored)} files)")

        # 9. Audit event recording
        event_recorder = RepairEventRecorder()
        ev_times = []
        for seq in range(50):
            t0 = time.perf_counter()
            event_recorder.record_event(
                execution_id="rex_bench",
                event_type="patch_validated",
                action_description=f"Recorded benchmark event {seq} with token sk-abcdef1234567890abcdef123456",
            )
            ev_times.append((time.perf_counter() - t0) * 1000.0)
        ev_mean = statistics.mean(ev_times)
        print(f"9. Audit event recording latency: mean={ev_mean:.3f} ms")

        # 10. End-to-end controlled execution
        executor = ControlledRepairExecutor(repo_path=repo, workspace_manager=ws_mgr)
        e2e_times = []
        for iter_idx in range(5):
            iter_ws = ws_mgr.allocate_workspace(allowed_paths=["pkg/calculator.py"], plan_id=f"plan_e2e_{iter_idx}")
            t0 = time.perf_counter()
            exec_result = executor.execute_repair(
                plan=plan,
                approval=approval,
                workspace=iter_ws,
                custom_patch=custom_patch,
            )
            e2e_ms = (time.perf_counter() - t0) * 1000.0
            e2e_times.append(e2e_ms)
            ws_mgr.cleanup_workspace(iter_ws.workspace_id)

        print(
            f"10. End-to-end controlled execution: "
            f"mean={statistics.mean(e2e_times):.2f} ms, "
            f"median={statistics.median(e2e_times):.2f} ms, "
            f"min={min(e2e_times):.2f} ms, "
            f"max={max(e2e_times):.2f} ms"
        )

        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"11. Benchmark memory footprint: current={current_mem / 1024:.2f} KB, peak={peak_mem / 1024:.2f} KB")

    print("=== Phase 12 M4 Controlled Repair Benchmark Completed Successfully ===")


if __name__ == "__main__":
    run_benchmark()
