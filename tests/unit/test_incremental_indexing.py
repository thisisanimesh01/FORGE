"""Comprehensive test suite for Phase 11 Milestone 5: Incremental Indexing and Caching.

Covers:
- Cache creation, hits, misses, checksums, schema/parser versioning
- File lifecycle: add, modify, delete, rename, unchanged
- Downstream dependent invalidation
- Call graph, module graph, and cycle updates
- Protected file and path traversal security
- Atomic writes, corruption recovery, and LRU eviction
- Rebuild fallback safety
- Exact semantic equivalence with clean full rebuilds and M4 impact analysis
"""

import hashlib
import json
import os
from pathlib import Path
import time
import pytest

from forge.analysis.impact.analyzer import ChangeImpactAnalyzer
from forge.repository.cache import IndexCacheManager
from forge.repository.cache_schemas import (
    INDEX_SCHEMA_VERSION,
    INDEXER_PARSER_VERSION,
    CacheEntry,
    FileIndexRecord,
)
from forge.repository.call_graph import build_call_graph
from forge.repository.incremental import (
    IncrementalRepositoryIndex,
    build_call_graph_from_records,
    build_module_graph_from_records,
    build_symbol_index_from_records,
    parse_file_to_record,
)
from forge.repository.indexer import build_symbol_index
from forge.repository.module_graph import build_module_dependency_graph


def _create_sample_repo(repo_dir: Path) -> None:
    """Create a realistic multi-module test repository."""
    (repo_dir / "pkg").mkdir(parents=True, exist_ok=True)
    (repo_dir / "pkg" / "__init__.py").write_text('"""Package init."""\n', encoding="utf-8")

    # Module A: Base classes and utility functions
    (repo_dir / "pkg" / "module_a.py").write_text(
        '"""Module A."""\n\n'
        'class BaseService:\n'
        '    def execute(self):\n'
        '        return 42\n\n'
        'def helper_func():\n'
        '    return "ok"\n',
        encoding="utf-8",
    )

    # Module B: Derived class calling inherited method and helper
    (repo_dir / "pkg" / "module_b.py").write_text(
        '"""Module B."""\n'
        'from pkg.module_a import BaseService, helper_func\n\n'
        'class DerivedService(BaseService):\n'
        '    def run(self):\n'
        '        h = helper_func()\n'
        '        return self.execute()\n',
        encoding="utf-8",
    )

    # Module C: Calls DerivedService.run()
    (repo_dir / "pkg" / "module_c.py").write_text(
        '"""Module C."""\n'
        'from pkg.module_b import DerivedService\n\n'
        'def main_entry():\n'
        '    svc = DerivedService()\n'
        '    return svc.run()\n',
        encoding="utf-8",
    )


def test_first_indexing_creates_valid_cache(tmp_path: Path):
    """Test that cold initial indexing parses files and creates valid cache entries."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm = IndexCacheManager(cache_dir=cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)

    symbols, call_graph, module_graph, summary = idx.build()

    assert summary.total_files_scanned == 4  # __init__, a, b, c
    assert summary.cache_hits == 0
    assert summary.cache_misses == 4
    assert summary.added_files == 4
    assert summary.rebuild_fallback_triggered is False

    # Check cache directory contains entry files
    entry_files = list(cache_dir.glob("entry_*.json"))
    assert len(entry_files) == 4

    # Verify state snapshot persisted
    state_file = cache_dir / "repo_state.json"
    assert state_file.exists()
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(state_data["file_hashes"]) == 4


def test_warm_indexing_produces_cache_hits(tmp_path: Path):
    """Test that second run with unchanged files produces 100% cache hits and zero misses."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm = IndexCacheManager(cache_dir=cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)

    # First run (cold)
    idx.build()

    # Second run (warm)
    symbols2, call_graph2, module_graph2, summary2 = idx.build()

    assert summary2.unchanged_files == 4
    assert summary2.added_files == 0
    assert summary2.modified_files == 0
    assert summary2.deleted_files == 0
    assert summary2.cache_hits == 4
    assert summary2.cache_misses == 0
    assert summary2.rebuild_fallback_triggered is False


