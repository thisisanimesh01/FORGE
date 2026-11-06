"""Comprehensive unit tests for Module Dependency Graph and Cycle Detection (Phase 11 Milestone 3)."""

from pathlib import Path
import pytest

from forge.repository.graph_schemas import (
    CallResolutionStatus,
    DependencyCycle,
    ExternalImportRecord,
    ImportEvidence,
    ModuleDependencyGraphSummary,
    ModuleEdge,
    ModuleNode,
    StronglyConnectedComponent,
    UnresolvedImportRecord,
)
from forge.repository.module_graph import (
    ModuleDependencyGraph,
    build_module_dependency_graph,
)
from forge.repository.call_graph import build_call_graph
from forge.repository.indexer import build_symbol_index


@pytest.fixture
def clean_workspace(tmp_path: Path) -> Path:
    """Fixture providing a fresh isolated directory for mock workspace repos."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_basic_module_dependency_extraction(clean_workspace: Path):
    """1. Basic module dependency extraction between two simple modules."""
    (clean_workspace / "mod_a.py").write_text("import mod_b\ndef func_a(): return mod_b.func_b()\n")
    (clean_workspace / "mod_b.py").write_text("def func_b(): return 42\n")

    graph = build_module_dependency_graph(clean_workspace)
    assert len(graph.nodes) == 2
    assert "mod_a" in graph.nodes
    assert "mod_b" in graph.nodes

    deps = graph.get_dependencies("mod_a")
    assert deps == ["mod_b"]
    assert graph.get_dependents("mod_b") == ["mod_a"]

    edge = graph.get_edge("mod_a", "mod_b")
    assert edge is not None
    assert edge.source_module == "mod_a"
    assert edge.target_module == "mod_b"
    assert edge.weight == 1
    assert len(edge.evidence) == 1
    assert edge.evidence[0].imported_name == "mod_b"
    assert edge.evidence[0].import_type == "direct"


def test_absolute_imports(clean_workspace: Path):
    """2. Absolute imports: direct module import and from-import with submodules."""
    pkg = clean_workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "service.py").write_text("def run(): pass\n")
    (clean_workspace / "main.py").write_text(
        "import pkg.service\nfrom pkg.service import run\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    assert "main" in graph.nodes
    assert "pkg.service" in graph.nodes

    deps = graph.get_dependencies("main")
    assert "pkg.service" in deps

    edge = graph.get_edge("main", "pkg.service")
    assert edge is not None
    assert edge.weight == 2
    assert len(edge.evidence) == 2
    types = [ev.import_type for ev in edge.evidence]
    assert "direct" in types
    assert "from" in types


def test_relative_imports(clean_workspace: Path):
    """3. Relative imports: same directory (.) and parent directory (..)."""
    pkg = clean_workspace / "mypackage"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def base(): pass\n")

    sub = pkg / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "worker.py").write_text("def work(): pass\n")
    (sub / "manager.py").write_text(
        "from .worker import work\nfrom ..core import base\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    assert "mypackage.sub.manager" in graph.nodes
    assert "mypackage.sub.worker" in graph.nodes
    assert "mypackage.core" in graph.nodes

    deps = graph.get_dependencies("mypackage.sub.manager")
    assert "mypackage.core" in deps
    assert "mypackage.sub.worker" in deps

    edge_worker = graph.get_edge("mypackage.sub.manager", "mypackage.sub.worker")
    assert edge_worker is not None
    assert edge_worker.evidence[0].import_type == "relative"
    assert edge_worker.evidence[0].level == 1

    edge_core = graph.get_edge("mypackage.sub.manager", "mypackage.core")
    assert edge_core is not None
    assert edge_core.evidence[0].import_type == "relative"
    assert edge_core.evidence[0].level == 2


def test_aliased_imports(clean_workspace: Path):
    """4. Aliased imports: import X as Y and from X import Y as Z."""
    (clean_workspace / "utils.py").write_text("def helper(): return 1\n")
    (clean_workspace / "consumer.py").write_text(
        "import utils as u\nfrom utils import helper as h\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    deps = graph.get_dependencies("consumer")
    assert deps == ["utils"]

    edge = graph.get_edge("consumer", "utils")
    assert edge is not None
    assert edge.weight == 2
    aliases = [ev.alias for ev in edge.evidence]
    assert "u" in aliases
    assert "h" in aliases


def test_package_initialization_relationships(clean_workspace: Path):
    """5. Package __init__.py importing submodules and being imported."""
    pkg = clean_workspace / "auth"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .models import User\n")
    (pkg / "models.py").write_text("class User: pass\n")

    (clean_workspace / "app.py").write_text("from auth import User\n")

    graph = build_module_dependency_graph(clean_workspace)
    assert graph.nodes["auth"].is_package is True
    assert graph.nodes["auth.models"].is_package is False

    # auth/__init__.py depends on auth/models.py
    assert "auth.models" in graph.get_dependencies("auth")

    # app.py depends on auth
    assert "auth" in graph.get_dependencies("app")


def test_multiple_imports_and_duplicate_consolidation(clean_workspace: Path):
    """6 & 7. Multiple imports from same module consolidated into 1 edge with multiple evidence."""
    (clean_workspace / "helpers.py").write_text(
        "def a(): pass\ndef b(): pass\ndef c(): pass\n"
    )
    (clean_workspace / "app.py").write_text(
        "from helpers import a\n"
        "from helpers import b\n"
        "from helpers import c\n"
        "import helpers\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    deps = graph.get_dependencies("app")
    assert deps == ["helpers"]

    edge = graph.get_edge("app", "helpers")
    assert edge is not None
    assert edge.weight == 4
    assert len(edge.evidence) == 4
    lines = [ev.line_number for ev in edge.evidence]
    assert lines == [1, 2, 3, 4]


def test_standard_library_classification(clean_workspace: Path):
    """8. Standard-library imports classified properly in external_imports."""
    (clean_workspace / "std_consumer.py").write_text(
        "import sys\nimport os.path\nfrom typing import List, Optional\nfrom pathlib import Path\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    assert len(graph.edges) == 0  # No internal dependencies

    ext = graph.get_external_dependencies("std_consumer")
    assert len(ext) >= 4
    stdlib_packages = {e.package_name for e in ext if e.is_stdlib}
    assert "sys" in stdlib_packages
    assert "os" in stdlib_packages
    assert "typing" in stdlib_packages
    assert "pathlib" in stdlib_packages

    summary = graph.get_dependency_summary()
    assert summary.total_stdlib_imports >= 4
    assert summary.total_third_party_imports == 0


def test_third_party_classification(clean_workspace: Path):
    """9. Third-party imports classified properly."""
    (clean_workspace / "third_party_consumer.py").write_text(
        "import pytest\nimport pydantic\nfrom requests import get\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    ext = graph.get_external_dependencies("third_party_consumer")
    assert len(ext) == 3

    for item in ext:
        assert item.is_stdlib is False

    pkgs = {e.package_name for e in ext}
    assert pkgs == {"pytest", "pydantic", "requests"}

    summary = graph.get_dependency_summary()
    assert summary.total_third_party_imports == 3


def test_unresolved_imports(clean_workspace: Path):
    """10. Unresolved imports recorded in unresolved_imports without creating internal edges."""
    (clean_workspace / "unresolved_consumer.py").write_text(
        "from ...nonexistent_parent import ghost\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    assert len(graph.edges) == 0

    unres = graph.get_unresolved_imports("unresolved_consumer")
    assert len(unres) == 1
    assert unres[0].imported_name == "ghost"
    assert "escapes workspace root" in unres[0].reason

    summary = graph.get_dependency_summary()
    assert summary.total_unresolved_imports == 1


def test_star_import_resolution(clean_workspace: Path):
    """11. Star import from internal module resolves cleanly."""
    (clean_workspace / "constants.py").write_text("A = 1\nB = 2\n")
    (clean_workspace / "importer.py").write_text("from constants import *\n")

    graph = build_module_dependency_graph(clean_workspace)
    assert "constants" in graph.get_dependencies("importer")

    edge = graph.get_edge("importer", "constants")
    assert edge is not None
    assert edge.evidence[0].import_type == "star"
    assert edge.evidence[0].imported_name == "*"


def test_self_dependency_cycle(clean_workspace: Path):
    """12. Direct self-dependency detected (A -> A, length 1)."""
    (clean_workspace / "self_ref.py").write_text(
        "import self_ref\ndef loop(): return self_ref.loop()\n"
    )

    graph = build_module_dependency_graph(clean_workspace)
    cycles = graph.detect_cycles()
    assert len(cycles) == 1
    c = cycles[0]
    assert c.is_self_cycle is True
    assert c.length == 1
    assert c.modules == ["self_ref", "self_ref"]
    assert c.edges == [("self_ref", "self_ref")]
    assert graph.is_in_cycle("self_ref") is True


def test_two_module_cycle(clean_workspace: Path):
    """13. Two-module cycle detected (A -> B -> A)."""
    (clean_workspace / "mod_x.py").write_text("import mod_y\ndef fx(): pass\n")
    (clean_workspace / "mod_y.py").write_text("import mod_x\ndef fy(): pass\n")

    graph = build_module_dependency_graph(clean_workspace)
    cycles = graph.detect_cycles()
    assert len(cycles) == 1
    c = cycles[0]
    assert c.length == 2
    assert c.is_self_cycle is False
    assert c.modules == ["mod_x", "mod_y", "mod_x"]
    assert graph.is_in_cycle("mod_x") is True
    assert graph.is_in_cycle("mod_y") is True


def test_three_module_cycle(clean_workspace: Path):
    """14. Three-module cycle detected (A -> B -> C -> A)."""
    (clean_workspace / "mod_1.py").write_text("import mod_2\n")
    (clean_workspace / "mod_2.py").write_text("import mod_3\n")
    (clean_workspace / "mod_3.py").write_text("import mod_1\n")

    graph = build_module_dependency_graph(clean_workspace)
    cycles = graph.detect_cycles()
    assert len(cycles) == 1
    c = cycles[0]
    assert c.length == 3
    assert c.modules == ["mod_1", "mod_2", "mod_3", "mod_1"]
    assert graph.is_in_cycle("mod_1") is True
    assert graph.is_in_cycle("mod_2") is True
    assert graph.is_in_cycle("mod_3") is True


def test_long_dependency_cycle(clean_workspace: Path):
    """15. Long dependency cycle (6 modules: 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 0)."""
    n = 6
    for i in range(n):
        nxt = (i + 1) % n
        (clean_workspace / f"node_{i}.py").write_text(f"import node_{nxt}\n")

    graph = build_module_dependency_graph(clean_workspace)
    cycles = graph.detect_cycles()
    assert len(cycles) == 1
    c = cycles[0]
    assert c.length == n
    expected_path = [f"node_{i}" for i in range(n)] + ["node_0"]
    assert c.modules == expected_path


def test_multiple_independent_cycles(clean_workspace: Path):
    """16. Multiple independent cycles detected in separate components."""
    # Component 1: A <-> B
    (clean_workspace / "alpha.py").write_text("import beta\n")
    (clean_workspace / "beta.py").write_text("import alpha\n")

    # Component 2: X -> Y -> Z -> X
    (clean_workspace / "chi.py").write_text("import psi\n")
    (clean_workspace / "psi.py").write_text("import omega\n")
    (clean_workspace / "omega.py").write_text("import chi\n")

    # Acyclic standalone module
    (clean_workspace / "standalone.py").write_text("x = 10\n")

    graph = build_module_dependency_graph(clean_workspace)
    cycles = graph.detect_cycles()
    assert len(cycles) == 2

    lengths = {c.length for c in cycles}
    assert lengths == {2, 3}

    cyclic_sccs = graph.get_strongly_connected_components(only_cyclic=True)
    assert len(cyclic_sccs) == 2

    assert graph.is_in_cycle("alpha") is True
    assert graph.is_in_cycle("chi") is True
    assert graph.is_in_cycle("standalone") is False


def test_strongly_connected_components_tarjan(clean_workspace: Path):
    """17. Strongly connected component correctness and partitioning."""
    # Subgraph 1: SCC of {a, b}
    (clean_workspace / "a.py").write_text("import b\nimport c\n")
    (clean_workspace / "b.py").write_text("import a\n")

    # Subgraph 2: c -> d (acyclic chain)
    (clean_workspace / "c.py").write_text("import d\n")
    (clean_workspace / "d.py").write_text("pass\n")

    graph = build_module_dependency_graph(clean_workspace)
    all_sccs = graph.get_strongly_connected_components()

    # Total 3 SCCs: {a, b} (cyclic), {c} (acyclic), {d} (acyclic)
    assert len(all_sccs) == 3

    cyclic = [s for s in all_sccs if s.is_cyclic]
    assert len(cyclic) == 1
    assert set(cyclic[0].modules) == {"a", "b"}

    acyclic = [s for s in all_sccs if not s.is_cyclic]
    assert len(acyclic) == 2


def test_deterministic_cycle_output(clean_workspace: Path):
    """18. Deterministic cycle output across repeated indexing runs."""
    (clean_workspace / "c1.py").write_text("import c2\n")
    (clean_workspace / "c2.py").write_text("import c3\n")
    (clean_workspace / "c3.py").write_text("import c1\n")
    (clean_workspace / "b1.py").write_text("import b2\n")
    (clean_workspace / "b2.py").write_text("import b1\n")

    graph1 = build_module_dependency_graph(clean_workspace)
    cycles1 = [c.cycle_id for c in graph1.detect_cycles()]

    graph2 = build_module_dependency_graph(clean_workspace)
    cycles2 = [c.cycle_id for c in graph2.detect_cycles()]

    assert cycles1 == cycles2
    assert len(cycles1) == 2


def test_transitive_dependency_and_dependent_queries(clean_workspace: Path):
    """19 & 20. Forward transitive dependencies and reverse transitive dependents."""
    # Chain: A -> B -> C -> D
    (clean_workspace / "mod_a.py").write_text("import mod_b\n")
    (clean_workspace / "mod_b.py").write_text("import mod_c\n")
    (clean_workspace / "mod_c.py").write_text("import mod_d\n")
    (clean_workspace / "mod_d.py").write_text("x = 1\n")

    graph = build_module_dependency_graph(clean_workspace)

    # Transitive dependencies of A: [B, C, D]
    t_deps = graph.get_transitive_dependencies("mod_a", max_depth=5)
    assert t_deps == ["mod_b", "mod_c", "mod_d"]

    # Transitive dependents of D: [C, B, A]
    t_dependents = graph.get_transitive_dependents("mod_d", max_depth=5)
    assert set(t_dependents) == {"mod_c", "mod_b", "mod_a"}


def test_bounded_traversal(clean_workspace: Path):
    """21. Bounded traversal enforces max_depth and max_nodes."""
    # Chain: A -> B -> C -> D
    (clean_workspace / "mod_a.py").write_text("import mod_b\n")
    (clean_workspace / "mod_b.py").write_text("import mod_c\n")
    (clean_workspace / "mod_c.py").write_text("import mod_d\n")
    (clean_workspace / "mod_d.py").write_text("x = 1\n")

    graph = build_module_dependency_graph(clean_workspace)

    # max_depth = 1 gives only direct dependencies
    deps_depth_1 = graph.get_transitive_dependencies("mod_a", max_depth=1)
    assert deps_depth_1 == ["mod_b"]

    # max_depth = 2 gives direct and 2nd level
    deps_depth_2 = graph.get_transitive_dependencies("mod_a", max_depth=2)
    assert deps_depth_2 == ["mod_b", "mod_c"]

    # max_nodes = 1 caps at 1 node
    deps_capped = graph.get_transitive_dependencies("mod_a", max_depth=5, max_nodes=1)
    assert len(deps_capped) == 1


def test_empty_and_disconnected_graphs(clean_workspace: Path):
    """22. Empty repository and disconnected graph with isolated modules."""
    # Empty workspace
    empty_graph = build_module_dependency_graph(clean_workspace)
    assert len(empty_graph.nodes) == 0
    assert len(empty_graph.edges) == 0
    assert empty_graph.get_dependencies("nonexistent") == []

    # Disconnected workspace
    (clean_workspace / "iso1.py").write_text("a = 1\n")
    (clean_workspace / "iso2.py").write_text("b = 2\n")

    graph = build_module_dependency_graph(clean_workspace)
    assert len(graph.nodes) == 2
    assert len(graph.edges) == 0
    isolated = graph.get_isolated_modules()
    assert isolated == ["iso1", "iso2"]
    assert graph.summary.isolated_modules == 2


def test_syntax_error_resilience(clean_workspace: Path):
    """23. Files with syntax errors do not crash graph construction."""
    (clean_workspace / "valid.py").write_text("import broken\nx = 1\n")
    (clean_workspace / "broken.py").write_text("def def def syntax error!! @@\n")

    graph = build_module_dependency_graph(clean_workspace)
    assert "valid" in graph.nodes
    assert "broken" in graph.nodes
    # The valid module still depends on broken
    assert "broken" in graph.get_dependencies("valid")


def test_path_containment_and_symlinks(clean_workspace: Path, tmp_path: Path):
    """24. Symlink pointing outside workspace root is rejected fail-closed."""
    outside_dir = tmp_path / "outside_secret"
    outside_dir.mkdir()
    (outside_dir / "secret.py").write_text("SECRET_KEY = 'leak'\n")

    symlink_file = clean_workspace / "external_link.py"
    try:
        symlink_file.symlink_to(outside_dir / "secret.py")
    except OSError:
        pytest.skip("Symlinks not supported on this platform")

    (clean_workspace / "app.py").write_text("x = 1\n")

    graph = build_module_dependency_graph(clean_workspace)
    # external_link.py escapes workspace root so it must not be in nodes
    assert "external_link" not in graph.nodes
    assert "app" in graph.nodes


def test_protected_files_excluded_from_graph(clean_workspace: Path):
    """25. Protected files (.env, contacts.json, app.db) excluded from module graph."""
    (clean_workspace / ".env").write_text("DB_PASSWORD=secret\n")
    (clean_workspace / "app.db").write_text("sqlite format 3\n")
    (clean_workspace / "contacts.json").write_text('{"name": "test"}\n')
    (clean_workspace / "app.py").write_text("def run(): pass\n")

    graph = build_module_dependency_graph(clean_workspace)
    for node in graph.nodes.values():
        assert not node.file_path.endswith(".env")
        assert not node.file_path.endswith("contacts.json")
        assert not node.file_path.endswith("app.db")


def test_backward_compatibility_with_m2_apis(clean_workspace: Path):
    """26. M2 CallGraph and SymbolIndex APIs remain 100% operational alongside M3."""
    (clean_workspace / "service.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        return self.step()\n"
        "    def step(self):\n"
        "        return 1\n"
    )

    call_graph = build_call_graph(clean_workspace)
    assert len(call_graph.nodes) >= 2

    symbol_index = build_symbol_index(clean_workspace)
    assert symbol_index.total_classes >= 1
    assert symbol_index.total_methods >= 2

    # M3 Module graph builds cleanly alongside
    mod_graph = build_module_dependency_graph(clean_workspace)
    assert "service" in mod_graph.nodes
