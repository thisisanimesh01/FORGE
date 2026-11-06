"""Comprehensive unit tests for Symbol Graph and Call-Graph Engine in Phase 11 Milestone 2."""

from pathlib import Path
import pytest

from forge.repository.call_graph import build_call_graph, CallGraph
from forge.repository.graph_schemas import (
    CallResolutionStatus,
    GraphNode,
    NodeKind,
)
from forge.repository.indexer import parse_python_file_details


@pytest.fixture
def mock_call_graph_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample_service"
    repo.mkdir()

    # 1. Base models and interfaces: models/base.py
    models = repo / "models"
    models.mkdir()
    (models / "__init__.py").write_text("")
    (models / "base.py").write_text(
        'class BaseEntity:\n'
        '    def get_id(self) -> str:\n'
        '        return "base_id"\n\n'
        '    @classmethod\n'
        '    def get_type_name(cls) -> str:\n'
        '        return "BaseEntity"\n'
    )

    # 2. Service implementation: services/user_service.py
    services = repo / "services"
    services.mkdir()
    (services / "__init__.py").write_text("")
    (services / "user_service.py").write_text(
        'from models.base import BaseEntity\n\n'
        'class User(BaseEntity):\n'
        '    def __init__(self, name: str):\n'
        '        self.name = name\n\n'
        '    def identify(self) -> str:\n'
        '        # Call inherited method from BaseEntity\n'
        '        return self.get_id()\n\n'
        '    def describe(self) -> str:\n'
        '        # Call self method in same class\n'
        '        return self.identify()\n\n'
        '    @classmethod\n'
        '    def entity_type(cls) -> str:\n'
        '        # Call inherited classmethod\n'
        '        return cls.get_type_name()\n\n'
        'def create_user(name: str) -> User:\n'
        '    u = User(name)\n'
        '    return u\n'
    )

    # 3. Algorithms & recursion: utils/math_ops.py
    utils = repo / "utils"
    utils.mkdir()
    (utils / "__init__.py").write_text("")
    (utils / "math_ops.py").write_text(
        'def factorial(n: int) -> int:\n'
        '    if n <= 1:\n'
        '        return 1\n'
        '    return n * factorial(n - 1)\n\n'
        'def ping(n: int) -> int:\n'
        '    if n <= 0:\n'
        '        return 0\n'
        '    return pong(n - 1)\n\n'
        'def pong(n: int) -> int:\n'
        '    if n <= 0:\n'
        '        return 0\n'
        '    return ping(n - 1)\n\n'
        'def complex_pipeline(x: int) -> int:\n'
        '    # Nested function\n'
        '    def inner_step(val: int) -> int:\n'
        '        return val * 2\n'
        '    return inner_step(x)\n'
    )

    # 4. Caller app: app.py
    (repo / "app.py").write_text(
        'import os\n'
        'from services.user_service import create_user, User\n'
        'from utils.math_ops import factorial as fact\n\n'
        'async def run_pipeline():\n'
        '    # Direct call to imported function\n'
        '    user = create_user("alice")\n'
        '    # Call method on class\n'
        '    desc = user.describe()\n'
        '    # Call aliased function twice (testing multiple call-sites)\n'
        '    f1 = fact(5)\n'
        '    f2 = fact(10)\n'
        '    # Call built-in\n'
        '    print("done")\n'
        '    # Dynamic call\n'
        '    getattr(user, "identify")()\n'
        '    return f1 + f2\n'
    )

    return repo


def test_qualified_name_and_node_id_extraction(mock_call_graph_repo: Path):
    """AST parser extracts stable qualified names and node IDs."""
    cg = build_call_graph(mock_call_graph_repo)

    # Check BaseEntity and User
    assert "models/base.py:BaseEntity" in cg.nodes
    base_node = cg.nodes["models/base.py:BaseEntity"]
    assert base_node.kind == NodeKind.CLASS
    assert base_node.name == "BaseEntity"
    assert "models.base.BaseEntity" in base_node.qualified_name

    assert "models/base.py:BaseEntity.get_id" in cg.nodes
    method_node = cg.nodes["models/base.py:BaseEntity.get_id"]
    assert method_node.kind == NodeKind.METHOD
    assert method_node.parent_scope == "BaseEntity"
    assert "models.base.BaseEntity.get_id" in method_node.qualified_name

    # Check nested function
    assert "utils/math_ops.py:complex_pipeline.<locals>.inner_step" in cg.nodes
    nested_node = cg.nodes["utils/math_ops.py:complex_pipeline.<locals>.inner_step"]
    assert nested_node.kind == NodeKind.FUNCTION
    assert nested_node.name == "inner_step"


