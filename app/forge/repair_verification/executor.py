"""Phase 12 M5: Repair Verification Orchestration Engine.

Coordinates the complete evidence-driven repair verification lifecycle:
1. Validates M4 RepairExecutionResult and RepairWorkspace contracts.
2. Generates reproducible VerificationPlan across targeted, regression, and static checks.
3. Checks container sandbox health, failing closed if unavailable.
4. Audits baseline snapshot integrity and executes baseline verification.
5. Executes verification plan against isolated repaired workspace.
6. Parses results with consistency verification and collection error detection.
7. Performs deterministic regression analysis.
8. Verifies post-execution workspace and repository integrity.
9. Evaluates verification gates and records cryptographically chained audit events.
10. Produces final VerificationResult, updating verification_pending handoff only if PASSED.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from forge.repair_execution.schemas import ExecutionStatus, RepairExecutionResult, RepairWorkspace
from forge.repair_execution.workspace import compute_tree_digest
from forge.repair_verification.baseline import BaselineVerificationRunner
from forge.repair_verification.events import VerificationEventRecorder
from forge.repair_verification.gates import VerificationGateKeeper
from forge.repair_verification.integrity import VerificationIntegrityChecker
from forge.repair_verification.regression import DeterministicRegressionEngine
from forge.repair_verification.result_parser import PytestResultParser
from forge.repair_verification.sandbox import VerificationSandbox
from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    RegressionClassification,
    RegressionFinding,
    SelectedCheck,
    SuiteExecutionSummary,
    VerificationEvidenceRecord,
    VerificationEventType,
    VerificationPlan,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.repair_verification.verification_plan import VerificationPlanGenerator


class RepairVerificationExecutor:
    """Orchestrates the entire repair verification lifecycle."""

    def __init__(
        self,
        sandbox: Optional[VerificationSandbox] = None,
        event_recorder: Optional[VerificationEventRecorder] = None,
        baseline_runner: Optional[BaselineVerificationRunner] = None,
    ) -> None:
        self.sandbox = sandbox or VerificationSandbox()
        self.event_recorder = event_recorder or VerificationEventRecorder()
        self.baseline_runner = baseline_runner or BaselineVerificationRunner(sandbox=self.sandbox)
        self.plan_generator = VerificationPlanGenerator()

    def verify_repair(
        self,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        plan: Optional[VerificationPlan] = None,
        policy: Optional[VerificationPolicy] = None,
    ) -> Tuple[VerificationResult, Optional[RepairExecutionResult]]:
        """Execute full verification pipeline for an applied repair.

        Returns:
            Tuple of (VerificationResult, Optional[RepairExecutionResult with verification_pending=False if PASSED])
        """
        active_policy = policy or (plan.policy if plan else VerificationPolicy())
        verification_seed = f"{execution_result.execution_id}:{workspace.workspace_id}"
        verification_hash = hashlib.sha256(verification_seed.encode("utf-8")).hexdigest()[:12]
        verification_id = f"vrf_{verification_hash}"

        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.VERIFICATION_REQUESTED,
            action_description=f"Initiated repair verification for execution {execution_result.execution_id}",
        )

        # 1. Generate or validate VerificationPlan
        if plan is None:
            generator = VerificationPlanGenerator(policy=active_policy)
            plan = generator.generate_plan(execution_result=execution_result, workspace=workspace)

        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.VERIFICATION_PLAN_GENERATED,
            action_description=f"Generated plan {plan.plan_id} with {len(plan.selected_checks)} check(s)",
        )

        # 2. Inspect Sandbox Health
        sandbox_avail, health_msg = self.sandbox.check_health()
        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.SANDBOX_INSPECTED,
            action_description=f"Sandbox health check: available={sandbox_avail} ({health_msg})",
            outcome="success" if sandbox_avail else "blocked",
        )

        evidence_records: List[VerificationEvidenceRecord] = []
        limitations: List[str] = []

        if not sandbox_avail:
            limitations.append(f"Verification blocked: secure container sandbox is unavailable ({health_msg})")
            self.event_recorder.record_event(
                verification_id=verification_id,
                event_type=VerificationEventType.VERIFICATION_FAILED,
                action_description="Verification failed closed due to missing sandbox",
                outcome="blocked",
            )
            v_result = VerificationResult(
                verification_id=verification_id,
                execution_id=execution_result.execution_id,
                workspace_id=workspace.workspace_id,
                repair_plan_id=execution_result.repair_plan_id,
                repair_plan_digest=plan.expected_patch_digest,
                applied_patch_digest=execution_result.applied_patch_digest or "",
                baseline_tree_digest=workspace.baseline_tree_digest,
                verification_status=VerificationStatus.SANDBOX_UNAVAILABLE,
                regression_classification=RegressionClassification.INCONCLUSIVE,
                gate_decision="BLOCKED",
                requires_human_review=True,
                limitations=limitations,
            )
            return v_result, None

        # 3. Baseline Verification Execution
        baseline_summary: Optional[SuiteExecutionSummary] = None
        if active_policy.require_baseline_comparison:
            self.event_recorder.record_event(
                verification_id=verification_id,
                event_type=VerificationEventType.BASELINE_EXECUTION_STARTED,
                action_description=f"Starting baseline verification against snapshot for workspace {workspace.workspace_id}",
            )
            baseline_summary = self.baseline_runner.run_baseline(plan=plan, workspace=workspace)
            self.event_recorder.record_event(
                verification_id=verification_id,
                event_type=VerificationEventType.BASELINE_EXECUTION_COMPLETED,
                action_description=f"Baseline verification completed: all_passed={baseline_summary.all_passed}",
                outcome="success" if baseline_summary.all_passed else "failure",
            )

        # 4. Repaired Workspace Execution
        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.REPAIR_EXECUTION_STARTED,
            action_description=f"Executing {len(plan.selected_checks)} check(s) against repaired workspace {workspace.workspace_id}",
        )

        ws_path = Path(workspace.isolated_workspace_path)
        repaired_checks: List[CheckExecutionResult] = []
        all_repaired_passed = True
        test_counts: Dict[str, int] = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0, "total": 0}

        for chk in plan.selected_checks:
            sbx_res = self.sandbox.execute_check(
                check=chk,
                workspace_path=ws_path,
                policy=active_policy,
                run_id=f"rep_{workspace.workspace_id}",
            )
            parsed_chk = PytestResultParser.parse_result(check=chk, sandbox_res=sbx_res)
            repaired_checks.append(parsed_chk)

            # Accumulate counts
            test_counts["passed"] += parsed_chk.tests_passed
            test_counts["failed"] += parsed_chk.tests_failed
            test_counts["errored"] += parsed_chk.tests_errored
            test_counts["skipped"] += parsed_chk.tests_skipped
            test_counts["total"] += (
                parsed_chk.tests_passed
                + parsed_chk.tests_failed
                + parsed_chk.tests_errored
                + parsed_chk.tests_skipped
            )

            if parsed_chk.outcome != CheckOutcome.PASSED:
                all_repaired_passed = False

            # Create evidence record
            ev_rec = VerificationEvidenceRecord(
                evidence_id=f"vev_{parsed_chk.check_id}_{verification_id[:6]}",
                verification_id=verification_id,
                source_command=chk.command,
                category=chk.category.value,
                target_scope=", ".join(chk.target_files) if chk.target_files else "workspace",
                classification=parsed_chk.outcome.value,
                output_digest=parsed_chk.stdout_digest,
            )
            evidence_records.append(ev_rec)

        current_ws_digest = compute_tree_digest(ws_path)
        repaired_summary = SuiteExecutionSummary(
            execution_type="repaired",
            workspace_id=workspace.workspace_id,
            tree_digest=current_ws_digest,
            checks=repaired_checks,
            all_passed=all_repaired_passed,
            completed_at=datetime.now(timezone.utc).isoformat(),
            sandbox_available=True,
        )

        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.REPAIR_EXECUTION_COMPLETED,
            action_description=f"Repaired execution completed: all_passed={all_repaired_passed}",
            outcome="success" if all_repaired_passed else "failure",
        )

        # 5. Deterministic Regression Analysis
        regression_class, findings = DeterministicRegressionEngine.analyze_regressions(
            baseline_summary=baseline_summary,
            repaired_summary=repaired_summary,
        )

        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.REGRESSION_ANALYSIS_COMPLETED,
            action_description=f"Regression analysis outcome: {regression_class.value} with {len(findings)} finding(s)",
        )

        # 6. Integrity Verification
        is_intact, violations = VerificationIntegrityChecker.verify_workspace_integrity(
            workspace_path=ws_path,
            allowed_modified_paths=workspace.allowed_paths,
        )
        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=(
                VerificationEventType.INTEGRITY_CHECK_COMPLETED
                if is_intact
                else VerificationEventType.INTEGRITY_VIOLATION_DETECTED
            ),
            action_description=(
                "Workspace integrity verified successfully"
                if is_intact
                else f"Integrity violations detected: {', '.join(violations)}"
            ),
            outcome="success" if is_intact else "violation",
        )

        # 7. Gate Evaluation
        (
            final_status,
            gate_decision,
            req_completed,
            req_failed,
            opt_failed,
        ) = VerificationGateKeeper.evaluate_gates(
            execution_result=execution_result,
            plan=plan,
            sandbox_available=sandbox_avail,
            baseline_summary=baseline_summary,
            repaired_summary=repaired_summary,
            regression_class=regression_class,
            workspace_integrity_passed=is_intact,
            integrity_violations=violations,
        )

        self.event_recorder.record_event(
            verification_id=verification_id,
            event_type=VerificationEventType.GATES_EVALUATED,
            action_description=f"Gates evaluated: status={final_status.value}, decision={gate_decision}",
        )

        if final_status == VerificationStatus.PASSED:
            self.event_recorder.record_event(
                verification_id=verification_id,
                event_type=VerificationEventType.VERIFICATION_PASSED,
                action_description=f"Repair verification PASSED for plan {plan.plan_id}",
            )
            # Create updated M4 execution result with verification_pending = False
            updated_exec_result = execution_result.model_copy(
                update={"verification_pending": False}
            )
        else:
            self.event_recorder.record_event(
                verification_id=verification_id,
                event_type=VerificationEventType.VERIFICATION_FAILED,
                action_description=f"Repair verification {final_status.value} (decision: {gate_decision})",
                outcome="failed",
            )
            updated_exec_result = None

        if plan.is_partial_verification:
            limitations.append(
                "Verification was performed on a partial test selection, not the entire test suite."
            )

        verification_result = VerificationResult(
            verification_id=verification_id,
            execution_id=execution_result.execution_id,
            workspace_id=workspace.workspace_id,
            repair_plan_id=execution_result.repair_plan_id,
            repair_plan_digest=plan.expected_patch_digest,
            applied_patch_digest=execution_result.applied_patch_digest or "",
            baseline_tree_digest=workspace.baseline_tree_digest,
            verification_status=final_status,
            regression_classification=regression_class,
            required_checks_completed=req_completed,
            required_checks_failed=req_failed,
            optional_checks_failed=opt_failed,
            skipped_checks=plan.excluded_checks,
            test_counts=test_counts,
            baseline_summary=baseline_summary,
            repaired_summary=repaired_summary,
            regression_findings=findings,
            evidence_records=evidence_records,
            gate_decision=gate_decision,
            requires_human_review=True,
            limitations=limitations,
        )

        return verification_result, updated_exec_result
