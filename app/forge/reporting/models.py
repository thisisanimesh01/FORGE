"""Data models and schemas for FORGE investigation reports."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

REPORT_SCHEMA_VERSION = "1.0.0"


class ReportEvidenceItem(BaseModel):
    """Summarized evidence item for inclusion in audit reports."""
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_type: str
    description: str
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    provenance: str = "forge:system"
    content_snippet: str = ""


class ReportFinding(BaseModel):
    """Detailed finding entry in an investigation report."""
    finding_id: str
    title: str
    description: str
    severity: str
    confidence: str
    lifecycle_state: str
    affected_files: List[str] = Field(default_factory=list)
    limitations: Optional[str] = None
    evidence_items: List[ReportEvidenceItem] = Field(default_factory=list)


class ReportTimelineEvent(BaseModel):
    """High-level event entry in the audit timeline."""
    sequence: int
    event_type: str
    timestamp: str
    summary: str


class InvestigationReport(BaseModel):
    """Complete, versioned investigation report for an audit run."""
    model_config = ConfigDict(frozen=True)

    schema_version: str = REPORT_SCHEMA_VERSION
    run_id: str
    target: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0

    # System and runtime environment diagnostics
    system_environment: Dict[str, Any] = Field(default_factory=dict)
    capabilities_unavailable: List[str] = Field(default_factory=list)

    # Statistical summary
    summary_counts: Dict[str, Any] = Field(default_factory=dict)

    # Core contents
    findings: List[ReportFinding] = Field(default_factory=list)
    evidence: List[ReportEvidenceItem] = Field(default_factory=list)
    events_timeline: List[ReportTimelineEvent] = Field(default_factory=list)
    failure_details: Optional[Dict[str, Any]] = None
    final_synthesis: Optional[str] = None
