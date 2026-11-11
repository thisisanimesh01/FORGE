# FORGE Phase 13 Milestone 3: Full-System Security & Reliability Audit Report

**Audit Date:** 2026-11-11
**Branch:** `phase-13-m3-system-security-reliability` (branched from `main` at `05dcfd5a98dc6f8fb797ab58d39d5dd18d778481`)
**PR:** #17 (`https://github.com/thisisanimesh01/FORGE/pull/17`) targeting `main`
**Scope:** Two-pass independent adversarial security and reliability audit across FORGE Phases 1–13 M2 (`app/forge/`, CLI, ToolRegistry, SQLite ledgers, sandbox policies, LLM providers/context, archive export, git execution, and crash recovery).

---

## 1. Executive Summary

Two consecutive adversarial audit passes across the FORGE codebase identified and remediated critical, high, and medium-severity vulnerabilities across outbound SSRF validation, archive tar extraction, git subprocess environment sanitization, filesystem TOCTOU/hardlink escapes during patch application and rollback, SQLite approval ledger tamper/migration integrity, autonomous tool authorization forgery, verification gate fail-closed logic, markdown prompt-fence sanitization, and crash-recovery state continuity.

All confirmed findings within scope have been remediated cleanly with regression tests, without relaxing security policies, altering protected configuration (`.env`, `.gitignore`, `.github/workflows/`, Docker security constraints), or introducing cloud fallback inference.

---

## 2. Host Environment & Audit Constraints

- **Workspace Isolation**: Conducted strictly inside `/Users/animeshyadav/Desktop/project/forge`.
- **Docker Status**: Local Docker daemon is unavailable (`unix:///Users/animeshyadav/.docker/run/docker.sock: connect: no such file or directory`). Live Docker daemon integration tests (`tests/integration/test_docker_sandbox_integration.py`, `tests/integration/test_phase10_docker_runtime_isolation.py`) skip cleanly and fail-closed `ExecutionStatus.UNAVAILABLE` enforcement is preserved.
- **CI Tooling Inventory**: `.github/workflows/ci.yml` executes `pip_audit -r requirements/constraints.txt`, `python -m compileall app/forge`, `pytest tests/unit tests/security`, `python -m build --no-isolation`, and `forge release manifest/verify` across Python 3.11, 3.12, and 3.13.
- **Standalone Static Tools**: `mypy`, `ruff`, and `bandit` are not installed on the local host and are not executed by `.github/workflows/ci.yml`.

---

## 3. Additional Attack Scenarios Audited & Verified (Second-Pass Closure Review)

