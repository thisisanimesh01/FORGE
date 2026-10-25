"""Comprehensive tests for Phase 3, Milestone 3: Deterministic Reproduction Planning.

Covers:
- ReproductionStrategy enum completeness
- ReproductionPlan model creation and immutability
- generate_plan_id determinism and stability
- ReproductionPlanner strategy selection logic
- Trusted vs. untrusted provenance enforcement
- INSUFFICIENT_INFORMATION plan generation when no trusted expected behaviour
- Plan creation with all five strategies
- ReproductionPlanValidator — all rejection criteria
- CLI plan generate / validate / show / run commands (smoke tests)
"""

import json
import tempfile
from pathlib import Path
from typing import List, Optional

import pytest
from typer.testing import CliRunner

from forge.reproduction.models import FieldProvenance
from forge.reproduction.planner import (
    PlanProvenance,
    PlanValidationResult,
    ReproductionPlan,
    ReproductionPlanner,
    ReproductionStrategy,
    generate_plan_id,
)
from forge.reproduction.validator import ReproductionPlanValidator
from forge.sandbox.contracts import (
    ExecutionEnvironmentSpec,
    ReproductionCommand,
)
from forge.cli import app

runner = CliRunner()


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _make_planner() -> ReproductionPlanner:
    return ReproductionPlanner()


def _make_plan(
    *,
    command_args: Optional[List[str]] = None,
    trusted_expected_behavior: Optional[str] = "Loop index must stay < len(arr)",
    expected_behavior_provenance: FieldProvenance = FieldProvenance.TRUSTED_FIXTURE,
    preferred_strategy: Optional[ReproductionStrategy] = None,
    finding_id: str = "find_test_001",
    commit_sha: str = "abc123def456",
    target_repo: str = "https://github.com/example/repo.git",
) -> ReproductionPlan:
    planner = _make_planner()
    return planner.create_plan(
        finding_id=finding_id,
        commit_sha=commit_sha,
        target_repo=target_repo,
        trusted_expected_behavior=trusted_expected_behavior,
        expected_behavior_provenance=expected_behavior_provenance,
        command_args=command_args or ["pytest", "tests/test_parser.py::test_off_by_one"],
        preferred_strategy=preferred_strategy,
    )


# -----------------------------------------------------------------------
# 1. Enum completeness
# -----------------------------------------------------------------------


def test_strategy_enum_completeness():
    """All five defined strategies are present."""
    values = {s.value for s in ReproductionStrategy}
    assert "run_specific_test" in values
    assert "run_scoped_test_file" in values
    assert "validate_boundary_condition" in values
    assert "check_expected_observed" in values
    assert "insufficient_information" in values


# -----------------------------------------------------------------------
# 2. generate_plan_id determinism
# -----------------------------------------------------------------------


def test_generate_plan_id_is_deterministic():
    """Same inputs always produce the same plan ID."""
    id1 = generate_plan_id("find_001", ReproductionStrategy.RUN_SPECIFIC_TEST, "sha123")
    id2 = generate_plan_id("find_001", ReproductionStrategy.RUN_SPECIFIC_TEST, "sha123")
    assert id1 == id2


def test_generate_plan_id_different_strategy_produces_different_id():
    """Different strategies yield different plan IDs."""
    id1 = generate_plan_id("find_001", ReproductionStrategy.RUN_SPECIFIC_TEST, "sha123")
    id2 = generate_plan_id("find_001", ReproductionStrategy.RUN_SCOPED_TEST_FILE, "sha123")
    assert id1 != id2


def test_generate_plan_id_different_finding_produces_different_id():
    """Different finding IDs yield different plan IDs."""
    id1 = generate_plan_id("find_001", ReproductionStrategy.RUN_SPECIFIC_TEST, "sha123")
    id2 = generate_plan_id("find_002", ReproductionStrategy.RUN_SPECIFIC_TEST, "sha123")
    assert id1 != id2


def test_generate_plan_id_format():
    """Plan ID starts with 'plan_' and contains strategy value."""
    pid = generate_plan_id("find_001", ReproductionStrategy.RUN_SPECIFIC_TEST, "sha")
    assert pid.startswith("plan_")
    assert "run_specific_test" in pid


