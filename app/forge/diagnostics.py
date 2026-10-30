"""Comprehensive operational diagnostics and live environment validation for FORGE.

Categorizes system dependencies, validates execution boundaries, detects misconfigurations,
and provides actionable remediation guidance without compromising security invariants.
"""

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

from forge.config import ForgeSettings, get_settings, redact_secrets
from forge.storage.database import SCHEMA_VERSION


class DiagnosticCategory(str, Enum):
    """Deterministic classification of diagnostic check outcomes."""
    AVAILABLE_HEALTHY = "available_healthy"
    MISSING_OPTIONAL = "missing_optional"
    MISCONFIGURED_ENV = "misconfigured_env"
    UNAVAILABLE_REQUIRED = "unavailable_required"
    PERMISSION_DENIED = "permission_denied"
    NETWORK_FAILURE = "network_failure"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    UNSAFE_CONFIGURATION = "unsafe_configuration"

    @property
    def label(self) -> str:
        """Formatted terminal badge for status display."""
        mapping = {
            self.AVAILABLE_HEALTHY: "[OK]",
            self.MISSING_OPTIONAL: "[OPTIONAL]",
            self.MISCONFIGURED_ENV: "[MISCONFIGURED]",
            self.UNAVAILABLE_REQUIRED: "[UNAVAILABLE]",
            self.PERMISSION_DENIED: "[PERMISSION DENIED]",
            self.NETWORK_FAILURE: "[CONNECTION FAILED]",
            self.UNSUPPORTED_RUNTIME: "[UNSUPPORTED]",
            self.UNSAFE_CONFIGURATION: "[UNSAFE]",
        }
        return mapping[self]

    @property
    def badge(self) -> str:
        """Alias for label."""
        return self.label


@dataclass
class DiagnosticCheck:
    """Detailed result of a single operational diagnostic check."""
    name: str
    category: DiagnosticCategory
    message: str
    is_healthy: bool = True
    details: Dict[str, Any] = field(default_factory=dict)
    actionable_remediation: Optional[str] = None
    remediation: Optional[str] = None

    def __post_init__(self) -> None:
        if self.remediation is not None and self.actionable_remediation is None:
            self.actionable_remediation = self.remediation
        elif self.actionable_remediation is not None and self.remediation is None:
            self.remediation = self.actionable_remediation
        if self.category not in (DiagnosticCategory.AVAILABLE_HEALTHY, DiagnosticCategory.MISSING_OPTIONAL):
            # Non-healthy categories default is_healthy to False unless explicitly overridden
            if self.is_healthy is True and self.category != DiagnosticCategory.AVAILABLE_HEALTHY:
                self.is_healthy = False


@dataclass
class SystemReadinessSummary:
    """Comprehensive readiness assessment across execution tiers."""
    checks: List[DiagnosticCheck]
    static_inspection_ready: bool
    live_execution_ready: bool
    local_inference_ready: bool
    overall_status: str
    summary_message: str

    @property
    def summary_text(self) -> str:
        """Alias for summary_message."""
        return self.summary_message


# =============================================================================
# Diagnostic Probes
# =============================================================================

def check_python_environment() -> DiagnosticCheck:
    """Validate host Python runtime version and architecture."""
    v = sys.version_info
    ver_str = f"{v.major}.{v.minor}.{v.micro}"
    exe = sys.executable

    if v.major < 3 or (v.major == 3 and v.minor < 11):
        return DiagnosticCheck(
            name="Python Runtime",
            category=DiagnosticCategory.UNSUPPORTED_RUNTIME,
            is_healthy=False,
            message=f"Python {ver_str} detected at {exe}. FORGE requires Python 3.11+.",
            details={"version": ver_str, "executable": exe, "major": v.major, "minor": v.minor},
            actionable_remediation="Install and activate Python 3.11 or higher in your virtual environment.",
        )

    return DiagnosticCheck(
        name="Python Runtime",
        category=DiagnosticCategory.AVAILABLE_HEALTHY,
        is_healthy=True,
        message=f"Python {ver_str} ({exe})",
        details={"version": ver_str, "executable": exe},
    )


