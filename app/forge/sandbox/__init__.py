"""FORGE Sandbox subsystem: isolated execution interface and security policies."""

from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    NetworkPolicy,
    SandboxRequest,
    SandboxResult,
)
from forge.sandbox.policy import (
    validate_sandbox_request,
    PolicyViolationError,
    APPROVED_EXECUTABLES,
    FORBIDDEN_EXECUTABLES,
    ALLOWED_ENV_VAR_NAMES,
)
from forge.sandbox.adapter import Sandbox
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.evidence import record_sandbox_result_in_store

__all__ = [
    "ExecutionLimits",
    "ExecutionStatus",
    "FailureClassification",
    "NetworkPolicy",
    "SandboxRequest",
    "SandboxResult",
    "validate_sandbox_request",
    "PolicyViolationError",
    "APPROVED_EXECUTABLES",
    "FORBIDDEN_EXECUTABLES",
    "ALLOWED_ENV_VAR_NAMES",
    "Sandbox",
    "DockerSandbox",
    "record_sandbox_result_in_store",
]
