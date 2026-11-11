"""Phase 13 M1: Engineering Workflow Orchestrator Service.

Coordinates the end-to-end autonomous engineering lifecycle:
Diagnosis -> Root Cause -> Planning -> Human Approval -> Repair Execution ->
Verification -> Quality Evaluation -> Final Engineering Report.
Enforces fail-closed state transitions, cryptographic audit chaining, and
permanent human approval boundaries.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.causality.schemas import CausalStatus, RootCauseReport
from forge.diagnosis.schemas import DiagnosisInput, DiagnosisResult, DiagnosisStatus
from forge.repair_execution.approval import compute_plan_digest
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.schemas import (
    ExecutionStatus,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
)
from forge.repair_planning.schemas import RepairPlan, RepairPlanStatus
from forge.repair_quality.schemas import (
    QualityGateDecision,
    QualityStatus,
    RepairQualityPolicy,
    RepairQualityResult,
)
from forge.repair_verification.schemas import (
    RegressionClassification,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.orchestration.adapters import (
    DefaultDiagnosisAdapter,
    DefaultExecutionAdapter,
    DefaultPlanningAdapter,
    DefaultQualityAdapter,
    DefaultRootCauseAdapter,
    DefaultVerificationAdapter,
    DiagnosisAdapter,
    ExecutionAdapter,
    FakeExecutionAdapter,
    PlanningAdapter,
    QualityAdapter,
    RootCauseAdapter,
    VerificationAdapter,
)
from forge.orchestration.approval import (
    ApprovalVerificationOutcome,
    OrchestrationApprovalError,
    OrchestrationApprovalManager,
)
from forge.orchestration.events import (
    AuditTrailTamperedError,
    OrchestrationEvent,
    OrchestrationEventRecorder,
    OrchestrationEventType,
)
from forge.orchestration.report import EngineeringReportGenerator
from forge.orchestration.schemas import (
    EngineeringWorkflowResult,
    StageExecutionRecord,
    StageOutputs,
    StageStatus,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.orchestration.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    OperationStatus,
    UncertainExecutionError,
    compute_idempotency_key,
)
from forge.orchestration.persistence import CorruptedStateError, OrchestrationStateStore
from forge.orchestration.retry import FailureClassifier, FailureCategory, RetryPolicy, RetryDecision
from forge.orchestration.state import (
    IllegalStateTransitionError,
    InvalidTransitionError,
    TerminalStateError,
    WorkflowStateError,
    is_terminal_state,
    validate_transition,
)


class EngineeringOrchestrator:
    """Orchestrates autonomous software engineering workflows with strict safety invariants."""

    def __init__(
        self,
        context: WorkflowContext,
        event_recorder: Optional[OrchestrationEventRecorder] = None,
        approval_manager: Optional[OrchestrationApprovalManager] = None,
        diagnosis_adapter: Optional[DiagnosisAdapter] = None,
        root_cause_adapter: Optional[RootCauseAdapter] = None,
        planning_adapter: Optional[PlanningAdapter] = None,
        execution_adapter: Optional[ExecutionAdapter] = None,
        verification_adapter: Optional[VerificationAdapter] = None,
        quality_adapter: Optional[QualityAdapter] = None,
        state_store: Optional[OrchestrationStateStore] = None,
        retry_policy: Optional[RetryPolicy] = None,
        idempotency_ledger: Optional[IdempotencyLedger] = None,
    ) -> None:
        self.context = context
        self.workflow_id = context.workflow_id
        self._lock = threading.Lock()

        # State & Records
        self.state = WorkflowState.CREATED
        self.stage_records: Dict[str, StageExecutionRecord] = {}
        self.stage_outputs = StageOutputs()
        self.completed_at: Optional[datetime] = None

        # Components
        self.event_recorder = event_recorder or OrchestrationEventRecorder()
        self.state_store = state_store
        if approval_manager is not None:
            self.approval_manager = approval_manager
        else:
            default_ledger = ApprovalLedger(db_path=state_store.db_path) if state_store else ApprovalLedger()
            self.approval_manager = OrchestrationApprovalManager(
                ledger=default_ledger,
                trusted_approvers=context.trusted_approvers,
            )
        self.retry_policy = retry_policy or RetryPolicy()
        self.idempotency_ledger = idempotency_ledger or (
            IdempotencyLedger(db_path=state_store.db_path) if state_store else None
        )

        # Stage Adapters
        self.diagnosis_adapter = diagnosis_adapter or DefaultDiagnosisAdapter()
        self.root_cause_adapter = root_cause_adapter or DefaultRootCauseAdapter()
        self.planning_adapter = planning_adapter or DefaultPlanningAdapter()
        self.execution_adapter = execution_adapter or DefaultExecutionAdapter()
        self.verification_adapter = verification_adapter or DefaultVerificationAdapter()
        self.quality_adapter = quality_adapter or DefaultQualityAdapter()

        # Emit and persist genesis event if not already recorded
        events = self.event_recorder.get_events(self.workflow_id)
        if not events:
            genesis_event = self.event_recorder.record_event(
                workflow_id=self.workflow_id,
                event_type=OrchestrationEventType.WORKFLOW_CREATED,
                state=self.state,
                action_description=f"Initialized engineering workflow for repository '{context.repository_path}'.",
                details={
                    "repository_path": context.repository_path,
                    "commit_sha": context.commit_sha,
                    "run_id": context.run_id,
                },
            )
            if self.state_store:
                self.state_store.save_workflow(
                    context=self.context,
                    state=self.state,
                    requires_human_review=True,
                )
                self.state_store.save_event(genesis_event)

    # -----------------------------------------------------------------------
    # State Machine Coordination & Checkpointing
    # -----------------------------------------------------------------------

    def _transition(
        self,
        target_state: WorkflowState,
        action_description: str,
        stage: Optional[WorkflowStage] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Validate and apply a lifecycle state transition with persistence and checkpointing."""
        validate_transition(self.state, target_state)
        prev_state = self.state
        self.state = target_state

        event_type = OrchestrationEventType.STAGE_COMPLETED
        if target_state == WorkflowState.COMPLETED:
            event_type = OrchestrationEventType.WORKFLOW_COMPLETED
            self.completed_at = datetime.now(timezone.utc)
        elif target_state == WorkflowState.BLOCKED:
            event_type = OrchestrationEventType.WORKFLOW_BLOCKED
            self.completed_at = datetime.now(timezone.utc)
        elif target_state == WorkflowState.FAILED:
            event_type = OrchestrationEventType.WORKFLOW_FAILED
            self.completed_at = datetime.now(timezone.utc)
        elif target_state == WorkflowState.CANCELLED:
            event_type = OrchestrationEventType.WORKFLOW_CANCELLED
            self.completed_at = datetime.now(timezone.utc)
        elif target_state in (
            WorkflowState.DIAGNOSING,
            WorkflowState.ROOT_CAUSE_ANALYSIS,
            WorkflowState.PLANNING,
            WorkflowState.REPAIR_EXECUTING,
            WorkflowState.VERIFICATION_RUNNING,
            WorkflowState.QUALITY_EVALUATION,
        ):
            event_type = OrchestrationEventType.STAGE_STARTED

        ev = self.event_recorder.record_event(
            workflow_id=self.workflow_id,
            event_type=event_type,
            state=self.state,
            action_description=action_description,
            stage=stage,
            details=details or {},
        )

        if self.state_store:
            active_plan_id = self.stage_outputs.repair_plan.plan_id if self.stage_outputs.repair_plan else None
            active_appr_id = self.stage_outputs.approval.approval_id if self.stage_outputs.approval else None
            self.state_store.save_workflow(
                context=self.context,
                state=self.state,
                current_stage=stage,
                active_repair_plan_id=active_plan_id,
                active_approval_id=active_appr_id,
                completed_at=self.completed_at,
                requires_human_review=True,
            )
            self.state_store.save_event(ev)

            # Record checkpoint on milestone transitions
            if target_state in (
                WorkflowState.DIAGNOSIS_COMPLETED,
                WorkflowState.ROOT_CAUSE_COMPLETED,
                WorkflowState.PLAN_READY,
                WorkflowState.AWAITING_HUMAN_APPROVAL,
                WorkflowState.APPROVED,
                WorkflowState.REPAIR_COMPLETED,
                WorkflowState.VERIFICATION_COMPLETED,
                WorkflowState.COMPLETED,
            ):
                self.state_store.save_checkpoint(
                    workflow_id=self.workflow_id,
                    stage=stage or WorkflowStage.DIAGNOSIS,
                    state=target_state,
                    last_event_sequence=ev.sequence,
                    last_event_hash=ev.event_hash,
                    outputs_digest=self._compute_outputs_digest(),
                )

    def _compute_outputs_digest(self) -> str:
        """Compute SHA-256 digest of current stage outputs."""
        keys: List[str] = []
        if self.stage_outputs.diagnosis_result:
            keys.append(f"diag:{self.stage_outputs.diagnosis_result.diagnosis_id}")
        if self.stage_outputs.root_cause_report:
            keys.append(f"rca:{self.stage_outputs.root_cause_report.report_id}")
        if self.stage_outputs.repair_plan:
            keys.append(f"plan:{self.stage_outputs.repair_plan.plan_id}")
        if self.stage_outputs.approval:
            keys.append(f"appr:{self.stage_outputs.approval.approval_id}")
        if self.stage_outputs.execution_result:
            keys.append(f"exec:{self.stage_outputs.execution_result.execution_status.value}")
        if self.stage_outputs.verification_result:
            keys.append(f"verif:{self.stage_outputs.verification_result.verification_id}")
        if self.stage_outputs.quality_result:
            keys.append(f"qual:{self.stage_outputs.quality_result.result_id}:{self.stage_outputs.quality_result.gate_decision.value}")
        raw = "|".join(keys) or "empty"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_stage_output_payload(self, stage: WorkflowStage) -> Optional[Dict[str, Any]]:
        """Return serialized dictionary payload for a given stage."""
        if stage == WorkflowStage.DIAGNOSIS and self.stage_outputs.diagnosis_result:
            return self.stage_outputs.diagnosis_result.model_dump(mode="json")
        if stage == WorkflowStage.ROOT_CAUSE and self.stage_outputs.root_cause_report:
            return self.stage_outputs.root_cause_report.model_dump(mode="json")
        if stage == WorkflowStage.PLANNING and self.stage_outputs.repair_plan:
            return self.stage_outputs.repair_plan.model_dump(mode="json")
        if stage == WorkflowStage.APPROVAL and self.stage_outputs.approval:
            return self.stage_outputs.approval.model_dump(mode="json")
        if stage == WorkflowStage.EXECUTION and self.stage_outputs.execution_result:
            return {
                "execution_result": self.stage_outputs.execution_result.model_dump(mode="json"),
                "repair_workspace": self.stage_outputs.repair_workspace.model_dump(mode="json") if self.stage_outputs.repair_workspace else None,
            }
        if stage == WorkflowStage.VERIFICATION and self.stage_outputs.verification_result:
            return self.stage_outputs.verification_result.model_dump(mode="json")
        if stage == WorkflowStage.QUALITY and self.stage_outputs.quality_result:
            return self.stage_outputs.quality_result.model_dump(mode="json")
        return None

    def _save_stage_record_if_store(self, stage: WorkflowStage) -> None:
        """Persist stage execution record and outputs to state store if present."""
        if self.state_store and stage.value in self.stage_records:
            self.state_store.save_stage_record(
                workflow_id=self.workflow_id,
                stage=stage,
                record=self.stage_records[stage.value],
                output_payload=self._get_stage_output_payload(stage),
            )

    def _execute_with_retry(
        self,
        stage: WorkflowStage,
        operation: Any,
        is_mutating: bool = False,
    ) -> Any:
        """Execute an adapter operation with bounded retry and deterministic failure classification."""
        import time
        attempt = 1
        while True:
            try:
                return operation()
            except Exception as exc:
                cat = FailureClassifier.classify_exception(exc, is_mutating=is_mutating)
                decision = self.retry_policy.evaluate(
                    category=cat,
                    attempt=attempt,
                    is_mutating=is_mutating,
                    operation_name=stage.value,
                )
                if decision.should_retry:
                    self.event_recorder.record_event(
                        workflow_id=self.workflow_id,
                        event_type=OrchestrationEventType.RETRY_ATTEMPTED,
                        state=self.state,
                        action_description=f"Retrying stage '{stage.value}' after transient failure ({cat.value}): {exc}",
                        stage=stage,
                        details={"attempt": attempt, "delay": decision.delay_seconds, "error": str(exc)},
                    )
                    time.sleep(decision.delay_seconds)
                    attempt += 1
                    continue
                raise

    # -----------------------------------------------------------------------
    # Stage 1: Diagnosis
    # -----------------------------------------------------------------------

    def run_diagnosis(self, input_data: Optional[DiagnosisInput] = None) -> DiagnosisResult:
        """Execute Stage 1: Defect Diagnosis."""
        with self._lock:
            self._transition(
                WorkflowState.DIAGNOSING,
                "Beginning defect diagnosis stage.",
                stage=WorkflowStage.DIAGNOSIS,
            )
            started_at = datetime.now(timezone.utc)

            try:
                diag_result = self._execute_with_retry(
                    WorkflowStage.DIAGNOSIS,
                    lambda: self.diagnosis_adapter.run_diagnosis(self.context, input_data),
                    is_mutating=False,
                )
                self.stage_outputs.diagnosis_result = diag_result

                # Check if defect diagnosis succeeded
                if diag_result.status in (DiagnosisStatus.CONFIRMED, DiagnosisStatus.SUSPECTED):
                    self.stage_records[WorkflowStage.DIAGNOSIS.value] = StageExecutionRecord(
                        stage=WorkflowStage.DIAGNOSIS,
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        metadata={"diagnosis_id": diag_result.diagnosis_id, "status": diag_result.status.value},
                    )
                    self._save_stage_record_if_store(WorkflowStage.DIAGNOSIS)
                    self._transition(
                        WorkflowState.DIAGNOSIS_COMPLETED,
                        f"Defect diagnosis completed with status '{diag_result.status.value}'.",
                        stage=WorkflowStage.DIAGNOSIS,
                        details={"diagnosis_id": diag_result.diagnosis_id},
                    )
                else:
                    err_msg = f"Diagnosis produced non-actionable status: '{diag_result.status.value}'."
                    self.stage_records[WorkflowStage.DIAGNOSIS.value] = StageExecutionRecord(
                        stage=WorkflowStage.DIAGNOSIS,
                        status=StageStatus.BLOCKED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        error_message=err_msg,
                        metadata={"diagnosis_id": diag_result.diagnosis_id},
                    )
                    self._save_stage_record_if_store(WorkflowStage.DIAGNOSIS)
                    self._transition(
                        WorkflowState.BLOCKED,
                        f"Workflow blocked: {err_msg}",
                        stage=WorkflowStage.DIAGNOSIS,
                    )

                return diag_result

            except Exception as exc:
                self.stage_records[WorkflowStage.DIAGNOSIS.value] = StageExecutionRecord(
                    stage=WorkflowStage.DIAGNOSIS,
                    status=StageStatus.FAILED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )
                self._save_stage_record_if_store(WorkflowStage.DIAGNOSIS)
                self._transition(
                    WorkflowState.FAILED,
                    f"Diagnosis stage failed with exception: {exc}",
                    stage=WorkflowStage.DIAGNOSIS,
                )
                raise

    # -----------------------------------------------------------------------
    # Stage 2: Root Cause Analysis
    # -----------------------------------------------------------------------

    def run_root_cause(self) -> RootCauseReport:
        """Execute Stage 2: Root Cause Analysis."""
        with self._lock:
            self._transition(
                WorkflowState.ROOT_CAUSE_ANALYSIS,
                "Beginning root-cause analysis stage.",
                stage=WorkflowStage.ROOT_CAUSE,
            )
            started_at = datetime.now(timezone.utc)

            try:
                diag_res = self.stage_outputs.diagnosis_result
                rc_report = self._execute_with_retry(
                    WorkflowStage.ROOT_CAUSE,
                    lambda: self.root_cause_adapter.run_root_cause(
                        self.context,
                        diagnosis=diag_res,
                    ),
                    is_mutating=False,
                )
                self.stage_outputs.root_cause_report = rc_report

                if rc_report.status in (
                    CausalStatus.PROVEN,
                    CausalStatus.PLAUSIBLE,
                    CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES,
                ):
                    self.stage_records[WorkflowStage.ROOT_CAUSE.value] = StageExecutionRecord(
                        stage=WorkflowStage.ROOT_CAUSE,
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        metadata={"report_id": rc_report.report_id, "status": rc_report.status.value},
                    )
                    self._save_stage_record_if_store(WorkflowStage.ROOT_CAUSE)
                    self._transition(
                        WorkflowState.ROOT_CAUSE_COMPLETED,
                        f"Root-cause analysis completed with status '{rc_report.status.value}'.",
                        stage=WorkflowStage.ROOT_CAUSE,
                        details={"report_id": rc_report.report_id},
                    )
                else:
                    err_msg = f"Root-cause analysis produced non-actionable status: '{rc_report.status.value}'."
                    self.stage_records[WorkflowStage.ROOT_CAUSE.value] = StageExecutionRecord(
                        stage=WorkflowStage.ROOT_CAUSE,
                        status=StageStatus.BLOCKED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        error_message=err_msg,
                        metadata={"report_id": rc_report.report_id},
                    )
                    self._save_stage_record_if_store(WorkflowStage.ROOT_CAUSE)
                    self._transition(
                        WorkflowState.BLOCKED,
                        f"Workflow blocked: {err_msg}",
                        stage=WorkflowStage.ROOT_CAUSE,
                    )

                return rc_report

            except Exception as exc:
                self.stage_records[WorkflowStage.ROOT_CAUSE.value] = StageExecutionRecord(
                    stage=WorkflowStage.ROOT_CAUSE,
                    status=StageStatus.FAILED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )
                self._transition(
                    WorkflowState.FAILED,
                    f"Root-cause analysis stage failed with exception: {exc}",
                    stage=WorkflowStage.ROOT_CAUSE,
                )
                raise

    # -----------------------------------------------------------------------
    # Stage 3: Repair Planning
    # -----------------------------------------------------------------------

    def run_planning(self) -> RepairPlan:
        """Execute Stage 3: Repair Planning."""
        with self._lock:
            self._transition(
                WorkflowState.PLANNING,
                "Beginning repair planning stage.",
                stage=WorkflowStage.PLANNING,
            )
            started_at = datetime.now(timezone.utc)

            try:
                diag_res = self.stage_outputs.diagnosis_result
                rca_res = self.stage_outputs.root_cause_report
                plan = self._execute_with_retry(
                    WorkflowStage.PLANNING,
                    lambda: self.planning_adapter.run_planning(
                        self.context,
                        root_cause=rca_res,
                        diagnosis=diag_res,
                    ),
                    is_mutating=False,
                )
                self.stage_outputs.repair_plan = plan

                if (
                    plan.status in (RepairPlanStatus.ACTIONABLE, RepairPlanStatus.CONDITIONAL)
                    and plan.selected_strategy_id is not None
                ):
                    self.stage_records[WorkflowStage.PLANNING.value] = StageExecutionRecord(
                        stage=WorkflowStage.PLANNING,
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        metadata={"plan_id": plan.plan_id, "strategy_id": plan.selected_strategy_id},
                    )
                    self._save_stage_record_if_store(WorkflowStage.PLANNING)
                    # Transition to PLAN_READY then immediately to AWAITING_HUMAN_APPROVAL
                    self._transition(
                        WorkflowState.PLAN_READY,
                        f"Repair plan generated: plan_id='{plan.plan_id}'.",
                        stage=WorkflowStage.PLANNING,
                        details={"plan_id": plan.plan_id},
                    )
                    self._transition(
                        WorkflowState.AWAITING_HUMAN_APPROVAL,
                        "Repair plan requires human authorization before execution.",
                        stage=WorkflowStage.APPROVAL,
                        details={"plan_id": plan.plan_id, "plan_digest": compute_plan_digest(plan)},
                    )
                else:
                    err_msg = f"Repair plan is not actionable: status='{plan.status.value}'."
                    self.stage_records[WorkflowStage.PLANNING.value] = StageExecutionRecord(
                        stage=WorkflowStage.PLANNING,
                        status=StageStatus.BLOCKED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        error_message=err_msg,
                        metadata={"plan_id": plan.plan_id},
                    )
                    self._save_stage_record_if_store(WorkflowStage.PLANNING)
                    self._transition(
                        WorkflowState.BLOCKED,
                        f"Workflow blocked: {err_msg}",
                        stage=WorkflowStage.PLANNING,
                    )

                return plan

            except Exception as exc:
                self.stage_records[WorkflowStage.PLANNING.value] = StageExecutionRecord(
                    stage=WorkflowStage.PLANNING,
                    status=StageStatus.FAILED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )
                self._save_stage_record_if_store(WorkflowStage.PLANNING)
                self._transition(
                    WorkflowState.FAILED,
                    f"Repair planning stage failed with exception: {exc}",
                    stage=WorkflowStage.PLANNING,
                )
                raise

    # -----------------------------------------------------------------------
    # Stage 4: Human Approval Gate
    # -----------------------------------------------------------------------

    def submit_approval(self, approval: RepairApproval) -> ApprovalVerificationOutcome:
        """Submit human authorization to execute the repair plan."""
        with self._lock:
            if self.state != WorkflowState.AWAITING_HUMAN_APPROVAL:
                raise InvalidTransitionError(
                    f"Cannot submit approval: workflow is in state '{self.state.value}', "
                    f"expected '{WorkflowState.AWAITING_HUMAN_APPROVAL.value}'."
                )

            plan = self.stage_outputs.repair_plan
            if not plan:
                raise WorkflowStateError("No active repair plan found to evaluate approval against.")

            outcome = self.approval_manager.verify_approval(approval, plan, workflow_id=self.workflow_id)
            started_at = datetime.now(timezone.utc)

            if outcome.is_valid:
                try:
                    self.approval_manager.validate_and_record_approval(
                        approval=approval,
                        plan=plan,
                        workflow_id=self.workflow_id,
                    )
                except (OrchestrationApprovalError, ValueError) as rec_err:
                    outcome = ApprovalVerificationOutcome(False, [str(rec_err)])

            if outcome.is_valid:
                self.stage_outputs.approval = approval
                self.stage_records[WorkflowStage.APPROVAL.value] = StageExecutionRecord(
                    stage=WorkflowStage.APPROVAL,
                    status=StageStatus.COMPLETED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    metadata={"approval_id": approval.approval_id, "approver": approval.approver_reference},
                )
                self._save_stage_record_if_store(WorkflowStage.APPROVAL)
                self._transition(
                    WorkflowState.APPROVED,
                    f"Human approval verified for plan '{plan.plan_id}' by '{approval.approver_reference}'.",
                    stage=WorkflowStage.APPROVAL,
                    details={
                        "approval_id": approval.approval_id,
                        "approver": approval.approver_reference,
                        "plan_digest": approval.plan_digest,
                    },
                )
            else:
                err_msg = f"Approval verification failed: {'; '.join(outcome.errors)}"
                self.stage_records[WorkflowStage.APPROVAL.value] = StageExecutionRecord(
                    stage=WorkflowStage.APPROVAL,
                    status=StageStatus.BLOCKED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=err_msg,
                    metadata={"approval_id": approval.approval_id, "errors": outcome.errors},
                )
                self._save_stage_record_if_store(WorkflowStage.APPROVAL)
                self._transition(
                    WorkflowState.BLOCKED,
                    f"Workflow blocked: {err_msg}",
                    stage=WorkflowStage.APPROVAL,
                    details={"errors": outcome.errors},
                )

            return outcome

    # -----------------------------------------------------------------------
    # Stage 5: Controlled Repair Execution
    # -----------------------------------------------------------------------

    def run_repair(self) -> RepairExecutionResult:
        """Execute Stage 5: Controlled Autonomous Repair in Isolated Workspace."""
        with self._lock:
            if self.state != WorkflowState.APPROVED:
                raise IllegalStateTransitionError(
                    f"Cannot execute repair: workflow state is '{self.state.value}', expected 'APPROVED'."
                )

            plan = self.stage_outputs.repair_plan
            approval = self.stage_outputs.approval
            if not plan or not approval:
                raise WorkflowStateError("Cannot execute repair without valid plan and approval.")

            # Idempotency check for repair execution (cached replay check before consuming approval)
            plan_digest = compute_plan_digest(plan)
            idemp_key = compute_idempotency_key(
                workflow_id=self.workflow_id,
                stage=WorkflowStage.EXECUTION,
                input_digest=plan_digest,
            )
            if self.idempotency_ledger:
                existing = self.idempotency_ledger.get(idemp_key)
                if existing and existing.status == OperationStatus.COMPLETED and existing.result_payload:
                    try:
                        cached_result = RepairExecutionResult.model_validate(existing.result_payload["execution_result"])
                        if existing.result_payload.get("repair_workspace"):
                            cached_ws = RepairWorkspace.model_validate(existing.result_payload["repair_workspace"])
                            self.stage_outputs.repair_workspace = cached_ws
                    except Exception as val_err:
                        raise CorruptedStateError(
                            f"Corrupted idempotency cached payload for key '{idemp_key}': {val_err}"
                        ) from val_err
                    self.stage_outputs.execution_result = cached_result
                    self._transition(
                        WorkflowState.REPAIR_EXECUTING,
                        f"Replaying cached execution for key '{idemp_key}'.",
                        stage=WorkflowStage.EXECUTION,
                    )
                    self._transition(
                        WorkflowState.REPAIR_COMPLETED,
                        f"Idempotent repair execution replayed from cache for key '{idemp_key}'.",
                        stage=WorkflowStage.EXECUTION,
                        details={"execution_id": cached_result.execution_id, "idempotency_key": idemp_key},
                    )
                    return cached_result

            # Immediate pre-execution authorization check (immediately before any workspace mutation)
            pre_check = self.approval_manager.verify_approval(
                approval=approval,
                plan=plan,
                workflow_id=self.workflow_id,
            )
            if not pre_check.is_valid:
                err_msg = f"Pre-execution approval verification failed: {'; '.join(pre_check.errors)}"
                self.stage_records[WorkflowStage.EXECUTION.value] = StageExecutionRecord(
                    stage=WorkflowStage.EXECUTION,
                    status=StageStatus.BLOCKED,
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                    error_message=err_msg,
                    metadata={"approval_id": approval.approval_id, "errors": pre_check.errors},
                )
                self._save_stage_record_if_store(WorkflowStage.EXECUTION)
                self._transition(WorkflowState.BLOCKED, err_msg, stage=WorkflowStage.EXECUTION)
                raise OrchestrationApprovalError(err_msg)

            # Ensure execution adapters share the approval manager ledger and trusted approvers for durable replay protection
            if isinstance(self.execution_adapter, FakeExecutionAdapter) and self.execution_adapter.ledger is None:
                self.execution_adapter.ledger = self.approval_manager.ledger
            elif isinstance(self.execution_adapter, DefaultExecutionAdapter):
                if self.execution_adapter.ledger is None:
                    self.execution_adapter.ledger = self.approval_manager.ledger
                if self.execution_adapter.trusted_approvers is None:
                    self.execution_adapter.trusted_approvers = self.approval_manager.trusted_approvers

            if self.idempotency_ledger:
                existing = self.idempotency_ledger.register_start(
                    idempotency_key=idemp_key,
                    workflow_id=self.workflow_id,
                    stage=WorkflowStage.EXECUTION,
                )
                if existing:
                    if existing.status in (OperationStatus.STARTED, OperationStatus.UNCERTAIN):
                        err_msg = f"Operation with key '{idemp_key}' was previously started or uncertain. Re-execution requires explicit reconciliation."
                        self._transition(
                            WorkflowState.BLOCKED,
                            err_msg,
                            stage=WorkflowStage.EXECUTION,
                            details={"idempotency_key": idemp_key, "status": existing.status.value},
                        )
                        raise UncertainExecutionError(err_msg)
                    elif existing.status == OperationStatus.FAILED:
                        raise IdempotencyConflictError(f"Previous execution attempt failed: {existing.error_message}")

            self._transition(
                WorkflowState.REPAIR_EXECUTING,
                "Beginning controlled repair execution in isolated workspace.",
                stage=WorkflowStage.EXECUTION,
            )
            started_at = datetime.now(timezone.utc)

            try:
                exec_result, workspace = self.execution_adapter.run_execution(
                    context=self.context,
                    plan=plan,
                    approval=approval,
                    workspace=self.stage_outputs.repair_workspace,
                )
                self.stage_outputs.execution_result = exec_result
                self.stage_outputs.repair_workspace = workspace

                if exec_result.execution_status == ExecutionStatus.APPLIED:
                    if not self.approval_manager.ledger.is_consumed(approval.approval_id):
                        self.approval_manager.ledger.record_consumed(
                            approval.approval_id,
                            exec_result.execution_id,
                        )
                    if self.idempotency_ledger:
                        self.idempotency_ledger.mark_completed(
                            idempotency_key=idemp_key,
                            result_payload={
                                "execution_result": exec_result.model_dump(mode="json"),
                                "repair_workspace": workspace.model_dump(mode="json"),
                            },
                            result_digest=exec_result.applied_patch_digest,
                        )
                    self.stage_records[WorkflowStage.EXECUTION.value] = StageExecutionRecord(
                        stage=WorkflowStage.EXECUTION,
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        metadata={
                            "execution_id": exec_result.execution_id,
                            "workspace_id": workspace.workspace_id,
                            "patch_digest": exec_result.applied_patch_digest,
                        },
                    )
                    self._save_stage_record_if_store(WorkflowStage.EXECUTION)
                    self._transition(
                        WorkflowState.REPAIR_COMPLETED,
                        f"Controlled repair applied in workspace '{workspace.workspace_id}'.",
                        stage=WorkflowStage.EXECUTION,
                        details={"execution_id": exec_result.execution_id},
                    )
                else:
                    if self.idempotency_ledger:
                        self.idempotency_ledger.mark_failed(
                            idempotency_key=idemp_key,
                            error_message=f"Execution status: {exec_result.execution_status.value}",
                        )
                    err_msg = f"Repair execution ended with status '{exec_result.execution_status.value}'."
                    self.stage_records[WorkflowStage.EXECUTION.value] = StageExecutionRecord(
                        stage=WorkflowStage.EXECUTION,
                        status=StageStatus.BLOCKED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        error_message=err_msg,
                        metadata={"execution_id": exec_result.execution_id},
                    )
                    self._save_stage_record_if_store(WorkflowStage.EXECUTION)
                    self._transition(
                        WorkflowState.BLOCKED,
                        f"Workflow blocked: {err_msg}",
                        stage=WorkflowStage.EXECUTION,
                    )

                return exec_result

            except Exception as exc:
                if self.idempotency_ledger:
                    self.idempotency_ledger.mark_uncertain(
                        idempotency_key=idemp_key,
                        reason=f"Interrupted during execution: {exc}",
                    )
                self.stage_records[WorkflowStage.EXECUTION.value] = StageExecutionRecord(
                    stage=WorkflowStage.EXECUTION,
                    status=StageStatus.FAILED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )
                self._save_stage_record_if_store(WorkflowStage.EXECUTION)
                self._transition(
                    WorkflowState.FAILED,
                    f"Repair execution stage failed with exception: {exc}",
                    stage=WorkflowStage.EXECUTION,
                )
                raise

    # -----------------------------------------------------------------------
    # Stage 6: Repair Verification
    # -----------------------------------------------------------------------

    def run_verification(self) -> VerificationResult:
        """Execute Stage 6: Repair Verification."""
        with self._lock:
            if self.state != WorkflowState.REPAIR_COMPLETED:
                raise InvalidTransitionError(
                    f"Cannot run verification: workflow state is '{self.state.value}', expected 'REPAIR_COMPLETED'."
                )

            exec_res = self.stage_outputs.execution_result
            workspace = self.stage_outputs.repair_workspace
            if not exec_res or not workspace:
                raise WorkflowStateError("Missing execution result or workspace for verification.")

            self._transition(
                WorkflowState.VERIFICATION_RUNNING,
                "Beginning repair verification stage in container sandbox.",
                stage=WorkflowStage.VERIFICATION,
            )
            started_at = datetime.now(timezone.utc)

            try:
                verif_result = self._execute_with_retry(
                    WorkflowStage.VERIFICATION,
                    lambda: self.verification_adapter.run_verification(
                        context=self.context,
                        execution_result=exec_res,
                        workspace=workspace,
                        policy=self.context.verification_policy,
                    ),
                    is_mutating=False,
                )
                self.stage_outputs.verification_result = verif_result

                if (
                    verif_result.verification_status == VerificationStatus.PASSED
                    and verif_result.gate_decision == "PASSED"
                    and verif_result.regression_classification
                    in (RegressionClassification.NO_REGRESSION_OBSERVED, RegressionClassification.IMPROVED)
                ):
                    self.stage_records[WorkflowStage.VERIFICATION.value] = StageExecutionRecord(
                        stage=WorkflowStage.VERIFICATION,
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        metadata={
                            "verification_id": verif_result.verification_id,
                            "regression_classification": verif_result.regression_classification.value,
                        },
                    )
                    self._save_stage_record_if_store(WorkflowStage.VERIFICATION)
                    self._transition(
                        WorkflowState.VERIFICATION_COMPLETED,
                        f"Repair verification passed with zero regressions.",
                        stage=WorkflowStage.VERIFICATION,
                        details={"verification_id": verif_result.verification_id},
                    )
                else:
                    err_msg = (
                        f"Verification failed: status='{verif_result.verification_status.value}', "
                        f"gate='{verif_result.gate_decision}', "
                        f"regression='{verif_result.regression_classification.value}'."
                    )
                    self.stage_records[WorkflowStage.VERIFICATION.value] = StageExecutionRecord(
                        stage=WorkflowStage.VERIFICATION,
                        status=StageStatus.BLOCKED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        error_message=err_msg,
                        metadata={"verification_id": verif_result.verification_id},
                    )
                    self._save_stage_record_if_store(WorkflowStage.VERIFICATION)
                    self._transition(
                        WorkflowState.BLOCKED,
                        f"Workflow blocked: {err_msg}",
                        stage=WorkflowStage.VERIFICATION,
                    )

                return verif_result

            except Exception as exc:
                self.stage_records[WorkflowStage.VERIFICATION.value] = StageExecutionRecord(
                    stage=WorkflowStage.VERIFICATION,
                    status=StageStatus.FAILED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )
                self._save_stage_record_if_store(WorkflowStage.VERIFICATION)
                self._transition(
                    WorkflowState.FAILED,
                    f"Repair verification stage failed with exception: {exc}",
                    stage=WorkflowStage.VERIFICATION,
                )
                raise

    # -----------------------------------------------------------------------
    # Stage 7: Repair Quality & Reliability Evaluation
    # -----------------------------------------------------------------------

    def run_quality(self) -> RepairQualityResult:
        """Execute Stage 7: Repair Quality & Reliability Evaluation."""
        with self._lock:
            if self.state != WorkflowState.VERIFICATION_COMPLETED:
                raise InvalidTransitionError(
                    f"Cannot evaluate quality: workflow state is '{self.state.value}', expected 'VERIFICATION_COMPLETED'."
                )

            exec_res = self.stage_outputs.execution_result
            workspace = self.stage_outputs.repair_workspace
            plan = self.stage_outputs.repair_plan
            verif_res = self.stage_outputs.verification_result

            if not exec_res or not workspace or not plan or not verif_res:
                raise WorkflowStateError("Missing prerequisite stage results for quality evaluation.")

            self._transition(
                WorkflowState.QUALITY_EVALUATION,
                "Beginning multi-dimensional repair quality evaluation.",
                stage=WorkflowStage.QUALITY,
            )
            started_at = datetime.now(timezone.utc)

            try:
                quality_result = self._execute_with_retry(
                    WorkflowStage.QUALITY,
                    lambda: self.quality_adapter.run_quality(
                        context=self.context,
                        execution_result=exec_res,
                        workspace=workspace,
                        plan=plan,
                        verification_result=verif_res,
                        diagnosis=self.stage_outputs.diagnosis_result,
                        root_cause=self.stage_outputs.root_cause_report,
                        policy=self.context.quality_policy,
                    ),
                    is_mutating=False,
                )
                self.stage_outputs.quality_result = quality_result

                if (
                    quality_result.quality_status == QualityStatus.PASSED
                    and quality_result.gate_decision == QualityGateDecision.PASSED
                    and not quality_result.policy_violations
                ):
                    self.stage_records[WorkflowStage.QUALITY.value] = StageExecutionRecord(
                        stage=WorkflowStage.QUALITY,
                        status=StageStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        metadata={
                            "result_id": quality_result.result_id,
                            "overall_score": quality_result.overall_score,
                        },
                    )
                    self._save_stage_record_if_store(WorkflowStage.QUALITY)
                    self._transition(
                        WorkflowState.COMPLETED,
                        f"Quality evaluation passed (score={quality_result.overall_score:.2f}). Workflow successfully completed.",
                        stage=WorkflowStage.QUALITY,
                        details={
                            "result_id": quality_result.result_id,
                            "score": quality_result.overall_score,
                        },
                    )
                else:
                    err_msg = (
                        f"Quality evaluation rejected repair: gate='{quality_result.gate_decision.value}', "
                        f"status='{quality_result.quality_status.value}', "
                        f"violations={quality_result.policy_violations}."
                    )
                    self.stage_records[WorkflowStage.QUALITY.value] = StageExecutionRecord(
                        stage=WorkflowStage.QUALITY,
                        status=StageStatus.BLOCKED,
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        error_message=err_msg,
                        metadata={
                            "result_id": quality_result.result_id,
                            "violations": quality_result.policy_violations,
                        },
                    )
                    self._save_stage_record_if_store(WorkflowStage.QUALITY)
                    self._transition(
                        WorkflowState.BLOCKED,
                        f"Workflow blocked: {err_msg}",
                        stage=WorkflowStage.QUALITY,
                        details={"violations": quality_result.policy_violations},
                    )

                return quality_result

            except Exception as exc:
                self.stage_records[WorkflowStage.QUALITY.value] = StageExecutionRecord(
                    stage=WorkflowStage.QUALITY,
                    status=StageStatus.FAILED,
                    started_at=started_at,
                    completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )
                self._save_stage_record_if_store(WorkflowStage.QUALITY)
                self._transition(
                    WorkflowState.FAILED,
                    f"Quality evaluation stage failed with exception: {exc}",
                    stage=WorkflowStage.QUALITY,
                )
                raise

    # -----------------------------------------------------------------------
    # Step-by-Step & End-to-End Orchestration Runners
    # -----------------------------------------------------------------------

    def execute_next_stage(self) -> WorkflowState:
        """Advance the workflow to its next permissible stage."""
        if is_terminal_state(self.state):
            raise TerminalStateError(
                f"Cannot advance workflow: already in terminal state '{self.state.value}'."
            )

        if self.state == WorkflowState.CREATED:
            self.run_diagnosis()
        elif self.state == WorkflowState.DIAGNOSIS_COMPLETED:
            self.run_root_cause()
        elif self.state == WorkflowState.ROOT_CAUSE_COMPLETED:
            self.run_planning()
        elif self.state == WorkflowState.PLAN_READY:
            # Plan is ready; transition to awaiting approval if not already there
            with self._lock:
                self._transition(
                    WorkflowState.AWAITING_HUMAN_APPROVAL,
                    "Awaiting human approval before repair execution.",
                    stage=WorkflowStage.APPROVAL,
                )
        elif self.state == WorkflowState.AWAITING_HUMAN_APPROVAL:
            # Cannot proceed without explicit human authorization
            return self.state
        elif self.state == WorkflowState.APPROVED:
            self.run_repair()
        elif self.state == WorkflowState.REPAIR_COMPLETED:
            self.run_verification()
        elif self.state == WorkflowState.VERIFICATION_COMPLETED:
            self.run_quality()

        return self.state

    def is_completed(self) -> bool:
        """Return True if the workflow has reached the terminal COMPLETED state."""
        return self.state == WorkflowState.COMPLETED

    def verify_audit_trail(self) -> bool:
        """Verify cryptographic SHA-256 integrity of the workflow's audit event trail."""
        events = self.event_recorder.get_events(self.workflow_id)
        is_valid, _ = OrchestrationEventRecorder.verify_event_chain(events)
        return is_valid

    def run_until_approval_or_completion(self) -> WorkflowState:
        """Execute stages sequentially until human approval is needed or terminal state is reached."""
        while not is_terminal_state(self.state) and self.state != WorkflowState.AWAITING_HUMAN_APPROVAL:
            prev = self.state
            curr = self.execute_next_stage()
            if curr == prev:
                break
        return self.state

    def cancel(self, reason: str = "Workflow cancelled by operator.") -> None:
        """Cancel the workflow."""
        with self._lock:
            self._transition(WorkflowState.CANCELLED, reason)

    # -----------------------------------------------------------------------
    # Result & Report Generation
    # -----------------------------------------------------------------------

    def get_result(self) -> EngineeringWorkflowResult:
        """Construct the immutable EngineeringWorkflowResult with full provenance."""
        with self._lock:
            diag = self.stage_outputs.diagnosis_result
            rc = self.stage_outputs.root_cause_report
            plan = self.stage_outputs.repair_plan
            appr = self.stage_outputs.approval
            exec_res = self.stage_outputs.execution_result
            ws = self.stage_outputs.repair_workspace
            verif = self.stage_outputs.verification_result
            qual = self.stage_outputs.quality_result

            violations = list(qual.policy_violations) if qual else []
            limitations: List[str] = []
            if diag and diag.limitations:
                limitations.extend(diag.limitations)
            if rc and rc.limitations:
                limitations.extend(rc.limitations)
            if plan and plan.limitations:
                limitations.extend(plan.limitations)
            if exec_res and exec_res.limitations:
                limitations.extend(exec_res.limitations)
            if verif and verif.limitations:
                limitations.extend(verif.limitations)

            events = self.event_recorder.get_events(self.workflow_id)
            is_valid, err = OrchestrationEventRecorder.verify_event_chain(events)
            if not is_valid:
                raise AuditTrailTamperedError(
                    f"Audit event chain integrity verification failed for workflow '{self.workflow_id}': {err}"
                )

            res = EngineeringWorkflowResult(
                workflow_id=self.workflow_id,
                state=self.state,
                repository_path=self.context.repository_path,
                commit_sha=self.context.commit_sha,
                created_at=self.context.created_at,
                completed_at=self.completed_at,
                stages=dict(self.stage_records),
                diagnosis_id=diag.diagnosis_id if diag else None,
                root_cause_report_id=rc.report_id if rc else None,
                repair_plan_id=plan.plan_id if plan else None,
                repair_plan_digest=compute_plan_digest(plan) if plan else None,
                approval_id=appr.approval_id if appr else None,
                approver_identity=appr.approver_reference if appr else None,
                execution_id=exec_res.execution_id if exec_res else None,
                workspace_id=ws.workspace_id if ws else None,
                applied_patch_digest=exec_res.applied_patch_digest if exec_res else None,
                baseline_tree_digest=ws.baseline_tree_digest if ws else None,
                verification_id=verif.verification_id if verif else None,
                verification_status=verif.verification_status.value if verif else None,
                regression_classification=verif.regression_classification.value if verif else None,
                quality_gate_decision=qual.gate_decision.value if qual else None,
                quality_score=qual.overall_score if qual else None,
                policy_violations=violations,
                limitations=limitations,
                event_count=len(events),
                requires_human_review=True,
            )

            # Generate markdown summary
            md_report = EngineeringReportGenerator.generate_markdown(res)
            return res.model_copy(update={"report_markdown": md_report})
