# Phase 13, Milestone 2: Agent Autonomy, Coordination & Recovery

**Milestone Report & Technical Verification**<br>
**Repository:** `FORGE`<br>
**Milestone:** Phase 13 M2 — Agent Autonomy, Coordination & Recovery<br>
**Status:** Completed & Verification Gate Passed<br>
**Branch:** `phase-13-m2-agent-autonomy-recovery`<br>
**Target:** `main`

---

## 1. Executive Summary

Phase 13 Milestone 2 delivers the **Agent Autonomy, Coordination & Recovery Engine** for FORGE. Building upon the Phase 13 M1 end-to-end orchestration pipeline, Milestone 2 equips FORGE to handle long-running engineering workflows, survive abrupt process terminations and reboots, resolve complex multi-task dependency graphs, recover deterministically from transient faults, and protect against unintended or duplicate mutations.

Key capabilities introduced and verified:
1. **Durable State Persistence (`forge.orchestration.persistence`)**: Full workflow state, cryptographic audit trail, per-stage execution records, stage outputs, and recovery checkpoints are durably stored in SQLite under WAL mode with foreign key constraints and schema migration tracking (`orchestration_schema_migrations`).
2. **Crash & Interruption Recovery (`forge.orchestration.recovery`)**: Deterministic reconstitution of in-flight workflows from SQLite checkpoints. Validates cryptographic SHA-256 event chains (`prev_event_hash -> event_hash`), enforces repository root path binding, and guarantees fail-closed blocking if the audit trail is tampered with or incomplete.
3. **Fail-Closed Mutation Reconciliation**: Workflows interrupted during controlled repair execution (`REPAIR_EXECUTING`) are automatically blocked with `reconciliation_required = True`. Blind re-execution is forbidden; an authorized operator must inspect the isolated workspace and resolve the mutation via `forge orchestrate reconcile <wf_id> --resolution <confirm|discard|revert>`.
4. **Preserved Human Authorization Gate**: Interrupted workflows in `AWAITING_HUMAN_APPROVAL` remain safely awaiting human operator review across restarts. FORGE strictly prohibits synthetic or automatic self-approval.
5. **Bounded Deterministic Retry Engine (`forge.orchestration.retry`)**: Implements a 9-category deterministic failure classifier (`FailureClassifier`) and bounded retry policy (`RetryPolicy`). Non-retryable failures (authorization errors, policy breaches, integrity violations, and mutating operations) fail closed immediately. Retryable transient failures (infrastructure blips, timeouts) back off exponentially with hard caps (max 5 attempts, max 30s delay).
6. **Task DAG Coordination (`forge.orchestration.coordination`)**: Multi-task directed acyclic graph coordination (`TaskCoordinator`) featuring Kahn's algorithm cycle detection (`DependencyCycleError`), priority-based scheduling, automatic failure propagation (`BLOCKED`), and cascading cancellation.
7. **At-Most-Once Idempotency Ledger (`forge.orchestration.idempotency`)**: Canonical SHA-256 idempotency keying and state tracking (`STARTED`, `COMPLETED`, `FAILED`, `UNCERTAIN`). Replays completed outcomes directly from cache while rejecting concurrent duplicate runs with `IdempotencyConflictError`.
8. **Expanded CLI & ToolRegistry**: Added 4 new CLI subcommands (`forge orchestrate inspect`, `recover`, `tasks`, `reconcile`) and 3 new typed tools (`inspect_workflow_recovery`, `recover_orchestration_workflow`, `inspect_workflow_tasks`), bringing the total tool count to 68.

---

## 2. Architecture & Component Layout

