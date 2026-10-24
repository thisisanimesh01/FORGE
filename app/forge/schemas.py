"""Pydantic schemas for FORGE Phase 1 data models."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingConfidence(str, Enum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingLifecycleState(str, Enum):
    """Explicit finding lifecycle. Phase 1 only permits suspected or reproduction analysis."""
    SUSPECTED = "suspected"
    REPRODUCED = "reproduced"
    ROOT_CAUSED = "root_caused"
    FIXED = "fixed"
    VERIFIED = "verified"


class EvidenceType(str, Enum):
    SOURCE_LOCATION = "source_location"
    STATIC_CHECK = "static_check"
    COMMAND_OUTPUT = "command_output"
    STACK_TRACE = "stack_trace"
    TEST_RESULT = "test_result"
    REPOSITORY_METADATA = "repository_metadata"


class EvidenceRecord(BaseModel):
    """Concrete evidence record supporting a finding."""
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(description="Unique identifier for the evidence")
    evidence_type: EvidenceType = Field(description="Type of evidence recorded")
    description: str = Field(description="Human-readable description of what this evidence shows")
    file_path: Optional[str] = Field(default=None, description="Path relative to repository root")
    start_line: Optional[int] = Field(default=None, description="Starting line number")
    end_line: Optional[int] = Field(default=None, description="Ending line number")
    content: str = Field(description="Observed output, snippet, or trace")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Finding(BaseModel):
    """A structured finding discovered during audit or investigation."""
    finding_id: str = Field(description="Unique finding identifier")
    title: str = Field(description="Summary title of the finding")
    description: str = Field(description="Detailed explanation of the issue")
    severity: FindingSeverity = Field(default=FindingSeverity.MEDIUM)
    confidence: FindingConfidence = Field(default=FindingConfidence.MEDIUM)
    lifecycle_state: FindingLifecycleState = Field(default=FindingLifecycleState.SUSPECTED)
    affected_files: List[str] = Field(default_factory=list, description="List of file paths involved")
    supporting_evidence_ids: List[str] = Field(
        default_factory=list,
        description="IDs of EvidenceRecords proving this finding",
    )
    suggested_investigation_step: Optional[str] = Field(
        default=None,
        description="Suggested next check or reading step",
    )
    limitations: Optional[str] = Field(default=None, description="Known uncertainties or limitations")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class BaselineCheckResult(BaseModel):
    """Result of an individual baseline check."""
    check_id: str = Field(description="Stable identifier for the check, e.g. python_ast_syntax")
    name: str = Field(description="Display name")
    status: CheckStatus = Field(description="Outcome status")
    duration_ms: float = Field(description="Execution time in milliseconds")
    details: str = Field(description="Sanitized summary or error description")
    output: Optional[str] = Field(default=None, description="Sanitized check stdout/stderr")
    skipped_reason: Optional[str] = Field(default=None, description="Reason if skipped")


class RepositoryMetadata(BaseModel):
    """Discovered repository properties and layout."""
    url_or_path: str = Field(description="Original input target")
    resolved_commit_sha: Optional[str] = Field(default=None, description="Git commit SHA if available")
    target_branch: Optional[str] = Field(default=None, description="Branch inspected")
    is_python: bool = Field(default=False)
    python_version_detected: Optional[str] = Field(default=None)
    frameworks: List[str] = Field(default_factory=list)
    has_agents_md: bool = Field(default=False)
    instruction_files: List[str] = Field(default_factory=list)
    total_files: int = Field(default=0)
    total_lines: int = Field(default=0)


class EventType(str, Enum):
    RUN_CREATED = "run_created"
    REPOSITORY_VALIDATED = "repository_validated"
    REPOSITORY_ACQUIRED = "repository_acquired"
    REPOSITORY_INSPECTED = "repository_inspected"
    BASELINE_STARTED = "baseline_started"
    BASELINE_COMPLETED = "baseline_completed"
    LLM_REQUEST_STARTED = "llm_request_started"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    EVIDENCE_RECORDED = "evidence_recorded"
    FINDING_CREATED = "finding_created"
    REPORT_GENERATED = "report_generated"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class RunEvent(BaseModel):
    """Immutable event representing a step during an audit run."""
    event_id: str = Field(description="Unique event ID")
    run_id: str = Field(description="Associated run ID")
    sequence: int = Field(description="Sequential ordering number within the run")
    event_type: EventType = Field(description="Type classification of this event")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Dict[str, Any] = Field(default_factory=dict, description="Sanitized event parameters")


class RunSummary(BaseModel):
    """High-level summary of an audit execution."""
    run_id: str = Field(description="Unique run ID")
    target: str = Field(description="Inspected target repository")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None)
    status: str = Field(default="running")
    total_findings: int = Field(default=0)
    total_evidence: int = Field(default=0)
    checks_passed: int = Field(default=0)
    checks_failed: int = Field(default=0)
    checks_skipped: int = Field(default=0)
