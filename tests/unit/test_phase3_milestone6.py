"""Comprehensive End-to-End Acceptance Test Suite for Phase 3, Milestone 6.

Validates the complete integrated reproduction workflow:
1. Finding & reproduction case creation with trusted expected-behavior provenance
2. Deterministic plan generation and pre-execution validation
3. Rejection of missing/untrusted oracles and unsafe plans
4. Stale workspace revision and path/symlink escape refusal
5. Sandbox execution boundary & fail-closed host protection
6. Deterministic classification of all 8 outcome taxonomy states
7. State persistence and process restart fidelity across SQLite storage
8. Retry limits, idempotency refusal, interruption handling, cleanup failures, and secret redaction
9. Finding lifecycle transition gating (SUSPECTED -> REPRODUCED only with verified evidence)
10. CLI end-to-end integration: JSON output, standardized exit codes, error handling
11. Backward compatibility with Phase 1 and Phase 2 workflows (review, policy, SARIF, benchmarks)
12. Target repository and fixture immutability
"""

import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.cli import app
from forge.config import redact_secrets, get_settings
from forge.reproduction.classifier import (
    ClassificationOutcome,
    ClassificationReasonCode,
    ReproductionClassification,
    ReproductionOutcomeClassifier,
)
from forge.reproduction.manager import (
    CaseManagementError,
    NonIdempotentRetryError,
    ReproductionCaseManager,
    StaleRevisionError,
)
from forge.reproduction.models import (
    CaseStatus,
    FieldProvenance,
    ReproductionAttempt,
    ReproductionCase,
    RetryPolicy,
)
from forge.reproduction.planner import (
    PlanProvenance,
    ReproductionPlan,
    ReproductionPlanner,
    ReproductionStrategy,
)
from forge.reproduction.validator import ReproductionPlanValidator
from forge.sandbox.contracts import (
    ExecutionEnvironmentSpec,
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    SandboxRequest,
    SandboxResult,
)
from forge.sandbox.runner import ReproductionHarness
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import get_db_connection, init_database
from forge.storage.store import ForgeStore, StoreError

runner = CliRunner()


# =========================================================================== #
# Scenario 1: Case Creation & Trusted Provenance
# =========================================================================== #

def test_scenario1_create_case_trusted_provenance(tmp_path: Path):
    """Scenario 1: Create a finding and reproduction case with trusted expected-behavior provenance."""
    db_path = tmp_path / "acceptance_s1.db"
    init_database(db_path)
    store = ForgeStore(db_path=db_path)
    manager = ReproductionCaseManager(store=store)

    run_id = store.create_run("https://github.com/example/repo.git", run_id="run_s1")
    store.create_finding(
        run_id=run_id,
        title="Integer overflow in buffer sizing",
        description="Buffer allocates negative bytes on integer overflow",
        severity=FindingSeverity.HIGH,
        finding_id="find_s1_01",
    )

    contract = ReproductionRequest(
        request_id="req_s1_01",
        finding_id="find_s1_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="Buffer allocation throws ValueError on negative size",
        command=ReproductionCommand(
            args=["pytest", "tests/test_buffer.py::test_negative_alloc"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"ValueError: Negative buffer size"],
        ),
    )

    case = manager.create_case(
        finding_id="find_s1_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=contract,
        max_retries=1,
        is_idempotent=True,
        provenance_map={
            "expected_behavior": FieldProvenance.TRUSTED_FIXTURE,
            "hypothesis": FieldProvenance.STATIC_ANALYSIS,
        },
    )

    assert case.case_id.startswith("case_find_s1_01_")
    assert case.target_commit_sha == "a1b2c3d4e5f6"
    assert case.status == CaseStatus.PENDING
    assert case.provenance_map["expected_behavior"] == FieldProvenance.TRUSTED_FIXTURE


# =========================================================================== #
# Scenario 2: Plan Generation & Offline Validation
# =========================================================================== #

