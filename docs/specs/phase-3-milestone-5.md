# Phase 3, Milestone 5: Reproduction Reliability and Regression Hardening

**Status:** Implemented and verified  
**Branch:** `main`  
**Implemented:** Phase 3, Milestone 5  

---

## Overview

Milestone 5 hardens the entire Phase 3 reproduction pipeline against race conditions, out-of-workspace symlink escapes, stale revision time-of-check-to-time-of-use (TOCTOU) manipulation, process termination/interruption failures, retry exhaustion bugs, and unstandardized CLI exit codes.

---

## Threat & Failure Matrix

| Area | Threat / Vulnerability | Concrete Hardening Implementation |
|---|---|---|
| **SQLite Concurrency & Transactions** | Race conditions causing duplicate attempts or partial attempt writes on crash/interrupt | Unique index `uq_reproduction_attempts_case_num` on `(case_id, attempt_number)`; atomic SQLite transaction (`with conn:`) wrapping evidence, attempt, case aggregate state, and classification; rollback on error. |
| **Case Duplication** | Concurrent workers creating duplicate cases for identical finding and commit | Atomic existence check and `sqlite3.IntegrityError` conversion to `CaseManagementError`. |
| **Stale Revision & TOCTOU** | Running reproduction against a workspace switched to a different branch/commit | Immediate pre-execution commit check: automatically resolves HEAD via `git rev-parse HEAD` and validates against pinned case commit in both `run_attempt` and `ReproductionPlanValidator`. |
| **Symlink & Path Containment** | Symlinks in working directory or arguments resolving outside workspace | Replaced substring `startswith` check with `Path.is_relative_to()`; recursive check on command arguments that resolve to symlinks pointing outside workspace root. |
| **Output Bounding & Truncation** | Large outputs dropping critical error stack traces located at the tail | Implemented head/tail preservation in both `DockerSandbox` and `ReproductionCaseManager`: preserves initial output and concluding traceback separated by explicit `[TRUNCATED: ...]` marker. |
| **Interruption & Crash Handling** | Interrupted execution or cleanup failure leaving corrupted state | Interrupted runs and cleanup failures (`cleanup_status != 'cleaned'`) classify deterministically as `INCONCLUSIVE` (`EXECUTION_INTERRUPTED` or `CLEANUP_FAILURE`). |
| **Idempotency & Retry Budgets** | Automated retries executing non-idempotent commands or exceeding retry limits | Strict enforcement: `NonIdempotentRetryError` if `not is_idempotent`; `CaseManagementError` if attempts exceed `max_retries + 1`. |
| **Oracle & Finding Integrity** | Untrusted oracle elevating findings to `REPRODUCED` | Non-zero exit with untrusted oracle (`UNTRUSTED_REPOSITORY` or `UNTRUSTED_LLM`) classifies as `UNRELATED_FAILURE`; zero exit classifies as `INCONCLUSIVE`. Finding state machine strictly rejects transitions without trusted oracle verification. |
| **CLI Exit Codes** | Unstandardized exit codes across `repro run`, `repro classify`, and `plan run` | Standardized exit code contract: `0` (reproduced), `1` (not reproduced / failed), `2` (invalid plan / error / stale revision), `3` (environment blocked), `4` (timeout / inconclusive / flaky). |

---

## Standardized CLI Exit Code Contract

Across all reproduction and planning commands (`forge repro run`, `forge repro classify`, `forge plan run`):

| Exit Code | Meaning | Outcome States |
|---|---|---|
| `0` | Success / Defect Reproduced | `REPRODUCED` |
| `1` | Not Reproduced / Harmless Exit / Test Failure | `NOT_REPRODUCED`, `FAILED`, `PASSED`, `UNRELATED_FAILURE` |
| `2` | Policy Violation / Invalid Plan / Stale Revision / Error | `INVALID_PLAN`, `ERROR` |
| `3` | Environment Blocked / Sandbox Unavailable | `ENVIRONMENT_BLOCKED`, `BLOCKED`, `SKIPPED_SANDBOX_UNAVAILABLE` |
| `4` | Inconclusive / Timed Out / Inconsistent (Flaky) | `INCONCLUSIVE`, `TIMEOUT`, `FLAKY` |

---

## Verification & Test Suite

Milestone 5 is validated by `tests/unit/test_phase3_milestone5.py` (20 new tests) and the full regression suite (456 tests total):
- Concurrency & unique attempt numbering constraints verified.
- Atomic partial-write rollback verified under simulated SQLite transaction failures.
- Stale revision detection from workspace `.git` verified.
- Symlink containment and path escape rejection verified.
- Head/tail output preservation tested on large payloads.
- Interruption, cleanup failure, and non-idempotent retry refusal tested.
- Untrusted oracle rejection tested.
- CLI exit codes (0, 1, 2, 3, 4) verified across execution outcomes.

---

## Limitations & Boundaries

- **Container Runtime**: Sandboxed reproduction requires a reachable Docker daemon; when Docker is absent, FORGE fails closed with exit code `3` (`ENVIRONMENT_BLOCKED`), refusing to execute repository code directly on the host.
- **Audit-Only**: FORGE Phase 3 never modifies target repository files, creates commits, or applies patches.