def check_git_environment() -> DiagnosticCheck:
    """Verify git executable availability and basic execution."""
    git_path = shutil.which("git")
    if not git_path:
        return DiagnosticCheck(
            name="Git Executable",
            category=DiagnosticCategory.UNAVAILABLE_REQUIRED,
            is_healthy=False,
            message="Git executable not found in system PATH.",
            details={},
            actionable_remediation="Install Git via your system package manager (e.g. 'brew install git' or 'apt install git').",
        )

    try:
        res = subprocess.run([git_path, "--version"], capture_output=True, text=True, timeout=3.0)
        ver = res.stdout.strip() if res.returncode == 0 else "unknown"
        return DiagnosticCheck(
            name="Git Executable",
            category=DiagnosticCategory.AVAILABLE_HEALTHY,
            is_healthy=True,
            message=f"Git executable found: {git_path} ({ver})",
            details={"path": git_path, "version": ver},
        )
    except Exception as e:
        return DiagnosticCheck(
            name="Git Executable",
            category=DiagnosticCategory.MISCONFIGURED_ENV,
            is_healthy=False,
            message=f"Git executable at {git_path} failed to execute: {e}",
            details={"path": git_path, "error": str(e)},
            actionable_remediation="Verify that the git binary is executable and not corrupted.",
        )


def check_sqlite_database(db_path: Path) -> DiagnosticCheck:
    """Verify SQLite database path, file accessibility, schema version, and integrity."""
    resolved = db_path.resolve()
    parent = resolved.parent

    # 1. Check parent directory write permission
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return DiagnosticCheck(
                name="SQLite Database",
                category=DiagnosticCategory.PERMISSION_DENIED,
                is_healthy=False,
                message=f"Cannot create parent directory for SQLite store at {parent}: {e.strerror}",
                details={"db_path": str(resolved), "error": str(e)},
                actionable_remediation=f"Ensure write permissions exist for {parent} or set FORGE_DB_PATH to a writable path.",
            )
    elif not os.access(parent, os.W_OK):
        return DiagnosticCheck(
            name="SQLite Database",
            category=DiagnosticCategory.PERMISSION_DENIED,
            is_healthy=False,
            message=f"Parent directory {parent} is not writable by current user.",
            details={"db_path": str(resolved)},
            actionable_remediation=f"Grant write permissions to {parent} or update FORGE_DB_PATH in your environment.",
        )

    # 2. Check if file exists
    if not resolved.exists():
        return DiagnosticCheck(
            name="SQLite Database",
            category=DiagnosticCategory.MISSING_OPTIONAL,
            is_healthy=True,
            message=f"Database file not yet created at {resolved} (pending initialization).",
            details={"db_path": str(resolved), "initialized": False},
            actionable_remediation="Run 'forge db init' to initialize the persistent SQLite schema.",
        )

    # 3. Check readability and writeability of existing file
    if not os.access(resolved, os.R_OK | os.W_OK):
        return DiagnosticCheck(
            name="SQLite Database",
            category=DiagnosticCategory.PERMISSION_DENIED,
            is_healthy=False,
            message=f"Database file exists at {resolved} but lacks read/write permissions.",
            details={"db_path": str(resolved)},
            actionable_remediation=f"Run 'chmod 600 {resolved}' or ensure file ownership matches the current user.",
        )

    # 4. Verify SQLite schema version and table structure
    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=rw", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r["name"] for r in cur.fetchall()}
            has_schema = "schema_migrations" in tables and "engineering_workflows" in tables
            cur_ver = None
            if "schema_migrations" in tables:
                row = conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()
                cur_ver = row["version"] if row else None

            if not has_schema:
                return DiagnosticCheck(
                    name="SQLite Database",
                    category=DiagnosticCategory.MISCONFIGURED_ENV,
                    is_healthy=False,
                    message=f"Database file exists ({resolved.stat().st_size} bytes) but schema is uninitialized or incomplete.",
                    details={"db_path": str(resolved), "tables": list(tables)},
                    actionable_remediation="Run 'forge db init' to apply required schema migrations.",
                )

            return DiagnosticCheck(
                name="SQLite Database",
                category=DiagnosticCategory.AVAILABLE_HEALTHY,
                is_healthy=True,
                message=f"Database file exists ({resolved.stat().st_size} bytes, Schema Version: {cur_ver or SCHEMA_VERSION})",
                details={"db_path": str(resolved), "version": cur_ver, "tables_count": len(tables)},
            )
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return DiagnosticCheck(
                name="SQLite Database",
                category=DiagnosticCategory.MISCONFIGURED_ENV,
                is_healthy=False,
                message=f"Database at {resolved} is locked by another process: {e}",
                details={"db_path": str(resolved), "error": str(e)},
                actionable_remediation="Wait for concurrent processes to finish or release stale file locks.",
            )
        return DiagnosticCheck(
            name="SQLite Database",
            category=DiagnosticCategory.PERMISSION_DENIED,
            is_healthy=False,
            message=f"Failed opening database at {resolved}: {e}",
            details={"db_path": str(resolved), "error": str(e)},
            actionable_remediation=f"Verify permissions and file integrity for {resolved}.",
        )
    except Exception as e:
        return DiagnosticCheck(
            name="SQLite Database",
            category=DiagnosticCategory.MISCONFIGURED_ENV,
            is_healthy=False,
            message=f"SQLite database error at {resolved}: {e}",
            details={"db_path": str(resolved), "error": str(e)},
            actionable_remediation="Verify database file integrity.",
        )


