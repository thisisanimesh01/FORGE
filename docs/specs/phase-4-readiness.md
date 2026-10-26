# Phase 4 Readiness & Sandbox Gate Specification

**Milestone:** Phase 4 Readiness & Sandbox Gate  
**Status:** ESTABLISHED & VERIFIED  
**Phase 4 Autonomous Repair:** NOT STARTED  
**Target Commit:** `test(security): establish phase four readiness gates`  
**Traceability:** Implements Phase 4 prerequisites, sandboxing audit, and repair safety contracts defined in `AGENTS.md`.

---

## 1. Executive Summary & Objective

Before initiating Phase 4 (Targeted Autonomous Repair), FORGE requires a rigorous readiness gate and formal safety boundaries. Phase 1 (Repository Intelligence), Phase 2 (Diff-Aware Review), and Phase 3 (Defect Reproduction & Verification) operated exclusively in an audit-only, read-only mode.

Phase 4 will eventually introduce patch generation and verification. However, to guarantee host integrity and prevent uncontrolled mutations, **Phase 4 autonomous repair is strictly NOT STARTED**.

This specification audits the existing sandbox and multi-provider architecture, proves fail-closed behavior on hosts without Docker, and codifies the formal **Repair Safety Contract** that any future Phase 4 component must satisfy.

---

## 2. Readiness Audit Findings

### 2.1 Git and Workspace State
- **Branch:** `main` tracking `origin/main` at commit `a0bc51c`.
- **Divergence:** Clean working tree, zero uncommitted modifications, synchronized with upstream.
- **Git Hook Invariant:** Git hooks disabled across acquisition and evaluation subprocesses.

### 2.2 Container Sandbox Boundary & Host Protection
- **Audited Modules:** `app/forge/sandbox/adapter.py`, `app/forge/sandbox/docker.py`, `app/forge/sandbox/runner.py`.
- **Environment Status:** Docker is currently not installed in the local host environment (`shutil.which("docker") is None`).
- **Fail-Closed Verification:**
  - `DockerSandbox.check_health()` detects missing binary and returns `(False, "Docker binary not found in PATH")`.
  - `DockerSandbox.run()` immediately halts execution without invoking host processes, returning `ExecutionStatus.UNAVAILABLE` with `isolation_established=False`.
  - `ReproductionHarness.execute()` intercepts the failed health check and returns `ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE`.
  - Host execution of repository-controlled code (scripts, tests, build commands) is strictly refused under all circumstances.

### 2.3 LLM Multi-Provider Architecture
- **Supported Providers:** `fake`, `ollama` (local), `ollama_cloud`, `gemini`, `deepseek`, and `groq`.
- **OpenAI API Provider:** Fully eliminated; requesting `openai` raises `LLMUnavailableError`.
- **Cloud Fallback Invariant:** `FORGE_ALLOW_CLOUD_FALLBACK=false` is enforced; requests never silently route between local and cloud providers.
- **Credential Hygiene:** API keys are read from environment variables (`.env`), sanitized in all outputs via `redact_secrets`, and never exposed in logs or test artifacts.

---

## 3. Formal Repair Safety Contract (Phase 4 Foundation)

The newly defined package `forge.repair.contracts` establishes the formal invariants required before any patch can be generated or tested:

```
+─────────────────────────────────────────────────────────────────────────────+
|                         RepairSafetyContract                                |
|                                                                             |
|  - finding_id: str (calibrated finding ID)                                 |
|  - base_commit_sha: str (immutable verified commit revision)                |
|  - target_repo_path: Path (STRICTLY READ-ONLY)                             |
|  - scratch_workspace_path: Path (ISOLATED CLONE)                            |
|  - allowed_files: List[str] (scoped relative POSIX paths)                   |
|  - budget: RepairBudget (max lines: 100, hunks: 5, files: 1, bytes: 20KB)   |
|  - require_human_approval: bool = True (MANDATORY GATE)                    |
|  - auto_merge_forbidden: bool = True (ENFORCED)                             |
|  - auto_deploy_forbidden: bool = True (ENFORCED)                            |
|  - target_repo_read_only: bool = True (ENFORCED)                            |
+─────────────────────────────────────────────────────────────────────────────+
                                      │
                                      ▼
                      parse_and_validate_unified_diff()
                                      │
                 ┌────────────────────┴────────────────────┐
                 │                                         │
        [Scope / Budget Pass]                     [Violation / Escape]
                 │                                         │
                 ▼                                         ▼
           PatchCandidate                          PatchValidationResult
                 │                                    (REJECTED)
                 ▼
       evaluate_repair_outcome()
                 │
  ┌──────────────┼──────────────┬──────────────┬──────────────┐
  ▼              ▼              ▼              ▼              ▼
REPAIR_        REPAIR_        BROKEN_       REGRESSION_    SANDBOX_
VERIFIED       FAILED         BUILD          DETECTED      UNAVAILABLE
                 │
                 ▼
    assert_human_approval_gate()
  (Blocks commit/push/PR export without explicit human confirmation)
```

### 3.1 Target Repository Immutability
- The target repository path (`target_repo_path`) must remain strictly read-only.
- All evaluation, patch applications, and trial runs must occur in a separate, isolated `scratch_workspace_path`.
- The contract validator prohibits `scratch_workspace_path` from being identical to or nested inside `target_repo_path` (and vice-versa).

### 3.2 Base Revision Pinning & TOCTOU Protection
- Repairs must be pinned to the exact Git commit SHA where the finding was reproduced.
- `validate_base_sha()` compares the workspace HEAD revision against the pinned SHA, raising `StaleBaseRevisionError` on drift.

