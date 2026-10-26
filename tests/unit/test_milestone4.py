"""Unit and security tests for Milestone 4: Sandbox Interface & Execution Policy."""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.schemas import EventType, EvidenceType
from forge.storage.store import ForgeStore
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
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.evidence import record_sandbox_result_in_store

runner = CliRunner()


# ---------------------------------------------------------------------------
# 1. Sandbox Models & Validation Tests
# ---------------------------------------------------------------------------

def test_sandbox_request_defaults(tmp_path: Path):
    """Test standard construction and frozen immutability of SandboxRequest."""
    req = SandboxRequest(
        request_id="req_001",
        run_id="run_001",
        workspace_path=tmp_path.resolve(),
        command=["python3", "-m", "unittest"],
    )
    assert req.relative_cwd == "."
    assert req.network_policy == NetworkPolicy.DISABLED
    assert req.limits.timeout_seconds == 60.0
    assert req.limits.memory_limit == "512m"
    assert req.read_only_workspace is True

    with pytest.raises(Exception):
        req.command = ["rm", "-rf", "/"]


def test_sandbox_result_defaults():
    """Test SandboxResult model fields."""
    res = SandboxResult(
        request_id="req_001",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="OK",
    )
    assert res.status == ExecutionStatus.SUCCEEDED
    assert res.failure_classification == FailureClassification.NONE
    assert res.isolation_established is False


# ---------------------------------------------------------------------------
# 2. Execution Policy Tests
# ---------------------------------------------------------------------------

def test_policy_allows_approved_executables(tmp_path: Path):
    """Approved binaries in allowlist pass policy validation."""
    for binary in ["python", "python3", "pytest", "flake8", "mypy", "ruff", "bandit"]:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=[binary, "--version"],
        )
        validate_sandbox_request(req)


def test_policy_rejects_forbidden_executables(tmp_path: Path):
    """Shell and dangerous binaries are strictly rejected."""
    for binary in ["sh", "bash", "zsh", "sudo", "curl", "wget", "nc"]:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=[binary, "something"],
        )
        with pytest.raises(PolicyViolationError, match="explicitly forbidden"):
            validate_sandbox_request(req)


def test_policy_rejects_unapproved_executables(tmp_path: Path):
    """Arbitrary executables not in allowlist are rejected."""
    req = SandboxRequest(
        request_id="req_test",
        run_id="run_test",
        workspace_path=tmp_path.resolve(),
        command=["gcc", "main.c"],
    )
    with pytest.raises(PolicyViolationError, match="not in the approved sandbox allowlist"):
        validate_sandbox_request(req)


def test_policy_rejects_empty_command(tmp_path: Path):
    """Empty command list is rejected."""
    req = SandboxRequest(
        request_id="req_test",
        run_id="run_test",
        workspace_path=tmp_path.resolve(),
        command=[],
    )
    with pytest.raises(PolicyViolationError, match="cannot be empty"):
        validate_sandbox_request(req)


def test_policy_rejects_shell_chaining_characters(tmp_path: Path):
    """Shell operators in command arguments are rejected."""
    bad_args = [
        ["pytest", "tests;rm -rf /"],
        ["pytest", "tests && cat /etc/passwd"],
        ["pytest", "tests | echo pwned"],
        ["pytest", "tests`id`"],
        ["pytest", "tests$(whoami)"],
        ["pytest", "tests\nls"],
        ["pytest", "test>output.txt"],
    ]
    for cmd in bad_args:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=cmd,
        )
        with pytest.raises(PolicyViolationError, match="forbidden shell character"):
            validate_sandbox_request(req)


def test_policy_rejects_inline_python_eval(tmp_path: Path):
    """python -c is explicitly forbidden."""
    req = SandboxRequest(
        request_id="req_test",
        run_id="run_test",
        workspace_path=tmp_path.resolve(),
        command=["python3", "-c", "import os; os.system('echo pwn')"],
    )
    with pytest.raises(PolicyViolationError, match="Inline python script execution"):
        validate_sandbox_request(req)


