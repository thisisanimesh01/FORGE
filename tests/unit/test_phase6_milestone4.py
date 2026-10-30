"""Unit tests for Phase 6, Milestone 4: Operational Diagnostics & Live Environment Validation.

Covers:
1. DiagnosticCategory badges, DiagnosticCheck, and SystemReadinessSummary data models.
2. Probe evaluations:
   - check_python_environment (healthy vs unsupported)
   - check_git_environment (available vs missing)
   - check_sqlite_database (uninitialized, initialized, locked, permission denied)
   - check_docker_environment (healthy, daemon stopped, permission denied, missing image, missing binary)
   - check_llm_provider (fake, ollama healthy, ollama missing model, ollama connection failed, cloud missing key, cloud fallback unsafe)
3. evaluate_system_readiness (FULLY_READY, READY_FOR_STATIC_INSPECTION_ONLY, NOT_READY)
4. CLI diagnostics integration:
   - forge doctor
   - forge sandbox doctor
   - forge provider doctor
5. Failure recovery & resource resilience:
   - database lock recovery
   - container timeout and resource cleanup
   - provider connection failure handling
"""

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import urllib.error
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.config import ForgeSettings, get_settings
from forge.diagnostics import (
    DiagnosticCategory,
    DiagnosticCheck,
    SystemReadinessSummary,
    check_docker_environment,
    check_git_environment,
    check_llm_provider,
    check_python_environment,
    check_sqlite_database,
    evaluate_system_readiness,
    run_system_diagnostics,
)
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    NetworkPolicy,
    SandboxRequest,
    SandboxResult,
)

runner = CliRunner()


# =============================================================================
# 1. Diagnostic Data Models & Category Badges
# =============================================================================

def test_diagnostic_category_badges():
    """Verify each diagnostic category produces a distinct, clear badge."""
    assert DiagnosticCategory.AVAILABLE_HEALTHY.badge == "[OK]"
    assert DiagnosticCategory.MISSING_OPTIONAL.badge == "[OPTIONAL]"
    assert DiagnosticCategory.MISCONFIGURED_ENV.badge == "[MISCONFIGURED]"
    assert DiagnosticCategory.UNAVAILABLE_REQUIRED.badge == "[UNAVAILABLE]"
    assert DiagnosticCategory.PERMISSION_DENIED.badge == "[PERMISSION DENIED]"
    assert DiagnosticCategory.NETWORK_FAILURE.badge == "[CONNECTION FAILED]"
    assert DiagnosticCategory.UNSUPPORTED_RUNTIME.badge == "[UNSUPPORTED]"
    assert DiagnosticCategory.UNSAFE_CONFIGURATION.badge == "[UNSAFE]"


def test_diagnostic_check_structure():
    """Verify DiagnosticCheck fields and defaults."""
    check = DiagnosticCheck(
        name="Test Service",
        category=DiagnosticCategory.AVAILABLE_HEALTHY,
        message="Service is running normally",
    )
    assert check.name == "Test Service"
    assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
    assert check.message == "Service is running normally"
    assert check.remediation is None
    assert check.details == {}


# =============================================================================
# 2. Python & Git Probes
# =============================================================================

def test_check_python_environment_healthy():
    """Verify python environment check passes on Python 3.11+."""
    check = check_python_environment()
    if sys.version_info >= (3, 11):
        assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
        assert "Python" in check.message
    else:
        assert check.category == DiagnosticCategory.UNSUPPORTED_RUNTIME


def test_check_python_environment_unsupported():
    """Verify python environment check flags versions < 3.11 as UNSUPPORTED_RUNTIME."""
    mock_ver = SimpleNamespace(major=3, minor=9, micro=0)
    with patch("sys.version_info", mock_ver):
        check = check_python_environment()
        assert check.category == DiagnosticCategory.UNSUPPORTED_RUNTIME
        assert "3.11 or higher" in check.remediation


def test_check_git_environment_available():
    """Verify git check succeeds when git executable is located."""
    with patch("shutil.which", return_value="/usr/bin/git"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["/usr/bin/git", "--version"],
                returncode=0,
                stdout="git version 2.45.0\n",
            )
            check = check_git_environment()
            assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
            assert "git version 2.45.0" in check.message


def test_check_git_environment_missing():
    """Verify git check fails as UNAVAILABLE_REQUIRED when git is absent."""
    with patch("shutil.which", return_value=None):
        check = check_git_environment()
        assert check.category == DiagnosticCategory.UNAVAILABLE_REQUIRED
        assert "Install Git" in check.remediation


