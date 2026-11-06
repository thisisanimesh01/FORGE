"""FORGE Change-Impact Analysis Engine (Phase 11 Milestone 4).

Provides evidence-backed, deterministic change-impact analysis by propagating
symbol-level and file-level changes through reverse call graphs and module
dependency graphs.
"""

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
    ImpactPathStep,
    ImpactReport,
    ImpactSummary,
    ImpactedEntity,
    SymbolChange,
)

__all__ = [
    "ChangeImpactAnalyzer",
    "ChangeSet",
    "ChangeType",
    "EntityType",
    "FileChange",
    "ImpactClassification",
    "ImpactPathStep",
    "ImpactReport",
    "ImpactSummary",
    "ImpactedEntity",
    "SymbolChange",
    "extract_changeset_from_diff",
    "extract_changeset_from_files",
]
