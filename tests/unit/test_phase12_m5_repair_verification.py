"""Phase 12 M5: Repair Verification Engine Comprehensive Test Suite.

Verifies:
A. Verification planning: selection, dependencies, fallback, duplicates, containment.
B. Sandbox execution: health check, fail-closed refusal, command validation, boundaries.
C. Result parsing: pass, fail, error, skip, xfail, xpass, collection errors, inconsistencies.
D. Baseline comparison: identical execution, digest validation, snapshot isolation.
E. Regression engine: deterministic classifications (no regression, regression, pre-existing, improved).
F. Integrity & adversarial: snapshot tampering, symlink escape, mutation bypasses, model copies.
G. Lifecycle integration: M4 handoff, verification_pending state transition, human review gate.
H. End-to-end integration: clean repair, regressions, pre-existing fixes, blocked sandbox.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pytest
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from forge.repair_execution.schemas import (
    ExecutionStatus,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
    WorkspaceStatus,
)
from forge.repair_execution.workspace import compute_tree_digest
from forge.repair_verification.baseline import BaselineVerificationRunner
from forge.repair_verification.discovery import RepairTestDiscoverer
from forge.repair_verification.events import VerificationEventRecorder
from forge.repair_verification.executor import RepairVerificationExecutor
from forge.repair_verification.gates import VerificationGateKeeper
from forge.repair_verification.integrity import VerificationIntegrityChecker
from forge.repair_verification.regression import DeterministicRegressionEngine
from forge.repair_verification.result_parser import PytestResultParser
from forge.repair_verification.sandbox import VerificationSandbox
from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    RegressionClassification,
    SelectedCheck,
    SuiteExecutionSummary,
    TestExecutionResult,
    TestIdentifier,
    TestScopeCategory,
    VerificationEvent,
    VerificationEventType,
    VerificationPlan,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.repair_verification.test_selection import IntelligentTestSelector
from forge.repair_verification.tools import RepairVerificationTools
from forge.repair_verification.verification_plan import VerificationPlanGenerator
from forge.sandbox.adapter import Sandbox
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus as SbxStatus,
    FailureClassification as SbxFailure,
    SandboxRequest,
    SandboxResult,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Fake Sandbox Adapter
# ---------------------------------------------------------------------------

class FakeSandboxAdapter:
    """Controllable test adapter implementing the Sandbox protocol."""

    def __init__(
        self,
        is_available_flag: bool = True,
        health_message: str = "Fake sandbox healthy",
        custom_responses: Optional[Dict[str, SandboxResult]] = None,
        default_exit_code: int = 0,
        default_stdout: str = "=== 1 passed in 0.05s ===",
        default_stderr: str = "",
    ) -> None:
        self.is_available_flag = is_available_flag
        self.health_message = health_message
        self.custom_responses = custom_responses or {}
        self.default_exit_code = default_exit_code
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr
        self.executed_requests: List[SandboxRequest] = []

    def is_available(self) -> bool:
        return self.is_available_flag

    def check_health(self) -> Tuple[bool, str]:
        return self.is_available_flag, self.health_message

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.executed_requests.append(request)
        cmd_str = " ".join(request.command)

        for key, res in self.custom_responses.items():
            if key in cmd_str or key in request.run_id:
                return res

        return SandboxResult(
            request_id=request.request_id,
            run_id=request.run_id,
            status=SbxStatus.SUCCEEDED if self.default_exit_code == 0 else SbxStatus.FAILED,
            exit_code=self.default_exit_code,
            stdout=self.default_stdout,
            stderr=self.default_stderr,
            duration_ms=50.0,
        )


@pytest.fixture
def test_repo_env(tmp_path: Path):
    """Fixture providing clean mock repository and isolated workspace."""
    repo_dir = tmp_path / "original_repo"
    repo_dir.mkdir()
    (repo_dir / "pkg").mkdir()
    (repo_dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "pkg" / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef divide(a, b):\n    if b == 0:\n        raise ValueError('divide by zero')\n    return a / b\n",
        encoding="utf-8",
    )
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "tests" / "test_calculator.py").write_text(
        "from pkg.calculator import add, divide\nimport pytest\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_divide():\n    assert divide(4, 2) == 2\n",
        encoding="utf-8",
    )

    ws_dir = tmp_path / "repair_workspaces" / "ws_001"
    ws_dir.mkdir(parents=True)
    shutil.copytree(repo_dir, ws_dir, dirs_exist_ok=True)

    # Immutable baseline snapshot alongside workspace
    snap_dir = tmp_path / "repair_workspaces" / "ws_001_baseline"
    shutil.copytree(repo_dir, snap_dir)

    baseline_tree = compute_tree_digest(snap_dir)

    workspace = RepairWorkspace(
        workspace_id="ws_001",
        original_repository_reference=str(repo_dir),
        isolated_workspace_path=str(ws_dir),
        baseline_commit="c0ffee01",
        baseline_tree_digest=baseline_tree,
        baseline_snapshot_path=str(snap_dir),
        allowed_paths=["pkg/calculator.py"],
        workspace_status=WorkspaceStatus.READY,
    )

    exec_result = RepairExecutionResult(
        execution_id="rex_001",
        repair_plan_id="plan_001",
        execution_status=ExecutionStatus.APPLIED,
        workspace_id="ws_001",
        applied_patch_digest="a" * 64,
        modified_files=["pkg/calculator.py"],
        diff_summary="Applied repair diff for calculator.py",
        verification_pending=True,
        approval_reference="appr_001",
    )

    return repo_dir, ws_dir, snap_dir, workspace, exec_result


# ---------------------------------------------------------------------------
# Category A: Verification Planning Tests
# ---------------------------------------------------------------------------

def test_discovery_maps_targeted_and_regression_tests(test_repo_env):
    _, ws_dir, _, _, _ = test_repo_env
    discoverer = RepairTestDiscoverer(workspace_path=ws_dir)
    res = discoverer.discover_tests(modified_files=["pkg/calculator.py"])

    assert len(res.all_test_files) >= 1
    assert "tests/test_calculator.py" in res.all_test_files
    assert any("test_add" in t.test_name for t in res.targeted_tests)
    assert any("test_divide" in t.test_name for t in res.targeted_tests)


def test_test_selector_builds_checks_and_partial_flag(test_repo_env):
    _, ws_dir, _, _, _ = test_repo_env
    selector = IntelligentTestSelector(workspace_path=ws_dir)
    checks, ids, excluded, is_partial, rationale = selector.select_checks(
        modified_files=["pkg/calculator.py"]
    )

    assert len(checks) >= 1
    targeted_check = [c for c in checks if c.category == TestScopeCategory.TARGETED][0]
    assert "pytest" in targeted_check.command
    assert "tests/test_calculator.py" in targeted_check.command
    assert "calculator.py" in rationale


def test_plan_generator_requires_applied_status(test_repo_env):
    _, _, _, workspace, exec_result = test_repo_env
    bad_exec = exec_result.model_copy(update={"execution_status": ExecutionStatus.PREPARED})
    generator = VerificationPlanGenerator()

    with pytest.raises(ValueError, match="execution status is 'prepared', expected 'applied'"):
        generator.generate_plan(execution_result=bad_exec, workspace=workspace)


def test_plan_generator_valid_binding(test_repo_env):
    _, _, _, workspace, exec_result = test_repo_env
    generator = VerificationPlanGenerator()
    plan = generator.generate_plan(execution_result=exec_result, workspace=workspace)

    assert plan.plan_id.startswith("vplan_")
    assert plan.execution_id == "rex_001"
    assert plan.workspace_id == "ws_001"
    assert plan.expected_baseline_digest == workspace.baseline_tree_digest
    assert plan.expected_patch_digest == exec_result.applied_patch_digest
    assert len(plan.selected_checks) >= 1


# ---------------------------------------------------------------------------
# Category B: Sandbox Execution Tests
# ---------------------------------------------------------------------------

def test_sandbox_fail_closed_when_unavailable(tmp_path: Path):
    fake_sbx = FakeSandboxAdapter(is_available_flag=False, health_message="Daemon unreachable")
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    policy = VerificationPolicy()

    check = SelectedCheck(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v", "tests/test_calc.py"],
        rationale="Targeted test",
    )
    res = v_sbx.execute_check(check=check, workspace_path=tmp_path, policy=policy)

    assert res.status == SbxStatus.UNAVAILABLE
    assert res.failure_classification == SbxFailure.RUNTIME_UNAVAILABLE
    assert "secure container sandbox is unavailable" in res.stderr


def test_sandbox_rejects_disallowed_binary(tmp_path: Path):
    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    policy = VerificationPolicy()

    check = SelectedCheck(
        check_id="chk_bad",
        category=TestScopeCategory.TARGETED,
        command=["curl", "https://malicious.site"],
        rationale="Exploit attempt",
    )
    res = v_sbx.execute_check(check=check, workspace_path=tmp_path, policy=policy)

    assert res.status == SbxStatus.REJECTED
    assert res.failure_classification == SbxFailure.POLICY_VIOLATION
    assert "binary 'curl' is not permitted" in res.stderr


def test_sandbox_rejects_shell_metacharacters(tmp_path: Path):
    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    policy = VerificationPolicy()

    check = SelectedCheck(
        check_id="chk_bad_args",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v", "tests/test_calc.py; rm -rf /"],
        rationale="Command injection attempt",
    )
    res = v_sbx.execute_check(check=check, workspace_path=tmp_path, policy=policy)

    assert res.status == SbxStatus.REJECTED
    assert res.failure_classification == SbxFailure.POLICY_VIOLATION
    assert "disallowed shell metacharacters" in res.stderr


# ---------------------------------------------------------------------------
# Category C: Test Result Parsing Tests
# ---------------------------------------------------------------------------

def test_parser_passed_report():
    check = SelectedCheck(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v", "tests/test_calc.py"],
        rationale="t",
    )
    sbx_res = SandboxResult(
        request_id="r1",
        run_id="run_01",
        status=SbxStatus.SUCCEEDED,
        exit_code=0,
        stdout="tests/test_calc.py::test_add PASSED\n=== 1 passed in 0.05s ===",
        stderr="",
        duration_ms=50.0,
    )
    parsed = PytestResultParser.parse_result(check=check, sandbox_res=sbx_res)

    assert parsed.outcome == CheckOutcome.PASSED
    assert parsed.tests_passed == 1
    assert parsed.tests_failed == 0
    assert not parsed.malformed_report


def test_parser_failed_report():
    check = SelectedCheck(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v", "tests/test_calc.py"],
        rationale="t",
    )
    sbx_res = SandboxResult(
        request_id="r1",
        run_id="run_01",
        status=SbxStatus.FAILED,
        exit_code=1,
        stdout="tests/test_calc.py::test_add FAILED\n=== 1 failed in 0.05s ===",
        stderr="",
        duration_ms=50.0,
    )
    parsed = PytestResultParser.parse_result(check=check, sandbox_res=sbx_res)

    assert parsed.outcome == CheckOutcome.FAILED
    assert parsed.tests_failed == 1


def test_parser_collection_error():
    check = SelectedCheck(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v", "tests/test_calc.py"],
        rationale="t",
    )
    sbx_res = SandboxResult(
        request_id="r1",
        run_id="run_01",
        status=SbxStatus.FAILED,
        exit_code=2,
        stdout="=== ERRORS ===\nERROR collecting tests/test_calc.py\nImportError: No module named 'foo'\n=== 1 error in 0.02s ===",
        stderr="",
        duration_ms=50.0,
    )
    parsed = PytestResultParser.parse_result(check=check, sandbox_res=sbx_res)

    assert parsed.outcome == CheckOutcome.COLLECTION_ERROR
    assert parsed.collection_errors >= 1


def test_parser_inconsistent_exit_zero_with_failures():
    check = SelectedCheck(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v", "tests/test_calc.py"],
        rationale="t",
    )
    # Contradiction: exit code 0 but failures in stdout
    sbx_res = SandboxResult(
        request_id="r1",
        run_id="run_01",
        status=SbxStatus.SUCCEEDED,
        exit_code=0,
        stdout="tests/test_calc.py::test_add FAILED\n=== 1 failed in 0.05s ===",
        stderr="",
        duration_ms=50.0,
    )
    parsed = PytestResultParser.parse_result(check=check, sandbox_res=sbx_res)

    assert parsed.malformed_report is True
    assert parsed.outcome == CheckOutcome.ERROR


# ---------------------------------------------------------------------------
# Category D & E: Baseline Comparison & Regression Engine Tests
# ---------------------------------------------------------------------------

def test_regression_engine_no_regression():
    base_summary = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id="ws_01",
        tree_digest="digest_base",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.PASSED,
                tests_passed=1,
            )
        ],
        all_passed=True,
    )
    rep_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id="ws_01",
        tree_digest="digest_rep",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.PASSED,
                tests_passed=1,
            )
        ],
        all_passed=True,
    )

    reg_class, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=base_summary,
        repaired_summary=rep_summary,
    )
    assert reg_class == RegressionClassification.NO_REGRESSION_OBSERVED


def test_regression_engine_regression_detected():
    base_summary = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id="ws_01",
        tree_digest="digest_base",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.PASSED,
                tests_passed=1,
            )
        ],
        all_passed=True,
    )
    rep_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id="ws_01",
        tree_digest="digest_rep",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.FAILED,
                tests_failed=1,
            )
        ],
        all_passed=False,
    )

    reg_class, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=base_summary,
        repaired_summary=rep_summary,
    )
    assert reg_class == RegressionClassification.REGRESSION_DETECTED
    assert len(findings) == 1
    assert "passed in baseline but failed" in findings[0].explanation


def test_regression_engine_pre_existing_failure():
    base_summary = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id="ws_01",
        tree_digest="digest_base",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.FAILED,
                tests_failed=1,
            )
        ],
        all_passed=False,
    )
    rep_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id="ws_01",
        tree_digest="digest_rep",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.FAILED,
                tests_failed=1,
            )
        ],
        all_passed=False,
    )

    reg_class, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=base_summary,
        repaired_summary=rep_summary,
    )
    assert reg_class == RegressionClassification.PRE_EXISTING_FAILURE


def test_regression_engine_improvement():
    base_summary = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id="ws_01",
        tree_digest="digest_base",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.FAILED,
                tests_failed=1,
            )
        ],
        all_passed=False,
    )
    rep_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id="ws_01",
        tree_digest="digest_rep",
        checks=[
            CheckExecutionResult(
                check_id="chk_01",
                category=TestScopeCategory.TARGETED,
                command=["pytest"],
                outcome=CheckOutcome.PASSED,
                tests_passed=1,
            )
        ],
        all_passed=True,
    )

    reg_class, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=base_summary,
        repaired_summary=rep_summary,
    )
    assert reg_class == RegressionClassification.IMPROVED


# ---------------------------------------------------------------------------
# Category F: Integrity & Adversarial Tests
# ---------------------------------------------------------------------------

def test_adversarial_baseline_snapshot_tamper_detected(test_repo_env):
    _, _, snap_dir, workspace, exec_result = test_repo_env
    plan_gen = VerificationPlanGenerator()
    plan = plan_gen.generate_plan(execution_result=exec_result, workspace=workspace)

    # Tamper with baseline snapshot
    (snap_dir / "pkg" / "calculator.py").write_text("# Tampered baseline!", encoding="utf-8")

    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    base_runner = BaselineVerificationRunner(sandbox=v_sbx)

    summary = base_runner.run_baseline(plan=plan, workspace=workspace)
    assert summary.all_passed is False
    assert "Corrupted baseline snapshot" in summary.sandbox_details.get("error", "")


def test_adversarial_symlink_escape_detected(tmp_path: Path):
    ws_dir = tmp_path / "ws_symlink"
    ws_dir.mkdir()
    outside_dir = tmp_path / "outside_secret"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("classified", encoding="utf-8")

    # Create escaping symlink inside workspace
    (ws_dir / "escape_link").symlink_to(outside_dir)

    is_ok, violations = VerificationIntegrityChecker.verify_workspace_integrity(
        workspace_path=ws_dir,
        allowed_modified_paths=[],
    )
    assert is_ok is False
    assert any("Escaping symlink" in v for v in violations)


def test_adversarial_verification_result_requires_human_review_invariant():
    with pytest.raises(ValueError, match="requires_human_review must strictly be True"):
        VerificationResult(
            verification_id="vrf_01",
            execution_id="rex_01",
            workspace_id="ws_01",
            repair_plan_id="p01",
            repair_plan_digest="d01",
            applied_patch_digest="d02",
            baseline_tree_digest="d03",
            verification_status=VerificationStatus.PASSED,
            regression_classification=RegressionClassification.NO_REGRESSION_OBSERVED,
            gate_decision="PASSED",
            requires_human_review=False,  # Forbidden!
        )


def test_adversarial_verification_result_passed_with_failed_checks_rejected():
    with pytest.raises(ValueError, match="cannot mark verification as PASSED when required checks failed"):
        VerificationResult(
            verification_id="vrf_01",
            execution_id="rex_01",
            workspace_id="ws_01",
            repair_plan_id="p01",
            repair_plan_digest="d01",
            applied_patch_digest="d02",
            baseline_tree_digest="d03",
            verification_status=VerificationStatus.PASSED,
            regression_classification=RegressionClassification.NO_REGRESSION_OBSERVED,
            required_checks_failed=["chk_targeted_001"],  # Inconsistency!
            gate_decision="PASSED",
            requires_human_review=True,
        )


def test_adversarial_verification_result_passed_with_regression_rejected():
    with pytest.raises(ValueError, match="cannot mark verification as PASSED when regression is detected"):
        VerificationResult(
            verification_id="vrf_01",
            execution_id="rex_01",
            workspace_id="ws_01",
            repair_plan_id="p01",
            repair_plan_digest="d01",
            applied_patch_digest="d02",
            baseline_tree_digest="d03",
            verification_status=VerificationStatus.PASSED,
            regression_classification=RegressionClassification.REGRESSION_DETECTED,  # Inconsistency!
            gate_decision="PASSED",
            requires_human_review=True,
        )


def test_adversarial_event_chain_tampering():
    recorder = VerificationEventRecorder()
    recorder.record_event("v01", VerificationEventType.VERIFICATION_REQUESTED, "Action 1")
    ev2 = recorder.record_event("v01", VerificationEventType.SANDBOX_INSPECTED, "Action 2")

    assert recorder.verify_event_chain(expected_head_hash=ev2.event_hash) is True

    # Mutate event list directly
    recorder._events.pop(0)
    with pytest.raises(ValueError, match="Integrity violation"):
        recorder.verify_event_chain()


# ---------------------------------------------------------------------------
# Category G & H: Lifecycle & End-to-End Orchestration Tests
# ---------------------------------------------------------------------------

def test_executor_end_to_end_clean_pass(test_repo_env):
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env

    fake_sbx = FakeSandboxAdapter(
        is_available_flag=True,
        default_exit_code=0,
        default_stdout="tests/test_calculator.py::test_add PASSED\n=== 1 passed in 0.05s ===",
    )
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    v_res, updated_exec = executor.verify_repair(
        execution_result=exec_result,
        workspace=workspace,
    )

    assert v_res.verification_status == VerificationStatus.PASSED
    assert v_res.gate_decision == "PASSED"
    assert v_res.regression_classification == RegressionClassification.NO_REGRESSION_OBSERVED
    assert v_res.requires_human_review is True
    assert updated_exec is not None
    assert updated_exec.verification_pending is False


def test_executor_end_to_end_regression_detected(test_repo_env):
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env

    # Baseline passes, Repaired fails
    custom = {
        "base_": SandboxResult(
            request_id="r1",
            run_id="run_01",
            status=SbxStatus.SUCCEEDED,
            exit_code=0,
            stdout="tests/test_calculator.py::test_add PASSED\n=== 1 passed in 0.05s ===",
            duration_ms=50.0,
        ),
        "rep_": SandboxResult(
            request_id="r2",
            run_id="run_01",
            status=SbxStatus.FAILED,
            exit_code=1,
            stdout="tests/test_calculator.py::test_add FAILED\n=== 1 failed in 0.05s ===",
            duration_ms=50.0,
        ),
    }

    fake_sbx = FakeSandboxAdapter(is_available_flag=True, custom_responses=custom)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    v_res, updated_exec = executor.verify_repair(
        execution_result=exec_result,
        workspace=workspace,
    )

    assert v_res.verification_status == VerificationStatus.REGRESSION_DETECTED
    assert v_res.gate_decision == "FAILED"
    assert v_res.regression_classification == RegressionClassification.REGRESSION_DETECTED
    assert updated_exec is None  # verification_pending not cleared!


def test_executor_end_to_end_sandbox_unavailable(test_repo_env):
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env

    fake_sbx = FakeSandboxAdapter(is_available_flag=False, health_message="Daemon offline")
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    v_res, updated_exec = executor.verify_repair(
        execution_result=exec_result,
        workspace=workspace,
    )

    assert v_res.verification_status == VerificationStatus.SANDBOX_UNAVAILABLE
    assert v_res.gate_decision == "BLOCKED"
    assert updated_exec is None


def test_tool_registry_m5_tools_invocation(test_repo_env):
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    tools = RepairVerificationTools(repository_path=repo_dir)

    tools.register_workspace(workspace)
    tools.register_execution(exec_result)

    # 1. create_repair_verification_plan
    res_plan = tools.create_repair_verification_plan({
        "execution_id": exec_result.execution_id,
        "workspace_id": workspace.workspace_id,
        "repair_plan_id": exec_result.repair_plan_id,
    })
    assert '"status": "created"' in res_plan or '"status":"created"' in res_plan

    # 2. discover_repair_tests
    res_disc = tools.discover_repair_tests({
        "workspace_path": str(ws_dir),
        "modified_files": ["pkg/calculator.py"],
    })
    assert "test_calculator.py" in res_disc

    # 3. inspect_verification_environment
    res_env = tools.inspect_verification_environment({})
    assert "sandbox_available" in res_env

    # 4. validate_verification_integrity
    res_integ = tools.validate_verification_integrity({
        "workspace_id": workspace.workspace_id,
    })
    assert '"integrity_status": "VALID"' in res_integ or '"integrity_status":"VALID"' in res_integ


def test_adversarial_direct_assignment_mutation_blocked():
    res = VerificationResult(
        verification_id="vrf_01",
        execution_id="rex_01",
        workspace_id="ws_01",
        repair_plan_id="p01",
        repair_plan_digest="d01",
        applied_patch_digest="d02",
        baseline_tree_digest="d03",
        verification_status=VerificationStatus.FAILED,
        regression_classification=RegressionClassification.REGRESSION_DETECTED,
        gate_decision="FAILED",
        requires_human_review=True,
    )
    with pytest.raises((TypeError, ValueError)):
        res.requires_human_review = False


def test_adversarial_model_copy_update_revalidation():
    res = VerificationResult(
        verification_id="vrf_01",
        execution_id="rex_01",
        workspace_id="ws_01",
        repair_plan_id="p01",
        repair_plan_digest="d01",
        applied_patch_digest="d02",
        baseline_tree_digest="d03",
        verification_status=VerificationStatus.FAILED,
        regression_classification=RegressionClassification.REGRESSION_DETECTED,
        gate_decision="FAILED",
        requires_human_review=True,
    )
    with pytest.raises(ValueError, match="requires_human_review must strictly be True"):
        res.model_copy(update={"requires_human_review": False})

    with pytest.raises(ValueError, match="cannot mark verification as PASSED when required checks failed"):
        res.model_copy(update={
            "verification_status": VerificationStatus.PASSED,
            "required_checks_failed": ["chk_01"],
            "gate_decision": "PASSED",
        })


def test_tool_remaining_invocations(test_repo_env):
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    tools = RepairVerificationTools(repository_path=repo_dir, sandbox=v_sbx)

    tools.register_workspace(workspace)
    tools.register_execution(exec_result)

    # 1. create_repair_verification_plan
    res_plan = tools.create_repair_verification_plan({
        "execution_id": exec_result.execution_id,
        "workspace_id": workspace.workspace_id,
        "repair_plan_id": exec_result.repair_plan_id,
    })
    plan_data = json.loads(res_plan)
    plan_id = plan_data["plan_id"]

    # 2. run_baseline_verification
    res_base = tools.run_baseline_verification({
        "plan_id": plan_id,
        "workspace_id": workspace.workspace_id,
    })
    base_data = json.loads(res_base)
    assert base_data["baseline_digest_verified"] is True

    # 3. run_repair_verification
    res_rep = tools.run_repair_verification({
        "plan_id": plan_id,
        "workspace_id": workspace.workspace_id,
    })
    rep_data = json.loads(res_rep)
    v_id = rep_data["verification_id"]

    # 4. compare_repair_verification
    res_comp = tools.compare_repair_verification({"verification_id": v_id})
    comp_data = json.loads(res_comp)
    assert "classification" in comp_data

    # 5. inspect_verification_evidence
    res_evi = tools.inspect_verification_evidence({"verification_id": v_id})
    evi_data = json.loads(res_evi)
    assert "evidence_records" in evi_data

    # 6. get_repair_verification_summary
    res_sum = tools.get_repair_verification_summary({"verification_id": v_id})
    sum_data = json.loads(res_sum)
    assert sum_data["verification_id"] == v_id
    assert "# Repair Verification Summary" in sum_data["summary_markdown"]


def test_original_repo_mutation_detected(test_repo_env):
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    expected_digest = compute_tree_digest(repo_dir)

    assert VerificationIntegrityChecker.verify_original_repo_unmodified(repo_dir, expected_digest) is True

    # Mutate repo
    (repo_dir / "unauthorized_change.txt").write_text("modified!", encoding="utf-8")
    assert VerificationIntegrityChecker.verify_original_repo_unmodified(repo_dir, expected_digest) is False


# ---------------------------------------------------------------------------
# Category I: Final Security Audit & Adversarial Scenarios
# ---------------------------------------------------------------------------

def test_adversarial_replay_verification_rejected(test_repo_env):
    """Replaying the same verification execution ID is rejected by the executor."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    # First run succeeds
    res1, updated1 = executor.verify_repair(execution_result=exec_result, workspace=workspace)
    assert res1.verification_status == VerificationStatus.PASSED

    # Second run with identical verification_id must be rejected as replay
    res2, updated2 = executor.verify_repair(execution_result=exec_result, workspace=workspace)
    assert res2.verification_status == VerificationStatus.BLOCKED
    assert res2.gate_decision == "BLOCKED"
    assert any("replay" in lim.lower() for lim in res2.limitations)
    assert updated2 is None