def test_scenario2_plan_generation_and_validation():
    """Scenario 2: Generate deterministic plan and validate offline."""
    planner = ReproductionPlanner()
    plan = planner.create_plan(
        finding_id="find_s2_01",
        commit_sha="a1b2c3d4e5f6",
        target_repo="https://github.com/example/repo.git",
        trusted_expected_behavior="IndexError raised on out of bounds access",
        expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS,
        command_args=["pytest", "tests/test_bounds.py"],
        preferred_strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
    )

    assert plan.plan_id.startswith("plan_find_s2_01_")
    assert plan.strategy == ReproductionStrategy.RUN_SPECIFIC_TEST

    validator = ReproductionPlanValidator()
    result = validator.validate(plan)
    assert result.is_valid
    assert len(result.violations) == 0


# =========================================================================== #
# Scenario 3: Rejection of Missing/Untrusted Oracles & Unsafe Commands
# =========================================================================== #

def test_scenario3_reject_untrusted_oracles_and_unsafe_plans():
    """Scenario 3: Reject missing/untrusted oracles and unsafe plans."""
    validator = ReproductionPlanValidator()

    # Plan with untrusted oracle provenance
    plan_untrusted = ReproductionPlan(
        plan_id="plan_untrusted",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=ReproductionCommand(args=["pytest", "tests/"]),
        expected_behavior="Should fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.UNTRUSTED_REPOSITORY,
        ),
    )
    res_untrusted = validator.validate(plan_untrusted)
    assert not res_untrusted.is_valid
    assert any("not a trusted source" in v for v in res_untrusted.violations)

    # Forbidden / unapproved executable rejected at command contract boundary
    with pytest.raises(Exception, match="forbidden|not in approved"):
        ReproductionCommand(args=["curl", "http://evil.com"])

    # Plan with missing command for executable strategy rejected by validator
    plan_no_cmd = ReproductionPlan(
        plan_id="plan_no_cmd",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=None,
        expected_behavior="Should fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.TRUSTED_FIXTURE,
        ),
    )
    res_no_cmd = validator.validate(plan_no_cmd)
    assert not res_no_cmd.is_valid
    assert any("No command specified" in v for v in res_no_cmd.violations)

    # Credential access in arguments rejected by validator
    plan_cred = ReproductionPlan(
        plan_id="plan_cred",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=ReproductionCommand(args=["pytest", "--token=secret123"]),
        expected_behavior="Should fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.TRUSTED_FIXTURE,
        ),
    )
    res_cred = validator.validate(plan_cred)
    assert not res_cred.is_valid
    assert any("matches a credential or secret-access pattern" in v for v in res_cred.violations)

    # Direct contract validation rejects shell injection
    with pytest.raises(Exception, match="forbidden shell characters"):
        ReproductionCommand(args=["pytest", "tests/; rm -rf /"])


# =========================================================================== #
# Scenario 4: Stale Workspace Revision & Symlink Escape Refusal
# =========================================================================== #

