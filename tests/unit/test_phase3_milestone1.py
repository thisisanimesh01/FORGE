"""Comprehensive unit and security tests for Phase 3, Milestone 1: Reproduction contracts."""

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.sandbox.contracts import (
    ReproductionOutcome,
    ExecutionEnvironmentSpec,
    ReproductionCommand,
    ReproductionRequest,
    ReproductionResult,
)
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import SandboxResult, ExecutionStatus
from forge.sandbox.runner import ReproductionHarness


runner = CliRunner()


def test_reproduction_command_valid():
    """Valid non-shell command parses correctly."""
    cmd = ReproductionCommand(
        args=["pytest", "-q", "tests/unit/test_calc.py"],
        relative_cwd="tests",
        expected_exit_codes=[1],
        expected_output_patterns=[r"FAILED"],
    )
    assert cmd.args == ["pytest", "-q", "tests/unit/test_calc.py"]
    assert cmd.relative_cwd == "tests"


def test_reproduction_command_rejects_empty():
    """Empty args array must be rejected."""
    with pytest.raises(ValueError, match="cannot be empty"):
        ReproductionCommand(args=[])


def test_reproduction_command_rejects_forbidden_executable():
    """Shell binaries like bash or sh are forbidden."""
    with pytest.raises(ValueError, match="explicitly forbidden"):
        ReproductionCommand(args=["bash", "-c", "pytest"])

    with pytest.raises(ValueError, match="explicitly forbidden"):
        ReproductionCommand(args=["sh", "script.sh"])


def test_reproduction_command_rejects_unapproved_executable():
    """Arbitrary executables outside approved list are rejected."""
    with pytest.raises(ValueError, match="not in approved executables list"):
        ReproductionCommand(args=["gcc", "main.c"])


def test_reproduction_command_rejects_shell_characters():
    """Arguments containing shell operators or chaining are rejected."""
    with pytest.raises(ValueError, match="forbidden shell characters"):
        ReproductionCommand(args=["pytest", ";", "rm", "-rf"])

    with pytest.raises(ValueError, match="forbidden shell characters"):
        ReproductionCommand(args=["pytest", "$(cat /etc/passwd)"])

    with pytest.raises(ValueError, match="forbidden shell characters"):
        ReproductionCommand(args=["pytest", "tests | tee output.txt"])


def test_reproduction_command_rejects_path_traversal_cwd():
    """CWD cannot be absolute or traverse outside workspace."""
    with pytest.raises(ValueError, match="contained within the repository"):
        ReproductionCommand(args=["pytest"], relative_cwd="/etc")

    with pytest.raises(ValueError, match="contained within the repository"):
        ReproductionCommand(args=["pytest"], relative_cwd="../outside")


def test_environment_spec_valid():
    """Environment spec accepts valid boundaries."""
    env = ExecutionEnvironmentSpec(
        docker_image="python:3.11-slim",
        timeout_seconds=30.0,
        memory_limit="256m",
        cpu_quota=1.5,
        pids_limit=32,
    )
    assert env.timeout_seconds == 30.0
    assert env.memory_limit == "256m"


def test_environment_spec_rejects_network_enabled():
    """Network access must remain disabled."""
    with pytest.raises(ValueError, match="network_enabled must be False"):
        ExecutionEnvironmentSpec(network_enabled=True)


def test_environment_spec_rejects_invalid_memory():
    """Malformed memory strings are rejected."""
    with pytest.raises(ValueError, match="Invalid memory limit"):
        ExecutionEnvironmentSpec(memory_limit="invalid_mem")


def test_environment_spec_rejects_unsafe_image():
    """Unsafe or flag-injection images are rejected."""
    with pytest.raises(ValueError, match="cannot start with a flag"):
        ExecutionEnvironmentSpec(docker_image="--rm")

    with pytest.raises(ValueError, match="Invalid or unsafe Docker image"):
        ExecutionEnvironmentSpec(docker_image="python; rm -rf")


def test_reproduction_request_validation():
    """ReproductionRequest validates complete contract."""
    req = ReproductionRequest(
        request_id="repro_req_001",
        finding_id="find_auth_bypass_01",
        target_repo=".",
        hypothesis="Authentication check bypassed when token header is omitted",
        command=ReproductionCommand(
            args=["pytest", "tests/unit/test_auth.py", "-k", "test_bypass"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"AssertionError: Expected 401"],
        ),
    )
    assert req.request_id == "repro_req_001"
    assert req.environment.read_only_root is True
    assert req.environment.network_enabled is False


def test_harness_fails_closed_when_docker_unavailable(monkeypatch, tmp_path: Path):
    """When Docker health check fails, harness returns SKIPPED_SANDBOX_UNAVAILABLE without host execution."""
    class FakeUnavailableDocker:
        def check_health(self):
            return False, "Docker daemon not running"

    harness = ReproductionHarness(sandbox=FakeUnavailableDocker())

    req = ReproductionRequest(
        request_id="repro_req_test",
        finding_id="find_test_01",
        target_repo=str(tmp_path),
        hypothesis="Off by one error in loop",
        command=ReproductionCommand(
            args=["pytest", "tests/test_loop.py"],
            expected_exit_codes=[1],
        ),
    )

    result = harness.execute(req, tmp_path)
    assert result.outcome == ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE
    assert result.sandbox_available is False
    assert result.exit_code is None
    assert "fail-closed" in result.diagnostic_message


