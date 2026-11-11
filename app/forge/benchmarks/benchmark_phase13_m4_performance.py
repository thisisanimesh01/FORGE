"""Reproducible performance benchmark suite for FORGE Phase 13 Milestone 4.

Covers:
1. All 10 repository & workload profiles:
   - small
   - medium
   - large
   - many_small_files
   - large_files
   - deeply_nested
   - complex_deps
   - high_change_freq
   - large_graph
   - concurrent_workflows
2. Static indexing, incremental indexing (cold / warm / incremental delta),
   call & module graph construction, cycle detection caching, change impact,
   LLM context construction (cold & repeated cached lookups), shared-index
   diagnosis -> root cause -> planning pipeline, repair workspace allocation,
   test discovery, and SQLite connection pool throughput/contention.
3. Optional local Ollama HTTP connection-reuse latency benchmark
   (`--include-ollama`, disabled by default for deterministic offline runs).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Callable, Dict, List, Optional, Tuple

from forge.analysis.impact.analyzer import ChangeImpactAnalyzer
from forge.causality.engine import RootCauseAnalysisEngine
from forge.diagnosis.engine import EvidenceDrivenDiagnosisEngine
from forge.diagnosis.schemas import DiagnosisInput, TestFailureReport
from forge.llm.context import RepositoryContextBuilder
from forge.orchestration.events import OrchestrationEventRecorder, OrchestrationEventType
from forge.orchestration.idempotency import IdempotencyLedger, compute_idempotency_key
from forge.orchestration.persistence import OrchestrationStateStore
from forge.orchestration.recovery import WorkflowRecoveryEngine
from forge.orchestration.schemas import WorkflowContext, WorkflowStage, WorkflowState
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.workspace import RepairWorkspaceManager
from forge.repair_planning.planner import RepairPlanningEngine
from forge.repair_verification.discovery import RepairTestDiscoverer
from forge.repository.acquisition import acquire_repository, cleanup_repository, run_git_command
from forge.repository.cache import IndexCacheManager
from forge.repository.call_graph import build_call_graph
from forge.repository.detector import detect_repository
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.indexer import build_symbol_index
from forge.repository.module_graph import build_module_dependency_graph


@dataclass(frozen=True)
class RepoProfileSpec:
    """Configuration for a synthetic benchmark repository profile."""

    name: str
    num_modules: int
    lines_per_file: int
    depth: int
    fanout_imports: int = 1
    cyclic_links: bool = False
    add_git_commit: bool = True


REPO_PROFILE_SPECS: Dict[str, RepoProfileSpec] = {
    "small": RepoProfileSpec(name="small", num_modules=15, lines_per_file=35, depth=2),
    "medium": RepoProfileSpec(name="medium", num_modules=80, lines_per_file=35, depth=2),
    "large": RepoProfileSpec(name="large", num_modules=250, lines_per_file=35, depth=2),
    "many_small_files": RepoProfileSpec(
        name="many_small_files", num_modules=320, lines_per_file=2, depth=2
    ),
    "large_files": RepoProfileSpec(
        name="large_files", num_modules=12, lines_per_file=300, depth=2
    ),
    "deeply_nested": RepoProfileSpec(
        name="deeply_nested", num_modules=60, lines_per_file=20, depth=7
    ),
    "complex_deps": RepoProfileSpec(
        name="complex_deps",
        num_modules=80,
        lines_per_file=20,
        depth=2,
        fanout_imports=4,
        cyclic_links=True,
    ),
    "high_change_freq": RepoProfileSpec(
        name="high_change_freq", num_modules=60, lines_per_file=25, depth=2
    ),
    "large_graph": RepoProfileSpec(
        name="large_graph",
        num_modules=120,
        lines_per_file=25,
        depth=2,
        fanout_imports=3,
    ),
}

ALL_PROFILES: Tuple[str, ...] = tuple(list(REPO_PROFILE_SPECS.keys()) + ["concurrent_workflows"])


def compute_stats(samples_ms: List[float]) -> Dict[str, float]:
    """Compute summary latency statistics (median, P95, min, max, mean) in ms."""
    if not samples_ms:
        return {"median_ms": 0.0, "p95_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0, "mean_ms": 0.0}
    ordered = sorted(samples_ms)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        median = ordered[mid]
    else:
        median = (ordered[mid - 1] + ordered[mid]) / 2.0
    p95_idx = min(n - 1, max(0, math.ceil(0.95 * n) - 1))
    mean = sum(ordered) / n
    return {
        "median_ms": round(median, 2),
        "p95_ms": round(ordered[p95_idx], 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
        "mean_ms": round(mean, 2),
    }


def _collect_environment_metadata() -> Dict[str, Any]:
    """Capture hardware, OS, Python, and Git metadata for reproducibility."""
    git_ver = "unknown"
    try:
        res = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            git_ver = res.stdout.strip()
    except Exception:
        pass
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "git_version": git_ver,
    }


def create_profile_repository(root: Path, spec: RepoProfileSpec) -> Tuple[Path, List[str]]:
    """Create a deterministic synthetic git repository matching the given profile spec."""
    root.mkdir(parents=True, exist_ok=True)
    src_dir = root / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "synth"\nversion = "0.1.0"\n', encoding="utf-8")

    rel_files: List[str] = []
    num_pkgs = max(1, min(10, spec.num_modules // 5 or 1))
    for p_idx in range(num_pkgs):
        pkg_dir = src_dir
        for d in range(spec.depth):
            pkg_dir = pkg_dir / f"pkg_{p_idx}_{d}"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    mod_dotted_names: List[str] = []
    for i in range(spec.num_modules):
        p_idx = i % num_pkgs
        parts = ["src"] + [f"pkg_{p_idx}_{d}" for d in range(spec.depth)] + [f"mod_{i:04d}"]
        mod_dotted_names.append(".".join(parts))

    for i in range(spec.num_modules):
        p_idx = i % num_pkgs
        pkg_dir = src_dir
        parts = ["src"]
        for d in range(spec.depth):
            seg = f"pkg_{p_idx}_{d}"
            pkg_dir = pkg_dir / seg
            parts.append(seg)
        fname = f"mod_{i:04d}.py"
        rel = "/".join(parts + [fname])
        rel_files.append(rel)

        lines: List[str] = ['"""Synthetic module for benchmarking."""\n']
        imported_targets: List[int] = []
        for offset in range(1, spec.fanout_imports + 1):
            if i - offset >= 0:
                imported_targets.append(i - offset)
        if spec.cyclic_links and spec.num_modules >= 4 and i % 5 == 0 and i + 1 < spec.num_modules:
            imported_targets.append(i + 1)

        for tgt in imported_targets:
            lines.append(f"from {mod_dotted_names[tgt]} import compute_{tgt:04d}\n")
        if not imported_targets:
            lines.append("import math\n")

        lines.append(f"\nclass Worker_{i:04d}:\n")
        lines.append("    def process(self, x: int) -> int:\n")
        if i == 0:
            lines.append("        if x <= 0:\n")
            lines.append("            return 0\n")
            lines.append("        return x - 1\n")
        else:
            call_target = imported_targets[0] if imported_targets else 0
            if call_target == i:
                lines.append("        return x + 1\n")
            else:
                lines.append(f"        return compute_{call_target:04d}(x) + 1\n")

        lines.append(f"\ndef compute_{i:04d}(val: int) -> int:\n")
        lines.append(f"    w = Worker_{i:04d}()\n")
        lines.append("    return w.process(val)\n")

        for p in range(spec.lines_per_file):
            lines.append(f"def helper_{i}_{p}(n: int) -> int:\n    return n + {p}\n\n")

        (pkg_dir / fname).write_text("".join(lines), encoding="utf-8")

    test_count = max(2, min(30, spec.num_modules // 10 or 2))
    for t in range(test_count):
        (tests_dir / f"test_mod_{t:03d}.py").write_text(
            f"from {mod_dotted_names[0]} import compute_0000\n\n"
            f"def test_compute_{t:03d}():\n"
            f"    assert compute_0000(5) == 5\n",
            encoding="utf-8",
        )

    if spec.add_git_commit:
        run_git_command(["init"], cwd=root)
        run_git_command(["config", "user.name", "Bench User"], cwd=root)
        run_git_command(["config", "user.email", "bench@forge.local"], cwd=root)
        run_git_command(["add", "."], cwd=root)
        run_git_command(["commit", "-m", "Initial synthetic repo"], cwd=root)
    return root, rel_files


def measure_iterations(
    stage_name: str,
    fn: Callable[[], Any],
    iterations: int = 3,
    warmup: int = 1,
) -> Tuple[Any, Dict[str, Any]]:
    """Measure warmup + iterations with median, P95, peak memory, and throughput."""
    last_res: Any = None
    for _ in range(max(0, warmup)):
        last_res = fn()

    samples_ms: List[float] = []
    peak_bytes = 0
    for _ in range(max(1, iterations)):
        tracemalloc.start()
        t0 = time.perf_counter()
        last_res = fn()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        samples_ms.append(elapsed_ms)
        if peak > peak_bytes:
            peak_bytes = peak

    stats = compute_stats(samples_ms)
    median_s = stats["median_ms"] / 1000.0
    stats.update(
        {
            "stage": stage_name,
            "iterations": max(1, iterations),
            "warmup": max(0, warmup),
            "peak_memory_kb": round(peak_bytes / 1024.0, 1),
            "throughput_ops_per_sec": round((1.0 / median_s), 2) if median_s > 0 else 0.0,
        }
    )
    return last_res, stats


def benchmark_repo_profile(
    spec: RepoProfileSpec,
    iterations: int = 3,
    warmup: int = 1,
) -> Dict[str, Any]:
    """Run full-pipeline benchmark for a single synthetic repository profile."""
    profile_res: Dict[str, Any] = {
        "profile": spec.name,
        "spec": {
            "num_modules": spec.num_modules,
            "lines_per_file": spec.lines_per_file,
            "depth": spec.depth,
            "fanout_imports": spec.fanout_imports,
            "cyclic_links": spec.cyclic_links,
        },
        "stages": [],
    }

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src_repo, rel_files = create_profile_repository(tmp / "upstream", spec)
        ws_root = tmp / "workspaces"
        cache_dir = tmp / "forge_cache"

        acquired, acq_stat = measure_iterations(
            "acquire_repository",
            lambda: acquire_repository(str(src_repo), custom_workspace=ws_root, allow_local=True),
            iterations=1,
            warmup=0,
        )
        profile_res["stages"].append(acq_stat)
        ws = acquired.workspace_path

        try:
            intel, det_stat = measure_iterations(
                "detect_repository",
                lambda: detect_repository(ws),
                iterations=iterations,
                warmup=warmup,
            )
            det_stat["python_files"] = intel.total_python_files
            det_stat["total_lines"] = intel.total_lines
            profile_res["stages"].append(det_stat)

            sym_idx, sym_stat = measure_iterations(
                "build_symbol_index",
                lambda: build_symbol_index(ws),
                iterations=iterations,
                warmup=warmup,
            )
            sym_stat["symbols"] = len(sym_idx.symbols)
            profile_res["stages"].append(sym_stat)

            cg, cg_stat = measure_iterations(
                "build_call_graph",
                lambda: build_call_graph(ws),
                iterations=iterations,
                warmup=warmup,
            )
            cg_stat["edges"] = len(cg.edges)
            profile_res["stages"].append(cg_stat)

            mg, mg_stat = measure_iterations(
                "build_module_graph",
                lambda: build_module_dependency_graph(ws),
                iterations=iterations,
                warmup=warmup,
            )
            mg_stat["edges"] = len(mg.edges)
            profile_res["stages"].append(mg_stat)

            _, cyc_stat = measure_iterations(
                "module_graph_detect_cycles_cached",
                lambda: mg.detect_cycles(),
                iterations=max(3, iterations),
                warmup=1,
            )
            profile_res["stages"].append(cyc_stat)

            # Cold incremental build (fresh cache per run)
            cold_counter = {"n": 0}

            def run_cold_inc() -> Any:
                cold_counter["n"] += 1
                cm = IndexCacheManager(tmp / f"cold_cache_{cold_counter['n']}")
                idx = IncrementalRepositoryIndex(repo_root=ws, cache_manager=cm)
                return idx.build()

            cold_res, cold_stat = measure_iterations(
                "incremental_build_cold", run_cold_inc, iterations=iterations, warmup=0
            )
            cold_stat["cache_hits"] = cold_res[3].cache_hits
            cold_stat["cache_misses"] = cold_res[3].cache_misses
            profile_res["stages"].append(cold_stat)

            # Warm incremental build
            warm_cache = IndexCacheManager(cache_dir)
            IncrementalRepositoryIndex(repo_root=ws, cache_manager=warm_cache).build()
            warm_res, warm_stat = measure_iterations(
                "incremental_build_warm",
                lambda: IncrementalRepositoryIndex(repo_root=ws, cache_manager=warm_cache).build(),
                iterations=iterations,
                warmup=warmup,
            )
            total_warm = warm_res[3].cache_hits + warm_res[3].cache_misses
            warm_stat["cache_hits"] = warm_res[3].cache_hits
            warm_stat["cache_hit_rate"] = round(warm_res[3].cache_hits / total_warm, 4) if total_warm else 0.0
            profile_res["stages"].append(warm_stat)

            # High change frequency / 1-file delta update + equivalence verification
            edit_counter = {"n": 0}
            target_edit_file = ws / rel_files[-1]
            orig_edit_text = target_edit_file.read_text(encoding="utf-8")

            def run_delta_inc() -> Any:
                edit_counter["n"] += 1
                target_edit_file.write_text(
                    orig_edit_text + f"\n# edit iteration {edit_counter['n']}\n",
                    encoding="utf-8",
                )
                return IncrementalRepositoryIndex(repo_root=ws, cache_manager=warm_cache).build()

            delta_rounds = 5 if spec.name == "high_change_freq" else iterations
            delta_res, delta_stat = measure_iterations(
                "incremental_build_1file_delta",
                run_delta_inc,
                iterations=delta_rounds,
                warmup=0,
            )
            total_delta = delta_res[3].cache_hits + delta_res[3].cache_misses
            delta_stat["cache_hits"] = delta_res[3].cache_hits
            delta_stat["cache_misses"] = delta_res[3].cache_misses
            delta_stat["cache_hit_rate"] = round(delta_res[3].cache_hits / total_delta, 4) if total_delta else 0.0
            profile_res["stages"].append(delta_stat)

            # Equivalence verification against full non-cached rebuild after delta edits
            eq_idx = IncrementalRepositoryIndex(repo_root=ws, cache_manager=warm_cache)
            inc_sym, inc_cg, inc_mg, _ = eq_idx.build()
            full_sym = build_symbol_index(ws)
            full_cg = build_call_graph(ws)
            full_mg = build_module_dependency_graph(ws)
            is_eq = IncrementalRepositoryIndex.verify_equivalence(
                full_symbols=full_sym,
                full_call=full_cg,
                full_module=full_mg,
                inc_symbols=inc_sym,
                inc_call=inc_cg,
                inc_module=inc_mg,
            )
            profile_res["equivalence_verified"] = is_eq

            # Change impact analysis
            target_rel = rel_files[0]
            impact_analyzer = ChangeImpactAnalyzer(repo_root=ws, incremental_index=eq_idx)
            _, imp_stat = measure_iterations(
                "change_impact_analysis",
                lambda: impact_analyzer.analyze_file_paths([target_rel]),
                iterations=iterations,
                warmup=warmup,
            )
            profile_res["stages"].append(imp_stat)

            # Context construction cold & repeat
            ctx_cold_counter = {"n": 0}

            def run_ctx_cold() -> Any:
                ctx_cold_counter["n"] += 1
                builder = RepositoryContextBuilder(workspace_path=ws, incremental_index=eq_idx)
                return builder.build_context(
                    query="off by one Worker_0000 compute_0000",
                    target_files=[target_rel],
                    target_symbols=["compute_0000"],
                )

            ctx_obj, ctx_cold_stat = measure_iterations(
                "context_build_cold", run_ctx_cold, iterations=iterations, warmup=0
            )
            ctx_cold_stat["context_chars"] = ctx_obj.char_count
            profile_res["stages"].append(ctx_cold_stat)

            shared_builder = RepositoryContextBuilder(workspace_path=ws, incremental_index=eq_idx)
            shared_builder.build_context(
                query="off by one Worker_0000 compute_0000",
                target_files=[target_rel],
                target_symbols=["compute_0000"],
            )
            _, ctx_rep_stat = measure_iterations(
                "context_build_cached_repeat",
                lambda: shared_builder.build_context(
                    query="off by one Worker_0000 compute_0000",
                    target_files=[target_rel],
                    target_symbols=["compute_0000"],
                ),
                iterations=iterations,
                warmup=warmup,
            )
            profile_res["stages"].append(ctx_rep_stat)

            # Shared-index multi-stage pipeline (Diagnosis -> Root Cause -> Repair Planning)
            diag_input = DiagnosisInput(
                user_description="compute_0000 returns x-1 instead of x",
                error_message=f"AssertionError in {target_rel}:8",
                test_failure=TestFailureReport(
                    test_name="test_compute_000",
                    test_file="tests/test_mod_000.py",
                    failure_message="assert 4 == 5",
                    stack_trace=f'File "{target_rel}", line 8, in process\n    return x - 1',
                    line_number=8,
                ),
                target_files=[target_rel],
                target_symbols=["compute_0000"],
            )

            def run_shared_3stage() -> Any:
                shared_idx = IncrementalRepositoryIndex(repo_root=ws, cache_manager=warm_cache)
                diag_res = EvidenceDrivenDiagnosisEngine(
                    workspace_path=ws, incremental_index=shared_idx
                ).diagnose(diag_input)
                rc_res = RootCauseAnalysisEngine(
                    workspace_path=ws, incremental_index=shared_idx
                ).analyze(diagnosis=diag_res)
                plan_res = RepairPlanningEngine(workspace_path=ws).plan_repair(
                    root_cause_report=rc_res, diagnosis=diag_res
                )
                return plan_res

            plan_res, shared_stat = measure_iterations(
                "diagnosis_rootcause_plan_shared_index",
                run_shared_3stage,
                iterations=iterations,
                warmup=warmup,
            )
            profile_res["stages"].append(shared_stat)

            # Repair workspace allocation & test discovery
            wm = RepairWorkspaceManager(original_repo_path=ws, base_workspaces_dir=tmp / "repair_ws")

            def run_alloc_cleanup() -> str:
                rws = wm.allocate_workspace(allowed_paths=[target_rel], plan_id=plan_res.plan_id)
                wid = rws.workspace_id
                wm.cleanup_workspace(wid)
                return wid

            _, alloc_stat = measure_iterations(
                "repair_workspace_alloc_and_cleanup",
                run_alloc_cleanup,
                iterations=iterations,
                warmup=0,
            )
            profile_res["stages"].append(alloc_stat)

            discoverer = RepairTestDiscoverer(ws)
            _, disc_stat = measure_iterations(
                "verification_test_discovery",
                lambda: discoverer.discover_tests(modified_files=[target_rel]),
                iterations=iterations,
                warmup=warmup,
            )
            profile_res["stages"].append(disc_stat)

        finally:
            cleanup_repository(acquired, allowed_base=ws_root)

    return profile_res


def benchmark_concurrent_workflows_and_db(iterations: int = 3) -> Dict[str, Any]:
    """Benchmark SQLite pool efficiency, checkpoint/event writes, recovery, and 8-thread contention."""
    stages: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "bench_pool.db"
        store = OrchestrationStateStore(db_path=db_path)
        idemp = IdempotencyLedger(db_path=db_path)
        approval_ledger = ApprovalLedger(db_path=db_path)
        rec_recorder = OrchestrationEventRecorder()

        connect_calls = {"count": 0}
        orig_connect = sqlite3.connect

        def counted_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connect_calls["count"] += 1
            return orig_connect(*args, **kwargs)

        sqlite3.connect = counted_connect  # type: ignore[assignment]
        try:
            batch = {"n": 0}

            def write_100_workflows() -> None:
                rec_recorder = OrchestrationEventRecorder()
                batch["n"] += 1
                base = batch["n"] * 1000
                for i in range(100):
                    wf_id = f"wf_p{base + i:05d}"
                    ctx = WorkflowContext(
                        workflow_id=wf_id,
                        repository_path="/tmp/bench_repo",
                        run_id=f"run_{base}_{i}",
                    )
                    store.save_workflow(ctx, WorkflowState.PLAN_READY, WorkflowStage.PLANNING)
                    ev = rec_recorder.record_event(
                        wf_id,
                        OrchestrationEventType.WORKFLOW_CREATED,
                        WorkflowState.CREATED,
                        "init",
                    )
                    store.save_event(ev)
                    store.save_checkpoint(
                        workflow_id=wf_id,
                        stage=WorkflowStage.PLANNING,
                        state=WorkflowState.PLAN_READY,
                        last_event_sequence=ev.sequence,
                        last_event_hash=ev.event_hash,
                        outputs_digest="0" * 64,
                    )
                    key = compute_idempotency_key(wf_id, WorkflowStage.EXECUTION, f"d_{i}")
                    idemp.register_start(key, wf_id, WorkflowStage.EXECUTION)
                    approval_ledger.record_consumed(f"appr_{base}_{i}", f"exec_{base}_{i}")

            connect_calls["count"] = 0
            _, w_stat = measure_iterations(
                "db_100_workflows_events_checkpoints",
                write_100_workflows,
                iterations=iterations,
                warmup=1,
            )
            w_stat["sqlite_connect_calls"] = connect_calls["count"]
            stages.append(w_stat)

            connect_calls["count"] = 0

            def read_100_workflows() -> None:
                base = batch["n"] * 1000
                for i in range(100):
                    wf_id = f"wf_p{base + i:05d}"
                    store.get_workflow(wf_id)
                    store.get_latest_checkpoint(wf_id)
                    store.get_events(wf_id)
                    approval_ledger.is_consumed(f"appr_{base}_{i}")

            _, r_stat = measure_iterations(
                "db_100_workflow_reads",
                read_100_workflows,
                iterations=iterations,
                warmup=1,
            )
            r_stat["sqlite_connect_calls"] = connect_calls["count"]
            stages.append(r_stat)

            rec_engine = WorkflowRecoveryEngine(state_store=store, idempotency_ledger=idemp)
            connect_calls["count"] = 0

            def recover_100() -> None:
                base = batch["n"] * 1000
                for i in range(100):
                    rec_engine.inspect_recovery(f"wf_p{base + i:05d}")

            _, rec_stat = measure_iterations(
                "recovery_100_inspections",
                recover_100,
                iterations=iterations,
                warmup=1,
            )
            rec_stat["sqlite_connect_calls"] = connect_calls["count"]
            stages.append(rec_stat)

            # Multi-threaded concurrent contention (8 workers x 5 workflows each)
            connect_calls["count"] = 0
            conc_batch = {"n": 0}

            def run_concurrent_pool() -> None:
                conc_batch["n"] += 1
                cb = conc_batch["n"] * 1000

                def worker(wid: int) -> None:
                    for k in range(5):
                        # Ensure last 6 chars are unique per iteration, worker, and item: f"{conc_batch['n']:02d}{wid:02d}{k:02d}"
                        wf_suffix = f"{conc_batch['n']:02d}{wid:02d}{k:02d}"
                        wf_id = f"wf_c_{cb}_{wf_suffix}"
                        ctx = WorkflowContext(
                            workflow_id=wf_id,
                            repository_path="/tmp/bench_repo",
                            run_id=f"run_conc_{cb}_{wid}_{k}",
                        )
                        store.save_workflow(ctx, WorkflowState.PLAN_READY, WorkflowStage.PLANNING)
                        store.save_checkpoint(
                            workflow_id=wf_id,
                            stage=WorkflowStage.PLANNING,
                            state=WorkflowState.PLAN_READY,
                            last_event_sequence=1,
                            last_event_hash="a" * 64,
                            outputs_digest="b" * 64,
                        )
                        store.get_workflow(wf_id)

                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(worker, range(8)))

            _, conc_stat = measure_iterations(
                "concurrent_8_threads_40_workflows",
                run_concurrent_pool,
                iterations=iterations,
                warmup=0,
            )
            conc_stat["sqlite_connect_calls"] = connect_calls["count"]
            stages.append(conc_stat)
        finally:
            sqlite3.connect = orig_connect  # type: ignore[assignment]

    return {
        "profile": "concurrent_workflows",
        "stages": stages,
    }


def benchmark_local_ollama_reuse(
    api_base: str = "http://127.0.0.1:11434/v1",
    model: str = "qwen2.5-coder:1.5b",
    iterations: int = 3,
) -> Dict[str, Any]:
    """Optionally benchmark local Ollama warm HTTP client reuse vs unpooled client."""
    from forge.llm.models import ChatMessage, LLMRequest, MessageRole
    from forge.llm.openai import OpenAICompatibleProvider

    req = LLMRequest(
        messages=[
            ChatMessage(role=MessageRole.SYSTEM, content="Respond with one word."),
            ChatMessage(role=MessageRole.USER, content="Reply OK."),
        ],
        max_tokens=4,
        temperature=0.0,
    )

    reused_samples: List[float] = []
    fresh_samples: List[float] = []
    provider = OpenAICompatibleProvider(api_base=api_base, model=model, timeout_seconds=30.0)
    try:
        # Warmup once so model is loaded in Ollama memory
        provider.generate(req)
        for _ in range(iterations):
            t0 = time.perf_counter()
            provider.generate(req)
            reused_samples.append((time.perf_counter() - t0) * 1000.0)
    finally:
        provider.close()

    for _ in range(iterations):
        p_fresh = OpenAICompatibleProvider(api_base=api_base, model=model, timeout_seconds=30.0)
        try:
            t0 = time.perf_counter()
            p_fresh.generate(req)
            fresh_samples.append((time.perf_counter() - t0) * 1000.0)
        finally:
            p_fresh.close()

    return {
        "model": model,
        "api_base": api_base,
        "reused_shared_client": compute_stats(reused_samples),
        "fresh_client_per_call": compute_stats(fresh_samples),
    }


def run_benchmark_suite(
    profiles: Optional[List[str]] = None,
    iterations: int = 3,
    warmup: int = 1,
    include_ollama: bool = False,
    ollama_model: str = "qwen2.5-coder:1.5b",
) -> Dict[str, Any]:
    """Execute the complete reproducible Phase 13 M4 benchmark suite."""
    selected = profiles or list(ALL_PROFILES)
    report: Dict[str, Any] = {
        "benchmark": "FORGE Phase 13 M4 Performance & Scalability",
        "environment": _collect_environment_metadata(),
        "config": {
            "profiles": selected,
            "iterations": iterations,
            "warmup": warmup,
            "include_ollama": include_ollama,
        },
        "profiles": [],
    }

    for name in selected:
        if name == "concurrent_workflows":
            report["profiles"].append(benchmark_concurrent_workflows_and_db(iterations=iterations))
        elif name in REPO_PROFILE_SPECS:
            report["profiles"].append(
                benchmark_repo_profile(REPO_PROFILE_SPECS[name], iterations=iterations, warmup=warmup)
            )
        else:
            raise ValueError(f"Unknown benchmark profile: {name}")

    if include_ollama:
        report["ollama_benchmark"] = benchmark_local_ollama_reuse(
            model=ollama_model, iterations=iterations
        )

    return report


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FORGE Phase 13 M4 Performance Benchmark Suite")
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=list(ALL_PROFILES),
        choices=list(ALL_PROFILES),
        help="Subset of benchmark profiles to execute.",
    )
    parser.add_argument("--iterations", type=int, default=3, help="Measured iterations per stage.")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup iterations per stage.")
    parser.add_argument(
        "--include-ollama",
        action="store_true",
        help="Include live local Ollama HTTP client reuse latency benchmark.",
    )
    parser.add_argument(
        "--ollama-model",
        default="qwen2.5-coder:1.5b",
        help="Local Ollama model name to benchmark when --include-ollama is set.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional output path to write full JSON benchmark report.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point for the FORGE Phase 13 M4 benchmark framework."""
    args = _parse_args(argv)
    report = run_benchmark_suite(
        profiles=args.profiles,
        iterations=args.iterations,
        warmup=args.warmup,
        include_ollama=args.include_ollama,
        ollama_model=args.ollama_model,
    )
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=" * 78)
    print("FORGE Phase 13 M4 Performance & Scalability Benchmark Results")
    env = report["environment"]
    print(f"Python {env['python_version']} | {env['machine']} | CPUs={env['cpu_count']} | {env['git_version']}")
    print("=" * 78)
    for prof in report["profiles"]:
        print(f"\n[Profile: {prof['profile']}]")
        if "equivalence_verified" in prof:
            print(f"  equivalence_verified: {prof['equivalence_verified']}")
        for st in prof["stages"]:
            print(
                f"  {st['stage']:<38} median={st['median_ms']:>8.2f}ms  "
                f"p95={st['p95_ms']:>8.2f}ms  peak={st['peak_memory_kb']:>8.1f}KB  "
                f"rps={st['throughput_ops_per_sec']:>7.2f}"
            )
    if "ollama_benchmark" in report:
        ob = report["ollama_benchmark"]
        print(f"\n[Local Ollama ({ob['model']})]")
        print(f"  reused_shared_client : {ob['reused_shared_client']}")
        print(f"  fresh_client_per_call: {ob['fresh_client_per_call']}")
    print("=" * 78)


if __name__ == "__main__":
    main()