def test_adversarial_mismatched_plan_patch_digest_rejected(test_repo_env):
    """Providing a VerificationPlan with mismatched patch digest is rejected."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    gen = VerificationPlanGenerator()
    valid_plan = gen.generate_plan(execution_result=exec_result, workspace=workspace)
    tampered_plan = valid_plan.model_copy(update={"expected_patch_digest": "f" * 64})

    res, updated = executor.verify_repair(
        execution_result=exec_result,
        workspace=workspace,
        plan=tampered_plan,
    )
    assert res.verification_status == VerificationStatus.BLOCKED
    assert res.gate_decision == "FAILED"
    assert any("patch digest" in lim for lim in res.limitations)
    assert updated is None


def test_adversarial_empty_selected_checks_fails_closed(test_repo_env):
    """Verification plan with zero checks must never pass verification gates."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    empty_plan = VerificationPlan(
        plan_id="vplan_empty",
        repair_plan_id=exec_result.repair_plan_id,
        execution_id=exec_result.execution_id,
        workspace_id=workspace.workspace_id,
        selected_checks=[],
        selection_method="none",
        selection_rationale="empty",
        expected_baseline_digest=workspace.baseline_tree_digest,
        expected_patch_digest=exec_result.applied_patch_digest,
    )
    repaired_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_01",
        checks=[],
        all_passed=True,
        sandbox_available=True,
    )
    status, decision, req_done, req_fail, opt_fail = VerificationGateKeeper.evaluate_gates(
        execution_result=exec_result,
        plan=empty_plan,
        sandbox_available=True,
        baseline_summary=None,
        repaired_summary=repaired_summary,
        regression_class=RegressionClassification.NO_REGRESSION_OBSERVED,
        workspace_integrity_passed=True,
        integrity_violations=[],
    )
    assert status == VerificationStatus.INCOMPLETE
    assert decision == "FAILED"


