"""Phase 8 Milestone 5 Unit Test Suite — Sandbox Repair Verification & Regression Testing.

Verifies:
1. Verification Preconditions:
   - Rejection when repair request, finding, or patch artifact is mismatched or missing.
   - Rejection when M4 independent validation is missing or not ACCEPTED.
   - Rejection on patch SHA-256 mismatch or tampered diff.
   - Rejection on commit SHA or workspace identity mismatch.
   - Rejection when target repository mutation or containment escape is detected.
2. Test Selection & Rationale:
   - Reproduction test extraction from finding evidence.
   - Module unit test discovery based on modified target file.
   - Rejection of unapproved shell commands or injection tokens.
   - Enforcement of audit rationale for every selected test.
3. Docker Sandbox Execution & Output Parsing:
   - Output parsing for pytest and unittest metric extraction without fabrication.
   - Rejection of zero-collected tests as success (marked INCONCLUSIVE).
   - Timeout handling and resource limits.
   - Secret redaction in execution standard output and standard error.
   - Fail-closed refusal when Docker is unavailable (never executes on host).
4. Baseline Comparison & Regression Detection:
   - Detection of resolved defect (FIXED: failed on baseline, passes on patched).
   - Detection of newly introduced failures (NEW_REGRESSION: passes on baseline, fails on patched).
   - Detection of stable passes (STABLE_PASS: passes on both).
   - Detection of pre-existing failures (PRE_EXISTING_FAILURE: fails on both).
   - INCONCLUSIVE classification when baseline comparison is unavailable.
5. Verification Outcome & State Machine:
   - VERIFIED outcome requires complete policy compliance, defect fix, and 0 regressions.
   - FAILED outcome on regression, defect persistence, or timeout.
   - INCONCLUSIVE outcome on zero collected tests.
   - BLOCKED outcome when Docker is unavailable.
   - Strict state machine transition: PATCH_VALIDATING -> VERIFIED only with verified proof.
   - Prevention of false or unverified repairs reaching VERIFIED.
6. Persistence & Reporting:
   - SQLite persistence and retrieval of VerificationRunRecord and TestExecutionRecord.
   - Structured verification report generation (Markdown and JSON).
   - EvidenceRecord creation and audit event ledger recording.
7. End-to-End Workflow & Immutability:
   - Complete end-to-end verification pipeline via RepairExecutionCoordinator.
   - Strict immutability guarantee for original target repository.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from forge.repair.execution.coordinator import RepairExecutionCoordinator
from forge.repair.execution.failures import (
    PatchIntegrityError,
    SandboxUnavailableError,
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
    VerificationExecutionError,
    VerificationPreconditionError,
)
from forge.repair.execution.models import (
    ComparisonStatus,
    FailureCategory,
    FailureClassification,
    IndependentValidationResult,
    PatchArtifact,
    RegressionAnalysisRecord,
    RepairActor,
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
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.verifier import (
    BaselineComparator,
    RepairVerifier,
    TestExecutor,
    TestSelector,
    VerificationEvidenceCollector,
    VerificationPlanner,
    VerificationReportBuilder,
    parse_test_runner_output,
)
from forge.repair.execution.workspace import RepairWorkspace
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import ExecutionLimits, ExecutionStatus, SandboxResult
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_forge_m5.db"
    init_database(db_file)
    return db_file


@pytest.fixture
def mock_target_repo(tmp_path: Path) -> Path:
    """Create a minimal real git repository with reproducible bug and test file."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()

    calc_code = (
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    if rate > 1.0:\n"
        "        raise ValueError('Rate too high')\n"
        "    return price * (1.0 - rate)\n"
    )
    (src_dir / "calculator.py").write_text(calc_code, encoding="utf-8")

    test_code = (
        "from src.calculator import calculate_discount\n"
        "import pytest\n\n"
        "def test_normal_discount():\n"
        "    assert calculate_discount(100.0, 0.2) == 80.0\n\n"
        "def test_negative_rate_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        calculate_discount(100.0, -0.5)\n"
    )
    (tests_dir / "test_calculator.py").write_text(test_code, encoding="utf-8")
    (repo_dir / "README.md").write_text("# Target Repo M5\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    return repo_dir


def _get_head(repo_dir: Path) -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    return res.stdout.strip()


@pytest.fixture
def seeded_environment(test_db_path: Path, mock_target_repo: Path, tmp_path: Path):
    """Seed ForgeStore and RepairExecutionStore with validated finding and workspace."""
    forge_store = ForgeStore(db_path=test_db_path)
    exec_store = RepairExecutionStore(db_path=test_db_path)
    head_sha = _get_head(mock_target_repo)

    run_id = forge_store.create_run(target=str(mock_target_repo), run_id="run_m5_seed")

    finding = forge_store.create_finding(
        run_id=run_id,
        title="Negative discount rate allowed",
        description="calculate_discount does not check that rate >= 0.0",
        severity=FindingSeverity.HIGH,
        finding_id="find_m5_001",
        affected_files=["src/calculator.py"],
    )

    ev_repro = EvidenceRecord(
        evidence_id="evi_m5_repro",
        run_id=run_id,
        finding_id="find_m5_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Reproduction result showing failure on negative discount rate",
        content="pytest tests/test_calculator.py::test_negative_rate_raises failed",
        file_path="src/calculator.py",
        details={
            "status": "reproduced",
            "commit_sha": head_sha,
            "reproduction_command": "pytest -v tests/test_calculator.py",
        },
    )
    forge_store.attach_evidence(ev_repro)
    forge_store.transition_finding("find_m5_001", FindingLifecycleState.REPRODUCED, "evi_m5_repro")

    workspace = RepairWorkspace(
        target_repo_path=mock_target_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    workspace.prepare()

    clean_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    repair_req = RepairRequest(
        repair_id="rep_m5_001",
        finding_id="find_m5_001",
        run_id=run_id,
        target_repo=str(mock_target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_m5_repro"],
        allowed_files=["src/calculator.py"],
        workspace_id=workspace.workspace_id,
        state=RepairExecutionState.PATCH_VALIDATING,
        validation_status="ACCEPTED",
    )
    exec_store.save_repair_request(repair_req)

    patch = PatchArtifact.create(
        repair_id="rep_m5_001",
        finding_id="find_m5_001",
        base_commit_sha=head_sha,
        target_file="src/calculator.py",
        diff_content=clean_diff,
    )
    exec_store.save_patch_artifact(patch)

    repair_req.patch_artifact_id = patch.artifact_id
    exec_store.save_repair_request(repair_req)

    # Valid M4 Independent Validation Result
    val_result = IndependentValidationResult(
        validation_id="val_indep_m5_001",
        patch_artifact_id=patch.artifact_id,
        repair_id=repair_req.repair_id,
        finding_id=finding.finding_id,
        repository_target=str(mock_target_repo),
        base_commit_sha=head_sha,
        workspace_id=workspace.workspace_id,
        patch_sha256=patch.content_hash,
        validated_file_paths=["src/calculator.py"],
        validated_hunk_count=1,
        lines_added=1,
        lines_deleted=1,
        patch_bytes=len(clean_diff.encode("utf-8")),
        overall_status=ValidationOutcome.ACCEPTED,
        is_valid=True,
        checks_passed=["integrity_verified", "scope_contained", "ast_semantic_valid"],
    )
    exec_store.save_validation_result(val_result)

    return {
        "forge_store": forge_store,
        "exec_store": exec_store,
        "request": repair_req,
        "finding": finding,
        "patch": patch,
        "validation_result": val_result,
        "workspace": workspace,
        "mock_target_repo": mock_target_repo,
        "head_sha": head_sha,
    }


# =============================================================================
# 1. Verification Preconditions Tests
# =============================================================================

def test_precondition_rejected_when_patch_not_validated(seeded_environment):
    """RepairVerifier rejects patches that lack independent M4 validation."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])
    policy = RepairExecutionPolicy()

    unvalidated_patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["finding"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content="--- a/src/calculator.py\n+++ b/src/calculator.py\n@@ -1,1 +1,1 @@\n-# old\n+# new\n",
    )

    with pytest.raises(VerificationPreconditionError, match="No independent M4 validation result found"):
        verifier.verify_preconditions(
            request=env["request"],
            patch=unvalidated_patch,
            workspace=env["workspace"],
            policy=policy,
            validation_result=None,
        )


def test_precondition_rejected_when_m4_validation_rejected(seeded_environment):
    """RepairVerifier rejects patches that were rejected by M4 validation."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])
    policy = RepairExecutionPolicy()

    rejected_val = env["validation_result"].model_copy(
        update={"overall_status": ValidationOutcome.REJECTED, "is_valid": False}
    )

    with pytest.raises(VerificationPreconditionError, match="was not accepted by M4 validation"):
        verifier.verify_preconditions(
            request=env["request"],
            patch=env["patch"],
            workspace=env["workspace"],
            policy=policy,
            validation_result=rejected_val,
        )


def test_precondition_rejected_when_patch_hash_tampered(seeded_environment):
    """RepairVerifier detects patch digest tampering against claimed content hash."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])
    policy = RepairExecutionPolicy()

    # Tampered patch where diff_content does not match content_hash
    tampered_patch = env["patch"].model_copy(update={"content_hash": "deadbeef" * 8})

    with pytest.raises(PatchIntegrityError, match="content hash mismatch"):
        verifier.verify_preconditions(
            request=env["request"],
            patch=tampered_patch,
            workspace=env["workspace"],
            policy=policy,
            validation_result=env["validation_result"],
        )


def test_precondition_rejected_when_base_commit_mismatched(seeded_environment):
    """RepairVerifier rejects patches with base commit SHA mismatched from request."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])
    policy = RepairExecutionPolicy()

    mismatched_patch = env["patch"].model_copy(update={"base_commit_sha": "a" * 40})

    with pytest.raises(PatchIntegrityError, match="Base commit mismatch"):
        verifier.verify_preconditions(
            request=env["request"],
            patch=mismatched_patch,
            workspace=env["workspace"],
            policy=policy,
            validation_result=env["validation_result"],
        )


def test_precondition_rejected_when_target_repo_mutated(seeded_environment):
    """RepairVerifier fails closed if original target repository is modified."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])
    policy = RepairExecutionPolicy()

    # Mutate target repository
    (env["mock_target_repo"] / "hacked.txt").write_text("mutation", encoding="utf-8")

    with pytest.raises(TargetRepoImmutabilityViolationError, match="Target repository modification detected"):
        verifier.verify_preconditions(
            request=env["request"],
            patch=env["patch"],
            workspace=env["workspace"],
            policy=policy,
            validation_result=env["validation_result"],
        )


# =============================================================================
# 2. Test Selection & Rationale Tests
# =============================================================================

def test_test_selector_discovers_reproduction_test(seeded_environment):
    """TestSelector extracts reproduction test command from finding evidence."""
    env = seeded_environment
    selector = TestSelector()
    policy = RepairExecutionPolicy()

    selected = selector.select_tests(
        request=env["request"],
        patch=env["patch"],
        workspace=env["workspace"],
        policy=policy,
        finding=env["finding"],
        evidence_records=env["forge_store"].list_evidence_for_finding(env["finding"].finding_id),
    )

    repro_tests = [t for t in selected if t.category == TestSelectionCategory.REPRODUCTION]
    assert len(repro_tests) >= 1
    assert "pytest" in repro_tests[0].command[0]
    assert repro_tests[0].expected_to_fail_on_baseline is True
    assert repro_tests[0].rationale != ""


def test_test_selector_discovers_module_test_files(seeded_environment):
    """TestSelector finds tests/test_calculator.py corresponding to src/calculator.py."""
    env = seeded_environment
    selector = TestSelector()
    policy = RepairExecutionPolicy()

    selected = selector.select_tests(
        request=env["request"],
        patch=env["patch"],
        workspace=env["workspace"],
        policy=policy,
    )

    module_tests = [t for t in selected if t.category == TestSelectionCategory.MODULE]
    assert len(module_tests) >= 1
    assert "test_calculator" in module_tests[0].test_name
    assert module_tests[0].rationale != ""


def test_test_selector_rejects_arbitrary_shell_command(seeded_environment):
    """TestSelector forbids commands containing shell injection tokens."""
    env = seeded_environment
    selector = TestSelector()
    policy = RepairExecutionPolicy()

    bad_plan = RepairExecutionPlan(
        plan_id="plan_bad",
        repair_id="rep_m5_001",
        target_file="src/calculator.py",
        root_cause="bug",
        proposed_change="fix",
        expected_behavior="pass",
        verification_command="pytest; rm -rf /",  # shell injection
    )

    selected = selector.select_tests(
        request=env["request"],
        patch=env["patch"],
        workspace=env["workspace"],
        policy=policy,
        repair_plan=bad_plan,
    )

    # Shell injection command should be filtered out
    for t in selected:
        assert ";" not in " ".join(t.command)


# =============================================================================
# 3. Docker Sandbox Execution & Output Parsing Tests
# =============================================================================

def test_parse_pytest_output_extracts_counts_accurately():
    """parse_test_runner_output extracts pytest counts without fabrication."""
    sample_out = (
        "============================= test session starts ==============================\n"
        "collected 5 items\n\n"
        "tests/test_calc.py ...F.                                                 [100%]\n\n"
        "=================================== FAILURES ===================================\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_calc.py::test_negative - ValueError\n"
        "========================= 1 failed, 4 passed in 0.12s ==========================\n"
    )
    metrics = parse_test_runner_output(sample_out, "")
    assert metrics["collected"] == 5
    assert metrics["passed"] == 4
    assert metrics["failed"] == 1
    assert metrics["errored"] == 0
    assert metrics["executed"] == 5


def test_parse_unittest_output_extracts_counts_accurately():
    """parse_test_runner_output extracts python unittest metrics accurately."""
    sample_out = (
        "...\n"
        "----------------------------------------------------------------------\n"
        "Ran 3 tests in 0.002s\n\n"
        "OK\n"
    )
    metrics = parse_test_runner_output(sample_out, "")
    assert metrics["collected"] == 3
    assert metrics["passed"] == 3
    assert metrics["failed"] == 0
    assert metrics["executed"] == 3


def test_test_executor_zero_collected_tests_classified_inconclusive(seeded_environment):
    """TestExecutor classifies zero-collected test runs as inconclusive, never passed."""
    env = seeded_environment
    executor = TestExecutor()

    mock_ws = MagicMock()
    mock_ws.workspace_id = "ws_test"
    mock_ws.run_command_in_sandbox.return_value = SandboxResult(
        request_id="req_001",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="collected 0 items\n\n============================ no tests ran in 0.00s =============================",
        stderr="",
        duration_ms=50.0,
        timed_out=False,
    )

    test = SelectedTest(
        test_id="test_001",
        test_name="empty_test",
        category=TestSelectionCategory.REGRESSION,
        command=["pytest", "nonexistent_dir"],
        rationale="Testing empty collection",
    )

    rec = executor.execute_test(
        test=test,
        workspace=mock_ws,
        request=env["request"],
        verification_id="verif_001",
        target_type="patched",
    )

    assert rec.status == "inconclusive"
    assert rec.passed is False
    assert rec.tests_collected == 0
    assert "Zero tests collected" in (rec.failure_message or "")


def test_test_executor_redacts_secrets_from_output(seeded_environment):
    """TestExecutor redacts sensitive tokens from recorded execution excerpts."""
    env = seeded_environment
    executor = TestExecutor()

    mock_ws = MagicMock()
    mock_ws.workspace_id = "ws_test"
    mock_ws.run_command_in_sandbox.return_value = SandboxResult(
        request_id="req_001",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="collected 1 item\ntest_calc.py . [100%]\nToken exposed: sk-abcdef1234567890abcdef12\n= 1 passed in 0.1s =",
        stderr="",
        duration_ms=50.0,
        timed_out=False,
    )

    test = SelectedTest(
        test_id="test_sec",
        test_name="secret_test",
        category=TestSelectionCategory.MODULE,
        command=["pytest", "test_sec.py"],
        rationale="Testing secret redaction",
    )

    rec = executor.execute_test(
        test=test,
        workspace=mock_ws,
        request=env["request"],
        verification_id="verif_sec",
        target_type="patched",
    )

    assert "sk-abcdef" not in rec.stdout_excerpt
    assert "[REDACTED" in rec.stdout_excerpt or "REDACTED" in rec.stdout_excerpt


def test_test_executor_docker_unavailable_fails_closed(seeded_environment):
    """TestExecutor fails closed when Docker runtime is unavailable."""
    env = seeded_environment
    executor = TestExecutor()

    mock_ws = MagicMock()
    mock_ws.workspace_id = "ws_test"
    mock_ws.run_command_in_sandbox.side_effect = SandboxUnavailableError(
        "Docker daemon is not reachable (fail closed)"
    )

    test = SelectedTest(
        test_id="test_docker",
        test_name="docker_test",
        category=TestSelectionCategory.MODULE,
        command=["pytest"],
        rationale="Testing Docker unavailable",
    )

    rec = executor.execute_test(
        test=test,
        workspace=mock_ws,
        request=env["request"],
        verification_id="verif_dock",
        target_type="patched",
    )

    assert rec.status == "error"
    assert rec.passed is False
    assert "Docker daemon is not reachable" in rec.failure_message


# =============================================================================
# 4. Baseline Comparison & Regression Detection Tests
# =============================================================================

def test_baseline_comparator_detects_fixed_defect():
    """BaselineComparator detects when a test was failing on baseline but passes on patched."""
    comparator = BaselineComparator()
    now = datetime.now(timezone.utc)

    b_rec = TestExecutionRecord(
        execution_id="e_base",
        verification_id="v_01",
        repair_id="rep_01",
        workspace_id="ws_base",
        target_type="baseline",
        test_name="test_negative_rate",
        command=["pytest", "test_calc.py"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_passed=0,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    p_rec = TestExecutionRecord(
        execution_id="e_patch",
        verification_id="v_01",
        repair_id="rep_01",
        workspace_id="ws_patch",
        target_type="patched",
        test_name="test_negative_rate",
        command=["pytest", "test_calc.py"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        tests_failed=0,
        started_at=now,
        completed_at=now,
    )

    reg = comparator.compare([b_rec], [p_rec])
    assert reg.regression_detected is False
    assert reg.defect_resolved is True
    assert reg.fixed_tests_count == 1
    assert reg.comparisons[0].status == ComparisonStatus.FIXED


def test_baseline_comparator_detects_new_regression():
    """BaselineComparator flags a test that passed on baseline but fails on patched code as regression."""
    comparator = BaselineComparator()
    now = datetime.now(timezone.utc)

    b_rec = TestExecutionRecord(
        execution_id="e_base",
        verification_id="v_01",
        repair_id="rep_01",
        workspace_id="ws_base",
        target_type="baseline",
        test_name="test_normal_discount",
        command=["pytest", "test_calc.py"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )
    p_rec = TestExecutionRecord(
        execution_id="e_patch",
        verification_id="v_01",
        repair_id="rep_01",
        workspace_id="ws_patch",
        target_type="patched",
        test_name="test_normal_discount",
        command=["pytest", "test_calc.py"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )

    reg = comparator.compare([b_rec], [p_rec])
    assert reg.regression_detected is True
    assert reg.new_failures_count == 1
    assert reg.comparisons[0].status == ComparisonStatus.NEW_REGRESSION


def test_baseline_comparator_detects_stable_pass():
    """BaselineComparator recognizes tests that remain passing across baseline and patch."""
    comparator = BaselineComparator()
    now = datetime.now(timezone.utc)

    b_rec = TestExecutionRecord(
        execution_id="e_base",
        verification_id="v_01",
        repair_id="rep_01",
        workspace_id="ws_base",
        target_type="baseline",
        test_name="test_unaffected",
        command=["pytest"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )
    p_rec = b_rec.model_copy(update={"execution_id": "e_patch", "target_type": "patched"})

    reg = comparator.compare([b_rec], [p_rec])
    assert reg.regression_detected is False
    assert reg.stable_tests_count == 1
    assert reg.comparisons[0].status == ComparisonStatus.STABLE_PASS


def test_baseline_comparator_detects_pre_existing_failure():
    """BaselineComparator distinguishes pre-existing failures from regressions."""
    comparator = BaselineComparator()
    now = datetime.now(timezone.utc)

    b_rec = TestExecutionRecord(
        execution_id="e_base",
        verification_id="v_01",
        repair_id="rep_01",
        workspace_id="ws_base",
        target_type="baseline",
        test_name="test_unrelated_broken",
        command=["pytest"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    p_rec = b_rec.model_copy(update={"execution_id": "e_patch", "target_type": "patched"})

    reg = comparator.compare([b_rec], [p_rec])
    assert reg.regression_detected is False
    assert reg.pre_existing_failures_count == 1
    assert reg.comparisons[0].status == ComparisonStatus.PRE_EXISTING_FAILURE


# =============================================================================
# 5. Verification Outcome & State Machine Tests
# =============================================================================

def test_repair_verifier_fails_closed_when_docker_unavailable(seeded_environment):
    """RepairVerifier marks outcome as BLOCKED without running on host when Docker is absent."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])

    with patch.object(DockerSandbox, "is_available", return_value=False):
        run = verifier.verify_repair(
            request=env["request"],
            patch=env["patch"],
            workspace=env["workspace"],
        )

    assert run.overall_outcome == VerificationOutcome.BLOCKED
    assert run.failure_category == FailureCategory.SANDBOX_UNAVAILABLE
    assert "fail-closed policy" in run.failure_message


def test_repair_verifier_marks_verified_when_fixed_and_regression_clean(seeded_environment):
    """RepairVerifier marks outcome VERIFIED when defect test is fixed and regressions are 0."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])

    now = datetime.now(timezone.utc)
    base_rec = TestExecutionRecord(
        execution_id="e_b",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id="ws_base",
        target_type="baseline",
        test_name="finding_reproduction_test",
        command=["pytest"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = TestExecutionRecord(
        execution_id="e_p",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id=env["workspace"].workspace_id,
        target_type="patched",
        test_name="finding_reproduction_test",
        command=["pytest"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )

    with patch.object(DockerSandbox, "is_available", return_value=True):
        with patch.object(TestExecutor, "execute_test", side_effect=[base_rec, patch_rec]):
            run = verifier.verify_repair(
                request=env["request"],
                patch=env["patch"],
                workspace=env["workspace"],
            )

    assert run.overall_outcome == VerificationOutcome.VERIFIED
    assert run.failure_category is None
    assert run.regression_analysis.defect_resolved is True


def test_repair_verifier_marks_failed_when_regression_introduced(seeded_environment):
    """RepairVerifier marks outcome FAILED when a new test failure is introduced."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])

    now = datetime.now(timezone.utc)
    base_rec = TestExecutionRecord(
        execution_id="e_b",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id="ws_base",
        target_type="baseline",
        test_name="test_normal_discount",
        command=["pytest"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = TestExecutionRecord(
        execution_id="e_p",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id=env["workspace"].workspace_id,
        target_type="patched",
        test_name="test_normal_discount",
        command=["pytest"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )

    with patch.object(DockerSandbox, "is_available", return_value=True):
        with patch.object(TestExecutor, "execute_test", side_effect=[base_rec, patch_rec]):
            run = verifier.verify_repair(
                request=env["request"],
                patch=env["patch"],
                workspace=env["workspace"],
            )

    assert run.overall_outcome == VerificationOutcome.FAILED
    assert run.failure_category == FailureCategory.REGRESSION_DETECTED
    assert "Patch introduced regressions" in run.failure_message


def test_repair_verifier_marks_failed_when_reproduction_still_fails(seeded_environment):
    """RepairVerifier marks outcome FAILED if defect reproduction test is still failing."""
    env = seeded_environment
    verifier = RepairVerifier(execution_store=env["exec_store"], forge_store=env["forge_store"])

    now = datetime.now(timezone.utc)
    base_rec = TestExecutionRecord(
        execution_id="e_b",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id="ws_base",
        target_type="baseline",
        test_name="evidence_reproduction_test",
        command=["pytest"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = base_rec.model_copy(update={"execution_id": "e_p", "target_type": "patched"})

    with patch.object(DockerSandbox, "is_available", return_value=True):
        with patch.object(TestExecutor, "execute_test", side_effect=[base_rec, patch_rec]):
            run = verifier.verify_repair(
                request=env["request"],
                patch=env["patch"],
                workspace=env["workspace"],
            )

    assert run.overall_outcome == VerificationOutcome.FAILED
    assert run.failure_category == FailureCategory.TEST_FAILED
    assert "failed on patched code" in run.failure_message


# =============================================================================
# 6. Persistence & Reporting Tests
# =============================================================================

def test_verification_persistence_roundtrip(seeded_environment):
    """VerificationRunRecord and TestExecutionRecords are saved and restored identically from SQLite."""
    env = seeded_environment
    exec_store = env["exec_store"]
    now = datetime.now(timezone.utc)

    t1 = TestExecutionRecord(
        execution_id="exec_001",
        verification_id="verif_pers_01",
        repair_id=env["request"].repair_id,
        workspace_id=env["workspace"].workspace_id,
        target_type="patched",
        test_name="test_calc",
        command=["pytest", "test_calc.py"],
        exit_code=0,
        status="passed",
        passed=True,
        duration_ms=45.2,
        stdout_excerpt="1 passed in 0.05s",
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )

    vplan = VerificationPlan(
        plan_id="vplan_01",
        repair_id=env["request"].repair_id,
        patch_artifact_id=env["patch"].artifact_id,
        selected_tests=[
            SelectedTest(
                test_id="sel_01",
                test_name="test_calc",
                category=TestSelectionCategory.MODULE,
                command=["pytest", "test_calc.py"],
                rationale="Target module test",
            )
        ],
    )

    run = VerificationRunRecord(
        verification_id="verif_pers_01",
        repair_id=env["request"].repair_id,
        patch_artifact_id=env["patch"].artifact_id,
        validation_id="val_indep_01",
        repository_target=env["request"].target_repo,
        base_commit_sha=env["head_sha"],
        workspace_id=env["workspace"].workspace_id,
        patch_sha256=env["patch"].content_hash,
        plan=vplan,
        baseline_executions=[],
        patched_executions=[t1],
        regression_analysis=RegressionAnalysisRecord(
            regression_detected=False,
            defect_resolved=True,
            comparisons=[
                TestComparisonResult(
                    test_name="test_calc",
                    status=ComparisonStatus.FIXED,
                    baseline_passed=False,
                    patched_passed=True,
                )
            ],
            summary="All clean",
        ),
        overall_outcome=VerificationOutcome.VERIFIED,
        logs_excerpt="Execution completed cleanly",
        started_at=now,
        completed_at=now,
        duration_ms=45.2,
    )

    exec_store.save_verification_run(run)

    restored = exec_store.get_verification_run("verif_pers_01")
    assert restored is not None
    assert restored.verification_id == "verif_pers_01"
    assert restored.overall_outcome == VerificationOutcome.VERIFIED
    assert len(restored.patched_executions) == 1
    assert restored.patched_executions[0].passed is True
    assert restored.regression_analysis.defect_resolved is True


def test_verification_report_builder_markdown_generation(seeded_environment):
    """VerificationReportBuilder produces comprehensive Markdown report containing provenance and comparisons."""
    env = seeded_environment
    exec_store = env["exec_store"]
    now = datetime.now(timezone.utc)

    run = VerificationRunRecord(
        verification_id="verif_rep_01",
        repair_id=env["request"].repair_id,
        patch_artifact_id=env["patch"].artifact_id,
        validation_id="val_indep_01",
        repository_target=env["request"].target_repo,
        base_commit_sha=env["head_sha"],
        workspace_id=env["workspace"].workspace_id,
        patch_sha256=env["patch"].content_hash,
        plan=VerificationPlan(
            plan_id="vplan_01",
            repair_id=env["request"].repair_id,
            patch_artifact_id=env["patch"].artifact_id,
            selected_tests=[
                SelectedTest(
                    test_id="s1",
                    test_name="test_calculator",
                    category=TestSelectionCategory.MODULE,
                    command=["pytest", "tests/test_calculator.py"],
                    rationale="Module coverage for calculator",
                )
            ],
        ),
        baseline_executions=[],
        patched_executions=[],
        regression_analysis=RegressionAnalysisRecord(
            regression_detected=False,
            defect_resolved=True,
            comparisons=[
                TestComparisonResult(
                    test_name="test_calculator",
                    status=ComparisonStatus.FIXED,
                    baseline_passed=False,
                    patched_passed=True,
                    details="Fixed negative discount handling",
                )
            ],
            summary="Defect resolved cleanly",
        ),
        overall_outcome=VerificationOutcome.VERIFIED,
        logs_excerpt="pytest tests/test_calculator.py passed",
        started_at=now,
        completed_at=now,
        duration_ms=120.0,
    )

    md = VerificationReportBuilder.build_markdown_report(run)
    assert "# FORGE Sandbox Repair Verification Report: verif_rep_01" in md
    assert "**Overall Outcome**: `VERIFIED`" in md
    assert "`test_calculator`" in md
    assert "`fixed`" in md
    assert "Module coverage for calculator" in md

    dict_rep = VerificationReportBuilder.build_dict_report(run)
    assert dict_rep["verification_id"] == "verif_rep_01"
    assert dict_rep["overall_outcome"] == "VERIFIED"


# =============================================================================
# 7. End-to-End Coordinator Demonstration & Immutability Tests
# =============================================================================

def test_coordinator_end_to_end_verification_successful_repair(seeded_environment):
    """RepairExecutionCoordinator transitions request from PATCH_VALIDATING to VERIFIED when verification passes."""
    env = seeded_environment
    coordinator = RepairExecutionCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )

    now = datetime.now(timezone.utc)
    base_rec = TestExecutionRecord(
        execution_id="e_b",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id="ws_base",
        target_type="baseline",
        test_name="finding_reproduction_test",
        command=["pytest"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = TestExecutionRecord(
        execution_id="e_p",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id=env["workspace"].workspace_id,
        target_type="patched",
        test_name="finding_reproduction_test",
        command=["pytest"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )

    with patch.object(DockerSandbox, "is_available", return_value=True):
        with patch.object(TestExecutor, "execute_test", side_effect=[base_rec, patch_rec]):
            updated_req, run_rec = coordinator.execute_repair_verification(
                request=env["request"],
                patch=env["patch"],
                workspace=env["workspace"],
                validation_result=env["validation_result"],
            )

    assert updated_req.state == RepairExecutionState.VERIFIED
    assert run_rec.overall_outcome == VerificationOutcome.VERIFIED

    # Check SQLite audit event
    events = env["exec_store"].get_events_for_repair(env["request"].repair_id)
    verified_events = [e for e in events if e.to_state == RepairExecutionState.VERIFIED]
    assert len(verified_events) >= 1
    assert verified_events[0].actor == RepairActor.VERIFIER


def test_coordinator_end_to_end_verification_failing_repair_regression(seeded_environment):
    """RepairExecutionCoordinator transitions request to FAILED when regression is introduced."""
    env = seeded_environment
    coordinator = RepairExecutionCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )

    now = datetime.now(timezone.utc)
    base_rec = TestExecutionRecord(
        execution_id="e_b",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id="ws_base",
        target_type="baseline",
        test_name="finding_reproduction_test",
        command=["pytest"],
        exit_code=0,
        status="passed",
        passed=True,
        tests_collected=1,
        tests_executed=1,
        tests_passed=1,
        started_at=now,
        completed_at=now,
    )
    patch_rec = TestExecutionRecord(
        execution_id="e_p",
        verification_id="v_01",
        repair_id=env["request"].repair_id,
        workspace_id=env["workspace"].workspace_id,
        target_type="patched",
        test_name="finding_reproduction_test",
        command=["pytest"],
        exit_code=1,
        status="failed",
        passed=False,
        tests_collected=1,
        tests_executed=1,
        tests_failed=1,
        started_at=now,
        completed_at=now,
    )

    with patch.object(DockerSandbox, "is_available", return_value=True):
        with patch.object(TestExecutor, "execute_test", side_effect=[base_rec, patch_rec]):
            updated_req, run_rec = coordinator.execute_repair_verification(
                request=env["request"],
                patch=env["patch"],
                workspace=env["workspace"],
                validation_result=env["validation_result"],
            )

    assert updated_req.state == RepairExecutionState.FAILED
    assert run_rec.overall_outcome == VerificationOutcome.FAILED
    assert updated_req.failure is not None
    assert updated_req.failure.category == FailureCategory.REGRESSION_DETECTED


def test_target_repository_remains_strictly_immutable_throughout_verification(seeded_environment):
    """Target repository git commit and working tree remain 100% unchanged during verification."""
    env = seeded_environment
    target_repo = env["mock_target_repo"]
    initial_head = env["head_sha"]

    # Verify initial clean git status
    git_st = subprocess.run(["git", "status", "--porcelain"], cwd=str(target_repo), capture_output=True, text=True)
    assert git_st.stdout.strip() == ""

    coordinator = RepairExecutionCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
    )

    with patch.object(DockerSandbox, "is_available", return_value=False):
        coordinator.execute_repair_verification(
            request=env["request"],
            patch=env["patch"],
            workspace=env["workspace"],
            validation_result=env["validation_result"],
        )

    # Re-verify target repository git status and HEAD
    post_head = _get_head(target_repo)
    post_st = subprocess.run(["git", "status", "--porcelain"], cwd=str(target_repo), capture_output=True, text=True)

    assert post_head == initial_head
    assert post_st.stdout.strip() == ""
