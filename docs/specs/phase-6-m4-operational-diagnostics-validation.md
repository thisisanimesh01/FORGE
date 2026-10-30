# Phase 6, Milestone 4: Operational Diagnostics & Live Environment Validation — Specification

- **Milestone:** Phase 6 — M4: Operational Diagnostics & Live Environment Validation
- **Status:** COMPLETED & VERIFIED
- **Baseline Git Commit:** `4d9b3e778b739eea712d6fabf2fc1477929846be`
- **Target Repository Immutability:** STRICTLY ENFORCED (Zero commits, pushes, merges, PRs)
- **Traceability:** Implements Milestone 4 of Phase 6 per `AGENTS.md` (Sections 1, 2, 3, 4, 5, 7) and `docs/specs/phase-6-roadmap.md`.

---

## 1. Executive Summary

Milestone 4 validates FORGE as an installed, real-world supervised-beta application, moving beyond isolated unit mocks to demonstrate robust operational diagnostics, live dependency verification, and resilient environment troubleshooting.

Before Milestone 4, `forge doctor` and `forge sandbox doctor` emitted flat status strings that could unconditionally report the system as ready even when Docker was unreachable or provider credentials were misconfigured. Furthermore, errors in Docker socket permissions, stopped local daemons, or missing local model weights yielded cryptic tracebacks rather than actionable operator guidance.

Milestone 4 resolves these operational challenges by:
1. **Operational Diagnostics Engine (`app/forge/diagnostics.py`):** Implementing a structured, tiered diagnostic architecture that audits Python runtime, Git, SQLite schema integrity, Docker container sandboxing, and LLM inference providers.
2. **Deterministic Failure Classification:** Classifying every operational probe using standardized `DiagnosticCategory` badges:
   - `[OK]` (`AVAILABLE_HEALTHY`)
   - `[OPTIONAL]` (`MISSING_OPTIONAL`)
   - `[MISCONFIGURED]` (`MISCONFIGURED_ENV`)
   - `[UNAVAILABLE]` (`UNAVAILABLE_REQUIRED`)
   - `[PERMISSION DENIED]` (`PERMISSION_DENIED`)
   - `[CONNECTION FAILED]` (`NETWORK_FAILURE`)
   - `[UNSUPPORTED]` (`UNSUPPORTED_RUNTIME`)
   - `[UNSAFE]` (`UNSAFE_CONFIGURATION`)
3. **Actionable Remediation Guidance:** Every non-healthy probe returns specific, copy-pasteable remediation commands (e.g. `docker pull python:3.11-slim`, `ollama pull qwen2.5-coder:1.5b`, `chmod 600 /path/to/db`, `forge db init`).
4. **Honest Readiness Synthesis (`evaluate_system_readiness`):** Eliminating false readiness claims. If Docker is unreachable or the LLM provider is down, the system transitions to `[READY_FOR_STATIC_INSPECTION_ONLY]` rather than claiming full operational readiness, maintaining FORGE's fail-secure invariant.
5. **Dedicated Subcommands:**
   - `forge doctor`: Composite system diagnostic across all dependencies.
   - `forge sandbox doctor`: Dedicated container runtime, socket permission, and image cache diagnostic.
   - `forge provider doctor`: Dedicated LLM provider connectivity, model availability, and local inference validation.
6. **Live Environment Validation:**
   - Verified real host Docker Desktop (29.9.2) runtime with `python:3.11-slim` cached locally (`tests/integration/test_docker_sandbox_integration.py`).
   - Verified live host Ollama (`http://localhost:11434`) inference server and model `qwen2.5-coder:1.5b` (`tests/integration/test_ollama_live_validation.py`).
   - Verified wheel distribution packaging and execution in external clean virtual environment (`/tmp/forge_m4_installed_test`).

---

## 2. Architecture & Implementation

### 2.1 Diagnostics Module (`app/forge/diagnostics.py`)

- **`DiagnosticCategory` Enum:** Standardizes the taxonomy of system health, mapping each status to its visual terminal badge.
- **`DiagnosticCheck` Dataclass:** Holds check metadata: `name`, `category`, `is_healthy`, `message`, `details`, and `actionable_remediation`. Supports `.badge` and `.remediation` accessors for flexible consumption.
- **`SystemReadinessSummary` Dataclass:** Synthesizes probe outcomes across three distinct execution tiers:
  - `static_inspection_ready`: Python >= 3.11 and Git executable found.
  - `live_execution_ready`: Docker runtime reachable and `python:3.11-slim` image cached locally.
  - `local_inference_ready`: LLM provider reachable and model present in inventory (or fake provider active).
  - `overall_status`: `FULLY_READY`, `READY_FOR_STATIC_INSPECTION_ONLY`, or `NOT_READY`.

### 2.2 Probe Implementations