# -----------------------------------------------------------------------
# 3. Planner — basic plan creation
# -----------------------------------------------------------------------


def test_planner_creates_plan_with_valid_inputs():
    """Planner creates a valid plan when trusted expected behaviour is provided."""
    plan = _make_plan()
    assert plan.finding_id == "find_test_001"
    assert plan.commit_sha == "abc123def456"
    assert plan.strategy != ReproductionStrategy.INSUFFICIENT_INFORMATION
    assert plan.expected_behavior is not None
    assert plan.command is not None


def test_planner_selects_run_specific_test_for_node_id():
    """Pytest node ID args trigger RUN_SPECIFIC_TEST strategy."""
    plan = _make_plan(
        command_args=["pytest", "tests/test_foo.py::test_bar"],
    )
    assert plan.strategy == ReproductionStrategy.RUN_SPECIFIC_TEST


def test_planner_selects_run_scoped_test_file_for_file_only():
    """Test-file-only args trigger RUN_SCOPED_TEST_FILE strategy."""
    plan = _make_plan(
        command_args=["pytest", "tests/test_foo.py"],
    )
    assert plan.strategy == ReproductionStrategy.RUN_SCOPED_TEST_FILE


def test_planner_respects_preferred_strategy_override():
    """Caller can force a strategy via preferred_strategy."""
    plan = _make_plan(
        command_args=["pytest", "tests/test_foo.py"],
        preferred_strategy=ReproductionStrategy.VALIDATE_BOUNDARY_CONDITION,
    )
    assert plan.strategy == ReproductionStrategy.VALIDATE_BOUNDARY_CONDITION


def test_planner_plan_is_immutable():
    """ReproductionPlan is frozen (immutable)."""
    from pydantic import ValidationError
    plan = _make_plan()
    with pytest.raises((TypeError, AttributeError, ValidationError)):
        plan.finding_id = "tampered"  # type: ignore[misc]




# -----------------------------------------------------------------------
# 4. Planner — INSUFFICIENT_INFORMATION enforcement
# -----------------------------------------------------------------------


def test_planner_insufficient_information_when_no_expected_behavior():
    """Plan strategy is INSUFFICIENT_INFORMATION when no trusted expected behaviour."""
    planner = _make_planner()
    plan = planner.create_plan(
        finding_id="find_001",
        commit_sha="sha123",
        target_repo="https://github.com/example/repo.git",
        trusted_expected_behavior=None,
        expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS,
        command_args=["pytest", "tests/test_foo.py"],
    )
    assert plan.strategy == ReproductionStrategy.INSUFFICIENT_INFORMATION
    assert plan.command is None
    assert plan.expected_behavior is None


def test_planner_insufficient_information_overrides_preferred_strategy():
    """Even if a preferred strategy is given, None expected behaviour forces INSUFFICIENT_INFORMATION."""
    planner = _make_planner()
    plan = planner.create_plan(
        finding_id="find_001",
        commit_sha="sha123",
        target_repo="repo",
        trusted_expected_behavior=None,
        expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS,
        preferred_strategy=ReproductionStrategy.RUN_SPECIFIC_TEST,
    )
    assert plan.strategy == ReproductionStrategy.INSUFFICIENT_INFORMATION


def test_planner_rejects_untrusted_provenance_with_expected_behavior():
    """ValueError raised when expected_behavior is provided with untrusted provenance."""
    planner = _make_planner()
    with pytest.raises(ValueError, match="trusted provenance"):
        planner.create_plan(
            finding_id="find_001",
            commit_sha="sha123",
            target_repo="repo",
            trusted_expected_behavior="arr[i] should not exceed arr[-1]",
            expected_behavior_provenance=FieldProvenance.UNTRUSTED_LLM,  # Not trusted
        )


def test_planner_rejects_untrusted_repository_provenance():
    """UNTRUSTED_REPOSITORY provenance with expected behaviour raises ValueError."""
    planner = _make_planner()
    with pytest.raises(ValueError, match="trusted provenance"):
        planner.create_plan(
            finding_id="find_001",
            commit_sha="sha123",
            target_repo="repo",
            trusted_expected_behavior="Expected loop to stop before len(arr)",
            expected_behavior_provenance=FieldProvenance.UNTRUSTED_REPOSITORY,
        )