```
app/forge/orchestration/
├── __init__.py           # Package exports for persistence, retry, idempotency, coordination, recovery
├── schemas.py            # M1 & M2 Pydantic schemas (InspectWorkflowRecovery, RecoverWorkflow, InspectWorkflowTasks)
├── state.py              # 18-state lifecycle state machine and transition validators
├── events.py             # Event types (RECOVERY_*, RETRY_*, TASK_*) & SHA-256 hash chaining
├── persistence.py        # OrchestrationStateStore: SQLite WAL, schema migrations, stage records
├── retry.py              # FailureClassifier, FailureCategory, RetryPolicy, RetryDecision
├── idempotency.py        # IdempotencyLedger, canonical SHA-256 keys, OperationStatus
├── coordination.py       # TaskCoordinator, TaskRecord, Kahn's algorithm cycle detection
├── recovery.py           # WorkflowRecoveryEngine, RecoveryDecision, reconciliation enforcement
├── approval.py           # OrchestrationApprovalManager & SQLite ApprovalLedger
├── adapters.py           # Abstract stage adapters & test fakes
├── orchestrator.py       # Integrated orchestrator with persistence, retry, and idempotency hooks
├── report.py             # EngineeringReportGenerator synthesizing comprehensive dossiers
└── tools.py              # ToolRegistry tools (65 -> 68 tools)
```

---

## 3. Empirical Performance Benchmarks

Measured via `forge.benchmarks.benchmark_phase13_m2_autonomy`:

| Benchmark Operation | Target Budget | Observed Result | Status |
|:---|:---|:---|:---|
| **Persistence (100 workflows + checkpoints)** | < 1,000 ms | **303.22 ms** | PASSED |
| **Persistence Reads (100 workflows)** | < 500 ms | **129.15 ms** | PASSED |
| **Recovery Inspection (50 workflows)** | < 300 ms | **96.49 ms** | PASSED |
| **DAG Resolution (100 tasks with Kahn)** | < 300 ms | **125.16 ms** | PASSED |
| **Idempotency Operations (500 ops)** | < 1,000 ms | **574.14 ms** | PASSED |
| **Retry Deterministic Evaluation (1,000 ops)** | < 50 ms | **14.41 ms** | PASSED |
| **Total Benchmark Time** | < 5,000 ms | **1,250.33 ms** | PASSED |
| **Peak Memory Overhead** | < 5 MB | **52.38 KB** | PASSED |

---

## 4. Verification & Scenario Matrix

The test suite in `tests/unit/test_phase13_m2_autonomy_recovery.py` contains 37 tests covering:
- **Unit Invariants**: SQLite table creations, foreign keys, migration versions, deterministic failure classification, retry limits, canonical idempotency hashing, and Kahn's algorithm cycle detection.
- **Scenario 1**: Clean end-to-end execution with full SQLite persistence and checkpointing.
- **Scenario 2**: Crash after diagnosis completed $\rightarrow$ resumes at root cause analysis.
- **Scenario 3**: Crash during repair planning $\rightarrow$ resumes at planning.
- **Scenario 4**: Workflow in `AWAITING_HUMAN_APPROVAL` preserves approval requirement across crash/recovery.
- **Scenario 5**: Crash after approval preserves `APPROVED` state.
- **Scenario 6**: Crash during repair execution (`REPAIR_EXECUTING`) blocks and requires operator reconciliation.
- **Scenario 7**: Operator reconciliation (`confirm`) unblocks workflow to `APPROVED`.
- **Scenario 8**: Operator reconciliation (`discard`) unblocks workflow to `AWAITING_HUMAN_APPROVAL`.
- **Scenario 9**: Duplicate repair execution returns cached result via `IdempotencyLedger` without re-executing.
- **Scenario 10**: Transient failure during verification retried and succeeds within retry bounds.
- **Scenario 11**: Authorization failure fails closed immediately with zero retries.
- **Scenario 12**: Multi-task DAG topological execution according to dependencies and priorities.
- **Scenario 13**: Task DAG failure propagation to downstream dependent tasks.
- **Scenario 14**: Tampered event chain in SQLite blocks recovery with `AuditTrailTamperedError`.
- **ToolRegistry & CLI**: Verified tool schemas and CLI command invocations (`inspect`, `recover`, `tasks`, `reconcile`).
