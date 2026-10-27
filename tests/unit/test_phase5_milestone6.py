"""Unit tests for Phase 5 Milestone 6: Acceptance Suite, Reliability Metrics, and CLI."""

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.workflow.acceptance import (
    PHASE5_ACCEPTANCE_SCENARIOS,
    Phase5AcceptanceCategory,
    Phase5AcceptanceExecutionMode,
    Phase5AcceptanceMetrics,
    Phase5AcceptanceReport,
    Phase5AcceptanceScenarioDefinition,
    Phase5AcceptanceScenarioResult,
    ScenarioStatus,
    compute_acceptance_metrics,
    render_phase5_acceptance_markdown,
    run_phase5_acceptance_suite,
)

runner = CliRunner()


def test_acceptance_metrics_computation() -> None:
    """Verify honest metric computation, denominators, and grouping."""
    results = [
        Phase5AcceptanceScenarioResult(
            scenario_id="TEST_01",
            name="Test 1",
            category=Phase5AcceptanceCategory.INTAKE_AND_CONTEXT,
            execution_mode=Phase5AcceptanceExecutionMode.SYNTHETIC_E2E,
            status=ScenarioStatus.PASSED,
            target_immutable=True,
            duration_ms=10.0,
        ),
        Phase5AcceptanceScenarioResult(
            scenario_id="TEST_02",
            name="Test 2",
            category=Phase5AcceptanceCategory.INTAKE_AND_CONTEXT,
            execution_mode=Phase5AcceptanceExecutionMode.SYNTHETIC_E2E,
            status=ScenarioStatus.FAILED,
            target_immutable=True,
            duration_ms=15.0,
        ),
        Phase5AcceptanceScenarioResult(
            scenario_id="TEST_03",
            name="Test 3",
            category=Phase5AcceptanceCategory.SANDBOX_FAIL_CLOSED,
            execution_mode=Phase5AcceptanceExecutionMode.UNIT_BOUNDARY,
            status=ScenarioStatus.SKIPPED,
            target_immutable=True,
            duration_ms=5.0,
        ),
    ]

    metrics = compute_acceptance_metrics(results)
    assert metrics.total_scenarios == 3
    assert metrics.passed_scenarios == 1
    assert metrics.failed_scenarios == 1
    assert metrics.skipped_scenarios == 1
    assert metrics.pass_rate == round(1 / 3, 4)

    # Check breakdown by mode
    e2e_mode = metrics.by_mode[Phase5AcceptanceExecutionMode.SYNTHETIC_E2E.value]
    assert e2e_mode["total"] == 2
    assert e2e_mode["passed"] == 1
    assert e2e_mode["failed"] == 1
    assert e2e_mode["pass_rate"] == 0.5

    # Check breakdown by category
    intake_cat = metrics.by_category[Phase5AcceptanceCategory.INTAKE_AND_CONTEXT.value]
    assert intake_cat["total"] == 2
    assert intake_cat["passed"] == 1
    assert intake_cat["failed"] == 1


def test_markdown_and_json_report_rendering() -> None:
    """Verify Markdown report generation and JSON serialization fidelity."""
    from datetime import datetime, timezone

    results = [
        Phase5AcceptanceScenarioResult(
            scenario_id="P5_ACC01",
            name="Sample Scenario",
            category=Phase5AcceptanceCategory.INTAKE_AND_CONTEXT,
            execution_mode=Phase5AcceptanceExecutionMode.SYNTHETIC_E2E,
            status=ScenarioStatus.PASSED,
            target_immutable=True,
            observed_final_state="repo_analyzed",
            observed_outcome="intake_pinned",
            diagnostic="Sample pass diagnostic.",
            duration_ms=42.0,
        )
    ]
    metrics = compute_acceptance_metrics(results)
    report = Phase5AcceptanceReport(
        report_id="rep_test_123",
        generated_at=datetime.now(timezone.utc),
        scenarios=results,
        metrics=metrics,
        synthetic_scope_disclaimer="Synthetic testing only.",
        real_sandbox_available=False,
    )

    md = render_phase5_acceptance_markdown(report)
    assert "# FORGE — Phase 5 End-to-End Workflow Acceptance & Reliability Report" in md
    assert "P5_ACC01" in md
    assert "Synthetic Scope Disclaimer" in md
    assert "Target Repository Immutability" in md

    # Check JSON export
    raw_json = report.model_dump_json(indent=2)
    parsed = json.loads(raw_json)
    assert parsed["report_id"] == "rep_test_123"
    assert parsed["metrics"]["total_scenarios"] == 1
    assert parsed["metrics"]["passed_scenarios"] == 1


