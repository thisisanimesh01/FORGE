# Phase 13 Milestone 3 Specification: Full-System Security & Reliability

## 1. System Objective & Scope

Phase 13 Milestone 3 conducts a full-system adversarial security and reliability audit and hardening pass across the entire FORGE platform (`app/forge/`), spanning Phases 1 through 13 Milestone 2.

Its guarantees enforce fail-closed behavior under hostile repository inputs, prompt injection attempts, path traversal and symlink escapes, inline-code or dependency-install sandbox bypasses, in-memory approval forgery, unverified regression classifications, state corruption, and crash-recovery edge cases.

---

## 2. Threat Model & Hardened Security Boundaries

### 2.1 Repository Acquisition & Git Isolation (`forge.repository`)
- **Git Reference & SHA Validation (`acquisition.py`)**: Strict regex and prefix validation rejects flag injection (leading `-`), control characters, whitespace, `..`, and shell metacharacters on `branch` and `commit_sha` before invoking `git`. Git invocations harden runtime configuration with `-c protocol.ext.allow=never`, `-c core.fsmonitor=false`, `-c submodule.recurse=false`, and `core.hooksPath=/dev/null`.
- **Tar Export Hardening (`git_mediator.py`)**: `GitMediator.export_sanitized_workspace()` rejects/skips tar archive members that are symlinks (`issym()`), hard links (`islnk()`), devices (`isdev()`, `ischr()`, `isblk()`), or FIFOs (`isfifo()`) before any extraction, and terminates subprocess pipes in a bounded `try...finally` block.

### 2.2 Secret Redaction, SSRF Prevention & Context Hardening (`forge.config`, `forge.llm`)
- **Extended Secret Redaction (`config.py`)**: `SECRET_PATTERNS` redacts `Authorization: Bearer/Basic` headers, `gh[pous]_` and `github_pat_` tokens, long `sk-` / `gsk_` keys (up to 256 chars), `xox...` Slack tokens, `AIza...` Google API keys, and PEM private key headers (`-----BEGIN ... PRIVATE KEY-----`).
- **Hostname-Exact Loopback Check (`llm/openai.py`)**: `OpenAICompatibleProvider.is_available()` parses `api_base` via `urlparse()` and permits unauthenticated local availability strictly when `hostname.lower() in {"localhost", "127.0.0.1", "::1"}`, preventing subdomain bypasses such as `localhost.evil.com`.
- **Untrusted Context Escaping (`llm/context.py`)**: Applies `redact_secrets()` to symbol docstring previews and table cells, escapes `|` and triple backticks in markdown tables, and wraps Section 5 source excerpts in `~~~~python` fences with neutralized inner `~~~~` / ```` ``` ```` sequences.

### 2.3 Container Sandbox & Verification Gates (`forge.sandbox`, `forge.repair_verification`)
- **Sandbox Command & Host-Mount Policy (`sandbox/policy.py`)**:
  - Rejects `-c`, `--command`, or `-c*` flags at any argument position for `python`/`python3`, plus `-m pip install/download/wheel/uninstall`.
  - Permits `PYTEST_DISABLE_PLUGIN_AUTOLOAD` in `ALLOWED_ENV_VAR_NAMES` for isolated pytest execution without third-party host plugin loading.
  - Explicitly blocks sensitive host mount prefixes (`/etc`, `/root`, `/System`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`).