- **Area A (SSRF & URL Validation)**: Tested IPv4/IPv6 loopback (`localhost`, `127.0.0.1`, `::1`), IPv4 private RFC1918 (`10/8`, `172.16/12`, `192.168/16`), cloud metadata/link-local (`169.254.169.254`, `fe80::1`), unspecified (`0.0.0.0`), IPv6 ULA (`fc00::1`, `fd12::1`), IPv4-mapped IPv6 (`[::ffff:169.254.169.254]`, `[::ffff:127.0.0.1]`, `[::ffff:10.0.0.1]`), alternate numeric/octal/hex IP encodings (`2130706433`, `0x7f000001`, `0177.0.0.1`), trailing-dot IP literals (`127.0.0.1.`), URL credentials (`user:pass@`), non-HTTP schemes (`file://`, `ftp://`, `gopher://`), DNS rebinding to private/loopback IPs, DNS TOCTOU via IP pinning, and HTTP `302` redirects in both `OpenAICompatibleProvider` and `diagnostics.check_llm_provider()`.
- **Area B (Archive Extraction & Acquisition)**: Tested backslash traversal (`..\\..\\escape.py`), leading-dot stripping regression (`./.env`), case-insensitive `.GIT/hooks` collision, hardlinks (`LNKTYPE`), symlinks, GNU sparse tar entries (`GNUTYPE_SPARSE`), duplicate case-colliding entries (`src/Config.py` + `src/config.py`), symlinked parent directory races, and per-file/total size and member limits.
- **Area C (Git Argument & Execution Hardening)**: Tested host `GIT_EXTERNAL_DIFF`, `GIT_CONFIG_PARAMETERS`, `GIT_SSH_COMMAND`, `GIT_TEMPLATE_DIR` injection, `core.symlinks=false`, `protocol.ext.allow=never`, `core.fsmonitor=false`, `submodule.recurse=false`, `diff.external=`, protocol scoping (`GIT_ALLOW_PROTOCOL`), and `--` / `--detach` separators.
- **Area D (Filesystem Containment & TOCTOU)**: Tested internal symlink copying in `RepairWorkspaceManager._safe_copy_tree()`, parent-directory symlink replacement (`ws/src -> outside_dir`), hardlinked files (`st_nlink > 1`), `O_NOFOLLOW` descriptor reads/writes in `ControlledPatchApplier` and `WorkspaceRollbackManager.rollback()`, forced cleanup after interrupted execution, and pre-apply live evidence freshness re-verification in `ControlledRepairExecutor`.
- **Area E (Approval Ledger Migration & Integrity)**: Tested pre-M3 SQLite schema migration (missing `revoked`, `operator_principal`, `record_digest`), orthogonal consumed+revoked state preservation, revoking unused approvals, 8-thread concurrent `record_consumed()` race, and SQLite tampering caught by `record_digest` verification.
- **Area F (Sandbox Runtime Validation)**: Verified `DockerSandbox.build_docker_arguments()` flags (`--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user 1000:1000`, `--pids-limit`, `--memory`, `--cpus`, `,readonly`), `UNAVAILABLE` fail-closed refusal without daemon, and `docker rm -f` cleanup after forced timeout termination.

---

## 4. Findings, Severity & Remediation Evidence

### Critical / High Findings Remediated
1. **[HIGH] In-Memory Approval Forgery via ToolRegistry (`app/forge/repair_execution/tools.py`)**
   - *Finding*: `apply_approved_repair` lacked durable SQLite `ApprovalLedger` backing and mandatory `FORGE_TRUSTED_APPROVERS` checks.
   - *Remediation*: Wired `ApprovalLedger` and non-empty `FORGE_TRUSTED_APPROVERS` into `RepairExecutionTools`; required matching durable CLI-issued ledger bindings prior to execution.
2. **[HIGH] Outbound SSRF, DNS Rebinding & Redirect Exposure (`app/forge/llm/url_safety.py`, `openai.py`, `diagnostics.py`)**
   - *Finding*: `diagnostics.check_llm_provider()` used unrestricted `urllib.request.urlopen()` (following redirects and accepting arbitrary schemes/IPs), while `OpenAICompatibleProvider` lacked scheme/userinfo/IPv4-mapped/numeric-IP/DNS-pinning/3xx checks. Furthermore, an initial `URLSecurityError(ValueError)` exception was caught by `except ValueError` on trailing-dot IP literals (`127.0.0.1.`) — reproduced and fixed cleanly in Pass 2.
   - *Remediation*: Introduced shared `validate_url_target()` / `is_canonical_loopback_url()`, IP pinning (`build_pinned_url()`, `Host`, `sni_hostname`), `_NoRedirectHandler`, explicit `300 <= status_code < 400` rejection, and trailing-dot IP enforcement outside `try/except ValueError`.
3. **[HIGH] Archive Extraction Traversal, Case-Collision & Parent Symlink Race (`app/forge/repository/git_mediator.py`)**
   - *Finding*: `export_sanitized_workspace()` used case-sensitive `.git` checks, missed backslash traversals, sparse/duplicate members, byte/member caps, `assert_contained_path()`, `./` dot-stripping hazard (`lstrip("./")`), and pre-existing destination symlink races masked by `is_protected_path()`.
   - *Remediation*: Enforced strict `isdir()`/`isreg()` + non-sparse allowlist, exact `./` prefix stripping, case-insensitive `.git` & duplicate collision detection, ancestor `is_symlink()` check before `is_protected_path()`, `assert_contained_path()`, byte/member quotas, and `os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW` writes.
