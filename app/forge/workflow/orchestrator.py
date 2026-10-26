"""Supervised workflow orchestrator and lifecycle engine for Phase 5.

Milestone 2: Supervised Workflow State Machine & Transactional Ledger.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.config import ForgeSettings, get_settings
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.context import assemble_repository_context
from forge.workflow.intake import validate_intake_spec
from forge.workflow.models import (
    EngineeringWorkflow,
    EngineeringWorkflowEvent,
    EngineeringWorkflowState,
    HumanApprovalRequiredError,
    InvalidWorkflowTransitionError,
    WorkflowConcurrencyError,
    WorkflowIntakeSpec,
    WorkflowResumeError,
)
from forge.workflow.state import is_resumable_state, is_terminal_state


class EngineeringWorkflowOrchestrator:
    """Coordinates supervised engineering workflow execution, transitions, and auditing."""

    def __init__(
        self,
        store: Optional[ForgeStore] = None,
        settings: Optional[ForgeSettings] = None,
    ):
        self.settings = settings or get_settings()
        self.store = store or ForgeStore(db_path=self.settings.db_path)

    def start_workflow(
        self,
        intake_spec: WorkflowIntakeSpec,
        target_repo: Optional[Path] = None,
        auto_advance: bool = False,
    ) -> EngineeringWorkflow:
        """Initialize an engineering workflow from a validated intake specification.

        If auto_advance is True, validates intake and assembles repository context
        to advance the state to INTAKE_VALIDATED and REPO_ANALYZED where possible.
        """
        validate_intake_spec(intake_spec)
        self.store.save_workflow_intake(intake_spec)

        wf_id = f"wf_eng_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        workflow = EngineeringWorkflow(
            workflow_id=wf_id,
            intake_id=intake_spec.intake_id,
            target_repo=intake_spec.target_repo,
            base_commit_sha=intake_spec.base_commit_sha,
            state=EngineeringWorkflowState.CREATED,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.store.save_engineering_workflow(workflow)

        if not auto_advance:
            return workflow

        # 1. Advance to INTAKE_VALIDATED
        workflow = self.store.transition_engineering_workflow(
            workflow_id=workflow.workflow_id,
            expected_version=workflow.version,
            target_state=EngineeringWorkflowState.INTAKE_VALIDATED,
            stage="intake_validation",
            payload={
                "title": intake_spec.title,
                "source_type": intake_spec.source_type.value,
            },
        )

        # 2. Advance to REPO_ANALYZED if repository is locally accessible
        repo_path = target_repo or Path(intake_spec.target_repo)
        if repo_path.exists() and repo_path.is_dir():
            try:
                context = assemble_repository_context(repo_path, intake_spec)
                workflow = self.store.transition_engineering_workflow(
                    workflow_id=workflow.workflow_id,
                    expected_version=workflow.version,
                    target_state=EngineeringWorkflowState.REPO_ANALYZED,
                    stage="context_assembly",
                    context_id=context.context_id,
                    payload={
                        "token_estimate": context.budget_summary.estimated_tokens,
                        "files_included": len(context.source_excerpts),
                        "truncated": context.budget_summary.is_truncated,
                    },
                )
            except Exception:
                # Retain INTAKE_VALIDATED if context assembly fails
                pass

        return workflow

    def resume_workflow(self, workflow_id: str) -> EngineeringWorkflow:
        """Resume an interrupted or blocked workflow.

        Transitions blocked states back to their corresponding pending states.
        Terminal states strictly reject resumption.
        """
        wf = self.store.get_engineering_workflow(workflow_id)
        if not wf:
            raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

        if is_terminal_state(wf.state):
            raise WorkflowResumeError(
                f"Cannot resume terminal workflow '{workflow_id}' in state '{wf.state.value}'."
            )

        if wf.state == EngineeringWorkflowState.REPRO_BLOCKED:
            return self.store.transition_engineering_workflow(
                workflow_id=workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRODUCTION_PENDING,
                stage="resume_reproduction",
                payload={"reason": "Resuming from REPRO_BLOCKED"},
            )

        if wf.state == EngineeringWorkflowState.VERIFICATION_BLOCKED:
            return self.store.transition_engineering_workflow(
                workflow_id=workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.VERIFICATION_PENDING,
                stage="resume_verification",
                payload={"reason": "Resuming from VERIFICATION_BLOCKED"},
            )

        if is_resumable_state(wf.state):
            return wf

        raise WorkflowResumeError(
            f"Workflow '{workflow_id}' in state '{wf.state.value}' is not eligible for resumption."
        )

    def review_workflow(
        self,
        workflow_id: str,
        decision: str,
        notes: Optional[str] = None,
    ) -> EngineeringWorkflow:
        """Submit a mandatory human review decision ('approve' or 'reject').

        Only workflows awaiting review can be decided upon.
        """
        normalized_decision = decision.strip().lower()
        if normalized_decision not in ("approve", "reject"):
            raise ValueError(
                f"Invalid human decision '{decision}'. Must be 'approve' or 'reject'."
            )

        wf = self.store.get_engineering_workflow(workflow_id)
        if not wf:
            raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

        if wf.state != EngineeringWorkflowState.AWAITING_HUMAN_REVIEW:
            raise InvalidWorkflowTransitionError(
                f"Cannot review workflow in state '{wf.state.value}'. "
                f"Workflow must be in 'awaiting_human_review' to receive human review."
            )

        target_state = (
            EngineeringWorkflowState.APPROVED
            if normalized_decision == "approve"
            else EngineeringWorkflowState.REJECTED
        )

        return self.store.transition_engineering_workflow(
            workflow_id=workflow_id,
            expected_version=wf.version,
            target_state=target_state,
            stage="human_review",
            human_decision=normalized_decision,
            human_notes=notes,
            payload={
                "decision": normalized_decision,
                "notes": notes or "",
            },
            transition_context={"human_decision": normalized_decision},
        )

    def get_workflow(self, workflow_id: str) -> Optional[EngineeringWorkflow]:
        """Fetch an engineering workflow record by ID."""
        return self.store.get_engineering_workflow(workflow_id)

    def list_workflows(
        self,
        intake_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[EngineeringWorkflow]:
        """List engineering workflows with optional filters."""
        return self.store.list_engineering_workflows(intake_id=intake_id, state=state)

    def list_events(self, workflow_id: str) -> List[EngineeringWorkflowEvent]:
        """Fetch all append-only events for a workflow in monotonic sequence order."""
        return self.store.list_engineering_workflow_events(workflow_id)
