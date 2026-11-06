"""Pydantic schemas and enums for Phase 11 Symbol Graph and Call-Graph Engine."""

from enum import Enum
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class NodeKind(str, Enum):
    """Classification of graph nodes."""
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


class EdgeKind(str, Enum):
    """Classification of graph edges."""
    CALLS = "calls"
    IMPORTS = "imports"


class CallResolutionStatus(str, Enum):
    """Static call resolution status categories."""
    RESOLVED_INTERNAL = "resolved_internal"
    RESOLVED_EXTERNAL = "resolved_external"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    DYNAMIC = "dynamic"


class GraphNode(BaseModel):
    """An individual node in the repository symbol graph."""
    node_id: str = Field(..., description="Stable unique identifier, e.g. 'rel/path.py:Class.method'")
    kind: NodeKind = Field(..., description="Kind of AST node")
    name: str = Field(..., description="Simple identifier name")
    qualified_name: str = Field(..., description="Fully-qualified symbol name")
    file_path: str = Field(..., description="Workspace-relative source path")
    start_line: int = Field(..., ge=1, description="1-indexed starting line")
    end_line: int = Field(..., ge=1, description="1-indexed ending line")
    docstring: Optional[str] = None
    parent_scope: Optional[str] = None
    parameters: List[str] = Field(default_factory=list)
    is_async: bool = False


class CallEdge(BaseModel):
    """An invocation relationship edge between a caller and a call site / callee."""
    source_id: str = Field(..., description="Source symbol node_id (caller)")
    target_id: Optional[str] = Field(None, description="Target symbol node_id if resolved internally")
    source_file: str = Field(..., description="Workspace-relative path of caller")
    line_number: int = Field(..., ge=1, description="1-indexed call-site line")
    col_offset: Optional[int] = Field(None, ge=0, description="Call-site column offset")
    raw_call_expr: str = Field(..., description="Raw or normalized call expression representation")
    resolution_status: CallResolutionStatus = Field(..., description="Resolution category")
    resolution_reason: str = Field(..., description="Deterministic rationale for the resolution")
    target_qualified_name: Optional[str] = Field(None, description="Target qualified name if determined")


class CallGraphSummary(BaseModel):
    """Statistical summary of the call-graph."""
    total_nodes: int = 0
    total_edges: int = 0
    resolved_internal_calls: int = 0
    resolved_external_calls: int = 0
    unresolved_calls: int = 0
    ambiguous_calls: int = 0
    dynamic_calls: int = 0


class ImportRecord(BaseModel):
    """Record of an import statement in a Python file."""
    name: str = Field(..., description="Imported symbol or module name")
    alias: Optional[str] = None
    from_module: Optional[str] = None
    level: int = 0
    line: int = 1


class CallSiteRecord(BaseModel):
    """Raw call-site extraction prior to cross-file resolution."""
    caller_id: str
    callee_expr: str
    raw_expr: str
    line: int
    col_offset: Optional[int] = None
    receiver_kind: str = "name"  # "name", "self", "cls", "module", "chained", "dynamic"
    callee_name: Optional[str] = None
    receiver_name: Optional[str] = None


class ImportEvidence(BaseModel):
    """Evidence for an import statement linking two modules or importing an external dependency."""
    importing_file: str = Field(..., description="Workspace-relative path of the importing file")
    line_number: int = Field(..., ge=1, description="1-indexed line number of the import statement")
    imported_name: str = Field(..., description="Imported symbol or module name")
    alias: Optional[str] = Field(None, description="Local alias if imported with 'as'")
    from_module: Optional[str] = Field(None, description="Base module for from-imports")
    level: int = Field(0, ge=0, description="Relative import level (0 for absolute)")
    import_type: str = Field("direct", description="Category: 'direct', 'from', 'relative', 'star'")
    resolution_status: CallResolutionStatus = Field(CallResolutionStatus.RESOLVED_INTERNAL, description="Resolution category")
    resolution_reason: str = Field("", description="Deterministic rationale for the resolution")