def test_direct_calls_and_aliasing(mock_call_graph_repo: Path):
    """Direct calls and aliased imports resolve correctly across files."""
    cg = build_call_graph(mock_call_graph_repo)

    # run_pipeline calls create_user
    callees = cg.get_direct_callees("app.py:run_pipeline")
    assert "services/user_service.py:create_user" in callees

    # run_pipeline calls aliased fact -> factorial
    assert "utils/math_ops.py:factorial" in callees


def test_multiple_call_sites_preserved(mock_call_graph_repo: Path):
    """Multiple invocations of the same callee are preserved as distinct edges with accurate line numbers."""
    cg = build_call_graph(mock_call_graph_repo)

    edges = cg.get_call_edges(
        source_id="app.py:run_pipeline",
        target_id="utils/math_ops.py:factorial",
    )

    # fact was called twice: fact(5) and fact(10)
    assert len(edges) == 2
    assert edges[0].line_number != edges[1].line_number
    assert "fact(5)" in edges[0].raw_call_expr or "fact" in edges[0].raw_call_expr
    assert "fact(10)" in edges[1].raw_call_expr or "fact" in edges[1].raw_call_expr


def test_self_method_resolution_same_class(mock_call_graph_repo: Path):
    """self.method() calls within the same class resolve to internal method node."""
    cg = build_call_graph(mock_call_graph_repo)

    callees = cg.get_direct_callees("services/user_service.py:User.describe")
    assert "services/user_service.py:User.identify" in callees


def test_self_method_resolution_inherited(mock_call_graph_repo: Path):
    """self.method() calling an inherited method resolves to the base class method across files."""
    cg = build_call_graph(mock_call_graph_repo)

    callees = cg.get_direct_callees("services/user_service.py:User.identify")
    assert "models/base.py:BaseEntity.get_id" in callees


def test_cls_method_resolution_inherited(mock_call_graph_repo: Path):
    """cls.method() calling an inherited classmethod resolves across files."""
    cg = build_call_graph(mock_call_graph_repo)

    callees = cg.get_direct_callees("services/user_service.py:User.entity_type")
    assert "models/base.py:BaseEntity.get_type_name" in callees


def test_recursive_function(mock_call_graph_repo: Path):
    """Recursive function has self-referential call edge."""
    cg = build_call_graph(mock_call_graph_repo)

    callees = cg.get_direct_callees("utils/math_ops.py:factorial")
    assert "utils/math_ops.py:factorial" in callees

    callers = cg.get_direct_callers("utils/math_ops.py:factorial")
    assert "utils/math_ops.py:factorial" in callers
    assert "app.py:run_pipeline" in callers


def test_mutually_recursive_functions(mock_call_graph_repo: Path):
    """Mutually recursive functions ping and pong form a cycle safely."""
    cg = build_call_graph(mock_call_graph_repo)

    assert "utils/math_ops.py:pong" in cg.get_direct_callees("utils/math_ops.py:ping")
    assert "utils/math_ops.py:ping" in cg.get_direct_callees("utils/math_ops.py:pong")

    # Transitive traversal does not infinite-loop
    callees_ping = cg.get_callees("utils/math_ops.py:ping", max_depth=5)
    assert "utils/math_ops.py:pong" in callees_ping
    assert len(callees_ping) == 1  # Only pong is callee (visited set prevents duplicate ping)


def test_nested_function_resolution(mock_call_graph_repo: Path):
    """Nested functions in local scope are resolved internally."""
    cg = build_call_graph(mock_call_graph_repo)

    callees = cg.get_direct_callees("utils/math_ops.py:complex_pipeline")
    assert "utils/math_ops.py:complex_pipeline.<locals>.inner_step" in callees


def test_dynamic_call_handling(mock_call_graph_repo: Path):
    """Dynamic calls via getattr are marked as DYNAMIC without guessing."""
    cg = build_call_graph(mock_call_graph_repo)

    dyn_edges = [
        e for e in cg.edges
        if e.source_id == "app.py:run_pipeline" and e.resolution_status == CallResolutionStatus.DYNAMIC
    ]
    assert len(dyn_edges) >= 1
    getattr_edge = next((e for e in dyn_edges if "getattr" in e.raw_call_expr), None)
    assert getattr_edge is not None
    assert getattr_edge.target_id is None
    assert "dynamic" in getattr_edge.resolution_reason.lower()


