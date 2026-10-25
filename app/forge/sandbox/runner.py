"""Harness for executing reproduction contracts under strict sandbox boundaries.

Enforces:
- Direct mapping of ReproductionRequest to SandboxRequest.
- Docker availability verification.
- Fail-closed skip reporting when Docker is absent (host execution is NEVER attempted).
- Deterministic outcome classification based on expected exit codes and output patterns.
- Secret redaction and output bounding.
"""

from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Optional

from forge.config import redact_secrets
from forge.sandbox.contracts import (
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    SandboxRequest,
    SandboxResult,
)


class ReproductionHarness:
    """Evaluates reproduction contracts in an isolated sandbox or fails closed."""

    def __init__(self, sandbox: Optional[DockerSandbox] = None):
        self.sandbox = sandbox or DockerSandbox()

    def execute(self, request: ReproductionRequest, workspace_path: Path) -> ReproductionResult:
        """Execute a reproduction request against an acquired repository workspace."""
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc)

        # 1. Health check & fail-closed behavior
        is_healthy, health_msg = self.sandbox.check_health()
        if not is_healthy:
            duration_ms = (time.monotonic() - start_time) * 1000.0
            return ReproductionResult(
                request_id=request.request_id,
                finding_id=request.finding_id,
                outcome=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=duration_ms,
                matched_patterns=[],
                sandbox_available=False,
                isolation_details={"reason": health_msg},
                diagnostic_message=(
                    f"Reproduction skipped: isolated container sandbox is unavailable ({health_msg}). "
                    "FORGE fail-closed security policy strictly refuses to run repository code directly on host."
                ),
                completed_at=datetime.now(timezone.utc),
            )

        # 2. Build SandboxRequest
        env = request.environment
        cmd = request.command
        limits = ExecutionLimits(
            timeout_seconds=env.timeout_seconds,
            memory_limit=env.memory_limit,
            cpu_quota=env.cpu_quota,
            pids_limit=env.pids_limit,
            max_stdout_bytes=env.max_output_bytes,
            max_stderr_bytes=env.max_output_bytes,
        )

        sandbox_req = SandboxRequest(
            request_id=f"sbx_{request.request_id}",
            run_id="reproduction_harness",
            workspace_path=workspace_path,
            command=cmd.args,
            relative_cwd=cmd.relative_cwd,
            limits=limits,
            env_vars=cmd.env_vars,
            read_only_workspace=True,
            provenance=f"forge:reproduction:{request.finding_id}",
        )

        # 3. Execute inside isolated container
        sbx_result: SandboxResult = self.sandbox.run(sandbox_req)
        duration_ms = (time.monotonic() - start_time) * 1000.0

        # 4. Classify outcome
        stdout_clean = redact_secrets(sbx_result.stdout)
        stderr_clean = redact_secrets(sbx_result.stderr)
        combined_output = f"{stdout_clean}\n{stderr_clean}"

        matched_patterns = []
        for pat in cmd.expected_output_patterns:
            if re.search(pat, combined_output):
                matched_patterns.append(pat)

        if sbx_result.timed_out:
            outcome = ReproductionOutcome.TIMEOUT
            diagnostic = f"Command exceeded timeout quota ({env.timeout_seconds}s)"
        elif sbx_result.status == ExecutionStatus.UNAVAILABLE:
            outcome = ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE
            diagnostic = sbx_result.error_message or "Sandbox unavailable"
        elif sbx_result.exit_code in cmd.expected_exit_codes:
            # Check pattern requirements if any
            if cmd.expected_output_patterns:
                if len(matched_patterns) == len(cmd.expected_output_patterns):
                    outcome = ReproductionOutcome.REPRODUCED
                    diagnostic = f"Defect reproduced: exit code {sbx_result.exit_code} and all {len(matched_patterns)} expected patterns matched."
                else:
                    outcome = ReproductionOutcome.NOT_REPRODUCED
                    diagnostic = f"Exit code {sbx_result.exit_code} matched, but missing expected output patterns."
            else:
                outcome = ReproductionOutcome.REPRODUCED
                diagnostic = f"Defect reproduced: exit code {sbx_result.exit_code} matched expected reproduction codes ({cmd.expected_exit_codes})."
        elif sbx_result.exit_code == 0:
            outcome = ReproductionOutcome.PASSED
            diagnostic = "Clean execution: command exited with 0 (defect did not reproduce)."
        else:
            outcome = ReproductionOutcome.FAILED
            diagnostic = f"Execution exited with unexpected code {sbx_result.exit_code}."

        return ReproductionResult(
            request_id=request.request_id,
            finding_id=request.finding_id,
            outcome=outcome,
            exit_code=sbx_result.exit_code,
            stdout=stdout_clean,
            stderr=stderr_clean,
            duration_ms=duration_ms,
            matched_patterns=matched_patterns,
            sandbox_available=True,
            isolation_details={
                "image": env.docker_image,
                "read_only_root": env.read_only_root,
                "network_disabled": not env.network_enabled,
                "memory_limit": env.memory_limit,
            },
            diagnostic_message=diagnostic,
            completed_at=datetime.now(timezone.utc),
        )
