"""Deterministic performance regression & scalability test suite for FORGE Phase 13 Milestone 4.

Uses invariant checks (operation counts, connection counts, bounded caches,
single-pass discovery/parsing, and semantic equivalence) rather than brittle
wall-clock timing thresholds so tests remain reliable across CI runners.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from typing import Any, List
from unittest.mock import patch

import httpx
import pytest

from forge.analysis.impact.analyzer import ChangeImpactAnalyzer
from forge.benchmarks.benchmark_phase13_m4_performance import (
    REPO_PROFILE_SPECS,
    create_profile_repository,
    run_benchmark_suite,
)
from forge.causality.engine import RootCauseAnalysisEngine
from forge.causality.schemas import (
    CausalNode,
    CausalNodeType,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.schemas import FindingConfidence
from forge.diagnosis.engine import EvidenceDrivenDiagnosisEngine
from forge.diagnosis.schemas import DiagnosisInput, TestFailureReport
from forge.llm.context import RepositoryContextBuilder
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.openai import OpenAICompatibleProvider
from forge.orchestration.adapters import (
    DefaultDiagnosisAdapter,
    DefaultPlanningAdapter,
    DefaultRootCauseAdapter,
)
from forge.orchestration.idempotency import IdempotencyLedger, compute_idempotency_key
from forge.orchestration.persistence import OrchestrationStateStore
from forge.orchestration.recovery import WorkflowRecoveryEngine
from forge.orchestration.schemas import WorkflowContext, WorkflowStage, WorkflowState
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.workspace import RepairWorkspaceManager
from forge.repair_planning.planner import RepairPlanningEngine
from forge.repair_planning.risk import RepairRiskAnalyzer
from forge.repair_verification.discovery import RepairTestDiscoverer
from forge.repository.cache import IndexCacheManager
from forge.repository.call_graph import build_call_graph
from forge.repository.detector import detect_repository
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.indexer import build_symbol_index
from forge.repository.module_graph import build_module_dependency_graph
from forge.repository.protection import is_protected_path
from forge.repository.resolver import WorkspaceImportResolver
from forge.storage.sqlite_pool import ThreadLocalSQLitePool


def test_cache_manager_eviction_is_o1_no_full_scan(tmp_path: Path) -> None:
    """Inserting entries past max_entries must evict via in-memory manifest without O(N^2) disk scans."""
    cm = IndexCacheManager(cache_dir=tmp_path / "cache", max_entries=50)
    from forge.repository.cache_schemas import FileIndexRecord

    put_count = 140
    with patch.object(Path, "glob", wraps=Path.glob, autospec=True) as mock_glob:
        for i in range(put_count):
            rec = FileIndexRecord(
                file_path=f"src/mod_{i:03d}.py",
                content_hash=f"{i:064x}",
                module_name=f"src.mod_{i:03d}",
                mtime_ns=i + 1,
                size_bytes=100,
            )
            cm.put(rec.file_path, rec)

        # In-memory manifest handles eviction: glob should only run at __init__/prune, never per put().
        entry_globs = [c for c in mock_glob.call_args_list if len(c.args) > 1 and c.args[1] == "entry_*.json"]
        assert len(entry_globs) <= 1

    assert len(list((tmp_path / "cache").glob("entry_*.json"))) <= 50


def test_sqlite_pool_reuses_one_connection_per_thread(tmp_path: Path) -> None:
    """SQLite pool must reuse 1 connection per thread across repeated orchestration & ledger ops."""
    db_path = tmp_path / "pooled.db"
    store = OrchestrationStateStore(db_path=db_path)
    idemp = IdempotencyLedger(db_path=db_path)
    ledger = ApprovalLedger(db_path=db_path)

    connect_calls = {"count": 0}
    orig_connect = sqlite3.connect

    def counted_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connect_calls["count"] += 1
        return orig_connect(*args, **kwargs)

    with patch("forge.storage.sqlite_pool.sqlite3.connect", side_effect=counted_connect):
        # First use after patching opens 1 pooled connection for this thread, then reuses it 150+ times.
        for i in range(50):
            wf_id = f"wf_pool_{i}"
            ctx = WorkflowContext(
                workflow_id=wf_id,
                repository_path=str(tmp_path),
                run_id=f"run_pool_{i}",
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
            key = compute_idempotency_key(wf_id, WorkflowStage.EXECUTION, "d")
            idemp.register_start(key, wf_id, WorkflowStage.EXECUTION)
            ledger.record_consumed(f"appr_{i}", f"exec_{i}")
            assert store.get_workflow(wf_id) is not None
            assert store.get_latest_checkpoint(wf_id) is not None

    assert connect_calls["count"] <= 1


def test_sqlite_pool_concurrent_threads_safe_and_consistent(tmp_path: Path) -> None:
    """Concurrent multi-threaded persistence and recovery must succeed with zero locking failures."""
    db_path = tmp_path / "conc.db"
    store = OrchestrationStateStore(db_path=db_path)
    idemp = IdempotencyLedger(db_path=db_path)
    rec = WorkflowRecoveryEngine(state_store=store, idempotency_ledger=idemp)

    def worker(tid: int) -> List[str]:
        ids: List[str] = []
        for i in range(10):
            wf_id = f"wf_t{tid}_{i}"
            ctx = WorkflowContext(
                workflow_id=wf_id,
                repository_path=str(tmp_path),
                run_id=f"run_t{tid}_{i}",
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
            ids.append(wf_id)
        return ids

    with ThreadPoolExecutor(max_workers=6) as ex:
        batches = list(ex.map(worker, range(6)))

    all_ids = [wid for batch in batches for wid in batch]
    assert len(all_ids) == 60
    for wid in all_ids:
        assert store.get_workflow(wid) is not None
        assert store.get_latest_checkpoint(wid) is not None


def test_sqlite_pool_close_all_cleans_connections(tmp_path: Path) -> None:
    """Pool close_all closes pooled connections cleanly and allows transparent reopening."""
    pool = ThreadLocalSQLitePool(tmp_path / "close_test.db")
    conn1 = pool.get_connection()
    conn1.execute("SELECT 1").fetchone()
    pool.close_all()
    conn2 = pool.get_connection()
    assert conn2 is not conn1
    assert conn2.execute("SELECT 42").fetchone()[0] == 42
    pool.close_all()


def test_single_pass_ast_parsing_and_incremental_equivalence(tmp_path: Path) -> None:
    """Incremental build must parse each file at most once and stay semantically identical to full rebuild."""
    spec = REPO_PROFILE_SPECS["complex_deps"]
    # Faster subset while preserving cycles and depth
    small_cyclic = type(spec)(
        name="cyclic_test",
        num_modules=20,
        lines_per_file=10,
        depth=2,
        fanout_imports=3,
        cyclic_links=True,
        add_git_commit=False,
    )
    repo, rel_files = create_profile_repository(tmp_path / "repo", small_cyclic)
    cache_dir = tmp_path / "cache"

    orig_parse = ast.parse
    with patch("ast.parse", side_effect=orig_parse) as mock_parse:
        inc_idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
        inc_sym, inc_cg, inc_mg, summary = inc_idx.build()
        total_files = summary.total_files_scanned
        assert mock_parse.call_count == total_files

    # Edit one file, rebuild incrementally, and assert full equivalence
    edited = repo / rel_files[0]
    edited.write_text(edited.read_text(encoding="utf-8") + "\ndef added_func() -> int:\n    return 1\n", encoding="utf-8")

    inc_idx2 = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    inc_sym2, inc_cg2, inc_mg2, summary2 = inc_idx2.build()
    assert summary2.cache_hits == total_files - 1
    assert summary2.cache_misses >= 1

    assert IncrementalRepositoryIndex.verify_equivalence(
        full_symbols=build_symbol_index(repo),
        full_call=build_call_graph(repo),
        full_module=build_module_dependency_graph(repo),
        inc_symbols=inc_sym2,
        inc_call=inc_cg2,
        inc_module=inc_mg2,
    ) is True


def test_module_graph_detect_cycles_is_cached(tmp_path: Path) -> None:
    """ModuleDependencyGraph.detect_cycles() must cache cycle detection across repeated calls."""
    spec = REPO_PROFILE_SPECS["complex_deps"]
    cyclic = type(spec)(
        name="cycle_cache",
        num_modules=15,
        lines_per_file=5,
        depth=2,
        fanout_imports=3,
        cyclic_links=True,
        add_git_commit=False,
    )
    repo, _ = create_profile_repository(tmp_path / "repo", cyclic)
    mg = build_module_dependency_graph(repo)
    c1 = mg.detect_cycles()
    c2 = mg.detect_cycles()
    assert c1 == c2
    assert mg._cached_cycles is not None


def test_context_builder_bounded_cache_and_invalidation(tmp_path: Path) -> None:
    """RepositoryContextBuilder must cache unchanged workspace queries, invalidate on edits, and bound LRU size."""
    spec = REPO_PROFILE_SPECS["small"]
    small_no_git = type(spec)(
        name="ctx_test",
        num_modules=8,
        lines_per_file=10,
        depth=2,
        add_git_commit=False,
    )
    repo, rel_files = create_profile_repository(tmp_path / "repo", small_no_git)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(tmp_path / "cache"))
    builder = RepositoryContextBuilder(workspace_path=repo, incremental_index=idx)

    with patch.object(idx, "build", wraps=idx.build) as mock_build:
        ctx1 = builder.build_context(query="compute_0000", target_files=[rel_files[0]])
        ctx2 = builder.build_context(query="compute_0000", target_files=[rel_files[0]])
        assert ctx1 == ctx2
        assert mock_build.call_count == 1

        # Modify target file -> signature changes -> triggers fresh index build
        target_p = repo / rel_files[0]
        target_p.write_text(
            target_p.read_text(encoding="utf-8") + "\ndef fresh_symbol() -> int:\n    return 99\n",
            encoding="utf-8",
        )
        ctx3 = builder.build_context(query="compute_0000", target_files=[rel_files[0]])
        assert mock_build.call_count >= 2
        assert ctx3 is not ctx1

    # Verify bounded LRU size (<= 16 entries)
    for k in range(25):
        builder.build_context(query=f"distinct_query_{k}", target_files=[rel_files[0]])
    assert len(builder._context_cache) <= 16


def test_shared_workspace_index_and_adapters_deduplicate_builds(tmp_path: Path) -> None:
    """Default diagnosis/root-cause/planning adapters sharing SharedWorkspaceIndex build once per run."""
    spec = REPO_PROFILE_SPECS["small"]
    small_no_git = type(spec)(
        name="shared_idx",
        num_modules=6,
        lines_per_file=5,
        depth=2,
        add_git_commit=False,
    )
    repo, rel_files = create_profile_repository(tmp_path / "repo", small_no_git)
    shared = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(tmp_path / "cache"))

    diag_adapter = DefaultDiagnosisAdapter(incremental_index=shared)
    rc_adapter = DefaultRootCauseAdapter(incremental_index=shared)
    plan_adapter = DefaultPlanningAdapter(incremental_index=shared)

    wf_ctx = WorkflowContext(
        workflow_id="wf_shared_001",
        repository_path=str(repo),
        run_id="run_shared_001",
        trusted_approvers={"alice"},
    )
    diag_in = DiagnosisInput(
        user_description="compute_0000 defect",
        error_message=f"AssertionError in {rel_files[0]}:8",
        target_files=[rel_files[0]],
        target_symbols=["compute_0000"],
    )

    with patch("forge.repository.incremental.IncrementalRepositoryIndex.build", wraps=IncrementalRepositoryIndex.build, autospec=True) as mock_inc_build:
        diag_res = diag_adapter.run_diagnosis(wf_ctx, diag_in)
        rc_res = rc_adapter.run_root_cause(wf_ctx, diag_res)
        plan_res = plan_adapter.run_planning(wf_ctx, rc_res, diag_res)
        assert mock_inc_build.call_count <= 2
        assert plan_res.plan_id


def test_risk_assessor_and_planner_handle_none_symbol_ids(tmp_path: Path) -> None:
    """Risk assessment and planning must never crash when symbol_id or affected_symbols contain None."""
    assessor = RepairRiskAnalyzer(workspace_path=tmp_path)
    risk = assessor.assess_risk(
        affected_files=["src/a.py"],
        affected_symbols=[None, "", "valid_fn"],  # type: ignore[list-item]
    )
    assert risk.risk_score >= 0.0

    # Also test planner directly with RootCauseReport whose primary node has symbol_id=None
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    rc = RootCauseReport(
        report_id="rc_none_sym",
        run_id="run_test",
        status=CausalStatus.INSUFFICIENT_EVIDENCE,
        primary_root_cause=CausalNode(
            node_id="n1",
            symbol_id=None,
            file_path="src/a.py",
            start_line=1,
            end_line=2,
            node_type=CausalNodeType.MODULE,
            role=CausalRole.ROOT_CAUSE,
            description="File-level finding without symbol",
        ),
        confidence=FindingConfidence.LOW,
        confidence_score=0.2,
    )
    planner = RepairPlanningEngine(workspace_path=tmp_path)
    plan = planner.plan_repair(root_cause_report=rc, file_path="src/a.py")
    assert plan.affected_files == ["src/a.py"]
    assert plan.affected_symbols == []


def test_openai_provider_reuses_http_client_and_closes_cleanly() -> None:
    """OpenAICompatibleProvider must reuse a single pooled httpx.Client across repeated calls."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "model": "qwen2.5-coder:1.5b",
                "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )
    )
    provider = OpenAICompatibleProvider(api_base="http://127.0.0.1:11434/v1", model="qwen2.5-coder:1.5b")
    # Simulate client initialization with mock transport
    c1 = provider._get_client()
    c1._transport = transport
    c2 = provider._get_client()
    assert c1 is c2

    req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="hi")], max_tokens=4)
    r1 = provider.generate(req)
    r2 = provider.generate(req)
    assert r1.content == "OK"
    assert r2.content == "OK"
    assert provider._get_client() is c1
    provider.close()
    assert provider._shared_client is None


