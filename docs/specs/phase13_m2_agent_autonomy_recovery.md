# Phase 13 M2 Specification: Agent Autonomy, Coordination & Recovery

## 1. System Objective & Scope

Phase 13 Milestone 2 extends FORGE's End-to-End Engineering Orchestration Engine (`forge.orchestration`) with a comprehensive autonomy, coordination, and recovery layer. Long-running software engineering tasks are subject to process terminations, system reboots, transient network/database errors, and multi-stage task dependencies.

Milestone 2 ensures FORGE can:
1. Safely survive and recover from crashes at any point in the engineering lifecycle without corrupting state or duplicating work.
2. Formally preserve the non-negotiable human approval boundary across restarts.
3. Require explicit operator reconciliation before unblocking workflows interrupted during controlled repair execution (`REPAIR_EXECUTING`).
4. Determinisitically classify execution failures and apply bounded retries with exponential backoff while strictly prohibiting blind mutation retries.
5. Coordinate multi-task directed acyclic graphs (DAGs) with topological resolution, cycle detection via Kahn's algorithm, priority scheduling, failure propagation, and cancellation.
6. Enforce at-most-once execution via canonical SHA-256 idempotency ledgers.

---

## 2. Architecture & Module Design

The autonomy, coordination, and recovery subsystem consists of five primary components within `app/forge/orchestration/`:

```
app/forge/orchestration/
├── persistence.py     # OrchestrationStateStore: SQLite WAL persistence & migrations
├── retry.py           # FailureClassifier & RetryPolicy: Deterministic taxonomy & bounded retry
├── idempotency.py     # IdempotencyLedger: Canonical SHA-256 keys & at-most-once safeguards
├── coordination.py    # TaskCoordinator: DAG scheduling, Kahn's algorithm cycle detection
├── recovery.py        # WorkflowRecoveryEngine: Crash recovery, hash verification, mutation reconciliation
├── events.py          # Extended event types & cryptographic hash chaining
├── schemas.py         # M2 schemas (InspectWorkflowRecovery, RecoverWorkflow, InspectWorkflowTasks)
├── orchestrator.py    # Integrated persistence, retry, and idempotency hooks
└── tools.py           # 3 new ToolRegistry tools (total: 68 tools)
```

---

## 3. Component Specifications

### 3.1 Durable State Persistence (`persistence.py`)

- **Schema Management**: Managed under SQLite table `orchestration_schema_migrations` with initial schema version `1`.
- **Database Tables**:
  - `orchestration_workflows`: Workflow metadata, state, timestamps, trusted approvers, reconciliation flags.
  - `orchestration_events`: Cryptographic audit trail with foreign key enforcement to `orchestration_workflows`.
  - `orchestration_stage_records`: Per-stage execution status, timestamps, attempt counters, and error diagnostics.
  - `orchestration_stage_outputs`: Serialized stage outputs and results.
  - `orchestration_recovery_checkpoints`: Checkpoints capturing verified state, stage, sequence, and digest.
  - `orchestration_tasks`: Task DAG specifications, prerequisites, priorities, states, and failure reasons.
  - `orchestration_idempotency_records`: Canonical operation tracking for at-most-once execution.
  - `orchestration_mutation_reconciliations`: Operator reconciliation records for interrupted mutations.
- **Transactional Integrity**: All multi-statement mutations execute in SQLite transactions with WAL mode and foreign key constraints enabled.

### 3.2 Crash & Interruption Recovery Engine (`recovery.py`)

- **State Reconstitution**: Restores workflow context, stage execution records, and deserialized outputs from SQLite.
- **Cryptographic Event Chain Verification**: Inspects the entire `orchestration_events` chain for the target workflow. If any sequence gap or hash mismatch is detected (`prev_event_hash != prev.event_hash`), recovery immediately aborts with `AuditTrailTamperedError`.
- **Repository Path Binding**: Enforces that recovered workflows cannot execute against a different repository root (`RecoveryValidationError`).
- **Human Gate Invariance**:
  - `AWAITING_HUMAN_APPROVAL` workflows remain awaiting human approval. FORGE strictly refuses to auto-approve on recovery.
  - `APPROVED` workflows remain approved and ready for execution.