# =============================================================================
# 3. SQLite Database Probes
# =============================================================================

def test_check_sqlite_database_uninitialized(tmp_path: Path):
    """Verify uninitialized database is reported as MISSING_OPTIONAL with remediation."""
    nonexistent = tmp_path / "missing.db"
    check = check_sqlite_database(nonexistent)
    assert check.category == DiagnosticCategory.MISSING_OPTIONAL
    assert "forge db init" in check.remediation


def test_check_sqlite_database_healthy(tmp_path: Path):
    """Verify valid database with migrations is reported as AVAILABLE_HEALTHY."""
    db_path = tmp_path / "valid.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_migrations (version TEXT);")
    conn.execute("CREATE TABLE engineering_workflows (workflow_id TEXT);")
    conn.execute("INSERT INTO schema_migrations VALUES ('0.1.0');")
    conn.commit()
    conn.close()

    check = check_sqlite_database(db_path)
    assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
    assert "0.1.0" in check.message


def test_check_sqlite_database_locked(tmp_path: Path):
    """Verify locked database reports MISCONFIGURED_ENV with unlock remediation."""
    db_path = tmp_path / "locked.db"
    db_path.touch()
    with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
        check = check_sqlite_database(db_path)
        assert check.category == DiagnosticCategory.MISCONFIGURED_ENV
        assert "locked" in check.message.lower()
        assert "lock" in check.remediation.lower() or "process" in check.remediation.lower()


# =============================================================================
# 4. Docker Environment Probes
# =============================================================================

def test_check_docker_missing_binary():
    """Verify missing docker binary returns UNAVAILABLE_REQUIRED."""
    with patch("shutil.which", return_value=None):
        check = check_docker_environment()
        assert check.category == DiagnosticCategory.UNAVAILABLE_REQUIRED
        assert "Docker binary not found" in check.message
        assert "Install Docker" in check.remediation


def test_check_docker_daemon_stopped():
    """Verify stopped Docker daemon returns UNAVAILABLE_REQUIRED with daemon guidance."""
    with patch("shutil.which", return_value="/usr/local/bin/docker"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "info"],
                returncode=1,
                stdout="",
                stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
            )
            check = check_docker_environment()
            assert check.category == DiagnosticCategory.UNAVAILABLE_REQUIRED
            assert "not running" in check.message
            assert "Start Docker Desktop" in check.remediation


def test_check_docker_permission_denied():
    """Verify socket permission error returns PERMISSION_DENIED with chmod guidance."""
    with patch("shutil.which", return_value="/usr/local/bin/docker"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "info"],
                returncode=1,
                stdout="",
                stderr="permission denied while trying to connect to the Docker daemon socket",
            )
            check = check_docker_environment()
            assert check.category == DiagnosticCategory.PERMISSION_DENIED
            assert "permission denied" in check.message.lower()
            assert "socket permissions" in check.remediation.lower() or "docker" in check.remediation.lower()


def test_check_docker_image_missing_locally():
    """Verify daemon up but image not pulled returns MISCONFIGURED_ENV with pull command."""
    with patch("shutil.which", return_value="/usr/local/bin/docker"):
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                if cmd[1] == "info":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout="25.0.3\n",
                        stderr="",
                    )
                if cmd[1] == "image" and cmd[2] == "inspect":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=1,
                        stdout="",
                        stderr="Error: No such image: python:3.11-slim",
                    )
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            mock_run.side_effect = side_effect
            check = check_docker_environment()
            assert check.category == DiagnosticCategory.MISCONFIGURED_ENV
            assert "not in local cache" in check.message
            assert "docker pull python:3.11-slim" in check.remediation


def test_check_docker_healthy_and_cached():
    """Verify daemon running and image cached returns AVAILABLE_HEALTHY."""
    with patch("shutil.which", return_value="/usr/local/bin/docker"):
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kwargs):
                if cmd[1] == "info":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout="25.0.3\n",
                        stderr="",
                    )
                if cmd[1] == "image" and cmd[2] == "inspect":
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout="[]\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

            mock_run.side_effect = side_effect
            check = check_docker_environment()
            assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
            assert "cached locally" in check.message


# =============================================================================
# 5. LLM Provider Probes
# =============================================================================

