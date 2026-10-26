# Phase 3, Milestone 6: End-to-End Reproduction Acceptance

**Status:** Implemented and verified  
**Branch:** `main`  
**Implemented:** Phase 3, Milestone 6  

---

## Overview

Milestone 6 represents the comprehensive end-to-end acceptance gate for Phase 3 (*Deterministic Defect Reproduction*). It rigorously validates the integrated reproduction lifecycle:
1. Finding and reproduction case management with strict provenance tracking.
2. Deterministic plan generation and offline policy validation.
3. Untrusted oracle rejection and command allowlist enforcement.
4. TOCTOU revision mismatch detection and symlink-escape containment.
5. Fail-closed host protection and isolated sandbox execution boundaries.
6. Deterministic outcome classification across all eight taxonomy states.
7. Atomic persistence and SQLite event replay across process restarts.
8. Retry budgets, non-idempotent retry refusal, head/tail output bounding, and secret redaction.
9. Finding lifecycle transition gating (`SUSPECTED` -> `REPRODUCED` only with verified reproduction evidence).
10. CLI end-to-end workflow, JSON/Markdown output formatting, and standardized exit codes.
11. Full backward compatibility with Phase 1 audit and Phase 2 review engines, SARIF export, and benchmarks.
12. Target repository and fixture immutability guarantees.

---

## Acceptance Matrix

The following matrix maps the twelve Phase 3 acceptance scenarios to their verifying implementation and unit test functions in `tests/unit/test_phase3_milestone6.py`:

| # | Acceptance Scenario | Requirements & Hardening Verified | Verifying Test Function |
|---|---|---|---|
| **1** | **Case Creation & Trusted Provenance** | Pinned commit SHA, deterministic case ID, explicit provenance tracking (`TRUSTED_FIXTURE`, `STATIC_ANALYSIS`), rejection of untrusted expected behavior. | `test_scenario1_create_case_trusted_provenance` |
| **2** | **Plan Generation & Validation** | Deterministic plan generation (`generate_plan_id`), offline validation against policy, inspectable via JSON and Markdown. | `test_scenario2_plan_generation_and_validation` |
| **3** | **Oracle & Unsafe Plan Rejection** | Rejection of unapproved executables, forbidden shell metacharacters (`;`, `\|`, `&`, `$`, `\n`), credential arguments (`--token`), and untrusted expected behavior sources. | `test_scenario3_reject_untrusted_oracles_and_unsafe_plans` |
| **4** | **Stale Revision & Symlink Containment** | TOCTOU check against workspace HEAD; path resolution containment (`Path.is_relative_to`); rejection of symlink directory escapes outside workspace. | `test_scenario4_refuse_stale_revisions_and_symlinks` |
| **5** | **Sandbox Boundary & Fail-Closed Host Protection** | Fail-secure execution: when Docker is unavailable or misconfigured, executions report `SKIPPED_SANDBOX_UNAVAILABLE` / `ENVIRONMENT_BLOCKED`; zero target code runs on host. | `test_scenario5_sandbox_fail_closed_without_docker` |
| **6** | **Outcome Taxonomy Completeness** | Deterministic classification covering all 8 states: `REPRODUCED`, `NOT_REPRODUCED`, `UNRELATED_FAILURE`, `ENVIRONMENT_BLOCKED`, `TIMEOUT`, `FLAKY`, `INVALID_PLAN`, `INCONCLUSIVE`. | `test_scenario6_classify_all_eight_outcomes` |
| **7** | **Persistence & Process Restart** | Database re-openability across processes; state reconstruction matches persisted state; event replay preserves complete audit trail. | `test_scenario7_persistence_and_process_restart` |
| **8** | **Retry Budgets, Idempotency & Redaction** | Strict retry caps (`max_retries + 1`); refusal of non-idempotent retries (`NonIdempotentRetryError`); head/tail output bounding; automatic secret redaction. | `test_scenario8_retry_limits_idempotency_and_redaction` |
| **9** | **Finding Lifecycle Transition Gating** | Finding state machine advances `SUSPECTED` -> `REPRODUCED` only with verified reproduction evidence; rejects advancement on untrusted, flaky, or failed attempts. | `test_scenario9_lifecycle_transition_gating` |
| **10** | **CLI Integration & Standardized Exit Codes** | Support for `--format json` and `--format markdown`; deterministic exit codes: `0` (reproduced), `1` (not reproduced), `2` (invalid/error), `3` (blocked), `4` (timeout/inconclusive). | `test_scenario10_cli_plan_and_repro_commands` |
| **11** | **Backward Compatibility** | Phase 1 repository acquisition, inspection, symbol indexing; Phase 2 review engine, evidence calibration, SARIF exporter, and review benchmark remain intact. | `test_scenario11_backward_compatibility` |
| **12** | **Target Repository Immutability** | Read-only guarantee: target workspace git tree, HEAD, untracked files, and benchmark fixtures remain bit-identical before and after reproduction runs. | `test_scenario12_target_repository_immutability` |