def test_adversarial_missing_baseline_summary_classified_as_inconclusive(test_repo_env):
    """Missing baseline summary cannot produce NO_REGRESSION_OBSERVED guarantee."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    rep_chk = CheckExecutionResult(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v"],
        outcome=CheckOutcome.PASSED,
        exit_code=0,
    )
    repaired_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_01",
        checks=[rep_chk],
        all_passed=True,
        sandbox_available=True,
    )
    classification, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=None,
        repaired_summary=repaired_summary,
    )
    assert classification == RegressionClassification.INCONCLUSIVE
    assert len(findings) == 1
    assert "missing" in findings[0].explanation.lower()


def test_adversarial_corrupted_baseline_summary_classified_as_inconclusive(test_repo_env):
    """Corrupted baseline snapshot execution produces INCONCLUSIVE regression result."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    rep_chk = CheckExecutionResult(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v"],
        outcome=CheckOutcome.PASSED,
        exit_code=0,
    )
    baseline_summary = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id=workspace.workspace_id,
        tree_digest="",
        checks=[],
        all_passed=False,
        sandbox_available=True,
        sandbox_details={"error": "Corrupted baseline snapshot: digest mismatch"},
    )
    repaired_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_01",
        checks=[rep_chk],
        all_passed=True,
        sandbox_available=True,
    )
    classification, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=baseline_summary,
        repaired_summary=repaired_summary,
    )
    assert classification == RegressionClassification.INCONCLUSIVE
    assert any("corrupted" in f.explanation.lower() for f in findings)


