"""Phase 12 M5: Repair Verification Benchmark Suite.

Benchmarks:
1. Test discovery across isolated workspaces.
2. Verification plan generation and intelligent test selection.
3. Baseline verification planning and snapshot integrity audit.
4. Raw pytest output parsing and consistency validation.
5. Deterministic regression comparison.
6. Workspace and baseline integrity verification.
7. Cryptographic audit event recording and hash chaining.
8. End-to-end fake-adapter verification orchestration latency.
9. Real sandbox availability diagnosis.
10. Peak memory footprint via tracemalloc.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import statistics
import tempfile
import time
import tracemalloc
from typing import Dict, List, Optional, Tuple

from forge.repair_execution.schemas import (
    ExecutionStatus,
    RepairExecutionResult,
    RepairWorkspace,
    WorkspaceStatus,
)
from forge.repair_execution.workspace import compute_tree_digest
from forge.repair_verification.baseline import BaselineVerificationRunner
from forge.repair_verification.discovery import RepairTestDiscoverer
from forge.repair_verification.events import VerificationEventRecorder
from forge.repair_verification.executor import RepairVerificationExecutor
from forge.repair_verification.integrity import VerificationIntegrityChecker
from forge.repair_verification.regression import DeterministicRegressionEngine
from forge.repair_verification.result_parser import PytestResultParser
from forge.repair_verification.sandbox import VerificationSandbox
from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    SelectedCheck,
    SuiteExecutionSummary,
    TestScopeCategory,
    VerificationEventType,
    VerificationPlan,
    VerificationPolicy,
)
from forge.repair_verification.test_selection import IntelligentTestSelector
from forge.repair_verification.verification_plan import VerificationPlanGenerator
from forge.sandbox.adapter import Sandbox
from forge.sandbox.models import (
    ExecutionStatus as SbxStatus,
    SandboxRequest,
    SandboxResult,
)


class BenchmarkFakeSandbox:
    """Fast, deterministic fake sandbox adapter for orchestration benchmarking."""

    def is_available(self) -> bool:
        return True

    def check_health(self) -> Tuple[bool, str]:
        return True, "Benchmark fake sandbox healthy"

    def run(self, request: SandboxRequest) -> SandboxResult:
        return SandboxResult(
            request_id=request.request_id,
            run_id=request.run_id,
            status=SbxStatus.SUCCEEDED,
            exit_code=0,
            stdout="tests/test_calc.py::test_add PASSED\n=== 1 passed in 0.01s ===",
            stderr="",
            duration_ms=1.5,
        )


def run_benchmark() -> None:
    """Execute all benchmark passes and print measured metrics."""
    print("=== Phase 12 M5: Repair Verification Engine Benchmark Suite ===")
    tracemalloc.start()

    with tempfile.TemporaryDirectory(prefix="forge_m5_bench_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Setup mock workspace fixtures
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "pkg").mkdir()
        (repo_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (repo_dir / "pkg" / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        (repo_dir / "tests").mkdir()
        (repo_dir / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (repo_dir / "tests" / "test_calc.py").write_text(
            "from pkg.calc import add\ndef test_add(): assert add(1, 2) == 3\n",
            encoding="utf-8",
        )

        ws_dir = tmp_path / "ws_bench"
        shutil.copytree(repo_dir, ws_dir)
        snap_dir = tmp_path / "ws_bench_baseline"
        shutil.copytree(repo_dir, snap_dir)

        baseline_digest = compute_tree_digest(snap_dir)
        workspace = RepairWorkspace(
            workspace_id="ws_bench",
            original_repository_reference=str(repo_dir),
            isolated_workspace_path=str(ws_dir),
            baseline_commit="bench_commit",
            baseline_tree_digest=baseline_digest,
            baseline_snapshot_path=str(snap_dir),
            allowed_paths=["pkg/calc.py"],
            workspace_status=WorkspaceStatus.READY,
        )
        exec_res = RepairExecutionResult(
            execution_id="rex_bench",
            repair_plan_id="plan_bench",
            execution_status=ExecutionStatus.APPLIED,
            workspace_id="ws_bench",
            applied_patch_digest="b" * 64,
            modified_files=["pkg/calc.py"],
            diff_summary="Benchmark diff",
            verification_pending=True,
            approval_reference="appr_bench",
        )

        # Pass 1: Test Discovery
        discoverer = RepairTestDiscoverer(workspace_path=ws_dir)
        t_disc = []
        for _ in range(20):
            t0 = time.perf_counter()
            res = discoverer.discover_tests(modified_files=["pkg/calc.py"])
            t_disc.append((time.perf_counter() - t0) * 1000.0)
        print(f"1. Test discovery latency: mean={statistics.mean(t_disc):.3f} ms, median={statistics.median(t_disc):.3f} ms")

        # Pass 2: Verification Plan Generation
        plan_gen = VerificationPlanGenerator()
        t_plan = []
        for _ in range(20):
            t0 = time.perf_counter()
            plan = plan_gen.generate_plan(execution_result=exec_res, workspace=workspace)
            t_plan.append((time.perf_counter() - t0) * 1000.0)
        print(f"2. Plan generation latency: mean={statistics.mean(t_plan):.3f} ms, median={statistics.median(t_plan):.3f} ms")

        # Pass 3: Baseline verification planning & snapshot verification
        t_base_prep = []
        for _ in range(20):
            t0 = time.perf_counter()
            is_ok, _ = VerificationIntegrityChecker.verify_baseline_snapshot(snap_dir, baseline_digest)
            t_base_prep.append((time.perf_counter() - t0) * 1000.0)
        print(f"3. Baseline snapshot audit latency: mean={statistics.mean(t_base_prep):.3f} ms")

        # Pass 4: Raw test result parsing
        sample_check = plan.selected_checks[0]
        sample_sbx_res = SandboxResult(
            request_id="r_bench",
            run_id="run_bench",
            status=SbxStatus.SUCCEEDED,
            exit_code=0,
            stdout="tests/test_calc.py::test_add PASSED\n=== 1 passed, 0 failed in 0.05s ===",
            duration_ms=50.0,
        )
        t_parse = []
        for _ in range(50):
            t0 = time.perf_counter()
            parsed = PytestResultParser.parse_result(check=sample_check, sandbox_res=sample_sbx_res)
            t_parse.append((time.perf_counter() - t0) * 1000.0)
        print(f"4. Test result parsing latency: mean={statistics.mean(t_parse):.3f} ms")

        # Pass 5: Deterministic regression comparison
        base_summary = SuiteExecutionSummary(
            execution_type="baseline",
            workspace_id="ws_bench",
            tree_digest=baseline_digest,
            checks=[parsed],
            all_passed=True,
        )
        rep_summary = SuiteExecutionSummary(
            execution_type="repaired",
            workspace_id="ws_bench",
            tree_digest=baseline_digest,
            checks=[parsed],
            all_passed=True,
        )
        t_regr = []
        for _ in range(50):
            t0 = time.perf_counter()
            reg_class, findings = DeterministicRegressionEngine.analyze_regressions(
                baseline_summary=base_summary,
                repaired_summary=rep_summary,
            )
            t_regr.append((time.perf_counter() - t0) * 1000.0)
        print(f"5. Regression comparison latency: mean={statistics.mean(t_regr):.3f} ms")

        # Pass 6: Workspace & repository integrity verification
        t_integ = []
        for _ in range(20):
            t0 = time.perf_counter()
            VerificationIntegrityChecker.verify_original_repo_unmodified(repo_dir, baseline_digest)
            VerificationIntegrityChecker.verify_workspace_integrity(ws_dir, ["pkg/calc.py"])
            t_integ.append((time.perf_counter() - t0) * 1000.0)
        print(f"6. Integrity audit latency: mean={statistics.mean(t_integ):.3f} ms")

        # Pass 7: Cryptographic audit event recording
        recorder = VerificationEventRecorder()
        t_events = []
        for i in range(50):
            t0 = time.perf_counter()
            recorder.record_event(
                verification_id="vrf_bench",
                event_type=VerificationEventType.REPAIR_EXECUTION_STARTED,
                action_description=f"Benchmark audit step {i}",
            )
            t_events.append((time.perf_counter() - t0) * 1000.0)
        print(f"7. Audit event recording latency: mean={statistics.mean(t_events):.3f} ms")

        # Pass 8: End-to-end orchestration (Fake adapter)
        fake_sbx = BenchmarkFakeSandbox()
        v_sbx = VerificationSandbox(sandbox=fake_sbx)
        executor = RepairVerificationExecutor(sandbox=v_sbx)
        t_e2e = []
        for _ in range(5):
            t0 = time.perf_counter()
            v_res, upd_exec = executor.verify_repair(execution_result=exec_res, workspace=workspace)
            t_e2e.append((time.perf_counter() - t0) * 1000.0)
        print(
            f"8. End-to-end fake-adapter orchestration latency: "
            f"mean={statistics.mean(t_e2e):.2f} ms, min={min(t_e2e):.2f} ms, max={max(t_e2e):.2f} ms"
        )

        # Pass 9: Real container sandbox diagnosis
        real_sbx = VerificationSandbox()
        is_real_avail, real_msg = real_sbx.check_health()
        print(
            f"9. Real Docker sandbox environment status: "
            f"available={is_real_avail} ({real_msg})"
        )

        # Pass 10: Memory footprint
        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"10. Memory footprint: current={current_mem / 1024:.2f} KB, peak={peak_mem / 1024:.2f} KB")

    print("=== Phase 12 M5 Benchmark Completed Successfully ===")


if __name__ == "__main__":
    run_benchmark()
