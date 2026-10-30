# Phase 6, Milestone 3: Operator Workflow Reliability & Recovery — Specification

- **Milestone:** Phase 6 — M3: Operator Workflow Reliability & Recovery
- **Status:** COMPLETED & VERIFIED
- **Baseline Git Commit:** `53f9e1286be2e431032df5741029c513ce0f9a56`
- **Target Repository Immutability:** STRICTLY ENFORCED (Zero commits, pushes, merges, PRs)
- **Traceability:** Implements Milestone 3 of Phase 6 per `AGENTS.md` (Sections 1, 2, 3, 4, 5, 7) and `docs/specs/phase-6-roadmap.md`.

---

## 1. Executive Summary

Milestone 3 hardens FORGE's supervised engineering workflow lifecycle so that human operators can safely inspect, understand, recover, and decide upon defect repairs across interruptions and operational failures.

Before Milestone 3, while automated repair planning and multi-check verification operated within strict boundaries, workflow execution could encounter friction when processes were interrupted mid-flight (e.g. SIGINT during repair planning or container verification), when commands in user guides (`show`, `reproduce`) diverged from CLI flags (`status`, `repro`), or when stale repository revisions permitted approving an out-of-date candidate patch.

Milestone 3 resolves these operational vulnerabilities by:
1. **Unifying Workflow Inspection (`show` / `status`):** Delivering a clear Markdown dashboard rendering lifecycle state, status category (`[PENDING]`, `[BLOCKED]`, `[AWAITING OPERATOR REVIEW]`, `[APPROVED]`, `[REJECTED]`, `[FAILED]`), actionable next commands, candidate patch details with unified diff and SHA-256 digest, multi-check verification matrix, and monotonic audit timeline.
2. **Hardening Interruption & Idempotent Recovery:** Ensuring workflows interrupted during reproduction (`REPRO_BLOCKED`), repair planning (`REPAIR_PENDING` without plan), verification, or human review (`AWAITING_HUMAN_REVIEW`) can be safely inspected and resumed without losing state or bypassing safety gates.
3. **Enforcing Human Review Decision Integrity:**
   - Requiring verified candidate patch SHA-256 digest match against stored diff prior to approval.
   - Enforcing verification aggregate outcome must be `PASSED` with zero regressions.
   - Intercepting git repository revision drift (`RevisionDriftError`) if the target repository HEAD has shifted away from the pinned `base_commit_sha`.
   - Recording permanent rejection decisions in the event ledger and freezing approved workflows against candidate alteration.
4. **Preserving Security Boundaries:** Guaranteeing fail-closed Docker sandboxing, disabled cloud fallback (`FORGE_ALLOW_CLOUD_FALLBACK=false`), zero host execution, and continuous secret redaction across all events and logs.

---

## 2. Key Architectural Deliverables

### 2.1 Workflow State Machine & Resumability

- **Resumable Review State:** Added `EngineeringWorkflowState.AWAITING_HUMAN_REVIEW` to `RESUMABLE_STATES` in `app/forge/workflow/state.py`. An interrupted operator review process can be safely re-inspected and resumed idempotently without raising `WorkflowResumeError`.
- **Interrupted Repair Planning Recovery:** Updated `plan_repair` in `app/forge/workflow/orchestrator.py` to allow re-entry from `REPAIR_PENDING` when `repair_plan_id` is null, preventing unrecoverable state traps if the process was terminated while invoking local LLM inference.
- **Terminal State Immutability:** Enforced terminal state checks in `attach_repair_plan_to_workflow` (`app/forge/storage/store.py`), raising `InvalidWorkflowTransitionError` if an attempt is made to replace or modify candidates in an approved or rejected workflow.

### 2.2 Decision Verification & Drift Interception

In `orchestrator.review_workflow`:
- **Patch Candidate Integrity:** Validates that `repair_plan.patch_candidate.sha256_digest` exactly matches `compute_patch_sha256(patch_candidate.unified_diff)`. If the diff was modified or corrupted out-of-band, approval is rejected with `ValueError("Candidate patch SHA-256 digest mismatch")`.
- **Verification Gate Requirement:** Verifies that the multi-check verification attempt outcome is `VerificationAggregateOutcome.PASSED` with `regression_detected == False`. Approving unverified, failed, or regressed candidates raises `ValueError`.
- **Target Repository Revision Drift Detection:** When `--repo` is supplied or target git repository is detected, checks current git `HEAD` commit against workflow `base_commit_sha`. If the repository has diverged, raises `RevisionDriftError` and halts approval.
- **Permanent Rejection Recording:** Rejection transitions the workflow to `REJECTED`, recording the operator identity and notes into SQLite `engineering_workflows` and appending `EventType.WORKFLOW_HUMAN_REJECTED` into the immutable event history.

### 2.3 CLI Workflow Inspection Experience

