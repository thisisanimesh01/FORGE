"""Pydantic schemas and enums for Phase 11 Milestone 4: Change-Impact Analysis Engine."""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ChangeType(str, Enum):
    """Classification of change action."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class EntityType(str, Enum):
    """Type of entity undergoing change or experiencing impact."""
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    UNKNOWN = "unknown"


class ImpactClassification(str, Enum):
    """Categorization of change-impact relationship."""
    DIRECT = "direct"          # Direct caller of changed symbol or direct importer of changed module
    TRANSITIVE = "transitive"  # Multi-hop caller or indirect module dependency
    POTENTIAL = "potential"    # Plausible impact (dynamic call sites, star-imports, ambiguous references)
    UNRESOLVED = "unresolved"  # Reference cannot be statically established or target was deleted/renamed
    UNAFFECTED = "unaffected"  # Verified to be outside analyzed dependency scope


class FileChange(BaseModel):
    """Record of a changed file."""
    path: str = Field(..., description="Workspace-relative path of the changed file")
    change_type: ChangeType = Field(..., description="Nature of file change")
    old_path: Optional[str] = Field(None, description="Previous workspace-relative path if renamed")
    changed_lines: List[int] = Field(default_factory=list, description="1-indexed line numbers modified or added")
    deleted_lines: List[int] = Field(default_factory=list, description="1-indexed line numbers deleted")


class SymbolChange(BaseModel):
    """Record of a changed symbol (function, method, class, variable)."""
    symbol_id: str = Field(..., description="Symbol node_id (e.g. 'pkg/mod.py:ClassName.method')")
    name: str = Field(..., description="Simple identifier name")
    qualified_name: str = Field(..., description="Fully qualified dotted symbol name")
    file_path: str = Field(..., description="Workspace-relative file path")
    entity_type: EntityType = Field(..., description="Classification of the symbol")
    change_type: ChangeType = Field(..., description="Nature of symbol change")
    old_symbol_id: Optional[str] = Field(None, description="Previous symbol_id if renamed")
    start_line: Optional[int] = Field(None, ge=1, description="1-indexed starting line")
    end_line: Optional[int] = Field(None, ge=1, description="1-indexed ending line")
    details: Dict[str, Any] = Field(default_factory=dict, description="Metadata such as parameter diffs or signature shifts")


class ChangeSet(BaseModel):
    """Aggregated collection of file and symbol changes forming an analysis changeset."""
    files: List[FileChange] = Field(default_factory=list, description="List of changed files")
    symbols: List[SymbolChange] = Field(default_factory=list, description="List of changed symbols")
    description: Optional[str] = Field(None, description="Optional description of changeset or PR title")
    base_revision: Optional[str] = Field(None, description="Base Git commit or revision identifier")
    head_revision: Optional[str] = Field(None, description="Head Git commit or revision identifier")


class ImpactPathStep(BaseModel):
    """Single step along an impact propagation path."""
    from_entity: str = Field(..., description="Source entity ID in step")
    to_entity: str = Field(..., description="Target entity ID in step")
    relationship: str = Field(..., description="Relationship connecting entities: 'calls', 'imports', 'member_of', 'enclosed_by'")
    file_path: Optional[str] = Field(None, description="Source file associated with this step")
    line_number: Optional[int] = Field(None, description="Call-site or import line number")


class ImpactedEntity(BaseModel):
    """Representation of an entity affected by a changeset."""
    entity_id: str = Field(..., description="Unique identifier of impacted entity (node_id, module_id, or file_path)")
    entity_type: EntityType = Field(..., description="Kind of entity: function, method, class, module, file")
    file_path: str = Field(..., description="Workspace-relative path of the impacted entity")
    line_number: Optional[int] = Field(None, description="Location of affected symbol or call site")
    classification: ImpactClassification = Field(..., description="DIRECT, TRANSITIVE, POTENTIAL, UNRESOLVED, UNAFFECTED")
    depth: int = Field(1, ge=0, description="Propagation hop distance from original change (0 for self, 1 for direct, 2+ for transitive)")
    impact_path: List[str] = Field(default_factory=list, description="Human-readable chain, e.g. ['mod_a:func', 'mod_b:caller']")
    path_steps: List[ImpactPathStep] = Field(default_factory=list, description="Structured step records along impact path")
    trigger_changes: List[str] = Field(default_factory=list, description="IDs of changes that triggered this impact")
    relationship_type: str = Field(..., description="Primary relationship type, e.g. 'direct_caller', 'transitive_caller', 'module_dependent'")
    confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Deterministic heuristic confidence score (ranking weight, not calibrated probability; 1.0 for confirmed static edges, lower for potential/dynamic)",
    )
    explanation: str = Field(..., description="Deterministic explanation of why this entity is impacted")


class ImpactSummary(BaseModel):
    """Statistical overview of change-impact analysis."""
    total_changed_files: int = 0
    total_changed_symbols: int = 0
    total_impacted_entities: int = 0
    direct_impacts: int = 0
    transitive_impacts: int = 0
    potential_impacts: int = 0
    unresolved_impacts: int = 0
    impacted_modules: int = 0
    impacted_symbols: int = 0
    impacted_files: int = 0
    max_depth_reached: int = 0
    is_truncated: bool = False
    truncation_reason: Optional[str] = None


class ImpactReport(BaseModel):
    """Complete change-impact analysis report."""
    changeset: ChangeSet = Field(default_factory=ChangeSet, description="Underlying changeset analyzed")
    impacted_entities: List[ImpactedEntity] = Field(default_factory=list, description="All identified impacted entities")
    summary: ImpactSummary = Field(default_factory=ImpactSummary, description="Statistical metrics and truncation status")
    warnings: List[str] = Field(default_factory=list, description="Diagnostics, containment warnings, or truncation notices")