- **Verification Command Validation (`repair_verification/sandbox.py`)**: Rejects control characters (`\r`, `\n`, `\x00`) and path separators (`/`, `\`) in executable names.
- **Verification Gate Fail-Closed Semantics (`repair_verification/gates.py`)**: Gate 4 returns `VerificationStatus.INCOMPLETE` / `"FAILED"` whenever `plan.policy.require_baseline_comparison` is enabled and `baseline_summary is None` or `regression_class` is `INCONCLUSIVE` or `ENVIRONMENT_MISMATCH`.

### 2.4 Human Approval Durability & Repair Execution Boundaries (`forge.repair_execution`, `forge.orchestration`, `forge.cli`)
- **Immutable Ledger & Orthogonal Revocation (`repair_execution/ledger.py`)**:
  - Adds `revoked` column migration to `repair_approval_ledger` so revoking an already-consumed approval preserves `status = 'consumed'` rather than overwriting consumption state.
  - Adds `operator_principal` persistence on `orchestration_approval_records`.
  - Rejects conflicting re-bindings (`repair_plan_id`, `plan_digest`, `approved_strategy_id`, `approver_reference`, `workflow_id`) on existing `approval_id`s with `ValueError`.
  - Queries consumption and revocation state directly from SQLite.
- **Channel Allowlist & Principal Binding (`orchestration/approval.py`, `cli.py`)**:
  - Replaces auth-channel blacklists with strict allowlist `{"interactive_cli", "trusted_operator", "cli"}`.
  - Validates `approval.operator_principal` against prohibited synthetic identities and `trusted_approvers`.
  - Enforces expiry checks in `is_approved()`.
  - `forge orchestrate approve` checks `getpass.getuser() in trusted_set` fail-closed before confirmation or persistence and writes `operator_principal` into `ApprovalLedger`.
- **ToolRegistry Forgery Defense (`repair_execution/tools.py`)**:
  - Wires SQLite `ApprovalLedger` and `FORGE_TRUSTED_APPROVERS` into `RepairExecutionTools`.
  - `apply_approved_repair` enforces a non-empty trusted approvers allowlist and verifies durable CLI-issued ledger bindings matching `repair_plan_id`, `plan_digest`, `approved_strategy_id`, `approver_reference`, and `auth_channel`, preventing autonomous tool callers from forging in-memory `RepairApproval` JSON.
- **Workspace & Repository Path Containment**:
  - `prepare_repair_workspace` (`repair_execution/tools.py`), `create_orchestration_workflow` (`orchestration/tools.py`), `analyze_repair_maintainability` (`repair_quality/tools.py`), and `discover_repair_tests` (`repair_verification/tools.py`) enforce containment via `assert_contained_path()`.
  - `ControlledPatchApplier` rejects empty `allowed_files`, traversal, protected paths, and escaping symlinks.
- **Live Evidence Freshness (`repair_execution/executor.py`)**:
  - `_check_live_evidence_freshness()` re-verifies source file hashes on disk before repair execution and blocks execution (`ExecutionStatus.BLOCKED`) if files changed after plan synthesis.

### 2.5 Orchestration, Crash Recovery & Reconciliation Hardening (`forge.orchestration`)
- **Shared Production Ledger & Regression Guard (`orchestration/adapters.py`, `orchestration/orchestrator.py`)**:
  - `DefaultExecutionAdapter` allocates isolated `RepairWorkspace` instances cleanly and shares `approval_manager.ledger` and `trusted_approvers` with the orchestrator.
  - `submit_approval()` records validated bindings durably in SQLite immediately.
  - `run_verification()` requires `regression_classification` in `("no_regression_observed", "improved")` alongside `PASSED` status and gate decision.
- **Corrupted State Fail-Closed (`orchestration/idempotency.py`, `orchestration/recovery.py`)**:
  - `_row_to_record` and `_hydrate_stage_outputs` raise `CorruptedStateError` on malformed JSON or Pydantic validation failures.
- **Reconciliation & Tool Integration (`orchestration/recovery.py`, `orchestration/tools.py`)**:
  - Serializes `reconcile_mutation` with a threading lock, enforces strict `auth_channel` allowlist, generates UUID-suffixed `reconcile_id`s, checks nested `execution_result.execution_status` for `confirm`, clears stale `APPROVAL`/`EXECUTION` outputs in `orchestration_stage_records` on `discard`/`revert`, persists recovery events with `save_event()` while retaining `active_repair_plan_id` / `active_approval_id`, and checks `operator_principal` / `resolution` audit consistency in `verify_reconciliation_integrity()`.
  - `inspect_workflow_recovery`, `recover_orchestration_workflow`, `submit_orchestration_approval`, and `reconcile_orchestration_mutation` in `OrchestrationTools` use exact `get_latest_checkpoint()` / `RecoveryDecision` fields, persist rejection events, and guard against redundant `BLOCKED`/terminal transitions.