def test_harness_reproduces_when_docker_runs(monkeypatch, tmp_path: Path):
    """When Docker is simulated as running and defect reproduces, outcome is REPRODUCED."""
    class FakeRunningDocker:
        def check_health(self):
            return True, "Docker reachable"

        def run(self, sbx_req):
            return SandboxResult(
                request_id=sbx_req.request_id,
                run_id=sbx_req.run_id,
                status=ExecutionStatus.FAILED,
                exit_code=1,
                stdout="FAILED tests/test_calc.py::test_zero_div - ZeroDivisionError",
                stderr="",
            )

    harness = ReproductionHarness(sandbox=FakeRunningDocker())
    req = ReproductionRequest(
        request_id="repro_req_test",
        finding_id="find_calc_01",
        target_repo=str(tmp_path),
        hypothesis="Zero division crashes calculator",
        command=ReproductionCommand(
            args=["pytest", "tests/test_calc.py"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"ZeroDivisionError"],
        ),
    )

    result = harness.execute(req, tmp_path)
    assert result.outcome == ReproductionOutcome.REPRODUCED
    assert result.exit_code == 1
    assert len(result.matched_patterns) == 1


def test_harness_secret_redaction(monkeypatch, tmp_path: Path):
    """Secrets present in command output are sanitized in reproduction result."""
    class FakeDockerWithSecret:
        def check_health(self):
            return True, "Docker reachable"

        def run(self, sbx_req):
            return SandboxResult(
                request_id=sbx_req.request_id,
                run_id=sbx_req.run_id,
                status=ExecutionStatus.FAILED,
                exit_code=1,
                stdout="Error encountered with token sk-abcdef1234567890abcdef12 at line 10",
                stderr="",
            )

    harness = ReproductionHarness(sandbox=FakeDockerWithSecret())
    req = ReproductionRequest(
        request_id="repro_sec",
        finding_id="find_sec",
        target_repo=str(tmp_path),
        hypothesis="Sensitive token leak",
        command=ReproductionCommand(
            args=["pytest", "tests/test_token.py"],
            expected_exit_codes=[1],
        ),
    )

    result = harness.execute(req, tmp_path)
    assert "sk-" not in result.stdout or "REDACTED" in result.stdout


def test_cli_sandbox_check_valid_and_invalid(tmp_path: Path):
    """CLI forge sandbox check validates schema without execution."""
    valid_contract = {
        "request_id": "req_cli_01",
        "finding_id": "find_01",
        "target_repo": ".",
        "hypothesis": "Test hypothesis",
        "command": {
            "args": ["pytest", "tests/unit/test_math.py"],
            "expected_exit_codes": [1]
        }
    }
    p_valid = tmp_path / "contract_valid.json"
    p_valid.write_text(json.dumps(valid_contract), encoding="utf-8")

    res = runner.invoke(app, ["sandbox", "check", str(p_valid)])
    assert res.exit_code == 0
    assert "Contract 'req_cli_01' is valid" in res.output

    # Invalid contract (shell execution attempted)
    invalid_contract = {
        "request_id": "req_cli_02",
        "finding_id": "find_02",
        "target_repo": ".",
        "hypothesis": "Injected shell command",
        "command": {
            "args": ["bash", "-c", "echo 1"],
            "expected_exit_codes": [1]
        }
    }
    p_invalid = tmp_path / "contract_invalid.json"
    p_invalid.write_text(json.dumps(invalid_contract), encoding="utf-8")

    res_invalid = runner.invoke(app, ["sandbox", "check", str(p_invalid)])
    assert res_invalid.exit_code == 2
    assert "explicitly forbidden" in res_invalid.output


def test_cli_sandbox_reproduce_fails_closed_when_docker_unavailable(tmp_path: Path):
    """CLI forge sandbox reproduce returns clean fail-closed status when Docker is absent."""
    contract = {
        "request_id": "req_repro_cli",
        "finding_id": "find_cli",
        "target_repo": str(Path(".").resolve()),
        "hypothesis": "Verify fail closed",
        "command": {
            "args": ["pytest", "tests/unit/test_calc.py"],
            "expected_exit_codes": [1]
        }
    }
    p_contract = tmp_path / "reproduce_contract.json"
    p_contract.write_text(json.dumps(contract), encoding="utf-8")
    p_out = tmp_path / "result.json"

    res = runner.invoke(app, ["sandbox", "reproduce", str(p_contract), "-o", str(p_out)])
    assert res.exit_code == 0
    assert "SKIPPED_SANDBOX_UNAVAILABLE" in res.output
    assert p_out.exists()
    data = json.loads(p_out.read_text(encoding="utf-8"))
    assert data["outcome"] == "skipped_sandbox_unavailable"
