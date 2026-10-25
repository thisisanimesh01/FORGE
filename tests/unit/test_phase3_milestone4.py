"""Comprehensive tests for Phase 3, Milestone 4: Reproduction Outcome Classification and Evidence Validation.

Covers:
- Trusted oracle violation (REPRODUCED, ORACLE_VIOLATION_CONFIRMED)
- Trusted oracle non-violation (NOT_REPRODUCED, ORACLE_OBSERVED_NO_VIOLATION)
- Missing or untrusted oracle (ORACLE_ABSENT_OR_UNTRUSTED)
- Generic non-zero exits (UNRELATED_FAILURE, GENERIC_NONZERO_EXIT)
- Unrelated exceptions / syntax errors (UNRELATED_FAILURE, UNRELATED_EXCEPTION)
- Timeouts (TIMEOUT, TIMEOUT_EXCEEDED)
- Unavailable Docker / sandbox (ENVIRONMENT_BLOCKED, ENVIRONMENT_UNAVAILABLE)
- Invalid plans and policy violations (INVALID_PLAN, PLAN_POLICY_VIOLATION)
- Stale revisions (INVALID_PLAN, STALE_REVISION_MISMATCH)
- Flaky retries across attempts (FLAKY, INCONSISTENT_RETRIES)
- Corrupted or truncated evidence (INCONCLUSIVE, EVIDENCE_TRUNCATED_OR_MISSING)
- Interrupted execution (INCONCLUSIVE, EXECUTION_INTERRUPTED)
- Cleanup failure (INCONCLUSIVE, CLEANUP_FAILURE)
- Determinism and idempotency (identical outputs for identical inputs)
- Secret redaction in diagnostics and explanations
- SQLite persistence of classifications and event history
- CLI commands: forge repro classify, forge plan classify, exit codes
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import List, Optional

import pytest
from typer.testing import CliRunner

from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.cli import app
from forge.reproduction.classifier import (
    CLASSIFIER_VERSION,
    ClassificationOutcome,
    ClassificationReasonCode,
    ReproductionClassification,
    ReproductionOutcomeClassifier,
    generate_classification_id,
)
from forge.reproduction.manager import ReproductionCaseManager
from forge.reproduction.models import (
    CaseStatus,
    FieldProvenance,
    ReproductionAttempt,
    ReproductionCase,
    RetryPolicy,
)
from forge.reproduction.planner import (
    PlanProvenance,
    PlanValidationResult,
    ReproductionPlan,
    ReproductionPlanner,
    ReproductionStrategy,
)
from forge.sandbox.contracts import (
    ExecutionEnvironmentSpec,
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    EventType,
    Finding,
    FindingLifecycleState,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _make_attempt(
    *,
    case_id: str = "case_find_01_test",
    attempt_number: int = 1,
    outcome: ReproductionOutcome = ReproductionOutcome.REPRODUCED,
    exit_code: int = 1,
    stdout: str = "",
    stderr: str = "",
    matched_patterns: Optional[List[str]] = None,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    cleanup_status: str = "cleaned",
    diagnostic_message: Optional[str] = None,
) -> ReproductionAttempt:
    return ReproductionAttempt(
        attempt_id=f"att_test_{attempt_number}",
        case_id=case_id,
        attempt_number=attempt_number,
        run_id="run_test",
        outcome=outcome,
        exit_code=exit_code,
        duration_ms=150.0,
        stdout_excerpt=stdout,
        stderr_excerpt=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        matched_patterns=matched_patterns or [],
        isolation_details={"network": "disabled", "read_only": True},
        provenance={"execution": FieldProvenance.SANDBOX_EXECUTION},
        diagnostic_message=diagnostic_message,
        cleanup_status=cleanup_status,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )


def _make_case(
    *,
    finding_id: str = "find_01",
    expected_patterns: Optional[List[str]] = None,
    oracle_provenance: FieldProvenance = FieldProvenance.TRUSTED_FIXTURE,
) -> ReproductionCase:
    cmd = ReproductionCommand(
        args=["pytest", "tests/test_parser.py::test_off_by_one"],
        expected_exit_codes=[1],
        expected_output_patterns=expected_patterns or ["IndexError: list index out of range"],
    )
    req = ReproductionRequest(
        request_id="req_test_01",
        finding_id=finding_id,
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="IndexError occurs when accessing index len(arr)",
        command=cmd,
    )
    return ReproductionCase(
        case_id="case_find_01_test",
        finding_id=finding_id,
        run_id="run_test",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=req,
        provenance_map={
            "expected_behavior": oracle_provenance,
            "hypothesis": oracle_provenance,
            "command": FieldProvenance.TRUSTED_FIXTURE,
        },
    )


# ---------------------------------------------------------------------------
# 1. Outcome Taxonomy & Reason Code Completeness
# ---------------------------------------------------------------------------


def test_outcome_taxonomy_completeness():
    """All 8 required outcomes exist in ClassificationOutcome."""
    expected = {
        "reproduced",
        "not_reproduced",
        "unrelated_failure",
        "environment_blocked",
        "timeout",
        "flaky",
        "invalid_plan",
        "inconclusive",
    }
    actual = {o.value for o in ClassificationOutcome}
    assert expected.issubset(actual)


def test_reason_code_completeness():
    """All essential reason codes are defined."""
    codes = {r.value for r in ClassificationReasonCode}
    assert "oracle_violation_confirmed" in codes
    assert "oracle_observed_no_violation" in codes
    assert "oracle_absent_or_untrusted" in codes
    assert "generic_nonzero_exit" in codes
    assert "unrelated_exception" in codes
    assert "timeout_exceeded" in codes
    assert "environment_unavailable" in codes
    assert "plan_policy_violation" in codes
    assert "stale_revision_mismatch" in codes
    assert "inconsistent_retries" in codes


# ---------------------------------------------------------------------------
# 2. Trusted Oracle Violation (REPRODUCED)
# ---------------------------------------------------------------------------


def test_classify_trusted_oracle_violation_confirmed():
    """Confirmed defect reproduction with trusted oracle and matching pattern."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["IndexError"])
    attempt = _make_attempt(
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        stderr="IndexError: list index out of range",
        matched_patterns=["IndexError"],
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.REPRODUCED
    assert classification.reason_code == ClassificationReasonCode.ORACLE_VIOLATION_CONFIRMED
    assert classification.is_reproduced is True
    assert classification.oracle_verified is True


# ---------------------------------------------------------------------------
# 3. Trusted Oracle Clean Run (NOT_REPRODUCED)
# ---------------------------------------------------------------------------


def test_classify_trusted_oracle_observed_no_violation():
    """Clean test execution under trusted oracle classifies as NOT_REPRODUCED."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt(
        outcome=ReproductionOutcome.PASSED,
        exit_code=0,
        stdout="1 passed in 0.05s",
        matched_patterns=[],
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.NOT_REPRODUCED
    assert classification.reason_code == ClassificationReasonCode.ORACLE_OBSERVED_NO_VIOLATION
    assert classification.is_reproduced is False
    assert classification.oracle_verified is True
    # Explicit disclaimer about tested conditions
    assert "Passing tests do not prove" in classification.explanation


# ---------------------------------------------------------------------------
# 4. Missing or Untrusted Oracle (Never claim REPRODUCED)
# ---------------------------------------------------------------------------


def test_classify_untrusted_oracle_with_error():
    """Nonzero exit without trusted oracle classifies as UNRELATED_FAILURE, never REPRODUCED."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(oracle_provenance=FieldProvenance.UNTRUSTED_LLM)
    attempt = _make_attempt(
        exit_code=1,
        matched_patterns=["SomeError"],
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.UNRELATED_FAILURE
    assert classification.reason_code == ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED
    assert classification.is_reproduced is False
    assert classification.oracle_verified is False


def test_classify_untrusted_oracle_with_pass():
    """Clean pass without trusted oracle is INCONCLUSIVE because no oracle proved absence."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(oracle_provenance=FieldProvenance.UNTRUSTED_REPOSITORY)
    attempt = _make_attempt(exit_code=0, matched_patterns=[])
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.INCONCLUSIVE
    assert classification.reason_code == ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED
    assert classification.is_reproduced is False


# ---------------------------------------------------------------------------
# 5. Generic Non-Zero Exit & Unrelated Failures
# ---------------------------------------------------------------------------


def test_classify_generic_nonzero_exit_without_pattern_match():
    """Exit code 1 without matching defect patterns classifies as UNRELATED_FAILURE."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["IndexError"])
    attempt = _make_attempt(
        exit_code=1,
        stderr="TypeError: unhashable type: 'list'",
        matched_patterns=[],  # Did not match IndexError
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.UNRELATED_FAILURE
    assert classification.reason_code == ClassificationReasonCode.GENERIC_NONZERO_EXIT
    assert classification.is_reproduced is False


def test_classify_unrelated_syntax_exception():
    """Syntax error in test code classifies as UNRELATED_FAILURE with UNRELATED_EXCEPTION."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["AssertionError"])
    attempt = _make_attempt(
        exit_code=2,
        stderr="SyntaxError: invalid syntax in test file line 4",
        matched_patterns=[],
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.UNRELATED_FAILURE
    assert classification.reason_code == ClassificationReasonCode.UNRELATED_EXCEPTION


# ---------------------------------------------------------------------------
# 6. Timeouts & Resource Exhaustion
# ---------------------------------------------------------------------------


def test_classify_timeout():
    """Execution timeout classifies as TIMEOUT."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt(
        outcome=ReproductionOutcome.TIMEOUT,
        exit_code=None,
        diagnostic_message="Execution quota of 60s exceeded",
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.TIMEOUT
    assert classification.reason_code == ClassificationReasonCode.TIMEOUT_EXCEEDED
    assert classification.is_reproduced is False


# ---------------------------------------------------------------------------
# 7. Environment & Sandbox Boundary (Fail-Closed)
# ---------------------------------------------------------------------------


def test_classify_environment_blocked():
    """Unavailable Docker runtime classifies as ENVIRONMENT_BLOCKED."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt(
        outcome=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
        exit_code=None,
        diagnostic_message="Docker binary not found in PATH",
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.ENVIRONMENT_BLOCKED
    assert classification.reason_code == ClassificationReasonCode.ENVIRONMENT_UNAVAILABLE
    assert classification.is_reproduced is False


# ---------------------------------------------------------------------------
# 8. Plan Validation & Stale Revision Precedence
# ---------------------------------------------------------------------------


def test_classify_invalid_plan_policy_violation():
    """Failed plan validation takes highest precedence as INVALID_PLAN."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt()
    val_res = PlanValidationResult(
        is_valid=False,
        violations=["Executable 'bash' is forbidden by security policy"],
        plan_id="plan_bad",
    )
    classification = classifier.classify_attempt(attempt, case=case, validation_result=val_res)

    assert classification.outcome == ClassificationOutcome.INVALID_PLAN
    assert classification.reason_code == ClassificationReasonCode.PLAN_POLICY_VIOLATION


def test_classify_stale_revision():
    """Stale revision mismatch takes precedence as INVALID_PLAN."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt()
    classification = classifier.classify_attempt(attempt, case=case, is_stale_revision=True)

    assert classification.outcome == ClassificationOutcome.INVALID_PLAN
    assert classification.reason_code == ClassificationReasonCode.STALE_REVISION_MISMATCH


# ---------------------------------------------------------------------------
# 9. Flaky Retries (Inconsistent Multi-Attempt History)
# ---------------------------------------------------------------------------


def test_classify_case_history_flaky_detection():
    """Conflicting attempt outcomes (reproduced then passed) classify as FLAKY."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["IndexError"])

    att1 = _make_attempt(
        attempt_number=1,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        matched_patterns=["IndexError"],
    )
    att2 = _make_attempt(
        attempt_number=2,
        outcome=ReproductionOutcome.PASSED,
        exit_code=0,
        matched_patterns=[],
    )

    history_class = classifier.classify_case_history(case, [att1, att2])

    assert history_class.outcome == ClassificationOutcome.FLAKY
    assert history_class.reason_code == ClassificationReasonCode.INCONSISTENT_RETRIES
    assert history_class.is_reproduced is False
    assert "FLAKY" in history_class.explanation


def test_classify_case_history_consistent_reproduction():
    """Consistent reproduction across multiple attempts classifies as REPRODUCED."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["IndexError"])

    att1 = _make_attempt(
        attempt_number=1,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        matched_patterns=["IndexError"],
    )
    att2 = _make_attempt(
        attempt_number=2,
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        matched_patterns=["IndexError"],
    )

    history_class = classifier.classify_case_history(case, [att1, att2])

    assert history_class.outcome == ClassificationOutcome.REPRODUCED
    assert history_class.is_reproduced is True


# ---------------------------------------------------------------------------
# 10. Truncated Evidence, Interruption & Cleanup Failure
# ---------------------------------------------------------------------------


def test_classify_truncated_evidence_is_inconclusive():
    """Truncated output without pattern match classifies as INCONCLUSIVE."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["IndexError"])
    attempt = _make_attempt(
        exit_code=1,
        stdout_truncated=True,
        matched_patterns=[],
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.INCONCLUSIVE
    assert classification.reason_code == ClassificationReasonCode.EVIDENCE_TRUNCATED_OR_MISSING


def test_classify_cleanup_failure_is_inconclusive():
    """Container cleanup failure classifies as INCONCLUSIVE."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt(
        exit_code=1,
        cleanup_status="failed_container_removal",
        matched_patterns=["IndexError"],
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert classification.outcome == ClassificationOutcome.INCONCLUSIVE
    assert classification.reason_code == ClassificationReasonCode.CLEANUP_FAILURE


def test_classify_interrupted_execution():
    """Interrupted execution classifies as INCONCLUSIVE."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt()
    classification = classifier.classify_attempt(attempt, case=case, is_interrupted=True)

    assert classification.outcome == ClassificationOutcome.INCONCLUSIVE
    assert classification.reason_code == ClassificationReasonCode.EXECUTION_INTERRUPTED


# ---------------------------------------------------------------------------
# 11. Determinism, Idempotency & Secret Redaction
# ---------------------------------------------------------------------------


def test_classifier_idempotence():
    """Identical attempt inputs produce identical classification records."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case(expected_patterns=["IndexError"])
    attempt = _make_attempt(
        outcome=ReproductionOutcome.REPRODUCED,
        exit_code=1,
        matched_patterns=["IndexError"],
    )
    c1 = classifier.classify_attempt(attempt, case=case)
    c2 = classifier.classify_attempt(attempt, case=case)

    assert c1.classification_id == c2.classification_id
    assert c1.outcome == c2.outcome
    assert c1.reason_code == c2.reason_code
    assert c1.explanation == c2.explanation
    assert c1.is_reproduced == c2.is_reproduced


def test_classifier_secret_redaction():
    """Secrets in diagnostic messages are redacted."""
    classifier = ReproductionOutcomeClassifier()
    case = _make_case()
    attempt = _make_attempt(
        outcome=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
        diagnostic_message="Docker failed with token sk-abcdef1234567890abcdef12 in config",
    )
    classification = classifier.classify_attempt(attempt, case=case)

    assert "sk-" not in classification.explanation or "REDACTED" in classification.explanation


# ---------------------------------------------------------------------------
# 12. ReproductionCaseManager Integration & Persistence
# ---------------------------------------------------------------------------


def test_case_manager_classify_and_persist(tmp_path: Path):
    """Case manager classifies case history and persists classification to SQLite."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run(target="https://github.com/example/repo.git", run_id="run_01")
    store.create_finding(
        run_id="run_01",
        title="Off by one",
        description="Loop index",
        finding_id="find_01",
    )

    manager = ReproductionCaseManager(store=store)
    cmd = ReproductionCommand(
        args=["pytest", "tests/test_foo.py::test_bar"],
        expected_exit_codes=[1],
        expected_output_patterns=["IndexError"],
    )
    req = ReproductionRequest(
        request_id="req_01",
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        commit_sha="a1b2c3d4e5f6",
        hypothesis="IndexError occurs",
        command=cmd,
    )
    case = manager.create_case(
        finding_id="find_01",
        target_repo="https://github.com/example/repo.git",
        target_commit_sha="a1b2c3d4e5f6",
        contract=req,
        run_id="run_01",
    )

    # Initially no attempts -> classify_case reports INCONCLUSIVE
    classification = manager.classify_case(case.case_id)
    assert classification.outcome == ClassificationOutcome.INCONCLUSIVE

    # Verify persisted in database
    retrieved = manager.get_classification(case.case_id)
    assert retrieved is not None
    assert retrieved.outcome == ClassificationOutcome.INCONCLUSIVE
    assert retrieved.reason_code == ClassificationReasonCode.EVIDENCE_TRUNCATED_OR_MISSING


def test_flaky_case_blocks_finding_lifecycle_transition(tmp_path: Path):
    """A flaky outcome prevents finding from transitioning to REPRODUCED."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run(target="repo", run_id="run_01")
    finding = store.create_finding(
        run_id="run_01",
        title="Flaky defect",
        description="Timing defect",
        finding_id="find_flaky",
    )

    # Construct evidence with flaky details
    ev = EvidenceRecord(
        evidence_id="evi_flaky",
        run_id="run_01",
        finding_id="find_flaky",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Flaky run",
        content="Attempt 1 reproduced, Attempt 2 passed",
        details={"status": "flaky", "outcome": "flaky"},
        provenance="forge:sandbox:case_01",
    )

    # validate_transition must reject flaky evidence for REPRODUCED state
    with pytest.raises(InvalidTransitionError):
        validate_transition(finding, FindingLifecycleState.REPRODUCED, ev)


# ---------------------------------------------------------------------------
# 13. CLI Integration Tests
# ---------------------------------------------------------------------------


def test_cli_repro_classify_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge repro classify on non-existent case exits with code 1."""
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "cli_test.db"))
    init_database(tmp_path / "cli_test.db")

    result = runner.invoke(app, ["repro", "classify", "case_nonexistent"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_cli_repro_classify_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge repro classify --format json outputs machine-readable classification."""
    db_file = tmp_path / "cli_test.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
    store = ForgeStore(db_path=db_file)
    store.create_run(target="repo", run_id="run_cli")
    store.create_finding(run_id="run_cli", title="Bug", description="d", finding_id="find_cli")

    manager = ReproductionCaseManager(store=store)
    cmd = ReproductionCommand(args=["pytest", "tests/test_foo.py"])
    req = ReproductionRequest(
        request_id="req_cli",
        finding_id="find_cli",
        target_repo="repo",
        commit_sha="a1b2c3d4",
        hypothesis="test",
        command=cmd,
    )
    case = manager.create_case(
        finding_id="find_cli",
        target_repo="repo",
        target_commit_sha="a1b2c3d4",
        contract=req,
        run_id="run_cli",
    )

    result = runner.invoke(app, ["repro", "classify", case.case_id, "--format", "json"])
    # With 0 attempts, classify returns INCONCLUSIVE (exit code 4)
    assert result.exit_code == 4
    data = json.loads(result.output)
    assert data["outcome"] == "inconclusive"
    assert data["classifier_version"] == CLASSIFIER_VERSION


def test_cli_plan_classify_valid_plan(tmp_path: Path):
    """forge plan classify accepts a valid plan with trusted oracle."""
    planner = ReproductionPlanner()
    plan = planner.create_plan(
        finding_id="find_cli_plan",
        commit_sha="deadbeef1234",
        target_repo="https://github.com/example/repo.git",
        trusted_expected_behavior="Index must stay bounded",
        expected_behavior_provenance=FieldProvenance.TRUSTED_FIXTURE,
        command_args=["pytest", "tests/test_foo.py::test_bar"],
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan.model_dump(mode="json"), indent=2))

    result = runner.invoke(app, ["plan", "classify", str(plan_file)])
    assert result.exit_code == 0
    assert "Plan Classification" in result.output
    assert "Oracle Proven: YES" in result.output


def test_cli_plan_classify_insufficient_information(tmp_path: Path):
    """forge plan classify rejects a plan with INSUFFICIENT_INFORMATION (exit code 2)."""
    planner = ReproductionPlanner()
    plan = planner.create_plan(
        finding_id="find_cli_plan_no_oracle",
        commit_sha="deadbeef1234",
        target_repo="https://github.com/example/repo.git",
        trusted_expected_behavior=None,  # Forces INSUFFICIENT_INFORMATION
        expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS,
    )
    plan_file = tmp_path / "plan_no_oracle.json"
    plan_file.write_text(json.dumps(plan.model_dump(mode="json"), indent=2))

    result = runner.invoke(app, ["plan", "classify", str(plan_file)])
    assert result.exit_code == 2
    assert "INVALID_PLAN" in result.output