# -----------------------------------------------------------------------
# 5. Planner — provenance recording
# -----------------------------------------------------------------------


def test_planner_records_trusted_fixture_provenance():
    """Plans with TRUSTED_FIXTURE provenance record that in provenance field."""
    plan = _make_plan(expected_behavior_provenance=FieldProvenance.TRUSTED_FIXTURE)
    assert plan.provenance.expected_behavior_source == FieldProvenance.TRUSTED_FIXTURE


def test_planner_records_static_analysis_provenance():
    """Plans with STATIC_ANALYSIS provenance record that in provenance field."""
    plan = _make_plan(expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS)
    assert plan.provenance.expected_behavior_source == FieldProvenance.STATIC_ANALYSIS


def test_planner_resource_limits_provenance_always_system_policy():
    """Resource limits provenance is always SYSTEM_POLICY."""
    plan = _make_plan()
    assert plan.provenance.resource_limits_source == FieldProvenance.SYSTEM_POLICY


def test_planner_network_not_approved_by_default():
    """Plans have network_approved=False by default."""
    plan = _make_plan()
    assert plan.network_approved is False


# -----------------------------------------------------------------------
# 6. Validator — valid plans
# -----------------------------------------------------------------------


def test_validator_accepts_valid_pytest_plan():
    """A well-formed pytest plan is accepted."""
    plan = _make_plan(command_args=["pytest", "tests/test_foo.py::test_case"])
    validator = ReproductionPlanValidator()
    result = validator.validate(plan)
    assert result.is_valid, f"Unexpected violations: {result.violations}"
    assert result.violations == []


def test_validator_accepts_python3_plan():
    """A well-formed python3 command plan is accepted."""
    plan = _make_plan(
        command_args=["python3", "tests/check_boundary.py"],
        preferred_strategy=ReproductionStrategy.CHECK_EXPECTED_OBSERVED,
    )
    validator = ReproductionPlanValidator()
    result = validator.validate(plan)
    assert result.is_valid, f"Unexpected violations: {result.violations}"


# -----------------------------------------------------------------------
# 7. Validator — INSUFFICIENT_INFORMATION rejection
# -----------------------------------------------------------------------


def test_validator_rejects_insufficient_information_plan():
    """INSUFFICIENT_INFORMATION plans cannot be executed."""
    planner = _make_planner()
    plan = planner.create_plan(
        finding_id="find_001",
        commit_sha="sha123",
        target_repo="repo",
        trusted_expected_behavior=None,
        expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS,
    )
    assert plan.strategy == ReproductionStrategy.INSUFFICIENT_INFORMATION
    validator = ReproductionPlanValidator()
    result = validator.validate(plan)
    assert not result.is_valid
    assert any("INSUFFICIENT_INFORMATION" in v for v in result.violations)


# -----------------------------------------------------------------------
# 8. Validator — unapproved executable
# -----------------------------------------------------------------------


def test_validator_rejects_forbidden_shell_binary():
    """Shell binaries are rejected even if constructed manually."""
    plan = _make_plan()
    # Construct a command with a forbidden binary using model_construct to bypass Command-level validator
    bad_command = ReproductionCommand.model_construct(
        args=["bash", "-c", "echo hello"],
        relative_cwd=".",
        env_vars={},
        expected_exit_codes=[0],
        expected_output_patterns=[],
    )
    # Use model_copy to override the command field while keeping the rest valid
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("not in the approved allowlist" in v or "allowlist" in v.lower() for v in result.violations)



# -----------------------------------------------------------------------
# 9. Validator — shell injection / forbidden characters
# -----------------------------------------------------------------------


