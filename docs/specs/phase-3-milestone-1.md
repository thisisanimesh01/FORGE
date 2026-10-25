# Phase 3, Milestone 1: Sandbox Execution Contracts

## 1. Overview & Objective

Phase 3 transitions FORGE from static review and intelligence to **isolated defect reproduction and automated verification**. Milestone 1 establishes the typed execution contract architecture governing how defect reproduction attempts are specified, constrained, validated, and evaluated without compromising host integrity.

---

## 2. Core Execution Contracts (`forge.sandbox.contracts`)

### 2.1 Separation of Intent, Environment, and Outcome
- **`ReproductionRequest`**: Formal contract defining target repository, commit SHA, reproduction hypothesis, and command sequence.
- **`ReproductionCommand`**: Non-shell argument vector (`List[str]`), working directory, expected exit codes, and expected output patterns.
- **`ExecutionEnvironmentSpec`**: Resource quotas (timeout, memory, CPU quota, PID limits, stdout/stderr byte limits) and strict isolation boundaries (`network_enabled: False`, `read_only_root: True`).
- **`ReproductionOutcome`**: Deterministic status enum (`reproduced`, `not_reproduced`, `passed`, `failed`, `timeout`, `resource_exhausted`, `skipped_sandbox_unavailable`, `error`).
- **`ReproductionResult`**: Verifiable outcome capturing process exit code, sanitized outputs, matched regex patterns, and sandbox isolation diagnostics.

---

## 3. Security & Isolation Boundaries

1. **Strict Non-Shell Arguments**: Arbitrary shell strings, wrappers (`sh`, `bash`, `sudo`), chaining operators (`;`, `&&`, `|`, `>`), and command substitutions (`$(...)`, backticks) are rejected during Pydantic schema validation.
2. **Approved Binary Allowlist**: Only approved verification executables (`pytest`, `python`, `python3`, `mypy`, `ruff`, `flake8`, `bandit`, `pip`) are permitted.
3. **Workspace Path Containment**: Working directories must be strictly relative and contained within the repository (path traversal and absolute paths are forbidden).
4. **Network Disabled**: External network connectivity is strictly disabled in all reproduction containers.
5. **Fail-Closed Semantics**: When Docker is unavailable on the host or health checks fail, the execution harness marks the contract as `SKIPPED_SANDBOX_UNAVAILABLE`. Host execution is strictly refused under all circumstances.
6. **Secret Redaction**: All command outputs, stack traces, and diagnostics are sanitized through the FORGE secret redaction engine before persisting or rendering.

---

## 4. CLI Commands

### 4.1 `forge sandbox check <contract.json>`
Validates a reproduction contract file without running any repository code:
```bash
forge sandbox check contracts/reproduce_calc.json
```

### 4.2 `forge sandbox reproduce <contract.json>`
Safely acquires the target repository, checks sandbox runtime health, and executes the reproduction contract inside the isolated container (or reports fail-closed skip):
```bash
forge sandbox reproduce contracts/reproduce_calc.json -o reports/repro_result.json
```