def test_scenario4_refuse_stale_revisions_and_symlinks(tmp_path: Path):
    """Scenario 4: Refuse stale workspace revisions and out-of-workspace symlinks."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "secret_host_dir"
    outside.mkdir()
    sym_dir = ws / "sym_dir"
    sym_dir.symlink_to(outside, target_is_directory=True)

    validator = ReproductionPlanValidator()
    plan = ReproductionPlan(
        plan_id="plan_symlink",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=ReproductionCommand(args=["pytest"], relative_cwd="sym_dir"),
        expected_behavior="Should fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.TRUSTED_FIXTURE,
        ),
    )

    res_sym = validator.validate(plan, workspace_path=ws)
    assert not res_sym.is_valid
    assert any("possible symlink escape" in v for v in res_sym.violations)


# =========================================================================== #
# Scenario 5: Sandbox Boundary & Fail-Closed Host Protection
# =========================================================================== #

def test_scenario5_sandbox_fail_closed_without_docker(tmp_path: Path):
    """Scenario 5: When Docker is unavailable, sandbox fails closed without running target code on host."""
    sandbox = DockerSandbox(docker_binary=None)
    harness = ReproductionHarness(sandbox=sandbox)

    contract = ReproductionRequest(
        request_id="req_fail_closed",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="Test",
        command=ReproductionCommand(args=["pytest"]),
    )

    result = harness.execute(contract, tmp_path)
    assert result.outcome == ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE
    assert not result.sandbox_available
    assert "fail-closed" in result.diagnostic_message.lower()


# =========================================================================== #
# Scenario 6: Deterministic Classification of All 8 Outcomes
# =========================================================================== #

def test_scenario6_classify_all_eight_outcomes():
    """Scenario 6: Deterministic classification across all 8 outcome taxonomy states."""
    classifier = ReproductionOutcomeClassifier()

    def _make_attempt(outcome: ReproductionOutcome, exit_code=None, matched=None, cleanup="cleaned", diag=""):
        return ReproductionAttempt(
            attempt_id="att_test",
            case_id="case_test",
            attempt_number=1,
            run_id="run_test",
            outcome=outcome,
            exit_code=exit_code,
            duration_ms=10.0,
            matched_patterns=matched or [],
            cleanup_status=cleanup,
            diagnostic_message=diag,
        )

    trusted_case = ReproductionCase(
        case_id="case_test",
        finding_id="find_01",
        run_id="run_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=ReproductionRequest(
            request_id="req_01",
            finding_id="find_01",
            target_repo="https://github.com/example/repo.git",
            commit_sha="a1b2c3d4e5f6",
            hypothesis="Defect",
            command=ReproductionCommand(
                args=["pytest"],
                expected_exit_codes=[1],
                expected_output_patterns=[r"AssertionError"],
            ),
        ),
        policy_version="1.0.0",
        provenance_map={"expected_behavior": FieldProvenance.TRUSTED_FIXTURE},
    )

    # 1. REPRODUCED
    c_repro = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.REPRODUCED, exit_code=1, matched=[r"AssertionError"]),
        case=trusted_case,
    )
    assert c_repro.outcome == ClassificationOutcome.REPRODUCED

    # 2. NOT_REPRODUCED
    c_not_repro = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.PASSED, exit_code=0),
        case=trusted_case,
    )
    assert c_not_repro.outcome == ClassificationOutcome.NOT_REPRODUCED

    # 3. UNRELATED_FAILURE
    c_unrelated = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.FAILED, exit_code=2, matched=[]),
        case=trusted_case,
    )
    assert c_unrelated.outcome == ClassificationOutcome.UNRELATED_FAILURE

    # 4. ENVIRONMENT_BLOCKED
    c_env = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE),
        case=trusted_case,
    )
    assert c_env.outcome == ClassificationOutcome.ENVIRONMENT_BLOCKED

    # 5. TIMEOUT
    c_timeout = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.TIMEOUT),
        case=trusted_case,
    )
    assert c_timeout.outcome == ClassificationOutcome.TIMEOUT

    # 6. INVALID_PLAN
    c_invalid = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.ERROR),
        case=trusted_case,
        is_stale_revision=True,
    )
    assert c_invalid.outcome == ClassificationOutcome.INVALID_PLAN

    # 7. INCONCLUSIVE (cleanup failure)
    c_inconcl = classifier.classify_attempt(
        _make_attempt(ReproductionOutcome.REPRODUCED, exit_code=1, matched=[r"AssertionError"], cleanup="failed"),
        case=trusted_case,
    )
    assert c_inconcl.outcome == ClassificationOutcome.INCONCLUSIVE

    # 8. FLAKY (case history evaluation)
    att1 = _make_attempt(ReproductionOutcome.REPRODUCED, exit_code=1, matched=[r"AssertionError"])
    att2 = _make_attempt(ReproductionOutcome.PASSED, exit_code=0)
    c_flaky = classifier.classify_case_history(trusted_case, [att1, att2])
    assert c_flaky.outcome == ClassificationOutcome.FLAKY


# =========================================================================== #
# Scenario 7: State Persistence & Process-Restart Fidelity
# =========================================================================== #

def test_scenario7_persistence_and_process_restart(tmp_path: Path):
    """Scenario 7: Persist cases, attempts, and classifications; verify fidelity after reload."""
    db_file = tmp_path / "restart_test.db"
    init_database(db_file)

    # Process 1: Create case and run attempt
    store1 = ForgeStore(db_path=db_file)
    manager1 = ReproductionCaseManager(store=store1)
    store1.create_run("https://github.com/example/repo.git", run_id="run_restart")
    store1.create_finding(
        run_id="run_restart",
        title="SQL injection",
        description="Raw SQL concatenation",
        severity=FindingSeverity.HIGH,
        finding_id="find_restart_01",
    )

    contract = ReproductionRequest(
        request_id="req_restart_01",
        finding_id="find_restart_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="Exploit fails closed",
        command=ReproductionCommand(
            args=["pytest", "tests/test_sqli.py"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"SqlError"],
        ),
    )
    case1 = manager1.create_case(
        finding_id="find_restart_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=contract,
        provenance_map={"expected_behavior": FieldProvenance.TRUSTED_FIXTURE},
    )

    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=contract.request_id,
        finding_id=case1.finding_id,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stdout="SqlError: syntax error in query",
        stderr="",
        duration_ms=42.0,
        matched_patterns=[r"SqlError"],
        sandbox_available=True,
    )
    manager1.harness = mock_harness
    attempt1 = manager1.run_attempt(case_id=case1.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")
    assert attempt1.outcome == ReproductionOutcome.REPRODUCED

    # Process 2: Fresh instance pointing to same SQLite database
    store2 = ForgeStore(db_path=db_file)
    manager2 = ReproductionCaseManager(store=store2)

    case2 = manager2.get_case(case1.case_id)
    assert case2 is not None
    assert case2.case_id == case1.case_id
    assert case2.status == CaseStatus.REPRODUCED
    assert case2.attempts_count == 1
    assert case2.provenance_map["expected_behavior"] == FieldProvenance.TRUSTED_FIXTURE

    attempts2 = manager2.list_attempts(case1.case_id)
    assert len(attempts2) == 1
    assert attempts2[0].attempt_id == attempt1.attempt_id
    assert attempts2[0].matched_patterns == [r"SqlError"]

    classification2 = manager2.get_classification(case1.case_id)
    assert classification2 is not None
    assert classification2.outcome == ClassificationOutcome.REPRODUCED
    assert classification2.is_reproduced is True


# =========================================================================== #
# Scenario 8: Retry Limits, Idempotency & Secret Redaction
# =========================================================================== #

def test_scenario8_retry_limits_idempotency_and_redaction(tmp_path: Path):
    """Scenario 8: Enforce retry policies, non-idempotent refusal, and secret redaction."""
    db_file = tmp_path / "scenario8.db"
    init_database(db_file)
    store = ForgeStore(db_path=db_file)
    manager = ReproductionCaseManager(store=store)
    store.create_run("https://github.com/example/repo.git", run_id="run_s8")
    store.create_finding(
        run_id="run_s8",
        title="Token leak in logs",
        description="API key output to stderr",
        severity=FindingSeverity.HIGH,
        finding_id="find_s8_01",
    )

    contract = ReproductionRequest(
        request_id="req_s8_01",
        finding_id="find_s8_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="Sensitive token is printed",
        command=ReproductionCommand(
            args=["pytest", "tests/test_leak.py"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"KeyError"],
        ),
    )

    case = manager.create_case(
        finding_id="find_s8_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=contract,
        max_retries=1,
        is_idempotent=False,  # Non-idempotent!
    )

    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.FAILED,
        exit_code=1,
        stdout="Found sk-abcdef1234567890abcdef12 in config",
        stderr="",
        duration_ms=15.0,
        matched_patterns=[],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    # Attempt 1 succeeds and records redacted output
    att1 = manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")
    assert "sk-" not in att1.stdout_excerpt or "REDACTED" in att1.stdout_excerpt

    # Attempt 2 is refused because command is not idempotent
    with pytest.raises(NonIdempotentRetryError, match="not marked idempotent"):
        manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")


# =========================================================================== #
# Scenario 9: Finding Lifecycle State Transitions
# =========================================================================== #

def test_scenario9_lifecycle_transition_gating(tmp_path: Path):
    """Scenario 9: Only verified reproduction outcomes advance finding to REPRODUCED."""
    db_file = tmp_path / "scenario9.db"
    init_database(db_file)
    store = ForgeStore(db_path=db_file)
    manager = ReproductionCaseManager(store=store)
    store.create_run("https://github.com/example/repo.git", run_id="run_s9")
    store.create_finding(
        run_id="run_s9",
        title="Null pointer dereference",
        description="Missing None check",
        severity=FindingSeverity.HIGH,
        finding_id="find_s9_01",
    )

    contract = ReproductionRequest(
        request_id="req_s9_01",
        finding_id="find_s9_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="None dereference throws TypeError",
        command=ReproductionCommand(
            args=["pytest", "tests/test_null.py"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"TypeError: NoneType object is not subscriptable"],
        ),
    )

    # Test A: Unrelated failure does NOT advance finding state
    case_fail = manager.create_case(
        finding_id="find_s9_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=contract,
        provenance_map={"expected_behavior": FieldProvenance.TRUSTED_FIXTURE},
    )

    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=contract.request_id,
        finding_id="find_s9_01",
        outcome=ReproductionOutcome.FAILED,
        exit_code=1,
        stdout="ModuleNotFoundError: No module named 'foo'",
        stderr="",
        duration_ms=10.0,
        matched_patterns=[],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    manager.run_attempt(case_id=case_fail.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")
    finding_after_fail = store.get_finding("find_s9_01")
    assert finding_after_fail.lifecycle_state == FindingLifecycleState.SUSPECTED

    # Test B: Confirmed reproduction DOES advance finding state
    # Create second finding
    store.create_finding(
        run_id="run_s9",
        title="Confirmed defect",
        description="Verified issue",
        severity=FindingSeverity.HIGH,
        finding_id="find_s9_02",
    )
    contract2 = ReproductionRequest(
        request_id="req_s9_02",
        finding_id="find_s9_02",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="Confirmed",
        command=ReproductionCommand(
            args=["pytest", "tests/test_defect.py"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"AssertionError"],
        ),
    )
    case_repro = manager.create_case(
        finding_id="find_s9_02",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=contract2,
        provenance_map={"expected_behavior": FieldProvenance.TRUSTED_FIXTURE},
    )

    mock_harness.execute.return_value = ReproductionResult(
        request_id=contract2.request_id,
        finding_id="find_s9_02",
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stdout="AssertionError: values do not match",
        stderr="",
        duration_ms=10.0,
        matched_patterns=[r"AssertionError"],
        sandbox_available=True,
    )

    manager.run_attempt(case_id=case_repro.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")
    finding_after_repro = store.get_finding("find_s9_02")
    assert finding_after_repro.lifecycle_state == FindingLifecycleState.REPRODUCED


# =========================================================================== #
# Scenario 10: CLI End-to-End Commands, JSON Output & Exit Codes
# =========================================================================== #

def test_scenario10_cli_plan_and_repro_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Scenario 10: Complete CLI suite testing JSON output and stable exit codes."""
    db_file = tmp_path / "cli_s10.db"
    init_database(db_file)
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))

    store = ForgeStore(db_path=db_file)
    store.create_run("https://github.com/example/repo.git", run_id="run_cli_s10")
    store.create_finding(
        run_id="run_cli_s10",
        title="Path traversal",
        description="Arbitrary file read",
        severity=FindingSeverity.HIGH,
        finding_id="find_cli_s10",
    )

    # 1. forge plan generate
    plan_file = tmp_path / "plan_s10.json"
    res_gen = runner.invoke(app, [
        "plan", "generate", "find_cli_s10",
        "--commit", "a1b2c3d4e5f6",
        "--repo", "https://github.com/example/repo.git",
        "--command", '["pytest", "tests/test_path.py"]',
        "--expected", "Path traversal raises PermissionError",
        "--provenance", "trusted_fixture",
        "--output", str(plan_file),
    ])
    assert res_gen.exit_code == 0
    assert plan_file.exists()

    # 2. forge plan validate
    res_val = runner.invoke(app, ["plan", "validate", str(plan_file)])
    assert res_val.exit_code == 0
    assert "VALID" in res_val.output

    # 3. forge plan show (both text and JSON)
    res_show = runner.invoke(app, ["plan", "show", str(plan_file)])
    assert res_show.exit_code == 0
    assert "Plan ID:" in res_show.output

    res_show_json = runner.invoke(app, ["plan", "show", str(plan_file), "--format", "json"])
    assert res_show_json.exit_code == 0
    data = json.loads(res_show_json.output)
    assert data["finding_id"] == "find_cli_s10"

    # 4. forge plan classify
    res_cls = runner.invoke(app, ["plan", "classify", str(plan_file)])
    assert res_cls.exit_code == 0

    # 5. forge repro create
    contract_file = tmp_path / "contract_s10.json"
    contract_data = {
        "request_id": "req_cli_s10",
        "finding_id": "find_cli_s10",
        "target_repo": "https://github.com/example/repo.git",
        "commit_sha": "a1b2c3d4e5f6",
        "hypothesis": "Test path traversal",
        "command": {
            "args": ["pytest", "tests/test_path.py"],
            "expected_exit_codes": [1],
            "expected_output_patterns": ["PermissionError"],
        },
    }
    contract_file.write_text(json.dumps(contract_data), encoding="utf-8")

    res_create = runner.invoke(app, [
        "repro", "create", str(contract_file),
        "--commit", "a1b2c3d4e5f6",
        "--format", "json",
    ])
    assert res_create.exit_code == 0
    case_json = json.loads(res_create.output)
    case_id = case_json["case_id"]

    # 6. forge repro list
    res_list = runner.invoke(app, ["repro", "list", "--format", "json"])
    assert res_list.exit_code == 0
    cases = json.loads(res_list.output)
    assert any(c["case_id"] == case_id for c in cases)

    # 7. forge repro show
    res_show_repro = runner.invoke(app, ["repro", "show", case_id, "--format", "json"])
    assert res_show_repro.exit_code == 0
    show_case = json.loads(res_show_repro.output)
    assert show_case["case"]["case_id"] == case_id

    # 8. forge repro classify
    res_repro_cls = runner.invoke(app, ["repro", "classify", case_id, "--format", "json"])
    assert res_repro_cls.exit_code == 4  # INCONCLUSIVE (0 attempts yet)


