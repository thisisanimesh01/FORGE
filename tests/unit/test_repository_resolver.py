"""Comprehensive unit tests for WorkspaceImportResolver in Phase 11 Milestone 2."""

from pathlib import Path
import pytest

from forge.repository.graph_schemas import CallResolutionStatus, ImportRecord
from forge.repository.resolver import WorkspaceImportResolver, ResolvedImport


@pytest.fixture
def mock_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "test_workspace"
    ws.mkdir()

    # Package structure:
    # test_workspace/
    #   ├── root_mod.py
    #   ├── pkg_a/
    #   │     ├── __init__.py
    #   │     ├── worker.py
    #   │     └── sub/
    #   │           ├── __init__.py
    #   │           └── helper.py
    #   └── pkg_b/
    #         ├── __init__.py
    #         └── client.py

    (ws / "root_mod.py").write_text("def root_func(): pass\n")

    pkg_a = ws / "pkg_a"
    pkg_a.mkdir()
    (pkg_a / "__init__.py").write_text("from .worker import do_work\n")
    (pkg_a / "worker.py").write_text("def do_work(): pass\n")

    sub = pkg_a / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "helper.py").write_text("def run_helper(): pass\n")

    pkg_b = ws / "pkg_b"
    pkg_b.mkdir()
    (pkg_b / "__init__.py").write_text("")
    (pkg_b / "client.py").write_text("def call_api(): pass\n")

    return ws


def test_resolver_module_mapping(mock_workspace: Path):
    """Resolver discovers and maps modules across directory structures."""
    resolver = WorkspaceImportResolver(mock_workspace)

    assert "root_mod" in resolver.module_to_file
    assert resolver.module_to_file["root_mod"] == "root_mod.py"

    assert "pkg_a" in resolver.module_to_file
    assert resolver.module_to_file["pkg_a"] == "pkg_a/__init__.py"

    assert "pkg_a.worker" in resolver.module_to_file
    assert resolver.module_to_file["pkg_a.worker"] == "pkg_a/worker.py"

    assert "pkg_a.sub.helper" in resolver.module_to_file
    assert resolver.module_to_file["pkg_a.sub.helper"] == "pkg_a/sub/helper.py"


def test_resolve_absolute_import_module(mock_workspace: Path):
    """Resolve plain absolute module import."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="pkg_a.worker", line=1)
    res = resolver.resolve_import("root_mod.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.target_symbol is None
    assert res.local_name == "pkg_a"


def test_resolve_absolute_import_with_alias(mock_workspace: Path):
    """Resolve absolute import with alias (import x as y)."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="pkg_a.worker", alias="wkr", line=1)
    res = resolver.resolve_import("root_mod.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.local_name == "wkr"


def test_resolve_from_import_symbol(mock_workspace: Path):
    """Resolve symbol from a workspace module (from pkg_a.worker import do_work)."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="do_work", from_module="pkg_a.worker", line=1)
    res = resolver.resolve_import("root_mod.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.target_symbol == "do_work"
    assert res.local_name == "do_work"


def test_resolve_from_import_submodule(mock_workspace: Path):
    """Resolve submodule imported via from-statement (from pkg_a import worker)."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="worker", from_module="pkg_a", line=1)
    res = resolver.resolve_import("root_mod.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.target_symbol is None


def test_resolve_from_import_with_alias(mock_workspace: Path):
    """Resolve symbol from-import with alias (from pkg_a.worker import do_work as work)."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="do_work", alias="work", from_module="pkg_a.worker", line=1)
    res = resolver.resolve_import("root_mod.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.target_symbol == "do_work"
    assert res.local_name == "work"


def test_resolve_relative_import_same_dir(mock_workspace: Path):
    """Resolve relative import within the same directory (from .worker import do_work)."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="do_work", from_module="worker", level=1, line=1)
    res = resolver.resolve_import("pkg_a/__init__.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.target_symbol == "do_work"


def test_resolve_relative_import_parent_dir(mock_workspace: Path):
    """Resolve relative import climbing to parent directory (from ..worker import do_work in sub/helper.py)."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="do_work", from_module="worker", level=2, line=1)
    res = resolver.resolve_import("pkg_a/sub/helper.py", imp)

    assert res.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    assert res.target_file == "pkg_a/worker.py"
    assert res.target_symbol == "do_work"


def test_resolve_relative_import_escape_rejection(mock_workspace: Path):
    """Relative import attempting to navigate above repository root is rejected fail-closed."""
    resolver = WorkspaceImportResolver(mock_workspace)

    # In root_mod.py, climbing level=2 attempts to escape mock_workspace
    imp = ImportRecord(name="secret", level=2, line=1)
    res = resolver.resolve_import("root_mod.py", imp)

    assert res.resolution_status == CallResolutionStatus.UNRESOLVED
    assert "escapes workspace root" in res.reason


def test_resolve_external_stdlib(mock_workspace: Path):
    """Standard library modules are identified as external."""
    resolver = WorkspaceImportResolver(mock_workspace)

    for mod in ["os", "sys", "ast", "pathlib", "json", "typing"]:
        imp = ImportRecord(name=mod, line=1)
        res = resolver.resolve_import("root_mod.py", imp)
        assert res.resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL
        assert res.is_external is True


def test_resolve_external_third_party(mock_workspace: Path):
    """Third party modules not in workspace or stdlib are resolved as external."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="pytest", line=1)
    res = resolver.resolve_import("root_mod.py", imp)
    assert res.resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL
    assert res.is_external is True

    imp2 = ImportRecord(name="FastAPI", from_module="fastapi", line=1)
    res2 = resolver.resolve_import("root_mod.py", imp2)
    assert res2.resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL
    assert res2.is_external is True


def test_resolve_unknown_nonexistent(mock_workspace: Path):
    """Nonexistent internal module is reported unresolved."""
    resolver = WorkspaceImportResolver(mock_workspace)

    imp = ImportRecord(name="nonexistent_func", from_module="pkg_a.ghost", line=1)
    res = resolver.resolve_import("root_mod.py", imp)
    assert res.resolution_status == CallResolutionStatus.UNRESOLVED
    assert "not found" in res.reason
