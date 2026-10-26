"""Comprehensive test suite for Phase 3, Milestone 5: Reproduction Reliability and Regression Hardening.

Covers:
1. Concurrency, Transaction Atomicity & Partial Write Rollback
2. Stale Revision & TOCTOU Detection
3. Symlink Escape & Path Containment
4. Execution Robustness & Head/Tail Output Bounding
5. Idempotency & Retry Budget Enforcement
6. Untrusted Oracle & Classification Integrity
7. CLI Integration & Standardized Exit Codes
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.cli import app
from forge.config import redact_secrets
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
# Fixtures
# =========================================================================== #

@pytest.fixture
def test_store(tmp_path: Path) -> ForgeStore:
    db_file = tmp_path / "test_m5.db"
    init_database(db_file)
    store = ForgeStore(db_path=db_file)
    store.create_run(target="https://github.com/example/repo.git", run_id="run_m5")
    store.create_finding(
        run_id="run_m5",
        title="Sample finding",
        description="Vulnerability description",
        severity=FindingSeverity.HIGH,
        finding_id="find_m5_01",
    )
    return store


@pytest.fixture
def sample_contract() -> ReproductionRequest:
    return ReproductionRequest(
        request_id="req_m5_01",
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="Defect reproduces with non-zero exit under pytest",
        command=ReproductionCommand(
            args=["pytest", "tests/test_defect.py"],
            expected_exit_codes=[1],
            expected_output_patterns=[r"AssertionError"],
        ),
        environment=ExecutionEnvironmentSpec(timeout_seconds=30.0),
    )


# =========================================================================== #
# 1. Concurrency, Transaction Atomicity & Partial Write Rollback
# =========================================================================== #

def test_duplicate_case_creation_rejected(test_store: ForgeStore, sample_contract: ReproductionRequest):
    """Creating a duplicate case raises CaseManagementError and does not corrupt store."""
    manager = ReproductionCaseManager(store=test_store)
    case1 = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )
    assert case1.case_id is not None

    with pytest.raises(CaseManagementError, match="already exists"):
        manager.create_case(
            finding_id="find_m5_01",
            target_repo="https://github.com/example/repo.git",
            target_commit_sha="a1b2c3d4e5f6",
            contract=sample_contract,
        )


def test_unique_attempt_number_constraint(test_store: ForgeStore, sample_contract: ReproductionRequest):
    """Duplicate attempt numbers for the same case cannot be committed to SQLite."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    conn = get_db_connection(test_store.db_path)
    try:
        # First attempt insert succeeds
        with conn:
            conn.execute(
                """
                INSERT INTO reproduction_attempts (
                    attempt_id, case_id, attempt_number, run_id, outcome, exit_code,
                    duration_ms, stdout_excerpt, stderr_excerpt, cleanup_status, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("att_1", case.case_id, 1, "run_m5", "reproduced", 1, 10.0, "", "", "cleaned", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
            )

        # Duplicate attempt_number for same case fails with sqlite3.IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    """
                    INSERT INTO reproduction_attempts (
                        attempt_id, case_id, attempt_number, run_id, outcome, exit_code,
                        duration_ms, stdout_excerpt, stderr_excerpt, cleanup_status, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("att_2", case.case_id, 1, "run_m5", "passed", 0, 10.0, "", "", "cleaned", "2026-01-01T00:00:02Z", "2026-01-01T00:00:03Z"),
                )
    finally:
        conn.close()


def test_atomic_rollback_on_transaction_failure(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """If an integrity error occurs during attempt persistence, all partial writes are rolled back."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    # Pre-insert an attempt with attempt_number 1 to force a conflict in run_attempt
    conn = get_db_connection(test_store.db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO reproduction_attempts (
                    attempt_id, case_id, attempt_number, run_id, outcome, exit_code,
                    duration_ms, stdout_excerpt, stderr_excerpt, cleanup_status, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("att_pre", case.case_id, 1, "run_m5", "reproduced", 1, 10.0, "", "", "cleaned", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
            )
    finally:
        conn.close()

    # Configure mock harness
    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=sample_contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stdout="AssertionError: failed",
        stderr="",
        duration_ms=50.0,
        matched_patterns=[r"AssertionError"],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    # Intentionally trigger an attempt that will attempt to write attempt_number 1
    with patch.object(manager, "list_attempts", return_value=[]):
        with pytest.raises(CaseManagementError, match="integrity conflict"):
            manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    # Verify no evidence record was leaked for this failed attempt
    evidence_list = test_store.list_evidence_for_finding("find_m5_01")
    assert len(evidence_list) == 0


# =========================================================================== #
# 2. Stale Revision & TOCTOU Detection
# =========================================================================== #

def test_stale_revision_rejected_in_run_attempt(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """Mismatched commit SHA raises StaleRevisionError before execution."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    with pytest.raises(StaleRevisionError, match="Repository revision mismatch"):
        manager.run_attempt(
            case_id=case.case_id,
            workspace_path=tmp_path,
            current_commit_sha="different_commit_9999",
        )


def test_automatic_git_revision_detection_from_workspace(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """When current_commit_sha is None, manager inspects git repository in workspace."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    # Mock a .git directory in workspace
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    with patch("forge.repository.acquisition.run_git_command") as mock_git:
        # Simulate workspace git repo at a divergent commit
        mock_git.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="deadbeef0000\n", stderr="")
        with pytest.raises(StaleRevisionError, match="Repository revision mismatch"):
            manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha=None)


def test_validator_rejects_git_workspace_revision_mismatch(tmp_path: Path):
    """Plan validator rejects execution against workspace with mismatched git revision."""
    validator = ReproductionPlanValidator()
    plan = ReproductionPlan(
        plan_id="plan_test_rev",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="111122223333",
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

    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    with patch("forge.repository.acquisition.run_git_command") as mock_git:
        mock_git.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="999988887777\n", stderr="")
        result = validator.validate(plan, workspace_path=tmp_path)
        assert not result.is_valid
        assert any("stale revision mismatch" in v.lower() for v in result.violations)


# =========================================================================== #
# 3. Symlink Escape & Path Containment
# =========================================================================== #

def test_validator_rejects_symlink_cwd_escaping_workspace(tmp_path: Path):
    """Validator rejects relative_cwd that escapes workspace via symlink."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside_secret"
    outside.mkdir()

    # Symlink inside workspace pointing outside
    escape_link = ws / "sym_escape"
    escape_link.symlink_to(outside, target_is_directory=True)

    validator = ReproductionPlanValidator()
    plan = ReproductionPlan(
        plan_id="plan_escape",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=ReproductionCommand(args=["pytest"], relative_cwd="sym_escape"),
        expected_behavior="Must fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.TRUSTED_FIXTURE,
        ),
    )

    result = validator.validate(plan, workspace_path=ws)
    assert not result.is_valid
    assert any("possible symlink escape" in v for v in result.violations)


def test_validator_rejects_symlink_argument_escaping_workspace(tmp_path: Path):
    """Validator rejects arguments that are symlinks pointing outside workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target_outside = tmp_path / "outside_file.py"
    target_outside.write_text("print('outside')")

    arg_symlink = ws / "link_to_outside.py"
    arg_symlink.symlink_to(target_outside)

    validator = ReproductionPlanValidator()
    plan = ReproductionPlan(
        plan_id="plan_arg_symlink",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
        strategy_rationale="Test",
        command=ReproductionCommand(args=["python", "link_to_outside.py"]),
        expected_behavior="Must fail",
        provenance=PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            command_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=FieldProvenance.TRUSTED_FIXTURE,
        ),
    )

    result = validator.validate(plan, workspace_path=ws)
    assert not result.is_valid
    assert any("is a symlink resolving" in v and "outside workspace" in v for v in result.violations)


# =========================================================================== #
# 4. Execution Robustness & Head/Tail Output Bounding
# =========================================================================== #

def test_dockersandbox_head_tail_preservation(tmp_path: Path):
    """DockerSandbox preserves both head and tail of large output with omitted byte marker."""
    sandbox = DockerSandbox(docker_binary="/usr/bin/docker")

    with patch.object(sandbox, "check_health", return_value=(True, "Docker daemon reachable")):
        mock_proc = MagicMock()
        # Head has START, Tail has END
        huge_output = b"START_OF_OUTPUT " + (b"X" * 10_000) + b" END_OF_OUTPUT"
        mock_proc.communicate.return_value = (huge_output, b"")
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):
                req = SandboxRequest(
                    request_id="req_head_tail",
                    run_id="run_001",
                    workspace_path=tmp_path.resolve(),
                    command=["pytest"],
                    limits=ExecutionLimits(max_stdout_bytes=1000),
                )
                res = sandbox.run(req)

                assert res.stdout_truncated is True
                assert "START_OF_OUTPUT" in res.stdout
                assert "END_OF_OUTPUT" in res.stdout
                assert "[TRUNCATED: Output byte limit reached;" in res.stdout


def test_manager_head_tail_excerpt_bounding(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """Manager bounds stdout excerpt while preserving both head and tail within 5000 chars."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    huge_stdout = "HEAD_MARKER " + ("Z" * 20_000) + " TAIL_MARKER AssertionError"
    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=sample_contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stdout=huge_stdout,
        stderr="",
        duration_ms=10.0,
        matched_patterns=[r"AssertionError"],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    attempt = manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    assert attempt.stdout_truncated is True
    assert len(attempt.stdout_excerpt) <= 5000
    assert "HEAD_MARKER" in attempt.stdout_excerpt
    assert "TAIL_MARKER" in attempt.stdout_excerpt
    assert "[TRUNCATED:" in attempt.stdout_excerpt


def test_cleanup_failure_classified_as_inconclusive(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """When workspace cleanup status indicates failure, attempt is classified INCONCLUSIVE."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    # Simulate harness throwing an exception that marks cleanup status error
    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.side_effect = RuntimeError("Disk full during cleanup")
    manager.harness = mock_harness

    attempt = manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    assert attempt.cleanup_status == "error_in_cleanup"
    classification = manager.get_classification(case.case_id)
    assert classification.outcome == ClassificationOutcome.INCONCLUSIVE
    assert classification.reason_code == ClassificationReasonCode.CLEANUP_FAILURE


def test_interruption_classified_as_execution_interrupted():
    """Classifier assigns EXECUTION_INTERRUPTED when attempt is interrupted."""
    classifier = ReproductionOutcomeClassifier()
    attempt = ReproductionAttempt(
        attempt_id="att_int_01",
        case_id="case_int_01",
        attempt_number=1,
        run_id="run_01",
        outcome=ReproductionOutcome.INCONCLUSIVE,
        exit_code=None,
        cleanup_status="interrupted",
        diagnostic_message="Execution interrupted by SIGINT",
    )

    cls = classifier.classify_attempt(attempt)
    assert cls.outcome == ClassificationOutcome.INCONCLUSIVE
    assert cls.reason_code == ClassificationReasonCode.EXECUTION_INTERRUPTED


# =========================================================================== #
# 5. Idempotency & Retry Budget Enforcement
# =========================================================================== #

def test_non_idempotent_retry_refusal(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """Non-idempotent commands refuse a second attempt."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
        max_retries=2,
        is_idempotent=False,
    )

    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=sample_contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.FAILED,
        exit_code=1,
        stdout="Test failed",
        stderr="",
        duration_ms=10.0,
        matched_patterns=[],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    # First attempt succeeds
    manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    # Second attempt refused because is_idempotent=False
    with pytest.raises(NonIdempotentRetryError, match="not marked idempotent"):
        manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")


def test_retry_budget_exhaustion(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """Exceeding max_retries refuses further attempts."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
        max_retries=1,  # Allows attempt 1 + 1 retry = 2 attempts total
        is_idempotent=True,
    )

    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=sample_contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.FAILED,
        exit_code=1,
        stdout="failed",
        stderr="",
        duration_ms=5.0,
        matched_patterns=[],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    # Attempt 1
    manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")
    # Attempt 2 (retry 1)
    manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    # Attempt 3 (exceeds budget)
    with pytest.raises(CaseManagementError, match="exceeded max attempts"):
        manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")


# =========================================================================== #
# 6. Untrusted Oracle & Classification Integrity
# =========================================================================== #

def test_untrusted_oracle_nonzero_exit_never_reproduced(test_store: ForgeStore, tmp_path: Path):
    """A non-zero exit under an untrusted oracle classifies as UNRELATED_FAILURE, never REPRODUCED."""
    classifier = ReproductionOutcomeClassifier()
    case = ReproductionCase(
        case_id="case_untrusted_oracle",
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
            command=ReproductionCommand(args=["pytest"], expected_exit_codes=[1]),
        ),
        policy_version="1.0.0",
        provenance_map={"expected_behavior": FieldProvenance.UNTRUSTED_REPOSITORY},
    )

    attempt = ReproductionAttempt(
        attempt_id="att_untrusted",
        case_id=case.case_id,
        attempt_number=1,
        run_id="run_01",
        outcome=ReproductionOutcome.FAILED,
        exit_code=1,
        duration_ms=10.0,
        stdout_excerpt="SyntaxError: invalid syntax",
        cleanup_status="cleaned",
    )

    cls = classifier.classify_attempt(attempt, case=case)
    assert cls.outcome == ClassificationOutcome.UNRELATED_FAILURE
    assert cls.reason_code == ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED
    assert not cls.is_reproduced


def test_finding_transition_blocked_by_untrusted_oracle(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """Finding lifecycle state remains SUSPECTED when oracle is untrusted."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
        provenance_map={"expected_behavior": FieldProvenance.UNTRUSTED_REPOSITORY},
    )

    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=sample_contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stdout="AssertionError: failed",
        stderr="",
        duration_ms=10.0,
        matched_patterns=[r"AssertionError"],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    # Finding should NOT transition to REPRODUCED because oracle is untrusted
    finding = test_store.get_finding("find_m5_01")
    assert finding.lifecycle_state == FindingLifecycleState.SUSPECTED


def test_secret_redaction_in_classification_and_evidence(test_store: ForgeStore, sample_contract: ReproductionRequest, tmp_path: Path):
    """Secrets present in output or diagnostics are redacted from evidence and classification records."""
    manager = ReproductionCaseManager(store=test_store)
    case = manager.create_case(
        finding_id="find_m5_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    raw_output = "Error connecting with secret sk-1234567890abcdef1234567890abcdef\nAssertionError"
    mock_harness = MagicMock(spec=ReproductionHarness)
    mock_harness.execute.return_value = ReproductionResult(
        request_id=sample_contract.request_id,
        finding_id=case.finding_id,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stdout=raw_output,
        stderr="",
        duration_ms=10.0,
        matched_patterns=[r"AssertionError"],
        sandbox_available=True,
    )
    manager.harness = mock_harness

    attempt = manager.run_attempt(case_id=case.case_id, workspace_path=tmp_path, current_commit_sha="a1b2c3d4e5f6")

    assert "sk-" not in attempt.stdout_excerpt or "REDACTED" in attempt.stdout_excerpt
    evidence = test_store.get_evidence(attempt.evidence_id)
    assert "sk-" not in evidence.content or "REDACTED" in evidence.content


# =========================================================================== #
# 7. CLI Integration & Standardized Exit Codes
# =========================================================================== #

def test_cli_repro_run_standardized_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_contract: ReproductionRequest):
    """forge repro run yields standardized exit codes for each execution outcome."""
    db_file = tmp_path / "cli_repro.db"
    init_database(db_file)
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))

    store = ForgeStore(db_path=db_file)
    store.create_run("https://github.com/example/repo.git", run_id="run_cli")
    store.create_finding(
        run_id="run_cli",
        title="Sample",
        description="Desc",
        severity=FindingSeverity.HIGH,
        finding_id="find_cli_01",
    )
    manager = ReproductionCaseManager(store=store)
    case = manager.create_case(
        finding_id="find_cli_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=sample_contract,
    )

    mock_acquired = MagicMock()
    mock_acquired.workspace_path = tmp_path
    mock_acquired.resolved_commit_sha = "a1b2c3d4e5f6"

    # Test Outcome: REPRODUCED -> exit code 0
    with patch("forge.repository.acquire_repository", return_value=mock_acquired):
        with patch("forge.repository.cleanup_repository"):
            with patch.object(ReproductionCaseManager, "run_attempt") as mock_run:
                mock_run.return_value = ReproductionAttempt(
                    attempt_id="att_01",
                    case_id=case.case_id,
                    attempt_number=1,
                    run_id="run_cli",
                    outcome=ReproductionOutcome.REPRODUCED,
                    exit_code=1,
                    duration_ms=10.0,
                )
                res0 = runner.invoke(app, ["repro", "run", case.case_id])
                assert res0.exit_code == 0

    # Test Outcome: NOT_REPRODUCED -> exit code 1
    with patch("forge.repository.acquire_repository", return_value=mock_acquired):
        with patch("forge.repository.cleanup_repository"):
            with patch.object(ReproductionCaseManager, "run_attempt") as mock_run:
                mock_run.return_value = ReproductionAttempt(
                    attempt_id="att_02",
                    case_id=case.case_id,
                    attempt_number=1,
                    run_id="run_cli",
                    outcome=ReproductionOutcome.NOT_REPRODUCED,
                    exit_code=0,
                    duration_ms=10.0,
                )
                res1 = runner.invoke(app, ["repro", "run", case.case_id])
                assert res1.exit_code == 1

    # Test Outcome: SKIPPED_SANDBOX_UNAVAILABLE -> exit code 3
    with patch("forge.repository.acquire_repository", return_value=mock_acquired):
        with patch("forge.repository.cleanup_repository"):
            with patch.object(ReproductionCaseManager, "run_attempt") as mock_run:
                mock_run.return_value = ReproductionAttempt(
                    attempt_id="att_03",
                    case_id=case.case_id,
                    attempt_number=1,
                    run_id="run_cli",
                    outcome=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
                    exit_code=None,
                    duration_ms=0.0,
                )
                res3 = runner.invoke(app, ["repro", "run", case.case_id])
                assert res3.exit_code == 3

    # Test Outcome: TIMEOUT -> exit code 4
    with patch("forge.repository.acquire_repository", return_value=mock_acquired):
        with patch("forge.repository.cleanup_repository"):
            with patch.object(ReproductionCaseManager, "run_attempt") as mock_run:
                mock_run.return_value = ReproductionAttempt(
                    attempt_id="att_04",
                    case_id=case.case_id,
                    attempt_number=1,
                    run_id="run_cli",
                    outcome=ReproductionOutcome.TIMEOUT,
                    exit_code=None,
                    duration_ms=30000.0,
                )
                res4 = runner.invoke(app, ["repro", "run", case.case_id])
                assert res4.exit_code == 4


def test_cli_plan_run_missing_file():
    """forge plan run handles nonexistent plan file cleanly."""
    res = runner.invoke(app, ["plan", "run", "nonexistent_plan.json", "--workspace", "/tmp"])
    assert res.exit_code == 2
    assert "Plan file not found" in res.output


def test_cli_plan_run_corrupt_json(tmp_path: Path):
    """forge plan run handles corrupt JSON gracefully."""
    bad_file = tmp_path / "bad_plan.json"
    bad_file.write_text("{ this is corrupt json", encoding="utf-8")
    res = runner.invoke(app, ["plan", "run", str(bad_file), "--workspace", str(tmp_path)])
    assert res.exit_code == 2
    assert "Failed to load plan" in res.output
