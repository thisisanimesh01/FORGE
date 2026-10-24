# Milestone 4 Specification: Sandbox Interface & Execution Policy

## 1. Goal
Build a secure, provider-independent sandbox interface that executes approved repository commands inside an isolated environment, enforces strict resource/execution policies, collects structured results, and produces trustworthy evidence for FORGE investigation workflows. If a real isolation boundary is unavailable or fails, FORGE must fail closed and refuse to execute repository code on the host.

## 2. Architecture & Design

### A. Sandbox Models & Statuses (`app/forge/sandbox/models.py`)
- `ExecutionStatus`:
  - `SUCCEEDED = "succeeded"`
  - `FAILED = "failed"`
  - `TIMED_OUT = "timed_out"`
  - `REJECTED = "rejected"`
  - `UNAVAILABLE = "unavailable"`
  - `RUNTIME_ERROR = "runtime_error"`
- `FailureClassification`:
  - `NONE = "none"`
  - `POLICY_VIOLATION = "policy_violation"`
  - `TIMEOUT = "timeout"`
  - `RESOURCE_EXHAUSTED = "resource_exhausted"`
  - `RUNTIME_UNAVAILABLE = "runtime_unavailable"`
  - `CONTAINER_ERROR = "container_error"`
  - `COMMAND_FAILED = "command_failed"`
- `ExecutionLimits`:
  - `timeout_seconds`: float (default 60s, max 300s)
  - `memory_limit`: str (default "512m", e.g. max "2g")
  - `cpu_quota`: float (default 1.0, max 2.0 cores)
  - `pids_limit`: int (default 64, max 256 processes)
  - `max_stdout_bytes`: int (default 50_000, max 100_000)
  - `max_stderr_bytes`: int (default 50_000, max 100_000)
- `NetworkPolicy`:
  - `DISABLED = "none"`
  - `ALLOWLIST = "allowlist"` (future extension, disabled by default)
- `SandboxRequest`:
  - `request_id`: str (`req_<uuid>`)
  - `run_id`: str
  - `workspace_path`: Path
  - `command`: List[str] (exact executable + arguments, strictly typed list)
  - `relative_cwd`: str (relative to workspace root, normalized, default ".")
  - `limits`: ExecutionLimits
  - `env_vars`: Dict[str, str] (selected from strict allowlist only)
  - `network_policy`: NetworkPolicy (default DISABLED)
  - `read_only_workspace`: bool (default True)
  - `provenance`: str (e.g. `forge:baseline_check`)
- `SandboxResult`:
  - `request_id`: str
  - `run_id`: str
  - `status`: ExecutionStatus
  - `exit_code`: Optional[int]
  - `stdout`: str (bounded, redacted)
  - `stderr`: str (bounded, redacted)
  - `started_at`: datetime
  - `finished_at`: datetime
  - `duration_ms`: float
  - `timed_out`: bool
  - `stdout_truncated`: bool
  - `stderr_truncated`: bool
  - `isolation_established`: bool
  - `failure_classification`: FailureClassification
  - `error_message`: Optional[str]
  - `runtime_metadata`: Dict[str, Any]

### B. Execution Policy (`app/forge/sandbox/policy.py`)
- Central policy engine: `ExecutionPolicy` and `validate_sandbox_request(request: SandboxRequest) -> None`.
- **Allowed Executables**: Conservative allowlist:
  - `python`, `python3`, `pytest`, `flake8`, `mypy`, `ruff`, `bandit`, `pip` (inspection flags only, no install)
- **Forbidden Patterns**:
  - Rejects shell execution (`sh`, `bash`, `zsh`, `sudo`, `su`, `eval`, `exec`).
  - Rejects shell operators: `|`, `&`, `;`, `>`, `<`, `$`, `\n`, `\r`, `\x00`.
  - Rejects shell flags like `-c` for python when executing arbitrary strings.
- **Working Directory**: Validated to be inside `workspace_path`. Traversal (`..`, absolute paths outside workspace) is strictly rejected.
- **Environment Allowlist**: Only explicitly approved harmless environment variables:
  - `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `LANG`, `LC_ALL`, `TERM`, `PATH`.
  - Disallows inheriting host environment wholesale or passing API keys / tokens (`FORGE_LLM_API_KEY`, `GITHUB_TOKEN`, etc.).
- **Dependency Installation**: Disabled in Phase 1 / Milestone 4. Repository install hooks or `setup.py install` / `pip install` from untrusted repos are rejected by default.

### C. Sandbox Protocol & Docker Adapter (`app/forge/sandbox/adapter.py`, `app/forge/sandbox/docker.py`)
- `Sandbox` protocol:
  - `is_available() -> bool`
  - `check_health() -> Tuple[bool, str]`
  - `run(request: SandboxRequest) -> SandboxResult`
- `DockerSandbox` implementation:
  - Uses subprocess execution of `docker` CLI with argument vectors (`shell=False`).
  - Health check checks `docker info` / `docker --version`.
  - Fail closed: If Docker is unavailable, returns `ExecutionStatus.UNAVAILABLE` with `isolation_established=False`. Never falls back to host execution.
  - Safe argument construction:
    - `--network none` (disabled network)
    - `--read-only` (read-only root filesystem)
    - `--cap-drop ALL`
    - `--security-opt no-new-privileges`
    - `--pids-limit <limit>`
    - `--memory <limit>`
    - `--cpus <limit>`
    - `--user 1000:1000` (non-root)
    - Mounts workspace with `:ro` (read-only)
    - Mounts temporary isolated scratch tmpfs on `/tmp` (bounded, e.g. `tmpfs,size=64m`)
    - `--rm` container cleanup, with explicit container naming (`forge_<request_id>`)
    - Container timeout and guaranteed kill/rm in `finally` blocks.

### D. Evidence and Event Integration (`app/forge/sandbox/evidence.py`)
- Connects `SandboxResult` to `create_evidence()` and `ForgeStore.append_event()`:
  - Produces evidence of type `TEST_RESULT`, `COMMAND_RESULT`, or `REPRODUCTION_RESULT` with provenance `forge:sandbox`.
  - Records events: `SANDBOX_EXECUTION_REQUESTED`, `SANDBOX_EXECUTION_COMPLETED`, `SANDBOX_EXECUTION_FAILED`, `SANDBOX_UNAVAILABLE`.
  - Enforces that exit code 0 alone does NOT authorize a `verified` state change without reproducing defects or passing verified test suites.

### E. CLI Integration (`app/forge/cli.py`)
- `forge sandbox doctor`:
  - Reports Docker CLI presence, daemon reachability, configured sandbox image, and network policy.
- `forge doctor`:
  - Updated to distinguish static inspection vs sandbox interface vs runtime availability.