4. **[HIGH] Symlink Ancestor & Hardlink Escape in Patch Application & Rollback (`app/forge/repair_execution/patch_applier.py`, `rollback.py`, `workspace.py`, `executor.py`)**
   - *Finding*: `WorkspaceRollbackManager.rollback()` restored files to `ws_root / rel_p` without `assert_contained_path()` or parent/leaf symlink checks, `ControlledPatchApplier` skipped ancestor/hardlink checks, `_safe_copy_tree()` preserved internal symlinks, and `ControlledRepairExecutor` checked disk freshness only before approval checks.
   - *Remediation*: Enforced ancestor `lstat` symlink rejection, `st_nlink > 1` hardlink rejection, and `O_NOFOLLOW` descriptor I/O across patching and rollback; dropped all symlinks during `_safe_copy_tree()`; added pre-apply live freshness verification in `ControlledRepairExecutor`.
5. **[HIGH] Approval Revocation Clobbering Consumed State & Principal/Channel Bypasses (`app/forge/repair_execution/ledger.py`, `orchestration/approval.py`, `recovery.py`, `cli.py`)**
   - *Finding*: Revocation overwrote `status = 'consumed'`, channels used blacklists, `orchestrate approve` did not verify `getpass.getuser()` against `trusted_approvers`, `is_approved()` omitted expiry verification, and SQLite approval records lacked tamper digests.
   - *Remediation*: Migrated `revoked`, `operator_principal`, and `record_digest` columns orthogonally, rejected immutable re-binding conflicts, switched auth channels to allowlist `{"interactive_cli", "trusted_operator", "cli"}`, enforced `operator_user in trusted_set` in CLI, and validated `record_digest` on reads.
6. **[HIGH] Sandbox Inline Execution & Verification Gate Bypasses (`app/forge/sandbox/policy.py`, `repair_verification/sandbox.py`, `gates.py`, `orchestrator.py`)**
   - *Finding*: Python `-c` was checked only at argv[1], `-m pip` / sensitive host prefixes / `PYTEST_DISABLE_PLUGIN_AUTOLOAD` needed policy alignment, verification checks lacked control-char/path-separator checks, Gate 4 passed missing/inconclusive baselines under some branches, and `run_verification()` ignored `regression_classification`.
   - *Remediation*: Hardened argument scanning across all positions, sensitive host prefix blocks, check command validation, Gate 4 fail-closed logic, and orchestrator regression gating.
7. **[HIGH] ToolRegistry Host Workspace Escapes (`orchestration/tools.py`, `repair_execution/tools.py`, `repair_quality/tools.py`, `repair_verification/tools.py`)**
   - *Finding*: Workspace/repository path arguments escaped target repository/workspace roots.
   - *Remediation*: Enforced `assert_contained_path()` across `create_orchestration_workflow`, `prepare_repair_workspace`, `analyze_repair_maintainability`, and `discover_repair_tests`.

