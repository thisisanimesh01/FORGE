# Phase 12, Milestone 4: Controlled Autonomous Repair

**Milestone Report & Independent Security Audit Remediation**
**Repository:** `FORGE`
**Milestone:** Phase 12 M4 — Controlled Autonomous Repair
**Status:** Completed & Adversarially Audited
**Branch:** `phase-12-m4-controlled-repair`

---

## 1. Executive Summary

Phase 12 Milestone 4 introduces FORGE's **Controlled Autonomous Repair Engine**. The engine transforms approved Phase 12 M3 repair plans into reviewable, bounded code changes applied strictly within isolated repository workspaces without compromising the original repository or host system.

Following an independent adversarial security and correctness audit, the engine was hardened with an immutable baseline snapshot architecture, strict diff hunk and metadata validation, single-use approval consumption, approval revocation tracking, cryptographic audit event hash chaining, and native CRLF/EOF preservation.

The milestone enforces non-negotiable architectural and security invariants:
1. **Original Repository Immutability**: The target repository is strictly read-only. Repair modifications occur exclusively within isolated workspace directories allocated under the cache directory.
2. **Explicit Human Approval Binding**: Automated repair execution requires an explicit, non-expired, unconsumed, unrevoked `RepairApproval` contract cryptographically bound to the canonical SHA-256 digest of the repair plan (including descriptions and rationales), the exact strategy ID, and an approved file path allowlist.
3. **Pure In-Memory Patch Application**: Patch application is implemented in pure Python with zero host shell commands, zero subprocess execution, and zero script interpolation.
4. **Context-Line Integrity & In-Memory Backups**: Pre-patch file states are backed up in memory. Context mismatches or bad hunk counts cause immediate abort and complete in-memory reversion.
5. **Workspace Integrity & Automatic Rollback**: Post-application state is verified against an immutable baseline snapshot directory captured during workspace allocation. Any modification outside the approved file scope triggers automatic rollback.
6. **Immutable Cryptographic Audit Event Chain**: All lifecycle events are recorded with sequential IDs, UTC timestamps, automatic secret redaction, and an append-only SHA-256 hash chain detecting any retroactive tampering.
7. **Verification-Pending Handoff**: M4 strictly scopes execution to patch application; verification and test execution are cleanly deferred to Phase 12 M5 (`verification_pending = True`).

---

## 2. Architecture & Component Design

The implementation is modularized under `app/forge/repair_execution/`:

```
app/forge/repair_execution/
├── __init__.py           # Package exports and public interfaces
├── schemas.py            # Strict Pydantic contracts for approval, workspace, patches, and events
├── approval.py           # RepairApprovalValidator enforcing 10 mandatory authorization invariants
├── workspace.py          # RepairWorkspaceManager allocating isolated workspaces with baseline snapshots
├── patch_generator.py    # RepairPatchGenerator synthesizing bounded unified diff patches
├── patch_validator.py    # RepairPatchValidator enforcing strict hunk counts, ordering, and metadata
├── patch_applier.py      # ControlledPatchApplier applying diffs in-memory with CRLF/EOF preservation
├── integrity.py          # WorkspaceIntegrityChecker auditing post-application file scope against baseline
├── rollback.py           # WorkspaceRollbackManager restoring workspace from immutable baseline snapshot
├── events.py             # RepairEventRecorder producing tamper-evident SHA-256 hash chains
├── executor.py           # ControlledRepairExecutor orchestrating end-to-end repair lifecycle
└── tools.py              # RepairExecutionTools exposing 8 tools registered in ToolRegistry
```

### 2.1 Strict Data Contracts (`schemas.py`)

All models enforce `ConfigDict(extra="forbid")` and explicit resource budgets:

