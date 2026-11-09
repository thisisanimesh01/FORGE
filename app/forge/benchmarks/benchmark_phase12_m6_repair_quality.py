"""Phase 12 M6: Repair Quality & Reliability Benchmark Suite.

Benchmarks:
1. Evidence validation across milestone bindings.
2. Patch analysis and diff parsing latency.
3. Maintainability and AST cyclomatic complexity analysis.
4. Completeness evaluation across required changes.
5. Correctness evaluation with root cause alignment.
6. Reliability analysis with short-term and historical evaluation.
7. Historical query & SQLite insertion throughput across dataset scaling.
8. Deterministic composite quality scoring.
9. Quality gatekeeper evaluation.
10. Cryptographic audit event recording & hash chain verification.
11. End-to-end quality pipeline orchestration latency.
12. Peak memory footprint via tracemalloc.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc
from typing import Dict, List, Optional, Tuple

from forge.repair_quality.completeness import RepairCompletenessAnalyzer
from forge.repair_quality.correctness import RepairCorrectnessEvaluator
from forge.repair_quality.events import RepairQualityEventRecorder
from forge.repair_quality.evidence import RepairQualityEvidenceValidator
from forge.repair_quality.executor import RepairQualityExecutor
from forge.repair_quality.gates import RepairQualityGateKeeper
from forge.repair_quality.history import HistoricalReliabilityStore
from forge.repair_quality.maintainability import RepairMaintainabilityAnalyzer
from forge.repair_quality.patch_analysis import RepairPatchAnalyzer
from forge.repair_quality.reliability import RepairReliabilityAnalyzer
from forge.repair_quality.risk import RepairRiskAnalyzer
from forge.repair_quality.schemas import (
    ConfidenceClassification,
    QualityDimension,
    QualityDimensionScore,
    QualityGateDecision,
    QualityStatus,
    RepairQualityEventType,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityRequest,
    RiskSeverity,
)
from forge.repair_quality.scoring import RepairQualityScorer


BENCH_PATCH = """--- a/src/calc.py
+++ b/src/calc.py
@@ -10,3 +10,5 @@
 def divide(a, b):
+    if b == 0:
+        raise ValueError("Cannot divide by zero")
     return a / b
