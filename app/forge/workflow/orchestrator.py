"""Supervised workflow orchestrator and lifecycle engine for Phase 5.

Milestone 2: Supervised Workflow State Machine & Transactional Ledger.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from forge.config import ForgeSettings, get_settings
from forge.reproduction.manager import ReproductionCaseManager
from forge.reproduction.models import CaseStatus, generate_case_id
from forge.sandbox.contracts import ReproductionCommand, ReproductionOutcome, ReproductionRequest
from forge.schemas import EvidenceType
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.context import assemble_repository_context
from forge.workflow.evidence_chain import (
    ensure_finding_in_store,
    extract_reproduction_command_from_intake,
    resolve_intake_finding_id,
    validate_reproduction_evidence,
)
from forge.workflow.intake import validate_intake_spec
from forge.workflow.models import (
    EngineeringWorkflow,
    EngineeringWorkflowEvent,
    EngineeringWorkflowState,
    HumanApprovalRequiredError,
    InvalidWorkflowTransitionError,
    MissingEvidenceError,
    MultiCheckVerificationResult,
    MultiCheckVerificationStageResult,
    RepairPlanningStageResult,
    ReproductionStageResult,
    RevisionDriftError,
    StaleRevisionEvidenceError,
    VerificationAggregateOutcome,
    WorkflowConcurrencyError,
    WorkflowIntakeSpec,
    WorkflowResumeError,
)
from forge.workflow.state import is_resumable_state, is_terminal_state
from forge.workflow.verification import MultiCheckVerifier, build_default_verification_plan


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

        existing = self.store.list_engineering_workflows(intake_id=intake_spec.intake_id)
        active = [w for w in existing if not is_terminal_state(w.state)]
        if active:
            return active[0]

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
        target_repo: Optional[Union[str, Path]] = None,
        require_verified_candidate: bool = False,
    ) -> EngineeringWorkflow:
        """Submit a mandatory human review decision ('approve' or 'reject').

        Only workflows awaiting review can be decided upon.
        On approval, verifies candidate patch digest, verification status, and git revision.
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

        if normalized_decision == "approve":
            # 1. Require candidate presence if enforced
            if require_verified_candidate and not wf.repair_plan_id:
                raise ValueError(
                    f"Workflow '{workflow_id}' has no associated repair plan candidate to approve."
                )

            # 2. If repair plan is attached, verify patch integrity
            if wf.repair_plan_id:
                plan = self.store.get_repair_plan(wf.repair_plan_id)
                if not plan or not plan.patch_candidate:
                    raise ValueError(
                        f"Repair plan '{wf.repair_plan_id}' or patch candidate not found in store."
                    )
                from forge.repair.contracts import compute_patch_sha256
                expected_digest = plan.patch_candidate.sha256_digest
                computed_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
                if expected_digest != computed_digest:
                    raise ValueError(
                        f"Candidate patch SHA-256 digest mismatch: expected '{expected_digest}', computed '{computed_digest}'. "
                        "Patch candidate diff does not match stored integrity digest."
                    )

            # 3. If verification attempt is attached, verify outcome and regressions
            if wf.verification_attempt_id:
                vresult = self.store.get_multi_check_verification(wf.verification_attempt_id)
                if not vresult:
                    raise ValueError(
                        f"Verification attempt '{wf.verification_attempt_id}' not found in store."
                    )
                if vresult.aggregate_outcome != VerificationAggregateOutcome.PASSED:
                    raise ValueError(
                        f"Cannot approve workflow with unverified aggregate outcome: '{vresult.aggregate_outcome.value}'."
                    )
                if vresult.regression_detected:
                    raise ValueError(
                        "Cannot approve workflow with detected regressions."
                    )

            # 4. Check target repository revision drift if repository is accessible
            repo_path = Path(target_repo) if target_repo else Path(wf.target_repo)
            if repo_path.exists() and (repo_path / ".git").exists():
                from forge.repository.acquisition import run_git_command
                try:
                    git_res = run_git_command(["rev-parse", "HEAD"], cwd=repo_path, timeout_seconds=5)
                    if git_res.returncode == 0:
                        current_sha = git_res.stdout.strip().lower()
                        expected_sha = wf.base_commit_sha.strip().lower()
                        if not current_sha.startswith(expected_sha) and not expected_sha.startswith(current_sha):
                            raise RevisionDriftError(
                                f"Target repository revision '{current_sha}' does not match workflow base commit '{wf.base_commit_sha}'. "
                                "Stale revisions invalidate approval. Workspace drift detected."
                            )
                except RevisionDriftError:
                    raise
                except Exception:
                    pass

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
                "repair_plan_id": wf.repair_plan_id,
                "verification_attempt_id": wf.verification_attempt_id,
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

    def reproduce_defect(
        self,
        workflow_id: str,
        workspace_path: Optional[Path] = None,
        command_args: Optional[List[str]] = None,
        expected_exit_codes: Optional[List[int]] = None,
        expected_output_patterns: Optional[List[str]] = None,
        max_retries: int = 0,
        harness: Optional[Any] = None,
    ) -> EngineeringWorkflow:
        """Execute or resume defect reproduction and chain validated evidence into the workflow."""
        wf = self.store.get_engineering_workflow(workflow_id)
        if not wf:
            raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

        # Idempotent return if already in REPRODUCED
        if wf.state == EngineeringWorkflowState.REPRODUCED:
            return wf

        if wf.state not in (
            EngineeringWorkflowState.REPO_ANALYZED,
            EngineeringWorkflowState.REPRODUCTION_PENDING,
            EngineeringWorkflowState.REPRO_BLOCKED,
        ):
            raise InvalidWorkflowTransitionError(
                f"Cannot run reproduction for workflow in state '{wf.state.value}'. "
                f"Workflow must be in 'repo_analyzed', 'reproduction_pending', or 'repro_blocked'."
            )

        intake = self.store.get_workflow_intake(wf.intake_id)
        if not intake:
            raise StoreError(f"Intake specification '{wf.intake_id}' not found.")

        repo_path = workspace_path or Path(wf.target_repo)
        if (repo_path / ".git").exists():
            from forge.repository.acquisition import run_git_command
            try:
                git_res = run_git_command(["rev-parse", "HEAD"], cwd=repo_path, timeout_seconds=5)
                if git_res.returncode == 0:
                    current_sha = git_res.stdout.strip().lower()
                    expected_sha = wf.base_commit_sha.strip().lower()
                    if not current_sha.startswith(expected_sha) and not expected_sha.startswith(current_sha):
                        raise RevisionDriftError(
                            f"Workspace revision '{current_sha}' does not match workflow base commit '{wf.base_commit_sha}'. Revision drift detected."
                        )
            except RevisionDriftError:
                raise
            except Exception:
                pass

        # Transition to REPRODUCTION_PENDING if currently in REPO_ANALYZED or REPRO_BLOCKED
        if wf.state == EngineeringWorkflowState.REPO_ANALYZED:
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRODUCTION_PENDING,
                stage="start_reproduction",
            )
        elif wf.state == EngineeringWorkflowState.REPRO_BLOCKED:
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRODUCTION_PENDING,
                stage="resume_reproduction",
                payload={"reason": "Retrying reproduction from blocked state"},
            )

        finding = ensure_finding_in_store(self.store, intake, run_id=wf.workflow_id)

        # Derive reproduction command
        repro_cmd: Optional[ReproductionCommand] = None
        if command_args:
            repro_cmd = ReproductionCommand(
                args=list(command_args),
                relative_cwd=".",
                expected_exit_codes=expected_exit_codes or [1],
                expected_output_patterns=expected_output_patterns or [],
            )
        else:
            repro_cmd = extract_reproduction_command_from_intake(intake)

        if repro_cmd is None:
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRO_INCONCLUSIVE,
                stage="reproduction_planning",
                payload={"reason": "No safe reproduction command could be derived from intake or inputs."},
            )

        manager = ReproductionCaseManager(store=self.store, harness=harness)
        case_id = generate_case_id(finding.finding_id, str(repo_path.resolve()), wf.base_commit_sha)
        case = manager.get_case(case_id)

        # Idempotency check: if case already reproduced with valid evidence, reuse it
        if case and case.status == CaseStatus.REPRODUCED:
            ev_list = self.store.list_evidence_for_finding(finding.finding_id)
            repro_evs = [e for e in ev_list if e.evidence_type == EvidenceType.REPRODUCTION_RESULT]
            if repro_evs:
                latest_ev = repro_evs[-1]
                validate_reproduction_evidence(latest_ev, wf, intake, case, self.store)
                return self.store.transition_engineering_workflow(
                    workflow_id=wf.workflow_id,
                    expected_version=wf.version,
                    target_state=EngineeringWorkflowState.REPRODUCED,
                    stage="reproduction_idempotent_reuse",
                    repro_case_id=case.case_id,
                    payload={"case_id": case.case_id, "evidence_id": latest_ev.evidence_id, "reused": True},
                )

        if case is None:
            contract = ReproductionRequest(
                request_id=f"req_{finding.finding_id}_{uuid.uuid4().hex[:8]}",
                finding_id=finding.finding_id,
                target_repo=str(repo_path.resolve()),
                commit_sha=wf.base_commit_sha,
                hypothesis=f"Reproduction hypothesis for {finding.finding_id}",
                command=repro_cmd,
            )
            case = manager.create_case(
                finding_id=finding.finding_id,
                target_repo=str(repo_path.resolve()),
                target_commit_sha=wf.base_commit_sha,
                contract=contract,
                run_id=wf.workflow_id,
                max_retries=max_retries,
                is_idempotent=True,
            )

        attempt = manager.run_attempt(
            case_id=case.case_id,
            workspace_path=repo_path.resolve(),
            current_commit_sha=wf.base_commit_sha,
        )

        stage_res = ReproductionStageResult(
            case_id=case.case_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            outcome=attempt.outcome.value,
            exit_code=attempt.exit_code,
            evidence_id=attempt.evidence_id,
            sandbox_available=(attempt.outcome != ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE),
            duration_ms=attempt.duration_ms,
            diagnostic_message=attempt.diagnostic_message,
        )

        if attempt.outcome in (ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE, ReproductionOutcome.BLOCKED):
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRO_BLOCKED,
                stage="defect_reproduction",
                repro_case_id=case.case_id,
                error_message=attempt.diagnostic_message or "Sandbox unavailable or environment blocked",
                payload=stage_res.model_dump(mode="json"),
            )
        elif attempt.outcome == ReproductionOutcome.REPRODUCED:
            evidence = self.store.get_evidence(attempt.evidence_id) if attempt.evidence_id else None
            validate_reproduction_evidence(evidence, wf, intake, case, self.store)
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRODUCED,
                stage="defect_reproduction",
                repro_case_id=case.case_id,
                payload=stage_res.model_dump(mode="json"),
            )
        elif attempt.outcome in (ReproductionOutcome.NOT_REPRODUCED, ReproductionOutcome.TIMEOUT):
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRO_FAILED,
                stage="defect_reproduction",
                repro_case_id=case.case_id,
                error_message=attempt.diagnostic_message or f"Reproduction failed with outcome: {attempt.outcome.value}",
                payload=stage_res.model_dump(mode="json"),
            )
        else:
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPRO_INCONCLUSIVE,
                stage="defect_reproduction",
                repro_case_id=case.case_id,
                error_message=attempt.diagnostic_message or f"Reproduction inconclusive with outcome: {attempt.outcome.value}",
                payload=stage_res.model_dump(mode="json"),
            )

    def plan_repair(
        self,
        workflow_id: str,
        workspace_path: Optional[Path] = None,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        scratch_workspace_path: Optional[Path] = None,
        planner: Optional[Any] = None,
    ) -> EngineeringWorkflow:
        """Validate reproduction evidence and generate an evidence-grounded repair plan."""
        from forge.repair.models import RepairPlanStatus
        from forge.repair.planner import RepairPlanner

        wf = self.store.get_engineering_workflow(workflow_id)
        if not wf:
            raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

        # Idempotent return if already planned and validated
        if wf.state == EngineeringWorkflowState.REPAIR_PENDING and wf.repair_plan_id:
            existing_plan = self.store.get_repair_plan(wf.repair_plan_id)
            if existing_plan and existing_plan.status == RepairPlanStatus.VALIDATED:
                return wf

        # Allow REPRODUCED or interrupted REPAIR_PENDING
        if wf.state not in (
            EngineeringWorkflowState.REPRODUCED,
            EngineeringWorkflowState.REPAIR_PENDING,
        ):
            raise InvalidWorkflowTransitionError(
                f"Cannot plan repair for workflow in state '{wf.state.value}'. "
                f"Workflow must be in 'reproduced' state (or 'repair_pending' to resume)."
            )

        intake = self.store.get_workflow_intake(wf.intake_id)
        if not intake:
            raise StoreError(f"Intake specification '{wf.intake_id}' not found.")

        repo_path = workspace_path or Path(wf.target_repo)
        if (repo_path / ".git").exists():
            from forge.repository.acquisition import run_git_command
            try:
                git_res = run_git_command(["rev-parse", "HEAD"], cwd=repo_path, timeout_seconds=5)
                if git_res.returncode == 0:
                    current_sha = git_res.stdout.strip().lower()
                    expected_sha = wf.base_commit_sha.strip().lower()
                    if not current_sha.startswith(expected_sha) and not expected_sha.startswith(current_sha):
                        raise RevisionDriftError(
                            f"Workspace revision '{current_sha}' does not match workflow base commit '{wf.base_commit_sha}'. Revision drift detected."
                        )
            except RevisionDriftError:
                raise
            except Exception:
                pass

        fid = resolve_intake_finding_id(intake)
        finding = self.store.get_finding(fid)
        evidence_records = self.store.list_evidence_for_finding(fid)
        repro_evidence = [e for e in evidence_records if e.evidence_type == EvidenceType.REPRODUCTION_RESULT]
        if not repro_evidence:
            raise MissingEvidenceError(
                f"Workflow '{workflow_id}' has no reproduction evidence attached to finding '{fid}'."
            )

        case = None
        if wf.repro_case_id:
            case = ReproductionCaseManager(store=self.store).get_case(wf.repro_case_id)

        latest_repro_ev = repro_evidence[-1]
        validate_reproduction_evidence(latest_repro_ev, wf, intake, case, self.store)

        # Transition to REPAIR_PENDING with verified evidence context if not already REPAIR_PENDING
        if wf.state != EngineeringWorkflowState.REPAIR_PENDING:
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPAIR_PENDING,
                stage="start_repair_planning",
                transition_context={"has_repro_evidence": True},
            )

        repair_planner = planner or RepairPlanner(provider_name=provider_name, model_name=model_name)
        plan = repair_planner.plan_repair(
            finding_id=fid,
            target_repo=repo_path,
            base_commit_sha=wf.base_commit_sha,
            finding=finding,
            evidence_records=evidence_records,
            scratch_workspace_path=scratch_workspace_path,
            store=self.store,
            run_id=wf.workflow_id,
        )

        if plan.status == RepairPlanStatus.VALIDATED:
            if hasattr(self.store, "get_repair_plan") and hasattr(self.store, "save_repair_plan"):
                if not self.store.get_repair_plan(plan.plan_id):
                    self.store.save_repair_plan(plan)

            patch_digest = plan.patch_candidate.sha256_digest if plan.patch_candidate else None
            lines_changed = (plan.patch_candidate.added_lines + plan.patch_candidate.removed_lines) if plan.patch_candidate else 0
            stage_res = RepairPlanningStageResult(
                plan_id=plan.plan_id,
                status=plan.status.value,
                patch_digest=patch_digest,
                target_file=plan.target_file,
                lines_changed=lines_changed,
            )
            return self.store.attach_repair_plan_to_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                repair_plan_id=plan.plan_id,
                stage="repair_planning_completed",
                payload=stage_res.model_dump(mode="json"),
            )
        else:
            plan_violations = (
                plan.validation.violations
                if (plan.validation and plan.validation.violations)
                else (getattr(plan, "violations", None) or [plan.status.value])
            )
            stage_res = RepairPlanningStageResult(
                plan_id=plan.plan_id,
                status=plan.status.value,
                violations=plan_violations,
            )
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REPAIR_FAILED,
                stage="repair_planning_failed",
                error_message="; ".join(plan_violations),
                payload=stage_res.model_dump(mode="json"),
            )

    def verify_repair(
        self,
        workflow_id: str,
        workspace_path: Optional[Path] = None,
        scratch_workspace_path: Optional[Path] = None,
        verifier: Optional[Any] = None,
        include_lint: bool = True,
        include_security: bool = True,
        baseline_test_command: Optional[List[str]] = None,
    ) -> EngineeringWorkflow:
        """Execute sandboxed multi-check verification for a proposed repair plan."""
        from forge.repair.models import RepairPlan

        wf = self.store.get_engineering_workflow(workflow_id)
        if not wf:
            raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

        # Idempotent return if already verified or awaiting human review
        if wf.state in (EngineeringWorkflowState.VERIFIED, EngineeringWorkflowState.AWAITING_HUMAN_REVIEW):
            return wf

        # Check state: can verify from REPAIR_PENDING, VERIFICATION_PENDING, or resume from VERIFICATION_BLOCKED
        if wf.state not in (
            EngineeringWorkflowState.REPAIR_PENDING,
            EngineeringWorkflowState.VERIFICATION_PENDING,
            EngineeringWorkflowState.VERIFICATION_BLOCKED,
        ):
            raise InvalidWorkflowTransitionError(
                f"Cannot verify repair for workflow in state '{wf.state.value}'. "
                f"Workflow must be in 'repair_pending', 'verification_pending', or 'verification_blocked'."
            )

        if not wf.repair_plan_id:
            raise StoreError(f"Workflow '{workflow_id}' has no associated repair plan to verify.")

        plan: Optional[RepairPlan] = self.store.get_repair_plan(wf.repair_plan_id)
        if not plan:
            raise StoreError(f"Repair plan '{wf.repair_plan_id}' not found in store.")

        intake = self.store.get_workflow_intake(wf.intake_id)
        if not intake:
            raise StoreError(f"Intake specification '{wf.intake_id}' not found.")

        repo_path = workspace_path or Path(wf.target_repo)
        if (repo_path / ".git").exists():
            from forge.repository.acquisition import run_git_command
            try:
                git_res = run_git_command(["rev-parse", "HEAD"], cwd=repo_path, timeout_seconds=5)
                if git_res.returncode == 0:
                    current_sha = git_res.stdout.strip().lower()
                    expected_sha = wf.base_commit_sha.strip().lower()
                    if not current_sha.startswith(expected_sha) and not expected_sha.startswith(current_sha):
                        raise RevisionDriftError(
                            f"Workspace revision '{current_sha}' does not match workflow base commit '{wf.base_commit_sha}'. Revision drift detected."
                        )
            except RevisionDriftError:
                raise
            except Exception:
                pass

        # Transition to VERIFICATION_PENDING if not already
        if wf.state != EngineeringWorkflowState.VERIFICATION_PENDING:
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.VERIFICATION_PENDING,
                stage="start_verification",
            )

        multi_verifier = verifier or MultiCheckVerifier()
        vplan = build_default_verification_plan(
            workflow_id=wf.workflow_id,
            repair_plan=plan,
            intake_spec=intake,
            repo_path=repo_path,
            include_lint=include_lint,
            include_security=include_security,
            baseline_test_command=baseline_test_command,
        )

        vresult = multi_verifier.execute_verification(
            plan=vplan,
            repair_plan=plan,
            target_repo=repo_path,
            scratch_parent=scratch_workspace_path,
        )

        self.store.save_multi_check_verification(vresult)

        stage_res = MultiCheckVerificationStageResult(
            attempt_id=vresult.attempt_id,
            repair_plan_id=plan.plan_id,
            aggregate_outcome=vresult.aggregate_outcome.value,
            checks_total=len(vresult.candidate_results) or len(vplan.checks),
            checks_passed=sum(1 for c in vresult.candidate_results if c.status.value == "passed"),
            checks_failed=sum(1 for c in vresult.candidate_results if c.status.value == "failed"),
            checks_skipped=sum(1 for c in vresult.candidate_results if c.status.value == "skipped"),
            regression_detected=vresult.regression_detected,
            sandbox_available=vresult.sandbox_available,
            duration_ms=vresult.total_duration_ms,
            diagnostic=vresult.diagnostic,
        )

        if vresult.aggregate_outcome == VerificationAggregateOutcome.PASSED:
            # 1. VERIFICATION_PENDING -> VERIFIED
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.VERIFIED,
                stage="multi_check_verification",
                verification_attempt_id=vresult.attempt_id,
                regression_summary={
                    "regression_detected": False,
                    "checks_total": stage_res.checks_total,
                    "checks_passed": stage_res.checks_passed,
                    "limitations": vresult.limitations,
                },
                transition_context={"verification_outcome": "passed"},
                payload=stage_res.model_dump(mode="json"),
            )

            # 2. VERIFIED -> REGRESSION_CHECKING
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.REGRESSION_CHECKING,
                stage="regression_evaluation",
                verification_attempt_id=vresult.attempt_id,
                regression_summary={
                    "regression_detected": False,
                    "checks_total": stage_res.checks_total,
                },
            )

            # 3. REGRESSION_CHECKING -> AWAITING_HUMAN_REVIEW (Halts strictly for human decision)
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
                stage="awaiting_operator_review",
                verification_attempt_id=vresult.attempt_id,
                regression_summary={"regression_detected": False},
                transition_context={"regression_detected": False},
            )
            return wf

        elif vresult.aggregate_outcome == VerificationAggregateOutcome.BLOCKED:
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.VERIFICATION_BLOCKED,
                stage="verification_blocked",
                verification_attempt_id=vresult.attempt_id,
                error_message=vresult.diagnostic,
                payload=stage_res.model_dump(mode="json"),
            )

        elif vresult.aggregate_outcome == VerificationAggregateOutcome.FAILED:
            if vresult.regression_detected:
                # Target reproduction was fixed, but regression occurred
                wf = self.store.transition_engineering_workflow(
                    workflow_id=wf.workflow_id,
                    expected_version=wf.version,
                    target_state=EngineeringWorkflowState.VERIFIED,
                    stage="defect_oracle_passed",
                    verification_attempt_id=vresult.attempt_id,
                    transition_context={"verification_outcome": "passed"},
                )
                wf = self.store.transition_engineering_workflow(
                    workflow_id=wf.workflow_id,
                    expected_version=wf.version,
                    target_state=EngineeringWorkflowState.REGRESSION_CHECKING,
                    stage="regression_evaluation",
                    verification_attempt_id=vresult.attempt_id,
                    regression_summary={"regression_detected": True},
                )
                return self.store.transition_engineering_workflow(
                    workflow_id=wf.workflow_id,
                    expected_version=wf.version,
                    target_state=EngineeringWorkflowState.REGRESSION_FAILED,
                    stage="regression_detected",
                    verification_attempt_id=vresult.attempt_id,
                    regression_summary={"regression_detected": True},
                    error_message=vresult.diagnostic,
                    payload=stage_res.model_dump(mode="json"),
                )
            else:
                return self.store.transition_engineering_workflow(
                    workflow_id=wf.workflow_id,
                    expected_version=wf.version,
                    target_state=EngineeringWorkflowState.VERIFICATION_FAILED,
                    stage="verification_failed",
                    verification_attempt_id=vresult.attempt_id,
                    error_message=vresult.diagnostic,
                    payload=stage_res.model_dump(mode="json"),
                )

        else:
            # TIMED_OUT, BASELINE_FAILED, INCONCLUSIVE
            return self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.VERIFICATION_FAILED,
                stage=f"verification_{vresult.aggregate_outcome.value}",
                verification_attempt_id=vresult.attempt_id,
                error_message=vresult.diagnostic,
                payload=stage_res.model_dump(mode="json"),
            )

    def advance_workflow(
        self,
        workflow_id: str,
        workspace_path: Optional[Path] = None,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        scratch_workspace_path: Optional[Path] = None,
        harness: Optional[Any] = None,
        planner: Optional[Any] = None,
        verifier: Optional[Any] = None,
        auto_verify: bool = False,
    ) -> EngineeringWorkflow:
        """Progress an engineering workflow through sequential stages up to repair planning (or human review if auto_verify=True)."""
        wf = self.store.get_engineering_workflow(workflow_id)
        if not wf:
            raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

        # 1. CREATED -> INTAKE_VALIDATED
        if wf.state == EngineeringWorkflowState.CREATED:
            intake = self.store.get_workflow_intake(wf.intake_id)
            wf = self.store.transition_engineering_workflow(
                workflow_id=wf.workflow_id,
                expected_version=wf.version,
                target_state=EngineeringWorkflowState.INTAKE_VALIDATED,
                stage="intake_validation",
                payload={"title": intake.title, "source_type": intake.source_type.value} if intake else {},
            )

        # 2. INTAKE_VALIDATED -> REPO_ANALYZED
        if wf.state == EngineeringWorkflowState.INTAKE_VALIDATED:
            intake = self.store.get_workflow_intake(wf.intake_id)
            repo = workspace_path or Path(wf.target_repo)
            if repo.exists() and repo.is_dir() and intake:
                context = assemble_repository_context(repo, intake)
                wf = self.store.transition_engineering_workflow(
                    workflow_id=wf.workflow_id,
                    expected_version=wf.version,
                    target_state=EngineeringWorkflowState.REPO_ANALYZED,
                    stage="context_assembly",
                    context_id=context.context_id,
                    payload={
                        "token_estimate": context.budget_summary.estimated_tokens,
                        "files_included": len(context.source_excerpts),
                        "truncated": context.budget_summary.is_truncated,
                    },
                )
            else:
                return wf

        # 3. REPO_ANALYZED / REPRODUCTION_PENDING -> reproduce_defect
        if wf.state in (EngineeringWorkflowState.REPO_ANALYZED, EngineeringWorkflowState.REPRODUCTION_PENDING):
            wf = self.reproduce_defect(
                workflow_id=wf.workflow_id,
                workspace_path=workspace_path,
                harness=harness,
            )

        # 4. REPRODUCED or interrupted REPAIR_PENDING without repair_plan_id -> plan_repair
        if wf.state == EngineeringWorkflowState.REPRODUCED or (
            wf.state == EngineeringWorkflowState.REPAIR_PENDING and not wf.repair_plan_id
        ):
            wf = self.plan_repair(
                workflow_id=wf.workflow_id,
                workspace_path=workspace_path,
                provider_name=provider_name,
                model_name=model_name,
                scratch_workspace_path=scratch_workspace_path,
                planner=planner,
            )

        # 5. REPAIR_PENDING -> verify_repair (if auto_verify is True)
        if auto_verify and wf.state == EngineeringWorkflowState.REPAIR_PENDING and wf.repair_plan_id:
            wf = self.verify_repair(
                workflow_id=wf.workflow_id,
                workspace_path=workspace_path,
                scratch_workspace_path=scratch_workspace_path,
                verifier=verifier,
            )

        return wf

    def export_dossier(
        self,
        workflow_id: str,
        output_dir: Union[str, Path],
        target_repo_path: Optional[Union[str, Path]] = None,
        signing_key: Optional[Union[str, bytes]] = None,
        key_id: Optional[str] = None,
        force: bool = False,
    ) -> Any:
        """Export an approved engineering workflow into an integrity-verified engineering dossier."""
        from forge.workflow.dossier import export_engineering_dossier
        return export_engineering_dossier(
            workflow_id=workflow_id,
            output_dir=output_dir,
            store=self.store,
            target_repo_path=target_repo_path,
            signing_key=signing_key,
            key_id=key_id,
            force=force,
        )
