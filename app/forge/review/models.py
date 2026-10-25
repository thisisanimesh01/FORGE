"""Data models for diff-aware code review."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.schemas import FindingSeverity, FindingConfidence

REVIEW_SCHEMA_VERSION = "1.0.0"


class ReviewFileStatus(str, Enum):
    """Status classification of a changed file in a diff."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    BINARY = "binary"
    UNMODIFIED = "unmodified"


class DiffLine(BaseModel):
    """Single line representation in a diff hunk."""
    model_config = ConfigDict(frozen=True)

    origin: str  # '+', '-', ' '
    content: str
    old_lineno: Optional[int] = None
    new_lineno: Optional[int] = None


class DiffHunk(BaseModel):
    """Structured diff hunk with line mappings."""
    model_config = ConfigDict(frozen=True)

    header: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[DiffLine] = Field(default_factory=list)

    @property
    def added_lines(self) -> List[DiffLine]:
        """Return all added lines in this hunk."""
        return [l for l in self.lines if l.origin == "+"]

    @property
    def changed_line_numbers(self) -> List[int]:
        """List of 1-indexed target line numbers introduced or modified."""
        return [l.new_lineno for l in self.lines if l.origin == "+" and l.new_lineno is not None]


class DiffFile(BaseModel):
    """File-level changes parsed from a unified diff."""
    model_config = ConfigDict(frozen=True)

    path: str
    old_path: Optional[str] = None
    status: ReviewFileStatus = ReviewFileStatus.MODIFIED
    hunks: List[DiffHunk] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False
    is_oversized: bool = False

    @property
    def changed_line_numbers(self) -> List[int]:
        """All changed line numbers across hunks for this file."""
        lines: List[int] = []
        for h in self.hunks:
            lines.extend(h.changed_line_numbers)
        return sorted(list(set(lines)))


class DiffBundle(BaseModel):
    """Complete structured diff between base and head revisions."""
    model_config = ConfigDict(frozen=True)

    base_revision: str
    head_revision: str
    files: List[DiffFile] = Field(default_factory=list)
    total_files_changed: int = 0
    total_additions: int = 0
    total_deletions: int = 0
    raw_size_bytes: int = 0
    is_truncated: bool = False


class ReviewFinding(BaseModel):
    """Code review finding tied directly to diff changes."""
    model_config = ConfigDict(frozen=True)

    finding_id: str
    file_path: str
    line_number: Optional[int] = None
    end_line_number: Optional[int] = None
    severity: FindingSeverity = FindingSeverity.MEDIUM
    confidence: FindingConfidence = FindingConfidence.MEDIUM
    title: str
    explanation: str
    rule_id: str
    suggested_action: Optional[str] = None
    code_snippet: Optional[str] = None
    evidence_references: List[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    """Complete versioned review report."""
    model_config = ConfigDict(frozen=True)

    schema_version: str = REVIEW_SCHEMA_VERSION
    review_id: str
    target: str
    base_revision: str
    head_revision: str
    mode: str = "static"
    created_at: str
    duration_seconds: float = 0.0
    diff_summary: Dict[str, Any] = Field(default_factory=dict)
    summary_counts: Dict[str, Any] = Field(default_factory=dict)
    findings: List[ReviewFinding] = Field(default_factory=list)
    files_reviewed: List[str] = Field(default_factory=list)
    system_environment: Dict[str, Any] = Field(default_factory=dict)
    capabilities_unavailable: List[str] = Field(default_factory=list)
