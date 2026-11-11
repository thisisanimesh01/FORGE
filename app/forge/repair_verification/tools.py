"""Phase 12 M5: Repair Verification Tools for Agent & LLM Invocation.

Exposes 9 structured, typed, read-only tools:
- create_repair_verification_plan: Synthesizes a structured VerificationPlan.
- discover_repair_tests: Bounded discovery mapping modified files to test files.
- inspect_verification_environment: Diagnoses container sandbox availability and health.
- run_repair_verification: Executes verification plan against repaired workspace.
- run_baseline_verification: Executes verification plan against clean baseline snapshot.
- compare_repair_verification: Compares baseline and repaired results for regressions.
- inspect_verification_evidence: Queries cryptographic evidence records.
- validate_verification_integrity: Checks repository, snapshot, and workspace containment.
- get_repair_verification_summary: Returns human-readable markdown verification report.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from forge.config import get_settings, redact_secrets
from forge.llm.models import ToolDefinition
from forge.repair_execution.schemas import RepairExecutionResult, RepairWorkspace
from forge.repair_verification.discovery import RepairTestDiscoverer
from forge.repair_verification.events import VerificationEventRecorder
from forge.repair_verification.executor import RepairVerificationExecutor
from forge.repair_verification.integrity import VerificationIntegrityChecker
from forge.repair_verification.sandbox import VerificationSandbox
from forge.repair_verification.schemas import (
    CompareRepairVerificationInput,
    CompareRepairVerificationOutput,
    CreateRepairVerificationPlanInput,
    CreateRepairVerificationPlanOutput,
    DiscoverRepairTestsInput,
    DiscoverRepairTestsOutput,
    GetRepairVerificationSummaryInput,
    GetRepairVerificationSummaryOutput,
    InspectVerificationEvidenceInput,
    InspectVerificationEvidenceOutput,
    InspectVerificationEnvironmentInput,
    InspectVerificationEnvironmentOutput,
    RegressionClassification,
    RunBaselineVerificationInput,
    RunBaselineVerificationOutput,
    RunRepairVerificationInput,
    RunRepairVerificationOutput,
    ValidateVerificationIntegrityInput,
    ValidateVerificationIntegrityOutput,
    VerificationPlan,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.repository.validation import SecurityError, assert_contained_path


class RepairVerificationTools:
    """Tool invocation handler for Phase 12 M5 Repair Verification."""

    def __init__(
        self,
        repository_path: Path,
        sandbox: Optional[VerificationSandbox] = None,
        event_recorder: Optional[VerificationEventRecorder] = None,
        max_output_chars: int = 50_000,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.max_output_chars = max_output_chars
        self.sandbox = sandbox or VerificationSandbox()
        self.event_recorder = event_recorder or VerificationEventRecorder()
        self.executor = RepairVerificationExecutor(
            sandbox=self.sandbox,
            event_recorder=self.event_recorder,
        )
        self._plans: Dict[str, VerificationPlan] = {}
        self._results: Dict[str, VerificationResult] = {}
        self._workspaces: Dict[str, RepairWorkspace] = {}
        self._executions: Dict[str, RepairExecutionResult] = {}

    def register_workspace(self, workspace: RepairWorkspace) -> None:
        """Register workspace for tool lookup."""
        self._workspaces[workspace.workspace_id] = workspace

    def register_execution(self, execution: RepairExecutionResult) -> None:
        """Register execution result for tool lookup."""
        self._executions[execution.execution_id] = execution

    def register_plan(self, plan: VerificationPlan) -> None:
        """Register verification plan for tool lookup."""
        self._plans[plan.plan_id] = plan

    def register_result(self, result: VerificationResult) -> None:
        """Register verification result for tool lookup."""
        self._results[result.verification_id] = result

    # -------------------------------------------------------------
    # Tool Handlers
    # -------------------------------------------------------------

    def create_repair_verification_plan(self, args: Dict[str, Any]) -> str:
        """Handle create_repair_verification_plan tool."""
        try:
            inp = CreateRepairVerificationPlanInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        exec_res = self._executions.get(inp.execution_id)
        if not exec_res:
            return json.dumps({"error": f"Execution ID '{inp.execution_id}' not found."})

        ws = self._workspaces.get(inp.workspace_id)
        if not ws:
            return json.dumps({"error": f"Workspace ID '{inp.workspace_id}' not found."})

        policy = VerificationPolicy()
        if inp.policy_overrides:
            try:
                policy = VerificationPolicy(**inp.policy_overrides)
            except Exception as e:
                return json.dumps({"error": f"Invalid policy overrides: {e}"})

        try:
            plan = self.executor.plan_generator.generate_plan(
                execution_result=exec_res,
                workspace=ws,
            )
            self.register_plan(plan)
            out = CreateRepairVerificationPlanOutput(
                plan_id=plan.plan_id,
                selected_checks_count=len(plan.selected_checks),
                is_partial_verification=plan.is_partial_verification,
                selection_rationale=plan.selection_rationale,
                status="created",
            )
            return out.model_dump_json()
        except Exception as e:
            return json.dumps({"error": f"Failed to generate verification plan: {e}"})

    def _is_contained_workspace(self, target: Path) -> bool:
        resolved = target.resolve()
        roots = [self.repository_path, get_settings().workspace_dir.resolve()]
        roots.extend(Path(ws.isolated_workspace_path).resolve() for ws in self._workspaces.values())
        for root in roots:
            try:
                assert_contained_path(resolved, root)
                return True
            except SecurityError:
                continue
        return False

    def discover_repair_tests(self, args: Dict[str, Any]) -> str:
        """Handle discover_repair_tests tool."""
        try:
            inp = DiscoverRepairTestsInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        ws_path = Path(inp.workspace_path).resolve()
        if not self._is_contained_workspace(ws_path):
            return json.dumps({"error": f"Security boundary violation: workspace path '{ws_path}' is outside allowed roots."})
        if not ws_path.exists():
            return json.dumps({"error": f"Workspace path does not exist: {inp.workspace_path}"})

        try:
            discoverer = RepairTestDiscoverer(workspace_path=ws_path)
            res = discoverer.discover_tests(modified_files=inp.modified_files)
            out = DiscoverRepairTestsOutput(
                discovered_test_files=res.all_test_files,
                targeted_tests=[t.test_id for t in res.targeted_tests],
                regression_tests=[t.test_id for t in res.regression_tests],
                total_discovered=len(res.targeted_tests) + len(res.regression_tests) + len(res.other_tests),
            )
            return out.model_dump_json()
        except Exception as e:
            return json.dumps({"error": f"Test discovery failed: {e}"})

    def inspect_verification_environment(self, args: Dict[str, Any]) -> str:
        """Handle inspect_verification_environment tool."""
        try:
            InspectVerificationEnvironmentInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        healthy, msg = self.sandbox.check_health()
        policy = VerificationPolicy()
        out = InspectVerificationEnvironmentOutput(
            sandbox_available=self.sandbox.is_available(),
            sandbox_healthy=healthy,
            diagnostic_message=msg,
            docker_image=getattr(getattr(self.sandbox, "sandbox", None), "docker_image", None),
            allowed_executables=policy.allowed_executables,
        )
        return out.model_dump_json()

    def run_repair_verification(self, args: Dict[str, Any]) -> str:
        """Handle run_repair_verification tool."""
        try:
            inp = RunRepairVerificationInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        plan = self._plans.get(inp.plan_id)
        if not plan:
            return json.dumps({"error": f"Verification plan '{inp.plan_id}' not found."})

        ws = self._workspaces.get(inp.workspace_id)
        if not ws:
            return json.dumps({"error": f"Workspace '{inp.workspace_id}' not found."})

        exec_res = self._executions.get(plan.execution_id)
        if not exec_res:
            return json.dumps({"error": f"Execution result '{plan.execution_id}' not found."})

        try:
            v_res, _ = self.executor.verify_repair(
                execution_result=exec_res,
                workspace=ws,
                plan=plan,
            )
            self.register_result(v_res)
            all_passed = v_res.verification_status == VerificationStatus.PASSED
            out = RunRepairVerificationOutput(
                verification_id=v_res.verification_id,
                status=v_res.verification_status,
                all_passed=all_passed,
                tests_passed=v_res.test_counts.get("passed", 0),
                tests_failed=v_res.test_counts.get("failed", 0),
                collection_errors=v_res.test_counts.get("errored", 0),
                diagnostic_message=(
                    f"Verification outcome: {v_res.verification_status.value} (Gate: {v_res.gate_decision})"
                ),
            )
            return out.model_dump_json()
        except Exception as e:
            return json.dumps({"error": f"Verification execution failed: {e}"})

    def run_baseline_verification(self, args: Dict[str, Any]) -> str:
        """Handle run_baseline_verification tool."""
        try:
            inp = RunBaselineVerificationInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        plan = self._plans.get(inp.plan_id)
        if not plan:
            return json.dumps({"error": f"Verification plan '{inp.plan_id}' not found."})

        ws = self._workspaces.get(inp.workspace_id)
        if not ws:
            return json.dumps({"error": f"Workspace '{inp.workspace_id}' not found."})

        try:
            base_summary = self.executor.baseline_runner.run_baseline(plan=plan, workspace=ws)
            tests_passed = sum(c.tests_passed for c in base_summary.checks)
            tests_failed = sum(c.tests_failed for c in base_summary.checks)
            out = RunBaselineVerificationOutput(
                execution_type="baseline",
                baseline_digest_verified=base_summary.tree_digest == plan.expected_baseline_digest,
                all_passed=base_summary.all_passed,
                tests_passed=tests_passed,
                tests_failed=tests_failed,
                diagnostic_message=base_summary.sandbox_details.get("error"),
            )
            return out.model_dump_json()
        except Exception as e:
            return json.dumps({"error": f"Baseline verification failed: {e}"})

    def compare_repair_verification(self, args: Dict[str, Any]) -> str:
        """Handle compare_repair_verification tool."""
        try:
            inp = CompareRepairVerificationInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        v_res = self._results.get(inp.verification_id)
        if not v_res:
            return json.dumps({"error": f"Verification result '{inp.verification_id}' not found."})

        reg_cnt = sum(1 for f in v_res.regression_findings if f.classification == RegressionClassification.REGRESSION_DETECTED)
        imp_cnt = sum(1 for f in v_res.regression_findings if f.classification == RegressionClassification.IMPROVED)
        pre_cnt = sum(1 for f in v_res.regression_findings if f.classification == RegressionClassification.PRE_EXISTING_FAILURE)

        out = CompareRepairVerificationOutput(
            classification=v_res.regression_classification,
            regressions_count=reg_cnt,
            improvements_count=imp_cnt,
            pre_existing_failures_count=pre_cnt,
            explanation=f"Regression classification: {v_res.regression_classification.value}",
        )
        return out.model_dump_json()

    def inspect_verification_evidence(self, args: Dict[str, Any]) -> str:
        """Handle inspect_verification_evidence tool."""
        try:
            inp = InspectVerificationEvidenceInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        v_res = self._results.get(inp.verification_id)
        if not v_res:
            return json.dumps({"error": f"Verification result '{inp.verification_id}' not found."})

        records = v_res.evidence_records
        if inp.evidence_id:
            records = [r for r in records if r.evidence_id == inp.evidence_id]

        out = InspectVerificationEvidenceOutput(
            evidence_records=records,
            total_records=len(records),
        )
        return out.model_dump_json()

    def validate_verification_integrity(self, args: Dict[str, Any]) -> str:
        """Handle validate_verification_integrity tool."""
        try:
            inp = ValidateVerificationIntegrityInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        ws = self._workspaces.get(inp.workspace_id)
        if not ws:
            return json.dumps({"error": f"Workspace '{inp.workspace_id}' not found."})

        ws_path = Path(ws.isolated_workspace_path)
        snap_path = ws_path.parent / f"{ws.workspace_id}_baseline"

        repo_ok = VerificationIntegrityChecker.verify_original_repo_unmodified(self.repository_path)
        snap_ok = True
        if snap_path.exists():
            snap_ok, _ = VerificationIntegrityChecker.verify_baseline_snapshot(snap_path, ws.baseline_tree_digest)
        ws_ok, violations = VerificationIntegrityChecker.verify_workspace_integrity(ws_path, ws.allowed_paths)

        is_valid = repo_ok and snap_ok and ws_ok
        out = ValidateVerificationIntegrityOutput(
            original_repo_unmodified=repo_ok,
            baseline_snapshot_intact=snap_ok,
            repaired_workspace_valid=ws_ok,
            integrity_status="VALID" if is_valid else "VIOLATION_DETECTED",
            violations=violations,
        )
        return out.model_dump_json()

    def get_repair_verification_summary(self, args: Dict[str, Any]) -> str:
        """Handle get_repair_verification_summary tool."""
        try:
            inp = GetRepairVerificationSummaryInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Validation error: {e}"})

        v_res = self._results.get(inp.verification_id)
        if not v_res:
            return json.dumps({"error": f"Verification result '{inp.verification_id}' not found."})

        md_lines = [
            f"# Repair Verification Summary: {v_res.verification_id}",
            f"- **Status**: `{v_res.verification_status.value}`",
            f"- **Gate Decision**: `{v_res.gate_decision}`",
            f"- **Regression Status**: `{v_res.regression_classification.value}`",
            f"- **Human Review Required**: `{v_res.requires_human_review}`",
            f"- **Tests Passed**: `{v_res.test_counts.get('passed', 0)}`",
            f"- **Tests Failed**: `{v_res.test_counts.get('failed', 0)}`",
            f"- **Required Checks Completed**: `{', '.join(v_res.required_checks_completed) or 'None'}`",
            f"- **Required Checks Failed**: `{', '.join(v_res.required_checks_failed) or 'None'}`",
        ]
        if v_res.limitations:
            md_lines.append("\n### Limitations:")
            for lim in v_res.limitations:
                md_lines.append(f"- {lim}")

        summary_md = "\n".join(md_lines)
        out = GetRepairVerificationSummaryOutput(
            verification_id=v_res.verification_id,
            status=v_res.verification_status,
            gate_decision=v_res.gate_decision,
            regression_classification=v_res.regression_classification,
            requires_human_review=v_res.requires_human_review,
            summary_markdown=summary_md,
        )
        return out.model_dump_json()

    # -------------------------------------------------------------
    # Tool Definitions
    # -------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinition schemas for all 9 verification tools."""
        return [
            ToolDefinition(
                name="create_repair_verification_plan",
                description="Synthesize a structured, minimal VerificationPlan for an applied repair execution.",
                parameters=CreateRepairVerificationPlanInput.model_json_schema(),
            ),
            ToolDefinition(
                name="discover_repair_tests",
                description="Bounded test discovery mapping modified source files to targeted and regression tests.",
                parameters=DiscoverRepairTestsInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_verification_environment",
                description="Inspect and diagnose container sandbox execution availability and health.",
                parameters=InspectVerificationEnvironmentInput.model_json_schema(),
            ),
            ToolDefinition(
                name="run_repair_verification",
                description="Execute verification checks inside isolated container sandbox against repaired workspace.",
                parameters=RunRepairVerificationInput.model_json_schema(),
            ),
            ToolDefinition(
                name="run_baseline_verification",
                description="Execute verification checks inside isolated container sandbox against clean baseline snapshot.",
                parameters=RunBaselineVerificationInput.model_json_schema(),
            ),
            ToolDefinition(
                name="compare_repair_verification",
                description="Perform deterministic regression comparison between baseline and repaired outcomes.",
                parameters=CompareRepairVerificationInput.model_json_schema(),
            ),
            ToolDefinition(
                name="inspect_verification_evidence",
                description="Retrieve cryptographic evidence records and execution artifacts.",
                parameters=InspectVerificationEvidenceInput.model_json_schema(),
            ),
            ToolDefinition(
                name="validate_verification_integrity",
                description="Audit workspace, baseline snapshot, and repository containment and immutability.",
                parameters=ValidateVerificationIntegrityInput.model_json_schema(),
            ),
            ToolDefinition(
                name="get_repair_verification_summary",
                description="Retrieve formatted markdown summary of repair verification outcome.",
                parameters=GetRepairVerificationSummaryInput.model_json_schema(),
            ),
        ]