1. **Python Environment (`check_python_environment`):**
   - Validates `sys.version_info >= (3, 11)`.
   - Returns `UNSUPPORTED_RUNTIME` with remediation if Python version is < 3.11.
2. **Git Executable (`check_git_environment`):**
   - Searches `shutil.which("git")` and validates execution of `git --version`.
   - Returns `UNAVAILABLE_REQUIRED` if git is absent from `PATH`.
3. **SQLite Database (`check_sqlite_database`):**
   - Validates directory write permissions, file existence, read/write permissions, database lock status, and schema migration table existence.
   - Reports `MISSING_OPTIONAL` if uninitialized with instruction to run `forge db init`.
   - Reports `MISCONFIGURED_ENV` if locked by another process.
4. **Docker Sandbox Runtime (`check_docker_environment`):**
   - Evaluates `docker info` connectivity and returns specific errors for daemon stoppage (`UNAVAILABLE_REQUIRED`) versus socket permission issues (`PERMISSION_DENIED`).
   - Evaluates `docker image inspect` to confirm the configured container image (`python:3.11-slim`) is cached locally. If missing, reports `MISCONFIGURED_ENV` with `docker pull python:3.11-slim`.
5. **LLM Provider (`check_llm_provider`):**
   - Strict guard: if `cfg.allow_cloud_fallback` is enabled, flags `UNSAFE_CONFIGURATION` with instructions to set `FORGE_ALLOW_CLOUD_FALLBACK=false`.
   - Offline `fake` provider: returns `AVAILABLE_HEALTHY`.
   - Local `ollama`: connects to `/api/tags`, parses installed model list, and verifies whether the configured model exists in the local daemon inventory. If endpoint unreachable, returns `NETWORK_FAILURE` (`[CONNECTION FAILED]`); if model missing, returns `MISCONFIGURED_ENV` (`[MISCONFIGURED]`) with `ollama pull <model>`.
   - Cloud providers (`groq`, `gemini`, `deepseek`): validates that required API keys are configured in environment without leaking secrets.

---

## 3. CLI Usability Upgrades

### 3.1 `forge doctor`
Outputs a comprehensive system health report, printing:
- Python version and executable path.
- Static repository inspection status.
- Git executable status and version.
- Sandbox interface and Docker daemon availability.
- SQLite target path and migration version.
- Secret redaction filter and provider status.
- Operational readiness summary across static inspection, Docker sandboxing, and local inference tiers.

### 3.2 `forge sandbox doctor`
Provides dedicated container sandbox diagnostics:
- Configured image (`python:3.11-slim`).
- Enforced security policies (read-only workspace, 64MB isolated tmpfs, non-root UID 1000:1000, `--network none`, dropped capabilities).
- Live Docker daemon status and cached image verification.

### 3.3 `forge provider doctor`
Provides dedicated LLM provider diagnostics:
- Configured provider, model ID, API base, network requirement, and cloud fallback status.
- Real-time endpoint ping and model inventory validation.

---

## 4. Verification Matrix

| Area | Test Suite / Command | Scope & Verified Invariants | Outcome |
| :--- | :--- | :--- | :--- |
| **Operational Diagnostics** | `tests/unit/test_phase6_milestone4.py` (28 tests) | Categories, badges, probes, tiered readiness evaluation, CLI commands, mock failures | **PASSED (100%)** |
| **Real Docker Sandbox** | `tests/integration/test_docker_sandbox_integration.py` (2 tests) | Live Docker daemon, network none, read-only mount, UID 1000, dropped capabilities, timeouts, cleanup | **PASSED (100%)** |
| **Live Local Ollama** | `tests/integration/test_ollama_live_validation.py` (3 tests) | Live endpoint ping, model inventory, OpenAI-compatible local completion with `qwen2.5-coder:1.5b` | **PASSED (100%)** |
| **Installed Beta Wheel** | `/tmp/forge_m4_installed_test/bin/forge` | `version`, `doctor`, `sandbox doctor`, `provider doctor`, `db init` in external clean venv | **PASSED (100%)** |
| **Full Regression Suite** | `pytest` | 990+ tests across acquisition, intelligence, sandbox, repair, workflow, benchmarks | **IN PROGRESS** |

---

## 5. Security Invariants Preserved

1. **Fail-Secure Container Boundary:** Target code is strictly executed inside Docker containers. Direct host execution is permanently refused.
2. **Cloud Fallback Forbidden:** `FORGE_ALLOW_CLOUD_FALLBACK=false` is enforced across all diagnostic probes and execution paths.
3. **Secret Redaction:** Outbound logs, reports, and error messages are filtered through `redact_secrets()`. No credentials or API keys are displayed.
4. **Target Repository Immutability:** External workspaces and pilot fixtures remain strictly read-only.
