# Phase 13 Specification: Autonomous Engineering Orchestration

## Overview

Phase 13 establishes the overarching orchestration layer for FORGE, unifying the discrete defect diagnosis, root-cause analysis, repair planning, controlled repair execution, repair verification, and quality evaluation engines developed in Phase 12 into a single deterministic, auditable, and human-supervised engineering pipeline.

While Phase 12 proved that individual stages can execute safely, autonomously, and with rigorous isolation, Phase 13 provides the orchestration fabric that guarantees:
- **Deterministic Forward Progression**: Workflows transition strictly through well-defined lifecycle states governed by an explicit state transition matrix.
- **Fail-Closed Gatekeeping**: Every handoff between stages is governed by strict evidence validation, cryptographic digest matching, and automated safety gates.
- **Non-Negotiable Human Oversight Boundary**: No repair patch can be applied without explicit human approval bound to a cryptographic digest of the exact plan, strategy, and file scope.
- **Permanent Safety Invariant (`requires_human_review = True`)**: Hardcoded across all workflow models, results, and reports, ensuring autonomous execution never triggers unverified commits, merges, or software deployments.
- **Auditability & Cryptographic Provenance**: Every stage transition, tool call, approval decision, and gate evaluation is recorded in an append-only event ledger chained with SHA-256 hashes.
- **Target Repository Immutability**: All modifications and test executions are strictly confined to isolated scratch workspaces; target repositories remain bit-for-bit immutable.

---

## Phase 13 Roadmap

- **Milestone 1**: End-to-End Engineering Orchestration `[COMPLETED]`
  - Unified orchestration engine (`EngineeringOrchestrator`) coordinating the full lifecycle across Phase 12 M1–M6 capabilities.
  - Explicit 18-state lifecycle state machine with zero illegal shortcuts or skips.
  - Strict human approval boundary with canonical SHA-256 plan digest binding, strategy validation, bidirectional file scope allowlisting, and durable SQLite replay protection.
  - Fail-closed verification and quality gate integration blocking any workflow that fails checks or quality criteria.
  - Cryptographic tamper-evident SHA-256 event chaining with automatic secret redaction.
  - Production stage adapters and deterministic test fakes.
  - Comprehensive markdown engineering dossier generator (`EngineeringReportGenerator`).
  - 6 typed orchestration tools registered in `ToolRegistry` (bringing total tools to 65).
  - CLI integration via `forge orchestrate run`, `status`, and `report`.
  - Dedicated milestone documentation in [`docs/milestones/phase-13-m1-engineering-orchestration.md`](../milestones/phase-13-m1-engineering-orchestration.md).

- **Milestone 2**: Multi-Defect Batch Orchestration & Resource Isolation `[PLANNED]`
- **Milestone 3**: Interactive Human Approval Console & Rich Diffs `[PLANNED]`
- **Milestone 4**: Automated Rollback Recovery & Fault Tolerance `[PLANNED]`
- **Milestone 5**: Enterprise Telemetry, Metrics & Observability `[PLANNED]`
- **Milestone 6**: Supervised Release Gate & Acceptance Verification `[PLANNED]`

---

## 1. Explicit 18-State Lifecycle State Machine

The engineering lifecycle is governed by a formal finite state machine defined in `app/forge/orchestration/state.py`. Every transition must satisfy explicit prerequisite invariants.

### Lifecycle States

1. `CREATED`: Initial workflow instantiated; parameters validated.
2. `DIAGNOSING`: Collecting failure signals, ranking candidate locations, formulating hypotheses.
3. `DIAGNOSIS_COMPLETED`: Candidate defect identified, grounded evidence verified.
4. `ROOT_CAUSE_ANALYSIS`: Tracing causal chains, analyzing call graphs, inspecting AST guards.
5. `ROOT_CAUSE_COMPLETED`: Causal explanation produced with verified certainty status.
6. `PLANNING`: Synthesizing candidate repair strategies, analyzing risk, preparing verification plan.
7. `PLAN_READY`: Deterministic repair plan synthesized with ranked strategies and rollback triggers.
8. `AWAITING_HUMAN_APPROVAL`: Mandatory human approval gate; execution blocked until authorization is received.
9. `APPROVED`: Validated approval received and consumed; bound to plan digest and file scope.
10. `REPAIR_EXECUTING`: Isolated workspace provisioned, baseline snapshotted, patch applied in-memory.
11. `REPAIR_COMPLETED`: Patch applied successfully, workspace integrity verified, verification pending.
12. `VERIFICATION_RUNNING`: Targeted and regression checks executing in container sandbox against baseline.
13. `VERIFICATION_COMPLETED`: Test results parsed, regression analysis completed (`NO_REGRESSION_OBSERVED`).
14. `QUALITY_EVALUATION`: Evaluating 6 quality dimensions, complexity deltas, and historical reliability.
15. `COMPLETED`: Terminal success; repair verified, quality gates passed, engineering dossier generated.
16. `BLOCKED`: Terminal blocked; verification regression, quality gate failure, or approval rejection.
17. `FAILED`: Terminal failure; unhandled error, timeout, or workspace corruption.
18. `CANCELLED`: Terminal cancellation; operator explicitly aborted the workflow.

