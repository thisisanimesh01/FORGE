"""Pydantic schemas for Phase 11 M6 Repository Intelligence Tools.

Strict input and output schemas for LLM-accessible tools:
- Repository overview & indexing status
- Symbol exploration & ambiguous symbol resolution
- Cross-file call-graph exploration
- Module dependency exploration & cycle detection
- Change-impact analysis
- Bounded source context retrieval
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class CallDirection(str, Enum):
    """Direction for call graph exploration."""
    CALLERS = "callers"
    CALLEES = "callees"
    BOTH = "both"


class SymbolTypeFilter(str, Enum):
    """Supported symbol types for filtering."""
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    VARIABLE = "variable"
    IMPORT = "import"


# ---------------------------------------------------------------------------
# Tool A: Repository Overview
# ---------------------------------------------------------------------------

class RepositoryOverviewInput(BaseModel):
    """Input parameters for get_repository_overview tool."""
    model_config = ConfigDict(extra="forbid")

    include_cache_stats: bool = Field(
        default=True,
        description="Whether to include indexing cache statistics and storage metrics.",
    )
    check_freshness: bool = Field(
        default=True,
        description="Whether to perform a quick content-hash check to determine index freshness.",
    )


class CacheFreshnessInfo(BaseModel):
    """Indexing cache freshness metadata."""
    is_fresh: bool = Field(description="True if the cache matches current workspace state.")
    last_indexed_at: Optional[float] = Field(default=None, description="Timestamp of last index update.")
    cache_entries_count: int = Field(default=0, description="Number of cached file entries.")
    cache_disk_bytes: int = Field(default=0, description="Disk space used by cache in bytes.")
    cache_hits: int = Field(default=0, description="Total cache hits recorded.")
    cache_misses: int = Field(default=0, description="Total cache misses recorded.")
    rebuild_fallback_triggered: bool = Field(default=False, description="Whether fallback rebuild was triggered.")


class RepositoryOverviewOutput(BaseModel):
    """Structured output for get_repository_overview tool."""
    repository_root: str = Field(description="Repository root directory name.")
    total_files_scanned: int = Field(description="Total Python files tracked.")
    total_symbols: int = Field(description="Total indexed symbols.")
    total_classes: int = Field(description="Total classes discovered.")
    total_functions: int = Field(description="Total functions discovered.")
    total_methods: int = Field(description="Total methods discovered.")
    total_call_nodes: int = Field(description="Total nodes in call graph.")
    total_call_edges: int = Field(description="Total resolved and unresolved call edges.")
    total_modules: int = Field(description="Total modules in dependency graph.")
    total_module_edges: int = Field(description="Total module dependency edges.")
    total_circular_cycles: int = Field(description="Count of detected circular dependency cycles.")
    frameworks_detected: List[str] = Field(default_factory=list, description="Detected frameworks.")
    cache_freshness: Optional[CacheFreshnessInfo] = Field(default=None, description="Cache status metadata.")


# ---------------------------------------------------------------------------
# Tool B: Symbol Exploration
# ---------------------------------------------------------------------------

class SymbolSearchInput(BaseModel):
    """Input parameters for search_symbols tool."""
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        description="Symbol name or substring to search for (e.g. 'parse', 'User', 'authenticate').",
        min_length=1,
        max_length=200,
    )
    symbol_type: Optional[SymbolTypeFilter] = Field(
        default=None,
        description="Optional filter by symbol type: class, function, method, variable, import.",
    )
    exact_match: bool = Field(
        default=False,
        description="If True, match exact symbol name instead of substring matching.",
    )
    max_results: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum number of symbol matches to return (capped at 100).",
    )


class SymbolMatch(BaseModel):
    """Individual symbol match description."""
    symbol_id: str = Field(description="Canonical symbol ID, e.g. 'module.submodule.SymbolName'.")
    name: str = Field(description="Simple symbol name.")
    symbol_type: str = Field(description="Symbol type: function, class, method, etc.")
    file_path: str = Field(description="Repository-relative file path where defined.")
    start_line: int = Field(description="Starting line number (1-indexed).")
    end_line: int = Field(description="Ending line number (inclusive).")
    parent_name: Optional[str] = Field(default=None, description="Parent class or function if nested.")
    docstring_preview: Optional[str] = Field(default=None, description="First line of docstring if present.")


class SymbolSearchOutput(BaseModel):
    """Structured output for search_symbols tool."""
    query: str = Field(description="Search query string.")
    matches_count: int = Field(description="Number of matches returned.")
    total_matches_found: int = Field(description="Total matches found before capping.")
    is_ambiguous: bool = Field(
        description="True if multiple symbols match the same simple name across different files/modules.",
    )
    ambiguous_candidate_ids: List[str] = Field(
        default_factory=list,
        description="List of distinct canonical symbol IDs when ambiguous.",
    )
    symbols: List[SymbolMatch] = Field(default_factory=list, description="Matched symbols.")


class SymbolDetailsInput(BaseModel):
    """Input parameters for get_symbol_details tool."""
    model_config = ConfigDict(extra="forbid")

    symbol_id: str = Field(
        description="Canonical symbol ID (e.g. 'forge.repository.indexer.SymbolIndex') or simple name.",
        min_length=1,
        max_length=250,
    )
    file_path: Optional[str] = Field(
        default=None,
        description="Optional file path to disambiguate if multiple symbols share the same name.",
    )


class SymbolRelationship(BaseModel):
    """Relationship from this symbol to other symbols or modules."""
    relationship_type: str = Field(description="caller, callee, base_class, subclass, or container.")
    target_id: str = Field(description="Target symbol or module identifier.")
    file_path: Optional[str] = Field(default=None, description="Target file path.")
    line_number: Optional[int] = Field(default=None, description="Relevant source line number.")


class SymbolDetailsOutput(BaseModel):
    """Structured output for get_symbol_details tool."""
    symbol_id: str = Field(description="Canonical symbol identifier.")
    name: str = Field(description="Simple symbol name.")
    symbol_type: str = Field(description="Symbol type: class, function, method, etc.")
    file_path: str = Field(description="Repository-relative file path.")
    start_line: int = Field(description="Starting line number.")
    end_line: int = Field(description="Ending line number.")
    parent_name: Optional[str] = Field(default=None, description="Containing class or scope.")
    parameters: List[str] = Field(default_factory=list, description="Parameter list if callable.")
    docstring: Optional[str] = Field(default=None, description="Full docstring if present.")
    relationships: List[SymbolRelationship] = Field(
        default_factory=list,
        description="Known structural relationships (callers, callees, inheritance).",
    )
    is_ambiguous: bool = Field(default=False, description="True if query matched multiple symbols.")
    disambiguation_options: List[str] = Field(
        default_factory=list,
        description="Alternative canonical IDs if ambiguous.",
    )


# ---------------------------------------------------------------------------
# Tool C: Call-Graph Exploration
# ---------------------------------------------------------------------------

class CallGraphExploreInput(BaseModel):
    """Input parameters for explore_call_graph tool."""
    model_config = ConfigDict(extra="forbid")

    symbol_id: str = Field(
        description="Target symbol identifier (e.g. 'forge.repository.indexer.build_symbol_index').",
        min_length=1,
        max_length=250,
    )
    direction: CallDirection = Field(
        default=CallDirection.BOTH,
        description="Exploration direction: 'callers' (who calls this), 'callees' (who this calls), or 'both'.",
    )
    max_depth: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Maximum traversal depth (default: 1, max: 5).",
    )
    max_results: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum call relations to return (default: 30, max: 100).",
    )


class CallEdgeInfo(BaseModel):
    """Structured information on an individual call graph edge."""
    caller_id: str = Field(description="Caller symbol ID.")
    callee_id: str = Field(description="Callee symbol ID or expression.")
    caller_file: str = Field(description="File containing the call site.")
    line_number: int = Field(description="Line number of call invocation.")
    resolution_type: str = Field(
        description="internal (resolved in repo), unresolved (not found), ambiguous, or dynamic.",
    )
    resolution_reason: Optional[str] = Field(default=None, description="Diagnostic explanation for resolution.")
    depth: int = Field(default=1, description="Traversal depth from target symbol.")


class CallGraphExploreOutput(BaseModel):
    """Structured output for explore_call_graph tool."""
    target_symbol_id: str = Field(description="Queried symbol ID.")
    target_file: Optional[str] = Field(default=None, description="Target symbol definition file.")
    direction: str = Field(description="Exploration direction used.")
    callers_count: int = Field(description="Number of callers returned.")
    callees_count: int = Field(description="Number of callees returned.")
    callers: List[CallEdgeInfo] = Field(default_factory=list, description="Incoming call edges.")
    callees: List[CallEdgeInfo] = Field(default_factory=list, description="Outgoing call edges.")
    unresolved_calls_count: int = Field(default=0, description="Count of unresolved/dynamic calls.")
    is_truncated: bool = Field(default=False, description="True if results were capped by max_results limit.")


# ---------------------------------------------------------------------------
# Tool D: Module Dependency Exploration
# ---------------------------------------------------------------------------

class ModuleDependencyInput(BaseModel):
    """Input parameters for explore_module_dependencies tool."""
    model_config = ConfigDict(extra="forbid")

    module_name: Optional[str] = Field(
        default=None,
        description="Specific module to inspect (e.g. 'forge.repository.indexer'). If omitted, returns package overview and cycles.",
    )
    include_cycles: bool = Field(
        default=True,
        description="Whether to include circular dependency cycles detected in the workspace.",
    )
    transitive: bool = Field(
        default=False,
        description="Whether to compute all transitive reverse dependents of the specified module.",
    )
    max_depth: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum depth for transitive dependency search (default: 3, max: 10).",
    )
    max_results: int = Field(
        default=40,
        ge=1,
        le=100,
        description="Maximum dependency entries to return.",
    )


class ModuleCycleInfo(BaseModel):
    """Information about a circular dependency cycle."""
    cycle_id: str = Field(description="Stable identifier for the cycle.")
    length: int = Field(description="Number of modules in the cycle.")
    cycle_path: List[str] = Field(description="Ordered list of module names forming the cycle.")


class ModuleDependencyOutput(BaseModel):
    """Structured output for explore_module_dependencies tool."""
    target_module: Optional[str] = Field(default=None, description="Inspected module name.")
    target_file: Optional[str] = Field(default=None, description="Module file path if known.")
    direct_dependencies: List[str] = Field(
        default_factory=list,
        description="Modules imported by this module.",
    )
    direct_dependents: List[str] = Field(
        default_factory=list,
        description="Modules that directly import this module.",
    )
    transitive_dependents: List[str] = Field(
        default_factory=list,
        description="All downstream modules transitively dependent on this module.",
    )
    circular_cycles: List[ModuleCycleInfo] = Field(
        default_factory=list,
        description="Detected circular dependency cycles in repository.",
    )
    total_workspace_modules: int = Field(description="Total modules in repository dependency graph.")
    total_workspace_edges: int = Field(description="Total import edges across repository.")


# ---------------------------------------------------------------------------
# Tool E: Change-Impact Analysis
# ---------------------------------------------------------------------------

class ChangeImpactInput(BaseModel):
    """Input parameters for analyze_change_impact tool."""
    model_config = ConfigDict(extra="forbid")

    modified_files: List[str] = Field(
        default_factory=list,
        description="List of repository-relative file paths being added or modified.",
    )
    modified_symbols: List[str] = Field(
        default_factory=list,
        description="Optional list of specific symbol identifiers modified.",
    )
    diff: Optional[str] = Field(
        default=None,
        description="Optional unified diff text representing the proposed changes.",
    )
    max_transitive_depth: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum depth for transitive impact propagation.",
    )
    max_results: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum impacted entities to return.",
    )


class ImpactedEntityInfo(BaseModel):
    """Summary of an entity impacted by code changes."""
    entity_id: str = Field(description="Symbol or module identifier.")
    entity_type: str = Field(description="symbol or module.")
    file_path: str = Field(description="Repository-relative file path.")
    impact_type: str = Field(description="direct, transitive, caller, callee, import_dependency.")
    confidence: str = Field(description="deterministic heuristic confidence rating (definite, potential, unresolved).")
    heuristic_score: float = Field(description="Deterministic propagation weight (0.0 to 1.0).")
    hop_distance: int = Field(description="Distance in graph from nearest modified entity.")
    impact_rationale: str = Field(description="Explanation of how this entity is impacted.")


class ImpactPathInfo(BaseModel):
    """Trace of how impact propagated from change to entity."""
    target_id: str = Field(description="Impacted target identifier.")
    path: List[str] = Field(description="Sequence of graph nodes from origin to target.")
    path_type: str = Field(description="call_chain, import_chain, or hybrid.")


class ChangeImpactOutput(BaseModel):
    """Structured output for analyze_change_impact tool."""
    total_impacted_entities: int = Field(description="Total distinct entities impacted.")
    direct_impact_count: int = Field(description="Count of directly modified or direct caller entities.")
    transitive_impact_count: int = Field(description="Count of downstream transitively impacted entities.")
    impacted_files: List[str] = Field(default_factory=list, description="Unique files containing impacted entities.")
    impacted_entities: List[ImpactedEntityInfo] = Field(default_factory=list, description="Top impacted entities.")
    sample_impact_paths: List[ImpactPathInfo] = Field(default_factory=list, description="Representative propagation paths.")
    is_truncated: bool = Field(default=False, description="True if output was capped by max_results limit.")


# ---------------------------------------------------------------------------
# Tool F: Bounded Source Context Retrieval
# ---------------------------------------------------------------------------

class SourceContextInput(BaseModel):
    """Input parameters for get_source_context tool."""
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(
        description="Repository-relative path to source file (e.g. 'app/forge/cli.py').",
        min_length=1,
        max_length=300,
    )
    start_line: int = Field(
        default=1,
        ge=1,
        description="Starting line number (1-indexed).",
    )
    end_line: int = Field(
        default=100,
        ge=1,
        description="Ending line number (inclusive).",
    )
    include_symbols: bool = Field(
        default=True,
        description="Whether to include symbol definitions located within the requested line range.",
    )


class SourceSymbolMetadata(BaseModel):
    """Metadata of a symbol defined in requested source range."""
    name: str = Field(description="Symbol name.")
    symbol_type: str = Field(description="function, class, method, etc.")
    start_line: int = Field(description="Definition start line.")
    end_line: int = Field(description="Definition end line.")


class SourceContextOutput(BaseModel):
    """Structured output for get_source_context tool."""
    file_path: str = Field(description="Repository-relative file path.")
    start_line: int = Field(description="Starting line number of excerpt.")
    end_line: int = Field(description="Ending line number of excerpt.")
    total_file_lines: int = Field(description="Total lines in file.")
    content_sha256: str = Field(description="SHA-256 hash of raw extracted excerpt.")
    source_content: str = Field(description="Line-numbered and secret-redacted source excerpt.")
    symbols_in_range: List[SourceSymbolMetadata] = Field(
        default_factory=list,
        description="Symbols defined within this range.",
    )
    was_truncated: bool = Field(
        default=False,
        description="True if requested line span exceeded maximum allowable window.",
    )


class SourceExcerpt(BaseModel):
    """Scoped source code excerpt for an affected or relevant file."""
    model_config = ConfigDict(frozen=True)

    file_path: str = Field(description="Normalized relative path to source file")
    start_line: int = Field(ge=1, description="1-based starting line number")
    end_line: int = Field(ge=1, description="1-based ending line number")
    total_lines: int = Field(ge=0, description="Total lines in excerpt")
    content: str = Field(description="Sanitized source text with line numbers")
    content_sha256: str = Field(description="SHA256 hex digest of raw excerpt content")
    was_truncated: bool = Field(default=False, description="Whether excerpt was truncated to budget")
