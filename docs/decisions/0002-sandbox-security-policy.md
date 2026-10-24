# Architectural Decision Record: Sandbox Interface & Execution Policy

## Context
FORGE investigates untrusted repositories containing arbitrary code, tests, configuration scripts, and documentation. In Milestone 4, FORGE requires an execution capability to run approved baseline tests, analyzers, and reproduction scripts. Executing untrusted repository code directly on the host machine presents critical security risks: arbitrary code execution, privilege escalation, host credential exfiltration, network abuse, or filesystem destruction.

## Decision
1. **Provider-Independent Protocol**:
   Define `Sandbox` protocol in `forge.sandbox.adapter` decouples execution requests from runtime implementations.
2. **Centralized Execution Policy (`forge.sandbox.policy`)**:
   Enforce default-deny:
   - Only allowlisted executables (`python`, `python3`, `pytest`, `flake8`, `mypy`, `ruff`, `bandit`, inspection `pip`) may execute.
   - Shell interpreters (`sh`, `bash`, `zsh`, `sudo`, `eval`) and shell operators (`;`, `&`, `|`, `` ` ``, `$`, `>`, `<`, newlines) are strictly rejected.
   - Repository configuration, instructions, or LLMs cannot override execution policies.
   - Automatic package installation (`pip install`) from untrusted repositories is disabled in Phase 1.
3. **Container Isolation Defaults (Docker)**:
   - `--network none` (network disabled by default).
   - `--read-only` root filesystem.
   - `--cap-drop ALL` and `--security-opt no-new-privileges`.
   - Non-root user (`1000:1000`).
   - Workspaces mounted read-only (`:ro`). Writable data isolated to a bounded in-memory tmpfs mount (`/tmp`).
   - Resource limits: explicit CPU quota, memory limit, process count cap (`pids-limit`), and wall-clock timeout.
4. **Fail-Closed on Missing Isolation**:
   If Docker is unavailable, unconfigured, or failing health checks, FORGE strictly returns `ExecutionStatus.UNAVAILABLE` with `isolation_established=False`. It **never** falls back to executing repository code on the host.
5. **No Fake Evidence**:
   When runtime execution is unavailable or rejected, no execution evidence is attached to findings or stored as verified test results.

## Consequences
- Environments without Docker can safely perform static inspection (`forge inspect . --no-llm`), index symbols, and evaluate instructions.
- Runtime test/benchmark execution is skipped cleanly with diagnostic reporting (`forge sandbox doctor`).
- No host code execution risk exists for users investigating untrusted repositories.