### Medium Findings Remediated
8. **[MEDIUM] Git Ref/Arg Injection & Subprocess Environment Leakage (`acquisition.py`, `git_mediator.py`)** — Added `_sanitized_git_env()` stripping `GIT_EXTERNAL_DIFF`/`GIT_CONFIG_PARAMETERS`/`GIT_SSH_COMMAND`/`GIT_TEMPLATE_DIR`, restricted `GIT_ALLOW_PROTOCOL`, disabled `core.symlinks`/`core.fsmonitor`/`submodule.recurse`, and added `--` / `--detach`.
9. **[MEDIUM] Secret Redaction & Context Markdown Fence Injection (`config.py`, `llm/context.py`)** — Expanded `SECRET_PATTERNS` (Bearer/Basic, GitHub/Google/Slack/OpenAI/Groq tokens, PEM headers) and sanitized/escaped docstrings & `~~~~python` code blocks.
10. **[MEDIUM] Recovery/Reconciliation Desync, Ledger Sharing & Corrupt State (`orchestration/adapters.py`, `orchestrator.py`, `idempotency.py`, `recovery.py`, `orchestration/tools.py`)** — Shared `ApprovalLedger`/`trusted_approvers` into `DefaultExecutionAdapter`, raised `CorruptedStateError` on malformed JSON/stage outputs, serialized `reconcile_mutation`, cleared `APPROVAL`/`EXECUTION` outputs cleanly on discard/revert, generated UUID-unique `reconcile_id`s, verified `operator_principal`/`resolution` event consistency, and fixed `get_latest_checkpoint()`/`RecoveryDecision` field mappings and already-blocked tool rejections.

---

## 5. Complete Test-to-Invariant Mapping (`tests/unit/test_phase13_m3_security_reliability.py`)

