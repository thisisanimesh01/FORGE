"""Phase 12 M5: Safe Sandbox Execution Adapter for Repair Verification.

Enforces strict containment and isolation boundaries:
- Validates executable names against policy allowlist.
- Rejects arbitrary shell strings, metacharacters, and command injection attempts.
- Enforces container network disabling, memory limits, and timeout boundaries.
- Rejects host execution fallback: fails closed if Docker sandbox is unavailable.
- Redacts secrets from stdout and stderr.
"""

from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from forge.config import redact_secrets
from forge.sandbox.adapter import Sandbox
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    SandboxRequest,
    SandboxResult,
)
from forge.repair_verification.schemas import (
    SelectedCheck,
    VerificationPolicy,
)


DISALLOWED_COMMAND_CHARS = re.compile(r"[;&|`$<>\r\n\x00]")


class VerificationSandbox:
    """Safe execution bridge executing verification checks inside container sandbox."""

    def __init__(self, sandbox: Optional[Sandbox] = None) -> None:
        self.sandbox: Sandbox = sandbox or DockerSandbox()

    def is_available(self) -> bool:
        """Check whether the secure isolation boundary is available."""
        return self.sandbox.is_available()

    def check_health(self) -> Tuple[bool, str]:
        """Verify runtime health of container sandbox."""
        return self.sandbox.check_health()

    def execute_check(
        self,
        check: SelectedCheck,
        workspace_path: Path,
        policy: VerificationPolicy,
        run_id: str = "verification_run",
    ) -> SandboxResult:
        """Execute a validated verification check inside isolated container.

        Fails closed with SANDBOX_UNAVAILABLE if container boundary is unavailable.
        """
        # 1. Validate command structure
        if not check.command or not isinstance(check.command, list):
            return SandboxResult(
                request_id=f"chk_{check.check_id}",
                run_id=run_id,
                status=ExecutionStatus.REJECTED,
                exit_code=1,
                stdout="",
                stderr="Rejected: command must be a non-empty list of strings.",
                failure_classification=FailureClassification.POLICY_VIOLATION,
                error_message="Invalid command vector",
            )

        binary = check.command[0]
        if "/" in binary or "\\" in binary:
            return SandboxResult(
                request_id=f"chk_{check.check_id}",
                run_id=run_id,
                status=ExecutionStatus.REJECTED,
                exit_code=1,
                stdout="",
                stderr=f"Rejected: binary '{binary}' cannot contain path separators.",
                failure_classification=FailureClassification.POLICY_VIOLATION,
                error_message="Path separator in executable",
            )
        # Allow python3 / python interchangeably if allowed
        base_binary = Path(binary).name
        if base_binary not in policy.allowed_executables:
            return SandboxResult(
                request_id=f"chk_{check.check_id}",
                run_id=run_id,
                status=ExecutionStatus.REJECTED,
                exit_code=1,
                stdout="",
                stderr=f"Rejected: binary '{base_binary}' is not permitted by verification policy.",
                failure_classification=FailureClassification.POLICY_VIOLATION,
                error_message=f"Disallowed binary '{base_binary}'",
            )

        # Disallow dangerous shell metacharacters in any argument
        for arg in check.command:
            if DISALLOWED_COMMAND_CHARS.search(arg):
                return SandboxResult(
                    request_id=f"chk_{check.check_id}",
                    run_id=run_id,
                    status=ExecutionStatus.REJECTED,
                    exit_code=1,
                    stdout="",
                    stderr=f"Rejected: argument '{arg}' contains disallowed shell metacharacters.",
                    failure_classification=FailureClassification.POLICY_VIOLATION,
                    error_message="Disallowed shell metacharacter in command arguments",
                )

        # 2. Check Sandbox Availability & Health
        is_healthy, health_msg = self.sandbox.check_health()
        if not is_healthy:
            return SandboxResult(
                request_id=f"chk_{check.check_id}",
                run_id=run_id,
                status=ExecutionStatus.UNAVAILABLE,
                exit_code=None,
                stdout="",
                stderr=f"Verification blocked: secure container sandbox is unavailable ({health_msg}). Host execution is strictly forbidden by FORGE policy.",
                failure_classification=FailureClassification.RUNTIME_UNAVAILABLE,
                error_message=f"Sandbox unavailable: {health_msg}",
            )

        # 3. Construct SandboxRequest
        capped_output_bytes = min(int(policy.max_output_bytes), 100_000)
        limits = ExecutionLimits(
            timeout_seconds=min(policy.timeout_seconds, 300.0),
            memory_limit=f"{policy.memory_limit_mb}m",
            cpu_quota=min(policy.cpu_quota, 4.0),
            pids_limit=min(policy.pids_limit, 256),
            max_stdout_bytes=capped_output_bytes,
            max_stderr_bytes=capped_output_bytes,
        )

        safe_env = {
            "PYTHONPATH": ".",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        req = SandboxRequest(
            request_id=f"sbx_v_{check.check_id}",
            run_id=run_id,
            workspace_path=workspace_path,
            command=check.command,
            relative_cwd=".",
            limits=limits,
            env_vars=safe_env,
            read_only_workspace=False,
            provenance=f"forge:verification:{check.check_id}",
        )

        # 4. Execute inside container
        result = self.sandbox.run(req)

        # 5. Sanitize output
        clean_stdout = redact_secrets(result.stdout)
        clean_stderr = redact_secrets(result.stderr)

        return SandboxResult(
            request_id=result.request_id,
            run_id=result.run_id,
            status=result.status,
            exit_code=result.exit_code,
            stdout=clean_stdout,
            stderr=clean_stderr,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            failure_classification=result.failure_classification,
            error_message=result.error_message,
        )