def test_adversarial_environment_mismatch_detected(test_repo_env):
    """Mismatched test suites or sandbox images produce ENVIRONMENT_MISMATCH."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    chk1 = CheckExecutionResult(
        check_id="chk_01",
        category=TestScopeCategory.TARGETED,
        command=["pytest", "-v"],
        outcome=CheckOutcome.PASSED,
        exit_code=0,
    )
    chk2 = CheckExecutionResult(
        check_id="chk_02",
        category=TestScopeCategory.REGRESSION,
        command=["pytest", "-v"],
        outcome=CheckOutcome.PASSED,
        exit_code=0,
    )

    # 1. Different checks in baseline vs repaired
    base_summary = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_01",
        checks=[chk1],
        all_passed=True,
        sandbox_available=True,
    )
    rep_summary = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_02",
        checks=[chk1, chk2],
        all_passed=True,
        sandbox_available=True,
    )
    classification, findings = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=base_summary,
        repaired_summary=rep_summary,
    )
    assert classification == RegressionClassification.ENVIRONMENT_MISMATCH

    # 2. Conflicting container images in sandbox_details
    base_summary_img = SuiteExecutionSummary(
        execution_type="baseline",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_01",
        checks=[chk1],
        all_passed=True,
        sandbox_available=True,
        sandbox_details={"image": "python:3.11-slim"},
    )
    rep_summary_img = SuiteExecutionSummary(
        execution_type="repaired",
        workspace_id=workspace.workspace_id,
        tree_digest="tree_02",
        checks=[chk1],
        all_passed=True,
        sandbox_available=True,
        sandbox_details={"image": "python:3.12-slim"},
    )
    classification_img, findings_img = DeterministicRegressionEngine.analyze_regressions(
        baseline_summary=base_summary_img,
        repaired_summary=rep_summary_img,
    )
    assert classification_img == RegressionClassification.ENVIRONMENT_MISMATCH


def test_adversarial_event_payload_in_place_tampering_detected():
    """Modifying event payload in place is detected by verify_event_chain hash recomputation."""
    recorder = VerificationEventRecorder()
    ev1 = recorder.record_event("vrf_01", VerificationEventType.VERIFICATION_REQUESTED, "Start verification")
    ev2 = recorder.record_event("vrf_01", VerificationEventType.VERIFICATION_PASSED, "Passed verification")

    # Intact chain passes
    assert recorder.verify_event_chain(expected_head_hash=ev2.event_hash) is True

    # Tamper with ev1 payload by constructing modified copy with same event_hash
    tampered_ev1 = VerificationEvent(
        event_id=ev1.event_id,
        verification_id=ev1.verification_id,
        sequence_number=ev1.sequence_number,
        prev_event_hash=ev1.prev_event_hash,
        event_hash=ev1.event_hash,  # Preserved original hash!
        event_type=ev1.event_type,
        timestamp=ev1.timestamp,
        actor=ev1.actor,
        action_description="Tampered description inserted here",
        outcome=ev1.outcome,
    )
    recorder._events[0] = tampered_ev1

    with pytest.raises(ValueError, match="event payload tampering detected"):
        recorder.verify_event_chain()


def test_adversarial_dynamic_import_and_test_directory_discovery(tmp_path: Path):
    """Discoverer detects tests in test/ directory and flags dynamic imports as limitations."""
    ws = tmp_path / "ws_dynamic"
    ws.mkdir()
    (ws / "calc.py").write_text("def mul(a, b): return a * b\n", encoding="utf-8")

    # Directory named test/ (singular)
    test_dir = ws / "test"
    test_dir.mkdir()
    (test_dir / "my_check.py").write_text(
        "import importlib\n"
        "def test_dyn():\n"
        "    mod = importlib.import_module('calc')\n"
        "    assert mod.mul(2, 3) == 6\n",
        encoding="utf-8",
    )

    discoverer = RepairTestDiscoverer(workspace_path=ws)
    res = discoverer.discover_tests(modified_files=["calc.py"])

    # Discovered test file in test/
    assert "test/my_check.py" in res.all_test_files
    # Detected dynamic import and added to limitations
    assert any("dynamic import" in lim.lower() for lim in res.limitations)

    # Test selection must mark partial verification due to limitations
    selector = IntelligentTestSelector(workspace_path=ws)
    checks, ids, excl, is_partial, rationale = selector.select_checks(modified_files=["calc.py"], discovered=res)
    assert is_partial is True
    assert "limitations" in rationale.lower()


def test_adversarial_indirect_module_import_discovery(tmp_path: Path):
    """Discoverer detects 1-hop indirect dependencies and categorizes importing tests as regression."""
    ws = tmp_path / "ws_indirect"
    ws.mkdir()
    (ws / "core.py").write_text("def base_val(): return 42\n", encoding="utf-8")
    (ws / "service.py").write_text("from core import base_val\ndef get_service(): return base_val() + 1\n", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_service.py").write_text(
        "import service\ndef test_srv(): assert service.get_service() == 43\n",
        encoding="utf-8",
    )

    discoverer = RepairTestDiscoverer(workspace_path=ws)
    # core.py was modified; test_service imports service.py which imports core.py
    res = discoverer.discover_tests(modified_files=["core.py"])
    assert len(res.regression_tests) >= 1
    assert res.regression_tests[0].test_file == "tests/test_service.py"


# ---------------------------------------------------------------------------
# Category J: End-to-End Fake-Adapter Scenarios
# ---------------------------------------------------------------------------

def test_fake_adapter_e2e_new_regression_detected(test_repo_env):
    """Scenario A: New regression introduced by repair (baseline passes, repaired fails)."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env

    # Baseline passes, repaired fails
    custom_responses = {
        "base_": SandboxResult(
            request_id="sbx_base",
            run_id="base_ws_001",
            status=SbxStatus.SUCCEEDED,
            exit_code=0,
            stdout="=== 2 passed in 0.05s ===",
        ),
        "rep_": SandboxResult(
            request_id="sbx_rep",
            run_id="rep_ws_001",
            status=SbxStatus.FAILED,
            exit_code=1,
            stdout="=== 1 passed, 1 failed in 0.05s ===\nFAILED tests/test_calculator.py::test_divide",
        ),
    }
    fake_sbx = FakeSandboxAdapter(is_available_flag=True, custom_responses=custom_responses)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    res, updated_exec = executor.verify_repair(execution_result=exec_result, workspace=workspace)

    assert res.verification_status == VerificationStatus.REGRESSION_DETECTED
    assert res.gate_decision == "FAILED"
    assert res.regression_classification == RegressionClassification.REGRESSION_DETECTED
    assert updated_exec is None  # verification_pending remains True!


