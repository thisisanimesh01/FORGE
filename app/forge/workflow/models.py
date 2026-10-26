"""Typed models and contracts for Phase 5 end-to-end engineering workflow intake and context.

Phase 5, Milestone 1: Unified Task Intake & Repository Context Pipeline.

Invariants:
- Untrusted input boundary: all repository files, diffs, issues, and instructions are untrusted data.
- Immutability: target repository is never mutated; git state is strictly read-only.
- Deterministic IDs and stable ordering across all collections.
- Strict schema validation using Pydantic V2 with frozen models where appropriate.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.config import redact_secrets
from forge.reproduction.models import FieldProvenance
from forge.repository.detector import ProjectIntelligence
from forge.repository.indexer import SymbolRecord
from forge.repository.instructions import InstructionFile
from forge.schemas import FindingConfidence, FindingSeverity


# =============================================================================
# Exception Hierarchy
# =============================================================================

class WorkflowError(Exception):
    """Base exception for all workflow-related errors."""


class IntakeValidationError(WorkflowError):
    """Raised when task intake fails schema validation or integrity constraints."""


class RevisionDriftError(WorkflowError):
    """Raised when target repository HEAD does not match expected pinned commit SHA."""


class DirtyRepositoryError(WorkflowError):
    """Raised when target repository has uncommitted changes and clean tree is required."""


class ContextBudgetExceededError(WorkflowError):
    """Raised when repository context exceeds maximum allocation bounds."""


class TargetImmutabilityViolationError(WorkflowError):
    """Raised when an operation attempts to mutate or compromise target repository."""


# =============================================================================
# Enums
# =============================================================================

class IntakeSourceType(str, Enum):
    """Classification of supported intake sources."""
    REVIEW_FINDING = "review_finding"
    UNIFIED_DIFF = "unified_diff"
    ISSUE_DESCRIPTION = "issue_description"
    TEST_SPECIFICATION = "test_specification"


# =============================================================================
# Revision Models
# =============================================================================

class RepositoryRevisionInfo(BaseModel):
    """Immutable record of resolved repository git revision state."""
    model_config = ConfigDict(frozen=True)

    commit_sha: str = Field(description="Full 40-character hexadecimal commit SHA")
    short_sha: str = Field(description="Short 8-character commit SHA prefix")
    branch: Optional[str] = Field(default=None, description="Current branch name or HEAD")
    is_dirty: bool = Field(default=False, description="Whether working tree has uncommitted modifications")
    dirty_files: List[str] = Field(default_factory=list, description="List of uncommitted or modified files")
    head_commit_message: Optional[str] = Field(default=None, description="Subject line of HEAD commit")
    head_commit_timestamp: Optional[str] = Field(default=None, description="ISO timestamp of HEAD commit")

    @field_validator("commit_sha")
    @classmethod
    def validate_commit_sha(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not re.match(r"^[0-9a-f]{7,40}$", cleaned):
            raise ValueError(f"Invalid git commit SHA: '{v}' (must be 7-40 hex chars)")
        return cleaned


# =============================================================================
# Intake Payloads
# =============================================================================

class FindingIntakePayload(BaseModel):
    """Payload for intake originating from an audited review or investigation finding."""
    model_config = ConfigDict(frozen=True)

    finding_id: str = Field(description="Unique finding identifier in FORGE storage")
    run_id: Optional[str] = Field(default=None, description="Associated audit run ID")
    severity: Optional[FindingSeverity] = Field(default=None, description="Finding severity level")
    confidence: Optional[FindingConfidence] = Field(default=None, description="Finding confidence rating")
    rule_id: Optional[str] = Field(default=None, description="Associated policy rule identifier if any")
    category: Optional[str] = Field(default=None, description="Finding category (security, defect, logic, etc.)")
    evidence_ids: List[str] = Field(default_factory=list, description="IDs of linked evidence records")


class DiffIntakePayload(BaseModel):
    """Payload for intake originating from a unified git diff."""
    model_config = ConfigDict(frozen=True)

    raw_diff: str = Field(description="Sanitized unified diff content")
    base_revision: Optional[str] = Field(default=None, description="Base revision of diff")
    head_revision: Optional[str] = Field(default=None, description="Head revision of diff")
    total_additions: int = Field(default=0, ge=0, description="Total lines added")
    total_deletions: int = Field(default=0, ge=0, description="Total lines deleted")
    diff_bytes: int = Field(default=0, ge=0, description="Size of diff in bytes")


class IssueIntakePayload(BaseModel):
    """Payload for intake originating from an issue or defect report."""
    model_config = ConfigDict(frozen=True)

    issue_id: Optional[str] = Field(default=None, description="Optional external issue identifier (e.g. #142)")
    issue_text: str = Field(description="Full description or problem statement")
    expected_behavior: Optional[str] = Field(default=None, description="Expected software behavior")
    observed_behavior: Optional[str] = Field(default=None, description="Observed defective behavior")
    reproduction_hints: List[str] = Field(default_factory=list, description="Hints or commands to reproduce")


class TestSpecIntakePayload(BaseModel):
    """Payload for intake originating from a specific failing test specification."""
    model_config = ConfigDict(frozen=True)

    test_file: str = Field(description="Relative path to test file")
    test_command: str = Field(description="Exact check/test command to run")
    test_name: Optional[str] = Field(default=None, description="Specific test function or class name")
    expected_failure_pattern: Optional[str] = Field(default=None, description="Expected failure regex/substring")
    framework: str = Field(default="pytest", description="Testing framework (pytest, unittest, etc.)")


# =============================================================================
# Unified Workflow Intake Specification
# =============================================================================

class WorkflowIntakeSpec(BaseModel):
    """Canonical, normalized task intake contract for Phase 5 engineering workflows."""
    model_config = ConfigDict(frozen=True)

    intake_id: str = Field(description="Deterministic intake identifier (intake_<sha256_prefix>)")
    source_type: IntakeSourceType = Field(description="Origin type of this defect intake")
    target_repo: str = Field(description="Repository URL or normalized local path")
    base_commit_sha: str = Field(description="Pinned hexadecimal commit SHA")
    title: str = Field(description="Descriptive summary of the task")
    description: str = Field(description="Detailed defect description or instructions")
    affected_files: List[str] = Field(default_factory=list, description="Sanitized relative paths involved")
    finding_payload: Optional[FindingIntakePayload] = Field(default=None)
    diff_payload: Optional[DiffIntakePayload] = Field(default=None)
    issue_payload: Optional[IssueIntakePayload] = Field(default=None)
    test_payload: Optional[TestSpecIntakePayload] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary sanitized metadata")
    provenance: Dict[str, FieldProvenance] = Field(default_factory=dict, description="Field provenance mappings")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("base_commit_sha")
    @classmethod
    def validate_sha(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not re.match(r"^[0-9a-f]{7,40}$", cleaned):
            raise ValueError(f"Invalid git commit SHA: '{v}'")
        return cleaned

    @field_validator("affected_files")
    @classmethod
    def validate_affected_files(cls, files: List[str]) -> List[str]:
        cleaned: List[str] = []
        for f in files:
            norm = Path(f).as_posix().strip()
            if not norm or norm == ".":
                continue
            if norm.startswith("/") or norm.startswith("\\") or re.match(r"^[a-zA-Z]:", norm):
                raise ValueError(f"Absolute path forbidden in affected_files: '{f}'")
            parts = norm.split("/")
            if ".." in parts:
                raise ValueError(f"Path traversal ('..') forbidden in affected_files: '{f}'")
            cleaned.append(norm)
        return sorted(list(set(cleaned)))

    @model_validator(mode="after")
    def validate_payload_consistency(self) -> "WorkflowIntakeSpec":
        """Ensure the appropriate payload is supplied and matches source_type."""
        if self.source_type == IntakeSourceType.REVIEW_FINDING:
            if self.finding_payload is None:
                raise ValueError("finding_payload is required when source_type is REVIEW_FINDING")
        elif self.source_type == IntakeSourceType.UNIFIED_DIFF:
            if self.diff_payload is None:
                raise ValueError("diff_payload is required when source_type is UNIFIED_DIFF")
        elif self.source_type == IntakeSourceType.ISSUE_DESCRIPTION:
            if self.issue_payload is None:
                raise ValueError("issue_payload is required when source_type is ISSUE_DESCRIPTION")
        elif self.source_type == IntakeSourceType.TEST_SPECIFICATION:
            if self.test_payload is None:
                raise ValueError("test_payload is required when source_type is TEST_SPECIFICATION")
        return self


# =============================================================================
# Context Pipeline Models
# =============================================================================

class ContextBudget(BaseModel):
    """Explicit size and token budget constraints for repository context assembly."""
    model_config = ConfigDict(frozen=True)

    max_total_chars: int = Field(default=25_000, ge=1_000, le=150_000, description="Max total rendered characters")
    max_symbols: int = Field(default=50, ge=5, le=200, description="Max AST symbols to include")
    max_instruction_chars: int = Field(default=4_000, ge=500, le=20_000, description="Max chars across instructions")
    max_source_lines_per_file: int = Field(default=100, ge=10, le=500, description="Max source lines per file excerpt")
    max_files_excerpted: int = Field(default=5, ge=1, le=20, description="Max files to excerpt source code for")


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


class ContextBudgetSummary(BaseModel):
    """Detailed summary of context budgeting and truncation indicators."""
    model_config = ConfigDict(frozen=True)

    char_budget: int = Field(description="Configured total character budget")
    total_chars: int = Field(description="Actual total characters in rendered prompt")
    estimated_tokens: int = Field(description="Estimated token count (~4 chars/token)")
    is_truncated: bool = Field(default=False, description="Whether any truncation occurred")
    truncation_reasons: List[str] = Field(default_factory=list, description="Explanations for truncation events")


class RepositoryContext(BaseModel):
    """Structured, immutable repository context bundle assembled for an intake task."""
    model_config = ConfigDict(frozen=True)

    context_id: str = Field(description="Unique deterministic context identifier")
    intake_id: str = Field(description="Associated workflow intake identifier")
    target_repo: str = Field(description="Target repository identifier or path")
    revision: RepositoryRevisionInfo = Field(description="Pinned repository revision metadata")
    intelligence: ProjectIntelligence = Field(description="Static framework and project intelligence")
    relevant_symbols: List[SymbolRecord] = Field(default_factory=list, description="Ranked, sorted AST symbols")
    instruction_files: List[InstructionFile] = Field(default_factory=list, description="Discovered instructions")
    source_excerpts: List[SourceExcerpt] = Field(default_factory=list, description="Source excerpts for affected files")
    provenance: Dict[str, FieldProvenance] = Field(default_factory=dict, description="Field provenance mappings")
    budget_summary: ContextBudgetSummary = Field(description="Budget accounting and truncation audit")
    rendered_prompt: str = Field(description="Budgeted, untrusted-framed markdown prompt for model intake")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
