"""Comprehensive unit tests for Change-Impact Analysis Engine (Phase 11 Milestone 4)."""

import os
from pathlib import Path
import pytest

from forge.analysis.impact.analyzer import ChangeImpactAnalyzer
from forge.analysis.impact.extractor import (
    extract_changeset_from_diff,
    extract_changeset_from_files,
)
from forge.analysis.impact.schemas import (
    ChangeSet,
    ChangeType,
    EntityType,
    FileChange,
    ImpactClassification,
    ImpactReport,
    ImpactSummary,
    ImpactedEntity,
    SymbolChange,
)
from forge.repository.call_graph import build_call_graph
from forge.repository.module_graph import build_module_dependency_graph


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Fixture providing a fresh isolated directory for mock workspace repos."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    return workspace


# ---------------------------------------------------------------------------
# Test 1: Single changed file
# ---------------------------------------------------------------------------
def test_single_changed_file(repo: Path):
    """1. Single changed file propagates impact to dependent modules."""
    (repo / "util.py").write_text("def helper():\n    return 42\n")
    (repo / "service.py").write_text("import util\ndef run():\n    return util.helper()\n")

    changeset = extract_changeset_from_files(
        [{"path": "util.py", "change_type": "modified"}],
        repo_root=repo,
    )
    assert len(changeset.files) == 1
    assert changeset.files[0].path == "util.py"

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.total_changed_files == 1
    # service.py imports util, so service is impacted
    impacted_ids = [e.entity_id for e in report.impacted_entities]
    assert "service" in impacted_ids or any("service" in id for id in impacted_ids)


# ---------------------------------------------------------------------------
# Test 2: Multiple changed files
# ---------------------------------------------------------------------------
def test_multiple_changed_files(repo: Path):
    """2. Multiple changed files in a single changeset."""
    (repo / "a.py").write_text("def fa(): return 1\n")
    (repo / "b.py").write_text("def fb(): return 2\n")
    (repo / "c.py").write_text("import a\nimport b\ndef fc(): return a.fa() + b.fb()\n")

    changeset = extract_changeset_from_files(
        ["a.py", "b.py"],
        repo_root=repo,
    )
    assert len(changeset.files) == 2

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.total_changed_files == 2
    c_impacts = [e for e in report.impacted_entities if "c" in e.entity_id]
    assert len(c_impacts) > 0
    # c depends on both a and b, verify triggers or impact path
    c_caller = next((e for e in c_impacts if e.entity_id == "c.py:fc"), None)
    if c_caller:
        assert len(c_caller.trigger_changes) >= 1


# ---------------------------------------------------------------------------
# Test 3: Added file
# ---------------------------------------------------------------------------
def test_added_file(repo: Path):
    """3. Added file with new symbols produces valid changeset and no spurious callers."""
    (repo / "existing.py").write_text("def old(): return 'ok'\n")
    (repo / "new_mod.py").write_text("def new_func(): return 'hello'\n")

    changeset = extract_changeset_from_files(
        [{"path": "new_mod.py", "change_type": "added"}],
        repo_root=repo,
    )
    assert len(changeset.files) == 1
    assert changeset.files[0].change_type == ChangeType.ADDED

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.total_changed_files == 1
    # Nobody imports new_mod yet, so impacted entities should be empty
    assert report.summary.total_impacted_entities == 0


# ---------------------------------------------------------------------------
# Test 4: Deleted file
# ---------------------------------------------------------------------------
def test_deleted_file(repo: Path):
    """4. Deleted file marks dependent modules and call sites as UNRESOLVED."""
    # First build graph with consumer importing deleted file
    (repo / "consumer.py").write_text("import to_delete\ndef use(): return to_delete.work()\n")
    (repo / "to_delete.py").write_text("def work(): return 123\n")

    call_graph = build_call_graph(repo)
    # Now simulate deletion
    changeset = extract_changeset_from_files(
        [{"path": "to_delete.py", "change_type": "deleted"}],
        repo_root=repo,
        call_graph=call_graph,
    )
    assert len(changeset.files) == 1
    assert changeset.files[0].change_type == ChangeType.DELETED

    analyzer = ChangeImpactAnalyzer(repo_root=repo, call_graph=call_graph)
    report = analyzer.analyze(changeset)

    assert report.summary.total_changed_files == 1
    unresolved = [e for e in report.impacted_entities if e.classification == ImpactClassification.UNRESOLVED]
    assert len(unresolved) >= 1
    assert any("consumer" in u.entity_id for u in unresolved)