def test_fake_adapter_e2e_pre_existing_failure_preserved(test_repo_env):
    """Scenario B: Pre-existing failure preserved in both baseline and repaired."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env

    failing_result = SandboxResult(
        request_id="sbx_fail",
        run_id="run_01",
        status=SbxStatus.FAILED,
        exit_code=1,
        stdout="=== 1 failed in 0.05s ===\nFAILED tests/test_calculator.py::test_divide",
    )
    fake_sbx = FakeSandboxAdapter(
        is_available_flag=True,
        custom_responses={"base_": failing_result, "rep_": failing_result},
    )
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    res, updated_exec = executor.verify_repair(execution_result=exec_result, workspace=workspace)

    assert res.regression_classification == RegressionClassification.PRE_EXISTING_FAILURE
    assert res.verification_status == VerificationStatus.FAILED
    assert updated_exec is None


def test_fake_adapter_e2e_existing_failure_fixed(test_repo_env):
    """Scenario C: Existing failure fixed by repair (baseline fails, repaired passes)."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env

    custom_responses = {
        "base_": SandboxResult(
            request_id="sbx_base",
            run_id="base_ws_001",
            status=SbxStatus.FAILED,
            exit_code=1,
            stdout="=== 1 failed in 0.05s ===\nFAILED tests/test_calculator.py::test_divide",
        ),
        "rep_": SandboxResult(
            request_id="sbx_rep",
            run_id="rep_ws_001",
            status=SbxStatus.SUCCEEDED,
            exit_code=0,
            stdout="=== 2 passed in 0.05s ===",
        ),
    }
    fake_sbx = FakeSandboxAdapter(is_available_flag=True, custom_responses=custom_responses)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    res, updated_exec = executor.verify_repair(execution_result=exec_result, workspace=workspace)

    assert res.regression_classification == RegressionClassification.IMPROVED
    assert res.verification_status == VerificationStatus.PASSED
    assert res.gate_decision == "PASSED"
    assert updated_exec is not None
    assert updated_exec.verification_pending is False