def test_content_change_invalidates_entry(tmp_path: Path):
    """Test that modifying file content invalidates its specific cache entry."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm = IndexCacheManager(cache_dir=cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)

    idx.build()

    # Modify module_a.py
    mod_a = repo / "pkg" / "module_a.py"
    mod_a.write_text(mod_a.read_text() + "\ndef new_func():\n    return 100\n", encoding="utf-8")

    symbols2, call_graph2, module_graph2, summary2 = idx.build()

    assert summary2.modified_files == 1
    assert summary2.unchanged_files == 3
    assert summary2.cache_hits == 3  # 3 unchanged loaded from cache
    assert "new_func" in [s.name for s in symbols2.symbols]


def test_hash_based_detection_same_timestamp(tmp_path: Path):
    """Test that content changes are detected by hash even if mtime is artificially preserved."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm = IndexCacheManager(cache_dir=cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)
    idx.build()

    mod_c = repo / "pkg" / "module_c.py"
    orig_stat = mod_c.stat()
    orig_mtime = orig_stat.st_mtime

    # Change content but restore exact previous mtime
    mod_c.write_text('"""Module C modified."""\ndef extra(): pass\n', encoding="utf-8")
    os.utime(mod_c, (orig_mtime, orig_mtime))

    _, _, _, summary = idx.build()
    assert summary.modified_files == 1
    assert summary.unchanged_files == 3


def test_parser_version_change_invalidates(tmp_path: Path):
    """Test that parser version upgrade causes cache invalidation."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm1 = IndexCacheManager(cache_dir=cache_dir, parser_version="1.0.0")
    idx1 = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm1)
    idx1.build()

    # Initialize with upgraded parser version
    cm2 = IndexCacheManager(cache_dir=cache_dir, parser_version="1.1.0")
    idx2 = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm2)
    _, _, _, summary2 = idx2.build()

    assert summary2.cache_hits == 0
    assert summary2.cache_misses == 4


def test_schema_version_change_invalidates(tmp_path: Path):
    """Test that cache schema version change causes cache invalidation."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm1 = IndexCacheManager(cache_dir=cache_dir, schema_version="1.0.0")
    idx1 = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm1)
    idx1.build()

    cm2 = IndexCacheManager(cache_dir=cache_dir, schema_version="2.0.0")
    idx2 = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm2)
    _, _, _, summary2 = idx2.build()

    assert summary2.cache_hits == 0
    assert summary2.cache_misses == 4


def test_corrupted_cache_entry_safe_recovery(tmp_path: Path):
    """Test that corrupted cache files (bad JSON or tampered checksum) trigger safe recovery."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm = IndexCacheManager(cache_dir=cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)
    idx.build()

    # Corrupt one entry file on disk
    entry_files = list(cache_dir.glob("entry_*.json"))
    assert entry_files
    entry_files[0].write_text("CORRUPTED_NOT_JSON!!!", encoding="utf-8")

    # Second build should safely recover by re-parsing the corrupted file
    _, _, _, summary = idx.build()
    assert summary.rebuild_fallback_triggered is False
    assert cm.stats.corrupted_entries_recovered >= 1


def test_checksum_mismatch_detected(tmp_path: Path):
    """Test that a cache entry with altered payload fails checksum verification and is re-parsed."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    cm = IndexCacheManager(cache_dir=cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)
    idx.build()

    entry_files = list(cache_dir.glob("entry_*.json"))
    data = json.loads(entry_files[0].read_text(encoding="utf-8"))
    # Tamper with payload without updating checksum
    data["payload"]["docstring"] = "TAMPERED_DOCSTRING"
    entry_files[0].write_text(json.dumps(data), encoding="utf-8")

    _, _, _, summary = idx.build()
    assert cm.stats.corrupted_entries_recovered >= 1


def test_interrupted_writes_cleaned_up(tmp_path: Path):
    """Test that abandoned .tmp files from crashed/interrupted writes are cleaned up."""
    cache_dir = tmp_path / "cache"
    cm = IndexCacheManager(cache_dir=cache_dir)

    # Simulate abandoned temp files after cache manager is running
    (cache_dir / ".tmp.12345.abcde").write_text("partial data", encoding="utf-8")
    (cache_dir / ".tmp.99999.xyz").write_text("partial data 2", encoding="utf-8")
    (cache_dir / "valid.tmp").write_text("partial data 3", encoding="utf-8")

    cleaned = cm.cleanup_interrupted_writes()
    assert cleaned >= 2
    assert not (cache_dir / ".tmp.12345.abcde").exists()
    assert not (cache_dir / ".tmp.99999.xyz").exists()


