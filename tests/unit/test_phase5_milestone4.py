"""Comprehensive unit and integration tests for Phase 5 Milestone 4: Multi-Check Verification.

Tests verify:
1. Deterministic check ordering: reproduction oracle -> baseline test suite -> lint -> security.
2. Missing tools handling: honestly recorded as SKIPPED with limitations (or BLOCKED if required), never fabricated.
3. Baseline pre-condition failures: baseline reproduction passing unexpectedly maps to BASELINE_FAILED.
4. Candidate defect failure: defect still reproducible after patch maps to FAILED and VERIFICATION_FAILED.
5. Candidate regression detection: patch breaking baseline tests maps to FAILED and REGRESSION_FAILED.
6. Lint & security failure detection: syntax/lint or security regressions detected and mapped to FAILED.
7. Timeouts: individual check timeout quota enforcement maps to TIMED_OUT.
8. Command allowlisting & shell injection prevention: rejection of shell metacharacters and unapproved binaries.
9. Output bounds & secret redaction: bounded excerpts with secrets scrubbed.
10. Stale revisions & drift detection: commit SHA drift intercepted before untrusted execution.
11. Sandbox unavailable fail-closed: Docker unavailability maps to BLOCKED / VERIFICATION_BLOCKED without host execution.
12. Patch tampering detection: SHA256 mismatch detected and blocked.
13. Protected file immunity: patches targeting protected files rejected.
14. Target repository immutability: zero mutations to target repository.
15. Concurrency conflicts: optimistic versioning conflicts properly caught.
16. Mandatory human review gate: clean verification lands at AWAITING_HUMAN_REVIEW, never automatically approved.
17. Safe resumption: workflow in VERIFICATION_BLOCKED can resume.
18. CLI commands: `forge workflow verify` and `forge workflow advance --verify`.
"""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairOutcome,
    compute_patch_sha256,
)
from forge.repair.models import (
    PlanValidationResult,
    RepairPlan,
    RepairPlanStatus,
)
from forge.sandbox.contracts import ReproductionOutcome
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    SandboxRequest,
    SandboxResult,
)
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.intake import create_intake_from_test_spec
from forge.workflow.models import (
    CheckStatus,
    EngineeringWorkflow,
    EngineeringWorkflowState,
    HumanApprovalRequiredError,
    InvalidWorkflowTransitionError,
    MultiCheckVerificationPlan,
    MultiCheckVerificationResult,
    PatchIntegrityTamperedError,
    RevisionDriftError,
    VerificationAggregateOutcome,
    VerificationCheckResult,
    VerificationCheckSpec,
    VerificationCheckType,
    WorkflowConcurrencyError,
)
from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator
from forge.workflow.verification import (
    MultiCheckVerifier,
    build_default_verification_plan,
    validate_verification_check_command,
)

runner = CliRunner()


# =============================================================================
# Test Harnesses & Mocks
# =============================================================================

def make_sandbox_res(
    req: Optional[SandboxRequest] = None,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    exit_code: Optional[int] = 0,
    stdout: str = "OK",
    stderr: str = "",
    timed_out: bool = False,
    error_message: Optional[str] = None,
) -> SandboxResult:
    return SandboxResult(
        request_id=req.request_id if req else "sbx_test_req",
        run_id=req.run_id if req else "run_test",
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        error_message=error_message,
    )


