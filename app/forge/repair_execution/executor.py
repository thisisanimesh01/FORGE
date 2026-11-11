"""Phase 12 M4: Controlled Repair Execution Engine.

Orchestrates the end-to-end controlled repair lifecycle:
1. Strict authorization & human approval verification.
2. Isolated workspace allocation & baseline integrity capture.
3. Patch generation & boundary validation.
4. Pure in-memory patch application to isolated workspace (zero shell/subprocess).
5. Post-application workspace integrity verification.
6. Automatic rollback on failure or scope violation.
7. Immutable audit event recording with secret redaction.
8. Clean handoff metadata with verification_pending=True for Phase 12 M5.
"""

from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Dict, List, Optional, Set
import uuid

from forge.repair_planning.schemas import RepairPlan
from forge.repair_execution.approval import RepairApprovalValidator, compute_plan_digest
from forge.repair_execution.events import RepairEventRecorder
from forge.repair_execution.integrity import WorkspaceIntegrityChecker
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.patch_applier import ControlledPatchApplier
from forge.repair_execution.patch_generator import RepairPatchGenerator
from forge.repair_execution.patch_validator import RepairPatchValidator
from forge.repair_execution.rollback import WorkspaceRollbackManager
from forge.repair_execution.schemas import (
    ApprovalStatus,
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    PatchValidationStatus,
    RepairApproval,
    RepairEvent,
    RepairExecutionResult,
    RepairPatch,
    RepairWorkspace,
    RepairEventType,
    RollbackStatus,
    WorkspaceStatus,
)
from forge.repair_execution.workspace import RepairWorkspaceManager