class ModuleNode(BaseModel):
    """Node representing a Python module in the workspace dependency graph."""
    module_id: str = Field(..., description="Canonical dotted module name, e.g. 'app.forge.repository.indexer'")
    file_path: str = Field(..., description="Workspace-relative file path, e.g. 'app/forge/repository/indexer.py'")
    is_package: bool = Field(False, description="True if module is a package initialization file (__init__.py)")
    docstring: Optional[str] = Field(None, description="Module top-level docstring")
    total_lines: int = Field(0, ge=0, description="Total lines in module file")
    internal_dependencies: List[str] = Field(default_factory=list, description="Direct internal module IDs imported by this module")
    internal_dependents: List[str] = Field(default_factory=list, description="Direct internal module IDs that import this module")
    external_dependencies: List[str] = Field(default_factory=list, description="External package names imported by this module")
    in_degree: int = Field(0, ge=0, description="Number of internal modules importing this module")
    out_degree: int = Field(0, ge=0, description="Number of internal modules imported by this module")


class ModuleEdge(BaseModel):
    """Directed dependency relationship edge: source_module depends on target_module."""
    source_module: str = Field(..., description="Canonical module_id of the importing module")
    target_module: str = Field(..., description="Canonical module_id of the imported module")
    source_file: str = Field(..., description="Workspace-relative file path of importing module")
    target_file: Optional[str] = Field(None, description="Workspace-relative file path of imported module")
    evidence: List[ImportEvidence] = Field(default_factory=list, description="Consolidated import statements for this dependency")
    weight: int = Field(1, ge=1, description="Number of import occurrences connecting these modules")
    is_self_dependency: bool = Field(False, description="True if source_module == target_module")


class ExternalImportRecord(BaseModel):
    """Record of an external dependency (standard library or third-party package)."""
    importing_module: str = Field(..., description="Canonical module_id of the importing module")
    importing_file: str = Field(..., description="Workspace-relative file path")
    package_name: str = Field(..., description="Top-level package name, e.g. 'pytest', 'sys'")
    imported_name: str = Field(..., description="Imported symbol or submodule name")
    is_stdlib: bool = Field(False, description="True if package belongs to Python standard library")
    line_number: int = Field(1, ge=1, description="Line number of the import statement")


class UnresolvedImportRecord(BaseModel):
    """Record of an import statement that could not be statically resolved."""
    importing_module: str = Field(..., description="Canonical module_id of the importing module")
    importing_file: str = Field(..., description="Workspace-relative file path")
    imported_name: str = Field(..., description="Imported name")
    from_module: Optional[str] = Field(None, description="From-module clause if present")
    level: int = Field(0, ge=0, description="Relative import level")
    line_number: int = Field(1, ge=1, description="Line number of the import statement")
    reason: str = Field(..., description="Deterministic reason why resolution failed")


class DependencyCycle(BaseModel):
    """A detected cycle of module dependencies."""
    cycle_id: str = Field(..., description="Canonical unique identifier, e.g. 'cycle:mod_a->mod_b->mod_a'")
    modules: List[str] = Field(..., description="Cycle path starting and ending with the canonical root module")
    length: int = Field(..., ge=1, description="Cycle edge length (1 for direct self-dependency)")
    is_self_cycle: bool = Field(False, description="True if cycle is a direct self-dependency (length 1)")
    edges: List[Tuple[str, str]] = Field(default_factory=list, description="Sequence of (source_module, target_module) edges")


class StronglyConnectedComponent(BaseModel):
    """A strongly connected component (SCC) in the module dependency graph."""
    scc_id: str = Field(..., description="Deterministic SCC identifier")
    modules: List[str] = Field(..., description="Lexicographically sorted module IDs in this component")
    size: int = Field(..., ge=1, description="Number of modules in this component")
    is_cyclic: bool = Field(False, description="True if component contains a cycle (size > 1 or self-loop)")


class ModuleDependencyGraphSummary(BaseModel):
    """Statistical summary of the module dependency graph and cycle analysis."""
    total_modules: int = 0
    total_internal_edges: int = 0
    total_external_imports: int = 0
    total_stdlib_imports: int = 0
    total_third_party_imports: int = 0
    total_unresolved_imports: int = 0
    total_ambiguous_imports: int = 0
    total_cycles: int = 0
    total_sccs: int = 0
    cyclic_sccs: int = 0
    modules_in_cycles: int = 0
    isolated_modules: int = 0
