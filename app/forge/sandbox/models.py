"""Pydantic data models and schemas for sandbox execution in FORGE."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ExecutionStatus(str, Enum):
    """Execution status outcome of a sandbox request."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    RUNTIME_ERROR = "runtime_error"


class FailureClassification(str, Enum):
    """Categorized reason for failure, rejection, or skip."""
    NONE = "none"
    POLICY_VIOLATION = "policy_violation"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    CONTAINER_ERROR = "container_error"
    COMMAND_FAILED = "command_failed"


class NetworkPolicy(str, Enum):
    """Network access policy for the sandbox container."""
    DISABLED = "none"
    ALLOWLIST = "allowlist"


class ExecutionLimits(BaseModel):
    """Resource and timing boundaries enforced on sandbox execution."""
    model_config = ConfigDict(frozen=True)

    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0, description="Max execution duration in seconds")
    memory_limit: str = Field(default="512m", description="Container memory limit (e.g. 512m, 1g)")
    cpu_quota: float = Field(default=1.0, ge=0.1, le=4.0, description="Max CPU cores quota")
    pids_limit: int = Field(default=64, ge=16, le=256, description="Max process/thread count")
    max_stdout_bytes: int = Field(default=50_000, ge=1_000, le=100_000, description="Max stdout captured bytes")
    max_stderr_bytes: int = Field(default=50_000, ge=1_000, le=100_000, description="Max stderr captured bytes")


class SandboxRequest(BaseModel):
    """Structured request to run an approved command in an isolated sandbox."""
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(description="Unique identifier for the sandbox execution request")
    run_id: str = Field(description="Associated FORGE run identifier")
    workspace_path: Path = Field(description="Host filesystem path to the acquired repository workspace")
    command: List[str] = Field(description="Command and arguments represented as a typed array (never a shell string)")
    relative_cwd: str = Field(default=".", description="Working directory relative to repository root")
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits, description="Execution boundaries")
    env_vars: Dict[str, str] = Field(default_factory=dict, description="Allowed environment variables")
    network_policy: NetworkPolicy = Field(default=NetworkPolicy.DISABLED, description="Network boundary")
    read_only_workspace: bool = Field(default=True, description="Whether to mount workspace read-only")
    provenance: str = Field(default="forge:sandbox", description="Originating tool or analyzer")


class SandboxResult(BaseModel):
    """Structured outcome from a sandbox execution attempt."""
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(description="Matching request identifier")
    run_id: str = Field(description="Associated FORGE run identifier")
    status: ExecutionStatus = Field(description="High-level execution status")
    exit_code: Optional[int] = Field(default=None, description="Process exit code if process ran")
    stdout: str = Field(default="", description="Sanitized and bounded standard output")
    stderr: str = Field(default="", description="Sanitized and bounded standard error")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = Field(default=0.0, description="Execution time in milliseconds")
    timed_out: bool = Field(default=False, description="Whether the command exceeded timeout")
    stdout_truncated: bool = Field(default=False, description="Whether stdout exceeded byte limits")
    stderr_truncated: bool = Field(default=False, description="Whether stderr exceeded byte limits")
    isolation_established: bool = Field(default=False, description="Whether isolated container execution was active")
    failure_classification: FailureClassification = Field(
        default=FailureClassification.NONE,
        description="Structured classification of any failure",
    )
    error_message: Optional[str] = Field(default=None, description="Diagnostic message when status != succeeded")
    runtime_metadata: Dict[str, Any] = Field(default_factory=dict, description="Container and execution metadata")