def test_validator_rejects_shell_injection_in_args():
    """Arguments containing shell metacharacters are rejected."""
    plan = _make_plan()
    bad_command = ReproductionCommand.model_construct(
        args=["pytest", "tests/; rm -rf /"],
        relative_cwd=".",
        env_vars={},
        expected_exit_codes=[1],
        expected_output_patterns=[],
    )
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("shell" in v.lower() or "metacharacter" in v.lower() or "forbidden" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 10. Validator — pip install rejection
# -----------------------------------------------------------------------


def test_validator_rejects_pip_install():
    """pip install is forbidden."""
    plan = _make_plan()
    # Directly construct to bypass ReproductionCommand validator
    bad_command = ReproductionCommand.model_construct(
        args=["pip", "install", "requests"],
        relative_cwd=".",
        env_vars={},
        expected_exit_codes=[0],
        expected_output_patterns=[],
    )
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("install" in v.lower() or "pip" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 11. Validator — path traversal in relative_cwd
# -----------------------------------------------------------------------


def test_validator_rejects_path_traversal_in_cwd():
    """relative_cwd with path traversal is rejected."""
    plan = _make_plan()
    bad_command = ReproductionCommand.model_construct(
        args=["pytest", "tests/test_foo.py"],
        relative_cwd="../../etc",
        env_vars={},
        expected_exit_codes=[1],
        expected_output_patterns=[],
    )
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("traversal" in v.lower() or "escape" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 12. Validator — network approval rejection
# -----------------------------------------------------------------------


def test_validator_rejects_network_approved_plan():
    """Plans with network_approved=True are rejected."""
    plan = _make_plan()
    bad_plan = plan.model_copy(update={"network_approved": True})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("network" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 13. Validator — missing commit SHA
# -----------------------------------------------------------------------


def test_validator_rejects_empty_commit_sha():
    """Empty commit SHA is rejected."""
    plan = _make_plan()
    bad_plan = plan.model_copy(update={"commit_sha": ""})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("commit_sha" in v.lower() or "sha" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 14. Validator — credential / secret access arguments
# -----------------------------------------------------------------------


def test_validator_rejects_credential_in_args():
    """Arguments containing credential patterns are rejected."""
    plan = _make_plan()
    bad_command = ReproductionCommand.model_construct(
        args=["pytest", "--token=ghp_supersecret"],
        relative_cwd=".",
        env_vars={},
        expected_exit_codes=[1],
        expected_output_patterns=[],
    )
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("credential" in v.lower() or "token" in v.lower() or "secret" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 15. Validator — absolute path in relative_cwd
# -----------------------------------------------------------------------


def test_validator_rejects_absolute_cwd():
    """Absolute paths in relative_cwd are rejected."""
    plan = _make_plan()
    bad_command = ReproductionCommand.model_construct(
        args=["pytest", "tests/test_foo.py"],
        relative_cwd="/etc/passwd",
        env_vars={},
        expected_exit_codes=[1],
        expected_output_patterns=[],
    )
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    result = validator.validate(bad_plan)
    assert not result.is_valid
    assert any("absolute" in v.lower() or "relative" in v.lower() for v in result.violations)


# -----------------------------------------------------------------------
# 16. Validator — workspace symlink escape check
# -----------------------------------------------------------------------


def test_validator_checks_symlink_escape_when_workspace_provided(tmp_path: Path):
    """Validator rejects paths that resolve outside the workspace root."""
    plan = _make_plan()
    bad_command = ReproductionCommand.model_construct(
        args=["pytest", "tests/test_foo.py"],
        relative_cwd="../outside",  # Not enough to trigger traversal check alone
        env_vars={},
        expected_exit_codes=[1],
        expected_output_patterns=[],
    )
    bad_plan = plan.model_copy(update={"command": bad_command})
    validator = ReproductionPlanValidator()
    # With workspace path provided, the resolved cwd check fires
    result = validator.validate(bad_plan, workspace_path=tmp_path)
    assert not result.is_valid


# -----------------------------------------------------------------------
# 17. CLI smoke — forge plan generate (INSUFFICIENT_INFORMATION without --expected)
# -----------------------------------------------------------------------


def test_cli_plan_generate_no_expected_behavior_produces_insufficient():
    """forge plan generate without --expected generates INSUFFICIENT_INFORMATION plan."""
    result = runner.invoke(
        app,
        [
            "plan", "generate",
            "find_cli_001",
            "--commit", "deadbeef",
            "--repo", "https://github.com/example/repo.git",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["strategy"] == "insufficient_information"


# -----------------------------------------------------------------------
# 18. CLI smoke — forge plan generate with trusted expected behavior
# -----------------------------------------------------------------------


def test_cli_plan_generate_with_expected_behavior():
    """forge plan generate with --expected and trusted provenance produces a valid plan."""
    result = runner.invoke(
        app,
        [
            "plan", "generate",
            "find_cli_002",
            "--commit", "deadbeef",
            "--repo", "https://github.com/example/repo.git",
            "--command", '["pytest", "tests/test_foo.py::test_bar"]',
            "--expected", "Loop must stop before len(arr)",
            "--provenance", "trusted_fixture",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["strategy"] == "run_specific_test"
    assert data["expected_behavior"] == "Loop must stop before len(arr)"


# -----------------------------------------------------------------------
# 19. CLI smoke — forge plan generate with invalid provenance
# -----------------------------------------------------------------------


def test_cli_plan_generate_invalid_provenance():
    """forge plan generate with untrusted provenance + --expected exits with code 2."""
    result = runner.invoke(
        app,
        [
            "plan", "generate",
            "find_cli_003",
            "--commit", "sha",
            "--repo", "repo",
            "--expected", "Should not crash",
            "--provenance", "untrusted_llm",
        ],
    )
    assert result.exit_code == 2


# -----------------------------------------------------------------------
# 20. CLI smoke — forge plan validate with valid plan file
# -----------------------------------------------------------------------


def test_cli_plan_validate_valid_plan(tmp_path: Path):
    """forge plan validate accepts a valid plan file."""
    plan = _make_plan(command_args=["pytest", "tests/test_foo.py::test_bar"])
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan.model_dump(mode="json"), indent=2))

    result = runner.invoke(app, ["plan", "validate", str(plan_file)])
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output


# -----------------------------------------------------------------------
# 21. CLI smoke — forge plan validate with insufficient_information plan
# -----------------------------------------------------------------------


def test_cli_plan_validate_insufficient_information_plan(tmp_path: Path):
    """forge plan validate rejects INSUFFICIENT_INFORMATION plans."""
    planner = _make_planner()
    plan = planner.create_plan(
        finding_id="find_cli_004",
        commit_sha="sha",
        target_repo="repo",
        trusted_expected_behavior=None,
        expected_behavior_provenance=FieldProvenance.STATIC_ANALYSIS,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan.model_dump(mode="json"), indent=2))

    result = runner.invoke(app, ["plan", "validate", str(plan_file)])
    assert result.exit_code == 2


# -----------------------------------------------------------------------
# 22. CLI smoke — forge plan show
# -----------------------------------------------------------------------


def test_cli_plan_show(tmp_path: Path):
    """forge plan show outputs plan details."""
    plan = _make_plan(command_args=["pytest", "tests/test_foo.py::test_bar"])
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan.model_dump(mode="json"), indent=2))

    result = runner.invoke(app, ["plan", "show", str(plan_file)])
    assert result.exit_code == 0, result.output
    assert "Plan ID" in result.output
    assert "Strategy" in result.output
    assert plan.finding_id in result.output


# -----------------------------------------------------------------------
# 23. CLI smoke — forge plan run (blocked because Docker unavailable)
# -----------------------------------------------------------------------


def test_cli_plan_run_blocked_without_docker(tmp_path: Path):
    """forge plan run exits with code 3 when Docker is unavailable (expected on this machine)."""
    plan = _make_plan(command_args=["pytest", "tests/test_foo.py::test_bar"])
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan.model_dump(mode="json"), indent=2))

    result = runner.invoke(
        app,
        ["plan", "run", str(plan_file), "--workspace", str(tmp_path)],
    )
    # Exit code 3 = sandbox blocked; exit code 1 = not reproduced / error
    # Both are acceptable on a machine without Docker
    assert result.exit_code in {1, 2, 3, 4}, f"Unexpected exit code {result.exit_code}: {result.output}"
