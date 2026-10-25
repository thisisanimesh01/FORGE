# Phase 3, Milestone 4: Reproduction Outcome Classification and Evidence Validation

**Status:** Implemented and verified
**Branch:** `main`
**Implemented:** Phase 3, Milestone 4

---

## Overview

Milestone 4 implements deterministic, typed classification of reproduction attempts. It enables FORGE to reliably distinguish confirmed defect reproduction from unrelated test harness failures, missing dependencies, environment or sandbox blocks, timeouts, flaky behavior, and inconclusive evidence.

---

## Outcome Taxonomy (Classifier Schema Version `1.0.0`)

| Outcome | Description | Security / Invariant Rule |
|---------|-------------|---------------------------|
| `REPRODUCED` | Defect manifested as expected under trusted oracle | Requires independent oracle (`TRUSTED_FIXTURE` or `STATIC_ANALYSIS`), non-zero exit code, and matching error pattern. |
| `NOT_REPRODUCED` | Test passed cleanly under trusted oracle | Disclaims: "Passing tests do not prove the defect is fixed outside the tested conditions." |
| `UNRELATED_FAILURE` | Execution failed due to reasons unrelated to the target defect | Non-zero exit code without matching defect patterns, syntax error, missing dependency, or untrusted oracle. |
| `ENVIRONMENT_BLOCKED` | Sandbox environment or host isolation could not be established | Docker not found or container launch failed; fails closed without executing code on host. |
| `TIMEOUT` | Execution quota exceeded | Process killed after timeout limit (`timeout_seconds`). |
| `FLAKY` | Inconsistent signals across multiple attempts on identical pinned revision | Attempt conflict (e.g. reproduced in attempt 1, passed in attempt 2); blocks finding transition to `REPRODUCED`. |
| `INVALID_PLAN` | Plan validation failed or stale revision mismatch | Security violation (shell injection, unapproved executable), `INSUFFICIENT_INFORMATION` plan, or commit mismatch. |
| `INCONCLUSIVE` | Execution output ambiguous, interrupted, truncated, or cleanup failed | Evidence quality cannot guarantee defect presence or absence. |

---

## Classification Reason Codes

| Reason Code | Meaning |
|-------------|---------|
| `oracle_violation_confirmed` | Expected failure patterns and exit code matched under a trusted oracle. |
| `oracle_observed_no_violation` | Trusted oracle ran and observed zero failures (exit code 0). |
| `oracle_absent_or_untrusted` | Expected behavior not provided or came from untrusted provenance (`UNTRUSTED_LLM`, `UNTRUSTED_REPOSITORY`). |
| `generic_nonzero_exit` | Process failed with non-zero exit code but did not match expected defect signatures. |
| `unrelated_exception` | Recognized harness or syntax exception (e.g., `SyntaxError`, `ModuleNotFoundError`). |
| `timeout_exceeded` | Process killed due to timeout limit quota. |
| `environment_unavailable` | Docker daemon or container isolation unavailable. |
| `plan_policy_violation` | Plan failed pre-execution security checks (`ReproductionPlanValidator`). |
| `stale_revision_mismatch` | Target workspace revision does not match pinned case revision. |
| `evidence_truncated_or_missing` | Output was truncated before patterns could be evaluated. |
| `cleanup_failure` | Sandbox workspace cleanup returned non-clean status. |
| `execution_interrupted` | Attempt was halted or aborted before completion. |
| `inconsistent_retries` | Conflicting results across attempts on the same commit SHA. |
| `ambiguous_output_signals` | Contradictory signals in stdout, stderr, or exit code. |

---

## Precedence Rules

Decisions follow strict deterministic precedence (higher rule overrides lower rule):

1. **Precedence 1: Plan / Revision Validity (`INVALID_PLAN`)**
   - Stale workspace commit revision.
   - Plan validation failure (shell metacharacters, unapproved binaries, credential arguments).
   - Plan strategy is `INSUFFICIENT_INFORMATION`.
2. **Precedence 2: Environment / Sandbox Boundary (`ENVIRONMENT_BLOCKED`)**
   - Docker binary missing from PATH or daemon unavailable.
   - Container isolation refused or failed to start.
3. **Precedence 3: Timeout Quota (`TIMEOUT`)**
   - Process duration exceeded `timeout_seconds` limit.
4. **Precedence 4: Execution Integrity (`INCONCLUSIVE`)**
   - Execution was interrupted or aborted.
   - Scratch workspace cleanup failed (`cleanup_status != 'cleaned'`).
   - Output truncated before defect patterns could be verified.
5. **Precedence 5: Multi-Attempt Inconsistency (`FLAKY`)**
   - Case history contains conflicting outcomes across identical revision attempts (e.g. reproduced in one attempt, passed in another).
6. **Precedence 6: Oracle Presence & Evaluation**
   - Untrusted or absent oracle: Non-zero exit classifies as `UNRELATED_FAILURE` (`oracle_absent_or_untrusted`), never `REPRODUCED`. Zero exit classifies as `INCONCLUSIVE`.
   - Trusted oracle + clean exit 0: `NOT_REPRODUCED` (`oracle_observed_no_violation`).
   - Trusted oracle + matching error patterns + expected exit code: `REPRODUCED` (`oracle_violation_confirmed`).
   - Trusted oracle + non-zero exit + no matching patterns: `UNRELATED_FAILURE` (`generic_nonzero_exit`).
   - Ambiguous signals: `INCONCLUSIVE` (`ambiguous_output_signals`).

---

## CLI Commands

### 1. `forge repro classify <case_id>`
Classify an existing case and its execution attempts:
```bash
# Text format
forge repro classify case_find_01_a1b2c3d4e5f6

# JSON format
forge repro classify case_find_01_a1b2c3d4e5f6 --format json
```

**Exit Codes:**
- `0`: Defect reproduced (`REPRODUCED`)
- `1`: Not reproduced or unrelated failure (`NOT_REPRODUCED`, `UNRELATED_FAILURE`)
- `2`: Invalid plan or policy violation (`INVALID_PLAN`)
- `3`: Environment blocked (`ENVIRONMENT_BLOCKED`)
- `4`: Inconclusive, flaky, or timed out (`INCONCLUSIVE`, `FLAKY`, `TIMEOUT`)

### 2. `forge plan classify <plan_file>`
Classify a plan's execution viability and oracle provenance offline:
```bash
forge plan classify plans/plan_find_01.json
forge plan classify plans/plan_find_01.json --format json
```

### 3. Enhanced `forge repro show <case_id>`
Now includes the persistent classification record (outcome, reason code, explanation, and oracle verification status).

---

## Verification & Testing

The milestone is validated by `tests/unit/test_phase3_milestone4.py` (25 tests) and the full regression suite (436 tests):
- All 8 outcomes verified.
- All 13 reason codes verified.
- Untrusted oracle rejection tested (exit 1 with untrusted oracle never becomes `REPRODUCED`).
- Flaky case blocking verified (flaky evidence rejected by `validate_transition()`).
- Determinism and idempotency verified.
- Secret redaction and output bounds verified.
- Offline execution only: no untrusted target code executed on host.

---

## Deferred Functionality

- Automated statistical power analysis for flaky tests (currently 2+ conflicting attempts trigger `FLAKY`).
- Dynamic fault injection to differentiate flaky tests from environmental race conditions.