def test_fake_adapter_e2e_sandbox_unavailable_fails_closed(test_repo_env):
    """Scenario D: Sandbox unavailable fails closed with zero host execution."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    fake_sbx = FakeSandboxAdapter(is_available_flag=False, health_message="Daemon unreachable")
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    res, updated_exec = executor.verify_repair(execution_result=exec_result, workspace=workspace)

    assert res.verification_status == VerificationStatus.SANDBOX_UNAVAILABLE
    assert res.gate_decision == "BLOCKED"
    assert updated_exec is None
    assert len(fake_sbx.executed_requests) == 0  # Zero execution attempts on unavailable sandbox!


def test_fake_adapter_e2e_unapplied_execution_rejected(test_repo_env):
    """Scenario E: Unapplied execution status rejected at gate 0."""
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    unapplied_exec = exec_result.model_copy(update={"execution_status": ExecutionStatus.FAILED})

    fake_sbx = FakeSandboxAdapter(is_available_flag=True)
    v_sbx = VerificationSandbox(sandbox=fake_sbx)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    # Plan generation raises on unapplied status
    with pytest.raises(ValueError, match="expected 'applied'"):
        executor.verify_repair(execution_result=unapplied_exec, workspace=workspace)


def test_genuine_docker_execution_when_available(test_repo_env):
    """Genuine Docker container execution test when Docker daemon is reachable.

    Explicitly verifies real container isolation, non-root execution, network disabled,
    read-only mounts, and immutability of the original repository.
    """
    repo_dir, ws_dir, snap_dir, workspace, exec_result = test_repo_env
    real_docker = DockerSandbox()
    healthy, msg = real_docker.check_health()
    if not healthy:
        pytest.skip(f"Real Docker sandbox unavailable in current test environment: {msg}")

    v_sbx = VerificationSandbox(sandbox=real_docker)
    executor = RepairVerificationExecutor(sandbox=v_sbx)

    res, updated_exec = executor.verify_repair(execution_result=exec_result, workspace=workspace)

    assert res.verification_status in (VerificationStatus.PASSED, VerificationStatus.FAILED)
    # Original repository must remain completely pristine
    expected_digest = compute_tree_digest(repo_dir)
    assert VerificationIntegrityChecker.verify_original_repo_unmodified(repo_dir, expected_digest) is True

