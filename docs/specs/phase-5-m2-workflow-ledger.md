# Phase 5 — Milestone 2: Supervised Workflow State Machine & Transactional Ledger

**Document ID**: `docs/specs/phase-5-m2-workflow-ledger.md`  
**Status**: APPROVED & IMPLEMENTED  
**Date**: 2026-10-27  
**Author**: FORGE Engineering Team  
**Preceding Milestone**: [Phase 5, Milestone 1: Unified Task Intake & Repository Context Pipeline](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m1-task-intake-context.md)  
**Succeeding Milestone**: Phase 5, Milestone 3: Automated Reproduction-to-Repair Chaining  

---

## 1. Executive Summary

Milestone 2 delivers a typed, guarded workflow state machine backed by an append-only transactional ledger for FORGE Phase 5. It manages end-to-end engineering tasks through formal lifecycle stages—from task intake and analysis through reproduction, repair, verification, regression, and mandatory human authorization.

Transitions are validated in pure Python code with zero model discretion. Every state progression records an immutable, secret-redacted audit event in SQLite within an atomic transaction. Optimistic concurrency control using monotonic integer versioning guarantees crash safety and prevents race conditions. The workflow engine enforces a hard pause at `awaiting_human_review`, forbidding autonomous target repository modification, pull request generation, or code deployment.

---

## 2. Invariants & Safety Boundary

1. **Target Repository Immutability**: The workflow engine operates strictly read-only against the target repository. Target code is never modified, committed, branched, or pushed.
2. **Transition Enforcement in Code**: Legal transitions are declared in `ALLOWED_TRANSITIONS` and enforced by `validate_workflow_transition()`. Prompts cannot override or bypass transition guards.
3. **Mandatory Human Authorization Gate**: Automated transitions halt at `awaiting_human_review`. Advancement to `approved` strictly requires explicit human input (`human_decision="approve"`).
4. **Honest Outcome Attribution**: Transitions to `reproduced` or `verified` strictly require concrete verification evidence; false passes, inconclusive runs, or unavailable sandbox skips cannot fabricate success.
5. **Optimistic Concurrency**: Monotonically increasing `version` fields prevent dirty writes. Concurrent or out-of-order updates fail closed with `WorkflowConcurrencyError`.
6. **Append-Only Event Ledger**: Every transition appends a sequential event record with strict monotonicity (`sequence >= 1`) enforced by database uniqueness constraints (`uq_eng_workflow_events_seq`).
7. **Secret Scrubbing**: All event payloads and stored metadata pass through `redact_secrets()`, scrubbing API keys, tokens, and authorization credentials before writing to disk.

---

## 3. Architecture & State Machine

### 3.1 Lifecycle States

The 19 formal states defined in `EngineeringWorkflowState`:

| Stage | Lifecycle State | Description | Next Allowed States |
|---|---|---|---|
| **Intake & Analysis** | `created` | Workflow initialized from intake spec | `intake_validated`, `rejected` |
| | `intake_validated` | Intake schema and revision verified | `repo_analyzed`, `rejected` |
| | `repo_analyzed` | Repository context assembled | `reproduction_pending`, `repair_pending` (fast-forward if repro evidence exists), `rejected` |
| **Reproduction** | `reproduction_pending` | Queued for isolated reproduction | `reproduced`, `repro_failed`, `repro_blocked`, `repro_inconclusive`, `rejected` |
| | `reproduced` | Defect deterministically reproduced | `repair_pending`, `rejected` |
| | `repro_blocked` | Blocked (e.g. sandbox unavailable) | `reproduction_pending` (resumed), `rejected` |
| | `repro_failed` | Defect failed reproduction (terminal) | *None* |
| | `repro_inconclusive` | Reproduction inconclusive (terminal) | *None* |
| **Repair** | `repair_pending` | Queued for repair planning & patch gen | `verification_pending`, `repair_failed`, `rejected` |
| | `repair_failed` | Repair planning or patch validation failed (terminal) | *None* |
| **Verification & Regression** | `verification_pending` | Queued for patch verification in sandbox | `verified`, `verification_failed`, `verification_blocked`, `rejected` |
| | `verified` | Patch independently verified | `regression_checking`, `awaiting_human_review`, `rejected` |
| | `verification_blocked` | Verification blocked by environment | `verification_pending` (resumed), `rejected` |
| | `verification_failed` | Verification failed (terminal) | *None* |
| | `regression_checking` | Evaluating test suite regressions | `awaiting_human_review`, `regression_failed`, `rejected` |
| | `regression_failed` | Regression detected | `repair_pending` (retry repair), `rejected` |
| **Review & Terminals** | `awaiting_human_review` | Paused for human decision | `approved`, `rejected` |
| | `approved` | Approved by human operator (terminal) | *None* |
| | `rejected` | Rejected by human or system (terminal) | *None* |

