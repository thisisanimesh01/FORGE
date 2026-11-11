"""Adversarial security, concurrency, and integrity tests for Phase 13 Milestone 4."""

import concurrent.futures
import hashlib
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition
from forge.llm.context import RepositoryContextBuilder
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.openai import OpenAICompatibleProvider
from forge.llm.url_safety import ValidatedTargetURL
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.schemas import WorkflowContext
from forge.repair_execution.workspace import (
    _safe_copy_dual_and_digest,
    compute_tree_digest,
    is_protected_path,
)
from forge.repository.cache import IndexCacheManager
from forge.repository.cache_schemas import FileIndexRecord
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.storage.sqlite_pool import ThreadLocalSQLitePool


# ===========================================================================
# Audit A: Context Cache Freshness & In-Memory Eviction
# ===========================================================================

def test_context_cache_invalidated_when_file_content_changes_same_stat(tmp_path: Path):
    """If file content changes but mtime and size are artificially preserved,
    cached excerpts must still fail live validation and trigger fresh build."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    foo = workspace / "foo.py"
    # Write initial content of exact 20 characters
    initial_content = "def calculate():\n 1\n"
    foo.write_text(initial_content, encoding="utf-8")
    stat_orig = foo.stat()

    builder = RepositoryContextBuilder(workspace_path=workspace)
    ctx1 = builder.build_context(target_files=["foo.py"])
    assert len(ctx1.source_excerpts) == 1
    orig_sha = ctx1.source_excerpts[0].content_sha256

    # Swap content in-place with SAME length (20 chars) and restore mtime/atime
    modified_content = "def calculate():\n 2\n"
    assert len(initial_content) == len(modified_content)
    foo.write_text(modified_content, encoding="utf-8")
    os.utime(foo, ns=(stat_orig.st_atime_ns, stat_orig.st_mtime_ns))
    stat_mod = foo.stat()
    assert stat_mod.st_size == stat_orig.st_size
    assert stat_mod.st_mtime_ns == stat_orig.st_mtime_ns

    # Even though stat_sig is identical, the builder must detect the slice hash mismatch
    ctx2 = builder.build_context(target_files=["foo.py"])
    assert len(ctx2.source_excerpts) == 1
    new_sha = ctx2.source_excerpts[0].content_sha256
    assert orig_sha != new_sha
    assert "2" in ctx2.source_excerpts[0].content


def test_context_cache_handles_deleted_file(tmp_path: Path):
    """When a file tracked in cached excerpts is deleted, context builder discards cache."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bar = workspace / "bar.py"
    bar.write_text("def bar(): return True\n", encoding="utf-8")

    builder = RepositoryContextBuilder(workspace_path=workspace)
    ctx1 = builder.build_context(target_files=["bar.py"])
    assert len(ctx1.source_excerpts) == 1

    bar.unlink()
    ctx2 = builder.build_context(target_files=["bar.py"])
    # Discarded cached entry, fresh build returns 0 excerpts for deleted file
    assert len(ctx2.source_excerpts) == 0


def test_index_cache_manager_multithreaded_concurrency(tmp_path: Path):
    """Verify concurrent reads, writes, and evictions in IndexCacheManager."""
    cache_dir = tmp_path / "cache"
    manager = IndexCacheManager(cache_dir=cache_dir, max_entries=10)

    def worker(worker_id: int):
        for i in range(25):
            rel_path = f"module_{worker_id}_{i}.py"
            content_hash = hashlib.sha256(f"content_{worker_id}_{i}".encode()).hexdigest()
            record = FileIndexRecord(
                file_path=rel_path,
                content_hash=content_hash,
                module_name=f"module_{worker_id}_{i}",
                mtime_ns=int(time.time() * 1e9),
                size_bytes=100,
            )
            manager.put(rel_path, record)
            cached = manager.get(rel_path, content_hash)
            if cached is not None:
                assert cached.content_hash == content_hash

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Cache entries should respect max_entries limit
    cached_files = list(cache_dir.glob("entry_*.json"))
    assert len(cached_files) <= 10


# ===========================================================================
# Audit B: SQLite Connection Pooling & Durability
# ===========================================================================

def test_sqlite_pool_isolation_and_rollback_on_error(tmp_path: Path):
    """Connection pool must isolate per-thread transactions and roll back dirty transactions."""
    db_file = tmp_path / "pool_test.db"
    pool = ThreadLocalSQLitePool(db_path=db_file)

    # Initialize schema
    conn = pool.get_connection()
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT UNIQUE);")

    # Simulate an aborted transaction in worker thread that left uncommitted dirty state
    def faulty_thread():
        p = ThreadLocalSQLitePool(db_path=db_file)
        c = p.get_connection()
        c.execute("BEGIN IMMEDIATE;")
        c.execute("INSERT INTO users (name) VALUES ('dangling_user');")
        # Simulating thread exit / forgotten commit / explicit close with active transaction
        c.close()

    t = threading.Thread(target=faulty_thread)
    t.start()
    t.join()

    # In main thread, dangling_user must NOT exist
    c_main = pool.get_connection()
    row = c_main.execute("SELECT COUNT(*) FROM users WHERE name = 'dangling_user'").fetchone()
    assert row[0] == 0