### Transition Matrix (`ALLOWED_TRANSITIONS`)

```mermaid
flowchart TD
    CREATED --> DIAGNOSING
    DIAGNOSING --> DIAGNOSIS_COMPLETED
    DIAGNOSIS_COMPLETED --> ROOT_CAUSE_ANALYSIS
    DIAGNOSIS_COMPLETED --> PLANNING
    ROOT_CAUSE_ANALYSIS --> ROOT_CAUSE_COMPLETED
    ROOT_CAUSE_COMPLETED --> PLANNING
    PLANNING --> PLAN_READY
    PLAN_READY --> AWAITING_HUMAN_APPROVAL
    AWAITING_HUMAN_APPROVAL --> APPROVED
    AWAITING_HUMAN_APPROVAL --> BLOCKED
    APPROVED --> REPAIR_EXECUTING
    REPAIR_EXECUTING --> REPAIR_COMPLETED
    REPAIR_COMPLETED --> VERIFICATION_RUNNING
    VERIFICATION_RUNNING --> VERIFICATION_COMPLETED
    VERIFICATION_COMPLETED --> QUALITY_EVALUATION
    QUALITY_EVALUATION --> COMPLETED
    QUALITY_EVALUATION --> BLOCKED
    VERIFICATION_COMPLETED --> BLOCKED

    %% Failure transitions
    DIAGNOSING --> FAILED
    ROOT_CAUSE_ANALYSIS --> FAILED
    PLANNING --> FAILED
    REPAIR_EXECUTING --> FAILED
    VERIFICATION_RUNNING --> FAILED
    QUALITY_EVALUATION --> FAILED

    %% Cancellation transitions
    CREATED --> CANCELLED
    AWAITING_HUMAN_APPROVAL --> CANCELLED
```

### Transition Validation Rules
- **Non-Bypassable States**: Transitioning directly from `PLAN_READY` to `REPAIR_EXECUTING` raises `IllegalStateTransitionError`.
- **Terminal State Guard**: Transitioning from any terminal state (`COMPLETED`, `BLOCKED`, `FAILED`, `CANCELLED`) raises `TerminalStateError`.
- **Scope-Preserving Handoffs**: Every state update records an immutable transition event with the originating and target states.

---

## 2. Stage Adapters & Data Contracts

The orchestration engine decouples workflow coordination from individual stage implementations using the abstract `StageAdapter` interface:

```python
class StageAdapter(ABC):
    @abstractmethod
    def execute(self, stage: WorkflowStage, context: WorkflowContext) -> StageOutput:
        """Execute the stage and return a typed StageOutput."""
```

### Production Stage Adapters
1. **`ProductionDiagnosisAdapter`**: Invokes `FailureLocalizer`, `HypothesisEngine`, and `DefectClassificationEngine`. Enforces strict character budgets across failure signals.
2. **`ProductionRootCauseAdapter`**: Invokes `CausalTracer` and `ASTGuardInspector` over cross-file call and module graphs. Produces `RootCauseReport` with explicit certainty ratings.
3. **`ProductionPlanningAdapter`**: Invokes `RepairStrategyGenerator`, `RepairRiskAnalyzer`, and `RepairPlanner`. Produces `RepairPlan` with `approval_required = True` and canonical SHA-256 digest.
4. **`ProductionExecutionAdapter`**: Allocates isolated scratch workspace, snapshots baseline tree, validates in-memory unified diffs, and applies edits without host shell calls.
5. **`ProductionVerificationAdapter`**: Runs targeted and regression test checks within container sandboxes against immutable baseline snapshots. Evaluates regression classifications fail-closed.
6. **`ProductionQualityAdapter`**: Computes multi-dimensional quality scores, evaluates AST complexity deltas, queries SQLite historical stability, and checks 10 non-negotiable release gates.

### Deterministic Test Fakes
The test suite utilizes complete, standalone fake adapters (`FakeDiagnosisAdapter`, `FakeRootCauseAdapter`, etc.) that produce fully compliant Pydantic domain models without external dependencies, ensuring 100% deterministic unit testing and microbenchmark evaluation.

---

## 3. Human Approval Contract & Durable Replay Protection

The boundary between planning (Stage 3) and repair execution (Stage 4) is guarded by the `OrchestrationApprovalManager`:

### Verification Rules
1. **Plan Digest Match**: Canonical SHA-256 digest of the repair plan must match `approval.repair_plan_digest` bit-for-bit.
2. **Strategy Alignment**: `approval.strategy_id` must match `plan.selected_strategy.strategy_id`.
3. **Bidirectional Scope Enforcement**:
   - *Scope Breach Check*: Every file modified by the repair plan must appear in `approval.approved_files`.
   - *Scope Expansion Check*: The approval must not grant permissions for files not touched by the repair plan.