def test_external_stdlib_call_handling(mock_call_graph_repo: Path):
    """Calls to Python built-in functions are marked RESOLVED_EXTERNAL."""
    cg = build_call_graph(mock_call_graph_repo)

    print_edges = [
        e for e in cg.edges
        if e.source_id == "app.py:run_pipeline" and "print" in e.raw_call_expr
    ]
    assert len(print_edges) >= 1
    assert print_edges[0].resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL


def test_bounded_traversal_api(mock_call_graph_repo: Path):
    """Bounded BFS traversal honors max_depth and max_nodes."""
    cg = build_call_graph(mock_call_graph_repo)

    # Depth 1 from run_pipeline
    depth1 = cg.get_callees("app.py:run_pipeline", max_depth=1)
    # Depth 2 from run_pipeline includes transitive callees
    depth2 = cg.get_callees("app.py:run_pipeline", max_depth=2)

    assert len(depth2) >= len(depth1)

    # Node count budget
    max1 = cg.get_callees("app.py:run_pipeline", max_depth=5, max_nodes=1)
    assert len(max1) <= 1


def test_unknown_symbol_traversal_safety(mock_call_graph_repo: Path):
    """Querying unknown symbol IDs returns empty list deterministically without raising."""
    cg = build_call_graph(mock_call_graph_repo)

    assert cg.get_callees("nonexistent:symbol") == []
    assert cg.get_callers("nonexistent:symbol") == []
    assert cg.get_direct_callees("nonexistent:symbol") == []
    assert cg.get_direct_callers("nonexistent:symbol") == []


def test_star_import_ambiguity(tmp_path: Path):
    """When a symbol is defined in multiple star imports, it is marked AMBIGUOUS."""
    ws = tmp_path / "star_ws"
    ws.mkdir()

    (ws / "mod_a.py").write_text("def duplicate_symbol(): pass\n")
    (ws / "mod_b.py").write_text("def duplicate_symbol(): pass\n")
    (ws / "caller.py").write_text(
        "from mod_a import *\n"
        "from mod_b import *\n\n"
        "def main():\n"
        "    duplicate_symbol()\n"
    )

    cg = build_call_graph(ws)

    ambig_edges = [
        e for e in cg.edges
        if e.source_id == "caller.py:main" and e.resolution_status == CallResolutionStatus.AMBIGUOUS
    ]
    assert len(ambig_edges) == 1
    assert "Ambiguous" in ambig_edges[0].resolution_reason


def test_star_import_single_match(tmp_path: Path):
    """When a symbol is uniquely found in a single star import, it is resolved internally."""
    ws = tmp_path / "star_ws2"
    ws.mkdir()

    (ws / "mod_a.py").write_text("def unique_symbol(): pass\n")
    (ws / "caller.py").write_text(
        "from mod_a import *\n\n"
        "def main():\n"
        "    unique_symbol()\n"
    )

    cg = build_call_graph(ws)

    resolved_edges = [
        e for e in cg.edges
        if e.source_id == "caller.py:main" and e.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL
    ]
    assert len(resolved_edges) == 1
    assert resolved_edges[0].target_id == "mod_a.py:unique_symbol"


def test_syntax_error_resilience(tmp_path: Path):
    """Syntax errors in one file do not crash the call-graph build for other files."""
    ws = tmp_path / "syntax_ws"
    ws.mkdir()

    (ws / "broken.py").write_text("def unclosed_syntax(\n")
    (ws / "clean.py").write_text("def working_func(): pass\n")

    cg = build_call_graph(ws)
    assert "clean.py:working_func" in cg.nodes
    assert "broken.py:unclosed_syntax" not in cg.nodes


def test_protected_paths_excluded_from_graph(tmp_path: Path):
    """Protected files (.env, .git) are excluded from the call graph."""
    ws = tmp_path / "prot_ws"
    ws.mkdir()

    (ws / ".env").write_text("SECRET=123\n")
    (ws / ".env.local.py").write_text("def leak(): pass\n")
    (ws / "normal.py").write_text("def normal(): pass\n")

    cg = build_call_graph(ws)
    assert "normal.py:normal" in cg.nodes
    for node_id in cg.nodes:
        assert ".env" not in node_id


