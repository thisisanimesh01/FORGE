"""Phase 12 M4: Repair Execution Tools for LLM & Agent Invocation.

Exposes 8 structured, typed, read-only tools:
- prepare_repair_workspace: Allocates an isolated workspace with baseline integrity digest.
- validate_repair_approval: Audits human approval contracts against mandatory invariants.
- generate_repair_patch: Synthesizes a unified diff patch from an approved RepairPlan.
- validate_repair_patch: Audits unified diff syntax, hunk headers, and path containment.
- apply_approved_repair: Executes an approved patch within an isolated workspace.
- inspect_repair_execution: Inspects execution outcome, applied patch, and rollback status.
- rollback_repair_workspace: Reverts workspace modifications and restores baseline tree.
- get_repair_execution_history: Queries the immutable audit event log for an execution.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.repair_planning.schemas import RepairPlan
from forge.repair_execution.approval import RepairApprovalValidator
from forge.repair_execution.events import RepairEventRecorder
from forge.repair_execution.executor import ControlledRepairExecutor
from forge.repair_execution.patch_generator import RepairPatchGenerator
from forge.repair_execution.patch_validator import RepairPatchValidator
from forge.repair_execution.rollback import WorkspaceRollbackManager
from forge.repair_execution.schemas import (
    ApplyApprovedRepairInput,
    ApplyApprovedRepairOutput,
    GenerateRepairPatchInput,
    GenerateRepairPatchOutput,
    GetRepairExecutionHistoryInput,
    GetRepairExecutionHistoryOutput,
    InspectRepairExecutionInput,
    InspectRepairExecutionOutput,
    PatchValidationStatus,
    PrepareRepairWorkspaceInput,
    PrepareRepairWorkspaceOutput,
    RepairApproval,
    RepairPatch,
    RollbackRepairWorkspaceInput,
    RollbackRepairWorkspaceOutput,
    ValidateRepairApprovalInput,
    ValidateRepairApprovalOutput,
    ValidateRepairPatchInput,
    ValidateRepairPatchOutput,
)
from forge.repair_execution.workspace import RepairWorkspaceManager
from forge.repository.validation import SecurityError


class RepairExecutionTools:
    """Provides validated tool execution handlers for autonomous repair execution."""

    def __init__(
        self,
        repository_path: Path,
        workspace_manager: Optional[RepairWorkspaceManager] = None,
        event_recorder: Optional[RepairEventRecorder] = None,
        max_output_chars: int = 50_000,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.max_output_chars = max_output_chars
        self.workspace_manager = workspace_manager or RepairWorkspaceManager(original_repo_path=self.repository_path)
        self.event_recorder = event_recorder or RepairEventRecorder()
        self.executor = ControlledRepairExecutor(
            repo_path=self.repository_path,
            workspace_manager=self.workspace_manager,
            event_recorder=self.event_recorder,
        )
        self.approval_validator = RepairApprovalValidator()
        self.patch_generator = RepairPatchGenerator()
        self.patch_validator = RepairPatchValidator()
        self.rollback_manager = WorkspaceRollbackManager()
        self._plans: Dict[str, RepairPlan] = {}

    def register_plan(self, plan: RepairPlan) -> None:
        """Register a known RepairPlan in memory for tool lookups."""
        self._plans[plan.plan_id] = plan

    def revoke_approval(self, approval_id: str, reason: str = "revoked by human operator") -> bool:
        """Revoke a human approval, preventing any future execution."""
        return self.executor.revoke_approval(approval_id, reason)

    # -----------------------------------------------------------------------
    # Tool Handlers
    # -----------------------------------------------------------------------

    def prepare_repair_workspace(self, args: Dict[str, Any]) -> str:
        """Allocate an isolated workspace initialized with clean repository files."""
        try:
            params = PrepareRepairWorkspaceInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for prepare_repair_workspace: {e}"})

        repo_path = Path(params.repository_path).resolve()
        ws_mgr = self.workspace_manager
        if repo_path != self.repository_path:
            ws_mgr = RepairWorkspaceManager(original_repo_path=repo_path)

        try:
            ws = ws_mgr.allocate_workspace(
                allowed_paths=params.allowed_file_paths,
                plan_id=params.plan_id,
            )
            output = PrepareRepairWorkspaceOutput(
                workspace_id=ws.workspace_id,
                isolated_path=ws.isolated_workspace_path,
                baseline_commit=ws.baseline_commit,
                baseline_tree_digest=ws.baseline_tree_digest,
                status=ws.workspace_status,
            )
            return output.model_dump_json(indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to allocate repair workspace: {exc}"})

    def validate_repair_approval(self, args: Dict[str, Any]) -> str:
        """Audit a human approval contract against mandatory authorization invariants."""
        try:
            params = ValidateRepairApprovalInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for validate_repair_approval: {e}"})

        try:
            approval = RepairApproval.model_validate_json(params.approval_json)
        except Exception as e:
            return json.dumps({"error": f"Failed to parse approval JSON: {e}"})

        plan = None
        if params.plan_json:
            try:
                plan = RepairPlan.model_validate_json(params.plan_json)
                self.register_plan(plan)
            except Exception as e:
                return json.dumps({"error": f"Failed to parse plan JSON: {e}"})
        elif params.plan_id and params.plan_id in self._plans:
            plan = self._plans[params.plan_id]

        check = self.approval_validator.validate_approval(
            approval=approval,
            plan=plan,
            revoked_approvals=set(self.executor._revoked_approvals.keys()),
            consumed_approvals=self.executor._consumed_approvals,
        )
        output = ValidateRepairApprovalOutput(
            is_valid=check.is_valid,
            approval_status=approval.approval_status,
            validation_errors=check.errors,
            approved_plan_id=approval.repair_plan_id,
            approved_strategy_id=approval.approved_strategy_id,
            approved_file_paths=approval.approved_file_paths,
        )
        return output.model_dump_json(indent=2)

    def generate_repair_patch(self, args: Dict[str, Any]) -> str:
        """Synthesize a unified diff patch from an approved RepairPlan."""
        try:
            params = GenerateRepairPatchInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for generate_repair_patch: {e}"})

        plan = None
        if params.plan_json:
            try:
                plan = RepairPlan.model_validate_json(params.plan_json)
                self.register_plan(plan)
            except Exception as e:
                return json.dumps({"error": f"Failed to parse plan JSON: {e}"})
        elif params.plan_id in self._plans:
            plan = self._plans[params.plan_id]

        if not plan:
            return json.dumps({"error": f"Repair plan '{params.plan_id}' not found in registry."})

        try:
            patch = self.patch_generator.generate_patch(plan=plan, strategy_id=params.strategy_id)
            output = GenerateRepairPatchOutput(
                patch_id=patch.patch_id,
                unified_diff=patch.unified_diff,
                affected_files=patch.affected_files,
                patch_digest=patch.patch_digest,
                added_lines=patch.added_lines,
                removed_lines=patch.removed_lines,
                validation_status=patch.validation_status,
            )
            return output.model_dump_json(indent=2)
        except Exception as e:
            return json.dumps({"error": f"Failed to generate repair patch: {e}"})

    def validate_repair_patch(self, args: Dict[str, Any]) -> str:
        """Validate syntax, line boundaries, and file scope of a repair patch."""
        try:
            params = ValidateRepairPatchInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for validate_repair_patch: {e}"})

        val_result = self.patch_validator.validate_patch(
            unified_diff=params.unified_diff,
            allowed_paths=params.allowed_file_paths,
            expected_digest=params.expected_patch_digest,
        )

        output = ValidateRepairPatchOutput(
            is_valid=val_result.is_valid,
            validation_status=val_result.status,
            validation_errors=val_result.errors,
            affected_files=val_result.affected_files,
            added_lines=val_result.added_lines,
            removed_lines=val_result.removed_lines,
            patch_digest=val_result.patch_digest,
        )
        return output.model_dump_json(indent=2)

    def apply_approved_repair(self, args: Dict[str, Any]) -> str:
        """Apply an approved repair patch to an isolated workspace."""
        try:
            params = ApplyApprovedRepairInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for apply_approved_repair: {e}"})

        try:
            approval = RepairApproval.model_validate_json(params.approval_json)
        except Exception as e:
            return json.dumps({"error": f"Failed to parse approval JSON: {e}"})

        plan = None
        if params.plan_json:
            try:
                plan = RepairPlan.model_validate_json(params.plan_json)
                self.register_plan(plan)
            except Exception as e:
                return json.dumps({"error": f"Failed to parse plan JSON: {e}"})
        elif params.plan_id in self._plans:
            plan = self._plans[params.plan_id]

        if not plan:
            return json.dumps({"error": f"Repair plan '{params.plan_id}' not found."})

        ws = self.workspace_manager.get_workspace(params.workspace_id)
        if not ws:
            return json.dumps({"error": f"Workspace '{params.workspace_id}' not found."})

        custom_patch = None
        if params.unified_diff:
            from forge.repair_execution.patch_validator import compute_patch_digest
            p_digest = compute_patch_digest(params.unified_diff)
            custom_patch = RepairPatch(
                patch_id=params.patch_id or "custom_patch",
                repair_plan_id=plan.plan_id,
                strategy_id=approval.approved_strategy_id,
                unified_diff=params.unified_diff,
                affected_files=approval.approved_file_paths,
                patch_digest=p_digest,
            )

        try:
            res = self.executor.execute_repair(
                plan=plan,
                approval=approval,
                workspace=ws,
                custom_patch=custom_patch,
            )
            output = ApplyApprovedRepairOutput(
                execution_id=res.execution_id,
                execution_status=res.execution_status,
                modified_files=res.modified_files,
                applied_patch_digest=res.applied_patch_digest,
                diff_summary=res.diff_summary,
                rollback_status=res.rollback_status,
                failure_classification=res.failure_classification,
                verification_pending=res.verification_pending,
            )
            return output.model_dump_json(indent=2)
        except Exception as e:
            return json.dumps({"error": f"Execution failed: {e}"})

    def inspect_repair_execution(self, args: Dict[str, Any]) -> str:
        """Inspect the outcome and state of a completed repair execution."""
        try:
            params = InspectRepairExecutionInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for inspect_repair_execution: {e}"})

        res = self.executor.get_execution(params.execution_id)
        if not res:
            return json.dumps({"error": f"Execution '{params.execution_id}' not found."})

        output = InspectRepairExecutionOutput(
            execution_id=res.execution_id,
            repair_plan_id=res.repair_plan_id,
            execution_status=res.execution_status,
            modified_files=res.modified_files,
            applied_patch_digest=res.applied_patch_digest,
            rollback_status=res.rollback_status,
            failure_classification=res.failure_classification,
            event_count=len(res.execution_events),
            verification_pending=res.verification_pending,
        )
        return output.model_dump_json(indent=2)

    def rollback_repair_workspace(self, args: Dict[str, Any]) -> str:
        """Revert modifications in an isolated workspace to restore its baseline tree."""
        try:
            params = RollbackRepairWorkspaceInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for rollback_repair_workspace: {e}"})

        ws = self.workspace_manager.get_workspace(params.workspace_id)
        if not ws:
            return json.dumps({"error": f"Workspace '{params.workspace_id}' not found."})

        try:
            status, restored, digest = self.rollback_manager.rollback(workspace=ws)
            output = RollbackRepairWorkspaceOutput(
                workspace_id=ws.workspace_id,
                rollback_status=status,
                restored_files=restored,
                workspace_tree_digest=digest,
                message=f"Rollback {status.value}. {len(restored)} files restored.",
            )
            return output.model_dump_json(indent=2)
        except Exception as e:
            return json.dumps({"error": f"Rollback failed: {e}"})

    def get_repair_execution_history(self, args: Dict[str, Any]) -> str:
        """Retrieve the immutable audit events recorded for a repair execution."""
        try:
            params = GetRepairExecutionHistoryInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for get_repair_execution_history: {e}"})

        events = self.event_recorder.get_events(params.execution_id)
        output = GetRepairExecutionHistoryOutput(
            execution_id=params.execution_id,
            events=[e.model_dump() for e in events],
            event_count=len(events),
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool Definitions & Registry Interface
    # -----------------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinitions for repair execution tools."""
        return [
            ToolDefinition(
                name="prepare_repair_workspace",
                description="Allocate an isolated workspace for repair execution with baseline integrity tracking.",
                parameters=PrepareRepairWorkspaceInput.model_json_schema(),
            ),
            ToolDefinition(
                name="validate_repair_approval",
                description="Audit a human approval contract against mandatory authorization invariants.",
                parameters=ValidateRepairApprovalInput.model_json_schema(),
            ),
            ToolDefinition(
                name="generate_repair_patch",
                description="Synthesize a bounded unified diff patch from an approved RepairPlan.",
                parameters=GenerateRepairPatchInput.model_json_schema(),
            ),
            ToolDefinition(
                name="validate_repair_patch",
                description="Validate syntax, line boundaries, and file scope of a repair patch.",
                parameters=ValidateRepairPatchInput.model_json_schema(),
            ),
            ToolDefinition(
                name="apply_approved_repair",
                description="Apply an approved repair patch to an isolated workspace without touching original repo.",
                parameters=ApplyApprovedRepairInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_repair_execution",
                description="Inspect the execution outcome, modified files, and status of a repair execution.",
                parameters=InspectRepairExecutionInput.model_json_schema(),
            ),
            ToolDefinition(
                name="rollback_repair_workspace",
                description="Revert modifications in an isolated workspace to restore its clean baseline tree.",
                parameters=RollbackRepairWorkspaceInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_repair_execution_history",
                description="Retrieve the immutable audit event trail for a repair execution.",
                parameters=GetRepairExecutionHistoryInput.model_json_schema(),
            ),
        ]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a repair execution tool by name with arguments and return sanitized result."""
        handlers = {
            "prepare_repair_workspace": self.prepare_repair_workspace,
            "validate_repair_approval": self.validate_repair_approval,
            "generate_repair_patch": self.generate_repair_patch,
            "validate_repair_patch": self.validate_repair_patch,
            "apply_approved_repair": self.apply_approved_repair,
            "inspect_repair_execution": self.inspect_repair_execution,
            "rollback_repair_workspace": self.rollback_repair_workspace,
            "get_repair_execution_history": self.get_repair_execution_history,
        }

        if name not in handlers:
            return json.dumps({
                "error": f"Tool '{name}' is not recognized in RepairExecutionTools.",
                "available_tools": list(handlers.keys()),
            })

        try:
            raw_output = handlers[name](arguments)
            redacted = redact_secrets(raw_output)
            if len(redacted) > self.max_output_chars:
                return (
                    redacted[: self.max_output_chars]
                    + f"\n\n[TRUNCATED: Output exceeded maximum allowed size of {self.max_output_chars} characters]"
                )
            return redacted
        except SecurityError as sec_err:
            return json.dumps({"error": f"Security violation: {sec_err}"})
        except Exception as exc:
            return json.dumps({"error": f"Unexpected tool execution error: {exc}"})