- **Controlled Repair Mutation Safeguard**:
  - A workflow interrupted during `REPAIR_EXECUTING` has an uncertain workspace state.
  - The engine sets `reconciliation_required = True` and transitions the workflow to `BLOCKED`.
  - Automated progression is prohibited until an operator inspects the workspace and issues `forge orchestrate reconcile <wf_id> --resolution <confirm|discard|revert>`.

### 3.3 Bounded Deterministic Retry Engine (`retry.py`)

- **Failure Taxonomy**:
  - `TRANSIENT_INFRASTRUCTURE`: SQLite database locked, busy, disk I/O blips (Retryable).
  - `TIMEOUT`: Operation timed out within bounded deadline (Retryable).
  - `SANDBOX_UNAVAILABLE`: Docker daemon temporarily unreachable (Retryable with backoff).
  - `PERMANENT_EXECUTION_FAILURE`: Deterministic compile/test failure (Non-retryable).
  - `AUTHORIZATION_FAILURE`: Approval rejected, expired, missing, or unauthorized (Non-retryable).
  - `INTEGRITY_FAILURE`: Audit trail tampered, hash mismatch, state corrupted (Non-retryable).
  - `POLICY_VIOLATION`: Scope breach, protected path access, invariant breach (Non-retryable).
  - `INVALID_INPUT`: Malformed input or schema validation error (Non-retryable).
  - `UNCERTAIN_MUTATION`: Crash or interrupt during workspace mutation (Non-retryable without reconciliation).
- **Hard Bounds**:
  - Maximum attempts: 5.
  - Maximum delay: 30 seconds.
  - Exponential backoff: `delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)`.
  - Zero blind mutation retries: Mutating operations (Stage 5) strictly fail closed on interruption.

### 3.4 Task DAG Coordination (`coordination.py`)

- **Data Model**: `TaskRecord` tracking `task_id`, `workflow_id`, `stage`, `prerequisite_task_ids`, `priority` (1-100), `state` (`PENDING`, `READY`, `RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`, `CANCELLED`).
- **Cycle Detection**: Kahn's algorithm verifies DAG acyclicity upon registering tasks. Cycles trigger `DependencyCycleError`.
- **Topological Scheduling**: `get_ready_tasks` returns all tasks whose prerequisites are `COMPLETED`, sorted descending by priority.
- **Failure Propagation**: When a prerequisite task fails, all transitive downstream dependents are transitioned to `BLOCKED` with detailed cause tracking.
- **Cancellation**: Supports cascading cancellation across pending/ready tasks in a workflow.

### 3.5 Idempotency Ledger (`idempotency.py`)

- **Canonical Key Generation**: `compute_idempotency_key` constructs SHA-256 hex digests over workflow ID, stage name, input digests, and approval references.
- **At-Most-Once Enforcement**:
  - `register_start`: Registers operation in state `STARTED`. Fails if already `STARTED` or `UNCERTAIN`.
  - `mark_completed`: Caches validated execution outcome and result payload. Subsequent calls return cached results without re-executing.
  - `mark_uncertain`: Interrupted operations are marked `UNCERTAIN`, requiring operator reconciliation.

---

## 4. CLI & Tool Interfaces

### 4.1 CLI Commands (`forge orchestrate`)
- `forge orchestrate inspect <wf_id>`: Inspect workflow recovery decision, checkpoint, event count, and reconciliation status.
- `forge orchestrate recover <wf_id>`: Resume an interrupted workflow from its last verified checkpoint.
- `forge orchestrate tasks <wf_id>`: Inspect coordinated task DAG, priorities, readiness, and dependencies.
- `forge orchestrate reconcile <wf_id> --resolution <confirm|discard|revert> [--notes <text>]`: Human operator unblocking command.

### 4.2 ToolRegistry Tools
- `inspect_workflow_recovery`: Read-only inspection of recovery state.
- `recover_orchestration_workflow`: Reconstruct and recover an interrupted orchestrator instance.
- `inspect_workflow_tasks`: Read-only query of task DAG status and ready task lists.