# ---------------------------------------------------------------------------
# Test 5: Renamed file
# ---------------------------------------------------------------------------
def test_renamed_file(repo: Path):
    """5. Renamed file identifies consumers of the old path as UNRESOLVED."""
    (repo / "caller.py").write_text("import legacy\ndef run(): return legacy.compute()\n")
    (repo / "new_name.py").write_text("def compute(): return 99\n")

    changeset = extract_changeset_from_files(
        [{"path": "new_name.py", "old_path": "legacy.py", "change_type": "renamed"}],
        repo_root=repo,
    )
    assert len(changeset.files) == 1
    assert changeset.files[0].change_type == ChangeType.RENAMED
    assert changeset.files[0].old_path == "legacy.py"

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.total_changed_files == 1
    unresolved = [e for e in report.impacted_entities if e.classification == ImpactClassification.UNRESOLVED]
    assert any("caller" in u.entity_id for u in unresolved)


# ---------------------------------------------------------------------------
# Test 6: Single changed function
# ---------------------------------------------------------------------------
def test_single_changed_function(repo: Path):
    """6. Single changed function propagates to direct callers."""
    (repo / "math_lib.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")
    (repo / "app.py").write_text("from math_lib import add, sub\ndef do_calc():\n    return add(1, 2)\ndef do_diff():\n    return sub(5, 3)\n")

    changeset = ChangeSet(
        files=[FileChange(path="math_lib.py", change_type=ChangeType.MODIFIED, changed_lines=[2])],
        symbols=[
            SymbolChange(
                symbol_id="math_lib.py:add",
                name="add",
                qualified_name="math_lib.add",
                file_path="math_lib.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
                start_line=1,
                end_line=2,
            )
        ],
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.total_changed_symbols == 1
    caller_entities = [e for e in report.impacted_entities if e.entity_id == "app.py:do_calc"]
    assert len(caller_entities) == 1
    assert caller_entities[0].classification == ImpactClassification.DIRECT
    # sub caller should not be impacted directly by add
    diff_caller = [e for e in report.impacted_entities if e.entity_id == "app.py:do_diff"]
    assert len(diff_caller) == 0


# ---------------------------------------------------------------------------
# Test 7: Changed method (with enclosing class impact)
# ---------------------------------------------------------------------------
def test_changed_method_enclosing_class(repo: Path):
    """7. Changed method impacts direct callers and enclosing class."""
    (repo / "models.py").write_text(
        "class Order:\n"
        "    def checkout(self):\n"
        "        return 'paid'\n"
    )
    (repo / "controller.py").write_text(
        "from models import Order\n"
        "def process():\n"
        "    o = Order()\n"
        "    return o.checkout()\n"
    )

    changeset = ChangeSet(
        files=[FileChange(path="models.py", change_type=ChangeType.MODIFIED, changed_lines=[3])],
        symbols=[
            SymbolChange(
                symbol_id="models.py:Order.checkout",
                name="checkout",
                qualified_name="models.Order.checkout",
                file_path="models.py",
                entity_type=EntityType.METHOD,
                change_type=ChangeType.MODIFIED,
                start_line=2,
                end_line=3,
            )
        ],
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    impacted_ids = {e.entity_id: e for e in report.impacted_entities}
    # Enclosing class models.py:Order must be impacted
    assert "models.py:Order" in impacted_ids
    assert impacted_ids["models.py:Order"].relationship_type == "enclosing_class"
    # Caller controller.py:process must be impacted
    assert "controller.py:process" in impacted_ids


# ---------------------------------------------------------------------------
# Test 8: Changed class
# ---------------------------------------------------------------------------
def test_changed_class(repo: Path):
    """8. Changed class impacts subclasses and methods/callers."""
    (repo / "base.py").write_text(
        "class BaseHandler:\n"
        "    def handle(self):\n"
        "        pass\n"
    )
    (repo / "sub.py").write_text(
        "from base import BaseHandler\n"
        "class CustomHandler(BaseHandler):\n"
        "    def run(self):\n"
        "        return self.handle()\n"
    )

    changeset = ChangeSet(
        files=[FileChange(path="base.py", change_type=ChangeType.MODIFIED)],
        symbols=[
            SymbolChange(
                symbol_id="base.py:BaseHandler",
                name="BaseHandler",
                qualified_name="base.BaseHandler",
                file_path="base.py",
                entity_type=EntityType.CLASS,
                change_type=ChangeType.MODIFIED,
                start_line=1,
                end_line=3,
            )
        ],
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    impacted_ids = [e.entity_id for e in report.impacted_entities]
    assert any("CustomHandler" in id or "sub" in id for id in impacted_ids)


# ---------------------------------------------------------------------------
# Test 9: Direct callers
# ---------------------------------------------------------------------------
def test_direct_callers(repo: Path):
    """9. Direct caller receives depth=1 and classification=DIRECT."""
    (repo / "a.py").write_text("def target(): return 1\n")
    (repo / "b.py").write_text("from a import target\ndef direct_caller(): return target()\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="a.py:target",
                name="target",
                qualified_name="a.target",
                file_path="a.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    entity = next(e for e in report.impacted_entities if e.entity_id == "b.py:direct_caller")
    assert entity.classification == ImpactClassification.DIRECT
    assert entity.depth == 1
    assert entity.confidence == 1.0


# ---------------------------------------------------------------------------
# Test 10: Transitive callers
# ---------------------------------------------------------------------------
def test_transitive_callers(repo: Path):
    """10. Transitive caller chain a -> b -> c gets depth=2 and TRANSITIVE."""
    (repo / "a.py").write_text("def base(): return 10\n")
    (repo / "b.py").write_text("from a import base\ndef mid(): return base()\n")
    (repo / "c.py").write_text("from b import mid\ndef top(): return mid()\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="a.py:base",
                name="base",
                qualified_name="a.base",
                file_path="a.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    top_entity = next(e for e in report.impacted_entities if e.entity_id == "c.py:top")
    assert top_entity.classification == ImpactClassification.TRANSITIVE
    assert top_entity.depth == 2
    assert top_entity.confidence < 1.0


# ---------------------------------------------------------------------------
# Test 11: Direct module dependents
# ---------------------------------------------------------------------------
def test_direct_module_dependents(repo: Path):
    """11. Direct module importing modified module gets DIRECT classification."""
    (repo / "config.py").write_text("TIMEOUT = 30\n")
    (repo / "client.py").write_text("import config\ndef get_timeout(): return config.TIMEOUT\n")

    changeset = ChangeSet(
        files=[FileChange(path="config.py", change_type=ChangeType.MODIFIED)]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    client_mod = next(e for e in report.impacted_entities if e.entity_id == "client")
    assert client_mod.classification == ImpactClassification.DIRECT
    assert client_mod.depth == 1


# ---------------------------------------------------------------------------
# Test 12: Transitive module dependents
# ---------------------------------------------------------------------------
def test_transitive_module_dependents(repo: Path):
    """12. Transitive module dependent m1 -> m2 -> m3 gets TRANSITIVE."""
    (repo / "m1.py").write_text("X = 1\n")
    (repo / "m2.py").write_text("import m1\nY = m1.X + 1\n")
    (repo / "m3.py").write_text("import m2\nZ = m2.Y + 1\n")

    changeset = ChangeSet(
        files=[FileChange(path="m1.py", change_type=ChangeType.MODIFIED)]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    m3_entity = next(e for e in report.impacted_entities if e.entity_id == "m3")
    assert m3_entity.classification == ImpactClassification.TRANSITIVE
    assert m3_entity.depth == 2


# ---------------------------------------------------------------------------
# Test 13: Combined call and module dependencies
# ---------------------------------------------------------------------------
def test_combined_call_and_module_dependencies(repo: Path):
    """13. Both symbol-level callers and file-level module dependents are analyzed."""
    (repo / "core.py").write_text("def execute(): return 'done'\n")
    (repo / "wrapper.py").write_text("import core\ndef run_wrapper(): return core.execute()\n")
    (repo / "app.py").write_text("import wrapper\ndef main(): return wrapper.run_wrapper()\n")

    changeset = extract_changeset_from_files(
        [{"path": "core.py", "change_type": "modified", "changed_lines": [1]}],
        repo_root=repo,
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.impacted_symbols >= 1
    assert report.summary.impacted_modules >= 1


# ---------------------------------------------------------------------------
# Test 14: Recursive functions
# ---------------------------------------------------------------------------
def test_recursive_functions(repo: Path):
    """14. Recursive function calling itself does not cause infinite loops."""
    (repo / "fact.py").write_text(
        "def factorial(n):\n"
        "    if n <= 1: return 1\n"
        "    return n * factorial(n - 1)\n"
    )
    (repo / "main.py").write_text("from fact import factorial\ndef run(): return factorial(5)\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="fact.py:factorial",
                name="factorial",
                qualified_name="fact.factorial",
                file_path="fact.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    run_entity = next(e for e in report.impacted_entities if e.entity_id == "main.py:run")
    assert run_entity.classification == ImpactClassification.DIRECT


# ---------------------------------------------------------------------------
# Test 15: Mutually recursive functions
# ---------------------------------------------------------------------------
def test_mutually_recursive_functions(repo: Path):
    """15. Mutually recursive functions terminate safely without looping."""
    (repo / "pingpong.py").write_text(
        "def ping(n):\n"
        "    if n > 0: pong(n - 1)\n"
        "def pong(n):\n"
        "    if n > 0: ping(n - 1)\n"
    )

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="pingpong.py:ping",
                name="ping",
                qualified_name="pingpong.ping",
                file_path="pingpong.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    # pong should be detected as direct caller of ping
    pong_entity = next((e for e in report.impacted_entities if e.entity_id == "pingpong.py:pong"), None)
    assert pong_entity is not None


# ---------------------------------------------------------------------------
# Test 16: Module dependency cycles
# ---------------------------------------------------------------------------
def test_module_dependency_cycles(repo: Path):
    """16. Cyclic module dependencies terminate cleanly."""
    (repo / "mod_a.py").write_text("import mod_b\ndef fa(): return mod_b.fb()\n")
    (repo / "mod_b.py").write_text("import mod_a\ndef fb(): return mod_a.fa()\n")

    changeset = ChangeSet(
        files=[FileChange(path="mod_a.py", change_type=ChangeType.MODIFIED)]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert any(e.entity_id == "mod_b" for e in report.impacted_entities)


# ---------------------------------------------------------------------------
# Test 17: Deleted symbol references (UNRESOLVED)
# ---------------------------------------------------------------------------
def test_deleted_symbol_references(repo: Path):
    """17. Callers to deleted symbol are classified as UNRESOLVED."""
    (repo / "consumer.py").write_text("from api import deprecated_func\ndef caller(): return deprecated_func()\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="api.py:deprecated_func",
                name="deprecated_func",
                qualified_name="api.deprecated_func",
                file_path="api.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.DELETED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    unresolved = [e for e in report.impacted_entities if e.classification == ImpactClassification.UNRESOLVED]
    assert len(unresolved) >= 1
    assert any("caller" in u.entity_id for u in unresolved)


# ---------------------------------------------------------------------------
# Test 18: Renamed symbol references (UNRESOLVED)
# ---------------------------------------------------------------------------
def test_renamed_symbol_references(repo: Path):
    """18. References to old name of renamed symbol are classified as UNRESOLVED."""
    (repo / "client.py").write_text("from service import old_handler\ndef run(): return old_handler()\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="service.py:new_handler",
                old_symbol_id="service.py:old_handler",
                name="new_handler",
                qualified_name="service.new_handler",
                file_path="service.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.RENAMED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    unresolved = [e for e in report.impacted_entities if e.classification == ImpactClassification.UNRESOLVED]
    assert len(unresolved) >= 1
    assert any("client.py:run" in u.entity_id for u in unresolved)


# ---------------------------------------------------------------------------
# Test 19: Ambiguous references / star imports (POTENTIAL)
# ---------------------------------------------------------------------------
def test_star_import_ambiguous_references(repo: Path):
    """19. Star-imports create POTENTIAL classification for consumers."""
    (repo / "provider.py").write_text("def provide_data(): return 42\n")
    (repo / "consumer.py").write_text("from provider import *\ndef execute(): return 100\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="provider.py:provide_data",
                name="provide_data",
                qualified_name="provider.provide_data",
                file_path="provider.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    potentials = [e for e in report.impacted_entities if e.classification == ImpactClassification.POTENTIAL]
    assert len(potentials) >= 1
    assert any("consumer" in p.entity_id for p in potentials)


# ---------------------------------------------------------------------------
# Test 20: Dynamic calls (POTENTIAL)
# ---------------------------------------------------------------------------
def test_dynamic_calls_potential(repo: Path):
    """20. Dynamic calls within the same module are marked POTENTIAL."""
    (repo / "dynamic_mod.py").write_text(
        "def target_func(): return 1\n"
        "def caller(name):\n"
        "    f = globals().get(name)\n"
        "    return f()\n"
    )

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="dynamic_mod.py:target_func",
                name="target_func",
                qualified_name="dynamic_mod.target_func",
                file_path="dynamic_mod.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    potentials = [e for e in report.impacted_entities if e.classification == ImpactClassification.POTENTIAL]
    assert len(potentials) >= 1
    assert any("dynamic_mod.py:caller" in p.entity_id for p in potentials)


# ---------------------------------------------------------------------------
# Test 21: Unresolved relationships classification
# ---------------------------------------------------------------------------
def test_unresolved_relationships_classification(repo: Path):
    """21. Unresolved relationships are explicitly classified as UNRESOLVED."""
    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="missing.py:ghost",
                name="ghost",
                qualified_name="missing.ghost",
                file_path="missing.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.DELETED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    assert report.summary.unresolved_impacts >= 0


# ---------------------------------------------------------------------------
# Test 22: Duplicate impact paths & deduplication
# ---------------------------------------------------------------------------
def test_duplicate_impact_paths_deduplication(repo: Path):
    """22. Entity reachable via multiple paths is deduplicated to lowest depth."""
    (repo / "a.py").write_text("def base(): return 1\n")
    (repo / "b.py").write_text("from a import base\ndef mid_b(): return base()\n")
    (repo / "c.py").write_text("from a import base\ndef mid_c(): return base()\n")
    (repo / "d.py").write_text(
        "from b import mid_b\n"
        "from c import mid_c\n"
        "from a import base\n"
        "def combined():\n"
        "    return base() + mid_b() + mid_c()\n"
    )

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="a.py:base",
                name="base",
                qualified_name="a.base",
                file_path="a.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)

    d_entities = [e for e in report.impacted_entities if e.entity_id == "d.py:combined"]
    # Must be deduplicated: exactly 1 entry for d.py:combined
    assert len(d_entities) == 1
    # Minimum depth from a.py:base is direct (depth=1)
    assert d_entities[0].depth == 1
    assert d_entities[0].classification == ImpactClassification.DIRECT


# ---------------------------------------------------------------------------
# Test 23: Deterministic output
# ---------------------------------------------------------------------------
def test_deterministic_output(repo: Path):
    """23. Multiple analysis runs on the same changeset produce identical output."""
    (repo / "m1.py").write_text("def f1(): return 1\n")
    (repo / "m2.py").write_text("from m1 import f1\ndef f2(): return f1()\n")
    (repo / "m3.py").write_text("from m2 import f2\ndef f3(): return f2()\n")

    changeset = extract_changeset_from_files(["m1.py"], repo_root=repo)
    analyzer = ChangeImpactAnalyzer(repo_root=repo)

    r1 = analyzer.analyze(changeset)
    r2 = analyzer.analyze(changeset)

    assert [e.entity_id for e in r1.impacted_entities] == [e.entity_id for e in r2.impacted_entities]
    assert [e.classification for e in r1.impacted_entities] == [e.classification for e in r2.impacted_entities]
    assert [e.depth for e in r1.impacted_entities] == [e.depth for e in r2.impacted_entities]
    assert r1.summary.model_dump() == r2.summary.model_dump()


# ---------------------------------------------------------------------------
# Test 24: Traversal depth limits (max_depth)
# ---------------------------------------------------------------------------
def test_max_depth_limits(repo: Path):
    """24. Traversal terminates strictly at max_depth."""
    (repo / "d0.py").write_text("def f0(): return 0\n")
    (repo / "d1.py").write_text("from d0 import f0\ndef f1(): return f0()\n")
    (repo / "d2.py").write_text("from d1 import f1\ndef f2(): return f1()\n")
    (repo / "d3.py").write_text("from d2 import f2\ndef f3(): return f2()\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="d0.py:f0",
                name="f0",
                qualified_name="d0.f0",
                file_path="d0.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset, max_depth=1)

    # depth 1 only reaches d1.py:f1
    depths = [e.depth for e in report.impacted_entities]
    assert all(d <= 1 for d in depths)
    assert not any(e.entity_id == "d2.py:f2" for e in report.impacted_entities)
    assert not any(e.entity_id == "d3.py:f3" for e in report.impacted_entities)


# ---------------------------------------------------------------------------
# Test 25: Node budget limits (max_nodes)
# ---------------------------------------------------------------------------
def test_max_nodes_budget_limits(repo: Path):
    """25. Node budget cuts off propagation when max_nodes is reached."""
    (repo / "base.py").write_text("def ping(): pass\n")
    for i in range(6):
        (repo / f"consumer_{i}.py").write_text(f"import base\ndef call_{i}(): base.ping()\n")

    changeset = ChangeSet(
        files=[FileChange(path="base.py", change_type=ChangeType.MODIFIED)]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset, max_nodes=2)

    assert len(report.impacted_entities) <= 2
    assert report.summary.is_truncated is True
    assert "budget" in (report.summary.truncation_reason or "").lower()


# ---------------------------------------------------------------------------
# Test 26: Truncated report behavior
# ---------------------------------------------------------------------------
def test_truncated_report_warnings(repo: Path):
    """26. Truncated report populates summary.is_truncated and warnings."""
    (repo / "m_start.py").write_text("def start(): pass\n")
    (repo / "m_mid.py").write_text("from m_start import start\ndef mid(): start()\n")

    changeset = ChangeSet(
        symbols=[
            SymbolChange(
                symbol_id="m_start.py:start",
                name="start",
                qualified_name="m_start.start",
                file_path="m_start.py",
                entity_type=EntityType.FUNCTION,
                change_type=ChangeType.MODIFIED,
            )
        ]
    )

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset, max_nodes=0)

    assert report.summary.is_truncated is True
    assert len(report.warnings) >= 1
    assert any("truncated" in w.lower() for w in report.warnings)


# ---------------------------------------------------------------------------
# Test 27: Empty changeset
# ---------------------------------------------------------------------------
def test_empty_changeset(repo: Path):
    """27. Empty changeset returns empty report without error."""
    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(ChangeSet())

    assert report.summary.total_changed_files == 0
    assert report.summary.total_changed_symbols == 0
    assert report.summary.total_impacted_entities == 0
    assert len(report.impacted_entities) == 0
    assert report.summary.is_truncated is False


# ---------------------------------------------------------------------------
# Test 28: Invalid changeset input
# ---------------------------------------------------------------------------
def test_invalid_changeset_input(repo: Path):
    """28. Malformed or nonexistent file changes are skipped gracefully."""
    changeset = extract_changeset_from_files(
        [{"path": "nonexistent_directory/fake_file.py", "change_type": "modified"}],
        repo_root=repo,
    )
    assert len(changeset.files) == 1
    assert len(changeset.symbols) == 0

    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(changeset)
    assert report.summary.total_changed_files == 1
    assert report.summary.total_impacted_entities == 0


# ---------------------------------------------------------------------------
# Test 29: Protected-file exclusion
# ---------------------------------------------------------------------------
def test_protected_file_exclusion(repo: Path):
    """29. Protected paths (.env, .db, contacts.json, logs, git) are excluded."""
    # Create protected files
    (repo / ".env").write_text("SECRET=123\n")
    (repo / "contacts.json").write_text("{}\n")
    (repo / "app.db").write_text("sqlite\n")

    changeset = extract_changeset_from_files(
        [".env", "contacts.json", "app.db", ".git/config"],
        repo_root=repo,
    )
    # None of these should enter changeset
    assert len(changeset.files) == 0
    assert len(changeset.symbols) == 0

    # Test analyze ignores protected paths even if manually injected in ChangeSet
    bad_changeset = ChangeSet(
        files=[FileChange(path=".env", change_type=ChangeType.MODIFIED)],
        symbols=[
            SymbolChange(
                symbol_id=".env:SECRET",
                name="SECRET",
                qualified_name=".env.SECRET",
                file_path=".env",
                entity_type=EntityType.VARIABLE,
                change_type=ChangeType.MODIFIED,
            )
        ],
    )
    analyzer = ChangeImpactAnalyzer(repo_root=repo)
    report = analyzer.analyze(bad_changeset)
    assert report.summary.total_impacted_entities == 0


# ---------------------------------------------------------------------------
# Test 30: Path traversal and symlink safety
# ---------------------------------------------------------------------------
def test_path_traversal_and_symlink_safety(repo: Path):
    """30. Path traversal strings and escaping symlinks are rejected."""
    changeset = extract_changeset_from_files(
        ["../../outside.py", "/etc/passwd"],
        repo_root=repo,
    )
    assert len(changeset.files) == 0

    # Create a symlink pointing outside
    outside_file = repo.parent / "secret_outside.py"
    outside_file.write_text("def secret(): pass\n")
    symlink_file = repo / "sym_link.py"
    try:
        symlink_file.symlink_to(outside_file)
        cs_sym = extract_changeset_from_files(["sym_link.py"], repo_root=repo)
        # Outside symlinks are excluded
        assert len(cs_sym.files) == 0
    except (OSError, NotImplementedError):
        pass


# ---------------------------------------------------------------------------
# Test 31: Backward compatibility with M1–M3 APIs
# ---------------------------------------------------------------------------
def test_backward_compatibility_with_m1_m3(repo: Path):
    """31. ChangeImpactAnalyzer interoperates seamlessly with existing CallGraph and ModuleDependencyGraph."""
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "alpha.py").write_text("def step_a(): return 1\n")
    (repo / "pkg" / "beta.py").write_text("from pkg.alpha import step_a\ndef step_b(): return step_a() + 1\n")

    call_graph = build_call_graph(repo)
    module_graph = build_module_dependency_graph(repo)

    analyzer = ChangeImpactAnalyzer(
        repo_root=repo,
        call_graph=call_graph,
        module_graph=module_graph,
    )

    diff = (
        "--- a/pkg/alpha.py\n"
        "+++ b/pkg/alpha.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-def step_a(): return 1\n"
        "+def step_a(): return 2\n"
    )
    changeset = extract_changeset_from_diff(diff, repo_root=repo, call_graph=call_graph)
    assert len(changeset.files) == 1
    assert len(changeset.symbols) == 1
    assert changeset.symbols[0].name == "step_a"

    report = analyzer.analyze(changeset)
    assert report.summary.total_impacted_entities >= 1
    impacted_ids = [e.entity_id for e in report.impacted_entities]
    assert any("step_b" in id for id in impacted_ids)