def test_transitive_multi_level_inheritance(tmp_path: Path):
    """Method invocation via multi-level inheritance chain (Child -> Parent -> GrandParent)."""
    ws = tmp_path / "inherit_ws"
    ws.mkdir()

    (ws / "classes.py").write_text(
        "class GrandParent:\n"
        "    def ancestral_method(self) -> str:\n"
        "        return 'ancient'\n\n"
        "class Parent(GrandParent):\n"
        "    def parent_method(self) -> str:\n"
        "        return 'parental'\n\n"
        "class Child(Parent):\n"
        "    def speak(self) -> str:\n"
        "        return self.ancestral_method()\n"
    )

    cg = build_call_graph(ws)
    callees = cg.get_direct_callees("classes.py:Child.speak")
    assert "classes.py:GrandParent.ancestral_method" in callees


def test_multiple_inheritance_resolution(tmp_path: Path):
    """Method invocation on class inheriting from multiple base classes."""
    ws = tmp_path / "multi_inherit_ws"
    ws.mkdir()

    (ws / "classes.py").write_text(
        "class MixinA:\n"
        "    def action_a(self): pass\n\n"
        "class MixinB:\n"
        "    def action_b(self): pass\n\n"
        "class Combined(MixinA, MixinB):\n"
        "    def run_both(self):\n"
        "        self.action_a()\n"
        "        self.action_b()\n"
    )

    cg = build_call_graph(ws)
    callees = cg.get_direct_callees("classes.py:Combined.run_both")
    assert "classes.py:MixinA.action_a" in callees
    assert "classes.py:MixinB.action_b" in callees


def test_local_parameter_shadowing_prevents_false_positive(tmp_path: Path):
    """A parameter shadowing a global function name does not falsely resolve to the global function."""
    ws = tmp_path / "shadow_ws"
    ws.mkdir()

    (ws / "shadow.py").write_text(
        "def helper():\n"
        "    return 'global'\n\n"
        "def process(helper):\n"
        "    # helper here is a parameter, NOT the global function\n"
        "    helper()\n"
    )

    cg = build_call_graph(ws)
    callees = cg.get_direct_callees("shadow.py:process")
    assert "shadow.py:helper" not in callees

    edges = cg.get_call_edges(source_id="shadow.py:process")
    assert len(edges) == 1
    assert edges[0].resolution_status == CallResolutionStatus.DYNAMIC
    assert "shadowed by a local parameter or variable" in edges[0].resolution_reason


def test_local_variable_shadowing_prevents_false_positive(tmp_path: Path):
    """A locally assigned variable shadowing a global function does not falsely resolve to the global function."""
    ws = tmp_path / "var_shadow_ws"
    ws.mkdir()

    (ws / "shadow.py").write_text(
        "def compute():\n"
        "    return 42\n\n"
        "def execute():\n"
        "    compute = lambda: 100\n"
        "    compute()\n"
    )

    cg = build_call_graph(ws)
    callees = cg.get_direct_callees("shadow.py:execute")
    assert "shadow.py:compute" not in callees

    edges = cg.get_call_edges(source_id="shadow.py:execute")
    assert len(edges) == 1
    assert edges[0].resolution_status == CallResolutionStatus.DYNAMIC
    assert "shadowed by a local parameter or variable" in edges[0].resolution_reason


def test_receiver_parameter_shadowing_prevents_false_positive(tmp_path: Path):
    """A receiver parameter shadowing an imported module name does not falsely resolve to the module."""
    ws = tmp_path / "recv_shadow_ws"
    ws.mkdir()

    (ws / "caller.py").write_text(
        "import os\n\n"
        "def handle_system(os):\n"
        "    # os is a parameter shadowing the imported module 'os'\n"
        "    os.run_cmd()\n"
    )

    cg = build_call_graph(ws)
    edges = cg.get_call_edges(source_id="caller.py:handle_system")
    assert len(edges) == 1
    assert edges[0].resolution_status == CallResolutionStatus.UNRESOLVED
    assert "unknown type" in edges[0].resolution_reason


def test_deterministic_indexing_repeatability(mock_call_graph_repo: Path):
    """Building the call graph multiple times yields identical deterministic nodes and edges."""
    cg1 = build_call_graph(mock_call_graph_repo)
    cg2 = build_call_graph(mock_call_graph_repo)

    assert list(cg1.nodes.keys()) == list(cg2.nodes.keys())
    assert cg1.forward_adjacency == cg2.forward_adjacency
    assert cg1.reverse_adjacency == cg2.reverse_adjacency
    assert len(cg1.edges) == len(cg2.edges)
    for e1, e2 in zip(cg1.edges, cg2.edges):
        assert e1.source_id == e2.source_id
        assert e1.target_id == e2.target_id
        assert e1.line_number == e2.line_number
        assert e1.resolution_status == e2.resolution_status