def test_protection_fast_path_and_discovery_accuracy(tmp_path: Path) -> None:
    """Optimized protection fast-path and test discovery stem pre-filter preserve security & recall."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def compute_0000():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hit.py").write_text(
        "from src.app import compute_0000\n\ndef test_ok():\n    assert compute_0000() == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_unrelated.py").write_text("def test_other():\n    assert True\n", encoding="utf-8")

    assert is_protected_path(tmp_path / ".env", base_dir=tmp_path) is True
    assert is_protected_path(tmp_path / "contacts.json", base_dir=tmp_path) is True
    assert is_protected_path(tmp_path / ".." / "escape.py", base_dir=tmp_path) is True
    assert is_protected_path(tmp_path / "src" / "app.py", base_dir=tmp_path) is False

    discoverer = RepairTestDiscoverer(tmp_path)
    res = discoverer.discover_tests(modified_files=["src/app.py"])
    assert any("test_hit.py" in t.test_file for t in res.targeted_tests)


def test_workspace_resolver_string_prefix_accuracy(tmp_path: Path) -> None:
    """WorkspaceImportResolver with known_python_files matches standard relative resolution."""
    repo, _ = create_profile_repository(
        tmp_path / "repo",
        type(REPO_PROFILE_SPECS["small"])(
            name="resolver", num_modules=5, lines_per_file=5, depth=3, add_git_commit=False
        ),
    )
    from forge.repository.indexer import discover_workspace_python_files

    files = discover_workspace_python_files(repo, exclude_forge=True)
    fast_resolver = WorkspaceImportResolver(repo, known_python_files=files)
    slow_resolver = WorkspaceImportResolver(repo)
    # Both resolvers must map the same set of workspace-relative modules
    assert fast_resolver.module_to_file.keys() == slow_resolver.module_to_file.keys()
    # Every mapped module resolves to a consistent file path
    for mod, file_path in fast_resolver.module_to_file.items():
        assert slow_resolver.module_to_file.get(mod) == file_path
    assert detect_repository(repo).total_python_files == len(files)


def test_benchmark_framework_smoke_suite() -> None:
    """Reproducible benchmark runner succeeds on small + concurrent_workflows profiles."""
    rep = run_benchmark_suite(
        profiles=["small", "concurrent_workflows"],
        iterations=2,
        warmup=1,
        include_ollama=False,
    )
    assert len(rep["profiles"]) == 2
    small_prof = rep["profiles"][0]
    assert small_prof["profile"] == "small"
    assert small_prof["equivalence_verified"] is True
    assert all(st["median_ms"] >= 0.0 and st["p95_ms"] >= st["median_ms"] for st in small_prof["stages"])