| # | Test Name | Security Invariant | Affected Modules | Attack / Fault Scenario | Expected Fail-Closed Behavior |
|---|---|---|---|---|---|
| 1 | `test_acquisition_rejects_git_ref_and_sha_flag_injection` | Git CLI option safety | `repository/acquisition.py` | `--upload-pack`, `-c`, shell chars in ref/SHA | Raises `SecurityError` prior to `git` execution |
| 2 | `test_git_mediator_export_skips_symlinks_and_special_files` | Archive member filtering | `repository/git_mediator.py` | Tar stream symlink to `/etc/passwd` | Symlink dropped; regular source only extracted |
| 3 | `test_tool_containment_blocks_external_workspace_paths` | Tool workspace containment | `orchestration/tools.py`, `repair_execution/tools.py`, `repair_quality/tools.py`, `repair_verification/tools.py` | External host path passed to 4 tools | JSON error with `Security boundary violation` |
| 4 | `test_secret_redaction_extended_formats` | Secret scrubbing | `config.py` | Bearer JWT, `ghp_`, `github_pat_`, `AIza`, `sk-`, `gsk_`, `xoxb-`, PEM | All tokens replaced by `REDACTED` |
| 5 | `test_context_builder_redacts_secrets_and_neutralizes_markdown_fences` | Prompt-injection containment | `llm/context.py` | Pipes, ```` ``` ````, `~~~~`, tokens in docstrings/code | Secrets redacted; fences escaped in `~~~~python` |
| 6 | `test_openai_provider_localhost_hostname_check` | Hostname SSRF check | `llm/openai.py`, `llm/url_safety.py` | `https://localhost.evil.com/v1` | `is_available() == False`; canonical loopback `True` |
| 7 | `test_sandbox_policy_blocks_inline_python_pip_and_sensitive_mounts` | Sandbox command & mount policy | `sandbox/policy.py` | `-I -c`, `-csys`, `--command`, `-m pip install`, `/etc`, `~/.ssh` | Raises `PolicyViolationError`; allows `PYTEST_DISABLE_PLUGIN_AUTOLOAD` |
| 8 | `test_verification_sandbox_rejects_control_chars_and_path_separators` | Verification command hygiene | `repair_verification/sandbox.py` | Newline, NUL byte, `/usr/bin/pytest`, `..\pytest` | Returns `ExecutionStatus.REJECTED` |
| 9 | `test_verification_gate4_and_orchestrator_fail_closed_on_missing_or_inconclusive_baseline` | Fail-closed verification gate | `repair_verification/gates.py`, `orchestration/orchestrator.py` | Missing baseline, `INCONCLUSIVE` regression | Gate 4 `INCOMPLETE`/`FAILED`; workflow `BLOCKED` |
| 10 | `test_approval_ledger_revocation_preserves_consumed_and_blocks_rebind` | Approval consumption immutability | `repair_execution/ledger.py` | Revoke after consume; conflicting `record_approval` | `is_consumed()` remains `True`; rebind raises `ValueError` |
| 11 | `test_orchestration_approval_manager_enforces_channel_allowlist_principal_and_expiry` | Approval contract validation | `orchestration/approval.py` | `webhook` channel, untrusted principal, expired contract | Validation errors; `is_approved() == False` |
| 12 | `test_cli_orchestrate_approve_blocks_untrusted_local_os_user` | OS operator allowlist check | `cli.py` | Untrusted `getpass.getuser()` with trusted `--approver` | Non-zero exit; authorization error |
| 13 | `test_repair_execution_tools_blocks_forged_in_memory_approval` | Anti-forgery durable backing | `repair_execution/tools.py` | Crafted `RepairApproval` JSON absent from SQLite | Blocked (`no durable operator record`) |
| 14 | `test_controlled_executor_blocks_stale_evidence_and_patch_applier_containment` | Evidence freshness & patch scope | `repair_execution/executor.py`, `patch_applier.py` | Disk edit post-plan; empty scope; workspace symlink | Executor `BLOCKED`; patcher fails without touching target |
| 15 | `test_default_execution_adapter_shares_ledger_and_prevents_replay` | Cross-workflow single-use approval | `orchestration/adapters.py`, `orchestrator.py` | Reuse approval in second orchestrator | Shared ledger marks consumed; replay blocked |
| 16 | `test_corrupted_state_and_idempotency_payload_fail_closed` | State & cache deserialization | `orchestration/idempotency.py`, `recovery.py` | Corrupt idempotency JSON / stage output JSON | Raises `CorruptedStateError` |
| 17 | `test_reconcile_confirm_nested_evidence_discard_cleanup_and_audit_chain` | Crash recovery & reconciliation | `orchestration/recovery.py` | Untrusted channel; empty `confirm`; `discard`; SQLite tamper | Channel/unbacked confirm rejected; hash chain intact; tamper caught |
| 18 | `test_orchestration_tools_recovery_attributes_and_already_blocked_rejection` | Recovery tools & terminal safety | `orchestration/tools.py` | Inspection/rejection on `BLOCKED` workflow | Clean JSON output; no transition crash |
| 19 | `test_ssrf_url_validation_blocks_private_ipv6_numeric_creds_redirects_and_rebinding` | Complete SSRF, DNS pin & redirect defense | `llm/url_safety.py`, `llm/openai.py` | `file://`, creds, `169.254.169.254`, `10/8`, ULA, `::ffff:`, `0x7f000001`, trailing `.`, rebinding, `302` | Raises `URLSecurityError` / `LLMUnavailableError` |
| 20 | `test_diagnostics_ollama_probe_blocks_ssrf_urls_and_redirects` | Doctor probe SSRF guard | `diagnostics.py`, `llm/url_safety.py` | SSRF `OLLAMA_BASE_URL` targets | Unhealthy `MISCONFIGURED_ENV` (`SSRF`) before connect |
| 21 | `test_git_mediator_archive_blocks_traversal_case_collision_duplicates_sparse_and_symlink_race` | Tar extraction security | `repository/git_mediator.py` | `..\\..\\`, `.GIT`, `./.env`, `LNKTYPE`, `GNUTYPE_SPARSE`, collisions, symlink parent | Drops unsafe entries; raises `GitMediatorSecurityError` on collisions/races |
| 22 | `test_git_acquisition_sanitizes_host_git_env_and_hardens_flags` | Git env & flag hardening | `repository/acquisition.py` | Host `GIT_EXTERNAL_DIFF`, `GIT_SSH_COMMAND`, `GIT_CONFIG_PARAMETERS` | Host vars stripped; `core.symlinks=false`, `GIT_HOOKS_PATH=/dev/null` enforced |
| 23 | `test_patch_applier_and_rollback_block_ancestor_symlink_hardlink_and_toctou` | Patch/rollback TOCTOU & hardlinks | `patch_applier.py`, `rollback.py`, `workspace.py` | Internal symlink, hardlink (`st_nlink>1`), symlinked `ws/src` dir, forced cleanup | Symlinks/hardlinks rejected; outside files untouched; forced cleanup succeeds |
| 24 | `test_approval_ledger_legacy_migration_concurrency_and_tamper_detection` | Schema migration, concurrency & tamper check | `repair_execution/ledger.py` | Pre-M3 DB rows, unused revoke, 8-thread race, SQLite UPDATE tamper | Clean migration; 1/8 race winner; `record_digest` detects tamper |
| 25 | `test_docker_sandbox_isolation_arguments_timeout_cleanup_and_fail_closed` | Container sandbox policy & cleanup | `sandbox/docker.py` | Daemon absent; arg inspection; timeout `kill()` + `rm -f` | `UNAVAILABLE` without daemon; all isolation flags present; `docker rm -f` called |

