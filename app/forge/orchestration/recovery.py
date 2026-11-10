"""Phase 13 M2: Crash and Interruption Recovery Engine.

Provides deterministic, fail-closed recovery for interrupted engineering workflows.
Validates workflow context, cryptographic event chains, checkpoints, and
mutation reconciliation status before resuming or blocking execution.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from forge.causality.schemas import RootCauseReport
from forge.diagnosis.schemas import DiagnosisInput, DiagnosisResult
from forge.orchestration.events import (
    AuditTrailTamperedError,
    GENESIS_HASH,
    OrchestrationEvent,
    OrchestrationEventRecorder,
    OrchestrationEventType,
)
from forge.orchestration.idempotency import IdempotencyLedger, OperationStatus
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.persistence import (
    CorruptedStateError,
    OrchestrationStateStore,
    PersistenceError,
)
from forge.orchestration.schemas import (
    StageExecutionRecord,
    StageOutputs,
    StageStatus,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.orchestration.state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalStateTransitionError,
    validate_transition,
)
from forge.repair_execution.schemas import RepairApproval, RepairExecutionResult, RepairWorkspace
from forge.repair_planning.schemas import RepairPlan
from forge.repair_quality.schemas import RepairQualityResult
from forge.repair_verification.schemas import VerificationResult


class RecoveryValidationError(Exception):
    """Raised when a workflow fails recovery validation invariants."""


WorkflowRecoveryError = RecoveryValidationError
RecoveryStatus = WorkflowState


class MutationReconciliationRequiredError(RecoveryValidationError):
    """Raised when an interrupted repair mutation requires explicit human reconciliation."""


class RecoveryDecision(BaseModel):
    """Decision produced by the recovery engine evaluating whether to resume or block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    can_resume: bool
    recovered_state: WorkflowState
    last_completed_stage: Optional[WorkflowStage] = None
    next_stage_to_execute: Optional[WorkflowStage] = None
    reconciliation_required: bool = False
    reason: str
    human_action_required: Optional[str] = None
    checkpoint_id: Optional[str] = None
    event_count: int = 0