- **Enums**:
  - `ExecutionStatus`: `PREPARED`, `APPLYING`, `APPLIED`, `FAILED`, `ROLLED_BACK`, `RECOVERY_REQUIRED`, `BLOCKED`.
  - `ApprovalStatus`: `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `REVOKED`.
  - `WorkspaceStatus`: `INITIALIZED`, `READY`, `DIRTY`, `CLEANED_UP`, `CORRUPTED`, `PRESERVED_FOR_INSPECTION`.
  - `PatchValidationStatus`: `VALID`, `INVALID_SYNTAX`, `SCOPE_VIOLATION`, `PROTECTED_PATH_VIOLATION`, `OVERSIZED`, `DIGEST_MISMATCH`, `STALE_BASE`.
  - `FailureClassification`: `NONE`, `UNAUTHORIZED_EXECUTION`, `APPROVAL_EXPIRED`, `APPROVAL_REVOKED`, `SCOPE_BREACH`, `DIFF_SYNTAX_ERROR`, `TARGET_NOT_FOUND`, `PATCH_APPLICATION_FAILED`, `UNEXPECTED_MODIFICATION`, `TIMEOUT`, `ROLLBACK_FAILED`, `WORKSPACE_ERROR`.
  - `RollbackStatus`: `NOT_TRIGGERED`, `SUCCEEDED`, `FAILED`, `PRESERVED`.
  - `RepairEventType`: Categorizes 14 lifecycle events across workspace, approval, patch, diff, rollback, and integrity phases.

- **Core Contracts**:
  - `RepairApproval`: Frozen model binding `approval_id`, `repair_plan_id`, `plan_digest`, `approved_strategy_id`, `approved_file_paths`, `approval_timestamp`, `approval_expiry`, `approver_reference`, and `approval_status`.
  - `RepairWorkspace`: Tracks `workspace_id`, `original_repository_reference`, `isolated_workspace_path`, `baseline_snapshot_path`, `baseline_commit`, `baseline_tree_digest`, `allowed_paths`, and `workspace_status`.
  - `RepairPatch`: Structured representation of validated unified diffs with added/removed line metrics and SHA-256 patch digest.
  - `RepairEvent`: Immutable audit record capturing `event_id`, `execution_id`, `sequence_number`, `prev_event_hash`, `event_hash`, `event_type`, `timestamp`, `actor`, `action_description`, `related_evidence`, `integrity_digest`, and `outcome`.
  - `RepairExecutionResult`: Outcome returned for verification handoff, including `execution_status`, `workspace_id`, `applied_patch_digest`, `modified_files`, `diff_summary`, `execution_events`, `rollback_status`, `failure_classification`, `verification_pending = True`, and `approval_reference`.

### 2.2 Human Approval & Replay Protection (`approval.py`, `executor.py`)

`RepairApprovalValidator` enforces authorization rules:
1. **Status Invariant**: Approval status must be strictly `APPROVED`.
2. **Expiry Invariant**: Current UTC time must precede `approval_expiry`.
3. **Plan Binding Invariant**: `repair_plan_id` must match target plan ID.
4. **Digest Binding Invariant**: Canonical SHA-256 digest of plan content (including required changes description and rationale) must match `plan_digest`.
5. **Strategy Binding Invariant**: `approved_strategy_id` must match `plan.selected_strategy_id`.
6. **Actionability Invariant**: Plan status must be `ACTIONABLE`.
7. **Evidence Freshness Invariant**: Zero stale or unverified supporting evidence allowed.
8. **File Scope Invariant**: All affected files must be contained within `approved_file_paths`.
9. **Required Changes Scope Invariant**: All required changes must reside in approved files.
10. **Approver Reference Invariant**: Non-empty human identity required (rejects 'unknown', 'auto', 'llm').
11. **Single-Use Consumption**: Approvals are tracked in `executor._consumed_approvals`; subsequent reuse triggers `UNAUTHORIZED_EXECUTION` blocking.
12. **Revocation Registry**: Approvals can be explicitly revoked via `revoke_approval()`, immediately blocking execution with `APPROVAL_REVOKED`.

### 2.3 Immutable Baseline Snapshot & Workspace Lifecycle (`workspace.py`, `rollback.py`, `integrity.py`)

- **Safe Copy Tree**: Recursively traverses source files while filtering out FIFO special files, unix domain sockets, character/block devices, and escaping symlinks. Symlinks pointing outside the repository root are rejected with `WorkspaceError`.
- **Baseline Snapshot Creation**: Alongside the active workspace (`<workspace_id>`), an immutable baseline directory (`<workspace_id>_baseline`) is created.
- **Rollback Isolation**: `WorkspaceRollbackManager` restores files directly from the immutable baseline snapshot, not the live original repository. If the original host repository is modified, branch-switched, or deleted, rollback still achieves 100% deterministic restoration of the exact state at workspace allocation time.
- **Integrity Audit**: `WorkspaceIntegrityChecker` compares post-repair workspace against the baseline snapshot directory, ensuring modified files match the approved scope and no unexpected creations/deletions occurred.

### 2.4 Strict Patch Validation & Native Preservation (`patch_validator.py`, `patch_applier.py`)

- **Hunk Line Count Verification**: The validator parses each hunk header `@@ -old_start,old_len +new_start,new_len @@` and counts actual old lines (` ` and `-`) and new lines (` ` and `+`). Any discrepancy rejects the patch with `DIFF_SYNTAX_ERROR`.
- **Hunk Ordering & Overlap Prevention**: Enforces that hunks are strictly monotonically ascending in line numbers and do not overlap.
- **Git Metadata Rejection**: Rejects dangerous or unsupported git patch features including `rename from`, `rename to`, `old mode`, `new mode`, `copy from`, and file path renames.
- **CRLF & EOF Preservation**: `ControlledPatchApplier` detects native line endings (`\r\n` vs `\n`) and preserves them across patched files. Detects `\ No newline at end of file` diff markers to accurately preserve missing trailing newlines.

### 2.5 Tamper-Evident Event Hash Chaining (`events.py`)

- Each `RepairEvent` computes `event_hash = SHA256(prev_event_hash:seq:type:description:digest:outcome)`.
- Initial event uses `0` * 64 as seed.
- `verify_event_chain(execution_id)` validates the entire sequence, detecting any post-hoc insertion, deletion, or content tampering.
- Strictly caps recorded events at `MAX_EVENTS_PER_EXECUTION = 200` to prevent memory exhaustion.

---

## 3. Independent Security Audit Findings & Remediations

| ID | Severity | Finding | Root Cause | Remediation | Verification |
|---|---|---|---|---|---|
| **AUD-M4-01** | **CRITICAL** | Rollback contaminated if original repo modified | `rollback.py` read from live `original_repo_path` | Created immutable `baseline_snapshot_path` directory during workspace allocation; rollback restores strictly from snapshot | `test_adversarial_rollback_immune_to_original_repo_mutation` |
| **AUD-M4-02** | **HIGH** | Host symlink escape during workspace copying | `shutil.copytree(symlinks=False)` followed symlinks to external host paths | Implemented `_safe_copy_tree()` filtering out sockets/FIFOs and checking `symlink.resolve().is_relative_to(src_dir)` | `test_adversarial_symlink_escape_rejected_during_allocation` |
| **AUD-M4-03** | **HIGH** | Patch validator accepted invalid line counts & overlapping hunks | Lenient regex parsing in `patch_validator.py` | Added strict hunk count verification, ascending line order checks, overlap rejection, and git metadata prohibition | `test_adversarial_patch_bad_hunk_counts_rejected`, `test_adversarial_patch_overlapping_hunks_rejected`, `test_adversarial_patch_renames_and_git_modes_rejected` |
| **AUD-M4-04** | **MEDIUM** | Approval replay attacks & incomplete plan digest | Approvals lacked consumption tracking; `compute_plan_digest` omitted change descriptions | Implemented single-use consumption tracking, approval revocation registry, and included `description` and `rationale` in plan digest | `test_adversarial_approval_replay_rejected`, `test_adversarial_approval_revocation_blocked`, `test_adversarial_approval_plan_description_change_invalidates_digest` |
| **AUD-M4-05** | **MEDIUM** | Audit events lacked tamper detection | Events used sequential IDs but lacked cryptographic chaining | Implemented append-only SHA-256 hash chaining and `verify_event_chain()` verification | `test_adversarial_event_hash_chain_tamper_detection` |
| **AUD-M4-06** | **MEDIUM** | Patch applier corrupted CRLF and EOF newlines | Naive `.splitlines()` and `"\n".join()` forced LF endings | Added newline format detection, CRLF preservation, and `\ No newline at end of file` support | `test_adversarial_crlf_and_no_newline_preservation` |

---

## 4. Benchmark Performance Results

Executed via `app/forge/benchmarks/benchmark_phase12_m4_controlled_repair.py` on macOS:

| Benchmark Operation | Measured Metric | Result |
|---|---|---|
| Cold Workspace Allocation | Total latency (including snapshot creation) | **28.32 ms** |
| Warm Workspace Allocation | Mean latency (5 runs) | **29.17 ms** |
| Approval Invariant Validation | Mean latency (50 runs) | **0.071 ms** (median: 0.070 ms) |
| Patch Generation | Mean latency (20 runs) | **0.26 ms** |
| Patch Validation | Mean latency (50 runs) | **0.753 ms** (median: 0.739 ms) |
| In-Memory Patch Application | Execution latency | **0.99 ms** (success=True) |
| Workspace Integrity Check | Full directory audit against baseline snapshot | **8.20 ms** (valid=True) |
| Workspace Rollback & Recovery | Baseline restoration & digest check | **4.80 ms** (status=succeeded, restored=1 files) |
| Audit Event Recording & Hash Chaining | Mean latency per event (50 runs) | **0.039 ms** |
| End-to-End Controlled Execution | Mean latency (5 runs) | **2.10 ms** (median: 0.24 ms, min: 0.23 ms, max: 9.56 ms) |
| Peak Memory Usage | Traced heap memory | **188.20 KB** |

---

## 5. Verification & Test Suite

- **Dedicated Unit Test Suite**: `tests/unit/test_phase12_m4_controlled_repair.py`
  - **33 test cases** covering approval validation, single-use replay protection, revocation, workspace isolation, baseline snapshot immunity, path containment, patch syntax and hunk boundary enforcement, pure in-memory application, failure reversion, rollback, integrity audits, secret redaction, cryptographic hash chaining, CRLF/EOF preservation, and ToolRegistry integration.
  - **Result**: `33 passed in 0.40s`.
- **Full Regression Test Suite**: `tests/unit/`
  - **Result**: `1853 passed, 1 skipped in 169.55s (0:02:49)` with zero failures.

---

## 6. Limitations & Authentic Trust Boundaries

- **Human Approval Authentication Boundary**: While `RepairApproval` strictly verifies plan SHA-256 digest binding, approved strategy ID, parameter scopes, expiry, single-use consumption, and revocation state, it does **not** perform asymmetric cryptographic signature validation (e.g. Ed25519/RSA PKI) on the `approver_reference`. Verification of the approver's physical identity is entrusted to the surrounding CI/CD or IAM platform that provides the authenticated invocation context.
- **Verification Gate Handoff**: M4 strictly scopes execution to patch application within isolated workspaces. Repaired code verification (running test fixtures, static typing checks, semantic verification) is cleanly deferred to **Phase 12 Milestone 5: Verification & Safety Guarantees Engine** (`verification_pending = True`).
