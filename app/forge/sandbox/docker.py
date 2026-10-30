"""Docker-backed sandbox implementation with strict isolation controls.

Enforces:
- Health checks for Docker CLI and daemon reachability.
- Safe argument arrays with shell=False.
- Explicit container boundaries (--network none, --read-only, --cap-drop ALL).
- Non-root user execution, memory, CPU, and PID limits.
- Guaranteed container cleanup in finally blocks.
- Output bounding and secret redaction.
- Fail-closed refusal: never falls back to host execution if Docker is unavailable.
"""

from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess

import time
from typing import Any, Dict, List, Optional, Tuple

from forge.config import redact_secrets, get_settings
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    SandboxRequest,
    SandboxResult,
)
from forge.sandbox.policy import validate_sandbox_request, PolicyViolationError


_DEFAULT_BINARY = object()


class DockerSandbox:
    """Docker container execution adapter."""

    def __init__(
        self,
        docker_image: Optional[str] = None,
        docker_binary: Any = _DEFAULT_BINARY,
    ):
        settings = get_settings()
        raw_image = docker_image or settings.docker_image
        self.docker_image = self.validate_image(raw_image)
        if docker_binary is _DEFAULT_BINARY:
            self.docker_binary = shutil.which("docker")
        else:
            self.docker_binary = docker_binary

    @staticmethod
    def validate_image(image_str: str) -> str:
        """Validate container image string against command-injection and malformed characters.

        Accepts standard repository:tag or repository@sha256:digest formats.
        Rejects command flags, shell metacharacters, whitespace, and empty strings.
        """
        if not image_str or not image_str.strip():
            raise ValueError("Docker image identifier cannot be empty")

        cleaned = image_str.strip()
        # Disallow CLI flags
        if cleaned.startswith("-"):
            raise ValueError(f"Docker image cannot start with a flag: {cleaned}")

        # Strict regex matching standard Docker image references:
        # e.g. python:3.11-slim, myregistry.azurecr.io/repo/img:tag, python@sha256:abcdef...
        pattern = re.compile(r"^[a-zA-Z0-9_.\-]+(/[a-zA-Z0-9_.\-]+)*(:[a-zA-Z0-9_.\-]+)?(@sha256:[a-fA-F0-9]{64})?$")
        if not pattern.match(cleaned):
            raise ValueError(f"Invalid or unsafe Docker image reference: {cleaned}")

        return cleaned


    def is_available(self) -> bool:
        """Return True only if Docker executable exists and daemon is reachable."""
        healthy, _ = self.check_health()
        return healthy

    def check_health(self) -> Tuple[bool, str]:
        """Verify Docker executable, daemon status, socket permissions, and local sandbox image availability."""
        if not self.docker_binary:
            return False, "Docker binary not found in PATH"

        try:
            res = subprocess.run(
                [self.docker_binary, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            if res.returncode != 0:
                err_msg = res.stderr.strip() or "Docker daemon not responding"
                low_err = err_msg.lower()
                if "permission denied" in low_err:
                    return False, f"Docker socket permission denied: {err_msg}"
                elif "cannot connect" in low_err or "is the docker daemon running" in low_err:
                    return False, f"Docker daemon not running: {err_msg}"
                return False, f"Docker daemon unavailable: {err_msg}"
            version = res.stdout.strip()

            # Check if sandbox image is cached locally in Docker daemon
            try:
                img_res = subprocess.run(
                    [self.docker_binary, "image", "inspect", self.docker_image],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                if img_res.returncode != 0:
                    return False, f"Docker daemon reachable (Version: {version}), but sandbox image '{self.docker_image}' is not in local cache (run 'docker pull {self.docker_image}')"
            except Exception:
                pass

            return True, f"Docker daemon reachable (Version: {version}, Image: {self.docker_image})"
        except subprocess.TimeoutExpired:
            return False, "Docker info check timed out"
        except Exception as e:
            return False, f"Docker health check failed: {e}"

    def build_docker_arguments(
        self,
        request: SandboxRequest,
        container_name: str,
    ) -> List[str]:
        """Construct the isolated Docker container run argument vector."""
        workspace_resolved = request.workspace_path.resolve().as_posix()
        workdir_in_container = f"/workspace/{request.relative_cwd}".rstrip("/")

        args = [
            self.docker_binary,
            "run",
            "--name", container_name,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "1000:1000",
            "--pids-limit", str(request.limits.pids_limit),
            "--memory", request.limits.memory_limit,
            "--cpus", str(request.limits.cpu_quota),
            "--mount", f"type=bind,source={workspace_resolved},target=/workspace,readonly",
            "--mount", "type=tmpfs,destination=/tmp,tmpfs-size=64M,tmpfs-mode=1777",
            "-w", workdir_in_container,
        ]

        # Explicitly forward approved environment variables
        for k, v in sorted(request.env_vars.items()):
            args.extend(["-e", f"{k}={v}"])

        # Target image and command
        args.append(self.docker_image)
        args.extend(request.command)

        return args

    def _cleanup_container(self, container_name: str) -> None:
        """Guaranteed cleanup of the specific container created for this request."""
        if not self.docker_binary or not container_name:
            return
        try:
            subprocess.run(
                [self.docker_binary, "rm", "-f", container_name],
                capture_output=True,
                timeout=10.0,
            )
        except Exception:
            pass

    def run(self, request: SandboxRequest) -> SandboxResult:
        """Execute request inside Docker container or fail closed if unavailable."""
        started_at = datetime.now(timezone.utc)
        start_mono = time.monotonic()

        # 1. Policy validation
        try:
            validate_sandbox_request(request)
        except PolicyViolationError as e:
            finished_at = datetime.now(timezone.utc)
            duration_ms = (time.monotonic() - start_mono) * 1000
            return SandboxResult(
                request_id=request.request_id,
                run_id=request.run_id,
                status=ExecutionStatus.REJECTED,
                exit_code=None,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                isolation_established=False,
                failure_classification=FailureClassification.POLICY_VIOLATION,
                error_message=str(e),
                runtime_metadata={"policy_enforced": True},
            )

        # 2. Availability / Isolation check
        is_healthy, health_msg = self.check_health()
        if not is_healthy:
            finished_at = datetime.now(timezone.utc)
            duration_ms = (time.monotonic() - start_mono) * 1000
            return SandboxResult(
                request_id=request.request_id,
                run_id=request.run_id,
                status=ExecutionStatus.UNAVAILABLE,
                exit_code=None,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                isolation_established=False,
                failure_classification=FailureClassification.RUNTIME_UNAVAILABLE,
                error_message=(
                    f"Safe sandbox execution unavailable: {health_msg}. "
                    "In compliance with AGENTS.md rules, repository code execution is refused on host."
                ),
                runtime_metadata={"docker_available": False, "health_message": health_msg},
            )

        # 3. Execute in container
        container_name = f"forge_{request.request_id}"
        cmd_args = self.build_docker_arguments(request, container_name)

        timed_out = False
        stdout_truncated = False
        stderr_truncated = False
        exit_code: Optional[int] = None
        stdout_text = ""
        stderr_text = ""
        error_msg: Optional[str] = None
        status = ExecutionStatus.SUCCEEDED
        failure_class = FailureClassification.NONE

        try:
            proc = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )

            try:
                raw_out, raw_err = proc.communicate(timeout=request.limits.timeout_seconds)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                # Terminate host subprocess and kill container
                try:
                    proc.kill()
                except Exception:
                    pass
                self._cleanup_container(container_name)
                try:
                    raw_out, raw_err = proc.communicate(timeout=5.0)
                except Exception:
                    raw_out, raw_err = b"", b""
                status = ExecutionStatus.TIMED_OUT
                failure_class = FailureClassification.TIMEOUT
                error_msg = f"Execution exceeded timeout limit of {request.limits.timeout_seconds}s"


            # Bound and sanitize stdout with head/tail preservation
            max_out = request.limits.max_stdout_bytes
            if len(raw_out) > max_out:
                stdout_truncated = True
                half = max_out // 2
                omitted = len(raw_out) - max_out
                head_str = raw_out[:half].decode("utf-8", errors="replace")
                tail_str = raw_out[-half:].decode("utf-8", errors="replace")
                stdout_text = f"{head_str}\n\n[TRUNCATED: Output byte limit reached; {omitted} bytes omitted]\n\n{tail_str}"
            else:
                stdout_text = raw_out.decode("utf-8", errors="replace")

            # Bound and sanitize stderr with head/tail preservation
            max_err = request.limits.max_stderr_bytes
            if len(raw_err) > max_err:
                stderr_truncated = True
                half = max_err // 2
                omitted = len(raw_err) - max_err
                head_str = raw_err[:half].decode("utf-8", errors="replace")
                tail_str = raw_err[-half:].decode("utf-8", errors="replace")
                stderr_text = f"{head_str}\n\n[TRUNCATED: Output byte limit reached; {omitted} bytes omitted]\n\n{tail_str}"
            else:
                stderr_text = raw_err.decode("utf-8", errors="replace")

            stdout_text = redact_secrets(stdout_text)
            stderr_text = redact_secrets(stderr_text)

            if not timed_out:
                if exit_code == 0:
                    status = ExecutionStatus.SUCCEEDED
                    failure_class = FailureClassification.NONE
                else:
                    status = ExecutionStatus.FAILED
                    failure_class = FailureClassification.COMMAND_FAILED
                    error_msg = f"Command failed with exit code {exit_code}"

        except Exception as exc:
            status = ExecutionStatus.RUNTIME_ERROR
            failure_class = FailureClassification.CONTAINER_ERROR
            error_msg = f"Container execution error: {exc}"
        finally:
            self._cleanup_container(container_name)

        finished_at = datetime.now(timezone.utc)
        duration_ms = (time.monotonic() - start_mono) * 1000

        return SandboxResult(
            request_id=request.request_id,
            run_id=request.run_id,
            status=status,
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            isolation_established=True,
            failure_classification=failure_class,
            error_message=error_msg,
            runtime_metadata={
                "image": self.docker_image,
                "container_name": container_name,
                "command": request.command,
            },
        )