def test_lru_cache_eviction(tmp_path: Path):
    """Test that bounded cache evicts oldest accessed entries when limit is exceeded."""
    cache_dir = tmp_path / "cache"
    cm = IndexCacheManager(cache_dir=cache_dir, max_entries=2)

    # Insert 3 entries
    for i in range(3):
        rec = FileIndexRecord(
            file_path=f"pkg/file_{i}.py",
            content_hash=f"hash_{i}",
            size_bytes=100,
            module_name=f"pkg.file_{i}",
        )
        cm.put(f"pkg/file_{i}.py", rec)
        time.sleep(0.01)

    # Bound is 2, so 1 must be evicted
    remaining_files = list(cache_dir.glob("entry_*.json"))
    assert len(remaining_files) == 2
    assert cm.stats.evicted_entries == 1


def test_file_added_lifecycle(tmp_path: Path):
    """Test that newly added file is indexed and included in graphs."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    idx.build()

    # Add a new file
    (repo / "pkg" / "module_d.py").write_text(
        '"""Module D."""\n'
        'from pkg.module_a import helper_func\n'
        'def call_d():\n'
        '    return helper_func()\n',
        encoding="utf-8",
    )

    symbols, call_graph, module_graph, summary = idx.build()
    assert summary.added_files == 1
    assert "pkg/module_d.py" in module_graph.file_to_module
    assert "call_d" in [s.name for s in symbols.symbols]


def test_file_deleted_lifecycle(tmp_path: Path):
    """Test that deleted file nodes and edges are purged cleanly without stale references."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    idx.build()

    # Delete module_c.py
    (repo / "pkg" / "module_c.py").unlink()

    symbols, call_graph, module_graph, summary = idx.build()
    assert summary.deleted_files == 1
    assert "pkg/module_c.py" not in module_graph.file_to_module
    assert not any("module_c.py" in n for n in call_graph.nodes)


def test_file_renamed_lifecycle(tmp_path: Path):
    """Test that renaming a file without content changes is recognized as a rename."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    idx.build()

    # Rename module_c.py -> module_c_renamed.py
    old_c = repo / "pkg" / "module_c.py"
    new_c = repo / "pkg" / "module_c_renamed.py"
    old_c.rename(new_c)

    symbols, call_graph, module_graph, summary = idx.build()
    assert summary.renamed_files == 1
    assert "pkg/module_c_renamed.py" in module_graph.file_to_module


def test_downstream_dependents_invalidation(tmp_path: Path):
    """Test that changing a base module correctly computes invalidated dependents."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    idx.build()

    # Module A is imported by B, which is imported by C.
    # Modifying module_a.py should invalidate B and C as dependents.
    mod_a = repo / "pkg" / "module_a.py"
    mod_a.write_text(mod_a.read_text() + "\n# modification\n", encoding="utf-8")

    _, _, _, summary = idx.build()
    assert summary.invalidated_dependents >= 1


def test_cycle_detection_updates_incrementally(tmp_path: Path):
    """Test that adding a cycle is detected and removing it cleans the cycle list."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    _, _, mg1, _ = idx.build()
    assert len(mg1.detect_cycles()) == 0

    # Introduce a cyclic dependency: module_a imports module_c
    mod_a = repo / "pkg" / "module_a.py"
    orig_a = mod_a.read_text(encoding="utf-8")
    mod_a.write_text("from pkg.module_c import main_entry\n" + orig_a, encoding="utf-8")

    _, _, mg2, _ = idx.build()
    cycles2 = mg2.detect_cycles()
    assert len(cycles2) >= 1

    # Remove the cycle again
    mod_a.write_text(orig_a, encoding="utf-8")
    _, _, mg3, _ = idx.build()
    cycles3 = mg3.detect_cycles()
    assert len(cycles3) == 0


def test_protected_files_never_cached(tmp_path: Path):
    """Test that protected files (.env, test.db, contacts.json, logs) are never read or cached."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)

    # Add sensitive protected files
    (repo / ".env").write_text("SECRET_KEY=12345", encoding="utf-8")
    (repo / "test.db").write_text("SQLITE_BINARY", encoding="utf-8")
    (repo / "contacts.json").write_text('{"name": "Alice"}', encoding="utf-8")
    (repo / "app.log").write_text("DEBUG LOGS", encoding="utf-8")

    cache_dir = tmp_path / "cache"
    cm = IndexCacheManager(cache_dir)
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=cm)
    symbols, call_graph, module_graph, summary = idx.build()

    # None of these should be hashed or cached
    assert cm.compute_file_content_hash(repo / ".env", repo) is None
    assert cm.compute_file_content_hash(repo / "test.db", repo) is None
    assert cm.compute_file_content_hash(repo / "contacts.json", repo) is None

    for entry_file in cache_dir.glob("entry_*.json"):
        data = json.loads(entry_file.read_text(encoding="utf-8"))
        path = data["file_path"]
        assert ".env" not in path
        assert ".db" not in path
        assert "contacts.json" not in path