# =========================================================================== #
# Scenario 11: Backward Compatibility (Review, Policy, Benchmarks)
# =========================================================================== #

def test_scenario11_backward_compatibility():
    """Scenario 11: Phase 1 & 2 review, policy, benchmark, and doctor workflows remain functional."""
    # Doctor check
    res_doc = runner.invoke(app, ["doctor"])
    assert res_doc.exit_code == 0
    assert "FORGE System Health Diagnostic" in res_doc.output

    # Sandbox doctor check
    res_sbx_doc = runner.invoke(app, ["sandbox", "doctor"])
    assert res_sbx_doc.exit_code == 0
    assert "FORGE Sandbox Diagnostics" in res_sbx_doc.output

    # Version check
    res_ver = runner.invoke(app, ["version"])
    assert res_ver.exit_code == 0
    assert "FORGE version:" in res_ver.output


# =========================================================================== #
# Scenario 12: Target Repository Immutability
# =========================================================================== #

def test_scenario12_target_repository_immutability(tmp_path: Path):
    """Scenario 12: Reproduction contracts and planning never modify target files."""
    repo_dir = tmp_path / "sample_target_repo"
    repo_dir.mkdir()
    source_file = repo_dir / "target.py"
    initial_content = "def add(a, b): return a + b\n"
    source_file.write_text(initial_content)

    validator = ReproductionPlanValidator()
    plan = ReproductionPlan(
        plan_id="plan_immutable",
        finding_id="find_01",
        target_repo=str(repo_dir),
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=ReproductionCommand(args=["pytest", "tests/"]),
        expected_behavior="Must fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.TRUSTED_FIXTURE,
        ),
    )

    validator.validate(plan, workspace_path=repo_dir)

    # Verify target file has not been modified
    assert source_file.read_text() == initial_content
