"""Unit tests for Phase 4 Milestone 2: Isolated Patch Application & Verification.

Verifies:
- Safe patch application via pure-Python diff applier with AST syntax verification.
- Disposable workspace isolation and safe cleanup without mutating target repo.
- Verification command allowlisting and shell injection defense.
- Deterministic outcome classification (VERIFIED, FAILED, BROKEN_BUILD, REGRESSION, TIMEOUT, INCONCLUSIVE).
- Fail-closed sandbox unavailability: Docker absence returns SANDBOX_UNAVAILABLE without running code on host.
- Patch integrity anti-tampering guards (SHA256 mismatch detection).
- Protected path immunity and budget limit re-validation.
- SQLite attempt persistence and audit event emission.
- CLI subcommands: `forge repair verify` and `forge repair attempts`.
"""

import json
from pathlib import Path
from typing import Tuple
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner
import pytest

from forge.cli import app
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairOutcome,
    RepairSafetyContract,
    compute_patch_sha256,
    evaluate_repair_outcome,
)
from forge.repair.models import (
    ProtectedPathPolicy,
    RepairAttemptRecord,
    RepairPlan,
    RepairPlanStatus,
    RepairVerificationResult,
)
from forge.repair.verifier import (
    PatchApplicationError,
    RepairVerifier,
    apply_unified_diff_to_file,
    prepare_disposable_workspace,
    validate_verification_command,
)
from forge.sandbox.contracts import ReproductionOutcome
from forge.sandbox.models import ExecutionLimits, ExecutionStatus, SandboxRequest, SandboxResult
from forge.schemas import EventType
from forge.storage.store import ForgeStore

runner = CliRunner()


def _create_sample_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Helper to initialize a mock target git repository fixture."""
    repo = tmp_path / "target_repo"
    repo.mkdir(parents=True, exist_ok=True)
    src = repo / "src"
    src.mkdir(parents=True, exist_ok=True)
    parser = src / "parser.py"
    parser.write_text(
        "def parse_items(items):\n"
        "    for i in range(len(items) + 1):\n"
        "        print(items[i])\n"
    )
    return repo, "a1b2c3d4e5f67890"


def _make_valid_plan(
    repo_path: Path,
    commit_sha: str = "a1b2c3d4e5f67890",
    target_file: str = "src/parser.py",
    diff_override: str = None,
) -> RepairPlan:
    diff = diff_override or (
        f"--- a/{target_file}\n"
        f"+++ b/{target_file}\n"
        "@@ -1,3 +1,3 @@\n"
        " def parse_items(items):\n"
        "-    for i in range(len(items) + 1):\n"
        "+    for i in range(len(items)):\n"
        "         print(items[i])\n"
    )
    digest = compute_patch_sha256(diff)
    candidate = PatchCandidate(
        patch_id="patch_test_001",
        contract_id="contract_test_001",
        finding_id="find_001",
        base_commit_sha=commit_sha,
        target_files=[target_file],
        unified_diff=diff,
        added_lines=1,
        removed_lines=1,
        hunk_count=1,
        sha256_digest=digest,
    )
    return RepairPlan(
        plan_id="plan_test_001",
        finding_id="find_001",
        run_id="run_001",
        base_commit_sha=commit_sha,
        cited_evidence_ids=["evi_001"],
        target_repo=str(repo_path),
        target_file=target_file,
        status=RepairPlanStatus.VALIDATED,
        root_cause="Off-by-one error in loop upper bound",
        proposed_change="Change range limit to len(items)",
        expected_behavior="Does not raise IndexError on parse_items",
        regression_risks=["No regressions identified"],
        verification_plan="pytest tests/test_parser.py",
        confidence=0.95,
        uncertainty_notes=None,
        patch_candidate=candidate,
        budget=RepairBudget(),
    )


# =============================================================================
# 1. Unified Diff Application Tests
# =============================================================================

def test_apply_unified_diff_success(tmp_path: Path):
    """Test clean patch application and AST verification."""
    target_file = tmp_path / "calc.py"
    target_file.write_text(
        "def add(a, b):\n"
        "    return a - b\n"
    )

    diff = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )

    success, msg = apply_unified_diff_to_file(target_file, diff)
    assert success is True
    assert "Patch applied cleanly" in msg
    assert target_file.read_text() == "def add(a, b):\n    return a + b\n"


def test_apply_unified_diff_syntax_error_rejected(tmp_path: Path):
    """Test that a patch introducing a Python syntax error is rejected."""
    target_file = tmp_path / "syntax.py"
    target_file.write_text("def foo():\n    return 42\n")

    diff = (
        "--- a/syntax.py\n"
        "+++ b/syntax.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def foo():\n"
        "-    return 42\n"
        "+    return def def 42\n"
    )

    with pytest.raises(PatchApplicationError, match="syntax error"):
        apply_unified_diff_to_file(target_file, diff)


def test_apply_unified_diff_context_mismatch_rejected(tmp_path: Path):
    """Test that mismatched context lines raise PatchApplicationError."""
    target_file = tmp_path / "foo.py"
    target_file.write_text("def completely_different():\n    pass\n")

    diff = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def original():\n"
        "-    return 1\n"
        "+    return 2\n"
    )

    with pytest.raises(PatchApplicationError, match="context mismatch"):
        apply_unified_diff_to_file(target_file, diff)


def test_apply_unified_diff_missing_file_rejected(tmp_path: Path):
    """Test that a nonexistent target file raises PatchApplicationError."""
    target_file = tmp_path / "nonexistent.py"
    diff = "@@ -1,1 +1,1 @@\n-foo\n+bar\n"

    with pytest.raises(PatchApplicationError, match="does not exist"):
        apply_unified_diff_to_file(target_file, diff)


# =============================================================================
# 2. Disposable Workspace Isolation & Safe Cleanup
# =============================================================================

def test_prepare_disposable_workspace_isolation(tmp_path: Path):
    """Verify disposable workspace copy and cleanup."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    workspace, cleanup = prepare_disposable_workspace(repo, commit_sha)

    assert workspace.exists()
    assert (workspace / "src" / "parser.py").is_file()
    assert workspace != repo

    # Modify disposable copy and ensure target repo is untouched
    (workspace / "src" / "parser.py").write_text("MODIFIED")
    assert "MODIFIED" not in (repo / "src" / "parser.py").read_text()

    # Cleanup
    cleanup()
    assert not workspace.exists()
    # Target repo still exists
    assert repo.exists()