def test_policy_rejects_pip_install(tmp_path: Path):
    """pip install or download from untrusted repos is forbidden."""
    for subcmd in ["install", "download", "wheel", "uninstall"]:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=["pip", subcmd, "requests"],
        )
        with pytest.raises(PolicyViolationError, match="is forbidden in audit-only Phase 1"):
            validate_sandbox_request(req)


def test_policy_allows_pip_inspection(tmp_path: Path):
    """pip list/show/check for static inspection is permitted."""
    for subcmd in ["list", "check", "--version"]:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=["pip", subcmd],
        )
        validate_sandbox_request(req)


def test_policy_rejects_relative_cwd_traversal(tmp_path: Path):
    """Path traversal outside the workspace is rejected."""
    traversal_paths = ["../", "../../etc", "/etc", "subdir/../../outside"]
    for p in traversal_paths:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=["pytest"],
            relative_cwd=p,
        )
        with pytest.raises(PolicyViolationError, match="attempts path traversal"):
            validate_sandbox_request(req)


def test_policy_rejects_unapproved_env_vars(tmp_path: Path):
    """Environment variables outside allowlist are rejected."""
    req = SandboxRequest(
        request_id="req_test",
        run_id="run_test",
        workspace_path=tmp_path.resolve(),
        command=["pytest"],
        env_vars={"AWS_SECRET_ACCESS_KEY": "secret123", "PYTHONPATH": "src"},
    )
    with pytest.raises(PolicyViolationError, match="is not in approved allowlist"):
        validate_sandbox_request(req)


def test_policy_rejects_sensitive_host_paths():
    """Mounting root or home directory as workspace is rejected."""
    req = SandboxRequest(
        request_id="req_test",
        run_id="run_test",
        workspace_path=Path("/").resolve(),
        command=["pytest"],
    )
    with pytest.raises(PolicyViolationError, match="points to sensitive host directory"):
        validate_sandbox_request(req)


# ---------------------------------------------------------------------------
# 3. DockerSandbox Adapter & Fail-Secure Tests
# ---------------------------------------------------------------------------

def test_dockersandbox_health_check_unavailable():
    """When docker binary is not found, health check reports unavailable."""
    sandbox = DockerSandbox(docker_binary=None)
    available, msg = sandbox.check_health()
    assert available is False
    assert "not found" in msg.lower()


def test_dockersandbox_fails_closed_when_docker_missing(tmp_path: Path):
    """When Docker is missing, run() returns UNAVAILABLE without executing host code."""
    sandbox = DockerSandbox(docker_binary="/path/does/not/exist/docker")
    req = SandboxRequest(
        request_id="req_001",
        run_id="run_001",
        workspace_path=tmp_path.resolve(),
        command=["pytest"],
    )
    result = sandbox.run(req)
    assert result.status == ExecutionStatus.UNAVAILABLE
    assert result.isolation_established is False
    assert result.failure_classification == FailureClassification.RUNTIME_UNAVAILABLE
    assert "refused on host" in result.error_message


def test_dockersandbox_argument_construction(tmp_path: Path):
    """Verify exact Docker CLI flags construction (isolation, dropped caps, no-net, etc)."""
    sandbox = DockerSandbox(docker_binary="/usr/bin/docker", docker_image="python:3.11-slim")
    req = SandboxRequest(
        request_id="req_002",
        run_id="run_001",
        workspace_path=tmp_path.resolve(),
        command=["pytest", "-v"],
        env_vars={"PYTHONPATH": "src"},
    )
    args = sandbox.build_docker_arguments(req, "forge_req_002")

    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in args and args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert "--user" in args and args[args.index("--user") + 1] == "1000:1000"
    assert "python:3.11-slim" in args
    assert "pytest" in args
    assert "-v" in args
    assert "-e" in args and "PYTHONPATH=src" in args


def test_dockersandbox_mock_successful_execution(tmp_path: Path):
    """Simulate successful execution through Docker."""
    sandbox = DockerSandbox(docker_binary="/usr/bin/docker")

    with patch.object(sandbox, "check_health", return_value=(True, "Docker daemon reachable")):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"=== 5 passed ===", b"")
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):  # For cleanup container
                req = SandboxRequest(
                    request_id="req_003",
                    run_id="run_001",
                    workspace_path=tmp_path.resolve(),
                    command=["pytest"],
                )
                res = sandbox.run(req)

                assert res.status == ExecutionStatus.SUCCEEDED
                assert res.exit_code == 0
                assert "5 passed" in res.stdout
                assert res.isolation_established is True
                assert res.failure_classification == FailureClassification.NONE


