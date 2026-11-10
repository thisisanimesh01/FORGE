"""Phase 13 M1: Typed Orchestration Tools for Agent & LLM Invocation.

Exposes 6 structured, typed, read-only and state-controlled tools to ToolRegistry:
1. create_orchestration_workflow
2. execute_orchestration_stage
3. inspect_orchestration_status
4. inspect_stage_result
5. submit_orchestration_approval
6. get_engineering_report
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from forge.config import redact_secrets
from forge.diagnosis.schemas import DiagnosisInput
from forge.llm.models import ToolDefinition
from forge.repair_execution.schemas import ApprovalStatus, RepairApproval
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.persistence import OrchestrationStateStore
from forge.orchestration.idempotency import IdempotencyLedger
from forge.orchestration.coordination import TaskCoordinator, TaskState
from forge.orchestration.recovery import WorkflowRecoveryEngine
from forge.orchestration.schemas import (
    CreateOrchestrationWorkflowInput,
    CreateOrchestrationWorkflowOutput,
    ExecuteOrchestrationStageInput,
    ExecuteOrchestrationStageOutput,
    GetEngineeringReportInput,
    GetEngineeringReportOutput,
    InspectOrchestrationStatusInput,
    InspectOrchestrationStatusOutput,
    InspectStageResultInput,
    InspectStageResultOutput,
    InspectWorkflowRecoveryInput,
    InspectWorkflowRecoveryOutput,
    InspectWorkflowTasksInput,
    InspectWorkflowTasksOutput,
    RecoverOrchestrationWorkflowInput,
    RecoverOrchestrationWorkflowOutput,
    ReconcileOrchestrationMutationInput,
    ReconcileOrchestrationMutationOutput,
    RequestOrchestrationApprovalInput,
    RequestOrchestrationApprovalOutput,
    StageStatus,
    SubmitOrchestrationApprovalInput,
    SubmitOrchestrationApprovalOutput,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)


class OrchestrationTools:
    """Tool invocation handler for Phase 13 Engineering Orchestration & Autonomy."""

    def __init__(
        self,
        repository_path: Path,
        max_output_chars: int = 50_000,
        state_store: Optional[OrchestrationStateStore] = None,
        recovery_engine: Optional[WorkflowRecoveryEngine] = None,
        task_coordinator: Optional[TaskCoordinator] = None,
        idempotency_ledger: Optional[IdempotencyLedger] = None,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.max_output_chars = max_output_chars
        self.state_store = state_store or OrchestrationStateStore()
        self.idempotency_ledger = idempotency_ledger or IdempotencyLedger()
        self.recovery_engine = recovery_engine or WorkflowRecoveryEngine(
            state_store=self.state_store,
            idempotency_ledger=self.idempotency_ledger,
        )
        self.task_coordinator = task_coordinator or TaskCoordinator(
            state_store=self.state_store,
        )
        self._orchestrators: Dict[str, EngineeringOrchestrator] = {}

    def register_orchestrator(self, orchestrator: EngineeringOrchestrator) -> None:
        """Register an active orchestrator instance."""
        self._orchestrators[orchestrator.workflow_id] = orchestrator

    def get_orchestrator(self, workflow_id: str) -> Optional[EngineeringOrchestrator]:
        """Retrieve an orchestrator instance by workflow ID."""
        return self._orchestrators.get(workflow_id)

    # -------------------------------------------------------------
    # Tool Handlers
    # -------------------------------------------------------------

    def create_orchestration_workflow(self, args: Dict[str, Any]) -> str:
        """Tool: create_orchestration_workflow."""
        try:
            inp = CreateOrchestrationWorkflowInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        repo_path = Path(inp.repository_path).resolve()
        defect_input = DiagnosisInput(
            target_files=inp.target_files or [],
            target_symbols=inp.target_symbols or [],
            user_description=inp.user_description,
            error_message=inp.error_message,
            stack_trace=inp.stack_trace,
            diff=inp.diff,
            finding_id=inp.finding_id,
        )
        context = WorkflowContext(
            repository_path=str(repo_path),
            defect_input=defect_input,
        )
        orchestrator = EngineeringOrchestrator(
            context=context,
            state_store=self.state_store,
            idempotency_ledger=self.idempotency_ledger,
        )
        self.register_orchestrator(orchestrator)

        out = CreateOrchestrationWorkflowOutput(
            workflow_id=orchestrator.workflow_id,
            state=orchestrator.state,
            repository_path=str(repo_path),
            created_at=context.created_at.isoformat(),
            run_id=context.run_id,
            message=f"Initialized workflow '{orchestrator.workflow_id}' in state '{orchestrator.state.value}'.",
        )
        return out.model_dump_json()

    def execute_orchestration_stage(self, args: Dict[str, Any]) -> str:
        """Tool: execute_orchestration_stage."""
        try:
            inp = ExecuteOrchestrationStageInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        orchestrator = self.get_orchestrator(inp.workflow_id)
        if not orchestrator:
            return json.dumps({"error": f"Workflow '{inp.workflow_id}' not found."})

        prev_state = orchestrator.state
        stage_to_execute = inp.stage

        try:
            if stage_to_execute == WorkflowStage.DIAGNOSIS or (
                stage_to_execute is None and prev_state == WorkflowState.CREATED
            ):
                orchestrator.run_diagnosis()
                stage_executed = WorkflowStage.DIAGNOSIS
            elif stage_to_execute == WorkflowStage.ROOT_CAUSE or (
                stage_to_execute is None and prev_state == WorkflowState.DIAGNOSIS_COMPLETED
            ):
                orchestrator.run_root_cause()
                stage_executed = WorkflowStage.ROOT_CAUSE
            elif stage_to_execute == WorkflowStage.PLANNING or (
                stage_to_execute is None and prev_state == WorkflowState.ROOT_CAUSE_COMPLETED
            ):
                orchestrator.run_planning()
                stage_executed = WorkflowStage.PLANNING
            elif stage_to_execute == WorkflowStage.EXECUTION or (
                stage_to_execute is None and prev_state == WorkflowState.APPROVED
            ):
                orchestrator.run_repair()
                stage_executed = WorkflowStage.EXECUTION
            elif stage_to_execute == WorkflowStage.VERIFICATION or (
                stage_to_execute is None and prev_state == WorkflowState.REPAIR_COMPLETED
            ):
                orchestrator.run_verification()
                stage_executed = WorkflowStage.VERIFICATION
            elif stage_to_execute == WorkflowStage.QUALITY or (
                stage_to_execute is None and prev_state == WorkflowState.VERIFICATION_COMPLETED
            ):
                orchestrator.run_quality()
                stage_executed = WorkflowStage.QUALITY
            else:
                return json.dumps({
                    "error": f"Cannot execute stage '{stage_to_execute}' while in workflow state '{prev_state.value}'."
                })

            stage_rec = orchestrator.stage_records.get(stage_executed.value)
            stage_status = stage_rec.status if stage_rec else StageStatus.COMPLETED

            out = ExecuteOrchestrationStageOutput(
                workflow_id=orchestrator.workflow_id,
                previous_state=prev_state,
                current_state=orchestrator.state,
                stage_executed=stage_executed,
                stage_status=stage_status,
                message=f"Executed stage '{stage_executed.value}'. Current state: '{orchestrator.state.value}'.",
                error=stage_rec.error_message if stage_rec else None,
            )
            return out.model_dump_json()

        except Exception as exc:
            return json.dumps({
                "workflow_id": orchestrator.workflow_id,
                "previous_state": prev_state.value,
                "current_state": orchestrator.state.value,
                "error": f"Stage execution failed: {exc}",
            })

    def inspect_orchestration_status(self, args: Dict[str, Any]) -> str:
        """Tool: inspect_orchestration_status."""
        try:
            inp = InspectOrchestrationStatusInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        orchestrator = self.get_orchestrator(inp.workflow_id)
        if not orchestrator:
            return json.dumps({"error": f"Workflow '{inp.workflow_id}' not found."})

        completed_stages = [
            k for k, v in orchestrator.stage_records.items() if v.status == StageStatus.COMPLETED
        ]
        out = InspectOrchestrationStatusOutput(
            workflow_id=orchestrator.workflow_id,
            state=orchestrator.state,
            repository_path=orchestrator.context.repository_path,
            created_at=orchestrator.context.created_at.isoformat(),
            completed_at=orchestrator.completed_at.isoformat() if orchestrator.completed_at else None,
            completed_stages=completed_stages,
            current_stage=orchestrator.state.value,
            requires_human_review=True,
            summary=f"Workflow is currently in state '{orchestrator.state.value}' with {len(completed_stages)} stages completed.",
        )
        return out.model_dump_json()

    def inspect_stage_result(self, args: Dict[str, Any]) -> str:
        """Tool: inspect_stage_result."""
        try:
            inp = InspectStageResultInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        orchestrator = self.get_orchestrator(inp.workflow_id)
        if not orchestrator:
            return json.dumps({"error": f"Workflow '{inp.workflow_id}' not found."})

        stage_rec = orchestrator.stage_records.get(inp.stage.value)
        status = stage_rec.status if stage_rec else StageStatus.PENDING

        data: Dict[str, Any] = {}
        if inp.stage == WorkflowStage.DIAGNOSIS and orchestrator.stage_outputs.diagnosis_result:
            data = orchestrator.stage_outputs.diagnosis_result.model_dump(mode="json")
        elif inp.stage == WorkflowStage.ROOT_CAUSE and orchestrator.stage_outputs.root_cause_report:
            data = orchestrator.stage_outputs.root_cause_report.model_dump(mode="json")
        elif inp.stage == WorkflowStage.PLANNING and orchestrator.stage_outputs.repair_plan:
            data = orchestrator.stage_outputs.repair_plan.model_dump(mode="json")
        elif inp.stage == WorkflowStage.APPROVAL and orchestrator.stage_outputs.approval:
            data = orchestrator.stage_outputs.approval.model_dump(mode="json")
        elif inp.stage == WorkflowStage.EXECUTION and orchestrator.stage_outputs.execution_result:
            data = orchestrator.stage_outputs.execution_result.model_dump(mode="json")
        elif inp.stage == WorkflowStage.VERIFICATION and orchestrator.stage_outputs.verification_result:
            data = orchestrator.stage_outputs.verification_result.model_dump(mode="json")
        elif inp.stage == WorkflowStage.QUALITY and orchestrator.stage_outputs.quality_result:
            data = orchestrator.stage_outputs.quality_result.model_dump(mode="json")

        out = InspectStageResultOutput(
            workflow_id=orchestrator.workflow_id,
            stage=inp.stage,
            status=status,
            result_data=data,
            error_message=stage_rec.error_message if stage_rec else None,
        )
        return out.model_dump_json()

    def request_orchestration_approval(self, args: Dict[str, Any]) -> str:
        """Tool: request_orchestration_approval."""
        try:
            inp = RequestOrchestrationApprovalInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        orchestrator = self.get_orchestrator(inp.workflow_id)
        if not orchestrator:
            return json.dumps({"error": f"Workflow '{inp.workflow_id}' not found."})

        plan = orchestrator.stage_outputs.repair_plan
        from forge.repair_execution.approval import compute_plan_digest
        digest = compute_plan_digest(plan) if plan else None

        out = RequestOrchestrationApprovalOutput(
            workflow_id=orchestrator.workflow_id,
            state=orchestrator.state,
            requires_human_approval=(orchestrator.state == WorkflowState.AWAITING_HUMAN_APPROVAL),
            repair_plan_id=plan.plan_id if plan else None,
            repair_plan_digest=digest,
            selected_strategy_id=plan.selected_strategy_id if plan else None,
            affected_files=plan.affected_files if plan else [],
            instruction=(
                f"Human authorization pending. Please instruct the human operator to run: "
                f"'forge orchestrate approve {orchestrator.workflow_id} "
                f"--approver <operator_identity> "
                f"--plan-id {plan.plan_id} "
                f"--plan-digest {digest} "
                f"--strategy {plan.selected_strategy_id} "
                f"--file <path>' in an interactive terminal to authorize repair."
            ) if plan else "No active repair plan awaiting approval.",
        )
        return out.model_dump_json()

    def submit_orchestration_approval(self, args: Dict[str, Any]) -> str:
        """Tool: submit_orchestration_approval.

        Enforces architectural separation: autonomous tool callers cannot manufacture
        human approval or impersonate an authorized operator.
        """
        try:
            inp = SubmitOrchestrationApprovalInput.model_validate(args)
            wf_id = inp.workflow_id
        except Exception:
            wf_id = str(args.get("workflow_id", "unknown"))

        orchestrator = self.get_orchestrator(wf_id)
        if orchestrator:
            from forge.orchestration.events import OrchestrationEventType
            orchestrator.event_recorder.record_event(
                workflow_id=wf_id,
                event_type=OrchestrationEventType.STAGE_FAILED,
                state=orchestrator.state,
                action_description="Autonomous tool attempted to manufacture human approval (rejected).",
                details={"caller": "tool_registry", "attempted_approver": args.get("approver_identity")},
            )
            orchestrator._transition(
                WorkflowState.BLOCKED,
                "Workflow blocked: Autonomous tool caller attempted to manufacture human approval.",
                stage=WorkflowStage.APPROVAL,
                details={"error": "Tool callers cannot manufacture human approval"},
            )

        out = SubmitOrchestrationApprovalOutput(
            workflow_id=wf_id,
            approval_id="none",
            is_valid=False,
            state=orchestrator.state if orchestrator else WorkflowState.BLOCKED,
            message="Approval rejected: Autonomous tool caller cannot manufacture human approval or impersonate an authorized operator.",
            errors=[
                "Autonomous tool caller cannot manufacture human approval or impersonate an authorized operator. "
                "Human authorization must be granted by an authenticated human operator via interactive CLI ('forge orchestrate approve')."
            ],
        )
        return out.model_dump_json()

    def get_engineering_report(self, args: Dict[str, Any]) -> str:
        """Tool: get_engineering_report."""
        try:
            inp = GetEngineeringReportInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        orchestrator = self.get_orchestrator(inp.workflow_id)
        if not orchestrator:
            return json.dumps({"error": f"Workflow '{inp.workflow_id}' not found."})

        result = orchestrator.get_result()
        out = GetEngineeringReportOutput(
            workflow_id=result.workflow_id,
            state=result.state,
            report_markdown=result.report_markdown,
            requires_human_review=True,
        )
        return out.model_dump_json()

    def inspect_workflow_recovery(self, args: Dict[str, Any]) -> str:
        """Tool: inspect_workflow_recovery."""
        try:
            inp = InspectWorkflowRecoveryInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        decision = self.recovery_engine.inspect_recovery(inp.workflow_id)
        out = InspectWorkflowRecoveryOutput(
            workflow_id=inp.workflow_id,
            state=decision.checkpoint.state if decision.checkpoint else WorkflowState.BLOCKED,
            can_recover=decision.can_recover,
            checkpoint_sequence=decision.checkpoint.sequence if decision.checkpoint else None,
            checkpoint_digest=decision.checkpoint.checkpoint_digest if decision.checkpoint else None,
            reconciliation_required=decision.reconciliation_required,
            blocking_reason=decision.blocking_reason,
        )
        return out.model_dump_json()

    def recover_orchestration_workflow(self, args: Dict[str, Any]) -> str:
        """Tool: recover_orchestration_workflow."""
        try:
            inp = RecoverOrchestrationWorkflowInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        try:
            decision, orchestrator = self.recovery_engine.recover_workflow(
                workflow_id=inp.workflow_id,
                allow_discard_unconfirmed_mutation=inp.allow_discard_unconfirmed_mutation,
            )
            if orchestrator:
                self.register_orchestrator(orchestrator)
            out = RecoverOrchestrationWorkflowOutput(
                workflow_id=inp.workflow_id,
                recovered=decision.can_recover,
                state=orchestrator.state if orchestrator else WorkflowState.BLOCKED,
                message=decision.blocking_reason or f"Workflow '{inp.workflow_id}' recovered successfully in state '{orchestrator.state.value if orchestrator else 'BLOCKED'}'.",
                reconciliation_required=decision.reconciliation_required,
            )
            return out.model_dump_json()
        except Exception as exc:
            return json.dumps({"error": f"Recovery failed: {exc}"})

    def inspect_workflow_tasks(self, args: Dict[str, Any]) -> str:
        """Tool: inspect_workflow_tasks."""
        try:
            inp = InspectWorkflowTasksInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        tasks = self.task_coordinator.list_tasks(inp.workflow_id)
        ready_tasks = self.task_coordinator.get_ready_tasks(inp.workflow_id)
        has_failures = any(t.state == TaskState.FAILED for t in tasks)
        out = InspectWorkflowTasksOutput(
            workflow_id=inp.workflow_id,
            tasks=[t.model_dump(mode="json") for t in tasks],
            ready_task_ids=[t.task_id for t in ready_tasks],
            has_failures=has_failures,
        )
        return out.model_dump_json()

    def reconcile_orchestration_mutation(self, args: Dict[str, Any]) -> str:
        """Tool: reconcile_orchestration_mutation.

        Enforces architectural separation: autonomous tool callers cannot reconcile
        interrupted mutations or impersonate an authorized human operator.
        """
        try:
            inp = ReconcileOrchestrationMutationInput.model_validate(args)
            wf_id = inp.workflow_id
        except Exception:
            wf_id = str(args.get("workflow_id", "unknown"))

        orchestrator = self.get_orchestrator(wf_id)
        if orchestrator:
            from forge.orchestration.events import OrchestrationEventType
            orchestrator.event_recorder.record_event(
                workflow_id=wf_id,
                event_type=OrchestrationEventType.STAGE_FAILED,
                state=orchestrator.state,
                action_description="Autonomous tool attempted to reconcile interrupted mutation (rejected).",
                details={"caller": "tool_registry", "attempted_operator": args.get("reconciled_by"), "attempted_resolution": args.get("resolution")},
            )
            orchestrator._transition(
                WorkflowState.BLOCKED,
                "Workflow blocked: Autonomous tool caller attempted to reconcile interrupted mutation.",
                stage=WorkflowStage.EXECUTION,
                details={"error": "Tool callers cannot reconcile interrupted mutations"},
            )

        out = ReconcileOrchestrationMutationOutput(
            workflow_id=wf_id,
            reconciled=False,
            state=orchestrator.state if orchestrator else WorkflowState.BLOCKED,
            error=(
                "Reconciliation rejected: Autonomous tool caller cannot reconcile interrupted mutations "
                "or impersonate an authorized human operator. Mutation reconciliation strictly requires an "
                "authenticated human operator via interactive CLI ('forge orchestrate reconcile')."
            ),
        )
        return out.model_dump_json()

    # -------------------------------------------------------------
    # Tool Definitions for ToolRegistry
    # -------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinition schemas for all orchestration tools."""
        return [
            ToolDefinition(
                name="create_orchestration_workflow",
                description="Initialize an autonomous software engineering workflow with defect inputs and safety boundaries.",
                parameters=CreateOrchestrationWorkflowInput.model_json_schema(),
            ),
            ToolDefinition(
                name="execute_orchestration_stage",
                description="Execute the next permitted stage (Diagnosis, Root Cause, Planning, Repair, Verification, Quality) in the engineering lifecycle.",
                parameters=ExecuteOrchestrationStageInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_orchestration_status",
                description="Inspect the current state, completed stages, and metadata of an active engineering workflow.",
                parameters=InspectOrchestrationStatusInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_stage_result",
                description="Retrieve structured artifacts and details produced by a specific engineering stage.",
                parameters=InspectStageResultInput.model_json_schema(),
            ),
            ToolDefinition(
                name="request_orchestration_approval",
                description="Request human operator authorization for an awaiting repair plan. Returns the pending approval requirements and instructions for the operator to authorize via interactive CLI. Autonomous tools cannot manufacture approval.",
                parameters=RequestOrchestrationApprovalInput.model_json_schema(),
            ),
            ToolDefinition(
                name="submit_orchestration_approval",
                description="Submission endpoint for human authorization. Note: Autonomous tool callers (LLMs/subagents) cannot self-authorize and will be rejected.",
                parameters=SubmitOrchestrationApprovalInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_engineering_report",
                description="Retrieve the comprehensive, evidence-backed Markdown engineering report for a workflow.",
                parameters=GetEngineeringReportInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_workflow_recovery",
                description="Inspect recovery state, last verified checkpoint, event-chain verification, and reconciliation requirements for a workflow.",
                parameters=InspectWorkflowRecoveryInput.model_json_schema(),
            ),
            ToolDefinition(
                name="recover_orchestration_workflow",
                description="Safely recover an interrupted workflow from its last valid checkpoint, validating cryptographic audit log integrity.",
                parameters=RecoverOrchestrationWorkflowInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_workflow_tasks",
                description="Inspect DAG coordination tasks, execution order, prerequisite states, and failure propagation for a workflow.",
                parameters=InspectWorkflowTasksInput.model_json_schema(),
            ),
            ToolDefinition(
                name="reconcile_orchestration_mutation",
                description="Endpoint for reconciling interrupted mutations. Note: Autonomous tool callers (LLMs/subagents) cannot self-reconcile mutations and will be rejected.",
                parameters=ReconcileOrchestrationMutationInput.model_json_schema(),
            ),
        ]