---

## 6. Docker Execution Evidence & CI Audit Evidence

### Docker Runtime Verification
- **Local Host Status**: `docker info` fails (`unix:///Users/animeshyadav/.docker/run/docker.sock: connect: no such file or directory`). Live Docker integration tests (`tests/integration/test_docker_sandbox_integration.py` and `tests/integration/test_phase10_docker_runtime_isolation.py`) are skipped locally when run without a live Docker daemon.
- **Verified Docker Sandbox Controls**: `DockerSandbox` (`app/forge/sandbox/docker.py`) and `ReproductionHarness` (`app/forge/sandbox/runner.py`) are unit-verified to refuse host execution (`ExecutionStatus.UNAVAILABLE` / `SKIPPED_SANDBOX_UNAVAILABLE`) when Docker is absent, construct `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --pids-limit ... --memory ... --cpus ...`, enforce allowlisted envs/host-mount exclusions, and invoke `docker rm -f <container_name>` in `finally` on timeout.
- **Outstanding Item**: Actual live-container runtime execution of `tests/integration/` remains **outstanding** on a host with an active Docker daemon and `python:3.11-slim` image.

### CI Verification (`.github/workflows/ci.yml`)
- **What CI Executes**: Across Python `3.11`, `3.12`, and `3.13` (`ubuntu-latest`):
  - `pip_audit -r requirements/constraints.txt` (dependency vulnerability scan)
  - `python -m compileall app/forge` (bytecode compilation check)
  - `pytest tests/unit tests/security` (offline unit & security suites)
  - `python -m build --no-isolation` and `forge release manifest/verify`
- **What CI Does NOT Execute**:
  - `tests/integration/` (including live Docker container isolation and live Ollama tests)
  - `mypy`, `ruff`, or `bandit` (neither installed locally nor invoked in `.github/workflows/ci.yml`).

---

## 7. Regression Results & Diff Hygiene

- `./.venv/bin/pytest tests/unit/test_phase13_m3_security_reliability.py -v`: **27 passed** (includes 2 post-merge archive lifecycle & partial cleanup regression tests).
- `./.venv/bin/pytest tests/unit/ tests/security/ -q`: **2,134 passed, 2 skipped** (pre-existing host-conditional skips in `test_phase12_m5_repair_verification.py` and `test_repair_generalization.py`).
- `./.venv/bin/pytest tests/integration/ -v`: **6 skipped** (opt-in live Docker `FORGE_DOCKER_TESTS=1` and live Ollama `FORGE_RUN_OLLAMA_TESTS=1` not enabled/available on host).
- `git diff --check`: **clean (0 whitespace/patch issues)**; zero modifications to `.env`, `.gitignore`, `.github/workflows/`, or Docker configs.

---

## 8. Remaining Limitations

1. **Live Docker Daemon Tests**: `tests/integration/test_docker_sandbox_integration.py` and `tests/integration/test_phase10_docker_runtime_isolation.py` require a running Docker daemon (`FORGE_DOCKER_TESTS=1`) unavailable on this audit host and unexecuted in CI.
2. **Standalone Static Linters/Type-Checkers**: `mypy`, `ruff`, and `bandit` are absent from the host environment and `.github/workflows/ci.yml`.
3. **Host `pip-audit` Sandbox Limitation**: Local `.venv` contains an external editable `.pth` path triggering sandbox exit; dependency auditing is verified by GitHub Actions CI (`python -m pip_audit -r requirements/constraints.txt`).