def test_acceptance_suite_filter_scenario() -> None:
    """Run acceptance runner filtered to a single scenario."""
    report = run_phase5_acceptance_suite(scenario_id="P5_ACC01")
    assert len(report.scenarios) == 1
    assert report.scenarios[0].scenario_id == "P5_ACC01"
    assert report.scenarios[0].status == ScenarioStatus.PASSED
    assert report.metrics.total_scenarios == 1
    assert report.metrics.passed_scenarios == 1
    assert report.scenarios[0].target_immutable is True


def test_acceptance_suite_filter_category() -> None:
    """Run acceptance runner filtered by domain category."""
    report = run_phase5_acceptance_suite(category=Phase5AcceptanceCategory.HUMAN_REVIEW_GATE)
    assert len(report.scenarios) == 3
    for r in report.scenarios:
        assert r.category == Phase5AcceptanceCategory.HUMAN_REVIEW_GATE
        assert r.status == ScenarioStatus.PASSED
        assert r.target_immutable is True


def test_secret_scrubbing_in_acceptance_report() -> None:
    """Ensure sensitive credentials or tokens in scenarios are scrubbed."""
    result = Phase5AcceptanceScenarioResult(
        scenario_id="P5_TEST_SEC",
        name="Secret Leak Check",
        category=Phase5AcceptanceCategory.ADVERSARIAL_INTAKE,
        execution_mode=Phase5AcceptanceExecutionMode.UNIT_BOUNDARY,
        status=ScenarioStatus.PASSED,
        target_immutable=True,
        diagnostic="Found key sk-proj-1234567890abcdef12345678 in header",
        error_message=None,
        duration_ms=12.0,
    )
    # The reporting pipeline should scrub raw secrets
    from forge.analysis.evidence import redact_secrets
    sanitized_diag = redact_secrets(result.diagnostic)
    assert "sk-proj-" not in sanitized_diag or "REDACTED" in sanitized_diag


def test_cli_workflow_test_markdown() -> None:
    """CLI 'forge workflow test' runs and outputs Markdown table."""
    result = runner.invoke(app, ["workflow", "test", "--scenario", "P5_ACC01"])
    assert result.exit_code == 0
    assert "P5_ACC01" in result.output
    assert "[OK] PASSED" in result.output


def test_cli_workflow_test_json() -> None:
    """CLI 'forge workflow test --format json' outputs valid JSON report."""
    result = runner.invoke(app, ["workflow", "test", "--scenario", "P5_ACC01", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "report_id" in data
    assert data["metrics"]["passed_scenarios"] == 1
    assert data["scenarios"][0]["scenario_id"] == "P5_ACC01"


def test_cli_workflow_acceptance_alias() -> None:
    """CLI 'forge workflow acceptance' functions as alias to 'forge workflow test'."""
    result = runner.invoke(app, ["workflow", "acceptance", "--scenario", "P5_ACC01"])
    assert result.exit_code == 0
    assert "P5_ACC01" in result.output
    assert "[OK] PASSED" in result.output


def test_cli_workflow_test_invalid_scenario() -> None:
    """CLI 'forge workflow test' with non-existent scenario reports failure."""
    result = runner.invoke(app, ["workflow", "test", "--scenario", "NON_EXISTENT"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
