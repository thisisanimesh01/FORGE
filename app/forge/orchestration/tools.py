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
    StageStatus,
    SubmitOrchestrationApprovalInput,
    SubmitOrchestrationApprovalOutput,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)


class OrchestrationTools:
    """Tool invocation handler for Phase 13 M1 Engineering Orchestration."""

    def __init__(
        self,
        repository_path: Path,
        max_output_chars: int = 50_000,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.max_output_chars = max_output_chars
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
        orchestrator = EngineeringOrchestrator(context=context)
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

    def submit_orchestration_approval(self, args: Dict[str, Any]) -> str:
        """Tool: submit_orchestration_approval."""
        try:
            inp = SubmitOrchestrationApprovalInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        orchestrator = self.get_orchestrator(inp.workflow_id)
        if not orchestrator:
            return json.dumps({"error": f"Workflow '{inp.workflow_id}' not found."})

        import uuid
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        approval = RepairApproval(
            approval_id=f"appr_{uuid.uuid4().hex[:12]}",
            repair_plan_id=inp.repair_plan_id,
            plan_digest=inp.repair_plan_digest,
            approved_strategy_id=inp.approved_strategy_id or "strat_001",
            approved_file_paths=inp.approved_scope_file_paths or ["src/logic.py"],
            approval_timestamp=now.isoformat(),
            approval_expiry=(now + timedelta(days=1)).isoformat(),
            approver_reference=inp.approver_identity,
            approval_status=ApprovalStatus.APPROVED,
        )
        outcome = orchestrator.submit_approval(approval)
        out = SubmitOrchestrationApprovalOutput(
            workflow_id=orchestrator.workflow_id,
            approval_id=approval.approval_id,
            is_valid=outcome.is_valid,
            state=orchestrator.state,
            message="Approval accepted and verified." if outcome.is_valid else "Approval rejected by safety boundary.",
            errors=outcome.errors,
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

    # -------------------------------------------------------------
    # Tool Definitions for ToolRegistry
    # -------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinition schemas for all 6 orchestration tools."""
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
                name="submit_orchestration_approval",
                description="Submit and verify human authorization for autonomous repair execution against the canonical repair plan digest.",
                parameters=SubmitOrchestrationApprovalInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_engineering_report",
                description="Retrieve the comprehensive, evidence-backed Markdown engineering report for a workflow.",
                parameters=GetEngineeringReportInput.model_json_schema(),
            ),
        ]