def test_prepare_disposable_workspace_scratch_parent_safety(tmp_path: Path):
    """Verify that scratch parent inside target repo is refused."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    invalid_scratch = repo / "nested_scratch"

    with pytest.raises(ValueError, match="scratch_parent cannot equal or reside inside target_repo"):
        prepare_disposable_workspace(repo, commit_sha, scratch_parent=invalid_scratch)


# =============================================================================
# 3. Verification Command Validation & Allowlisting
# =============================================================================

def test_validate_verification_command_allowlist():
    """Approved executables pass validation."""
    valid = validate_verification_command(["pytest", "tests/test_foo.py", "-v"])
    assert valid == ["pytest", "tests/test_foo.py", "-v"]

    python_valid = validate_verification_command(["python", "-m", "unittest", "discover"])
    assert python_valid == ["python", "-m", "unittest", "discover"]


def test_validate_verification_command_unapproved_binary():
    """Unapproved executables are rejected."""
    with pytest.raises(ValueError, match="not permitted"):
        validate_verification_command(["curl", "https://malicious.example.com"])


def test_validate_verification_command_shell_injection():
    """Arguments with shell metacharacters are rejected."""
    with pytest.raises(ValueError, match="forbidden shell metacharacters"):
        validate_verification_command(["pytest", "tests/test_foo.py; rm -rf /"])

    with pytest.raises(ValueError, match="forbidden shell metacharacters"):
        validate_verification_command(["pytest", "tests/$(whoami).py"])


# =============================================================================
# 4. RepairVerifier Verification Pipeline & Outcomes
# =============================================================================

def test_verify_plan_success_repair_verified(tmp_path: Path):
    """Test successful patch verification resulting in REPAIR_VERIFIED."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run(str(repo), run_id="run_001")
    store.create_finding(run_id="run_001", finding_id="find_001", title="test", description="test")
    store.save_repair_plan(plan)

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker daemon active")
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_001",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="pytest: 1 passed in 0.05s",
        stderr="",
        duration_ms=50.0,
        timed_out=False,
    )

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(
        plan=plan,
        target_repo=repo,
        store=store,
        verification_command=["pytest", "tests/test_parser.py"],
        pre_patch_outcome=ReproductionOutcome.REPRODUCED,
        oracle_verified=True,
    )

    assert res.outcome == RepairOutcome.REPAIR_VERIFIED
    assert res.patch_applied is True
    assert res.syntax_valid is True
    assert res.sandbox_available is True
    assert res.oracle_verified is True
    assert "Repair verified" in res.diagnostic

    # Verify target repo was NEVER modified
    target_content = (repo / "src" / "parser.py").read_text()
    assert "range(len(items) + 1)" in target_content

    # Verify attempt recorded in store
    attempts = store.list_repair_attempts(plan.plan_id)
    assert len(attempts) == 1
    assert attempts[0].outcome == RepairOutcome.REPAIR_VERIFIED
    assert attempts[0].attempt_number == 1