4. **Temporal Expiration**: Current UTC timestamp must be prior to `approval.expires_at`.
5. **Active Revocation Check**: The approval ID must not be present in `_revoked_approvals`.
6. **Durable Replay Prevention**: The approval ID is verified against the SQLite `ApprovalLedger` to ensure it has not been previously consumed by another execution.
7. **Atomic State Handoff**: Upon successful verification, the approval is atomically marked consumed in the ledger before transitioning the workflow state to `APPROVED`.

---

## 4. Fail-Closed Verification & Quality Lifecycle

The orchestrator enforces strict fail-closed criteria before transitioning a workflow to `COMPLETED`:

- **Verification Gate**:
  - If `VerificationResult.verification_status != PASSED`, or `regression_classification == REGRESSION_DETECTED`, the workflow immediately transitions to `BLOCKED`.
  - Unverified fixes are strictly prevented from advancing to quality evaluation.
- **Quality Gate**:
  - If `RepairQualityResult.quality_status != PASSED`, or any critical quality gate fails, the workflow transitions to `BLOCKED`.
  - Score clamping ensures regressions or high-risk modifications cannot pass under any circumstances.
- **Completion Validation (`EngineeringWorkflowResult`)**:
  - Pydantic root validator ensures that a workflow with status `COMPLETED` MUST have `verification_status == PASSED` and `regression_classification` in `("NO_REGRESSION_OBSERVED", "IMPROVED")`.
  - Rejects any attempt to construct a `COMPLETED` result with failed verification.

---

## 5. Cryptographic Event Chaining & Engineering Dossier

### Event Chaining (`OrchestrationEventRecorder`)
Every significant action in the workflow is captured as an `OrchestrationEvent`:
- Links to the previous event via `prev_event_hash` (initial event links to `0` * 64).
- Computes `event_hash = sha256(prev_event_hash + sequence + event_type + timestamp + payload)`.
- Automatically scrubs sensitive credentials and API tokens via `redact_secrets()`.
- Verified at any time using `verify_event_chain(events)` with sequence and hash continuity checks.

### Engineering Dossier Generator (`EngineeringReportGenerator`)
Synthesizes a publication-ready Markdown report including:
- Executive summary with run identifiers and status.
- State progression timeline with microsecond timestamps.
- Grounded diagnosis and candidate failure locations.
- Root cause causal chain and certainty classification.
- Repair plan summary, selected strategy, and risk assessment.
- Human approval audit trail and SHA-256 digest bindings.
- Patch statistics, hunk counts, and modified files.
- Verification test suite results and regression comparison.
- Quality score radar breakdown and gatekeeper determinations.
- Immutable safety notice: `requires_human_review = True`.

---

## 6. ToolRegistry Integration

The orchestration engine exposes 6 typed, read-only tools to FORGE's `ToolRegistry`:

| Tool Name | Input Schema | Description |
|:---|:---|:---|
| `start_orchestrated_repair` | `OrchestrationStartRequest` | Initialize and begin a new end-to-end engineering workflow. |
| `submit_orchestration_approval` | `OrchestrationApprovalRequest` | Submit human approval to authorize repair execution. |
| `step_orchestration_workflow` | `OrchestrationStepRequest` | Advance an active workflow by a single stage. |
| `get_orchestration_status` | `OrchestrationStatusRequest` | Inspect current workflow state, active stage, and summary. |
| `get_orchestration_audit_trail` | `OrchestrationAuditRequest` | Retrieve and verify cryptographic event chain. |
| `generate_orchestration_report` | `OrchestrationReportRequest` | Generate full markdown engineering dossier. |

---

## 7. Command-Line Interface

Integrated into `forge cli` under the `orchestrate` subcommand group:

```bash
# Run end-to-end workflow
forge orchestrate run --repo /path/to/repo --desc "Fix KeyError in config parser" --auto-approve

# Inspect workflow status
forge orchestrate status <workflow_id>

# Generate full engineering report
forge orchestrate report <workflow_id> --format markdown
```

---

## 8. Empirical Performance Benchmarks

Microbenchmarking conducted across 1,000+ iterations:

| Operation | Scale / Iterations | Total / Mean Latency | Throughput |
|:---|:---|:---|:---|
| State Transition Validation | 7,000 transitions | 3.70 ms | ~1,891,891 ops/sec |
| Event Recording & Hashing | 200 events | 13.56 ms | ~14,749 ops/sec |
| Event Chain Verification | 200 events | 6.92 ms | ~28,901 ops/sec |
| Human Approval Validation | 500 validations | 62.66 ms | ~7,979 ops/sec |
| End-to-End Workflow Execution | 50 workflows | 97.98 ms | ~1.96 ms / workflow |
| Peak Memory Overhead | 50 workflows | 478.89 KB | Negligible footprint |