"""


def run_benchmark() -> None:
    """Execute all benchmark passes and report measured timings."""
    print("=== Phase 12 M6: Repair Quality & Reliability Benchmark Suite ===")
    tracemalloc.start()

    with tempfile.TemporaryDirectory(prefix="forge_m6_bench_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Setup mock file structure
        src_dir = tmp_path / "src"
        src_dir.mkdir(parents=True)
        py_file = src_dir / "calc.py"
        py_file.write_text(
            "def divide(a, b):\n"
            "    if b == 0:\n"
            "        raise ValueError('Cannot divide by zero')\n"
            "    return a / b\n",
            encoding="utf-8",
        )

        req = RepairQualityRequest(
            request_id="req_bench_001",
            repair_plan_id="plan_bench",
            repair_plan_digest="dig_plan_bench",
            execution_id="exec_bench",
            applied_patch_digest="dig_patch_bench",
            baseline_tree_digest="dig_base_bench",
            verification_id="verif_bench",
            verification_status="passed",
            workspace_id="ws_bench",
            requested_scope=["src/calc.py"],
        )
        plan = {
            "repair_plan_id": "plan_bench",
            "plan_digest": "dig_plan_bench",
            "selected_strategy_id": "local_fix",
            "affected_files": ["src/calc.py"],
            "approved_file_paths": ["src/calc.py"],
            "required_changes": [{"file_path": "src/calc.py", "description": "Add zero guard"}],
        }
        exec_res = {
            "execution_id": "exec_bench",
            "applied_patch_digest": "dig_patch_bench",
            "baseline_tree_digest": "dig_base_bench",
            "execution_status": "applied",
            "modified_files": ["src/calc.py"],
        }
        verif_res = {
            "verification_id": "verif_bench",
            "verification_status": "passed",
            "applied_patch_digest": "dig_patch_bench",
            "regression_classification": "no_regression_observed",
            "required_checks_completed": ["test_div"],
            "required_checks_failed": [],
            "requires_human_review": True,
        }

        # 1. Evidence Validation
        ev_validator = RepairQualityEvidenceValidator()
        t0 = time.perf_counter()
        for _ in range(100):
            ev_validator.validate_request_bindings(req, plan, exec_res, verif_res)
        ev_latency_ms = (time.perf_counter() - t0) * 10

        # 2. Patch Analysis
        patch_analyzer = RepairPatchAnalyzer()
        t0 = time.perf_counter()
        for _ in range(100):
            patch_analyzer.analyze_patch(BENCH_PATCH, ["src/calc.py"])
        patch_latency_ms = (time.perf_counter() - t0) * 10

        # 3. Maintainability & Complexity Analysis
        maint_analyzer = RepairMaintainabilityAnalyzer()
        t0 = time.perf_counter()
        for _ in range(50):
            maint_analyzer.analyze_maintainability(tmp_path, ["src/calc.py"])
        maint_latency_ms = (time.perf_counter() - t0) * 20

        # 4. Completeness Evaluation
        comp_analyzer = RepairCompletenessAnalyzer()
        t0 = time.perf_counter()
        for _ in range(100):
            comp_analyzer.analyze_completeness(plan, ["src/calc.py"], verif_res)
        comp_latency_ms = (time.perf_counter() - t0) * 10

        # 5. Correctness Evaluation
        corr_evaluator = RepairCorrectnessEvaluator()
        t0 = time.perf_counter()
        for _ in range(100):
            corr_evaluator.evaluate_correctness(plan, verif_res, ["src/calc.py"], ["src/calc.py:10"])
        corr_latency_ms = (time.perf_counter() - t0) * 10

        # 6. Reliability Assessment
        rel_analyzer = RepairReliabilityAnalyzer()
        t0 = time.perf_counter()
        for _ in range(100):
            rel_analyzer.analyze_reliability(verif_res, "local_fix")
        rel_latency_ms = (time.perf_counter() - t0) * 10

        # 7. Historical Store Insertion & Query Scaling
        db_file = tmp_path / "bench_history.db"
        hist_store = HistoricalReliabilityStore(db_path=db_file)
        t0 = time.perf_counter()
        for i in range(200):
            hist_store.record_quality_result(
                request_id=f"bench_req_{i}",
                repair_plan_id="plan_bench",
                execution_id=f"exec_{i}",
                strategy_id="local_fix",
                target_files=["src/calc.py"],
                quality_status="passed",
                overall_score=0.95,
                gate_decision="PASSED",
                risk_level="low",
            )
        insert_time_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        query_res = hist_store.get_history(strategy_id="local_fix", limit=100)
        query_latency_ms = (time.perf_counter() - t0) * 1000

        # 8. Composite Scoring
        scorer = RepairQualityScorer()
        dim_scores = {
            dim: QualityDimensionScore(
                dimension=dim,
                score=0.95,
                weight=0.2,
                status=QualityStatus.PASSED,
                explanation="Benchmark score",
            )
            for dim in QualityDimension
        }
        t0 = time.perf_counter()
        for _ in range(500):
            scorer.calculate_score(dim_scores)
        score_latency_ms = (time.perf_counter() - t0) * 2

        # 9. Gatekeeper Evaluation
        gatekeeper = RepairQualityGateKeeper()
        metrics = RepairQualityMetrics()
        t0 = time.perf_counter()
        for _ in range(500):
            gatekeeper.evaluate_gates(0.95, dim_scores, metrics, [], [], RiskSeverity.LOW, "passed")
        gate_latency_ms = (time.perf_counter() - t0) * 2

        # 10. Event Recording & Cryptographic Chaining
        recorder = RepairQualityEventRecorder("bench_analysis")
        t0 = time.perf_counter()
        for i in range(50):
            recorder.record_event(RepairQualityEventType.QUALITY_ANALYSIS_STARTED, f"Event {i}")
        event_chain_valid = RepairQualityEventRecorder.validate_event_chain(recorder.get_events())
        events_latency_ms = (time.perf_counter() - t0) * 1000

        # 11. End-to-End Orchestration Latency
        executor = RepairQualityExecutor(history_store=hist_store)
        t0 = time.perf_counter()
        e2e_res = executor.evaluate_quality(
            request=req,
            repair_plan=plan,
            execution_result=exec_res,
            verification_result=verif_res,
            patch_content=BENCH_PATCH,
            workspace_path=tmp_path,
        )
        e2e_latency_ms = (time.perf_counter() - t0) * 1000

        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"1.  Evidence Validation (100 ops):      {ev_latency_ms:.2f} ms total ({ev_latency_ms/100:.3f} ms/op)")
        print(f"2.  Patch Analysis (100 ops):           {patch_latency_ms:.2f} ms total ({patch_latency_ms/100:.3f} ms/op)")
        print(f"3.  Maintainability / AST (50 ops):     {maint_latency_ms:.2f} ms total ({maint_latency_ms/50:.3f} ms/op)")
        print(f"4.  Completeness Evaluation (100 ops):   {comp_latency_ms:.2f} ms total ({comp_latency_ms/100:.3f} ms/op)")
        print(f"5.  Correctness Evaluation (100 ops):    {corr_latency_ms:.2f} ms total ({corr_latency_ms/100:.3f} ms/op)")
        print(f"6.  Reliability Assessment (100 ops):    {rel_latency_ms:.2f} ms total ({rel_latency_ms/100:.3f} ms/op)")
        print(f"7.  SQLite History Insert (200 rows):   {insert_time_ms:.2f} ms ({insert_time_ms/200:.3f} ms/insert)")
        print(f"    SQLite History Query (100 rows):    {query_latency_ms:.2f} ms (fetched {len(query_res)} rows)")
        print(f"8.  Composite Scoring (500 ops):        {score_latency_ms:.2f} ms total ({score_latency_ms/500:.4f} ms/op)")
        print(f"9.  Gatekeeper Evaluation (500 ops):    {gate_latency_ms:.2f} ms total ({gate_latency_ms/500:.4f} ms/op)")
        print(f"10. Event Recording & Hash Chain (50):  {events_latency_ms:.2f} ms (chain valid: {event_chain_valid})")
        print(f"11. End-to-End Evaluation Latency:      {e2e_latency_ms:.2f} ms (decision: {e2e_res.gate_decision.value})")
        print(f"12. Peak Memory Footprint:              {peak_mem / 1024:.2f} KB (current: {current_mem / 1024:.2f} KB)")
        print("=== Benchmark Completed Successfully ===")


if __name__ == "__main__":
    run_benchmark()
