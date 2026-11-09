"""Phase 13 M1: Schemas and Data Contracts for End-to-End Engineering Orchestration.

Defines strict, validated Pydantic models for the entire autonomous repair orchestration
lifecycle: workflow states, stage records, execution context, typed outputs, and the
final engineering report with mandatory safety invariants.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.diagnosis.schemas import DiagnosisInput, DiagnosisResult
from forge.causality.schemas import RootCauseReport
from forge.repair_planning.schemas import RepairPlan
from forge.repair_execution.schemas import (
    ExecutionLimits,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
)
from forge.repair_verification.schemas import (
    VerificationPolicy,
    VerificationResult,
)
from forge.repair_quality.schemas import (
    RepairQualityPolicy,
    RepairQualityResult,
)


class WorkflowState(str, Enum):
    """Lifecycle states of an autonomous engineering workflow."""

    CREATED = "CREATED"
    DIAGNOSING = "DIAGNOSING"
    DIAGNOSIS_COMPLETED = "DIAGNOSIS_COMPLETED"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    ROOT_CAUSE_COMPLETED = "ROOT_CAUSE_COMPLETED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REPAIR_EXECUTING = "REPAIR_EXECUTING"
    REPAIR_COMPLETED = "REPAIR_COMPLETED"
    VERIFICATION_RUNNING = "VERIFICATION_RUNNING"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    QUALITY_EVALUATION = "QUALITY_EVALUATION"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkflowStage(str, Enum):
    """Discrete stages in the orchestration pipeline."""

    DIAGNOSIS = "DIAGNOSIS"
    ROOT_CAUSE = "ROOT_CAUSE"
    PLANNING = "PLANNING"
    APPROVAL = "APPROVAL"
    EXECUTION = "EXECUTION"
    VERIFICATION = "VERIFICATION"
    QUALITY = "QUALITY"


class StageStatus(str, Enum):
    """Operational status of an individual stage."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StageExecutionRecord(BaseModel):
    """Immutable audit record for a single stage execution."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    stage: WorkflowStage
    status: StageStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    failure_classification: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorkflowContext(BaseModel):
    """Execution context and configuration for an engineering workflow."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    workflow_id: str = Field(default_factory=lambda: f"wf_{uuid.uuid4().hex[:12]}")
    repository_path: str
    commit_sha: Optional[str] = None
    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    defect_input: Optional[DiagnosisInput] = None
    execution_limits: Optional[ExecutionLimits] = None
    verification_policy: Optional[VerificationPolicy] = None
    quality_policy: Optional[RepairQualityPolicy] = None
    trusted_approvers: Optional[Set[str]] = None


