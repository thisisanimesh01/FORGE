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
- **Controlled Repair Mutation Safeguard & Hardened Reconciliation**:
  - A workflow interrupted during `REPAIR_EXECUTING` has an uncertain workspace state.
  - The engine sets `reconciliation_required = True` and transitions the workflow to `BLOCKED`.
  - Automated progression is prohibited until an authorized human operator inspects the workspace and issues `forge orchestrate reconcile <wf_id> --resolution <confirm|discard|revert>`.
  - **Resolution Semantics**:
    - `confirm`: Strictly requires concrete evidence of completed execution prior to interruption (e.g. stage record completion, `execution_status == "applied"`, or completed idempotency record). It **never** re-executes mutations. Transitions directly to `REPAIR_COMPLETED`. Fails closed (`RecoveryValidationError`) if completion evidence is missing.
    - `discard` / `revert`: Marks in-flight/uncertain idempotency records as `FAILED`, cleans up unexecuted stage records, resets state to `AWAITING_HUMAN_APPROVAL`, and permanently preserves approval consumption in `ApprovalLedger` so the consumed approval can never be re-used. A new repair plan requires fresh human authorization.
  - **Operator Authentication & Allowlist Invariants**:
    - **Mandatory Allowlist Enforcement**: Production reconciliation strictly requires an explicitly configured, non-empty trusted approver allowlist (configured on workflow context or via `FORGE_TRUSTED_APPROVERS`). Missing, empty, or whitespace-only allowlists fail closed (`RecoveryValidationError`).
    - **Operator Identity & Principal Binding**: Both the human approver identity (`reconciled_by`) and the operating process principal (`operator_principal`) must be explicit members of the trusted allowlist. A caller-supplied identity string alone cannot establish authorization.
    - **Synthetic/Autonomous Identity Rejection**: Synthetic, AI, bot, and empty identities (`auto`, `llm`, `ai`, `bot`, `synthetic`, `none`, `unknown`, etc.) are rejected fail-closed.
    - **Architectural Tool Calling Boundary**: Autonomous `ToolRegistry` callers (`auth_channel="autonomous_tool"`) cannot reconcile interrupted mutations. Interrupted workspace mutations strictly require an authenticated human operator via interactive CLI.
    - **Interactive Operator Boundary**: CLI reconciliation (`forge orchestrate reconcile`) requires an interactive terminal session (TTY) or an explicit operator confirmation flag (`--yes`). Non-interactive sessions without `--yes` fail closed.
    - **Durable Audit Trail & Tampering Defense**: Reconciliation records durably capture `reconciled_by`, `operator_principal`, and `auth_channel`. `inspect_recovery()` and `verify_reconciliation_integrity()` audit these records against the trusted allowlist; database tampering immediately triggers `AuditTrailTamperedError`.
    - Automated discard bypass flags (`allow_discard_unconfirmed_mutation`) are strictly rejected.

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
- **At-Most-Once Enforcement & Concurrency Hardening**:
  - `register_start`: Registers operation in state `STARTED` inside a `BEGIN IMMEDIATE;` SQLite transaction with unique constraint handling to prevent concurrent execution races. Fails if already `STARTED` or `UNCERTAIN`.
  - `mark_completed`: Caches validated execution outcome and result payload. Subsequent calls return cached results without re-executing. Corrupted cached payloads fail closed with `CorruptedStateError`.
  - `mark_uncertain`: Interrupted operations are marked `UNCERTAIN`, requiring operator reconciliation.

---

## 4. CLI & Tool Interfaces

### 4.1 CLI Commands (`forge orchestrate`)
- `forge orchestrate inspect <wf_id>`: Inspect workflow recovery decision, checkpoint, event count, and reconciliation status.
- `forge orchestrate recover <wf_id>`: Resume an interrupted workflow from its last verified checkpoint.
- `forge orchestrate tasks <wf_id>`: Inspect coordinated task DAG, priorities, readiness, and dependencies.
- `forge orchestrate reconcile <wf_id> --resolution <confirm|discard|revert> [--operator <user>] [--yes] [--non-interactive]`: Human operator unblocking command enforcing authentic operator identity, allowlist verification, and interactive boundary.

### 4.2 ToolRegistry Tools
- `inspect_workflow_recovery`: Read-only inspection of recovery state.
- `recover_orchestration_workflow`: Reconstruct and recover an interrupted orchestrator instance.
- `inspect_workflow_tasks`: Read-only query of task DAG status and ready task lists.
- `reconcile_orchestration_mutation`: Exposed tool that strictly rejects autonomous tool callers fail closed with instructions to use interactive CLI.

---

## 5. Verification & Security Audit Suite

The verification suite in `tests/unit/test_phase13_m2_autonomy_recovery.py` contains 65 dedicated tests (including 28 adversarial security and authorization consistency audit tests):
1. **Crash-Injection at 6 Lifecycle Points**:
   - Point 1: Crash before patch application $\rightarrow$ cleanly unapplied, reconciliation required.
   - Point 2: Crash during patch application $\rightarrow$ unconfirmed mutation fails closed, unverified confirm rejected.
   - Point 3: Crash after patch application before state persistence $\rightarrow$ unblocking requires verified evidence or discard.
   - Point 4: Crash after persistence before transition $\rightarrow$ recovery validates completed stage record.
   - Point 5: Crash during rollback $\rightarrow$ fails closed with reconciliation required.
   - Point 6: Crash during operator reconciliation $\rightarrow$ preserves reconciliation requirement across process restarts.
2. **Approval Consumption Persistence Across Restarts & Discard**:
   - Consumed approvals cannot be re-applied after crash recovery.
   - Operator `discard` preserves consumed state in `ApprovalLedger`, requiring fresh human authorization for new plans.
3. **Idempotency Concurrency & Corruption Defense**:
   - Concurrent `register_start` invocations fail closed via `BEGIN IMMEDIATE` transactions.
   - Corrupted or malformed cached payloads raise `CorruptedStateError` fail-closed.
4. **Authorization Consistency Audit Suite (12 Invariants)**:
   - Missing trusted approver allowlist during confirm fails closed.
   - Empty trusted approver allowlist during confirm fails closed.
   - Missing trusted approver allowlist during discard fails closed.
   - Empty trusted approver allowlist during revert fails closed.
   - Unauthorized operator identity fails closed across all operations.
   - Caller-supplied identity string without authentic operator principal context fails closed.
   - Non-interactive CLI reconciliation without `--yes` fails closed.
   - ToolRegistry autonomous reconciliation attempts fail closed and block workflow.
   - Valid interactive authorized operator reconciliation persists complete identity context.
   - Reconciliation authorization records persist and verify cleanly across restarts.
   - Concurrent unauthorized and authorized reconciliation execution is safe and fail-closed.
   - Direct database tampering with reconciliation identity records triggers `AuditTrailTamperedError`.
