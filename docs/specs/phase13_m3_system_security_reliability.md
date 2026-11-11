# Phase 13 Milestone 3 Specification: Full-System Security & Reliability

## 1. System Objective & Scope

Phase 13 Milestone 3 conducts a full-system adversarial security and reliability audit and hardening pass across the entire FORGE platform (`app/forge/`), spanning Phases 1 through 13 Milestone 2.

Its guarantees enforce fail-closed behavior under hostile repository inputs, prompt injection attempts, path traversal and symlink escapes, inline-code or dependency-install sandbox bypasses, in-memory approval forgery, unverified regression classifications, state corruption, and crash-recovery edge cases.

---

## 2. Threat Model & Hardened Security Boundaries

### 2.1 Repository Acquisition & Git Isolation (`forge.repository`)
- **Git Reference, Env & SHA Validation (`acquisition.py`, `git_mediator.py`)**: Strict regex and prefix validation rejects flag injection (leading `-`), control characters, whitespace, `..`, and shell metacharacters on `branch` and `commit_sha` before invoking `git`. `_sanitized_git_env()` scrubs hostile `GIT_*` variables and restricts `GIT_ALLOW_PROTOCOL`; git invocations enforce `core.hooksPath=/dev/null`, `protocol.ext.allow=never`, `core.fsmonitor=false`, `core.symlinks=false`, `submodule.recurse=false`, `diff.external=`, and explicit `--` / `--detach` separators.
- **Tar Export Hardening (`git_mediator.py`)**: `GitMediator.export_sanitized_workspace()` enforces a regular-file/directory non-sparse allowlist, rejects backslashes, NUL bytes, leading slashes, Windows drive letters, case-insensitive `.git` segments, duplicate/case-colliding paths, and symlinked ancestor directories (`GitMediatorSecurityError`), validates `assert_contained_path()`, bounds member count and byte sizes, and writes payloads via `os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW`.

### 2.2 Secret Redaction, SSRF Prevention & Context Hardening (`forge.config`, `forge.llm`, `forge.diagnostics`)
- **Extended Secret Redaction (`config.py`)**: `SECRET_PATTERNS` redacts `Authorization: Bearer/Basic` headers, `gh[pous]_` and `github_pat_` tokens, long `sk-` / `gsk_` keys (up to 256 chars), `xox...` Slack tokens, `AIza...` Google API keys, and PEM private key headers (`-----BEGIN ... PRIVATE KEY-----`).
- **SSRF Validation, DNS Pinning & Redirect Blocking (`llm/url_safety.py`, `llm/openai.py`, `diagnostics.py`)**: Enforces `http`/`https` schemes, rejects credentials/userinfo and non-canonical numeric/octal/hex/trailing-dot IPs, unwraps IPv4-mapped/6to4/Teredo/NAT64 IPv6, blocks private/ULA/link-local/metadata/unspecified/multicast/DNS-rebound loopback targets, pins validated IPs (`Host` + `sni_hostname`), and rejects `3xx` redirects cleanly.
- **Untrusted Context Escaping (`llm/context.py`)**: Applies `redact_secrets()` to symbol docstring previews and table cells, escapes `|` and triple backticks in markdown tables, and wraps Section 5 source excerpts in `~~~~python` fences with neutralized inner `~~~~` / ```` ``` ```` sequences.

### 2.3 Container Sandbox & Verification Gates (`forge.sandbox`, `forge.repair_verification`)
- **Sandbox Command & Host-Mount Policy (`sandbox/policy.py`)**:
  - Rejects `-c`, `--command`, or `-c*` flags at any argument position for `python`/`python3`, plus `-m pip install/download/wheel/uninstall`.
  - Permits `PYTEST_DISABLE_PLUGIN_AUTOLOAD` in `ALLOWED_ENV_VAR_NAMES` for isolated pytest execution without third-party host plugin loading.
  - Explicitly blocks sensitive host mount prefixes (`/etc`, `/root`, `/System`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`).
- **Verification Command Validation (`repair_verification/sandbox.py`)**: Rejects control characters (`\r`, `\n`, `\x00`) and path separators (`/`, `\`) in executable names.
- **Verification Gate Fail-Closed Semantics (`repair_verification/gates.py`)**: Gate 4 returns `VerificationStatus.INCOMPLETE` / `"FAILED"` whenever `plan.policy.require_baseline_comparison` is enabled and `baseline_summary is None` or `regression_class` is `INCONCLUSIVE` or `ENVIRONMENT_MISMATCH`.

### 2.4 Human Approval Durability & Repair Execution Boundaries (`forge.repair_execution`, `forge.orchestration`, `forge.cli`)
- **Immutable Ledger, Orthogonal Revocation & Tamper Digest (`repair_execution/ledger.py`)**:
  - Migrates `revoked`, `operator_principal`, and `record_digest` columns so revoking an already-consumed approval preserves `status = 'consumed'` and SQLite tampering on approval records is detected fail-closed.
  - Rejects conflicting re-bindings (`repair_plan_id`, `plan_digest`, `approved_strategy_id`, `approver_reference`, `approved_file_paths`, `workflow_id`) on existing `approval_id`s with `ValueError`.
  - Queries consumption and revocation state directly from SQLite.
- **Channel Allowlist & Principal Binding (`orchestration/approval.py`, `cli.py`)**:
  - Replaces auth-channel blacklists with strict allowlist `{"interactive_cli", "trusted_operator", "cli"}`.
  - Validates `approval.operator_principal` against prohibited synthetic identities and `trusted_approvers`.
  - Enforces expiry checks in `is_approved()`.
  - `forge orchestrate approve` checks `getpass.getuser() in trusted_set` fail-closed before confirmation or persistence and writes `operator_principal` into `ApprovalLedger`.
- **ToolRegistry Forgery Defense (`repair_execution/tools.py`)**:
  - Wires SQLite `ApprovalLedger` and `FORGE_TRUSTED_APPROVERS` into `RepairExecutionTools`.
  - `apply_approved_repair` enforces a non-empty trusted approvers allowlist and verifies durable CLI-issued ledger bindings matching `repair_plan_id`, `plan_digest`, `approved_strategy_id`, `approver_reference`, and `auth_channel`, preventing autonomous tool callers from forging in-memory `RepairApproval` JSON.
- **Workspace Containment, Hardlink/Symlink TOCTOU & Rollback Safety**:
  - `prepare_repair_workspace` (`repair_execution/tools.py`), `create_orchestration_workflow` (`orchestration/tools.py`), `analyze_repair_maintainability` (`repair_quality/tools.py`), and `discover_repair_tests` (`repair_verification/tools.py`) enforce containment via `assert_contained_path()`.
  - `RepairWorkspaceManager._safe_copy_tree()` skips all symlinks cleanly. `ControlledPatchApplier` and `WorkspaceRollbackManager.rollback()` reject empty `allowed_files`, path traversal, symlink ancestors/leaves, and hardlinks (`st_nlink > 1`), performing file I/O via `O_NOFOLLOW` descriptors.
- **Live Evidence Freshness (`repair_execution/executor.py`)**:
  - `_check_live_evidence_freshness()` re-verifies source file hashes on disk before execution and immediately before patch application (`ExecutionStatus.BLOCKED` if modified).

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