class StageOutputs(BaseModel):
    """Container holding validated outputs produced across stages."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    diagnosis_result: Optional[DiagnosisResult] = None
    root_cause_report: Optional[RootCauseReport] = None
    repair_plan: Optional[RepairPlan] = None
    approval: Optional[RepairApproval] = None
    repair_workspace: Optional[RepairWorkspace] = None
    execution_result: Optional[RepairExecutionResult] = None
    verification_result: Optional[VerificationResult] = None
    quality_result: Optional[RepairQualityResult] = None


class EngineeringWorkflowResult(BaseModel):
    """Comprehensive final outcome of an engineering orchestration workflow."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    workflow_id: str
    state: WorkflowState
    repository_path: str
    commit_sha: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    stages: Dict[str, StageExecutionRecord] = Field(default_factory=dict)

    # Stage references & digests
    diagnosis_id: Optional[str] = None
    root_cause_report_id: Optional[str] = None
    repair_plan_id: Optional[str] = None
    repair_plan_digest: Optional[str] = None
    approval_id: Optional[str] = None
    approver_identity: Optional[str] = None
    execution_id: Optional[str] = None
    workspace_id: Optional[str] = None
    applied_patch_digest: Optional[str] = None
    baseline_tree_digest: Optional[str] = None
    verification_id: Optional[str] = None
    verification_status: Optional[str] = None
    regression_classification: Optional[str] = None
    quality_gate_decision: Optional[str] = None
    quality_score: Optional[float] = None

    # Audit & Diagnostics
    policy_violations: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    event_count: int = 0
    report_markdown: str = ""

    # Non-negotiable safety invariant
    requires_human_review: bool = Field(
        default=True,
        description="Permanent safety invariant: human review remains strictly mandatory.",
    )

    @field_validator("requires_human_review")
    @classmethod
    def _validate_requires_human_review(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError(
                "Safety invariant violated: requires_human_review must strictly remain True."
            )
        return True

    @model_validator(mode="after")
    def _validate_state_invariants(self) -> "EngineeringWorkflowResult":
        if self.requires_human_review is not True:
            raise ValueError(
                "Safety invariant violated: requires_human_review must strictly remain True."
            )

        # Invariant: COMPLETED state strictly requires successful repair, verification, and quality pass
        if self.state == WorkflowState.COMPLETED:
            if not self.repair_plan_id:
                raise ValueError("Workflow cannot be COMPLETED without a verified repair plan.")
            if not self.approval_id:
                raise ValueError("Workflow cannot be COMPLETED without validated human approval.")
            if not self.execution_id:
                raise ValueError("Workflow cannot be COMPLETED without repair execution.")
            if not self.verification_status or self.verification_status.lower() != "passed":
                raise ValueError(
                    f"Workflow cannot be COMPLETED with verification status '{self.verification_status}'."
                )
            if self.regression_classification and self.regression_classification.lower() not in (
                "no_regression_observed",
                "improved",
            ):
                raise ValueError(
                    f"Workflow cannot be COMPLETED with regression classification '{self.regression_classification}'."
                )
            if not self.quality_gate_decision or self.quality_gate_decision.upper() != "PASSED":
                raise ValueError(
                    f"Workflow cannot be COMPLETED with quality gate decision '{self.quality_gate_decision}'."
                )
            if self.policy_violations:
                raise ValueError(
                    f"Workflow cannot be COMPLETED with active policy violations: {self.policy_violations}"
                )
        return self


# ---------------------------------------------------------------------------
# Tool Schemas for ToolRegistry
# ---------------------------------------------------------------------------


class CreateOrchestrationWorkflowInput(BaseModel):
    """Input parameters for creating an engineering orchestration workflow."""

    model_config = ConfigDict(extra="forbid")

    repository_path: str
    target_files: Optional[List[str]] = None
    target_symbols: Optional[List[str]] = None
    user_description: Optional[str] = None
    error_message: Optional[str] = None
    stack_trace: Optional[str] = None
    diff: Optional[str] = None
    finding_id: Optional[str] = None


class CreateOrchestrationWorkflowOutput(BaseModel):
    """Output returned when a workflow is initialized."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    state: WorkflowState
    repository_path: str
    created_at: str
    run_id: str
    message: str


class ExecuteOrchestrationStageInput(BaseModel):
    """Input parameters for executing the next permitted stage or a named stage."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    stage: Optional[WorkflowStage] = None


class ExecuteOrchestrationStageOutput(BaseModel):
    """Output returned after executing a workflow stage."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    previous_state: WorkflowState
    current_state: WorkflowState
    stage_executed: WorkflowStage
    stage_status: StageStatus
    message: str
    error: Optional[str] = None


class InspectOrchestrationStatusInput(BaseModel):
    """Input parameters for inspecting the status of a workflow."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str


class InspectOrchestrationStatusOutput(BaseModel):
    """Output returning current state and completed stages."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    state: WorkflowState
    repository_path: str
    created_at: str
    completed_at: Optional[str] = None
    completed_stages: List[str]
    current_stage: Optional[str] = None
    requires_human_review: bool = True
    summary: str


class InspectStageResultInput(BaseModel):
    """Input parameters for inspecting a specific stage's output."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    stage: WorkflowStage


class InspectStageResultOutput(BaseModel):
    """Output returning details of a specific stage."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    stage: WorkflowStage
    status: StageStatus
    result_data: Dict[str, Any]
    error_message: Optional[str] = None


class RequestOrchestrationApprovalInput(BaseModel):
    """Input parameters for requesting human operator authorization."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    reason: Optional[str] = None


class RequestOrchestrationApprovalOutput(BaseModel):
    """Output providing details needed for human operator authorization."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    state: WorkflowState
    requires_human_approval: bool
    repair_plan_id: Optional[str] = None
    repair_plan_digest: Optional[str] = None
    selected_strategy_id: Optional[str] = None
    affected_files: List[str] = Field(default_factory=list)
    instruction: str


class SubmitOrchestrationApprovalInput(BaseModel):
    """Input parameters for submitting human authorization for repair execution."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    approver_identity: str
    repair_plan_id: str
    repair_plan_digest: str
    approved_strategy_id: str
    approved_scope_file_paths: List[str]
    risk_acknowledgement: bool = False
    comments: Optional[str] = None


class SubmitOrchestrationApprovalOutput(BaseModel):
    """Output returned when human approval is submitted."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    approval_id: str
    is_valid: bool
    state: WorkflowState
    message: str
    errors: List[str] = Field(default_factory=list)


class GetEngineeringReportInput(BaseModel):
    """Input parameters for retrieving the final engineering report."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    format: str = "markdown"


class GetEngineeringReportOutput(BaseModel):
    """Output containing the formatted engineering report."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    state: WorkflowState
    report_markdown: str
    requires_human_review: bool = True