### 3.2 Resumable vs. Terminal States

- **Resumable States**: `repro_blocked`, `verification_blocked`, `reproduction_pending`, `repair_pending`, `verification_pending`, `regression_checking`.
- **Terminal States**: `approved`, `rejected`, `repro_failed`, `repro_inconclusive`, `repair_failed`, `verification_failed`. Resumption of terminal states raises `WorkflowResumeError`.

---

## 4. Database Schema & Concurrency Control

Three tables support the workflow ledger in SQLite:

1. **`workflow_intakes`**:
   - `intake_id` (PRIMARY KEY)
   - `source_type`, `target_repo`, `base_commit_sha`, `title`, `description`
   - `affected_files_json`, `intake_json`, `created_at`

2. **`engineering_workflows`**:
   - `workflow_id` (PRIMARY KEY)
   - `intake_id` (FOREIGN KEY REFERENCES `workflow_intakes`)
   - `target_repo`, `base_commit_sha`, `state`
   - `context_id`, `repro_case_id`, `repair_plan_id`, `verification_attempt_id`
   - `regression_summary_json`, `human_decision`, `human_notes`, `error_message`
   - `version` (INTEGER NOT NULL DEFAULT 1)
   - `workflow_json`, `created_at`, `updated_at`

3. **`engineering_workflow_events`**:
   - `event_id` (PRIMARY KEY)
   - `workflow_id` (FOREIGN KEY REFERENCES `engineering_workflows`)
   - `sequence` (INTEGER NOT NULL)
   - `from_state`, `to_state`, `stage`, `payload_json`, `created_at`
   - Constraint: `UNIQUE(workflow_id, sequence)`

### Optimistic Concurrency Mechanism

Updates execute with `WHERE workflow_id = ? AND version = ?`. If another transaction advanced the version in the interim, `cur.rowcount == 0` causes SQLite transaction rollback and raises `WorkflowConcurrencyError`.

---

## 5. CLI Usage & Verification Examples

### 5.1 Initialize and Start Workflow

```bash
forge workflow start --intake intake_spec.json [--repo /path/to/repo] [--auto-advance]
```

Output:
```markdown
# Engineering Workflow `wf_eng_38b1f8ac9210`
- **State**: `repo_analyzed`
- **Intake ID**: `intake_issue_001`
- **Target Repo**: `/path/to/repo`
- **Pinned Commit**: `3dc3269063fb36d0faa208046036975571bd7ade`
- **Version**: `3`
- **Context ID**: `ctx_intake_issue_001_a9f38102`

## Event Ledger Timeline
- `#01` [2026-10-27T04:20:00+00:00] `init`: `None` → `created`
- `#02` [2026-10-27T04:20:01+00:00] `intake_validation`: `created` → `intake_validated`
- `#03` [2026-10-27T04:20:02+00:00] `context_assembly`: `intake_validated` → `repo_analyzed`
```

### 5.2 Inspect Status

```bash
forge workflow status wf_eng_38b1f8ac9210
```

### 5.3 List Workflows

```bash
forge workflow list [--state awaiting_human_review] [--intake-id intake_issue_001]
```

### 5.4 Resume Blocked Workflow

```bash
forge workflow resume wf_eng_38b1f8ac9210
```

Transitions `repro_blocked` back to `reproduction_pending` or `verification_blocked` to `verification_pending`.

### 5.5 Submit Human Review

```bash
forge workflow review wf_eng_38b1f8ac9210 --decision approve --notes "Verified fix offline"
```

Exit code: `0` on approve, `1` on reject.

---

## 6. Verification Summary

The test suite `tests/unit/test_phase5_milestone2.py` verifies:
- Complete happy-path pipeline transitions from `created` to `approved`.
- Illegal state skipping and terminal state transitions rejected without state corruption.
- Strict human review gate blocking unauthenticated transition to `approved`.
- Honest reproduction and verification guards blocking fabricated passes.
- Transactional rollback and optimistic concurrency conflict detection (`WorkflowConcurrencyError`).
- Append-only event ledger ordering, sequential monotonicity, and automatic secret redaction.
- Crash recovery and unblocking of blocked states.
- Absolute target repository immutability throughout workflow lifecycles.
- CLI commands (`start`, `status`, `list`, `resume`, `review`).