def test_dockersandbox_mock_timeout_handling(tmp_path: Path):
    """Simulate timeout during container execution."""
    sandbox = DockerSandbox(docker_binary="/usr/bin/docker")

    with patch.object(sandbox, "check_health", return_value=(True, "Docker daemon reachable")):
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["docker"], timeout=5.0),
            (b"", b"Killed"),
        ]

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):
                req = SandboxRequest(
                    request_id="req_004",
                    run_id="run_001",
                    workspace_path=tmp_path.resolve(),
                    command=["pytest"],
                    limits=ExecutionLimits(timeout_seconds=5.0),
                )
                res = sandbox.run(req)

                assert res.status == ExecutionStatus.TIMED_OUT
                assert res.timed_out is True
                assert res.failure_classification == FailureClassification.TIMEOUT
                mock_proc.kill.assert_called_once()


def test_dockersandbox_output_bounding_and_truncation(tmp_path: Path):
    """Output bytes exceeding limits are truncated and marked."""
    sandbox = DockerSandbox(docker_binary="/usr/bin/docker")

    with patch.object(sandbox, "check_health", return_value=(True, "Docker daemon reachable")):
        mock_proc = MagicMock()
        huge_output = b"A" * 60_000
        mock_proc.communicate.return_value = (huge_output, b"")
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):
                req = SandboxRequest(
                    request_id="req_005",
                    run_id="run_001",
                    workspace_path=tmp_path.resolve(),
                    command=["pytest"],
                    limits=ExecutionLimits(max_stdout_bytes=1000),
                )
                res = sandbox.run(req)

                assert res.stdout_truncated is True
                assert "[TRUNCATED:" in res.stdout
                assert len(res.stdout) < 2000


def test_dockersandbox_secret_redaction_in_output(tmp_path: Path):
    """Secrets discovered in container output are automatically scrubbed."""
    sandbox = DockerSandbox(docker_binary="/usr/bin/docker")

    with patch.object(sandbox, "check_health", return_value=(True, "Docker daemon reachable")):
        mock_proc = MagicMock()
        output_with_secret = b"Error with token: sk-abcdef1234567890abcdef123456"
        mock_proc.communicate.return_value = (output_with_secret, b"")
        mock_proc.returncode = 1

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):
                req = SandboxRequest(
                    request_id="req_006",
                    run_id="run_001",
                    workspace_path=tmp_path.resolve(),
                    command=["pytest"],
                )
                res = sandbox.run(req)

                assert "sk-abcdef" not in res.stdout
                assert "[REDACTED_SECRET]" in res.stdout


# ---------------------------------------------------------------------------
# 4. Evidence and Event History Integration Tests
# ---------------------------------------------------------------------------

def test_record_sandbox_result_in_store(tmp_path: Path):
    """Sandbox execution outcomes are logged in SQLite store."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("https://github.com/example/repo.git", run_id="run_001")

    result = SandboxResult(
        request_id="req_001",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="PASSED",
        isolation_established=True,
    )

    ev = record_sandbox_result_in_store(result, store)
    assert ev is not None
    assert ev.evidence_type == EvidenceType.TEST_RESULT
    assert ev.provenance == "forge:sandbox"

    events = store.list_run_events("run_001")
    event_types = [e.event_type for e in events]
    assert EventType.SANDBOX_EXECUTION_COMPLETED in event_types


def test_record_sandbox_unavailable_no_evidence_fabricated(tmp_path: Path):
    """When sandbox is unavailable, SANDBOX_UNAVAILABLE event is recorded and NO evidence is fabricated."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("https://github.com/example/repo.git", run_id="run_001")

    result = SandboxResult(
        request_id="req_002",
        run_id="run_001",
        status=ExecutionStatus.UNAVAILABLE,
        isolation_established=False,
        failure_classification=FailureClassification.RUNTIME_UNAVAILABLE,
        error_message="Docker unavailable",
    )

    ev = record_sandbox_result_in_store(result, store)
    assert ev is None  # No fake evidence attached!

    events = store.list_run_events("run_001")
    event_types = [e.event_type for e in events]
    assert EventType.SANDBOX_UNAVAILABLE in event_types


