"""Pydantic schemas and enums for Phase 11 Symbol Graph and Call-Graph Engine."""

from enum import Enum
from typing import Dict, List, Optional
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
