"""Typed execution contracts for isolated defect reproduction and sandboxed verification.

Part of Phase 3, Milestone 1: Defect Reproduction & Verification.

Guarantees:
- Strict Pydantic models with exhaustive runtime validation.
- Separation of reproduction specifications, command constraints, and execution outcomes.
- Prevention of arbitrary shell strings, shell meta-characters, and path traversal.
- Bounded execution limits (memory, CPU, timeout, output limits).
- Complete fail-closed semantics: refuses host fallback when Docker is unavailable.
"""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReproductionOutcome(str, Enum):
    """Categorized outcome of a reproduction execution."""
    REPRODUCED = "reproduced"              # Defect manifested as expected (e.g., expected exit code/pattern matched)
    NOT_REPRODUCED = "not_reproduced"      # Defect did not manifest (e.g. command passed unexpectedly)
    PASSED = "passed"                      # Clean control test passed as expected
    FAILED = "failed"                      # Execution failed with an unexpected error
    TIMEOUT = "timeout"                    # Execution exceeded allocated duration quota
    RESOURCE_EXHAUSTED = "resource_exhausted" # Container exceeded memory, CPU, or PID limit
    SKIPPED_SANDBOX_UNAVAILABLE = "skipped_sandbox_unavailable" # Docker unavailable; fail-closed
    ERROR = "error"                        # Malformed contract or initialization failure


class ExecutionEnvironmentSpec(BaseModel):
    """Resource quotas and container isolation parameters for a reproduction harness."""
    model_config = ConfigDict(frozen=True)

    docker_image: str = Field(default="python:3.11-slim", description="Docker image reference")
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0, description="Max execution timeout in seconds")
    memory_limit: str = Field(default="512m", description="Container memory limit (e.g., 512m, 1g)")
    cpu_quota: float = Field(default=1.0, ge=0.1, le=4.0, description="Max CPU core quota")
    pids_limit: int = Field(default=64, ge=16, le=256, description="Max process and thread count")
    network_enabled: bool = Field(default=False, description="Whether network is enabled. Must remain False in Phase 3.")
    read_only_root: bool = Field(default=True, description="Enforce read-only container root filesystem")
    tmpfs_size: str = Field(default="64M", description="Tmpfs mount size limit for /tmp")
    max_output_bytes: int = Field(default=50_000, ge=1_000, le=100_000, description="Max stdout/stderr bytes")

    @field_validator("docker_image")
    @classmethod
    def validate_docker_image(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Docker image identifier cannot be empty")
        cleaned = v.strip()
        if cleaned.startswith("-"):
            raise ValueError(f"Docker image cannot start with a flag: {cleaned}")
        pattern = re.compile(r"^[a-zA-Z0-9_.\-]+(/[a-zA-Z0-9_.\-]+)*(:[a-zA-Z0-9_.\-]+)?(@sha256:[a-fA-F0-9]{64})?$")
        if not pattern.match(cleaned):
            raise ValueError(f"Invalid or unsafe Docker image reference: {cleaned}")
        return cleaned

    @field_validator("network_enabled")
    @classmethod
    def validate_network_disabled(cls, v: bool) -> bool:
        if v:
            raise ValueError("network_enabled must be False; isolated sandbox forbids external network access")
        return v

    @field_validator("memory_limit")
    @classmethod
    def validate_memory_string(cls, v: str) -> str:
        if not re.match(r"^\d+[bkmgBKMG]$", v):
            raise ValueError(f"Invalid memory limit format: {v}")
        return v


class ReproductionCommand(BaseModel):
    """A strictly typed, non-shell command sequence to execute inside the sandbox."""
    model_config = ConfigDict(frozen=True)

    args: List[str] = Field(description="Command and arguments represented as a typed array (never a shell string)")
    relative_cwd: str = Field(default=".", description="Working directory relative to repository root")
    env_vars: Dict[str, str] = Field(default_factory=dict, description="Explicit environment variables")
    expected_exit_codes: List[int] = Field(default_factory=lambda: [1], description="Expected exit codes indicating defect reproduction")
    expected_output_patterns: List[str] = Field(default_factory=list, description="Regex patterns in stdout/stderr confirming reproduction")

    @field_validator("args")
    @classmethod
    def validate_args_array(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("Reproduction command args array cannot be empty")
        
        # Check executable binary
        binary = Path(v[0]).name
        from forge.sandbox.policy import APPROVED_EXECUTABLES, FORBIDDEN_EXECUTABLES, FORBIDDEN_ARGUMENT_PATTERNS
        if binary in FORBIDDEN_EXECUTABLES:
            raise ValueError(f"Executable '{binary}' is explicitly forbidden by security policy")
        if binary not in APPROVED_EXECUTABLES:
            raise ValueError(f"Executable '{binary}' is not in approved executables list ({APPROVED_EXECUTABLES})")

        # Disallow shell meta-characters across all arguments
        for arg in v:
            for pat in FORBIDDEN_ARGUMENT_PATTERNS:
                if pat.search(arg):
                    raise ValueError(f"Argument '{arg}' contains forbidden shell characters or metacharacters")
        return v

    @field_validator("relative_cwd")
    @classmethod
    def validate_cwd(cls, v: str) -> str:
        norm = Path(v).as_posix().strip()
        if norm.startswith("/") or ".." in norm.split("/"):
            raise ValueError(f"relative_cwd must be a relative path contained within the repository: '{v}'")
        return norm


class ReproductionRequest(BaseModel):
    """Specification of an isolated reproduction attempt for a target defect."""
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(description="Unique deterministic ID for this reproduction request")
    finding_id: str = Field(description="Target finding identifier being reproduced")
    target_repo: str = Field(description="Target repository directory or URL")
    commit_sha: Optional[str] = Field(default=None, description="Exact commit revision under test")
    hypothesis: str = Field(description="Concise description of the defect reproduction hypothesis")
    command: ReproductionCommand = Field(description="Non-shell command specification")
    environment: ExecutionEnvironmentSpec = Field(default_factory=ExecutionEnvironmentSpec, description="Isolation quotas")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")


class ReproductionResult(BaseModel):
    """Verifiable outcome of a reproduction run executed under contract boundaries."""
    model_config = ConfigDict(frozen=True)

    request_id: str = Field(description="Corresponding reproduction request ID")
    finding_id: str = Field(description="Target finding ID")
    outcome: ReproductionOutcome = Field(description="Deterministic reproduction outcome")
    exit_code: Optional[int] = Field(default=None, description="Observed process exit code")
    stdout: str = Field(default="", description="Sanitized and bounded standard output")
    stderr: str = Field(default="", description="Sanitized and bounded standard error")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Execution duration in milliseconds")
    matched_patterns: List[str] = Field(default_factory=list, description="Expected patterns that matched output")
    sandbox_available: bool = Field(default=True, description="Whether isolated sandbox execution was available")
    isolation_details: Dict[str, Any] = Field(default_factory=dict, description="Metadata describing isolation boundary")
    diagnostic_message: Optional[str] = Field(default=None, description="Diagnostic explanation of outcome or skip")
    evidence_id: Optional[str] = Field(default=None, description="Associated EvidenceRecord ID if persisted in store")
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Completion timestamp")