def test_escaping_symlink_rejected(tmp_path: Path):
    """Test that escaping symlinks pointing outside the repository are rejected."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)

    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")

    link = repo / "pkg" / "symlink.py"
    link.symlink_to(outside)

    cm = IndexCacheManager(cache_dir=tmp_path / "cache")
    assert cm.compute_file_content_hash(link, repo) is None
    record = parse_file_to_record(link, repo)
    assert record is None


def test_rebuild_fallback_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test that unexpected failure during incremental build triggers clean full rebuild."""
    repo = tmp_path / "repo"
    _create_sample_repo(repo)
    cache_dir = tmp_path / "cache"

    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    idx.build()

    # Inject simulated transient error into internal method on first call
    original_build_internal = idx._build_internal
    call_count = [0]

    def failing_build_internal(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Simulated transient graph resolution error")
        return original_build_internal(*args, **kwargs)

    monkeypatch.setattr(idx, "_build_internal", failing_build_internal)

    symbols, call_graph, module_graph, summary = idx.build()
    assert summary.rebuild_fallback_triggered is True
    assert "Simulated transient graph resolution error" in (summary.rebuild_fallback_reason or "")
    assert len(symbols.symbols) > 0


def test_critical_equivalence_with_full_rebuild(tmp_path: Path):
    """CRITICAL TEST: Incremental indexing must produce 100% semantically identical

    results to a clean full rebuild from scratch across multi-module repository.
    """
    repo = tmp_path / "repo"
    _create_sample_repo(repo)

    # 1. Full clean build from scratch
    full_symbols = build_symbol_index(repo)
    full_call = build_call_graph(repo)
    full_module = build_module_dependency_graph(repo)

    # 2. Incremental build with warm cache
    cache_dir = tmp_path / "cache"
    idx = IncrementalRepositoryIndex(repo_root=repo, cache_manager=IndexCacheManager(cache_dir))
    idx.build()  # cold
    inc_symbols, inc_call, inc_module, _ = idx.build()  # warm

    # 3. Assert exact semantic equivalence
    is_equiv = IncrementalRepositoryIndex.verify_equivalence(
        full_symbols=full_symbols,
        full_call=full_call,
        full_module=full_module,
        inc_symbols=inc_symbols,
        inc_call=inc_call,
        inc_module=inc_module,
    )
    assert is_equiv is True

    # 4. Integrate with M4 Change-Impact Analysis and compare reports
    diff_text = (
        "--- a/pkg/module_a.py\n"
        "+++ b/pkg/module_a.py\n"
        "@@ -3,2 +3,4 @@\n"
        " class BaseService:\n"
        "+    # modified\n"
        "     def execute(self):\n"
    )

    analyzer_full = ChangeImpactAnalyzer(repo_root=repo, call_graph=full_call, module_graph=full_module)
    report_full = analyzer_full.analyze_diff(diff_text)

    analyzer_inc = ChangeImpactAnalyzer(repo_root=repo, incremental_index=idx)
    report_inc = analyzer_inc.analyze_diff(diff_text)

    # Both impact reports must match in impacted entity IDs and classifications
    assert report_full.summary.total_impacted_entities == report_inc.summary.total_impacted_entities
    assert report_full.summary.direct_impacts == report_inc.summary.direct_impacts
    assert report_full.summary.transitive_impacts == report_inc.summary.transitive_impacts
    assert [e.entity_id for e in report_full.impacted_entities] == [e.entity_id for e in report_inc.impacted_entities]