class ControlledRepairExecutor:
    """Orchestrates controlled repair execution with strict isolation and safety invariants."""

    def __init__(
        self,
        repo_path: Optional[Path] = None,
        workspace_manager: Optional[RepairWorkspaceManager] = None,
        event_recorder: Optional[RepairEventRecorder] = None,
        db_path: Optional[Path] = None,
        trusted_approver_identities: Optional[Set[str]] = None,
        approval_ledger: Optional[ApprovalLedger] = None,
    ) -> None:
        self.repo_path = repo_path.resolve() if repo_path else None
        if workspace_manager:
            self.workspace_manager = workspace_manager
        elif self.repo_path:
            self.workspace_manager = RepairWorkspaceManager(original_repo_path=self.repo_path)
        else:
            self.workspace_manager = None

        self.event_recorder = event_recorder or RepairEventRecorder()
        self.approval_validator = RepairApprovalValidator()
        self.patch_generator = RepairPatchGenerator()
        self.patch_validator = RepairPatchValidator()
        self.patch_applier = ControlledPatchApplier()
        self.rollback_manager = WorkspaceRollbackManager()
        self.integrity_checker = WorkspaceIntegrityChecker()

        self.ledger = approval_ledger or ApprovalLedger(db_path=db_path)
        self.trusted_approver_identities = trusted_approver_identities
        self._lock = threading.Lock()
        self._executions: Dict[str, RepairExecutionResult] = {}

    @property
    def _consumed_approvals(self) -> Dict[str, str]:
        return self.ledger.get_consumed_map()

    @property
    def _revoked_approvals(self) -> Dict[str, str]:
        return self.ledger.get_revoked_map()

    def revoke_approval(self, approval_id: str, reason: str = "revoked by human operator") -> bool:
        """Revoke a human approval, preventing any future execution."""
        return self.ledger.record_revoked(approval_id, reason)

    def is_approval_consumed(self, approval_id: str) -> bool:
        """Check if an approval has already been used in an execution."""
        return self.ledger.is_consumed(approval_id)

    def _format_limitations(
        self,
        plan_limitations: Optional[str],
        extra: Optional[List[str]] = None,
    ) -> List[str]:
        """Convert plan string limitations and extra failure notes into a List[str]."""
        res: List[str] = [plan_limitations] if plan_limitations else []
        if extra:
            res.extend(extra)
        return res

    def _check_live_evidence_freshness(self, plan: RepairPlan) -> List[str]:
        """Re-verify supporting evidence source hashes against current repository disk state."""
        if not self.repo_path or not self.repo_path.exists() or not plan.supporting_evidence:
            return []
        from forge.repair_planning.evidence import RepairEvidenceCollector

        collector = RepairEvidenceCollector(workspace_path=self.repo_path)
        stale_errors: List[str] = []
        for ev in plan.supporting_evidence:
            if not ev.file_path or not ev.source_hash:
                continue
            is_valid, current_hash, err = collector.verify_file_location(
                file_path=ev.file_path,
                start_line=ev.start_line,
                end_line=ev.end_line,
            )
            if not is_valid:
                stale_errors.append(
                    f"Stale evidence '{ev.evidence_id}': file '{ev.file_path}' failed live verification ({err})."
                )
            elif current_hash != ev.source_hash:
                stale_errors.append(
                    f"Stale evidence '{ev.evidence_id}': '{ev.file_path}' modified on disk since plan synthesis."
                )
        return stale_errors

    def execute_repair(
        self,
        plan: RepairPlan,
        approval: RepairApproval,
        workspace: Optional[RepairWorkspace] = None,
        custom_patch: Optional[RepairPatch] = None,
        limits: Optional[ExecutionLimits] = None,
    ) -> RepairExecutionResult:
        """Execute an approved repair plan inside an isolated workspace.

        Args:
            plan: The RepairPlan to apply.
            approval: Human approval contract authorizing execution.
            workspace: Pre-allocated isolated workspace (or allocated automatically).
            custom_patch: Optional pre-generated RepairPatch.
            limits: Execution resource limits.

        Returns:
            RepairExecutionResult capturing complete outcome and audit events.
        """
        limits = limits or ExecutionLimits()
        execution_id = f"rex_{uuid.uuid4().hex[:12]}"

        # -------------------------------------------------------------------
        # Step 1: Authorization & Approval Verification
        # -------------------------------------------------------------------
        with self._lock:
            revoked_map = self.ledger.get_revoked_map()
            consumed_map = self.ledger.get_consumed_map()
            approval_check = self.approval_validator.validate_approval(
                approval=approval,
                plan=plan,
                revoked_approvals=set(revoked_map.keys()),
                consumed_approvals=consumed_map,
                trusted_approver_identities=self.trusted_approver_identities,
            )
            live_stale_errors = self._check_live_evidence_freshness(plan)
            all_auth_errors = list(approval_check.errors) + live_stale_errors
            if not approval_check.is_valid or live_stale_errors:
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.APPROVAL_REJECTED,
                    action_description=f"Approval rejected: {', '.join(all_auth_errors)}",
                    outcome="failure",
                )

                failure_class = FailureClassification.UNAUTHORIZED_EXECUTION
                if approval.approval_status == ApprovalStatus.EXPIRED or approval.is_expired():
                    failure_class = FailureClassification.APPROVAL_EXPIRED
                elif approval.approval_status == ApprovalStatus.REVOKED or any("has been revoked" in err for err in all_auth_errors):
                    failure_class = FailureClassification.APPROVAL_REVOKED
                elif any("outside approved" in err for err in all_auth_errors):
                    failure_class = FailureClassification.SCOPE_BREACH
                elif any("already been consumed" in err for err in all_auth_errors):
                    failure_class = FailureClassification.UNAUTHORIZED_EXECUTION

                result = RepairExecutionResult(
                    execution_id=execution_id,
                    repair_plan_id=plan.plan_id,
                    execution_status=ExecutionStatus.BLOCKED,
                    workspace_id=workspace.workspace_id if workspace else "none",
                    diff_summary=f"Execution blocked: Authorization failed: {'; '.join(all_auth_errors)}",
                    execution_events=self.event_recorder.get_events(execution_id),
                    rollback_status=RollbackStatus.NOT_TRIGGERED,
                    failure_classification=failure_class,
                    limitations=self._format_limitations(plan.limitations, ["Repair execution was blocked due to failed approval or stale evidence validation."]),
                    verification_pending=True,
                    approval_reference=approval.approval_id,
                )
                self._executions[execution_id] = result
                return result

            # Atomically record consumed approval in durable ledger before any workspace mutation
            recorded = self.ledger.record_consumed(approval.approval_id, execution_id)
            if not recorded:
                result = RepairExecutionResult(
                    execution_id=execution_id,
                    repair_plan_id=plan.plan_id,
                    execution_status=ExecutionStatus.BLOCKED,
                    workspace_id=workspace.workspace_id if workspace else "none",
                    diff_summary=f"Execution blocked: Approval '{approval.approval_id}' was already consumed concurrently.",
                    execution_events=self.event_recorder.get_events(execution_id),
                    rollback_status=RollbackStatus.NOT_TRIGGERED,
                    failure_classification=FailureClassification.UNAUTHORIZED_EXECUTION,
                    limitations=self._format_limitations(plan.limitations, ["Repair execution was blocked due to concurrent approval consumption."]),
                    verification_pending=True,
                    approval_reference=approval.approval_id,
                )
                self._executions[execution_id] = result
                return result

        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.APPROVAL_VERIFIED,
            action_description=f"Human approval verified for strategy {approval.approved_strategy_id} by {approval.approver_reference}",
            integrity_digest=approval.plan_digest,
            outcome="success",
        )

        # -------------------------------------------------------------------
        # Step 2: Workspace Allocation & Isolation
        # -------------------------------------------------------------------
        ws = workspace
        if ws is None:
            if not self.workspace_manager:
                raise ValueError("No workspace provided and no workspace_manager configured.")
            ws = self.workspace_manager.allocate_workspace(
                allowed_paths=approval.approved_file_paths,
                plan_id=plan.plan_id,
            )

        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.WORKSPACE_PREPARED,
            action_description=f"Isolated workspace {ws.workspace_id} prepared at {ws.isolated_workspace_path}",
            integrity_digest=ws.baseline_tree_digest,
            outcome="success",
        )

        # -------------------------------------------------------------------
        # Step 3: Patch Generation & Boundary Validation
        # -------------------------------------------------------------------
        patch = custom_patch
        if patch is None:
            try:
                patch = self.patch_generator.generate_patch(
                    plan=plan,
                    strategy_id=approval.approved_strategy_id,
                )
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.PATCH_GENERATED,
                    action_description=f"Generated repair patch {patch.patch_id} targeting {len(patch.affected_files)} files",
                    integrity_digest=patch.patch_digest,
                    outcome="success",
                )
            except Exception as e:
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.PATCH_APPLICATION_FAILED,
                    action_description=f"Patch generation failed: {e}",
                    outcome="failure",
                )
                result = RepairExecutionResult(
                    execution_id=execution_id,
                    repair_plan_id=plan.plan_id,
                    execution_status=ExecutionStatus.FAILED,
                    workspace_id=ws.workspace_id,
                    diff_summary=f"Failed to generate repair patch: {e}",
                    execution_events=self.event_recorder.get_events(execution_id),
                    rollback_status=RollbackStatus.NOT_TRIGGERED,
                    failure_classification=FailureClassification.DIFF_SYNTAX_ERROR,
                    limitations=self._format_limitations(plan.limitations),
                    verification_pending=True,
                    approval_reference=approval.approval_id,
                )
                self._executions[execution_id] = result
                return result

        # Validate patch boundaries, scope, and syntax
        val_result = self.patch_validator.validate_patch(
            unified_diff=patch.unified_diff,
            allowed_paths=approval.approved_file_paths,
            expected_digest=patch.patch_digest,
            max_bytes=limits.max_patch_bytes,
            max_changed_lines=limits.max_changed_lines,
        )

        if not val_result.is_valid:
            self.event_recorder.record_event(
                execution_id=execution_id,
                event_type=RepairEventType.PATCH_APPLICATION_FAILED,
                action_description=f"Patch validation failed: {', '.join(val_result.errors)}",
                integrity_digest=patch.patch_digest,
                outcome="failure",
            )
            failure_class = FailureClassification.DIFF_SYNTAX_ERROR
            if val_result.status == PatchValidationStatus.SCOPE_VIOLATION:
                failure_class = FailureClassification.SCOPE_BREACH
            elif val_result.status == PatchValidationStatus.OVERSIZED:
                failure_class = FailureClassification.DIFF_SYNTAX_ERROR

            result = RepairExecutionResult(
                execution_id=execution_id,
                repair_plan_id=plan.plan_id,
                execution_status=ExecutionStatus.FAILED,
                workspace_id=ws.workspace_id,
                diff_summary=f"Patch validation rejected: {'; '.join(val_result.errors)}",
                execution_events=self.event_recorder.get_events(execution_id),
                rollback_status=RollbackStatus.NOT_TRIGGERED,
                failure_classification=failure_class,
                limitations=self._format_limitations(plan.limitations, ["Repair patch violated syntax, scope, or size limits."]),
                verification_pending=True,
                approval_reference=approval.approval_id,
            )
            self._executions[execution_id] = result
            return result

        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.PATCH_VALIDATED,
            action_description=f"Repair patch validated: {val_result.added_lines} added, {val_result.removed_lines} removed",
            integrity_digest=patch.patch_digest,
            outcome="success",
        )

        # -------------------------------------------------------------------
        # Step 4: Patch Application
        # -------------------------------------------------------------------
        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.PATCH_APPLICATION_STARTED,
            action_description=f"Applying patch {patch.patch_id} to isolated workspace",
            outcome="started",
        )

        app_outcome = self.patch_applier.apply_patch(
            workspace_path=Path(ws.isolated_workspace_path),
            patch=patch,
            allowed_files=approval.approved_file_paths,
        )

        if not app_outcome.success:
            self.event_recorder.record_event(
                execution_id=execution_id,
                event_type=RepairEventType.PATCH_APPLICATION_FAILED,
                action_description=f"Patch application failed: {', '.join(app_outcome.errors)}",
                outcome="failure",
            )

            # Trigger automatic rollback
            self.event_recorder.record_event(
                execution_id=execution_id,
                event_type=RepairEventType.ROLLBACK_STARTED,
                action_description="Starting automatic rollback due to patch application failure",
                outcome="started",
            )

            rb_status, rb_files, rb_digest = self.rollback_manager.rollback(
                workspace=ws,
                modified_files=app_outcome.modified_files,
                backups=app_outcome.backups,
            )

            if rb_status == RollbackStatus.SUCCEEDED:
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.ROLLBACK_SUCCEEDED,
                    action_description=f"Rollback succeeded, restored {len(rb_files)} files",
                    integrity_digest=rb_digest,
                    outcome="success",
                )
                final_status = ExecutionStatus.ROLLED_BACK
                fail_class = FailureClassification.PATCH_APPLICATION_FAILED
            else:
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.ROLLBACK_FAILED,
                    action_description="Rollback failed to restore baseline digest; workspace preserved for inspection",
                    integrity_digest=rb_digest,
                    outcome="failure",
                )
                final_status = ExecutionStatus.RECOVERY_REQUIRED
                fail_class = FailureClassification.ROLLBACK_FAILED

            result = RepairExecutionResult(
                execution_id=execution_id,
                repair_plan_id=plan.plan_id,
                execution_status=final_status,
                workspace_id=ws.workspace_id,
                diff_summary=f"Patch application failed: {'; '.join(app_outcome.errors)}",
                execution_events=self.event_recorder.get_events(execution_id),
                rollback_status=rb_status,
                failure_classification=fail_class,
                limitations=self._format_limitations(plan.limitations, ["Application failed during in-memory hunk substitution."]),
                verification_pending=True,
                approval_reference=approval.approval_id,
            )
            self._executions[execution_id] = result
            return result

        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.PATCH_APPLICATION_SUCCEEDED,
            action_description=f"Patch successfully applied to {len(app_outcome.modified_files)} files",
            integrity_digest=patch.patch_digest,
            outcome="success",
        )

        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.DIFF_CAPTURED,
            action_description=f"Captured diff of {len(app_outcome.diff_summary)} characters",
            outcome="success",
        )

        # -------------------------------------------------------------------
        # Step 5: Post-Application Workspace Integrity Check
        # -------------------------------------------------------------------
        integrity_report = self.integrity_checker.verify_integrity(
            workspace=ws,
            allowed_paths=approval.approved_file_paths,
        )

        if not integrity_report.is_valid:
            self.event_recorder.record_event(
                execution_id=execution_id,
                event_type=RepairEventType.INTEGRITY_VIOLATION_DETECTED,
                action_description=f"Integrity violation detected: {', '.join(integrity_report.violations)}",
                integrity_digest=integrity_report.current_digest,
                outcome="failure",
            )

            # Roll back on unexpected modifications
            self.event_recorder.record_event(
                execution_id=execution_id,
                event_type=RepairEventType.ROLLBACK_STARTED,
                action_description="Rolling back due to unexpected file modification outside approved scope",
                outcome="started",
            )

            rb_status, rb_files, rb_digest = self.rollback_manager.rollback(
                workspace=ws,
                modified_files=integrity_report.modified_files + integrity_report.unexpected_files,
                backups=app_outcome.backups,
            )

            if rb_status == RollbackStatus.SUCCEEDED:
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.ROLLBACK_SUCCEEDED,
                    action_description=f"Rollback restored workspace to baseline after integrity breach",
                    integrity_digest=rb_digest,
                    outcome="success",
                )
                final_status = ExecutionStatus.ROLLED_BACK
                fail_class = FailureClassification.UNEXPECTED_MODIFICATION
            else:
                self.event_recorder.record_event(
                    execution_id=execution_id,
                    event_type=RepairEventType.ROLLBACK_FAILED,
                    action_description="Rollback failed after integrity breach; workspace preserved",
                    integrity_digest=rb_digest,
                    outcome="failure",
                )
                final_status = ExecutionStatus.RECOVERY_REQUIRED
                fail_class = FailureClassification.ROLLBACK_FAILED

            result = RepairExecutionResult(
                execution_id=execution_id,
                repair_plan_id=plan.plan_id,
                execution_status=final_status,
                workspace_id=ws.workspace_id,
                diff_summary=f"Integrity check failed: {'; '.join(integrity_report.violations)}",
                execution_events=self.event_recorder.get_events(execution_id),
                rollback_status=rb_status,
                failure_classification=fail_class,
                limitations=self._format_limitations(plan.limitations, ["Unexpected modifications detected during post-application integrity check."]),
                verification_pending=True,
                approval_reference=approval.approval_id,
            )
            self._executions[execution_id] = result
            return result

        self.event_recorder.record_event(
            execution_id=execution_id,
            event_type=RepairEventType.INTEGRITY_VERIFIED,
            action_description=f"Workspace integrity verified; modifications confined to approved scope",
            integrity_digest=integrity_report.current_digest,
            outcome="success",
        )

        ws.workspace_status = WorkspaceStatus.DIRTY

        # -------------------------------------------------------------------
        # Step 6: Result Synthesis
        # -------------------------------------------------------------------
        result = RepairExecutionResult(
            execution_id=execution_id,
            repair_plan_id=plan.plan_id,
            execution_status=ExecutionStatus.APPLIED,
            workspace_id=ws.workspace_id,
            applied_patch_digest=patch.patch_digest,
            modified_files=app_outcome.modified_files,
            diff_summary=app_outcome.diff_summary,
            execution_events=self.event_recorder.get_events(execution_id),
            rollback_status=RollbackStatus.NOT_TRIGGERED,
            failure_classification=FailureClassification.NONE,
            limitations=self._format_limitations(plan.limitations),
            verification_pending=True,
            approval_reference=approval.approval_id,
        )

        self._executions[execution_id] = result
        return result

    def get_execution(self, execution_id: str) -> Optional[RepairExecutionResult]:
        """Retrieve execution outcome record by ID."""
        return self._executions.get(execution_id)