---

## 9. Final Phase 13 M3 Verdict

**APPROVED FOR HUMAN REVIEW** — Zero unresolved critical or high-severity findings remain, every confirmed defect from both adversarial audit passes has been remediated and backed by dedicated regression tests, all feasible offline/unit/security checks (2,134 passed, 2 skipped) and GitHub Actions Python 3.11/3.12/3.13 checks pass cleanly, and the live Docker integration runtime limitation is explicitly documented for operator sign-off.

---

## 10. Post-Merge Hotfix Addendum: Git Archive Subprocess Termination Race (`hotfix/python312-sanitized-archive-export`)

- **Triggering CI Failure**: Post-merge CI run `42943495650` on `main` (`3d6267d5163f30805ba173e4d817bdacd637b4a6`) failed on Python 3.12 in `tests/security/test_phase10_security_boundary_audit.py::test_docker_sandbox_mount_never_includes_trusted_mirror` (`app/forge/repository/acquisition.py:337`) with:
  `AcquisitionError: Failed to export sanitized repository workspace: git archive failed during workspace export:` (empty stderr).
- **Root Cause**: In `GitMediator.export_sanitized_workspace()` (`app/forge/repository/git_mediator.py`), exiting the `with tarfile.open(fileobj=tar_proc.stdout, mode="r|") as tar:` context entered a `finally:` block that immediately checked `if tar_proc.poll() is None: tar_proc.terminate()` *before* calling `tar_proc.communicate(timeout=30)`. Because `tarfile` stops iterating once it consumes the tar EOF zero-block inside its read buffer while the child `git archive` process may still be flushing trailing padding blocks or finishing `exit(0)`, `tar_proc.poll()` intermittently returned `None` (reproduced `8/50` times on local Python 3.13.6 and `37/50` times on local Python 3.11; exposed under GitHub Actions Ubuntu Python 3.12 scheduling). Sending premature `SIGTERM` killed a healthy `git archive` (`returncode = -15`) with empty `stderr`.
- **Correction**:
  - Removed the unconditional `finally: terminate()` on normal stream completion; `export_sanitized_workspace()` now runs `tar_proc.communicate(timeout=30)` on normal completion so `git archive` drains and exits cleanly before checking `returncode`.
  - Restricted `_terminate_and_reap(tar_proc)` exclusively to aborted streams (`except Exception`, `subprocess.TimeoutExpired`, or missing `stdout` pipe), properly calling `terminate()` / `wait(timeout=5)` / `kill()` and closing both `stdout` and `stderr` pipes without FD leaks.
  - Caught `tarfile.TarError` to inspect `tar_proc` status and stderr so failing `git archive` invocations (e.g., invalid `ref`) surface redacted Git stderr rather than hiding behind `tarfile.ReadError('empty file')`, added `ref` option-injection check + `--format=tar`, fixed `os.fdopen` single-close ownership, and unlinked any partially created files on aborted/failed exports.
- **Preserved Invariants**: All M3 tar validation, traversal/backslash/drive-letter checks, `.git`/protected-path filtering, sparse/symlink/hardlink/device rejection, duplicate/case-collision detection, ancestor `is_symlink()` checks, `assert_contained_path()`, byte/member quotas, `O_EXCL`/`O_NOFOLLOW` extraction, and trusted mirror isolation remain unchanged.
- **New Regression Tests (`tests/unit/test_phase13_m3_security_reliability.py`)**:
  - `test_git_mediator_export_waits_for_slow_exit_and_surfaces_git_stderr`
  - `test_git_mediator_export_cleans_up_partial_files_and_reaps_aborted_process`