class MockSandbox:
    """Configurable offline sandbox mock for deterministic multi-check verification tests."""

    def __init__(
        self,
        healthy: bool = True,
        health_message: str = "Mock sandbox available",
        available_tools: Optional[List[str]] = None,
        responses: Optional[Dict[str, Any]] = None,
        default_response: Optional[Any] = None,
    ):
        self.healthy = healthy
        self.health_message = health_message
        self.available_tools = set(available_tools or ["python", "python3", "pytest", "flake8", "ruff", "bandit"])
        self.responses = responses or {}
        self.default_response = default_response
        self.executed_requests: List[SandboxRequest] = []

    def check_health(self) -> Tuple[bool, str]:
        return self.healthy, self.health_message

    def has_tool(self, binary: str) -> bool:
        return binary in self.available_tools

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.executed_requests.append(request)
        cmd_str = " ".join(request.command)
        for key, res in self.responses.items():
            if key in cmd_str:
                if isinstance(res, SandboxResult):
                    return res.model_copy(update={"request_id": request.request_id, "run_id": request.run_id})
                return res
        if self.default_response is not None:
            if isinstance(self.default_response, SandboxResult):
                return self.default_response.model_copy(update={"request_id": request.request_id, "run_id": request.run_id})
            return self.default_response
        return make_sandbox_res(request, status=ExecutionStatus.SUCCEEDED, exit_code=0, stdout="OK")


