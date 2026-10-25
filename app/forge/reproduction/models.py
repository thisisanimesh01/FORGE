"""Typed data models for reproduction case management and evidence capture.

Phase 3, Milestone 2: Reproduction Case Management & Evidence Capture.

Enforces:
- Stable case ID determinism based on finding ID, target repo, and pinned commit SHA.
- Explicit case status tracking (`pending`, `running`, `reproduced`, `not_reproduced`, `inconclusive`, `blocked`, `failed`).
- Fine-grained attempt records with stdout/stderr digests, truncated indicators, and cleanup status.
- Rigorous field-level provenance tracking (fixture metadata, user input, static analysis, LLM, or sandbox).
- Retry policy specification preventing automatic retries of non-idempotent commands.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge.sandbox.contracts import (
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)


class CaseStatus(str, Enum):
    """Lifecycle status of a reproduction case."""
    PENDING = "pending"
    RUNNING = "running"
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"
    FAILED = "failed"


class FieldProvenance(str, Enum):
    """Origin of a field or specification."""
    TRUSTED_FIXTURE = "trusted_fixture"
    USER_INPUT = "user_input"
    STATIC_ANALYSIS = "static_analysis"
    UNTRUSTED_LLM = "untrusted_llm"
    UNTRUSTED_REPOSITORY = "untrusted_repository"
    SANDBOX_EXECUTION = "sandbox_execution"
    SYSTEM_POLICY = "system_policy"


class RetryPolicy(BaseModel):
    """Policy governing safe, bounded reproduction attempt retries."""
    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(default=0, ge=0, le=3, description="Maximum number of retries permitted (default: 0)")
    is_idempotent: bool = Field(default=True, description="Whether the reproduction command is known to be idempotent")
    retry_on_outcomes: List[ReproductionOutcome] = Field(
        default_factory=lambda: [ReproductionOutcome.TIMEOUT, ReproductionOutcome.FAILED],
        description="Specific transient outcomes eligible for bounded retry",
    )


class ReproductionAttempt(BaseModel):
    """Structured record of a single reproduction execution attempt."""
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(description="Unique identifier for this reproduction attempt")
    case_id: str = Field(description="Associated reproduction case ID")
    attempt_number: int = Field(ge=1, description="Sequential 1-based attempt count")
    run_id: str = Field(description="Audit correlation run ID")
    outcome: ReproductionOutcome = Field(description="Reproduction execution outcome")
    exit_code: Optional[int] = Field(default=None, description="Observed exit code")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Duration in milliseconds")
    stdout_digest: Optional[str] = Field(default=None, description="SHA256 hex digest of raw stdout")
    stderr_digest: Optional[str] = Field(default=None, description="SHA256 hex digest of raw stderr")
    stdout_excerpt: str = Field(default="", description="Sanitized, bounded stdout excerpt")
    stderr_excerpt: str = Field(default="", description="Sanitized, bounded stderr excerpt")
    stdout_truncated: bool = Field(default=False, description="Whether stdout was truncated")
    stderr_truncated: bool = Field(default=False, description="Whether stderr was truncated")
    matched_patterns: List[str] = Field(default_factory=list, description="Expected patterns matched")
    evidence_id: Optional[str] = Field(default=None, description="ID of persisted EvidenceRecord")
    isolation_details: Dict[str, Any] = Field(default_factory=dict, description="Metadata describing sandbox boundary")
    provenance: Dict[str, FieldProvenance] = Field(default_factory=dict, description="Provenance for result components")
    diagnostic_message: Optional[str] = Field(default=None, description="Diagnostic explanation")
    cleanup_status: str = Field(default="cleaned", description="Container and scratch workspace cleanup status")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReproductionCase(BaseModel):
    """A managed reproduction case tracking defect reproduction attempts and evidence."""
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(description="Deterministic case identifier")
    finding_id: str = Field(description="Target finding identifier")
    run_id: str = Field(description="Associated audit run ID")
    target_repo: str = Field(description="Repository URL or local directory")
    target_commit_sha: str = Field(description="Immutable pinned commit revision")
    contract: ReproductionRequest = Field(description="Validated reproduction contract")
    policy_version: str = Field(default="1.0.0", description="Resource and sandbox policy version")
    status: CaseStatus = Field(default=CaseStatus.PENDING, description="Current aggregate case status")
    attempts_count: int = Field(default=0, ge=0, description="Number of attempts executed")
    max_retries: int = Field(default=0, ge=0, description="Configured max retries")
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy, description="Retry constraints")
    provenance_map: Dict[str, FieldProvenance] = Field(default_factory=dict, description="Field-level provenance")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def generate_case_id(finding_id: str, target_repo: str, commit_sha: str) -> str:
    """Compute a deterministic, stable case ID from finding ID, target, and commit SHA."""
    norm_repo = str(Path(target_repo).resolve().as_posix() if Path(target_repo).exists() else target_repo.strip())
    raw = f"{finding_id}::{norm_repo}::{commit_sha.strip()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    safe_fid = finding_id.replace(":", "_").replace("/", "_")
    return f"case_{safe_fid}_{digest}"
