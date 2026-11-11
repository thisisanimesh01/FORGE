# Phase 13 Milestone 3: Full-System Security & Reliability Hardening

## 1. Overview

Phase 13 Milestone 3 executes a full-system adversarial security and reliability review across FORGE (`app/forge/`), bridging static repository intelligence (Phase 10–11), autonomous engineering engines (Phase 12 M1–M6), orchestration and crash recovery (Phase 13 M1–M2), container sandboxing, LLM context/provider layers, secret handling, and CLI/tool entry points.

---

## 2. Remediated Subsystems & Files

| Subsystem | Modified Modules | Hardening Summary |
|---|---|---|
| Secret Redaction & Config | `app/forge/config.py` | Expanded `SECRET_PATTERNS` for `Bearer`/`Basic` headers, `gh[pous]_`/`github_pat_`, extended `sk-`/`gsk_` tokens (up to 256 chars), `xox*`, `AIza*`, and PEM private key blocks. |
| LLM Providers & Context | `app/forge/llm/openai.py`, `app/forge/llm/context.py` | Replaced hostname substring matching with exact `urlparse().hostname.lower()` loopback check; redacted docstring previews and escaped pipes/fences/tildes in `RepositoryContextBuilder` using `~~~~python` fences. |
| Repository & Git Isolation | `app/forge/repository/acquisition.py`, `app/forge/repository/git_mediator.py` | Enforced strict `_validate_git_ref` checks on branch/SHA, disabled `protocol.ext`, `core.fsmonitor`, and recursive submodules during `git`, and dropped symlinks/hardlinks/FIFOs/device nodes during tar extraction. |
| Sandbox & Verification Gates | `app/forge/sandbox/policy.py`, `app/forge/repair_verification/sandbox.py`, `app/forge/repair_verification/gates.py` | Blocked `-c`/`--command`/`-c*` at any python arg index, blocked `python -m pip install/download/wheel/uninstall`, permitted `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, restricted sensitive host prefixes, rejected control chars/path separators in verification checks, and enforced Gate 4 fail-closed on missing/inconclusive baselines. |
| Approval Ledger & Execution | `app/forge/repair_execution/ledger.py`, `app/forge/repair_execution/executor.py`, `app/forge/repair_execution/patch_applier.py`, `app/forge/repair_execution/tools.py` | Added `revoked` column migration preserving `status='consumed'`, enforced immutable binding conflicts on `record_approval()`, added `operator_principal` persistence, enforced live disk evidence freshness in `ControlledRepairExecutor`, rejected empty `allowed_files` and symlinks in `ControlledPatchApplier`, and required SQLite `ApprovalLedger` backing + containment in `RepairExecutionTools`. |
| Orchestration, Recovery & CLI | `app/forge/orchestration/approval.py`, `app/forge/orchestration/adapters.py`, `app/forge/orchestration/orchestrator.py`, `app/forge/orchestration/idempotency.py`, `app/forge/orchestration/recovery.py`, `app/forge/orchestration/tools.py`, `app/forge/cli.py`, `app/forge/repair_quality/tools.py`, `app/forge/repair_verification/tools.py` | Switched auth-channel validation to strict allowlist `{"interactive_cli", "trusted_operator", "cli"}`, checked `operator_principal` and expiry, wired `DefaultExecutionAdapter` to shared `ApprovalLedger` + `trusted_approvers`, gated verification completion on clean `regression_classification`, raised `CorruptedStateError` on corrupt state/idempotency records, serialized `reconcile_mutation`, cleared stale outputs cleanly on `discard`/`revert`, aligned `OrchestrationTools` recovery inspection/event persistence, enforced OS user allowlist in `forge orchestrate approve`, and enforced path containment on maintainability and test discovery tools. |

---

## 3. Verification Summary

- **Dedicated M3 Security & Fault-Injection Suite**: `tests/unit/test_phase13_m3_security_reliability.py` — **18 passed**.
- **Full Unit Regression Suite**: `./.venv/bin/pytest tests/unit/ -q` — **2,098 passed, 2 skipped** (Docker unavailable / repair generalization skip).
- **Integration Suite**: `./.venv/bin/pytest tests/integration/ -v` — **6 skipped** (requires `FORGE_RUN_DOCKER_TESTS=1` with live Docker daemon and `FORGE_RUN_LIVE_OLLAMA_TESTS=1` opt-in).
- **Host Environment Note**: Local Docker daemon is unavailable (`DOCKER_DOWN`); runtime container isolation enforcement was verified structurally and via policy/command unit tests rather than live container execution on this host.
