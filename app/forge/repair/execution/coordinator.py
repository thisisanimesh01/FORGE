"""Phase 8 Repair Execution Architecture — Repair Execution Coordinator.

Orchestrates the full evidence-grounded repair execution lifecycle:
request validation, grounded planning, candidate patch generation,
diff validation, safe workspace application, and guarded state transitions.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

from forge.repair.execution.applier import SafePatchApplier
from forge.repair.execution.failures import (
    InsufficientEvidenceError,
    PatchAdversarialDefectError,
    PatchApplicationError,
    PatchGenerationError,
    PatchIntegrityError,
    PatchScopeViolationError,
    PatchSemanticInvariantError,
    PatchValidationError,
    RepairPolicyViolationError,
    RepairRequestValidationError,
    ScopeContainmentViolationError,
    StaleBaseRevisionError,
    WorkspaceExecutionError,
)
from forge.repair.execution.generator import RepairPatchGenerator
from forge.repair.execution.interfaces import RepairWorkspaceProtocol
from forge.repair.execution.models import (
    FailureCategory,
    FailureClassification,
    IndependentValidationResult,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairExecutionState,
    RepairRequest,
    ValidationResult,
)
from forge.repair.execution.planner import RepairExecutionPlanner
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.validator import PatchValidator, RepairRequestValidator
from forge.schemas import EvidenceRecord, Finding
from forge.storage.store import ForgeStore


class RepairExecutionCoordinator:
    """End-to-end coordinator for evidence-grounded repair execution."""

    def __init__(
        self,
        execution_store: Optional[RepairExecutionStore] = None,
        forge_store: Optional[ForgeStore] = None,
        planner: Optional[RepairExecutionPlanner] = None,
        generator: Optional[RepairPatchGenerator] = None,
        patch_validator: Optional[PatchValidator] = None,
        applier: Optional[SafePatchApplier] = None,
        db_path: Optional[Path] = None,
    ):
        self.exec_store = execution_store or RepairExecutionStore(db_path=db_path)
        self.forge_store = forge_store or ForgeStore(db_path=db_path)
        self.req_validator = RepairRequestValidator(store=self.forge_store)
        self.planner = planner or RepairExecutionPlanner()
        self.generator = generator or RepairPatchGenerator()
        self.patch_validator = patch_validator or PatchValidator()
        self.applier = applier or SafePatchApplier()

    def execute_candidate_generation(
        self,
        request: RepairRequest,
        workspace: RepairWorkspaceProtocol,
        policy: Optional[RepairExecutionPolicy] = None,
        replacement_content: Optional[str] = None,
    ) -> Tuple[RepairRequest, Optional[PatchArtifact], Optional[ValidationResult]]:
        """Run the end-to-end candidate patch generation workflow for a RepairRequest.

        Transitions:
        CREATED -> PLANNED -> VALIDATING -> READY -> EXECUTING -> PATCH_GENERATED -> PATCH_VALIDATING

        Returns:
            Tuple of (updated_request, patch_artifact, validation_result)
        """
        active_policy = policy or RepairExecutionPolicy()

        # Step 1: Precondition & Request Validation
        try:
            finding, evidence_records = self.req_validator.validate_request(
                request,
                workspace,
                active_policy,
            )
        except Exception as val_err:
            cat = FailureCategory.SCOPE_VIOLATION if isinstance(val_err, ScopeContainmentViolationError) else FailureCategory.WORKSPACE_ERROR
            fail = FailureClassification(
                category=cat,
                message=str(val_err),
            )
            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.REJECTED,
                actor=RepairActor.VALIDATOR,
                context={"failure": fail, "error_message": str(val_err)},
            )
            raise val_err

        # Step 2: Formulate Grounded Repair Plan
        try:
            plan = self.planner.formulate_plan(
                request=request,
                policy=active_policy,
                finding=finding,
                evidence_records=evidence_records,
                workspace=workspace,
            )
            self.exec_store.save_repair_plan(plan)
            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.PLANNED,
                actor=RepairActor.PLANNER,
                context={"plan": plan, "reason": "Formulated grounded repair plan"},
            )
        except Exception as plan_err:
            fail = FailureClassification(
                category=FailureCategory.INTERNAL_ERROR,
                message=f"Planning failed: {plan_err}",
            )
            self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.FAILED,
                actor=RepairActor.PLANNER,
                context={"failure": fail, "error_message": str(plan_err)},
            )
            raise plan_err

        # Step 3: Validate Plan Boundaries (PLANNED -> VALIDATING -> READY)
        try:
            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.VALIDATING,
                actor=RepairActor.VALIDATOR,
                context={"reason": "Validating repair plan bounds and scope"},
            )

            # Validate plan scope against policy
            active_policy.validate_target_file(plan.target_file, request.allowed_files)
            val_res = ValidationResult(
                is_valid=True,
                checks_passed=["plan_scope_validated", "target_file_authorized"],
            )

            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.READY,
                actor=RepairActor.VALIDATOR,
                context={"validation": val_res, "reason": "Plan validated successfully"},
            )
        except Exception as val_plan_err:
            fail = FailureClassification(
                category=FailureCategory.SCOPE_VIOLATION,
                message=f"Plan validation failed: {val_plan_err}",
            )
            self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.FAILED,
                actor=RepairActor.VALIDATOR,
                context={"failure": fail, "error_message": str(val_plan_err)},
            )
            raise val_plan_err

        # Step 4: Generate Candidate Patch (READY -> EXECUTING -> PATCH_GENERATED)
        try:
            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.EXECUTING,
                actor=RepairActor.EXECUTOR,
                context={"reason": "Beginning candidate patch generation"},
            )

            patch = self.generator.execute_repair(
                plan=plan,
                workspace=workspace,
                policy=active_policy,
                finding=finding,
                evidence_records=evidence_records,
                replacement_content=replacement_content,
            )

            self.exec_store.save_patch_artifact(patch)
            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.PATCH_GENERATED,
                actor=RepairActor.EXECUTOR,
                context={"patch": patch, "reason": f"Generated candidate patch {patch.artifact_id}"},
            )
        except Exception as gen_err:
            fail = FailureClassification(
                category=FailureCategory.INTERNAL_ERROR,
                message=f"Patch generation failed: {gen_err}",
            )
            self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.FAILED,
                actor=RepairActor.EXECUTOR,
                context={"failure": fail, "error_message": str(gen_err)},
            )
            raise gen_err

        # Step 5: Validate Candidate Patch (PATCH_GENERATED -> PATCH_VALIDATING)
        try:
            req = self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.PATCH_VALIDATING,
                actor=RepairActor.VALIDATOR,
                context={"reason": "Validating candidate patch syntax and semantic invariants"},
            )

            patch_val_result = self.patch_validator.validate_patch(
                patch=patch,
                request=request,
                policy=active_policy,
                workspace=workspace,
            )

            if isinstance(patch_val_result, IndependentValidationResult):
                self.exec_store.save_validation_result(patch_val_result)

            if not patch_val_result.is_valid:
                v_str = " ".join(patch_val_result.violations).lower()
                if any(k in v_str for k in ("content hash", "artifact identity", "base commit", "workspace identity", "cross-repair", "baseline", "drift")):
                    fail_cat = FailureCategory.INTEGRITY_ERROR
                    exc_cls = PatchIntegrityError
                elif any(k in v_str for k in ("null byte", "control character", "secret", "duplicate hunk", "overlapping hunk", "file mode", "rename", "binary patches")):
                    fail_cat = FailureCategory.ADVERSARIAL_DEFECT
                    exc_cls = PatchAdversarialDefectError
                elif any(k in v_str for k in ("semantic invariant", "auth", "syntax", "exception handling", "dangerous", "unreachable", "guard")):
                    fail_cat = FailureCategory.SEMANTIC_VIOLATION
                    exc_cls = PatchSemanticInvariantError
                elif any(k in v_str for k in ("scope", "traversal", "absolute", "protected", "modifies")):
                    fail_cat = FailureCategory.SCOPE_VIOLATION
                    exc_cls = PatchScopeViolationError
                else:
                    fail_cat = FailureCategory.SYNTAX_ERROR
                    exc_cls = PatchValidationError

                fail = FailureClassification(
                    category=fail_cat,
                    message=f"Patch validation failed: {patch_val_result.violations}",
                    details={"violations": patch_val_result.violations},
                )
                self.exec_store.transition_repair_state(
                    repair_id=request.repair_id,
                    to_state=RepairExecutionState.FAILED,
                    actor=RepairActor.ORCHESTRATOR,
                    context={"failure": fail, "error_message": str(patch_val_result.violations)},
                )
                raise exc_cls(
                    f"Candidate patch validation failed: {patch_val_result.violations}",
                    details={"violations": patch_val_result.violations},
                )

            # Record independent validation audit event
            val_id = getattr(patch_val_result, "validation_id", f"val_{patch.artifact_id[:8]}")
            self.exec_store.record_event(
                RepairExecutionEvent(
                    event_id=f"ree_{uuid.uuid4().hex[:12]}",
                    repair_id=request.repair_id,
                    sequence=1,
                    from_state=RepairExecutionState.PATCH_VALIDATING,
                    to_state=RepairExecutionState.PATCH_VALIDATING,
                    actor=RepairActor.VALIDATOR,
                    payload={
                        "action": "patch_independently_validated",
                        "validation_id": val_id,
                        "patch_id": patch.artifact_id,
                        "overall_status": getattr(patch_val_result, "overall_status", "ACCEPTED"),
                        "checks_passed": patch_val_result.checks_passed,
                    },
                )
            )

        except Exception as p_val_err:
            if not isinstance(p_val_err, PatchValidationError):
                fail = FailureClassification(
                    category=FailureCategory.INTERNAL_ERROR,
                    message=f"Patch validation error: {p_val_err}",
                )
                self.exec_store.transition_repair_state(
                    repair_id=request.repair_id,
                    to_state=RepairExecutionState.FAILED,
                    actor=RepairActor.ORCHESTRATOR,
                    context={"failure": fail, "error_message": str(p_val_err)},
                )
            raise p_val_err

        # Step 6: Safe Patch Application inside Workspace (remains in PATCH_VALIDATING)
        applied_ok, apply_err = self.applier.apply_patch(
            patch=patch,
            workspace=workspace,
            allowed_files=request.allowed_files,
        )

        if not applied_ok:
            fail = FailureClassification(
                category=FailureCategory.BUILD_BROKEN,
                message=f"Patch application failed: {apply_err}",
            )
            self.exec_store.transition_repair_state(
                repair_id=request.repair_id,
                to_state=RepairExecutionState.FAILED,
                actor=RepairActor.EXECUTOR,
                context={"failure": fail, "error_message": apply_err},
            )
            raise PatchApplicationError(f"Failed to apply candidate patch: {apply_err}")

        # Record application success event
        self.exec_store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{patch.artifact_id[:8]}",
                repair_id=request.repair_id,
                sequence=1,
                from_state=RepairExecutionState.PATCH_VALIDATING,
                to_state=RepairExecutionState.PATCH_VALIDATING,
                actor=RepairActor.EXECUTOR,
                payload={
                    "action": "candidate_patch_applied",
                    "patch_id": patch.artifact_id,
                    "target_file": patch.target_file,
                    "content_hash": patch.content_hash,
                    "status": "applied_cleanly",
                },
            )
        )

        # Reload updated request
        final_req = self.exec_store.get_repair_request(request.repair_id) or req
        return final_req, patch, patch_val_result