---

## Standardized CLI Exit Code Contract

Across all reproduction, planning, and evaluation commands (`forge repro run`, `forge repro classify`, `forge plan run`):

| Exit Code | Meaning | Outcome Taxonomy States |
|---|---|---|
| `0` | Success / Defect Reproduced | `REPRODUCED` |
| `1` | Not Reproduced / Harmless / Expected Failure | `NOT_REPRODUCED`, `FAILED`, `PASSED`, `UNRELATED_FAILURE` |
| `2` | Policy Violation / Invalid Plan / Stale Revision / Error | `INVALID_PLAN`, `ERROR` |
| `3` | Environment Blocked / Sandbox Unavailable | `ENVIRONMENT_BLOCKED`, `BLOCKED`, `SKIPPED_SANDBOX_UNAVAILABLE` |
| `4` | Inconclusive / Timed Out / Inconsistent (Flaky) | `INCONCLUSIVE`, `TIMEOUT`, `FLAKY` |

---

## Benchmark & Verification Results

### Regression Test Suite
- **Total Test Cases**: 468 tests
- **Passing**: 468 / 468 (100%)
- **Failures / Errors**: 0
- **Phase 3 Milestone 6 Acceptance Suite**: 12 / 12 tests passed (`tests/unit/test_phase3_milestone6.py`)

### Review Quality Benchmark Suite (`forge benchmark review --mode static`)
- **Total Fixtures Evaluated**: 10
- **Fixture Pass Rate**: 100.00%
- **Precision**: 100.00%
- **Recall**: 100.00%
- **F1 Score**: 100.00%
- **False Positive Rate**: 0.00%
- **Location Accuracy**: 100.00%
- **Evidence Validity Rate**: 100.00%
- **Defects Detected**: 9 / 9 genuine defects; 0 false positives; 0 attribution failures

---

## Security Invariants & Host Protection

1. **Host Isolation Invariant**: Repository-controlled test or build code is never executed directly on the host operating system. If the Docker container runtime is absent or cannot be verified, FORGE reports `ENVIRONMENT_BLOCKED` and exits cleanly with code `3`.
2. **Command & Argument Filtering**: Shell execution wrappers (`sh`, `bash`, `cmd`, `eval`, `sudo`) and network tools (`curl`, `wget`, `nc`) are strictly forbidden. Only allowlisted, bare binary names (`python`, `python3`, `pytest`, `flake8`, `mypy`, `ruff`, `bandit`, `pip`) are permitted.
3. **Workspace Path Containment**: Working directories and command arguments are validated via `Path.is_relative_to` to guarantee containment within the acquired workspace, preventing symlink traversal attacks.
4. **Target Repository Immutability**: Read-only mounts (`--read-only`) and workspace checksum validation guarantee zero modification to repository source code, working trees, commit histories, or branches.
5. **Secret Sanitization**: Outbound reports, logs, and stored artifacts automatically sanitize known token and credential formats, replacing exposed secrets with redacted placeholders.

---

## Limitations & Boundaries

- **Docker Requirement for Active Reproduction**: Running containerized reproduction commands requires an active Docker daemon. In environments without Docker, FORGE operates in offline/static inspection and plan generation mode, failing closed for sandbox execution.
- **Audit-Only Operation**: Phase 3 concludes FORGE's investigation and deterministic defect reproduction capabilities. FORGE strictly prohibits autonomous code repair, patch application, branch creation, or pull request generation. Phase 4 will introduce autonomous repair under human oversight.