class WorkflowRecoveryEngine:
    """Evaluates and reconstructs interrupted workflows with strict safety guarantees."""

    def __init__(
        self,
        store: Optional[OrchestrationStateStore] = None,
        state_store: Optional[OrchestrationStateStore] = None,
        idempotency_ledger: Optional[Any] = None,
    ) -> None:
        self.store = store or state_store or OrchestrationStateStore()
        self.idempotency_ledger = idempotency_ledger or IdempotencyLedger(db_path=self.store.db_path)

    def inspect_recovery(
        self,
        workflow_id: str,
        expected_repo_path: Optional[str] = None,
    ) -> RecoveryDecision:
        """Inspect and evaluate recovery status without applying state mutations."""
        wf_data = self.store.get_workflow(workflow_id)
        if not wf_data:
            raise RecoveryValidationError(f"Workflow '{workflow_id}' not found in state store.")

        # 1. Repository Context Binding Validation
        if expected_repo_path:
            norm_expected = str(Path(expected_repo_path).resolve())
            norm_persisted = str(Path(wf_data["repository_path"]).resolve())
            if norm_expected != norm_persisted:
                raise RecoveryValidationError(
                    f"Repository binding mismatch for workflow '{workflow_id}': "
                    f"expected '{norm_expected}', persisted '{norm_persisted}'."
                )

        # 2. Cryptographic Audit Trail Verification
        events = self.store.get_events(workflow_id)
        if not events:
            raise RecoveryValidationError(f"Workflow '{workflow_id}' has no audit events recorded.")

        is_valid_chain, error_reason = OrchestrationEventRecorder.verify_event_chain(events)
        if not is_valid_chain:
            raise AuditTrailTamperedError(
                f"Cryptographic audit trail integrity check failed for '{workflow_id}': {error_reason}"
            )

        persisted_state = WorkflowState(wf_data["state"])
        last_event = events[-1]

        # 3. Interrupted Repair Execution: Requires Mandatory Mutation Reconciliation
        if persisted_state == WorkflowState.REPAIR_EXECUTING or wf_data.get("reconciliation_required") == 1:
            return RecoveryDecision(
                workflow_id=workflow_id,
                can_resume=False,
                recovered_state=WorkflowState.BLOCKED,
                last_completed_stage=WorkflowStage.APPROVAL,
                next_stage_to_execute=WorkflowStage.EXECUTION,
                reconciliation_required=True,
                reason=(
                    "Workflow was interrupted during controlled repair execution. "
                    "Workspace mutation status is uncertain. Operator reconciliation required "
                    "before any further execution can proceed."
                ),
                human_action_required=(
                    f"Run 'forge orchestrate reconcile {workflow_id} --resolution discard' or "
                    "'--resolution confirm' after inspecting workspace state."
                ),
                event_count=len(events),
            )

        # 4. Terminal Workflows cannot be resumed
        if persisted_state in TERMINAL_STATES:
            return RecoveryDecision(
                workflow_id=workflow_id,
                can_resume=False,
                recovered_state=persisted_state,
                last_completed_stage=WorkflowStage(wf_data["current_stage"]) if wf_data.get("current_stage") else None,
                reconciliation_required=False,
                reason=f"Workflow is in terminal state '{persisted_state.value}' and cannot be resumed.",
                human_action_required=None,
                event_count=len(events),
            )

        # 5. Awaiting Human Approval remains Awaiting Human Approval (Never Self-Approves)
        if persisted_state == WorkflowState.AWAITING_HUMAN_APPROVAL:
            return RecoveryDecision(
                workflow_id=workflow_id,
                can_resume=True,
                recovered_state=WorkflowState.AWAITING_HUMAN_APPROVAL,
                last_completed_stage=WorkflowStage.PLANNING,
                next_stage_to_execute=WorkflowStage.APPROVAL,
                reconciliation_required=False,
                reason="Workflow is safely awaiting human operator approval contract.",
                human_action_required=f"Submit approval using 'forge orchestrate approve {workflow_id} ...'",
                event_count=len(events),
            )

        # 6. Approved state ready for repair
        if persisted_state == WorkflowState.APPROVED:
            return RecoveryDecision(
                workflow_id=workflow_id,
                can_resume=True,
                recovered_state=WorkflowState.APPROVED,
                last_completed_stage=WorkflowStage.APPROVAL,
                next_stage_to_execute=WorkflowStage.EXECUTION,
                reconciliation_required=False,
                reason="Human approval verified. Ready to execute controlled repair.",
                human_action_required=None,
                event_count=len(events),
            )

        # 7. Checkpoint Evaluation for other intermediate states
        chk = self.store.get_latest_checkpoint(workflow_id)
        checkpoint_id = chk["checkpoint_id"] if chk else None

        # Determine next stage based on current state
        next_stage_map = {
            WorkflowState.CREATED: (None, WorkflowStage.DIAGNOSIS),
            WorkflowState.DIAGNOSING: (None, WorkflowStage.DIAGNOSIS),
            WorkflowState.DIAGNOSIS_COMPLETED: (WorkflowStage.DIAGNOSIS, WorkflowStage.ROOT_CAUSE),
            WorkflowState.ROOT_CAUSE_ANALYSIS: (WorkflowStage.DIAGNOSIS, WorkflowStage.ROOT_CAUSE),
            WorkflowState.ROOT_CAUSE_COMPLETED: (WorkflowStage.ROOT_CAUSE, WorkflowStage.PLANNING),
            WorkflowState.PLANNING: (WorkflowStage.ROOT_CAUSE, WorkflowStage.PLANNING),
            WorkflowState.PLAN_READY: (WorkflowStage.PLANNING, WorkflowStage.APPROVAL),
            WorkflowState.REPAIR_COMPLETED: (WorkflowStage.EXECUTION, WorkflowStage.VERIFICATION),
            WorkflowState.VERIFICATION_RUNNING: (WorkflowStage.EXECUTION, WorkflowStage.VERIFICATION),
            WorkflowState.VERIFICATION_COMPLETED: (WorkflowStage.VERIFICATION, WorkflowStage.QUALITY),
            WorkflowState.QUALITY_EVALUATION: (WorkflowStage.VERIFICATION, WorkflowStage.QUALITY),
        }

        last_comp, next_stg = next_stage_map.get(persisted_state, (None, None))

        return RecoveryDecision(
            workflow_id=workflow_id,
            can_resume=True,
            recovered_state=persisted_state,
            last_completed_stage=last_comp,
            next_stage_to_execute=next_stg,
            reconciliation_required=False,
            reason=f"Workflow safely recovered from checkpoint at state '{persisted_state.value}'.",
            human_action_required=None,
            checkpoint_id=checkpoint_id,
            event_count=len(events),
        )

    def recover_workflow(
        self,
        workflow_id: str,
        expected_repo_path: Optional[str] = None,
        approval_manager: Optional[Any] = None,
        diagnosis_adapter: Optional[Any] = None,
        root_cause_adapter: Optional[Any] = None,
        planning_adapter: Optional[Any] = None,
        execution_adapter: Optional[Any] = None,
        verification_adapter: Optional[Any] = None,
        quality_adapter: Optional[Any] = None,
        allow_discard_unconfirmed_mutation: bool = False,
    ) -> Tuple[RecoveryDecision, EngineeringOrchestrator]:
        """Perform full recovery, rebuilding the orchestrator instance and recording recovery events."""
        decision = self.inspect_recovery(workflow_id, expected_repo_path)
        wf_data = self.store.get_workflow(workflow_id)
        assert wf_data is not None

        # Reconstruct WorkflowContext
        defect_input = None
        if wf_data.get("problem_description"):
            defect_input = DiagnosisInput(user_description=wf_data["problem_description"])

        approvers = None
        if wf_data.get("trusted_approvers"):
            approvers = set(wf_data["trusted_approvers"])

        ctx = WorkflowContext(
            workflow_id=wf_data["workflow_id"],
            repository_path=wf_data["repository_path"],
            commit_sha=wf_data["commit_sha"],
            run_id=wf_data["run_id"],
            defect_input=defect_input,
            trusted_approvers=approvers,
            created_at=datetime.fromisoformat(wf_data["created_at"]),
        )

        # Restore EventRecorder with persisted events
        event_recorder = OrchestrationEventRecorder()
        persisted_events = self.store.get_events(workflow_id)
        with event_recorder._lock:
            event_recorder._events[workflow_id] = list(persisted_events)

        # Initialize Orchestrator with state store and idempotency ledger
        orchestrator = EngineeringOrchestrator(
            context=ctx,
            event_recorder=event_recorder,
            state_store=self.store,
            idempotency_ledger=self.idempotency_ledger,
            approval_manager=approval_manager,
            diagnosis_adapter=diagnosis_adapter,
            root_cause_adapter=root_cause_adapter,
            planning_adapter=planning_adapter,
            execution_adapter=execution_adapter,
            verification_adapter=verification_adapter,
            quality_adapter=quality_adapter,
        )

        # Restore State & Stage Records
        orchestrator.state = decision.recovered_state
        orchestrator.stage_records = self.store.get_stage_records(workflow_id)

        # Restore Deserialized Stage Outputs
        raw_outputs = self.store.get_stage_outputs_raw(workflow_id)
        self._hydrate_stage_outputs(orchestrator.stage_outputs, raw_outputs)

        # Handle Reconciliation Block if interrupted during repair execution
        if decision.reconciliation_required:
            if allow_discard_unconfirmed_mutation:
                raise RecoveryValidationError(
                    "Automatic discard of unconfirmed mutations is forbidden. "
                    "Interrupted repairs require explicit, authenticated human operator reconciliation via 'forge orchestrate reconcile'."
                )
            orchestrator.state = WorkflowState.BLOCKED
            event_recorder.record_event(
                workflow_id=workflow_id,
                event_type=OrchestrationEventType.WORKFLOW_BLOCKED,
                state=WorkflowState.BLOCKED,
                action_description="Workflow recovery blocked: repair execution interrupted. Reconciliation required.",
                stage=WorkflowStage.EXECUTION,
                details={"reason": decision.reason},
            )
            self.store.save_workflow(
                context=ctx,
                state=WorkflowState.BLOCKED,
                current_stage=WorkflowStage.EXECUTION,
                reconciliation_required=True,
            )
        else:
            event_recorder.record_event(
                workflow_id=workflow_id,
                event_type=OrchestrationEventType.STAGE_COMPLETED,
                state=orchestrator.state,
                action_description=f"Workflow successfully recovered at state '{orchestrator.state.value}'.",
                details={
                    "checkpoint_id": decision.checkpoint_id,
                    "event_count": decision.event_count,
                    "recovered_state": orchestrator.state.value,
                },
            )

        return decision, orchestrator

    def reconcile_mutation(
        self,
        workflow_id: str,
        resolution: str,
        reconciled_by: str,
        notes: Optional[str] = None,
    ) -> None:
        """Apply an operator reconciliation to unblock an interrupted repair."""
        wf_data = self.store.get_workflow(workflow_id)
        if not wf_data:
            raise RecoveryValidationError(f"Workflow '{workflow_id}' not found.")

        # 1. Reconciliation Necessity Check
        status_before = wf_data["state"]
        is_reconcil_needed = (
            wf_data.get("reconciliation_required") == 1
            or status_before in (WorkflowState.REPAIR_EXECUTING.value, WorkflowState.BLOCKED.value)
        )
        if not is_reconcil_needed:
            raise RecoveryValidationError(
                f"Workflow '{workflow_id}' does not require mutation reconciliation (state is '{status_before}')."
            )

        # 2. Operator Identity & Authorization Check
        prohibited = {"auto", "llm", "ai", "bot", "synthetic", "none", "unknown"}
        norm_operator = (reconciled_by or "").strip().lower()
        if not norm_operator or norm_operator in prohibited:
            raise RecoveryValidationError(
                f"Reconciliation rejected: operator identity '{reconciled_by}' is prohibited or invalid. "
                "Reconciliation requires an authenticated, trusted human operator."
            )

        trusted_approvers = wf_data.get("trusted_approvers")
        if trusted_approvers:
            trusted_set = set(trusted_approvers)
            if reconciled_by not in trusted_set:
                raise RecoveryValidationError(
                    f"Reconciliation rejected: operator '{reconciled_by}' is not in the trusted approvers list: {sorted(trusted_set)}"
                )

        # 3. Resolution Validation
        norm_res = resolution.strip().lower()
        valid_resolutions = {"discard", "confirm", "revert"}
        if norm_res not in valid_resolutions:
            raise RecoveryValidationError(
                f"Invalid reconciliation resolution '{resolution}'. Must be one of {valid_resolutions}."
            )

        # 4. Semantics of 'confirm' vs 'discard' / 'revert'
        if norm_res == "confirm":
            # Confirm must NEVER blindly execute a previous repair.
            # Inspect execution records, stage outputs, and idempotency records for evidence.
            stage_records = self.store.get_stage_records(workflow_id)
            exec_record = stage_records.get(WorkflowStage.EXECUTION.value)
            raw_outputs = self.store.get_stage_outputs_raw(workflow_id)
            exec_output = raw_outputs.get(WorkflowStage.EXECUTION.value, {})
            idemp_records = self.idempotency_ledger.get_records_for_workflow(workflow_id) if hasattr(self.idempotency_ledger, "get_records_for_workflow") else []
            exec_idemp = [r for r in idemp_records if r.stage == WorkflowStage.EXECUTION]

            has_completed_record = exec_record is not None and exec_record.status == StageStatus.COMPLETED
            has_applied_output = isinstance(exec_output, dict) and exec_output.get("execution_status") == "applied"
            has_completed_idemp = any(r.status == OperationStatus.COMPLETED for r in exec_idemp)

            if not (has_completed_record or has_applied_output or has_completed_idemp):
                raise RecoveryValidationError(
                    "Cannot confirm mutation: no evidence of completed repair execution. "
                    "A partially applied mutation must not be classified as completed without evidence. "
                    "Use 'discard' or 'revert' to reset workspace and request a new repair plan."
                )

            # Evidence of completion is verified
            status_after = WorkflowState.REPAIR_COMPLETED.value
            for r in exec_idemp:
                if r.status != OperationStatus.COMPLETED and has_applied_output:
                    self.idempotency_ledger.mark_completed(
                        idempotency_key=r.idempotency_key,
                        result_payload=exec_output,
                    )
        else:
            # discard or revert:
            # Interrupted mutation is discarded. Approval consumption is preserved (consumed approval is never reused).
            # Workflow returns to AWAITING_HUMAN_APPROVAL. A new repair plan will require fresh human approval.
            status_after = WorkflowState.AWAITING_HUMAN_APPROVAL.value
            if hasattr(self.idempotency_ledger, "mark_failed_for_workflow"):
                self.idempotency_ledger.mark_failed_for_workflow(
                    workflow_id=workflow_id,
                    stage=WorkflowStage.EXECUTION,
                    error_message=f"Mutation {norm_res}ed by operator reconciliation ({reconciled_by}).",
                )
            # Remove consumed approval from stage records so it cannot be re-applied
            conn = self.store._get_connection()
            try:
                conn.execute(
                    "DELETE FROM orchestration_stage_records WHERE workflow_id = ? AND stage = ?;",
                    (workflow_id, WorkflowStage.APPROVAL.value),
                )
            finally:
                conn.close()

        reconcile_id = f"rec_{workflow_id[-6:]}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
        self.store.record_reconciliation(
            reconciliation_id=reconcile_id,
            workflow_id=workflow_id,
            resolution=norm_res,
            reconciled_by=reconciled_by,
            status_before=status_before,
            status_after=status_after,
            notes=notes,
        )

        # Update workflow audit record
        events = self.store.get_events(workflow_id)
        if events:
            ev_rec = OrchestrationEventRecorder()
            with ev_rec._lock:
                ev_rec._events[workflow_id] = list(events)
            ev = ev_rec.record_event(
                workflow_id=workflow_id,
                event_type=OrchestrationEventType.STAGE_COMPLETED,
                state=WorkflowState(status_after),
                action_description=f"Operator '{reconciled_by}' reconciled interrupted mutation: {norm_res}.",
                details={"resolution": norm_res, "notes": notes},
            )
            self.store.save_event(ev)

        # Update workflow status in store
        conn = self.store._get_connection()
        try:
            conn.execute(
                """
                UPDATE orchestration_workflows SET
                    state = ?,
                    reconciliation_required = 0,
                    updated_at = ?
                WHERE workflow_id = ?;
                """,
                (status_after, datetime.now(timezone.utc).isoformat(), workflow_id),
            )
        finally:
            conn.close()

    def _hydrate_stage_outputs(self, target: StageOutputs, raw_map: Dict[str, Dict[str, Any]]) -> None:
        """Hydrate StageOutputs fields from raw dictionaries."""
        try:
            if WorkflowStage.DIAGNOSIS.value in raw_map and raw_map[WorkflowStage.DIAGNOSIS.value]:
                target.diagnosis_result = DiagnosisResult.model_validate(raw_map[WorkflowStage.DIAGNOSIS.value])
        except Exception:
            pass

        try:
            if WorkflowStage.ROOT_CAUSE.value in raw_map and raw_map[WorkflowStage.ROOT_CAUSE.value]:
                target.root_cause_report = RootCauseReport.model_validate(raw_map[WorkflowStage.ROOT_CAUSE.value])
        except Exception:
            pass

        try:
            if WorkflowStage.PLANNING.value in raw_map and raw_map[WorkflowStage.PLANNING.value]:
                target.repair_plan = RepairPlan.model_validate(raw_map[WorkflowStage.PLANNING.value])
        except Exception:
            pass

        try:
            if WorkflowStage.APPROVAL.value in raw_map and raw_map[WorkflowStage.APPROVAL.value]:
                target.approval = RepairApproval.model_validate(raw_map[WorkflowStage.APPROVAL.value])
        except Exception:
            pass

        try:
            if WorkflowStage.EXECUTION.value in raw_map and raw_map[WorkflowStage.EXECUTION.value]:
                payload = raw_map[WorkflowStage.EXECUTION.value]
                if "execution_result" in payload:
                    target.execution_result = RepairExecutionResult.model_validate(payload["execution_result"])
                if "repair_workspace" in payload:
                    target.repair_workspace = RepairWorkspace.model_validate(payload["repair_workspace"])
        except Exception:
            pass

        try:
            if WorkflowStage.VERIFICATION.value in raw_map and raw_map[WorkflowStage.VERIFICATION.value]:
                target.verification_result = VerificationResult.model_validate(raw_map[WorkflowStage.VERIFICATION.value])
        except Exception:
            pass

        try:
            if WorkflowStage.QUALITY.value in raw_map and raw_map[WorkflowStage.QUALITY.value]:
                target.quality_result = RepairQualityResult.model_validate(raw_map[WorkflowStage.QUALITY.value])
        except Exception:
            pass