In `app/forge/cli.py`:
- **Command Aliases:** Added `@eng_workflow_app.command(name="show")` (aliasing `status`) and `@eng_workflow_app.command(name="reproduce")` (aliasing `repro`) to align CLI usability with operator expectations.
- **Revision Drift Flag:** Added `--repo` option to `forge workflow review` to support explicit target repository path verification.
- **Rich Dashboard Rendering (`_render_eng_workflow_markdown`):**
  - Status banner with colorized operational status categories:
    - `[AWAITING OPERATOR REVIEW]` (Review Gate Active)
    - `[PENDING]` (Action Required)
    - `[BLOCKED]` (Blocked by External State)
    - `[APPROVED]` (Human Approval Granted)
    - `[REJECTED]` (Operator Rejected)
    - `[FAILED]` (Terminal Execution Failure)
  - Explicit "Next Action" instructions showing the exact shell command to run.
  - Candidate Repair Patch codeblock displaying target file, added/removed lines, SHA-256 digest, and syntax-highlighted unified diff.
  - Multi-Check Verification breakdown table detailing check type, name, status, duration, and diagnostic message.
  - Monotonic Audit Ledger displaying sequential event sequence numbers, timestamps, and transition payloads.

---

## 3. Interruption & Recovery Matrix

| Stage | Interruption Event | Persisted State | Recovery Action (`forge workflow ...`) | Resulting State |
|---|---|---|---|---|
| Reproduction | Container timeout / SIGINT | `REPRO_BLOCKED` | `resume <WF_ID>` or `reproduce <WF_ID>` | `REPRODUCTION_PENDING` $\rightarrow$ Docker reproduction re-executed |
| Repair Planning | Local LLM interruption / crash | `REPAIR_PENDING` (`repair_plan_id=None`) | `plan-repair <WF_ID>` | Re-invokes planner; returns validated plan $\rightarrow$ `REPAIR_PENDING` |
| Verification | Check failure or SIGINT | `REPAIR_PENDING` (`verification_attempt_id=None`) | `verify <WF_ID>` | Re-runs multi-check verification in sandbox $\rightarrow$ `VERIFIED` |
| Human Review | Session disconnect | `AWAITING_HUMAN_REVIEW` | `show <WF_ID>` or `resume <WF_ID>` | Idempotently returns review dashboard $\rightarrow$ `AWAITING_HUMAN_REVIEW` |
| Terminal State | Any subsequent command | `APPROVED` / `REJECTED` | `resume <WF_ID>` | Strictly rejected with `WorkflowResumeError` (fail-closed) |

---

## 4. Test Suite & Verification Results

A comprehensive unit and CLI test suite was created in `tests/unit/test_phase6_milestone3.py` comprising 12 targeted tests:
1. `test_resume_repro_blocked_to_reproduction_pending`: Workflow blocked on reproduction safely resumes to `REPRODUCTION_PENDING` idempotently.
2. `test_resume_interrupted_human_review_workflow`: Workflows in `AWAITING_HUMAN_REVIEW` resume idempotently.
3. `test_resume_terminal_state_strictly_rejected`: Resuming terminal states (`APPROVED`, `REJECTED`, `REPRO_FAILED`, `VERIFICATION_FAILED`) is rejected.
4. `test_plan_repair_resumes_interrupted_repair_pending`: Resuming repair planning from `REPAIR_PENDING` without a plan succeeds idempotently.
5. `test_verify_repair_idempotent_when_already_verified`: Calling `verify_repair` when already verified is idempotent.
6. `test_human_approval_validates_candidate_patch_digest`: Catches tampered or out-of-band corrupted patch candidates.
7. `test_human_approval_blocks_failed_or_regressed_verification`: Strictly blocks approval if verification outcome is not `PASSED`.
8. `test_human_approval_invalidated_by_revision_drift`: Intercepts git target repo commit changes and raises `RevisionDriftError`.
9. `test_approved_workflow_cannot_change_candidate`: Guarantees candidate patch immutability once approved.
10. `test_rejection_recorded_permanently_in_event_ledger`: Verifies rejection persistence and event ledger recording.
11. `test_cli_workflow_show_and_reproduce_aliases`: Validates `show` and `reproduce` CLI command aliases.
12. `test_cli_workflow_inspection_displays_patch_and_verification`: Verifies CLI rendering of candidate diffs, verification tables, and event audit trails.

### Test Execution Summary
- **Targeted M3 Test Suite (`tests/unit/test_phase6_milestone3.py`):** 12 passed in 0.71s.
- **Combined Workflow Suite (`test_phase6_milestone3.py` + `test_phase5_milestone3.py`):** 43 passed in 6.29s.
- **Full Repository Regression Suite (`pytest`):** 970 passed, 3 skipped in 120.60s (zero regressions).
- **Target Repository & Workspace Immutability:** Verified (0 mutations to external repos).