def test_verify_plan_sandbox_unavailable_fail_closed(tmp_path: Path):
    """When Docker is unavailable, verifier returns SANDBOX_UNAVAILABLE without running code on host."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run(str(repo), run_id="run_001")

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (False, "Docker binary not found")

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(
        plan=plan,
        target_repo=repo,
        store=store,
    )

    assert res.outcome == RepairOutcome.SANDBOX_UNAVAILABLE
    assert res.sandbox_available is False
    assert "fail-closed security policy strictly refuses" in res.diagnostic
    # Verify sandbox run was never invoked
    mock_sandbox.run.assert_not_called()


def test_verify_plan_patch_tampering_rejected(tmp_path: Path):
    """Tampered diff content not matching SHA256 digest is strictly rejected."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    # Tamper with the unified diff content without updating sha256_digest
    tampered_diff = plan.patch_candidate.unified_diff + "\n# Malicious comment appended"
    tampered_candidate = plan.patch_candidate.model_copy(update={"unified_diff": tampered_diff})
    tampered_plan = plan.model_copy(update={"patch_candidate": tampered_candidate})

    verifier = RepairVerifier()
    res = verifier.verify_plan(plan=tampered_plan, target_repo=repo)

    assert res.outcome == RepairOutcome.REPAIR_REJECTED
    assert "diff content does not match stored SHA256 digest" in res.diagnostic


def test_verify_plan_protected_path_rejected(tmp_path: Path):
    """Patch targeting protected files (e.g. AGENTS.md, pyproject.toml) is rejected."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha, target_file="AGENTS.md")

    verifier = RepairVerifier()
    res = verifier.verify_plan(plan=plan, target_repo=repo)

    assert res.outcome == RepairOutcome.REPAIR_REJECTED
    assert "protected from patch application" in res.diagnostic


def test_verify_plan_broken_build_syntax_error(tmp_path: Path):
    """Patch that fails syntax verification yields BROKEN_BUILD."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    bad_diff = (
        "--- a/src/parser.py\n"
        "+++ b/src/parser.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def parse_items(items):\n"
        "-    for i in range(len(items) + 1):\n"
        "+    for def invalid syntax\n"
        "         print(items[i])\n"
    )
    plan = _make_valid_plan(repo, commit_sha, diff_override=bad_diff)

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker available")

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(plan=plan, target_repo=repo)

    assert res.outcome == RepairOutcome.BROKEN_BUILD
    assert res.patch_applied is False
    assert "Patch application failed" in res.diagnostic


def test_verify_plan_regression_detected(tmp_path: Path):
    """Test outcome classification when post-patch test execution fails (exit code 1)."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker available")
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_002",
        run_id="run_001",
        status=ExecutionStatus.FAILED,
        exit_code=1,
        stdout="FAILED tests/test_parser.py - AssertionError: regression detected",
        stderr="",
        duration_ms=60.0,
        timed_out=False,
    )

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(
        plan=plan,
        target_repo=repo,
        pre_patch_outcome=ReproductionOutcome.REPRODUCED,
    )

    assert res.outcome == RepairOutcome.REGRESSION_DETECTED
    assert res.patch_applied is True
    assert "Regression detected" in res.diagnostic


def test_verify_plan_timeout_outcome(tmp_path: Path):
    """Test outcome classification when execution times out."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker available")
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_003",
        run_id="run_001",
        status=ExecutionStatus.TIMED_OUT,
        exit_code=124,
        stdout="",
        stderr="Execution timed out after 60s",
        duration_ms=60000.0,
        timed_out=True,
    )

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(
        plan=plan,
        target_repo=repo,
        pre_patch_outcome=ReproductionOutcome.REPRODUCED,
    )

    assert res.outcome == RepairOutcome.TIMEOUT
    assert "timeout" in res.diagnostic.lower()