# ---------------------------------------------------------------------------
# 5. CLI Tests
# ---------------------------------------------------------------------------

def test_cli_sandbox_doctor():
    """Test forge sandbox doctor output."""
    result = runner.invoke(app, ["sandbox", "doctor"])
    assert result.exit_code == 0
    assert "FORGE Sandbox Diagnostics" in result.output
    assert "Configured image:" in result.output
    assert "Network policy:" in result.output
    assert "refuses to run repository code directly on host" in result.output


def test_cli_sandbox_doctor_unavailable(monkeypatch: pytest.MonkeyPatch):
    """Test forge sandbox doctor output when Docker daemon is unavailable."""
    monkeypatch.setattr("forge.sandbox.docker.shutil.which", lambda _: None)
    result = runner.invoke(app, ["sandbox", "doctor"])
    assert result.exit_code == 0
    assert "FORGE Sandbox Diagnostics" in result.output
    assert "[UNAVAILABLE]" in result.output
    assert "refuses to run repository code directly on host" in result.output


def test_cli_doctor_includes_sandbox_and_static_distinction():
    """Test updated forge doctor clearly distinguishes static vs sandbox vs runtime availability."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Static repository inspection: available" in result.output
    assert "Sandbox interface: available" in result.output


def test_docker_image_validation_safeguards():
    """Docker image configuration is strictly validated against injection."""
    # Valid image formats
    assert DockerSandbox.validate_image("python:3.11-slim") == "python:3.11-slim"
    assert DockerSandbox.validate_image("ghcr.io/org/repo:v1.0") == "ghcr.io/org/repo:v1.0"
    assert DockerSandbox.validate_image("python@sha256:" + "a" * 64) == "python@sha256:" + "a" * 64

    # Invalid / dangerous image formats
    with pytest.raises(ValueError, match="cannot start with a flag"):
        DockerSandbox.validate_image("--privileged")

    with pytest.raises(ValueError, match="cannot be empty"):
        DockerSandbox.validate_image("")

    with pytest.raises(ValueError, match="Invalid or unsafe Docker image"):
        DockerSandbox.validate_image("python;rm -rf /")

    with pytest.raises(ValueError, match="Invalid or unsafe Docker image"):
        DockerSandbox.validate_image("python 3.11")


def test_policy_rejects_binary_with_path_separators(tmp_path: Path):
    """Executables with slashes or path traversal are strictly rejected."""
    bad_binaries = ["/usr/bin/python", "./python", "../python", "bin/pytest"]
    for b in bad_binaries:
        req = SandboxRequest(
            request_id="req_test",
            run_id="run_test",
            workspace_path=tmp_path.resolve(),
            command=[b, "--version"],
        )
        with pytest.raises(PolicyViolationError, match="cannot contain directory separators"):
            validate_sandbox_request(req)


def test_record_sandbox_result_nonexistent_run(tmp_path: Path):
    """Recording sandbox result for a nonexistent run raises ValueError."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    result = SandboxResult(
        request_id="req_001",
        run_id="nonexistent_run",
        status=ExecutionStatus.SUCCEEDED,
    )
    with pytest.raises(ValueError, match="does not exist in store"):
        record_sandbox_result_in_store(result, store)


def test_record_sandbox_result_mismatched_finding(tmp_path: Path):
    """Recording sandbox result for finding belonging to another run raises ValueError."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target1", run_id="run_001")
    store.create_run("target2", run_id="run_002")
    store.create_finding(run_id="run_001", title="title", description="desc", finding_id="find_001")

    result = SandboxResult(
        request_id="req_001",
        run_id="run_002",  # Mismatched run
        status=ExecutionStatus.SUCCEEDED,
        isolation_established=True,
    )
    with pytest.raises(ValueError, match="does not match result run"):
        record_sandbox_result_in_store(result, store, finding_id="find_001")

