"""Phase 8 Repair Execution Architecture — Sandbox Repair Verification & Regression Testing.

Orchestrates defense-in-depth verification of candidate patches inside isolated
Docker sandbox scratch workspaces:
- Enforces strict verification preconditions (provenance, M4 validation, hash integrity, target immutability).
- Discovers and prioritizes reproduction and regression test suites with explicit rationale.
- Executes tests in isolated Docker sandboxes via RepairWorkspaceManager with fail-closed guarantee.
- Performs dual-workspace baseline vs patched comparative regression analysis.
- Extracts and bounds test execution evidence, parsing test collection and assertion metrics.
- Collects structured evidence records and persists VerificationRunRecord in SQLite.
- Produces structured verification reports (Markdown and JSON).
- Adheres to zero-fabrication and audit immutability rules.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from forge.config import redact_secrets
from forge.repair.execution.failures import (
    PatchIntegrityError,
    SandboxUnavailableError,
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
    VerificationExecutionError,
    VerificationPreconditionError,
    WorkspaceExecutionError,
    WorkspaceIntegrityViolationError,
)
from forge.repair.execution.interfaces import (
    RepairVerifierProtocol,
    RepairWorkspaceProtocol,
)
from forge.repair.execution.manager import RepairWorkspaceManager
from forge.repair.execution.models import (
    ComparisonStatus,
    ExecutionTestResult,
    FailureCategory,
    FailureClassification,
    IndependentValidationResult,
    PatchArtifact,
    RegressionAnalysisRecord,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairExecutionState,
    RepairRequest,
    SelectedTest,
    TestComparisonResult,
    TestExecutionRecord,
    TestSelectionCategory,
    ValidationOutcome,
    VerificationOutcome,
    VerificationPlan,
    VerificationRunRecord,
)
from forge.repair.execution.policy import (
    ArbitraryExecutionForbiddenError,
    RepairExecutionPolicy,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.workspace import RepairWorkspace
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import ExecutionLimits, ExecutionStatus, SandboxResult
from forge.schemas import EvidenceRecord, EvidenceType, Finding
from forge.storage.store import ForgeStore


# =============================================================================
# Helper: Parse Test Runner Output
# =============================================================================

def parse_test_runner_output(stdout: str, stderr: str) -> Dict[str, int]:
    """Parse output from pytest or unittest to extract test counts without fabrication."""
    combined = f"{stdout}\n{stderr}"
    metrics: Dict[str, int] = {
        "collected": 0,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errored": 0,
    }

    # Pytest collection: "collected (\d+) items?"
    col_m = re.search(r"collected\s+(\d+)\s+item", combined, re.IGNORECASE)
    if col_m:
        metrics["collected"] = int(col_m.group(1))

    # Pytest summary tokens
    passed_m = re.search(r"(\d+)\s+passed\b", combined)
    if passed_m:
        metrics["passed"] = int(passed_m.group(1))

    failed_m = re.search(r"(\d+)\s+failed\b", combined)
    if failed_m:
        metrics["failed"] = int(failed_m.group(1))

    skipped_m = re.search(r"(\d+)\s+skipped\b", combined)
    if skipped_m:
        metrics["skipped"] = int(skipped_m.group(1))

    errored_m = re.search(r"(\d+)\s+error(?:s)?\b", combined)
    if errored_m:
        metrics["errored"] = int(errored_m.group(1))

    # Python standard unittest: "Ran (\d+) test[s]?"
    ran_m = re.search(r"Ran\s+(\d+)\s+tests?", combined)
    if ran_m:
        u_ran = int(ran_m.group(1))
        metrics["executed"] = max(metrics["executed"], u_ran)
        if metrics["collected"] == 0:
            metrics["collected"] = u_ran

        if "OK" in combined and "FAILED" not in combined:
            metrics["passed"] = max(metrics["passed"], u_ran)
        else:
            u_fail = re.search(r"failures=(\d+)", combined)
            if u_fail:
                metrics["failed"] = int(u_fail.group(1))
            u_err = re.search(r"errors=(\d+)", combined)
            if u_err:
                metrics["errored"] = int(u_err.group(1))
            metrics["passed"] = max(0, metrics["executed"] - metrics["failed"] - metrics["errored"])

    # Harmonize executed count if pytest summary was parsed
    if metrics["collected"] > 0 and metrics["executed"] == 0:
        metrics["executed"] = metrics["passed"] + metrics["failed"] + metrics["errored"]

    return metrics


# =============================================================================
# Component: TestSelector
# =============================================================================

class TestSelector:
    """Discovers, prioritizes, and explains test selections for candidate patch verification."""
    __test__ = False

    def select_tests(
        self,
        request: RepairRequest,
        patch: PatchArtifact,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        plan: Optional[RepairExecutionPlan] = None,
        repair_plan: Optional[RepairExecutionPlan] = None,
    ) -> List[SelectedTest]:
        """Select and prioritize tests based on defect reproduction, target file, and regression suite."""
        selected: List[SelectedTest] = []
        seen_commands: Set[str] = set()
        active_plan = plan or repair_plan

        # 1. Primary defect reproduction test
        repro_test = self._discover_reproduction_test(
            finding=finding,
            evidence_records=evidence_records,
            plan=active_plan,
            policy=policy,
        )
        if repro_test:
            cmd_key = " ".join(repro_test.command)
            if cmd_key not in seen_commands:
                seen_commands.add(cmd_key)
                selected.append(repro_test)

        # 2. Module / target file unit tests
        module_tests = self._discover_target_module_tests(
            target_file=patch.target_file,
            workspace=workspace,
            policy=policy,
        )
        for m_test in module_tests:
            cmd_key = " ".join(m_test.command)
            if cmd_key not in seen_commands:
                seen_commands.add(cmd_key)
                selected.append(m_test)

        # 3. Plan verification command (if present and distinct)
        if active_plan and active_plan.verification_command:
            try:
                cmd_tokens = active_plan.verification_command.strip().split()
                if cmd_tokens:
                    validated_tokens = policy.validate_command(cmd_tokens)
                    cmd_key = " ".join(validated_tokens)
                    if cmd_key not in seen_commands:
                        seen_commands.add(cmd_key)
                        selected.append(
                            SelectedTest(
                                test_id=f"test_plan_{uuid.uuid4().hex[:8]}",
                                test_name="plan_verification_command",
                                category=TestSelectionCategory.REPRODUCTION,
                                command=validated_tokens,
                                relative_cwd=".",
                                target_file=active_plan.target_file,
                                rationale="Pre-approved verification command formulated in repair plan",
                                expected_to_fail_on_baseline=True,
                            )
                        )
            except Exception:
                pass

        # 4. Fallback module discovery if nothing selected
        if not selected:
            fallback = self._build_default_test_suite(patch.target_file, policy)
            if fallback:
                selected.append(fallback)

        return selected

    def _discover_reproduction_test(
        self,
        finding: Optional[Finding],
        evidence_records: Optional[List[EvidenceRecord]],
        plan: Optional[RepairExecutionPlan],
        policy: RepairExecutionPolicy,
    ) -> Optional[SelectedTest]:
        """Extract explicit reproduction command from finding details, evidence, or plan."""
        # Check finding details if present
        finding_details = getattr(finding, "details", None)
        if finding and finding_details:
            repro_cmd = finding_details.get("reproduction_command") or finding_details.get("test_command")
            if repro_cmd:
                cmd_tokens = repro_cmd if isinstance(repro_cmd, list) else repro_cmd.strip().split()
                try:
                    val_cmd = policy.validate_command(cmd_tokens)
                    return SelectedTest(
                        test_id=f"test_repro_{uuid.uuid4().hex[:8]}",
                        test_name="finding_reproduction_test",
                        category=TestSelectionCategory.REPRODUCTION,
                        command=val_cmd,
                        relative_cwd=".",
                        target_file=finding.affected_files[0] if finding.affected_files else None,
                        rationale="Explicit defect reproduction command specified in finding evidence",
                        expected_to_fail_on_baseline=True,
                    )
                except Exception:
                    pass

        # Check evidence records
        if evidence_records:
            for ev in evidence_records:
                if ev.evidence_type in (EvidenceType.REPRODUCTION_RESULT, EvidenceType.COMMAND_RESULT):
                    repro_cmd = ev.details.get("command") or ev.details.get("reproduction_command")
                    if repro_cmd:
                        cmd_tokens = repro_cmd if isinstance(repro_cmd, list) else repro_cmd.strip().split()
                        try:
                            val_cmd = policy.validate_command(cmd_tokens)
                            return SelectedTest(
                                test_id=f"test_repro_{uuid.uuid4().hex[:8]}",
                                test_name="evidence_reproduction_test",
                                category=TestSelectionCategory.REPRODUCTION,
                                command=val_cmd,
                                relative_cwd=".",
                                target_file=ev.file_path,
                                rationale=f"Reproduction check derived from evidence record {ev.evidence_id}",
                                expected_to_fail_on_baseline=True,
                            )
                        except Exception:
                            pass

        return None

    def _discover_target_module_tests(
        self,
        target_file: str,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
    ) -> List[SelectedTest]:
        """Discover test files covering the target source module within the workspace."""
        results: List[SelectedTest] = []
        p = Path(target_file)
        stem = p.stem  # e.g. "calculator" from "src/calculator.py"

        ws_path = workspace.workspace_path
        candidate_rel_paths = [
            f"tests/test_{stem}.py",
            f"tests/unit/test_{stem}.py",
            f"test_{stem}.py",
            f"tests/{stem}_test.py",
        ]

        for cand in candidate_rel_paths:
            cand_full = ws_path / cand
            if cand_full.is_file():
                cmd = ["pytest", "-v", cand]
                try:
                    val_cmd = policy.validate_command(cmd)
                    results.append(
                        SelectedTest(
                            test_id=f"test_mod_{stem}_{uuid.uuid4().hex[:6]}",
                            test_name=f"test_{cand}",
                            category=TestSelectionCategory.MODULE,
                            command=val_cmd,
                            relative_cwd=".",
                            target_file=cand,
                            rationale=f"Unit tests covering modified source module '{target_file}'",
                            expected_to_fail_on_baseline=False,
                        )
                    )
                except Exception:
                    continue

        return results

    def _build_default_test_suite(
        self,
        target_file: str,
        policy: RepairExecutionPolicy,
    ) -> Optional[SelectedTest]:
        """Construct fallback test suite if no specific test files were discovered."""
        cmd = ["pytest", "-v"]
        try:
            val_cmd = policy.validate_command(cmd)
            return SelectedTest(
                test_id=f"test_suite_{uuid.uuid4().hex[:8]}",
                test_name="default_pytest_suite",
                category=TestSelectionCategory.REGRESSION,
                command=val_cmd,
                relative_cwd=".",
                target_file=target_file,
                rationale="Default test discovery across workspace for target repository",
                expected_to_fail_on_baseline=False,
            )
        except Exception:
            return None


# =============================================================================
# Component: TestExecutor
# =============================================================================

class TestExecutor:
    """Executes selected test commands safely inside Docker sandbox workspaces and collects metrics."""
    __test__ = False

    def __init__(self, workspace_manager: Optional[RepairWorkspaceManager] = None):
        self.ws_manager = workspace_manager or RepairWorkspaceManager()

    def execute_test(
        self,
        test: SelectedTest,
        workspace: RepairWorkspace,
        request: RepairRequest,
        verification_id: str,
        target_type: str,
        timeout_seconds: float = 60.0,
        limits: Optional[ExecutionLimits] = None,
    ) -> TestExecutionRecord:
        """Execute an individual SelectedTest inside DockerSandbox with timeout and metric extraction."""
        start_mono = time.monotonic()
        started_at = datetime.now(timezone.utc)
        exec_id = f"exec_{uuid.uuid4().hex[:12]}"

        try:
            sbx_result: SandboxResult = workspace.run_command_in_sandbox(
                command=test.command,
                cwd=test.relative_cwd,
                timeout_seconds=timeout_seconds,
                read_only=False,
                limits=limits,
                run_id=request.run_id,
            )
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            completed_at = datetime.now(timezone.utc)

            stdout_clean = redact_secrets(sbx_result.stdout)
            stderr_clean = redact_secrets(sbx_result.stderr)

            # Bound output excerpts
            stdout_excerpt = stdout_clean[:8192]
            stderr_excerpt = stderr_clean[:8192]

            metrics = parse_test_runner_output(stdout_clean, stderr_clean)
            exit_code = sbx_result.exit_code
            timed_out = sbx_result.timed_out

            # Classify execution status
            if timed_out:
                status = "timeout"
                passed = False
                failure_msg = f"Test command timed out after {timeout_seconds}s"
            elif metrics["collected"] == 0:
                # Prompt: "Do not interpret empty output or zero collected tests as success."
                status = "inconclusive"
                passed = False
                failure_msg = "Zero tests collected by runner"
            elif exit_code == 0 and metrics["failed"] == 0 and metrics["errored"] == 0 and metrics["passed"] > 0:
                status = "passed"
                passed = True
                failure_msg = None
            else:
                status = "failed"
                passed = False
                failure_msg = f"Test failed with exit code {exit_code} (failures: {metrics['failed']}, errors: {metrics['errored']})"

            return TestExecutionRecord(
                execution_id=exec_id,
                verification_id=verification_id,
                repair_id=request.repair_id,
                workspace_id=workspace.workspace_id,
                target_type=target_type,
                test_name=test.test_name,
                command=test.command,
                selection_rationale=test.rationale,
                exit_code=exit_code,
                status=status,
                passed=passed,
                timed_out=timed_out,
                duration_ms=duration_ms,
                stdout_excerpt=stdout_excerpt,
                stderr_excerpt=stderr_excerpt,
                tests_collected=metrics["collected"],
                tests_executed=metrics["executed"],
                tests_passed=metrics["passed"],
                tests_failed=metrics["failed"],
                tests_skipped=metrics["skipped"],
                tests_errored=metrics["errored"],
                failure_message=failure_msg,
                started_at=started_at,
                completed_at=completed_at,
            )

        except SandboxUnavailableError as e:
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            completed_at = datetime.now(timezone.utc)
            return TestExecutionRecord(
                execution_id=exec_id,
                verification_id=verification_id,
                repair_id=request.repair_id,
                workspace_id=workspace.workspace_id,
                target_type=target_type,
                test_name=test.test_name,
                command=test.command,
                selection_rationale=test.rationale,
                exit_code=None,
                status="error",
                passed=False,
                timed_out=False,
                duration_ms=duration_ms,
                stdout_excerpt="",
                stderr_excerpt=str(e),
                tests_collected=0,
                tests_executed=0,
                tests_passed=0,
                tests_failed=0,
                tests_skipped=0,
                tests_errored=0,
                failure_message=str(e),
                started_at=started_at,
                completed_at=completed_at,
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            completed_at = datetime.now(timezone.utc)
            return TestExecutionRecord(
                execution_id=exec_id,
                verification_id=verification_id,
                repair_id=request.repair_id,
                workspace_id=workspace.workspace_id,
                target_type=target_type,
                test_name=test.test_name,
                command=test.command,
                selection_rationale=test.rationale,
                exit_code=1,
                status="error",
                passed=False,
                timed_out=False,
                duration_ms=duration_ms,
                stdout_excerpt="",
                stderr_excerpt=str(e),
                tests_collected=0,
                tests_executed=0,
                tests_passed=0,
                tests_failed=0,
                tests_skipped=0,
                tests_errored=1,
                failure_message=str(e),
                started_at=started_at,
                completed_at=completed_at,
            )


# =============================================================================
# Component: BaselineComparator & RegressionAnalyzer
# =============================================================================

class BaselineComparator:
    """Compares baseline vs patched execution records to identify fixes and regressions."""

    def compare(
        self,
        baseline_records: List[TestExecutionRecord],
        patched_records: List[TestExecutionRecord],
    ) -> RegressionAnalysisRecord:
        """Analyze differences between baseline and patched execution records."""
        base_by_name = {r.test_name: r for r in baseline_records}
        patched_by_name = {r.test_name: r for r in patched_records}

        comparisons: List[TestComparisonResult] = []
        new_failures = 0
        fixed_tests = 0
        stable_tests = 0
        pre_existing = 0
        regression_detected = False
        defect_resolved = False

        for test_name, p_rec in patched_by_name.items():
            b_rec = base_by_name.get(test_name)

            if b_rec is None:
                # No baseline comparison available
                comp_status = ComparisonStatus.INCONCLUSIVE
                details = "Baseline execution record not available for comparison"
            elif p_rec.timed_out or p_rec.status in ("timeout", "error"):
                comp_status = ComparisonStatus.NEW_ERROR
                details = f"Test encountered error or timeout on patched workspace: {p_rec.failure_message}"
                new_failures += 1
                regression_detected = True
            elif b_rec.passed and not p_rec.passed:
                comp_status = ComparisonStatus.NEW_REGRESSION
                details = f"Regression: test passed on baseline but failed on patched workspace (exit code: {p_rec.exit_code})"
                new_failures += 1
                regression_detected = True
            elif not b_rec.passed and p_rec.passed:
                comp_status = ComparisonStatus.FIXED
                details = "Defect resolved: test failed on baseline revision but passed on patched workspace"
                fixed_tests += 1
                defect_resolved = True
            elif b_rec.passed and p_rec.passed:
                comp_status = ComparisonStatus.STABLE_PASS
                details = "Test passed on both baseline and patched workspace"
                stable_tests += 1
            else:
                comp_status = ComparisonStatus.PRE_EXISTING_FAILURE
                details = "Pre-existing failure: test failed on both baseline and patched workspace"
                pre_existing += 1

            comparisons.append(
                TestComparisonResult(
                    test_name=test_name,
                    status=comp_status,
                    baseline_passed=b_rec.passed if b_rec else None,
                    patched_passed=p_rec.passed,
                    details=details,
                )
            )

        # High-level summary
        if regression_detected:
            summary = f"REGRESSION DETECTED: {new_failures} newly failing test(s) introduced by patch"
        elif defect_resolved:
            summary = f"REPAIR VERIFIED: defect resolved ({fixed_tests} fixed test(s), {stable_tests} stable passes, 0 regressions)"
        elif all(c.status == ComparisonStatus.STABLE_PASS for c in comparisons) and comparisons:
            summary = f"ALL TESTS PASSED: {stable_tests} test(s) passed, 0 regressions"
        else:
            summary = f"INCONCLUSIVE: {len(comparisons)} test(s) compared without conclusive resolution"

        return RegressionAnalysisRecord(
            regression_detected=regression_detected,
            defect_resolved=defect_resolved,
            comparisons=comparisons,
            new_failures_count=new_failures,
            fixed_tests_count=fixed_tests,
            stable_tests_count=stable_tests,
            pre_existing_failures_count=pre_existing,
            summary=summary,
        )


# =============================================================================
# Component: VerificationEvidenceCollector
# =============================================================================

class VerificationEvidenceCollector:
    """Gathers and persists structured verification evidence in the FORGE evidence store."""

    def __init__(self, forge_store: Optional[ForgeStore] = None):
        self.forge_store = forge_store

    def collect_and_attach(
        self,
        request: RepairRequest,
        patch: PatchArtifact,
        run_record: VerificationRunRecord,
    ) -> List[str]:
        """Create and attach verification EvidenceRecord objects to the run/finding."""
        if not self.forge_store:
            return []

        evidence_ids: List[str] = []

        summary_content = (
            f"FORGE Patch Verification Report for Repair {request.repair_id}\n"
            f"Outcome: {run_record.overall_outcome.value}\n"
            f"Patch Artifact: {patch.artifact_id} (SHA256: {patch.content_hash})\n"
            f"Regression Analysis: {run_record.regression_analysis.summary}\n"
            f"Executions: {len(run_record.patched_executions)} patched, {len(run_record.baseline_executions)} baseline\n"
        )

        ev_id = f"evi_verif_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        record = EvidenceRecord(
            evidence_id=ev_id,
            run_id=request.run_id or f"run_repair_{request.repair_id}",
            finding_id=request.finding_id,
            evidence_type=EvidenceType.VERIFICATION_RESULT,
            description=f"Sandbox verification executed for repair {request.repair_id} ({run_record.overall_outcome.value})",
            content=redact_secrets(summary_content),
            file_path=patch.target_file,
            details={
                "verification_id": run_record.verification_id,
                "overall_outcome": run_record.overall_outcome.value,
                "patch_artifact_id": patch.artifact_id,
                "patch_sha256": patch.content_hash,
                "regression_detected": run_record.regression_analysis.regression_detected,
                "defect_resolved": run_record.regression_analysis.defect_resolved,
                "fixed_tests_count": run_record.regression_analysis.fixed_tests_count,
                "new_failures_count": run_record.regression_analysis.new_failures_count,
                "duration_ms": run_record.duration_ms,
            },
            created_at=now,
        )

        try:
            self.forge_store.attach_evidence(record)
            evidence_ids.append(ev_id)
        except Exception:
            pass

        return evidence_ids


# =============================================================================
# Component: VerificationReportBuilder
# =============================================================================

class VerificationReportBuilder:
    """Builds structured Markdown and JSON reports from VerificationRunRecord."""

    @staticmethod
    def build_markdown_report(run: VerificationRunRecord) -> str:
        """Produce a human-readable and auditable Markdown verification report."""
        lines = [
            f"# FORGE Sandbox Repair Verification Report: {run.verification_id}",
            "",
            "## 1. Executive Summary",
            f"- **Overall Outcome**: `{run.overall_outcome.value}`",
            f"- **Repair Request ID**: `{run.repair_id}`",
            f"- **Patch Artifact ID**: `{run.patch_artifact_id}`",
            f"- **Patch Digest (SHA-256)**: `{run.patch_sha256}`",
            f"- **Repository Target**: `{run.repository_target}`",
            f"- **Base Commit SHA**: `{run.base_commit_sha}`",
            f"- **Patched Workspace**: `{run.workspace_id}`",
            f"- **Baseline Workspace**: `{run.baseline_workspace_id or 'N/A'}`",
            f"- **Verification Duration**: `{run.duration_ms:.1f}ms`",
            "",
            "## 2. Regression & Defect Resolution Analysis",
            f"**Summary**: {run.regression_analysis.summary}",
            "",
            "| Test Name | Status | Baseline Result | Patched Result | Details |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]

        for c in run.regression_analysis.comparisons:
            b_str = "PASS" if c.baseline_passed is True else ("FAIL" if c.baseline_passed is False else "N/A")
            p_str = "PASS" if c.patched_passed is True else ("FAIL" if c.patched_passed is False else "N/A")
            lines.append(f"| `{c.test_name}` | `{c.status.value}` | `{b_str}` | `{p_str}` | {c.details} |")

        lines.extend([
            "",
            "## 3. Test Selection Rationale",
            "| Test Identifier | Category | Command | Selection Rationale |",
            "| :--- | :--- | :--- | :--- |",
        ])

        for t in run.plan.selected_tests:
            cmd_str = " ".join(t.command)
            lines.append(f"| `{t.test_name}` | `{t.category.value}` | `{cmd_str}` | {t.rationale} |")

        if run.failure_message:
            lines.extend([
                "",
                "## 4. Diagnostics & Failure Details",
                f"- **Failure Category**: `{run.failure_category.value if run.failure_category else 'N/A'}`",
                f"- **Diagnostic Message**: {run.failure_message}",
            ])

        lines.extend([
            "",
            "## 5. Execution Logs Excerpt",
            "```text",
            run.logs_excerpt or "(No log output recorded)",
            "```",
            "",
        ])

        return "\n".join(lines)

    @staticmethod
    def build_dict_report(run: VerificationRunRecord) -> Dict[str, Any]:
        """Produce structured JSON-serializable verification report."""
        return run.model_dump()


# =============================================================================
# Component: VerificationPlanner
# =============================================================================

class VerificationPlanner:
    """Formulates the VerificationPlan from request, finding, evidence, and workspace structure."""

    def __init__(self, selector: Optional[TestSelector] = None):
        self.selector = selector or TestSelector()

    def formulate_plan(
        self,
        request: RepairRequest,
        patch: PatchArtifact,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        repair_plan: Optional[RepairExecutionPlan] = None,
        timeout_seconds: float = 60.0,
    ) -> VerificationPlan:
        """Formulate a VerificationPlan containing vetted tests and execution parameters."""
        selected_tests = self.selector.select_tests(
            request=request,
            patch=patch,
            workspace=workspace,
            policy=policy,
            finding=finding,
            evidence_records=evidence_records,
            plan=repair_plan,
        )

        return VerificationPlan(
            plan_id=f"vplan_{uuid.uuid4().hex[:12]}",
            repair_id=request.repair_id,
            patch_artifact_id=patch.artifact_id,
            selected_tests=selected_tests,
            timeout_seconds=timeout_seconds,
            created_at=datetime.now(timezone.utc),
        )


# =============================================================================
# Main Verification Subsystem: RepairVerifier
# =============================================================================

class RepairVerifier(RepairVerifierProtocol):
    """Production-quality isolated verification engine for candidate software repairs."""

    def __init__(
        self,
        execution_store: Optional[RepairExecutionStore] = None,
        forge_store: Optional[ForgeStore] = None,
        workspace_manager: Optional[RepairWorkspaceManager] = None,
        planner: Optional[VerificationPlanner] = None,
        executor: Optional[TestExecutor] = None,
        comparator: Optional[BaselineComparator] = None,
        evidence_collector: Optional[VerificationEvidenceCollector] = None,
        db_path: Optional[Path] = None,
    ):
        self.exec_store = execution_store or RepairExecutionStore(db_path=db_path)
        self.forge_store = forge_store or ForgeStore(db_path=db_path)
        self.ws_manager = workspace_manager or RepairWorkspaceManager(store=self.exec_store)
        self.planner = planner or VerificationPlanner()
        self.executor = executor or TestExecutor(workspace_manager=self.ws_manager)
        self.comparator = comparator or BaselineComparator()
        self.evidence_collector = evidence_collector or VerificationEvidenceCollector(forge_store=self.forge_store)

    # -------------------------------------------------------------------------
    # Verification Preconditions Enforcement
    # -------------------------------------------------------------------------

    def verify_preconditions(
        self,
        request: RepairRequest,
        patch: PatchArtifact,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
        validation_result: Optional[IndependentValidationResult] = None,
        finding: Optional[Finding] = None,
    ) -> IndependentValidationResult:
        """Verify that all mandatory provenance, integrity, and safety gates pass before execution.

        Raises:
            VerificationPreconditionError: If preconditions fail.
            PatchIntegrityError: If patch digest or provenance fails.
            TargetRepoImmutabilityViolationError: If target repository was touched.
            WorkspaceIntegrityViolationError: If workspace was tampered with.
        """
        # 1. Base commit and repository matching
        if request.base_commit_sha.lower() != patch.base_commit_sha.lower():
            raise PatchIntegrityError(
                f"Base commit mismatch: request has '{request.base_commit_sha}', "
                f"patch has '{patch.base_commit_sha}'"
            )

        if workspace.base_commit_sha.lower() != request.base_commit_sha.lower():
            raise PatchIntegrityError(
                f"Workspace base commit mismatch: workspace has '{workspace.base_commit_sha}', "
                f"request has '{request.base_commit_sha}'"
            )

        # 2. Workspace identity
        if request.workspace_id and request.workspace_id != workspace.workspace_id:
            raise PatchIntegrityError(
                f"Workspace identity mismatch: request assigned '{request.workspace_id}', "
                f"provided '{workspace.workspace_id}'"
            )

        # 3. Patch artifact content hash recalculation
        clean_diff = patch.diff_content.strip() + "\n"
        recalc_hash = hashlib.sha256(clean_diff.encode("utf-8")).hexdigest()
        if recalc_hash != patch.content_hash:
            raise PatchIntegrityError(
                f"Patch artifact content hash mismatch: claimed '{patch.content_hash}', "
                f"recalculated '{recalc_hash}'"
            )

        # 4. Independent M4 validation outcome verification
        val_res = validation_result
        if not val_res:
            val_res = self.exec_store.get_validation_result_for_patch(patch.artifact_id)

        if not val_res:
            raise VerificationPreconditionError(
                f"No independent M4 validation result found for patch '{patch.artifact_id}'. "
                "Candidate patches must be validated before sandbox verification."
            )

        if val_res.overall_status != ValidationOutcome.ACCEPTED:
            raise VerificationPreconditionError(
                f"Patch '{patch.artifact_id}' was not accepted by M4 validation "
                f"(status: '{val_res.overall_status.value}'). Cannot verify unaccepted patches."
            )

        if val_res.patch_sha256 != patch.content_hash:
            raise PatchIntegrityError(
                f"Validation digest mismatch: M4 validated '{val_res.patch_sha256}', "
                f"patch has '{patch.content_hash}'"
            )

        # 5. Target repository immutability
        if not workspace.check_target_immutability():
            raise TargetRepoImmutabilityViolationError(
                "Target repository modification detected! Verification aborted."
            )

        # 6. Workspace containment
        try:
            workspace.validate_containment(patch.target_file)
        except ScopeContainmentViolationError as e:
            raise ScopeContainmentViolationError(
                f"Patch target file '{patch.target_file}' escapes workspace containment: {e}"
            )

        return val_res

    # -------------------------------------------------------------------------
    # Core Verification Engine
    # -------------------------------------------------------------------------

    def verify_repair(
        self,
        request: RepairRequest,
        patch: PatchArtifact,
        workspace: RepairWorkspace,
        policy: Optional[RepairExecutionPolicy] = None,
        validation_result: Optional[IndependentValidationResult] = None,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        repair_plan: Optional[RepairExecutionPlan] = None,
        baseline_workspace: Optional[RepairWorkspace] = None,
        scratch_root: Optional[Union[str, Path]] = None,
        timeout_seconds: float = 60.0,
    ) -> VerificationRunRecord:
        """Execute the end-to-end sandbox verification and regression testing workflow.

        Guarantees:
        - Strict precondition gate.
        - Fail-closed refusal if Docker is unavailable (never runs on host).
        - Baseline vs Patched dual execution.
        - Itemized regression analysis.
        - SQLite persistence and audit event ledger.
        """
        start_mono = time.monotonic()
        started_at = datetime.now(timezone.utc)
        verification_id = f"verif_{uuid.uuid4().hex[:12]}"
        active_policy = policy or RepairExecutionPolicy()

        # Step 1: Preconditions Gate
        val_res = self.verify_preconditions(
            request=request,
            patch=patch,
            workspace=workspace,
            policy=active_policy,
            validation_result=validation_result,
            finding=finding,
        )

        # Auto-load finding, evidence, and plan if not explicitly passed
        effective_finding = finding
        if effective_finding is None and self.forge_store and request.finding_id:
            effective_finding = self.forge_store.get_finding(request.finding_id)

        effective_evidence = evidence_records
        if effective_evidence is None and self.forge_store and request.finding_id:
            effective_evidence = self.forge_store.list_evidence_for_finding(request.finding_id)

        effective_repair_plan = repair_plan
        if effective_repair_plan is None and self.exec_store:
            effective_repair_plan = self.exec_store.get_repair_plan_for_repair(request.repair_id)

        # Step 2: Formulate Verification Plan
        vplan = self.planner.formulate_plan(
            request=request,
            patch=patch,
            workspace=workspace,
            policy=active_policy,
            finding=effective_finding,
            evidence_records=effective_evidence,
            repair_plan=effective_repair_plan,
            timeout_seconds=timeout_seconds,
        )

        # Step 3: Check Docker Sandbox Availability (Fail-Closed)
        docker_sbx = DockerSandbox()
        docker_available = docker_sbx.is_available()

        if not docker_available:
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            completed_at = datetime.now(timezone.utc)
            fail_msg = (
                "Docker container sandbox is unavailable. FORGE fail-closed policy "
                "strictly refuses to execute target repository code directly on host."
            )
            run_rec = VerificationRunRecord(
                verification_id=verification_id,
                repair_id=request.repair_id,
                patch_artifact_id=patch.artifact_id,
                validation_id=val_res.validation_id,
                repository_target=str(request.target_repo),
                base_commit_sha=request.base_commit_sha,
                workspace_id=workspace.workspace_id,
                baseline_workspace_id=None,
                patch_sha256=patch.content_hash,
                plan=vplan,
                baseline_executions=[],
                patched_executions=[],
                regression_analysis=RegressionAnalysisRecord(
                    regression_detected=False,
                    defect_resolved=False,
                    comparisons=[],
                    summary=fail_msg,
                ),
                overall_outcome=VerificationOutcome.BLOCKED,
                failure_category=FailureCategory.SANDBOX_UNAVAILABLE,
                failure_message=fail_msg,
                evidence_ids=[],
                logs_excerpt=fail_msg,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
            )
            self.exec_store.save_verification_run(run_rec)
            return run_rec

        # Step 4: Provision Baseline Workspace if not provided
        b_workspace = baseline_workspace
        managed_baseline = False
        if b_workspace is None:
            try:
                b_workspace = RepairWorkspace(
                    target_repo_path=request.target_repo,
                    base_commit_sha=request.base_commit_sha,
                    scratch_root=scratch_root,
                    policy=active_policy,
                )
                b_workspace.prepare()
                managed_baseline = True
            except Exception as b_err:
                # If baseline workspace cannot be provisioned, record limitation
                b_workspace = None

        baseline_executions: List[TestExecutionRecord] = []
        patched_executions: List[TestExecutionRecord] = []
        logs_fragments: List[str] = []

        try:
            # Step 5: Execute Baseline Tests
            if b_workspace is not None:
                for test in vplan.selected_tests:
                    b_rec = self.executor.execute_test(
                        test=test,
                        workspace=b_workspace,
                        request=request,
                        verification_id=verification_id,
                        target_type="baseline",
                        timeout_seconds=timeout_seconds,
                    )
                    baseline_executions.append(b_rec)
                    logs_fragments.append(
                        f"[BASELINE {b_rec.test_name}] status={b_rec.status} exit={b_rec.exit_code}\n"
                        f"{b_rec.stdout_excerpt}\n{b_rec.stderr_excerpt}"
                    )

            # Step 6: Execute Patched Tests
            for test in vplan.selected_tests:
                p_rec = self.executor.execute_test(
                    test=test,
                    workspace=workspace,
                    request=request,
                    verification_id=verification_id,
                    target_type="patched",
                    timeout_seconds=timeout_seconds,
                )
                patched_executions.append(p_rec)
                logs_fragments.append(
                    f"[PATCHED {p_rec.test_name}] status={p_rec.status} exit={p_rec.exit_code}\n"
                    f"{p_rec.stdout_excerpt}\n{p_rec.stderr_excerpt}"
                )

        finally:
            if managed_baseline and b_workspace is not None:
                try:
                    b_workspace.cleanup()
                except Exception:
                    pass

        # Step 7: Perform Comparative Regression Analysis
        reg_analysis = self.comparator.compare(
            baseline_records=baseline_executions,
            patched_records=patched_executions,
        )

        duration_ms = (time.monotonic() - start_mono) * 1000.0
        completed_at = datetime.now(timezone.utc)
        combined_logs = "\n".join(logs_fragments)

        # Step 8: Determine Verification Outcome
        overall_outcome, fail_cat, fail_msg = self._classify_verification_outcome(
            vplan=vplan,
            baseline_executions=baseline_executions,
            patched_executions=patched_executions,
            regression_analysis=reg_analysis,
        )

        run_rec = VerificationRunRecord(
            verification_id=verification_id,
            repair_id=request.repair_id,
            patch_artifact_id=patch.artifact_id,
            validation_id=val_res.validation_id,
            repository_target=str(request.target_repo),
            base_commit_sha=request.base_commit_sha,
            workspace_id=workspace.workspace_id,
            baseline_workspace_id=b_workspace.workspace_id if b_workspace else None,
            patch_sha256=patch.content_hash,
            plan=vplan,
            baseline_executions=baseline_executions,
            patched_executions=patched_executions,
            regression_analysis=reg_analysis,
            overall_outcome=overall_outcome,
            failure_category=fail_cat,
            failure_message=fail_msg,
            evidence_ids=[],
            logs_excerpt=combined_logs[:16384],
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )

        # Step 9: Collect Evidence Records & Persist
        evidence_ids = self.evidence_collector.collect_and_attach(
            request=request,
            patch=patch,
            run_record=run_rec,
        )
        if evidence_ids:
            run_rec = run_rec.model_copy(update={"evidence_ids": evidence_ids})

        self.exec_store.save_verification_run(run_rec)

        # Step 10: Record Sequential Audit Event
        self.exec_store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{uuid.uuid4().hex[:12]}",
                repair_id=request.repair_id,
                sequence=1,
                from_state=RepairExecutionState.PATCH_VALIDATING,
                to_state=RepairExecutionState.PATCH_VALIDATING,
                actor=RepairActor.VERIFIER,
                payload={
                    "action": "sandbox_verification_completed",
                    "verification_id": verification_id,
                    "overall_outcome": overall_outcome.value,
                    "regression_detected": reg_analysis.regression_detected,
                    "defect_resolved": reg_analysis.defect_resolved,
                    "duration_ms": duration_ms,
                    "failure_category": fail_cat.value if fail_cat else None,
                    "failure_message": fail_msg,
                },
                timestamp=completed_at,
            )
        )

        return run_rec

    def _classify_verification_outcome(
        self,
        vplan: VerificationPlan,
        baseline_executions: List[TestExecutionRecord],
        patched_executions: List[TestExecutionRecord],
        regression_analysis: RegressionAnalysisRecord,
    ) -> Tuple[VerificationOutcome, Optional[FailureCategory], Optional[str]]:
        """Determine explicit VerificationOutcome adhering strictly to policy rules."""
        if not patched_executions:
            return (
                VerificationOutcome.INCONCLUSIVE,
                FailureCategory.INTERNAL_ERROR,
                "No tests were selected or executed for verification.",
            )

        # 1. Timeout Check
        if any(p.timed_out for p in patched_executions):
            return (
                VerificationOutcome.FAILED,
                FailureCategory.TIMEOUT,
                "Test execution timed out during sandboxed verification.",
            )

        # 2. Regression Check
        if regression_analysis.regression_detected:
            return (
                VerificationOutcome.FAILED,
                FailureCategory.REGRESSION_DETECTED,
                f"Patch introduced regressions: {regression_analysis.new_failures_count} test(s) failed.",
            )

        # 3. Collection failure or zero collected tests
        # Prompt: "Do not interpret empty output or zero collected tests as success."
        all_zero_collected = all(p.tests_collected == 0 for p in patched_executions)
        if all_zero_collected:
            return (
                VerificationOutcome.INCONCLUSIVE,
                FailureCategory.INTERNAL_ERROR,
                "Zero tests were collected or discovered during execution.",
            )

        # 4. Check defect reproduction tests
        repro_tests = [t for t in vplan.selected_tests if t.expected_to_fail_on_baseline or t.category == TestSelectionCategory.REPRODUCTION]
        if repro_tests:
            repro_names = {t.test_name for t in repro_tests}
            for p in patched_executions:
                if p.test_name in repro_names and not p.passed:
                    return (
                        VerificationOutcome.FAILED,
                        FailureCategory.TEST_FAILED,
                        f"Original defect reproduction check '{p.test_name}' failed on patched code.",
                    )

        # 5. Check if all patched tests passed
        if all(p.passed for p in patched_executions):
            # Prompt: "VERIFIED allowed only when: required preconditions pass, independent patch validation accepted,
            # relevant tests genuinely execute, required defect-reproduction checks pass, regression checks meet defined policy"
            return (VerificationOutcome.VERIFIED, None, None)

        # 6. Any other failure in patched tests
        failing_p = [p.test_name for p in patched_executions if not p.passed]
        return (
            VerificationOutcome.FAILED,
            FailureCategory.TEST_FAILED,
            f"Tests failed on patched workspace: {', '.join(failing_p)}",
        )

    # -------------------------------------------------------------------------
    # Protocol Compatibility Method
    # -------------------------------------------------------------------------

    def verify_repair_protocol(
        self,
        patch: PatchArtifact,
        plan: RepairExecutionPlan,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
    ) -> Tuple[bool, List[ExecutionTestResult], Optional[str]]:
        """Satisfies the RepairVerifierProtocol defined in interfaces.py."""
        # Find request for repair_id
        req = self.exec_store.get_repair_request(patch.repair_id)
        if not req:
            return (False, [], f"Repair request '{patch.repair_id}' not found")

        try:
            ws = workspace if isinstance(workspace, RepairWorkspace) else RepairWorkspace(
                target_repo_path=req.target_repo,
                base_commit_sha=req.base_commit_sha,
                policy=policy,
            )
            run = self.verify_repair(
                request=req,
                patch=patch,
                workspace=ws,
                policy=policy,
                repair_plan=plan,
            )
            test_results = [
                ExecutionTestResult(
                    test_name=p.test_name,
                    passed=p.passed,
                    duration_ms=p.duration_ms,
                    output_excerpt=p.stdout_excerpt[:500],
                    failure_message=p.failure_message,
                )
                for p in run.patched_executions
            ]
            passed = (run.overall_outcome == VerificationOutcome.VERIFIED)
            return (passed, test_results, run.failure_message)
        except Exception as e:
            return (False, [], str(e))