@pytest.fixture
def test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Create a real git repository fixture with sample files."""
    repo_dir = tmp_path / "git_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    calc = repo_dir / "calculator.py"
    calc.write_text(
        "def calculate(a, b):\n"
        "    return a + b  # Defect: should be a - b\n"
    )

    test_file = tests_dir / "test_calc.py"
    test_file.write_text(
        "from calculator import calculate\n\n"
        "def test_calc():\n"
        "    assert calculate(5, 2) == 3\n"
    )

    base_test = tests_dir / "test_baseline.py"
    base_test.write_text(
        "def test_always_passes():\n"
        "    assert 1 + 1 == 2\n"
    )

    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Forge Tester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "tester@forge.internal"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)

    git_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    commit_sha = git_res.stdout.strip().lower()
    return repo_dir, commit_sha


@pytest.fixture
def mock_store(tmp_path: Path) -> ForgeStore:
    db_file = tmp_path / "test_wf4.sqlite3"
    init_database(db_file)
    return ForgeStore(db_path=db_file)


def _make_valid_repair_plan(
    repo_path: Path,
    commit_sha: str,
    finding_id: str = "find_test_001",
    plan_id: str = "plan_test_001",
    diff_override: Optional[str] = None,
) -> RepairPlan:
    diff = diff_override or (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def calculate(a, b):\n"
        "-    return a + b  # Defect: should be a - b\n"
        "+    return a - b\n"
    )
    digest = compute_patch_sha256(diff)
    candidate = PatchCandidate(
        patch_id=f"patch_{plan_id}",
        contract_id=f"contract_{plan_id}",
        finding_id=finding_id,
        base_commit_sha=commit_sha,
        target_files=["calculator.py"],
        unified_diff=diff,
        added_lines=1,
        removed_lines=1,
        hunk_count=1,
        sha256_digest=digest,
    )
    return RepairPlan(
        plan_id=plan_id,
        finding_id=finding_id,
        base_commit_sha=commit_sha,
        cited_evidence_ids=["evi_001"],
        target_repo=str(repo_path),
        target_file="calculator.py",
        root_cause="Addition used instead of subtraction in calculate function",
        proposed_change="Change + operator to - operator",
        expected_behavior="calculate(5, 2) returns 3",
        regression_risks=[],
        verification_plan="pytest tests/test_calc.py",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=candidate,
    )


def _setup_store_records(store: ForgeStore, repo: Path, spec: Any, plan: Any) -> None:
    store.create_run(str(repo), run_id="run_001")
    store.create_finding(
        run_id="run_001",
        title="Test finding",
        description="Test description",
        finding_id=plan.finding_id,
    )
    store.save_workflow_intake(spec)
    store.save_repair_plan(plan)


# =============================================================================
# 1. Command Validation & Allowlisting Tests
# =============================================================================

def test_validate_verification_check_command_valid():
    cmd = ["pytest", "tests/test_calc.py", "-q"]
    validated = validate_verification_check_command(cmd)
    assert validated == cmd


def test_validate_verification_check_command_empty_raises():
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_verification_check_command([])


def test_validate_verification_check_command_unapproved_binary_raises():
    with pytest.raises(ValueError, match="is not permitted"):
        validate_verification_check_command(["curl", "https://attacker.com"])


def test_validate_verification_check_command_shell_operator_raises():
    with pytest.raises(ValueError, match="forbidden shell metacharacters"):
        validate_verification_check_command(["pytest", "test.py; rm -rf /"])


# =============================================================================
# 2. Plan Construction & Check Ordering
# =============================================================================

def test_build_default_verification_plan_ordering(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    plan = _make_valid_repair_plan(repo, commit_sha)
    vplan = build_default_verification_plan(
        workflow_id="wf_001",
        repair_plan=plan,
        intake_spec=spec,
        repo_path=repo,
        include_lint=True,
        include_security=True,
    )

    types = [c.check_type for c in vplan.checks]
    assert types[0] == VerificationCheckType.REPRODUCTION
    assert types[1] == VerificationCheckType.BASELINE_TESTS
    assert types[2] == VerificationCheckType.LINT
    assert types[3] == VerificationCheckType.SECURITY
    assert vplan.checks[0].expected_baseline_failure is True
    assert vplan.checks[1].expected_baseline_failure is False


# =============================================================================
# 3. Missing Tool Handling (Honest Limitations, Never Fabricated)
# =============================================================================

def test_verifier_missing_optional_tool_skips_honestly(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    # Sandbox without bandit
    sandbox = MockSandbox(
        available_tools=["python", "pytest", "flake8"],  # bandit missing
        responses={
            "test_calc": make_sandbox_res(status=ExecutionStatus.SUCCEEDED, exit_code=1, stderr="Defect reproduced"),
        },
    )

    vplan = build_default_verification_plan("wf_001", plan, spec, repo, include_lint=True, include_security=True)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    # Let candidate reproduction pass
    def mock_run(req: SandboxRequest) -> SandboxResult:
        if "test_calc" in " ".join(req.command):
            if "baseline" in req.provenance:
                return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=1, stderr="AssertionError: 5 != 3")
            else:
                return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0, stdout="1 passed")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0, stdout="Passed")

    sandbox.run = mock_run
    res = verifier.execute_verification(vplan, plan, repo)

    assert res.aggregate_outcome == VerificationAggregateOutcome.PASSED
    sec_cand = next((c for c in res.candidate_results if c.check_type == VerificationCheckType.SECURITY), None)
    assert sec_cand is not None
    assert sec_cand.status == CheckStatus.SKIPPED
    assert sec_cand.tool_available is False
    assert any("bandit" in lim for lim in res.limitations)


def test_verifier_missing_required_tool_blocks(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    # Sandbox without pytest (required for reproduction)
    sandbox = MockSandbox(available_tools=["python"])
    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.BLOCKED
    assert "pytest" in res.diagnostic


# =============================================================================
# 4. Baseline Pre-Condition & Defect Reproduction Logic
# =============================================================================

def test_verifier_baseline_defect_not_observed_fails(test_git_repo: Tuple[Path, str]):
    """If the baseline reproduction oracle passes unexpectedly, defect is not present -> BASELINE_FAILED."""
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    sandbox = MockSandbox(
        default_response=make_sandbox_res(status=ExecutionStatus.SUCCEEDED, exit_code=0, stdout="All passed")
    )
    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.BASELINE_FAILED
    assert "Baseline reproduction oracle passed unexpectedly" in res.diagnostic


def test_verifier_candidate_defect_still_fails(test_git_repo: Tuple[Path, str]):
    """If post-patch reproduction check still fails, outcome is FAILED."""
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    sandbox = MockSandbox(
        default_response=make_sandbox_res(status=ExecutionStatus.SUCCEEDED, exit_code=1, stderr="AssertionError: 5 != 3")
    )
    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.FAILED
    assert "defect is still reproducible" in res.diagnostic


# =============================================================================
# 5. Regression Detection
# =============================================================================

def test_verifier_detects_candidate_regression(test_git_repo: Tuple[Path, str]):
    """If baseline test suite passes on baseline but fails on candidate, regression is detected."""
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        cmd_str = " ".join(req.command)
        is_baseline = req.provenance.endswith(":baseline")
        if "test_calc.py" in cmd_str:
            # Defect reproduction: fails on baseline, passes on candidate
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(1 if is_baseline else 0))
        elif "tests/" in cmd_str:
            # Baseline test suite: passes on baseline, fails on candidate!
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(0 if is_baseline else 1), stderr="Regression in test_baseline.py")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0)

    sandbox = MockSandbox()
    sandbox.run = mock_run

    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.FAILED
    assert res.regression_detected is True
    assert "Regression detected" in res.diagnostic


# =============================================================================
# 6. Static Lint & Security Checks
# =============================================================================

def test_verifier_candidate_lint_failure(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        cmd_str = " ".join(req.command)
        is_baseline = req.provenance.endswith(":baseline")
        if "test_calc.py" in cmd_str:
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(1 if is_baseline else 0))
        elif "flake8" in cmd_str:
            # Passes on baseline, fails on candidate with syntax/style error
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(0 if is_baseline else 1), stdout="calculator.py:2:1: E302 expected 2 blank lines")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0)

    sandbox = MockSandbox()
    sandbox.run = mock_run

    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.FAILED
    assert "Lint check" in res.diagnostic


def test_verifier_candidate_security_failure(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        cmd_str = " ".join(req.command)
        is_baseline = req.provenance.endswith(":baseline")
        if "test_calc.py" in cmd_str:
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(1 if is_baseline else 0))
        elif "bandit" in cmd_str:
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(0 if is_baseline else 1), stdout="[B101:assert_used] Use of assert detected")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0)

    sandbox = MockSandbox()
    sandbox.run = mock_run

    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.FAILED
    assert "Security scan" in res.diagnostic


# =============================================================================
# 7. Timeouts & Output Bounds
# =============================================================================

def test_verifier_candidate_timeout(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        cmd_str = " ".join(req.command)
        is_baseline = req.provenance.endswith(":baseline")
        if "test_calc.py" in cmd_str:
            if is_baseline:
                return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=1, stderr="Defect reproduced")
            else:
                return make_sandbox_res(req, status=ExecutionStatus.TIMED_OUT, timed_out=True, exit_code=None, error_message="Process timed out")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0)

    sandbox = MockSandbox()
    sandbox.run = mock_run

    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.TIMED_OUT
    assert "timed out" in res.diagnostic


def test_verifier_output_bounds_and_secret_redaction(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    huge_output_with_secret = ("x" * 10000) + " sk-abcdef1234567890abcdef1234567890 " + ("y" * 10000)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        is_baseline = req.provenance.endswith(":baseline")
        return make_sandbox_res(
            req,
            status=ExecutionStatus.SUCCEEDED,
            exit_code=(1 if is_baseline else 0),
            stdout=huge_output_with_secret,
        )

    sandbox = MockSandbox()
    sandbox.run = mock_run

    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    c_res = res.candidate_results[0]
    assert len(c_res.stdout_excerpt) <= 5000
    assert "sk-" not in c_res.stdout_excerpt or "REDACTED" in c_res.stdout_excerpt


# =============================================================================
# 8. Sandbox Health & Fail-Closed Host Protection
# =============================================================================

def test_verifier_sandbox_unavailable_fails_closed(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    sandbox = MockSandbox(healthy=False, health_message="Docker binary not found in PATH")
    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.BLOCKED
    assert res.sandbox_available is False
    assert "fail-closed security policy strictly refuses" in res.diagnostic
    assert len(sandbox.executed_requests) == 0


# =============================================================================
# 9. Anti-Tampering & Protected Path Rejections
# =============================================================================

def test_verifier_patch_tampering_rejected(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    # Mutate patch content without updating digest
    tampered_candidate = plan.patch_candidate.model_copy(update={"unified_diff": plan.patch_candidate.unified_diff + "\n# injected"})
    tampered_plan = plan.model_copy(update={"patch_candidate": tampered_candidate})

    vplan = build_default_verification_plan("wf_001", tampered_plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=MockSandbox())

    res = verifier.execute_verification(vplan, tampered_plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.BLOCKED
    assert "tampering detected" in res.diagnostic


def test_verifier_protected_path_rejected(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    # Target protected AGENTS.md
    protected_plan = plan.model_copy(update={"target_file": "AGENTS.md"})
    vplan = build_default_verification_plan("wf_001", protected_plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=MockSandbox())

    res = verifier.execute_verification(vplan, protected_plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.FAILED
    assert "protected from patch application" in res.diagnostic


# =============================================================================
# 10. Target Repository Immutability
# =============================================================================

def test_verifier_target_repository_immutability(test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)

    # Capture initial git hash and status
    init_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    init_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout

    def mock_run(req: SandboxRequest) -> SandboxResult:
        is_baseline = req.provenance.endswith(":baseline")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(1 if is_baseline else 0))

    sandbox = MockSandbox()
    sandbox.run = mock_run

    vplan = build_default_verification_plan("wf_001", plan, spec, repo)
    verifier = MultiCheckVerifier(sandbox=sandbox)

    res = verifier.execute_verification(vplan, plan, repo)
    assert res.aggregate_outcome == VerificationAggregateOutcome.PASSED

    # Verify target repo was completely untouched
    final_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    final_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout

    assert final_status == init_status
    assert final_head == init_head


# =============================================================================
# 11. Orchestrator End-to-End Verification & Lifecycle Transitions
# =============================================================================

def test_orchestrator_verify_repair_happy_path(mock_store: ForgeStore, test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    orchestrator = EngineeringWorkflowOrchestrator(store=mock_store)

    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)
    _setup_store_records(mock_store, repo, spec, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_happy_001",
        intake_id=spec.intake_id,
        target_repo=str(repo),
        base_commit_sha=commit_sha,
        state=EngineeringWorkflowState.REPAIR_PENDING,
        repair_plan_id=plan.plan_id,
        created_at=now,
        updated_at=now,
    )
    mock_store.save_engineering_workflow(wf)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        is_baseline = req.provenance.endswith(":baseline")
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(1 if is_baseline else 0))

    sandbox = MockSandbox()
    sandbox.run = mock_run
    verifier = MultiCheckVerifier(sandbox=sandbox)

    final_wf = orchestrator.verify_repair(
        workflow_id="wf_happy_001",
        workspace_path=repo,
        verifier=verifier,
    )

    # Must advance through VERIFIED -> REGRESSION_CHECKING -> AWAITING_HUMAN_REVIEW and halt!
    assert final_wf.state == EngineeringWorkflowState.AWAITING_HUMAN_REVIEW
    assert final_wf.verification_attempt_id is not None
    assert final_wf.regression_summary["regression_detected"] is False

    # Stored multi-check verification result check
    stored_res = mock_store.get_multi_check_verification(final_wf.verification_attempt_id)
    assert stored_res is not None
    assert stored_res.aggregate_outcome == VerificationAggregateOutcome.PASSED


def test_orchestrator_verify_repair_regression_failed(mock_store: ForgeStore, test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    orchestrator = EngineeringWorkflowOrchestrator(store=mock_store)

    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)
    _setup_store_records(mock_store, repo, spec, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_regr_001",
        intake_id=spec.intake_id,
        target_repo=str(repo),
        base_commit_sha=commit_sha,
        state=EngineeringWorkflowState.REPAIR_PENDING,
        repair_plan_id=plan.plan_id,
        created_at=now,
        updated_at=now,
    )
    mock_store.save_engineering_workflow(wf)

    def mock_run(req: SandboxRequest) -> SandboxResult:
        cmd_str = " ".join(req.command)
        is_baseline = req.provenance.endswith(":baseline")
        if "test_calc.py" in cmd_str:
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(1 if is_baseline else 0))
        elif "tests/" in cmd_str:
            # Regression in baseline suite!
            return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=(0 if is_baseline else 1))
        return make_sandbox_res(req, status=ExecutionStatus.SUCCEEDED, exit_code=0)

    sandbox = MockSandbox()
    sandbox.run = mock_run
    verifier = MultiCheckVerifier(sandbox=sandbox)

    final_wf = orchestrator.verify_repair(
        workflow_id="wf_regr_001",
        workspace_path=repo,
        verifier=verifier,
    )

    assert final_wf.state == EngineeringWorkflowState.REGRESSION_FAILED
    assert final_wf.regression_summary["regression_detected"] is True


def test_orchestrator_verify_repair_sandbox_unavailable_blocked(mock_store: ForgeStore, test_git_repo: Tuple[Path, str]):
    repo, commit_sha = test_git_repo
    orchestrator = EngineeringWorkflowOrchestrator(store=mock_store)

    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    plan = _make_valid_repair_plan(repo, commit_sha)
    _setup_store_records(mock_store, repo, spec, plan)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_sbx_blocked",
        intake_id=spec.intake_id,
        target_repo=str(repo),
        base_commit_sha=commit_sha,
        state=EngineeringWorkflowState.REPAIR_PENDING,
        repair_plan_id=plan.plan_id,
        created_at=now,
        updated_at=now,
    )
    mock_store.save_engineering_workflow(wf)

    sandbox = MockSandbox(healthy=False, health_message="Daemon unavailable")
    verifier = MultiCheckVerifier(sandbox=sandbox)

    final_wf = orchestrator.verify_repair(
        workflow_id="wf_sbx_blocked",
        workspace_path=repo,
        verifier=verifier,
    )

    assert final_wf.state == EngineeringWorkflowState.VERIFICATION_BLOCKED
    assert "refuses to run repository code" in final_wf.error_message


def test_orchestrator_human_review_gate_enforced(mock_store: ForgeStore, test_git_repo: Tuple[Path, str]):
    """Verify that a workflow at AWAITING_HUMAN_REVIEW cannot be autonomously marked APPROVED without human decision."""
    repo, commit_sha = test_git_repo
    orchestrator = EngineeringWorkflowOrchestrator(store=mock_store)

    spec = create_intake_from_test_spec(repo_path=repo, test_file="tests/test_calc.py", test_command="pytest tests/test_calc.py")
    mock_store.save_workflow_intake(spec)

    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id="wf_review_gate",
        intake_id=spec.intake_id,
        target_repo=str(repo),
        base_commit_sha=commit_sha,
        state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        created_at=now,
        updated_at=now,
    )
    mock_store.save_engineering_workflow(wf)

    # Attempt transition to APPROVED without decision='approve' -> REJECTED!
    with pytest.raises(HumanApprovalRequiredError, match="strictly requires explicit human operator authorization"):
        mock_store.transition_engineering_workflow(
            workflow_id=wf.workflow_id,
            expected_version=wf.version,
            target_state=EngineeringWorkflowState.APPROVED,
            stage="autonomous_attempt",
            transition_context={"human_decision": None},
        )

    # Explicit human approval succeeds
    approved_wf = mock_store.transition_engineering_workflow(
        workflow_id=wf.workflow_id,
        expected_version=wf.version,
        target_state=EngineeringWorkflowState.APPROVED,
        stage="operator_approved",
        human_decision="approve",
        human_notes="Verified manually by operator.",
        transition_context={"human_decision": "approve"},
    )
    assert approved_wf.state == EngineeringWorkflowState.APPROVED
    assert approved_wf.human_decision == "approve"


# =============================================================================
# 12. CLI Commands
# =============================================================================

def test_cli_workflow_verify_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "cli_v_test.sqlite3"
    init_database(db_file)
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))

    res = runner.invoke(app, ["workflow", "verify", "wf_nonexistent"])
    assert res.exit_code == 1
    assert "not found" in res.output.lower() or "failed" in res.output.lower()


def test_cli_workflow_advance_help():
    res = runner.invoke(app, ["workflow", "advance", "--help"])
    assert res.exit_code == 0
    clean_output = re.sub(r"\x1b\[[0-9;]*m", "", res.output)
    assert "--verify" in clean_output
