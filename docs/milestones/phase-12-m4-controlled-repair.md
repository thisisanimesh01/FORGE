# Phase 12, Milestone 4: Controlled Autonomous Repair

**Milestone Report**
**Repository:** `FORGE`
**Milestone:** Phase 12 M4 — Controlled Autonomous Repair
**Status:** Completed
**Branch:** `phase-12-m4-controlled-repair`

---

## 1. Executive Summary

Phase 12 Milestone 4 introduces FORGE's **Controlled Autonomous Repair Engine**. The engine transforms approved Phase 12 M3 repair plans into reviewable, bounded code changes applied strictly within isolated repository workspaces without compromising the original repository or host system.

The milestone enforces non-negotiable architectural and security invariants:
1. **Original Repository Immutability**: The target repository is strictly read-only. Repair modifications occur exclusively within isolated workspace directories allocated under the cache directory.
2. **Explicit Human Approval Binding**: Automated repair execution requires an explicit, non-expired, unrevoked `RepairApproval` contract cryptographically bound to the canonical SHA-256 digest of the repair plan, the exact strategy ID, and an approved file path allowlist.
3. **Pure In-Memory Patch Application**: Patch application is implemented in pure Python with zero host shell commands, zero subprocess execution, and zero script interpolation.
4. **Context-Line Integrity & In-Memory Backups**: Pre-patch file states are backed up in memory. Context mismatches cause immediate abort and complete in-memory reversion.
5. **Workspace Integrity & Automatic Rollback**: Post-application state is verified against baseline tree digests. Any modification outside the approved file scope triggers automatic rollback.
6. **Immutable Audit Event Log**: All lifecycle events are recorded with sequential IDs, UTC timestamps, and automatic secret redaction.
7. **Verification-Pending Handoff**: M4 strictly scopes execution to patch application; verification and test execution are cleanly deferred to Phase 12 M5 (`verification_pending = True`).

---

## 2. Architecture & Component Design

The implementation is modularized under `app/forge/repair_execution/`:

```
app/forge/repair_execution/
├── __init__.py           # Package exports and public interfaces
├── schemas.py            # Strict Pydantic contracts for approval, workspace, patches, and tools
├── approval.py           # RepairApprovalValidator enforcing 10 mandatory authorization invariants
├── workspace.py          # RepairWorkspaceManager allocating isolated workspaces with baseline digests
├── patch_generator.py    # RepairPatchGenerator synthesizing bounded unified diff patches
├── patch_validator.py    # RepairPatchValidator enforcing unified diff syntax and scope boundaries
├── patch_applier.py      # ControlledPatchApplier applying diffs in-memory with automatic backup reversion
├── integrity.py          # WorkspaceIntegrityChecker auditing post-application file scope
├── rollback.py           # WorkspaceRollbackManager restoring workspace to clean baseline tree
├── events.py             # RepairEventRecorder producing immutable redacted audit trails
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
  - `RepairWorkspace`: Tracks `workspace_id`, `original_repository_reference`, `isolated_workspace_path`, `baseline_commit`, `baseline_tree_digest`, `allowed_paths`, and `workspace_status`.
  - `RepairPatch`: Structured representation of validated unified diffs with added/removed line metrics and SHA-256 patch digest.
  - `RepairEvent`: Immutable audit record capturing `event_id`, `execution_id`, `event_type`, `timestamp`, `actor`, `action_description`, `related_evidence`, `integrity_digest`, and `outcome`.
  - `RepairExecutionResult`: Outcome returned for verification handoff, including `execution_status`, `workspace_id`, `applied_patch_digest`, `modified_files`, `diff_summary`, `execution_events`, `rollback_status`, `failure_classification`, `verification_pending = True`, and `approval_reference`.

### 2.2 Human Approval Invariants (`approval.py`)

`RepairApprovalValidator` enforces 10 non-negotiable authorization rules:
1. **Status Invariant**: Approval status must be strictly `APPROVED`.
2. **Expiry Invariant**: Current UTC time must precede `approval_expiry`.
3. **Plan Binding Invariant**: `repair_plan_id` must match target plan ID.
4. **Digest Binding Invariant**: Canonical SHA-256 digest of plan content must match `plan_digest`.
5. **Strategy Binding Invariant**: `approved_strategy_id` must match `plan.selected_strategy_id`.
6. **Actionability Invariant**: Plan status must be `ACTIONABLE`.
7. **Evidence Freshness Invariant**: Zero stale or unverified supporting evidence allowed.
8. **File Scope Invariant**: All affected files must be contained within `approved_file_paths`.
9. **Required Changes Scope Invariant**: All required changes must reside in approved files.
10. **Approver Reference Invariant**: Non-empty human identity required (rejects 'unknown', 'auto', 'llm').

### 2.3 Isolated Workspace Lifecycle (`workspace.py`, `rollback.py`, `integrity.py`)

- **Allocation**: Copies repository files into an isolated directory under `workspace_dir / "repair_workspaces" / <workspace_id>`. Excludes `.git` and protected paths. Resolves symlinks to prevent directory escape.
- **Deep Purge**: Calls `purge_protected_paths_from_workspace()` to remove credentials (`.env`, private keys, databases).
- **Baseline Integrity**: Computes canonical SHA-256 tree digest across all non-protected files.
- **Integrity Audit**: Compares post-repair tree against baseline. Detects unauthorized creation, modification, or deletion outside `allowed_paths`.
- **Automatic Rollback**: Restores original files from memory or original repo, verifies post-digest equals baseline digest. Transitions to `PRESERVED_FOR_INSPECTION` if recovery fails.

### 2.4 Controlled In-Memory Patch Application (`patch_applier.py`, `patch_generator.py`, `patch_validator.py`)

- **Synthesized Diffs**: `RepairPatchGenerator` constructs standard unified diffs from `RequiredChange` records.
- **Strict Validation**: `RepairPatchValidator` audits hunk headers (`@@ -start,len +start,len @@`), verifies file paths, rejects binary diffs, enforces size budgets (`MAX_DIFF_BYTES = 500,000`), and checks patch digest.
- **Pure Python Application**: `ControlledPatchApplier` parses hunks, checks context lines, and applies modifications in reverse line order to prevent offset disruption. Reverts in-memory backups immediately if any hunk fails.

---

## 3. ToolRegistry Integration

Eight structured, typed tools are registered in FORGE's `ToolRegistry`:

| Tool Name | Purpose | Input Schema | Output Schema |
|---|---|---|---|
| `prepare_repair_workspace` | Allocates isolated directory with baseline digest | `PrepareRepairWorkspaceInput` | `PrepareRepairWorkspaceOutput` |
| `validate_repair_approval` | Audits human approval against mandatory invariants | `ValidateRepairApprovalInput` | `ValidateRepairApprovalOutput` |
| `generate_repair_patch` | Synthesizes bounded unified diff patch from plan | `GenerateRepairPatchInput` | `GenerateRepairPatchOutput` |
| `validate_repair_patch` | Validates hunk syntax, line bounds, and file scope | `ValidateRepairPatchInput` | `ValidateRepairPatchOutput` |
| `apply_approved_repair` | Applies approved patch inside isolated workspace | `ApplyApprovedRepairInput` | `ApplyApprovedRepairOutput` |
| `inspect_repair_execution` | Inspects outcome, modified files, and rollback state | `InspectRepairExecutionInput` | `InspectRepairExecutionOutput` |
| `rollback_repair_workspace` | Reverts workspace modifications to restore baseline tree | `RollbackRepairWorkspaceInput` | `RollbackRepairWorkspaceOutput` |
| `get_repair_execution_history` | Queries immutable audit events for an execution | `GetRepairExecutionHistoryInput` | `GetRepairExecutionHistoryOutput` |

---

## 4. Benchmark Performance Results

Executed via `app/forge/benchmarks/benchmark_phase12_m4_controlled_repair.py` on macOS:

| Benchmark Operation | Measured Metric | Result |
|---|---|---|
| Cold Workspace Allocation | Total latency | **16.53 ms** |
| Warm Workspace Allocation | Mean latency (5 runs) | **12.59 ms** |
| Approval Invariant Validation | Mean latency (50 runs) | **0.062 ms** (median: 0.059 ms) |
| Patch Generation | Mean latency (20 runs) | **0.35 ms** |
| Patch Validation | Mean latency (50 runs) | **0.686 ms** (median: 0.678 ms) |
| In-Memory Patch Application | Execution latency | **0.84 ms** (success=True) |
| Workspace Integrity Check | Full directory audit | **7.50 ms** (valid=True) |
| Workspace Rollback & Recovery | Baseline restoration & digest check | **3.78 ms** (status=succeeded) |
| Audit Event Recording | Mean latency per event (50 runs) | **0.037 ms** |
| End-to-End Execution | Mean latency (5 runs) | **9.30 ms** (median: 9.04 ms, min: 8.86 ms, max: 10.63 ms) |
| Peak Memory Usage | Traced heap memory | **203.68 KB** |

---

## 5. Verification & Test Suite

- **Dedicated Unit Test Suite**: `tests/unit/test_phase12_m4_controlled_repair.py`
  - 19 test cases covering approval validation, workspace isolation, path containment, patch generation/validation, in-memory application, failure reversion, rollback, integrity audits, secret redaction, executor orchestration, and ToolRegistry integration.
  - **Result**: `19 passed in 0.36s`.
- **Full Regression Test Suite**: `tests/unit/`
  - **Result**: `1839 passed, 1 skipped in 163.08s (0:02:43)` with zero failures.

---

## 6. Limitations & Handoff to Phase 12 M5

- **Verification Gate**: M4 intentionally excludes executing repository test suites or building software. Repaired code verification is deferred to **Phase 12 Milestone 5: Verification & Safety Guarantees Engine**.
- **Result Contract**: All successful M4 execution results explicitly set `verification_pending = True` to mandate M5 safety validation before any code promotion.
