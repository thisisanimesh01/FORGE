"""Phase 12 M3: Repair Planning Benchmark Suite.

Benchmarks:
1. Cold vs warm repair planning initialization.
2. Diagnosis-to-plan conversion latency.
3. Evidence collection and freshness validation.
4. Candidate strategy generation.
5. Strategy comparison.
6. Deterministic risk assessment and dependency reach.
7. Verification-plan construction.
8. Rollback-plan construction.
9. Full end-to-end deterministic planning latency (mean, median, min, max).
10. Memory usage via tracemalloc.
"""

from pathlib import Path
import statistics
import tempfile
import time
import tracemalloc

from forge.causality.engine import RootCauseAnalysisEngine
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
from forge.diagnosis.schemas import (
    DefectCategory,
    DiagnosisResult,
    DiagnosisStatus,
)
from forge.repair_planning.evidence import RepairEvidenceCollector
from forge.repair_planning.planner import RepairPlanningEngine
from forge.repair_planning.risk import RepairRiskAnalyzer
from forge.repair_planning.rollback import RollbackPlanBuilder
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairPlan,
)
from forge.repair_planning.strategies import RepairStrategyGenerator
from forge.repair_planning.validation import RepairPlanValidator
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.schemas import FindingConfidence, FindingSeverity


def run_benchmark() -> None:
    workspace = Path(".").resolve()
    print("=== FORGE Phase 12 M3 Repair Planning Benchmark Suite ===")

    tracemalloc.start()

    # -----------------------------------------------------------------------
    # 0. Cold index & engine setup
    # -----------------------------------------------------------------------
    t0 = time.perf_counter()
    inc_index = IncrementalRepositoryIndex(repo_root=workspace)
    inc_index.build()
    cold_init_ms = (time.perf_counter() - t0) * 1000.0
    print(f"0. Cold graph/index initialization: {cold_init_ms:.2f} ms")

    # Synthetic realistic fixture for benchmark repeatability
    with tempfile.TemporaryDirectory() as td:
        bench_dir = Path(td)
        pkg = bench_dir / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        calc_content = (
            "def calculate_total(items, discount):\n"
            "    if discount < 0:\n"
            "        raise ValueError('Negative discount')\n"
            "    total = sum(items)\n"
            "    return total * (1 - discount)\n"
        )
        (pkg / "calculator.py").write_text(calc_content, encoding="utf-8")

        tests_d = bench_dir / "tests"
        tests_d.mkdir()
        (tests_d / "test_calculator.py").write_text(
            "from pkg.calculator import calculate_total\ndef test_calc(): assert calculate_total([10, 20], 0.1) == 27.0\n",
            encoding="utf-8",
        )

        root_node = CausalNode(
            node_id="cnode_bench_01",
            symbol_id="pkg.calculator.calculate_total",
            file_path="pkg/calculator.py",
            start_line=1,
            end_line=5,
            node_type=CausalNodeType.FUNCTION,
            role=CausalRole.ROOT_CAUSE,
            description="calculate_total missing zero/bounds validation",
            is_verified_on_disk=True,
        )
        symptom_node = CausalNode(
            node_id="cnode_bench_02",
            symbol_id="tests.test_calculator.test_calc",
            file_path="tests/test_calculator.py",
            start_line=2,
            end_line=2,
            node_type=CausalNodeType.FUNCTION,
            role=CausalRole.SYMPTOM,
            description="test asserts failed calculation",
            is_verified_on_disk=True,
        )
        edge = CausalEdge(
            edge_id="cedge_bench_01",
            source_node_id="cnode_bench_02",
            target_node_id="cnode_bench_01",
            relationship=CausalRelationship.CALLS,
            causal_explanation="test calls calculate_total",
            confidence=FindingConfidence.HIGH,
            confidence_score=0.90,
            is_directly_verified=True,
        )
        chain = CausalChain(
            chain_id="chain_bench_01",
            root_node_id="cnode_bench_01",
            symptom_node_id="cnode_bench_02",
            node_ids=["cnode_bench_01", "cnode_bench_02"],
            edge_ids=["cedge_bench_01"],
            summary="calculate_total executes with unvalidated discount",
            confidence=FindingConfidence.HIGH,
            confidence_score=0.90,
            is_fully_verified=True,
        )
        rc_report = RootCauseReport(
            report_id="rc_bench_01",
            run_id="run_bench",
            status=CausalStatus.PROVEN,
            primary_root_cause=root_node,
            symptom_locations=[symptom_node],
            nodes=[root_node, symptom_node],
            edges=[edge],
            causal_chains=[chain],
            confidence=FindingConfidence.HIGH,
            confidence_score=0.92,
        )

        diag = DiagnosisResult(
            diagnosis_id="diag_bench_01",
            run_id="run_bench",
            input_summary="Negative discount error",
            failing_behavior="calculate_total fails with invalid discount",
            defect_category=DefectCategory.INPUT_VALIDATION_FAILURE,
            severity=FindingSeverity.HIGH,
            status=DiagnosisStatus.CONFIRMED,
            confidence=FindingConfidence.HIGH,
            confidence_score=0.90,
            confidence_explanation="Confirmed on disk",
            recommended_next_step="Add input validation",
        )

        # -------------------------------------------------------------------
        # 1. Evidence Collection & Freshness Validation
        # -------------------------------------------------------------------
        collector = RepairEvidenceCollector(workspace_path=bench_dir)
        ev_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            evs, issues = collector.collect_from_root_cause(rc_report)
            ev_runs.append((time.perf_counter() - t0) * 1000.0)
        ev_mean = statistics.mean(ev_runs)
        ev_median = statistics.median(ev_runs)
        print(f"1. Evidence collection & freshness audit: mean={ev_mean:.3f} ms, median={ev_median:.3f} ms ({len(evs)} evidence records)")

        # -------------------------------------------------------------------
        # 2. Candidate Strategy Generation
        # -------------------------------------------------------------------
        strat_gen = RepairStrategyGenerator(workspace_path=bench_dir)
        strat_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            strats = strat_gen.generate_strategies(
                root_cause_report=rc_report,
                diagnosis=diag,
                evidence_list=evs,
                failing_symbol="pkg.calculator.calculate_total",
                file_path="pkg/calculator.py",
            )
            strat_runs.append((time.perf_counter() - t0) * 1000.0)
        strat_mean = statistics.mean(strat_runs)
        strat_median = statistics.median(strat_runs)
        print(f"2. Candidate strategy generation: mean={strat_mean:.3f} ms, median={strat_median:.3f} ms ({len(strats)} candidate strategies)")

        # -------------------------------------------------------------------
        # 3. Strategy Comparison
        # -------------------------------------------------------------------
        comp_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            ranked = sorted(strats, key=lambda s: s.score, reverse=True)
            comp_runs.append((time.perf_counter() - t0) * 1000.0)
        print(f"3. Strategy comparison & ranking: mean={statistics.mean(comp_runs):.3f} ms")

        # -------------------------------------------------------------------
        # 4. Deterministic Risk Assessment
        # -------------------------------------------------------------------
        risk_analyzer = RepairRiskAnalyzer(workspace_path=bench_dir)
        risk_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            risk = risk_analyzer.assess_risk(
                affected_files=["pkg/calculator.py"],
                affected_symbols=["pkg.calculator.calculate_total"],
                proposed_approach=RepairApproach.INPUT_VALIDATION,
                root_cause_report=rc_report,
            )
            risk_runs.append((time.perf_counter() - t0) * 1000.0)
        risk_mean = statistics.mean(risk_runs)
        risk_median = statistics.median(risk_runs)
        print(f"4. Risk assessment & dependency reach: mean={risk_mean:.3f} ms, median={risk_median:.3f} ms (risk: {risk.overall_risk.value})")

        # -------------------------------------------------------------------
        # 5. Verification-Plan Construction
        # -------------------------------------------------------------------
        engine = RepairPlanningEngine(workspace_path=bench_dir)
        v_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            existing, proposed = engine._build_verification_tests(["pkg/calculator.py"], "pkg.calculator.calculate_total")
            v_runs.append((time.perf_counter() - t0) * 1000.0)
        print(f"5. Verification-plan construction: mean={statistics.mean(v_runs):.3f} ms ({len(existing)} existing, {len(proposed)} proposed)")

        # -------------------------------------------------------------------
        # 6. Rollback-Plan Construction
        # -------------------------------------------------------------------
        rb_builder = RollbackPlanBuilder(workspace_path=bench_dir)
        rb_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            rb = rb_builder.build_rollback_plan(
                affected_files=["pkg/calculator.py"],
                affected_symbols=["pkg.calculator.calculate_total"],
                approach=RepairApproach.INPUT_VALIDATION,
            )
            rb_runs.append((time.perf_counter() - t0) * 1000.0)
        print(f"6. Rollback-plan construction: mean={statistics.mean(rb_runs):.3f} ms ({len(rb.changes_to_reverse)} steps)")

        # -------------------------------------------------------------------
        # 7. Plan Invariant Validation
        # -------------------------------------------------------------------
        validator = RepairPlanValidator(workspace_path=bench_dir)
        # Generate full plan first
        plan = engine.plan_repair(root_cause_report=rc_report, diagnosis=diag)
        val_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            is_valid, errs, chk = validator.validate_plan(plan=plan, root_cause_report=rc_report)
            val_runs.append((time.perf_counter() - t0) * 1000.0)
        print(f"7. Plan invariant validation: mean={statistics.mean(val_runs):.3f} ms (valid={is_valid}, checked={len(chk)} invariants)")

        # -------------------------------------------------------------------
        # 8. Full Deterministic Planning Latency (Cold & Warm)
        # -------------------------------------------------------------------
        # Cold run
        t0 = time.perf_counter()
        cold_engine = RepairPlanningEngine(workspace_path=bench_dir)
        cold_plan = cold_engine.plan_repair(root_cause_report=rc_report, diagnosis=diag)
        cold_plan_ms = (time.perf_counter() - t0) * 1000.0

        # Warm runs
        warm_plan_runs = []
        for _ in range(25):
            t0 = time.perf_counter()
            w_plan = engine.plan_repair(root_cause_report=rc_report, diagnosis=diag)
            warm_plan_runs.append((time.perf_counter() - t0) * 1000.0)

        warm_mean = statistics.mean(warm_plan_runs)
        warm_median = statistics.median(warm_plan_runs)
        warm_min = min(warm_plan_runs)
        warm_max = max(warm_plan_runs)

        print(f"8. Full deterministic planning latency:")
        print(f"   Cold run:   {cold_plan_ms:.2f} ms")
        print(f"   Warm mean:  {warm_mean:.2f} ms")
        print(f"   Warm median:{warm_median:.2f} ms")
        print(f"   Warm range: [{warm_min:.2f} ms .. {warm_max:.2f} ms]")

    # -----------------------------------------------------------------------
    # 9. Memory usage reporting
    # -----------------------------------------------------------------------
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"9. Memory profiling: current={current_mem / (1024 * 1024):.2f} MB, peak={peak_mem / (1024 * 1024):.2f} MB")
    print("10. Inference separation: Zero external cloud or local Ollama LLM latency involved in deterministic baseline.")
    print("=== Benchmark Complete ===")


if __name__ == "__main__":
    run_benchmark()
