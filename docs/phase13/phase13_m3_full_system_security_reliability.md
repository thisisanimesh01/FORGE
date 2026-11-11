# Phase 13 Milestone 3: Full-System Security & Reliability Hardening

## 1. Overview

Phase 13 Milestone 3 executes a two-pass full-system adversarial security and reliability review across FORGE (`app/forge/`), bridging static repository intelligence (Phase 10–11), autonomous engineering engines (Phase 12 M1–M6), orchestration and crash recovery (Phase 13 M1–M2), container sandboxing, SSRF-hardened LLM/diagnostics HTTP boundaries, archive tar export, git environment isolation, approval durability, and CLI/tool entry points.

---

## 2. Remediated Subsystems & Files

| Subsystem | Modified Modules | Hardening Summary |
|---|---|---|
| Secret Redaction & Config | `app/forge/config.py` | Expanded `SECRET_PATTERNS` for `Bearer`/`Basic` headers, `gh[pous]_`/`github_pat_`, extended `sk-`/`gsk_` tokens (up to 256 chars), `xox*`, `AIza*`, and PEM private key blocks. |
| Outbound SSRF & LLM Context | `app/forge/llm/url_safety.py`, `app/forge/llm/openai.py`, `app/forge/diagnostics.py`, `app/forge/llm/context.py` | Added shared SSRF validator rejecting non-HTTP schemes, userinfo, non-canonical numeric/octal/hex/trailing-dot IP encodings, private RFC1918, CGNAT, ULA, link-local/metadata, IPv4-mapped/6to4/Teredo/NAT64 IPv6, DNS rebinding, and HTTP 3xx redirects, with DNS IP pinning (`Host` + `sni_hostname`); redacted docstring previews and escaped pipes/fences/tildes in `RepositoryContextBuilder` using `~~~~python` fences. |
| Repository & Git Isolation | `app/forge/repository/acquisition.py`, `app/forge/repository/git_mediator.py` | Enforced strict `_validate_git_ref` checks on branch/SHA, scrubbed hostile `GIT_*` env vars via `_sanitized_git_env()`, restricted `GIT_ALLOW_PROTOCOL`, disabled `protocol.ext`, `core.fsmonitor`, `core.symlinks`, `diff.external`, and recursive submodules with `--` / `--detach`; hardened `export_sanitized_workspace()` with regular/dir non-sparse allowlist, backslash/case-collision `.git` filtering, exact `./` prefix stripping, duplicate rejection, ancestor `is_symlink()` check, `assert_contained_path()`, byte/member quotas, `O_EXCL`/`O_NOFOLLOW` writes, `communicate()`-before-terminate process lifecycle handling, redacted `TarError` Git stderr surfacing, and partial file cleanup on abort. |
| Sandbox & Verification Gates | `app/forge/sandbox/policy.py`, `app/forge/repair_verification/sandbox.py`, `app/forge/repair_verification/gates.py` | Blocked `-c`/`--command`/`-c*` at any python arg index, blocked `python -m pip install/download/wheel/uninstall`, permitted `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, restricted sensitive host prefixes, rejected control chars/path separators in verification checks, and enforced Gate 4 fail-closed on missing/inconclusive baselines. |
| Approval Ledger & Execution TOCTOU | `app/forge/repair_execution/ledger.py`, `app/forge/repair_execution/executor.py`, `app/forge/repair_execution/patch_applier.py`, `app/forge/repair_execution/rollback.py`, `app/forge/repair_execution/workspace.py`, `app/forge/repair_execution/tools.py` | Migrated `revoked`, `operator_principal`, and `record_digest` columns on approval tables to preserve `status='consumed'` across revocations and detect tampering, enforced immutable binding conflicts, re-verified live disk freshness pre-apply, enforced ancestor `lstat` symlink checks + `st_nlink > 1` hardlink rejection + `O_NOFOLLOW` descriptor I/O across patching & rollback, dropped all symlinks in `_safe_copy_tree()`, and required durable SQLite `ApprovalLedger` backing + containment in `RepairExecutionTools`. |
| Orchestration, Recovery & CLI | `app/forge/orchestration/approval.py`, `app/forge/orchestration/adapters.py`, `app/forge/orchestration/orchestrator.py`, `app/forge/orchestration/idempotency.py`, `app/forge/orchestration/recovery.py`, `app/forge/orchestration/tools.py`, `app/forge/cli.py`, `app/forge/repair_quality/tools.py`, `app/forge/repair_verification/tools.py` | Switched auth-channel validation to strict allowlist `{"interactive_cli", "trusted_operator", "cli"}`, checked `operator_principal` and expiry, wired `DefaultExecutionAdapter` to shared `ApprovalLedger` + `trusted_approvers`, gated verification completion on clean `regression_classification`, raised `CorruptedStateError` on corrupt state/idempotency records, serialized `reconcile_mutation`, cleared stale outputs cleanly on `discard`/`revert`, aligned `OrchestrationTools` recovery inspection/event persistence, enforced OS user allowlist in `forge orchestrate approve`, and enforced path containment on maintainability and test discovery tools. |

---

## 3. Verification Summary

- **Dedicated M3 Security & Fault-Injection Suite**: `tests/unit/test_phase13_m3_security_reliability.py` — **27 passed**.
- **Full Unit & Security Regression Suites**: `./.venv/bin/pytest tests/unit/ tests/security/ -q` — **2,134 passed, 2 skipped** (Docker unavailable / repair generalization skip).
- **Integration Suite**: `./.venv/bin/pytest tests/integration/ -v` — **6 skipped** (requires `FORGE_DOCKER_TESTS=1` with live Docker daemon and `FORGE_RUN_OLLAMA_TESTS=1` opt-in).
- **Host & CI Limitation Note**: Local Docker daemon is unavailable and `.github/workflows/ci.yml` executes `pip_audit`, `compileall`, `pytest tests/unit tests/security`, and wheel/sdist build verification (without `tests/integration`, `mypy`, `ruff`, or `bandit`).