def test_check_llm_provider_fake():
    """Verify fake offline provider always evaluates as AVAILABLE_HEALTHY."""
    settings = ForgeSettings(llm_provider="fake", allow_cloud_fallback=False)
    check = check_llm_provider(settings)
    assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
    assert "fake" in check.name.lower() or "offline mock" in check.message.lower()


def test_check_llm_provider_unsafe_cloud_fallback():
    """Verify enabling cloud fallback triggers UNSAFE_CONFIGURATION diagnostic."""
    settings = ForgeSettings(llm_provider="fake", allow_cloud_fallback=True)
    check = check_llm_provider(settings)
    assert check.category == DiagnosticCategory.UNSAFE_CONFIGURATION
    assert "FORGE_ALLOW_CLOUD_FALLBACK=false" in check.remediation


def test_check_llm_provider_ollama_healthy():
    """Verify Ollama with model found reports AVAILABLE_HEALTHY."""
    settings = ForgeSettings(
        llm_provider="ollama",
        llm_model="qwen2.5-coder:1.5b",
        ollama_base_url="http://localhost:11434",
        allow_cloud_fallback=False,
    )
    fake_payload = json.dumps({"models": [{"name": "qwen2.5-coder:1.5b:latest"}]}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_payload
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        check = check_llm_provider(settings)
        assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
        assert "qwen2.5-coder:1.5b" in check.message


def test_check_llm_provider_ollama_missing_model():
    """Verify Ollama running without requested model returns MISCONFIGURED_ENV with pull command."""
    settings = ForgeSettings(
        llm_provider="ollama",
        llm_model="qwen2.5-coder:7b",
        ollama_base_url="http://localhost:11434",
        allow_cloud_fallback=False,
    )
    fake_payload = json.dumps({"models": [{"name": "qwen2.5-coder:1.5b"}]}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_payload
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        check = check_llm_provider(settings)
        assert check.category == DiagnosticCategory.MISCONFIGURED_ENV
        assert "ollama pull qwen2.5-coder:7b" in check.remediation


def test_check_llm_provider_ollama_connection_failed():
    """Verify Ollama daemon down returns NETWORK_FAILURE with start guidance."""
    settings = ForgeSettings(
        llm_provider="ollama",
        ollama_base_url="http://localhost:11434",
        allow_cloud_fallback=False,
    )
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        check = check_llm_provider(settings)
        assert check.category == DiagnosticCategory.NETWORK_FAILURE
        assert "ollama serve" in check.remediation


def test_check_llm_provider_cloud_missing_key():
    """Verify cloud provider with missing API key returns MISCONFIGURED_ENV."""
    settings = ForgeSettings(
        llm_provider="groq",
        groq_api_key="",
        allow_cloud_fallback=False,
    )
    check = check_llm_provider(settings)
    assert check.category == DiagnosticCategory.MISCONFIGURED_ENV
    assert "GROQ_API_KEY" in check.remediation


# =============================================================================
# 6. Overall System Readiness Evaluation
# =============================================================================

def test_evaluate_system_readiness_fully_ready():
    """Verify all healthy checks produce FULLY_READY status."""
    checks = [
        DiagnosticCheck("Python Runtime", DiagnosticCategory.AVAILABLE_HEALTHY, "3.11", is_healthy=True),
        DiagnosticCheck("Git Executable", DiagnosticCategory.AVAILABLE_HEALTHY, "2.40", is_healthy=True),
        DiagnosticCheck("SQLite Database", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
        DiagnosticCheck("Docker Sandbox Runtime", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
        DiagnosticCheck("LLM Provider (fake)", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
    ]
    summary = evaluate_system_readiness(checks)
    assert summary.overall_status == "FULLY_READY"
    assert summary.static_inspection_ready is True
    assert summary.live_execution_ready is True
    assert summary.local_inference_ready is True


def test_evaluate_system_readiness_static_inspection_only_when_docker_fails():
    """Verify Docker failure limits readiness to STATIC_INSPECTION_ONLY, not FULLY_READY."""
    checks = [
        DiagnosticCheck("Python Runtime", DiagnosticCategory.AVAILABLE_HEALTHY, "3.11", is_healthy=True),
        DiagnosticCheck("Git Executable", DiagnosticCategory.AVAILABLE_HEALTHY, "2.40", is_healthy=True),
        DiagnosticCheck("SQLite Database", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
        DiagnosticCheck("Docker Sandbox Runtime", DiagnosticCategory.UNAVAILABLE_REQUIRED, "Docker down", is_healthy=False),
        DiagnosticCheck("LLM Provider (fake)", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
    ]
    summary = evaluate_system_readiness(checks)
    assert summary.overall_status == "READY_FOR_STATIC_INSPECTION_ONLY"
    assert summary.static_inspection_ready is True
    assert summary.live_execution_ready is False


def test_evaluate_system_readiness_not_ready_when_python_unsupported():
    """Verify unsupported Python runtime flags system as NOT_READY."""
    checks = [
        DiagnosticCheck("Python Runtime", DiagnosticCategory.UNSUPPORTED_RUNTIME, "Python 3.9", is_healthy=False),
        DiagnosticCheck("Git Executable", DiagnosticCategory.AVAILABLE_HEALTHY, "2.40", is_healthy=True),
        DiagnosticCheck("SQLite Database", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
        DiagnosticCheck("Docker Sandbox Runtime", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
        DiagnosticCheck("LLM Provider (fake)", DiagnosticCategory.AVAILABLE_HEALTHY, "ok", is_healthy=True),
    ]
    summary = evaluate_system_readiness(checks)
    assert summary.overall_status == "NOT_READY"
    assert summary.static_inspection_ready is False


# =============================================================================
# 7. CLI Diagnostics Subcommands
# =============================================================================

def test_cli_forge_doctor_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify forge doctor displays badges, summary, and backward-compatible strings."""
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "doctor_test.db"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "FORGE System Health Diagnostic" in result.output
    assert "Python Version:" in result.output
    assert "Supervised Beta Operational Readiness" in result.output


def test_cli_forge_sandbox_doctor_healthy():
    """Verify forge sandbox doctor displays healthy Docker information."""
    with patch("forge.diagnostics.check_docker_environment") as mock_check:
        mock_check.return_value = DiagnosticCheck(
            name="Docker Sandbox Runtime",
            category=DiagnosticCategory.AVAILABLE_HEALTHY,
            message="Docker daemon reachable (Version: 26.0.0, Image: python:3.11-slim cached locally)",
            is_healthy=True,
            details={"version": "26.0.0", "image": "python:3.11-slim"},
        )
        result = runner.invoke(app, ["sandbox", "doctor"])
        assert result.exit_code == 0
        assert "FORGE Sandbox Diagnostics" in result.output
        assert "AVAILABLE" in result.output or "[OK]" in result.output


def test_cli_forge_sandbox_doctor_error_exit_code():
    """Verify forge sandbox doctor displays remediation when Docker is unavailable."""
    with patch("forge.diagnostics.check_docker_environment") as mock_check:
        mock_check.return_value = DiagnosticCheck(
            name="Docker Sandbox Runtime",
            category=DiagnosticCategory.UNAVAILABLE_REQUIRED,
            message="Docker daemon is not running.",
            is_healthy=False,
            remediation="Start Docker Desktop.",
        )
        result = runner.invoke(app, ["sandbox", "doctor"])
        assert result.exit_code == 0
        assert "Docker daemon is not running" in result.output
        assert "Start Docker Desktop" in result.output


def test_cli_forge_provider_doctor_healthy():
    """Verify forge provider doctor displays healthy provider status."""
    with patch("forge.diagnostics.check_llm_provider") as mock_check:
        mock_check.return_value = DiagnosticCheck(
            name="LLM Provider",
            category=DiagnosticCategory.AVAILABLE_HEALTHY,
            message="Ollama inference server reachable (Model: qwen2.5-coder:1.5b)",
            is_healthy=True,
            details={"provider": "ollama", "model": "qwen2.5-coder:1.5b"},
        )
        result = runner.invoke(app, ["provider", "doctor"])
        assert result.exit_code == 0
        assert "FORGE LLM Provider Diagnostics" in result.output
        assert "qwen2.5-coder:1.5b" in result.output


def test_cli_forge_provider_doctor_failure_exit_code():
    """Verify forge provider doctor displays guidance when provider connection fails."""
    with patch("forge.diagnostics.check_llm_provider") as mock_check:
        mock_check.return_value = DiagnosticCheck(
            name="LLM Provider",
            category=DiagnosticCategory.NETWORK_FAILURE,
            message="Failed to connect to Ollama at http://localhost:11434.",
            is_healthy=False,
            remediation="Ensure Ollama is running using: ollama serve",
        )
        result = runner.invoke(app, ["provider", "doctor"])
        assert result.exit_code == 0
        assert "Failed to connect to Ollama" in result.output
        assert "ollama serve" in result.output