### 3.3 Patch Candidate Constraints & Budgeting
- `RepairBudget` limits candidate patches to prevent runaway generation:
  - Default: max 100 changed lines, max 5 hunks, max 1 modified file, max 20,000 diff bytes.
- Patches modifying undeclared files or exceeding budget limits are rejected prior to execution.

### 3.4 Scope Containment & Symlink Traversal Guards
- `verify_workspace_containment()` uses `Path.resolve()` and `Path.is_relative_to()` to guarantee all file operations remain strictly within the scratch workspace root.
- Rejects directory traversal (`..`), absolute paths (`/`), and symlinks resolving outside the root with `ScopeContainmentError`.

### 3.5 Deterministic Pre/Post Outcome Taxonomy
Repair evaluation classifies outcomes into a strict taxonomy:
1. `REPAIR_VERIFIED`: Pre-patch defect reproduction succeeded; post-patch defect is no longer reproducible; all baseline checks pass.
2. `REPAIR_FAILED`: Target defect remains reproducible after patch application.
3. `BROKEN_BUILD`: Patch application causes syntax errors, build crashes, or container timeouts.
4. `REGRESSION_DETECTED`: Target defect was resolved, but unrelated baseline checks failed.
5. `SANDBOX_UNAVAILABLE`: Isolated container environment is missing; fails closed without host execution.
6. `REPAIR_REJECTED`: Patch candidate violates scope, size, or syntax boundaries.

### 3.6 Mandatory Human Approval Gate
- Autonomous git commit, git push, pull request opening, branch merging, or software deployment is strictly forbidden.
- `assert_human_approval_gate()` enforces that any export or repository update requires explicit approval by a human operator (`human_approved=True`).

---

## 4. Verification and Test Evidence

A dedicated unit test suite (`tests/unit/test_phase4_readiness.py`) validates all readiness requirements offline without network access or live billing:

| Test Case | Invariant Verified | Outcome |
|---|---|---|
| `test_docker_sandbox_fails_closed_when_binary_missing` | Docker missing -> `ExecutionStatus.UNAVAILABLE` | PASSED |
| `test_reproduction_harness_fails_closed_without_host_execution` | Canary file untouched on host skip | PASSED |
| `test_supported_providers_inventory` | 6 supported providers verified; `openai` absent | PASSED |
| `test_cloud_fallback_policy_disabled_by_default` | `FORGE_ALLOW_CLOUD_FALLBACK=false` | PASSED |
| `test_openai_provider_strictly_rejected` | Requesting `openai` raises `LLMUnavailableError` | PASSED |
| `test_valid_repair_safety_contract` | Contract instantiation with mandatory defaults | PASSED |
| `test_reject_identical_target_and_scratch_paths` | Scratch cannot equal target repository | PASSED |
| `test_reject_scratch_inside_target_repo` | Scratch cannot nest in target repository | PASSED |
| `test_reject_target_repo_inside_scratch` | Target cannot nest in scratch repository | PASSED |
| `test_reject_disabling_human_approval` | `require_human_approval=False` rejected | PASSED |
| `test_reject_enabling_auto_merge_or_deploy` | Auto-merge/deploy enabling rejected | PASSED |
| `test_reject_invalid_commit_sha` | Non-hex / malformed SHA rejected | PASSED |
| `test_reject_path_traversal_in_allowed_files` | Traversal in declared files rejected | PASSED |
| `test_verify_workspace_containment_valid` | Containment passes within workspace | PASSED |
| `test_verify_workspace_containment_rejects_traversal` | `../` traversal raises `ScopeContainmentError` | PASSED |
| `test_verify_workspace_containment_rejects_absolute_path` | Absolute paths raise `ScopeContainmentError` | PASSED |
| `test_verify_workspace_containment_rejects_symlink_escape` | Symlink escape raises `ScopeContainmentError` | PASSED |
| `test_validate_base_sha_matching` | Matching SHA / prefix succeeds | PASSED |
| `test_validate_base_sha_stale_mismatch` | Mismatched SHA raises `StaleBaseRevisionError` | PASSED |
| `test_validate_base_sha_empty` | Empty SHA raises `StaleBaseRevisionError` | PASSED |
| `test_valid_patch_parsing` | Valid unified diff parses within budget | PASSED |
| `test_reject_patch_modifying_unallowed_file` | Unapproved file modification rejected | PASSED |
| `test_reject_patch_exceeding_line_budget` | Line budget excess rejected | PASSED |
| `test_reject_patch_exceeding_hunk_budget` | Hunk count excess rejected | PASSED |
| `test_reject_patch_with_path_traversal` | Diff header traversal rejected | PASSED |
| `test_evaluate_repair_verified` | Defect resolved + baselines pass -> `REPAIR_VERIFIED` | PASSED |
| `test_evaluate_repair_failed_still_reproducible` | Defect persists -> `REPAIR_FAILED` | PASSED |
| `test_evaluate_repair_regression` | Baselines fail -> `REGRESSION_DETECTED` | PASSED |
| `test_evaluate_repair_broken_build` | Crashes / timeouts -> `BROKEN_BUILD` | PASSED |
| `test_evaluate_repair_sandbox_unavailable` | Docker missing -> `SANDBOX_UNAVAILABLE` | PASSED |
| `test_assert_human_approval_gate_blocks_unapproved_commit` | Unapproved commit/push/PR blocked | PASSED |
| `test_assert_human_approval_gate_allows_approved_record` | Approved record passes gate | PASSED |
| `test_secret_redaction_in_patch` | Sensitive tokens scrubbed from diffs | PASSED |
| `test_target_repository_immutability` | Target files remain unmodified | PASSED |

**Full Regression Test Suite:** 573 passed in 8.14s. Zero regressions across Phase 1, Phase 2, Phase 3, and Pre-Phase 4.