def test_verify_plan_inconclusive_outcome_missing_oracle(tmp_path: Path):
    """Test outcome classification when oracle is unverified or missing."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker available")
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_004",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="passed",
        stderr="",
        duration_ms=50.0,
        timed_out=False,
    )

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(
        plan=plan,
        target_repo=repo,
        pre_patch_outcome=ReproductionOutcome.REPRODUCED,
        oracle_verified=False,  # Unverified oracle
    )

    assert res.outcome == RepairOutcome.INCONCLUSIVE
    assert "Verification is inconclusive" in res.diagnostic


# =============================================================================
# 5. SQLite Persistence & Event History
# =============================================================================

def test_repair_attempts_persistence_and_numbering(tmp_path: Path):
    """Verify repair attempt recording, sequential numbering, and event history."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run(str(repo), run_id="run_001")
    store.create_finding(run_id="run_001", finding_id="find_001", title="test", description="test")
    store.save_repair_plan(plan)

    # First attempt
    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker available")
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_att1",
        run_id="run_001",
        status=ExecutionStatus.FAILED,
        exit_code=1,
        stdout="failure",
        stderr="",
        duration_ms=10.0,
    )

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res1 = verifier.verify_plan(plan=plan, target_repo=repo, store=store)
    assert res1.attempt_record.attempt_number == 1

    # Second attempt
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_att2",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="success",
        stderr="",
        duration_ms=12.0,
    )
    res2 = verifier.verify_plan(plan=plan, target_repo=repo, store=store)
    assert res2.attempt_record.attempt_number == 2

    # Check store listing
    attempts = store.list_repair_attempts(plan.plan_id)
    assert len(attempts) == 2
    assert attempts[0].attempt_number == 1
    assert attempts[1].attempt_number == 2

    # Verify lifecycle events
    events = store.list_run_events("run_001")
    event_types = [e.event_type for e in events]
    assert EventType.REPAIR_EVALUATION_STARTED in event_types
    assert EventType.REPAIR_EVALUATION_COMPLETED in event_types
    assert EventType.REPAIR_ROLLBACK_EXECUTED in event_types


def test_repair_secrets_redaction_in_attempts(tmp_path: Path):
    """Verify secrets in command stdout/stderr are redacted before saving attempts."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run(str(repo), run_id="run_001")
    store.create_finding(run_id="run_001", finding_id="find_001", title="test", description="test")
    store.save_repair_plan(plan)

    mock_sandbox = MagicMock()
    mock_sandbox.check_health.return_value = (True, "Docker available")
    mock_sandbox.run.return_value = SandboxResult(
        request_id="req_secret",
        run_id="run_001",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout="Exposed token sk-abcdef1234567890abcdef12 in output",
        stderr="",
        duration_ms=20.0,
    )

    verifier = RepairVerifier(sandbox=mock_sandbox)
    res = verifier.verify_plan(plan=plan, target_repo=repo, store=store)

    attempts = store.list_repair_attempts(plan.plan_id)
    assert len(attempts) == 1
    assert "sk-abcdef1234567890abcdef12" not in attempts[0].stdout_excerpt
    assert "[REDACTED" in attempts[0].stdout_excerpt or "REDACTED" in attempts[0].stdout_excerpt


# =============================================================================
# 6. CLI Integration Tests
# =============================================================================

def test_cli_repair_verify_exit_code_3_sandbox_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI exits with code 3 when sandbox is unavailable."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())

    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "test.db"))

    # When docker is unavailable on host, verify returns code 3
    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["repair", "verify", str(plan_file), "--repo", str(repo)])
        assert result.exit_code == 3
        assert "SANDBOX_UNAVAILABLE" in result.output


def test_cli_repair_verify_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI verify supports --format json."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())

    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "test.db"))

    with patch("shutil.which", return_value=None):
        result = runner.invoke(app, ["repair", "verify", str(plan_file), "--repo", str(repo), "--format", "json"])
        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["plan_id"] == plan.plan_id
        assert data["outcome"] == "sandbox_unavailable"
        assert data["sandbox_available"] is False


def test_cli_repair_attempts_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI attempts lists verification attempts."""
    repo, commit_sha = _create_sample_repo(tmp_path)
    plan = _make_valid_plan(repo, commit_sha)

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

    store = ForgeStore(db_path=db_path)
    store.create_run(str(repo), run_id="run_001")
    store.create_finding(run_id="run_001", finding_id="find_001", title="test", description="test")
    store.save_repair_plan(plan)

    # Empty attempts
    result = runner.invoke(app, ["repair", "attempts", plan.plan_id])
    assert result.exit_code == 0
    assert "No verification attempts found" in result.output

    # Record an attempt
    attempt = RepairAttemptRecord(
        attempt_id="att_001",
        plan_id=plan.plan_id,
        attempt_number=1,
        finding_id=plan.finding_id,
        run_id="run_001",
        base_commit_sha=commit_sha,
        patch_digest="abc1234",
        outcome=RepairOutcome.REPAIR_VERIFIED,
        pre_repro_outcome=ReproductionOutcome.REPRODUCED,
        post_repro_outcome=ReproductionOutcome.NOT_REPRODUCED,
        duration_ms=45.2,
        sandbox_available=True,
        oracle_verified=True,
    )
    store.save_repair_attempt(attempt)

    # Table output
    result = runner.invoke(app, ["repair", "attempts", plan.plan_id])
    assert result.exit_code == 0
    assert "att_001" in result.output
    assert "repair_verified" in result.output

    # JSON output
    result_json = runner.invoke(app, ["repair", "attempts", plan.plan_id, "--format", "json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert len(data) == 1
    assert data[0]["attempt_id"] == "att_001"