def check_docker_environment(docker_image: str = "python:3.11-slim") -> DiagnosticCheck:
    """Validate Docker CLI, daemon connectivity, socket permissions, and local image availability."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return DiagnosticCheck(
            name="Docker Sandbox Runtime",
            category=DiagnosticCategory.UNAVAILABLE_REQUIRED,
            is_healthy=False,
            message="Docker binary not found in system PATH.",
            details={"docker_binary": None},
            actionable_remediation="Install Docker Desktop or Docker Engine to enable sandboxed reproduction and verification.",
        )

    # 1. Probe docker daemon info
    try:
        res = subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except subprocess.TimeoutExpired:
        return DiagnosticCheck(
            name="Docker Sandbox Runtime",
            category=DiagnosticCategory.NETWORK_FAILURE,
            is_healthy=False,
            message="Docker daemon health check timed out after 5.0 seconds.",
            details={"docker_binary": docker_bin},
            actionable_remediation="Docker daemon is hanging or unresponsive. Restart Docker Desktop or docker service.",
        )
    except Exception as e:
        return DiagnosticCheck(
            name="Docker Sandbox Runtime",
            category=DiagnosticCategory.MISCONFIGURED_ENV,
            is_healthy=False,
            message=f"Unexpected error executing docker info: {e}",
            details={"docker_binary": docker_bin, "error": str(e)},
            actionable_remediation="Verify docker executable permissions and system configuration.",
        )

    # 2. Analyze docker info returncode and stderr
    if res.returncode != 0:
        err = res.stderr.strip()
        low_err = err.lower()

        if "permission denied" in low_err:
            return DiagnosticCheck(
                name="Docker Sandbox Runtime",
                category=DiagnosticCategory.PERMISSION_DENIED,
                is_healthy=False,
                message=f"Docker socket permission denied: {err}",
                details={"docker_binary": docker_bin, "stderr": err},
                actionable_remediation="Ensure your user has access to Docker socket (e.g. 'sudo usermod -aG docker $USER' or check ~/.docker/run/docker.sock permissions).",
            )
        elif "cannot connect" in low_err or "is the docker daemon running" in low_err or "not responding" in low_err:
            return DiagnosticCheck(
                name="Docker Sandbox Runtime",
                category=DiagnosticCategory.UNAVAILABLE_REQUIRED,
                is_healthy=False,
                message="Docker daemon is not running or unreachable.",
                details={"docker_binary": docker_bin, "stderr": err},
                actionable_remediation="Start Docker Desktop or run 'sudo systemctl start docker'.",
            )
        else:
            return DiagnosticCheck(
                name="Docker Sandbox Runtime",
                category=DiagnosticCategory.UNAVAILABLE_REQUIRED,
                is_healthy=False,
                message=f"Docker daemon check failed: {err}",
                details={"docker_binary": docker_bin, "stderr": err},
                actionable_remediation="Inspect Docker daemon logs and verify daemon status.",
            )

    server_version = res.stdout.strip()

    # 3. Verify configured container image availability in local daemon cache
    try:
        img_res = subprocess.run(
            [docker_bin, "image", "inspect", docker_image],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        image_cached = (img_res.returncode == 0)
    except Exception:
        image_cached = False

    if not image_cached:
        return DiagnosticCheck(
            name="Docker Sandbox Runtime",
            category=DiagnosticCategory.MISCONFIGURED_ENV,
            is_healthy=False,
            message=f"Docker daemon reachable (Version: {server_version}), but sandbox image '{docker_image}' is not in local cache.",
            details={"server_version": server_version, "docker_image": docker_image, "image_cached": False},
            actionable_remediation=f"Pull the required container image: run 'docker pull {docker_image}'.",
        )

    return DiagnosticCheck(
        name="Docker Sandbox Runtime",
        category=DiagnosticCategory.AVAILABLE_HEALTHY,
        is_healthy=True,
        message=f"Docker daemon reachable (Version: {server_version}, Image: {docker_image} cached locally)",
        details={"server_version": server_version, "docker_image": docker_image, "image_cached": True},
    )


def check_llm_provider(settings: Optional[ForgeSettings] = None, timeout_seconds: float = 2.0) -> DiagnosticCheck:
    """Validate LLM provider configuration, credentials, local Ollama connectivity, and model availability."""
    cfg = settings or get_settings()

    # 1. Enforce cloud fallback forbidden invariant
    if cfg.allow_cloud_fallback:
        return DiagnosticCheck(
            name="LLM Provider Security",
            category=DiagnosticCategory.UNSAFE_CONFIGURATION,
            is_healthy=False,
            message="CRITICAL: FORGE_ALLOW_CLOUD_FALLBACK is enabled. Security policy strictly forbids automatic cloud fallback.",
            details={"allow_cloud_fallback": True},
            actionable_remediation="Set FORGE_ALLOW_CLOUD_FALLBACK=false in .env and environment.",
        )

    provider = (cfg.llm_provider or "fake").lower().strip().replace("-", "_")

    # 2. Fake Provider (offline mock)
    if provider == "fake":
        return DiagnosticCheck(
            name="LLM Provider (fake)",
            category=DiagnosticCategory.AVAILABLE_HEALTHY,
            is_healthy=True,
            message=f"Deterministic offline mock active (Model: {cfg.llm_model or 'forge-fake-model'}, no network/credentials required).",
            details={"provider": "fake", "model": cfg.llm_model or "forge-fake-model", "local": True},
        )

    # 3. Local Ollama Provider
    if provider == "ollama":
        base_url = cfg.ollama_base_url.rstrip("/")
        req_model = cfg.llm_model or "llama3"
        tags_url = f"{base_url}/api/tags"

        try:
            req = urllib.request.Request(tags_url, headers={"User-Agent": "FORGE-Doctor/0.1.0b1"})
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                if resp.status != 200:
                    return DiagnosticCheck(
                        name="LLM Provider (ollama)",
                        category=DiagnosticCategory.NETWORK_FAILURE,
                        is_healthy=False,
                        message=f"Ollama server at {base_url} returned HTTP {resp.status}.",
                        details={"base_url": base_url, "status_code": resp.status},
                        actionable_remediation=f"Verify Ollama server health at {base_url}.",
                    )
                data = json.loads(resp.read().decode("utf-8"))
                models = data.get("models", [])
                available_names = [m.get("name", "") for m in models if isinstance(m, dict)]
                available_tags = {n.split(":")[0] for n in available_names}

                # Check if requested model or tag matches
                model_found = any(
                    req_model == name or req_model == name.split(":")[0] or name.startswith(f"{req_model}:")
                    for name in available_names
                )

                if not model_found:
                    return DiagnosticCheck(
                        name="LLM Provider (ollama)",
                        category=DiagnosticCategory.MISCONFIGURED_ENV,
                        is_healthy=False,
                        message=f"Ollama reachable at {base_url}, but configured model '{req_model}' is not installed (found: {available_names or 'none'}).",
                        details={"base_url": base_url, "configured_model": req_model, "installed_models": available_names},
                        actionable_remediation=f"Run 'ollama pull {req_model}' or configure FORGE_LLM_MODEL to an installed model.",
                    )

                return DiagnosticCheck(
                    name="LLM Provider (ollama)",
                    category=DiagnosticCategory.AVAILABLE_HEALTHY,
                    is_healthy=True,
                    message=f"Ollama server reachable at {base_url} (Model '{req_model}' verified in local inventory).",
                    details={"base_url": base_url, "model": req_model, "installed_models": available_names},
                )
        except urllib.error.URLError as e:
            return DiagnosticCheck(
                name="LLM Provider (ollama)",
                category=DiagnosticCategory.NETWORK_FAILURE,
                is_healthy=False,
                message=f"Cannot connect to Ollama server at {base_url}: {e.reason}",
                details={"base_url": base_url, "error": str(e)},
                actionable_remediation="Start the local Ollama server ('ollama serve') or verify that OLLAMA_BASE_URL is reachable.",
            )
        except Exception as e:
            return DiagnosticCheck(
                name="LLM Provider (ollama)",
                category=DiagnosticCategory.NETWORK_FAILURE,
                is_healthy=False,
                message=f"Ollama connection check failed: {e}",
                details={"base_url": base_url, "error": str(e)},
                actionable_remediation="Check Ollama service status and connectivity.",
            )

    # 4. Cloud providers (groq, gemini, deepseek)
    key_map = {
        "groq": cfg.groq_api_key,
        "gemini": cfg.gemini_api_key,
        "deepseek": cfg.deepseek_api_key,
    }
    key_val = key_map.get(provider, "")

    if not key_val or not key_val.strip():
        env_var_name = f"{provider.upper()}_API_KEY"
        return DiagnosticCheck(
            name=f"LLM Provider ({provider})",
            category=DiagnosticCategory.MISCONFIGURED_ENV,
            is_healthy=False,
            message=f"Provider '{provider}' selected but credentials are not configured ({env_var_name} is empty).",
            details={"provider": provider, "configured": False},
            actionable_remediation=f"Add {env_var_name} to your .env file or switch to 'ollama' or 'fake'.",
        )

    return DiagnosticCheck(
        name=f"LLM Provider ({provider})",
        category=DiagnosticCategory.AVAILABLE_HEALTHY,
        is_healthy=True,
        message=f"Provider '{provider}' credentials configured (external network required for live requests).",
        details={"provider": provider, "configured": True, "requires_network": True},
    )


# =============================================================================
# Composite System Readiness Evaluation
# =============================================================================

def evaluate_system_readiness(checks: List[DiagnosticCheck]) -> SystemReadinessSummary:
    """Synthesize individual diagnostic checks into tiered operational readiness states."""
    check_dict = {c.name: c for c in checks}

    py_check = check_dict.get("Python Runtime")
    git_check = check_dict.get("Git Executable")
    db_check = check_dict.get("SQLite Database")
    docker_check = check_dict.get("Docker Sandbox Runtime")
    provider_checks = [c for c in checks if c.name.startswith("LLM Provider")]
    provider_check = provider_checks[0] if provider_checks else None

    # Static inspection readiness: Python >= 3.11 and Git OK
    static_ready = bool(
        py_check and py_check.is_healthy and
        git_check and git_check.is_healthy
    )

    # Live execution readiness: Docker runtime healthy with image cached
    live_exec_ready = bool(docker_check and docker_check.is_healthy)

    # Local inference readiness: Provider healthy
    inference_ready = bool(provider_check and provider_check.is_healthy)

    # Database ready
    db_ready = bool(db_check and db_check.is_healthy)

    # Determine overall status
    if static_ready and live_exec_ready and inference_ready and db_ready:
        overall = "FULLY_READY"
        summary_msg = "System is fully ready for live Supervised Beta operations."
    elif static_ready and db_ready:
        overall = "READY_FOR_STATIC_INSPECTION_ONLY"
        missing = []
        if not live_exec_ready:
            missing.append("Docker container execution")
        if not inference_ready:
            missing.append("LLM provider inference")
        summary_msg = (
            f"System is ready for static repository inspection ONLY. "
            f"Live execution capabilities disabled ({', '.join(missing)} unavailable). "
            f"FORGE fail-secure policy is active (host execution strictly refused)."
        )
    else:
        overall = "NOT_READY"
        summary_msg = "System is NOT ready. Essential dependencies (Python, Git, or Database storage) failed."

    return SystemReadinessSummary(
        checks=checks,
        static_inspection_ready=static_ready,
        live_execution_ready=live_exec_ready,
        local_inference_ready=inference_ready,
        overall_status=overall,
        summary_message=summary_msg,
    )


def run_system_diagnostics(settings: Optional[ForgeSettings] = None) -> SystemReadinessSummary:
    """Execute all system diagnostic probes and return comprehensive readiness assessment."""
    cfg = settings or get_settings()
    checks: List[DiagnosticCheck] = [
        check_python_environment(),
        check_git_environment(),
        check_sqlite_database(cfg.db_path),
        check_docker_environment(cfg.docker_image),
        check_llm_provider(cfg),
    ]
    return evaluate_system_readiness(checks)
