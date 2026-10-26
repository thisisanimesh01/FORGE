"""Controlled, resumable repair orchestration engine for FORGE Phase 4 Milestone 4.

Guarantees:
- Deterministic typed workflow state machine:
  CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED.
- Mandatory Human Approval Gate: verified patches pause at AWAITING_HUMAN_REVIEW and are NEVER automatically approved.
- Host & Target Repo Immutability: target repository is strictly read-only. FORGE never mutates target files, creates git commits, pushes branches, opens PRs, merges, or deploys.
- Fail-Closed Sandboxing: missing container isolation transitions to BLOCKED without running untrusted code on the host.
- Dry-Run Support: plan-only mode validates patches and transitions directly to AWAITING_HUMAN_REVIEW without executing repository code.
- Anti-Tampering & Stale Revision Defenses: verifies patch SHA256 digest and checks git commit HEAD before each stage.
- Append-Only Transactional Audit Trail: emits sequential events with redacted secrets.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional
import uuid

from forge.config import get_settings, redact_secrets
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairOutcome,
    RepairSafetyError,
    ScopeContainmentError,
    StaleBaseRevisionError,
    compute_patch_sha256,
    validate_base_sha,
)
from forge.repair.models import (
    ProtectedPathPolicy,
    RepairPlan,
    RepairPlanStatus,
    RepairVerificationResult,
)
from forge.repair.orchestrator_models import (
    InvalidWorkflowTransitionError,
    RepairWorkflowEvent,
    RepairWorkflowRecord,
    RepairWorkflowState,
    WorkflowConcurrencyError,
    validate_workflow_transition,
)
from forge.repair.planner import RepairPlanner, _extract_git_head_commit
from forge.repair.verifier import RepairVerifier
from forge.sandbox.contracts import ReproductionOutcome
from forge.sandbox.docker import DockerSandbox
from forge.schemas import EventType, FindingLifecycleState
from forge.storage.store import ForgeStore


class RepairOrchestrator:
    """Manages resumable, auditable repair orchestration from finding to human review."""

    def __init__(
        self,
        store: Optional[ForgeStore] = None,
        planner: Optional[RepairPlanner] = None,
        verifier: Optional[RepairVerifier] = None,
        sandbox: Optional[Any] = None,
        budget: Optional[RepairBudget] = None,
    ):
        settings = get_settings()
        self.store = store or ForgeStore(db_path=settings.db_path)
        self.sandbox = sandbox or DockerSandbox()
        self.planner = planner or RepairPlanner()
        self.verifier = verifier or RepairVerifier(sandbox=self.sandbox)
        self.budget = budget or RepairBudget()

    # -------------------------------------------------------------------------
    # 1. Workflow Initialization
    # -------------------------------------------------------------------------

    def start_workflow(
        self,
        finding_id: str,
        target_repo: Path | str,
        base_commit_sha: Optional[str] = None,
        dry_run: bool = False,
        run_id: Optional[str] = None,
        policy_version: str = "v1",
        force: bool = False,
    ) -> RepairWorkflowRecord:
        """Initialize and persist a new repair workflow run pinned to finding and base SHA."""
        target_path = Path(target_repo).resolve()
        if not target_path.exists() or not target_path.is_dir():
            raise FileNotFoundError(f"Target repository path '{target_repo}' does not exist or is not a directory")

        # Resolve base revision
        resolved_sha = base_commit_sha or _extract_git_head_commit(target_path) or "0000000000000000000000000000000000000000"
        validate_base_sha(resolved_sha, resolved_sha)

        # Idempotency / active workflow check
        existing = self.store.list_repair_workflows(finding_id=finding_id)
        active = [w for w in existing if not w.is_terminal]
        if active and not force:
            # Return active workflow if parameters match
            matched = [
                w for w in active
                if Path(w.target_repo).resolve() == target_path and w.base_commit_sha == resolved_sha
            ]
            if matched:
                return matched[0]
            raise WorkflowConcurrencyError(
                f"An active repair workflow ('{active[0].workflow_id}') already exists for finding '{finding_id}' in state '{active[0].state.value}'"
            )

        workflow_id = f"rwf_{finding_id[:16]}_{resolved_sha[:8]}_{uuid.uuid4().hex[:6]}"
        record = RepairWorkflowRecord(
            workflow_id=workflow_id,
            finding_id=finding_id,
            run_id=run_id,
            target_repo=str(target_path),
            base_commit_sha=resolved_sha,
            policy_version=policy_version,
            state=RepairWorkflowState.CREATED,
            dry_run=dry_run,
            sandbox_available=self.sandbox.is_available() if hasattr(self.sandbox, "is_available") else True,
            version=1,
        )

        self.store.save_repair_workflow(record)
        return record

    # -------------------------------------------------------------------------
    # 2. Stage Advancer
    # -------------------------------------------------------------------------

    def advance(self, workflow_id: str, auto_advance: bool = True) -> RepairWorkflowRecord:
        """Step or advance a repair workflow through its lifecycle until paused or complete."""
        while True:
            workflow = self.store.get_repair_workflow(workflow_id)
            if not workflow:
                raise KeyError(f"Repair workflow '{workflow_id}' not found")

            # Pause conditions: terminal states or paused for human review
            if workflow.is_terminal or workflow.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW:
                return workflow

            # Step based on current state
            if workflow.state == RepairWorkflowState.CREATED:
                workflow = self._step_validate_evidence(workflow)
            elif workflow.state == RepairWorkflowState.EVIDENCE_VALIDATED:
                workflow = self._step_generate_plan(workflow)
            elif workflow.state == RepairWorkflowState.PLAN_GENERATED:
                workflow = self._step_validate_patch(workflow)
            elif workflow.state == RepairWorkflowState.PATCH_VALIDATED:
                workflow = self._step_dispatch_verification(workflow)
            elif workflow.state == RepairWorkflowState.VERIFICATION_PENDING:
                workflow = self._step_execute_verification(workflow)
            elif workflow.state == RepairWorkflowState.VERIFIED:
                workflow = self._step_await_review(workflow)
            elif workflow.state in (RepairWorkflowState.FAILED, RepairWorkflowState.BLOCKED):
                # Cannot auto-advance from failed or blocked without explicit resume/retry
                return workflow
            else:
                return workflow

            if not auto_advance:
                return workflow

    # -------------------------------------------------------------------------
    # 3. Individual Stage Steps
    # -------------------------------------------------------------------------

    def _step_validate_evidence(self, workflow: RepairWorkflowRecord) -> RepairWorkflowRecord:
        """Stage: validate evidence backing finding and check base commit revision."""
        target_path = Path(workflow.target_repo).resolve()

        # Check repository exists
        if not target_path.exists():
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.CREATED,
                target_state=RepairWorkflowState.BLOCKED,
                stage="validate_evidence",
                error_message=f"Target repository path '{target_path}' does not exist",
            )

        # Check git HEAD matches pinned base commit SHA
        head_sha = _extract_git_head_commit(target_path)
        if head_sha and head_sha != workflow.base_commit_sha:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.CREATED,
                target_state=RepairWorkflowState.BLOCKED,
                stage="validate_evidence",
                error_message=f"Repository base revision mismatch: repo HEAD is '{head_sha}', but workflow is pinned to '{workflow.base_commit_sha}'",
            )

        # Revalidate finding in store
        finding = self.store.get_finding(workflow.finding_id)
        if not finding:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.CREATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_evidence",
                error_message=f"Finding '{workflow.finding_id}' not found in store",
            )

        evidence_list = self.store.list_evidence_for_finding(workflow.finding_id)

        def _is_valid_repro_evidence(ev: Any) -> bool:
            if ev.evidence_type.value != "reproduction_result":
                return False
            status = str(ev.details.get("status") or ev.details.get("outcome") or "").lower()
            if status in ("failed", "failing", "error", "unresolved", "not_reproduced", "timeout", "flaky"):
                return False
            return True

        has_repro = any(_is_valid_repro_evidence(e) for e in evidence_list)
        is_reproduced = finding.lifecycle_state in (
            FindingLifecycleState.REPRODUCED,
            FindingLifecycleState.ROOT_CAUSED,
            FindingLifecycleState.FIXED,
            FindingLifecycleState.VERIFIED,
        )

        if not has_repro and not is_reproduced:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.CREATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_evidence",
                error_message="Planning refused: finding lacks verified reproduction evidence (fail-closed gate)",
            )

        return self.store.transition_repair_workflow(
            workflow_id=workflow.workflow_id,
            expected_from_state=RepairWorkflowState.CREATED,
            target_state=RepairWorkflowState.EVIDENCE_VALIDATED,
            stage="validate_evidence",
            payload={
                "finding_id": finding.finding_id,
                "evidence_count": len(evidence_list),
                "lifecycle_state": finding.lifecycle_state.value,
            },
        )

    def _step_generate_plan(self, workflow: RepairWorkflowRecord) -> RepairWorkflowRecord:
        """Stage: generate or load evidence-grounded repair plan."""
        target_path = Path(workflow.target_repo).resolve()

        # Check stale base revision
        head_sha = _extract_git_head_commit(target_path)
        if head_sha and head_sha != workflow.base_commit_sha:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
                target_state=RepairWorkflowState.BLOCKED,
                stage="generate_plan",
                error_message=f"Stale base revision: expected '{workflow.base_commit_sha}', observed '{head_sha}'",
            )

        # Check if plan already exists or generate fresh
        plan: Optional[RepairPlan] = None
        if workflow.plan_id:
            plan = self.store.get_repair_plan(workflow.plan_id)

        if not plan:
            try:
                plan = self.planner.plan_repair(
                    finding_id=workflow.finding_id,
                    target_repo=target_path,
                    base_commit_sha=workflow.base_commit_sha,
                    store=self.store,
                    run_id=workflow.run_id,
                )
            except Exception as e:
                return self.store.transition_repair_workflow(
                    workflow_id=workflow.workflow_id,
                    expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
                    target_state=RepairWorkflowState.FAILED,
                    stage="generate_plan",
                    error_message=f"Plan generation failed with exception: {e}",
                )

        if not plan:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
                target_state=RepairWorkflowState.FAILED,
                stage="generate_plan",
                error_message="Repair plan generation returned null",
            )

        if plan.status == RepairPlanStatus.BLOCKED:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
                target_state=RepairWorkflowState.BLOCKED,
                stage="generate_plan",
                plan_id=plan.plan_id,
                error_message=plan.root_cause or "Plan generation was blocked by safety policy",
            )

        if plan.status == RepairPlanStatus.REJECTED or not plan.patch_candidate:
            reasons = plan.validation.violations if plan.validation else ["Plan rejected by safety validator"]
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="generate_plan",
                plan_id=plan.plan_id,
                error_message="; ".join(reasons),
            )

        # Pin plan and patch digest
        patch_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
        return self.store.transition_repair_workflow(
            workflow_id=workflow.workflow_id,
            expected_from_state=RepairWorkflowState.EVIDENCE_VALIDATED,
            target_state=RepairWorkflowState.PLAN_GENERATED,
            stage="generate_plan",
            plan_id=plan.plan_id,
            patch_digest=patch_digest,
            payload={
                "plan_id": plan.plan_id,
                "target_file": plan.target_file,
                "hunk_count": plan.patch_candidate.hunk_count,
                "added_lines": plan.patch_candidate.added_lines,
                "removed_lines": plan.patch_candidate.removed_lines,
                "patch_digest": patch_digest,
            },
        )

    def _step_validate_patch(self, workflow: RepairWorkflowRecord) -> RepairWorkflowRecord:
        """Stage: revalidate patch candidate bounds, protected paths, and SHA256 integrity."""
        if not workflow.plan_id:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message="Workflow missing plan_id",
            )

        plan = self.store.get_repair_plan(workflow.plan_id)
        if not plan or not plan.patch_candidate:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message="Plan missing patch candidate",
            )

        # Anti-tampering check: compare unified_diff against pinned digest
        actual_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
        if actual_digest != workflow.patch_digest:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message=f"Patch integrity mismatch: diff digest '{actual_digest}' does not match pinned digest '{workflow.patch_digest}'",
            )

        # Protected path policy revalidation
        is_prot, prot_reason = ProtectedPathPolicy.is_protected(plan.target_file)
        if is_prot:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message=f"Target file '{plan.target_file}' is protected by policy: {prot_reason}",
            )

        # Revalidate patch candidate bounds against budget
        cand = plan.patch_candidate
        total_lines = cand.added_lines + cand.removed_lines
        if total_lines > self.budget.max_lines_changed:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message=f"Patch exceeds lines budget: {total_lines} > {self.budget.max_lines_changed}",
            )

        if cand.hunk_count > self.budget.max_hunks:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message=f"Patch exceeds hunk count budget: {cand.hunk_count} > {self.budget.max_hunks}",
            )

        diff_bytes = len(cand.unified_diff.encode("utf-8"))
        if diff_bytes > self.budget.max_patch_bytes:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message=f"Patch exceeds diff size budget: {diff_bytes} > {self.budget.max_patch_bytes} bytes",
            )

        if len(cand.target_files) > self.budget.max_files_modified:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                target_state=RepairWorkflowState.REJECTED,
                stage="validate_patch",
                error_message=f"Patch modifies {len(cand.target_files)} files, exceeding limit of {self.budget.max_files_modified}",
            )

        # Path traversal check on target files
        for tf in cand.target_files:
            norm = Path(tf).as_posix().strip()
            if ".." in norm.split("/") or norm.startswith("/") or norm.startswith("\\"):
                return self.store.transition_repair_workflow(
                    workflow_id=workflow.workflow_id,
                    expected_from_state=RepairWorkflowState.PLAN_GENERATED,
                    target_state=RepairWorkflowState.REJECTED,
                    stage="validate_patch",
                    error_message=f"Path traversal forbidden in target files: '{tf}'",
                )

        return self.store.transition_repair_workflow(
            workflow_id=workflow.workflow_id,
            expected_from_state=RepairWorkflowState.PLAN_GENERATED,
            target_state=RepairWorkflowState.PATCH_VALIDATED,
            stage="validate_patch",
            payload={
                "patch_id": plan.patch_candidate.patch_id,
                "target_file": plan.target_file,
                "patch_digest": actual_digest,
                "lines_changed": plan.patch_candidate.added_lines + plan.patch_candidate.removed_lines,
            },
        )

    def _step_dispatch_verification(self, workflow: RepairWorkflowRecord) -> RepairWorkflowRecord:
        """Stage: dispatch either to sandbox verification pending or direct human review in dry-run mode."""
        if workflow.dry_run:
            # Dry-run mode: skip execution entirely, pause at AWAITING_HUMAN_REVIEW
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.PATCH_VALIDATED,
                target_state=RepairWorkflowState.AWAITING_HUMAN_REVIEW,
                stage="dry_run_dispatch",
                payload={
                    "dry_run": True,
                    "message": "Dry-run mode: sandboxed test execution skipped. Paused awaiting human review.",
                },
            )

        return self.store.transition_repair_workflow(
            workflow_id=workflow.workflow_id,
            expected_from_state=RepairWorkflowState.PATCH_VALIDATED,
            target_state=RepairWorkflowState.VERIFICATION_PENDING,
            stage="prepare_verification",
            payload={"message": "Patch validated; queued for container sandbox verification"},
        )

    def _step_execute_verification(self, workflow: RepairWorkflowRecord) -> RepairWorkflowRecord:
        """Stage: execute sandboxed verification inside isolated container."""
        target_path = Path(workflow.target_repo).resolve()

        # Re-check git HEAD before execution
        head_sha = _extract_git_head_commit(target_path)
        if head_sha and head_sha != workflow.base_commit_sha:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.BLOCKED,
                stage="execute_verification",
                error_message=f"Base revision changed before verification: expected '{workflow.base_commit_sha}', observed '{head_sha}'",
            )

        # 1. Fail-closed Sandbox Availability Gate
        sandbox_avail = self.sandbox.is_available() if hasattr(self.sandbox, "is_available") else True
        if not sandbox_avail:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.BLOCKED,
                stage="execute_verification",
                outcome=RepairOutcome.SANDBOX_UNAVAILABLE,
                sandbox_available=False,
                error_message="Container sandbox unavailable (Docker not found or inactive). Host execution refused.",
            )

        plan = self.store.get_repair_plan(workflow.plan_id)
        if not plan:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.FAILED,
                stage="execute_verification",
                error_message="Plan missing for verification",
            )

        # Anti-tampering verification
        current_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
        if current_digest != workflow.patch_digest:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.REJECTED,
                stage="execute_verification",
                error_message="Patch tampering detected immediately prior to verification",
            )

        # Execute sandboxed verification
        try:
            result: RepairVerificationResult = self.verifier.verify_plan(
                plan=plan,
                target_repo=target_path,
                store=self.store,
            )
        except Exception as exc:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.FAILED,
                stage="execute_verification",
                outcome=RepairOutcome.REPAIR_FAILED,
                error_message=f"Verification execution crashed with exception: {exc}",
            )

        # Map outcome to state transition
        if result.outcome == RepairOutcome.REPAIR_VERIFIED:
            # Transition to VERIFIED
            w_verified = self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.VERIFIED,
                stage="execute_verification",
                outcome=result.outcome,
                verification_attempt_id=result.attempt_id,
                sandbox_available=result.sandbox_available,
                payload={
                    "outcome": result.outcome.value,
                    "diagnostic": result.diagnostic,
                    "patch_applied": result.patch_applied,
                    "syntax_valid": result.syntax_valid,
                },
            )
            # Immediately advance to AWAITING_HUMAN_REVIEW (never auto-approve)
            return self.store.transition_repair_workflow(
                workflow_id=w_verified.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFIED,
                target_state=RepairWorkflowState.AWAITING_HUMAN_REVIEW,
                stage="require_review",
                payload={"message": "Patch verified successfully; paused awaiting explicit human review"},
            )

        elif result.outcome in (
            RepairOutcome.REPAIR_FAILED,
            RepairOutcome.BROKEN_BUILD,
            RepairOutcome.REGRESSION_DETECTED,
            RepairOutcome.TIMEOUT,
            RepairOutcome.INCONCLUSIVE,
            RepairOutcome.FLAKY,
        ):
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.FAILED,
                stage="execute_verification",
                outcome=result.outcome,
                verification_attempt_id=result.attempt_id,
                sandbox_available=result.sandbox_available,
                error_message=result.diagnostic,
            )

        elif result.outcome == RepairOutcome.SANDBOX_UNAVAILABLE:
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.BLOCKED,
                stage="execute_verification",
                outcome=result.outcome,
                verification_attempt_id=result.attempt_id,
                sandbox_available=False,
                error_message=result.diagnostic,
            )

        else:  # REPAIR_REJECTED or unexpected
            return self.store.transition_repair_workflow(
                workflow_id=workflow.workflow_id,
                expected_from_state=RepairWorkflowState.VERIFICATION_PENDING,
                target_state=RepairWorkflowState.REJECTED,
                stage="execute_verification",
                outcome=result.outcome,
                verification_attempt_id=result.attempt_id,
                sandbox_available=result.sandbox_available,
                error_message=result.diagnostic,
            )

    def _step_await_review(self, workflow: RepairWorkflowRecord) -> RepairWorkflowRecord:
        """Stage: transition verified patch into awaiting human review."""
        return self.store.transition_repair_workflow(
            workflow_id=workflow.workflow_id,
            expected_from_state=RepairWorkflowState.VERIFIED,
            target_state=RepairWorkflowState.AWAITING_HUMAN_REVIEW,
            stage="require_review",
            payload={"message": "Paused awaiting explicit human review decision"},
        )

    # -------------------------------------------------------------------------
    # 4. Resume Workflow
    # -------------------------------------------------------------------------

    def resume_workflow(self, workflow_id: str, auto_advance: bool = True) -> RepairWorkflowRecord:
        """Resume an interrupted, blocked, or failed repair workflow if eligible."""
        workflow = self.store.get_repair_workflow(workflow_id)
        if not workflow:
            raise KeyError(f"Repair workflow '{workflow_id}' not found")

        if workflow.is_terminal:
            raise RepairSafetyError(f"Workflow '{workflow_id}' is in terminal state '{workflow.state.value}'; cannot resume")

        if workflow.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW:
            return workflow

        target_path = Path(workflow.target_repo).resolve()
        head_sha = _extract_git_head_commit(target_path)

        # Handle BLOCKED state resume
        if workflow.state == RepairWorkflowState.BLOCKED:
            # Check if base commit SHA was the blocker and is now fixed
            if head_sha and head_sha == workflow.base_commit_sha:
                # Check if sandbox was the blocker
                sandbox_avail = self.sandbox.is_available() if hasattr(self.sandbox, "is_available") else True
                if workflow.outcome == RepairOutcome.SANDBOX_UNAVAILABLE and not sandbox_avail:
                    raise RepairSafetyError("Cannot resume: container sandbox is still unavailable")

                # Transition out of blocked
                if workflow.patch_digest:
                    next_state = RepairWorkflowState.VERIFICATION_PENDING
                elif workflow.plan_id:
                    next_state = RepairWorkflowState.EVIDENCE_VALIDATED
                else:
                    next_state = RepairWorkflowState.CREATED

                workflow = self.store.transition_repair_workflow(
                    workflow_id=workflow_id,
                    expected_from_state=RepairWorkflowState.BLOCKED,
                    target_state=next_state,
                    stage="resume",
                    payload={"message": f"Resumed from BLOCKED into {next_state.value}"},
                )
            else:
                raise StaleBaseRevisionError(
                    f"Cannot resume: repository HEAD '{head_sha}' still differs from pinned base SHA '{workflow.base_commit_sha}'"
                )

        # Handle FAILED state retry
        elif workflow.state == RepairWorkflowState.FAILED:
            if head_sha and head_sha != workflow.base_commit_sha:
                raise StaleBaseRevisionError(
                    f"Cannot retry: repository HEAD '{head_sha}' differs from pinned base SHA '{workflow.base_commit_sha}'"
                )
            next_state = RepairWorkflowState.VERIFICATION_PENDING if workflow.patch_digest else RepairWorkflowState.EVIDENCE_VALIDATED
            workflow = self.store.transition_repair_workflow(
                workflow_id=workflow_id,
                expected_from_state=RepairWorkflowState.FAILED,
                target_state=next_state,
                stage="retry_workflow",
                payload={"message": f"Retrying workflow from {next_state.value}"},
            )

        else:
            # Interrupted active workflow
            if head_sha and head_sha != workflow.base_commit_sha:
                raise StaleBaseRevisionError(
                    f"Cannot advance interrupted workflow: repository HEAD '{head_sha}' differs from pinned base SHA '{workflow.base_commit_sha}'"
                )

        if auto_advance:
            return self.advance(workflow_id)
        return workflow

    # -------------------------------------------------------------------------
    # 5. Human Review Boundary Gate
    # -------------------------------------------------------------------------

    def review_workflow(
        self,
        workflow_id: str,
        decision: str,
        notes: Optional[str] = None,
    ) -> RepairWorkflowRecord:
        """Process explicit human approval or rejection of a verified repair workflow.
        
        Enforces:
        - Workflow must be in AWAITING_HUMAN_REVIEW state.
        - Decision must be 'approve' or 'reject'.
        - Even when approved, FORGE strictly asserts that NO target repository mutation,
          git commit, branch push, pull request, merge, or deployment occurs.
        """
        workflow = self.store.get_repair_workflow(workflow_id)
        if not workflow:
            raise KeyError(f"Repair workflow '{workflow_id}' not found")

        if workflow.state != RepairWorkflowState.AWAITING_HUMAN_REVIEW:
            raise InvalidWorkflowTransitionError(
                f"Cannot review workflow in state '{workflow.state.value}'. Only workflows in 'awaiting_human_review' can be reviewed."
            )

        # Re-check base commit revision and patch tampering
        target_path = Path(workflow.target_repo).resolve()
        head_sha = _extract_git_head_commit(target_path)
        if head_sha and head_sha != workflow.base_commit_sha:
            raise StaleBaseRevisionError(
                f"Base revision changed during review: repository HEAD is '{head_sha}', pinned base is '{workflow.base_commit_sha}'"
            )

        if workflow.plan_id and workflow.patch_digest:
            plan = self.store.get_repair_plan(workflow.plan_id)
            if plan and plan.patch_candidate:
                curr_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
                if curr_digest != workflow.patch_digest:
                    raise RepairSafetyError("Patch tampering detected during human review")

        norm_dec = decision.strip().lower()
        clean_notes = redact_secrets(notes.strip()) if notes else ""

        if norm_dec in ("approve", "approved"):
            target_state = RepairWorkflowState.APPROVED
            decision_val = "approved"
        elif norm_dec in ("reject", "rejected"):
            target_state = RepairWorkflowState.REJECTED
            decision_val = "rejected"
        else:
            raise ValueError(f"Invalid review decision '{decision}'. Must be 'approve' or 'reject'.")

        updated = self.store.transition_repair_workflow(
            workflow_id=workflow_id,
            expected_from_state=RepairWorkflowState.AWAITING_HUMAN_REVIEW,
            target_state=target_state,
            stage="human_review",
            human_decision=decision_val,
            human_notes=clean_notes,
            payload={
                "decision": decision_val,
                "notes": clean_notes,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Audit event mirroring for human approval if run_id is linked
        if workflow.run_id:
            try:
                run_obj = self.store.get_run(workflow.run_id)
                if run_obj:
                    event_type = (
                        EventType.REPAIR_APPROVAL_GRANTED
                        if target_state == RepairWorkflowState.APPROVED
                        else EventType.REPAIR_APPROVAL_REJECTED
                    )
                    self.store.append_event(
                        run_id=workflow.run_id,
                        event_type=event_type,
                        payload={
                            "workflow_id": workflow_id,
                            "plan_id": workflow.plan_id,
                            "finding_id": workflow.finding_id,
                            "notes": clean_notes,
                        },
                    )
            except Exception:
                pass

        # STRICT SAFETY INVARIANT: Host & Target Repository Immutability
        # Never commit, push, create a PR, merge, or modify target repo files.
        return updated