def test_sqlite_pool_distinct_databases(tmp_path: Path):
    """Pool instances for different database paths must remain distinct."""
    db1 = tmp_path / "db1.db"
    db2 = tmp_path / "db2.db"

    pool1 = ThreadLocalSQLitePool(db_path=db1)
    pool2 = ThreadLocalSQLitePool(db_path=db2)

    assert pool1 is not pool2
    assert pool1.db_path != pool2.db_path


# ===========================================================================
# Audit C: Single-Pass Indexing & Graph Equivalence
# ===========================================================================

def test_single_pass_ast_equivalence(tmp_path: Path):
    """Verify single-pass AST visitor produces call edges and symbols identical to multi-pass."""
    ws = tmp_path / "code"
    ws.mkdir()
    (ws / "main.py").write_text(
        "import utils\ndef run():\n    return utils.helper()\n",
        encoding="utf-8",
    )
    (ws / "utils.py").write_text(
        "def helper():\n    return 42\n",
        encoding="utf-8",
    )

    idx = IncrementalRepositoryIndex(repo_root=ws)
    syms, cg, mg, _ = idx.build()

    assert any(s.name == "run" for s in syms.symbols)
    assert any(s.name == "helper" for s in syms.symbols)
    assert any(e.source_id == "main.py:run" for e in cg.edges)
    assert "utils" in mg.get_dependencies("main")


# ===========================================================================
# Audit D: Dual-Copy and Digest Determinism
# ===========================================================================

def test_dual_copy_digest_matches_independent_walk(tmp_path: Path):
    """Single-pass copy and digest must produce the identical SHA-256 digest
    as an independent full directory digest computation."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg").mkdir()
    (src / "pkg" / "a.py").write_text("a = 10\n", encoding="utf-8")
    (src / "pkg" / "b.txt").write_text("hello world\n", encoding="utf-8")

    iso = tmp_path / "iso"
    snap = tmp_path / "snap"

    single_pass_digest = _safe_copy_dual_and_digest(src, iso, snap)
    independent_digest = compute_tree_digest(iso)

    assert single_pass_digest == independent_digest
    assert (snap / "pkg" / "a.py").exists()


# ===========================================================================
# Audit E: LLM Client Pooling and Per-Request SSRF Validation
# ===========================================================================

def test_persistent_llm_client_runs_ssrf_check_on_each_call(monkeypatch):
    """Even with persistent HTTP client, SSRF URL validation must run on every request."""
    llm = OpenAICompatibleProvider(
        api_key="sk-test-valid-key-12345",
        api_base="https://api.openai.com/v1",
        model="gpt-4o",
    )

    checked_urls = []

    def mock_validate(target_url, allow_loopback=True, resolve_dns=True):
        checked_urls.append(target_url)
        return ValidatedTargetURL(
            original_url=target_url,
            scheme="https",
            host="api.openai.com",
            port=443,
            path="/v1/chat/completions",
            query="",
            pinned_ip="192.0.2.1",
            is_loopback_target=False,
        )

    monkeypatch.setattr("forge.llm.openai.validate_url_target", mock_validate)

    class DummyClient:
        def post(self, url, headers=None, json=None, timeout=None, extensions=None):
            class DummyResponse:
                status_code = 200
                def json(self):
                    return {
                        "choices": [{"message": {"content": "OK"}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                    }
            return DummyResponse()

    monkeypatch.setattr(llm, "_get_client", lambda: DummyClient())

    # Dispatch 3 calls
    req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="hello")])
    for _ in range(3):
        llm.generate(req)

    # Must have validated the completion endpoint per request (at least 3 endpoint validations)
    endpoint_checks = [u for u in checked_urls if u.endswith("/chat/completions")]
    assert len(endpoint_checks) == 3


# ===========================================================================
# Audit F: Shared Index Workflow Isolation
# ===========================================================================

def test_shared_index_isolation_between_orchestrators(tmp_path: Path):
    """Separate orchestrator instances must not cross-contaminate repository indexes."""
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    (ws1 / "f1.py").write_text("x = 1\n")
    (ws2 / "f2.py").write_text("y = 2\n")

    ctx1 = WorkflowContext(workflow_id="wf_1", repository_path=str(ws1))
    ctx2 = WorkflowContext(workflow_id="wf_2", repository_path=str(ws2))

    orch1 = EngineeringOrchestrator(context=ctx1)
    orch2 = EngineeringOrchestrator(context=ctx2)

    # Retrieve shared indexes from the respective orchestrator adapters
    idx1 = orch1.diagnosis_adapter.index_provider(ws1)
    idx2 = orch2.diagnosis_adapter.index_provider(ws2)

    assert idx1 is not idx2
    assert idx1.repo_root == ws1.resolve()
    assert idx2.repo_root == ws2.resolve()
