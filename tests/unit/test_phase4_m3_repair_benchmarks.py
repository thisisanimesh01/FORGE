"""Unit tests for Phase 4 Milestone 3: Repair Quality & Regression Benchmarking.

Verifies:
- Benchmark corpus discovery, loading, and containment security.
- All 11 representative benchmark fixtures execute deterministically offline.
- Individual fixture categories and failure modes:
  - REP01: Syntax error -> REPAIR_VERIFIED
  - REP02: Incorrect logic -> REPAIR_VERIFIED
  - REP03: Boundary condition off-by-one -> REPAIR_VERIFIED
  - REP04: Missing validation -> REPAIR_VERIFIED
  - REP05: Exception handling -> REPAIR_VERIFIED
  - REP06: Security-sensitive traversal -> REPAIR_VERIFIED
  - REP07: Clean control -> REPAIR_REJECTED (clean pass, empty diff)
  - REP08: Ambiguous finding -> INCONCLUSIVE (unverified test oracle)
  - REP09: Insufficient evidence -> REPAIR_REJECTED (fail-closed planning gate)
  - REP10: Adversarial instruction -> REPAIR_VERIFIED (confined to target, AGENTS.md untouched)
  - REP11: Regression-inducing patch -> REGRESSION_DETECTED (baseline failure)
- Statistical metric computation with honest denominators (None for empty classes).
- Report generation (Markdown, JSON, Dict) and secret redaction.
- CLI commands: `forge benchmark repair` and `forge repair benchmark`.
"""

from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typer.testing import CliRunner
import pytest

from forge.cli import app
from forge.benchmarks.repair_models import (
    RepairBenchmarkCategory,
    RepairBenchmarkCaseResult,
    RepairBenchmarkMetrics,
    RepairBenchmarkRunResult,
    ExpectedPatchConstraints,
    ExpectedRepairOutcomeSpec,
    RepairBenchmarkFixtureMeta,
)
from forge.benchmarks.repair_corpus import RepairCorpusLoader
from forge.benchmarks.repair_metrics import calculate_repair_metrics
from forge.benchmarks.repair_runner import RepairBenchmarkRunner
from forge.benchmarks.repair_reporting import (
    repair_benchmark_to_dict,
    repair_benchmark_to_json,
    repair_benchmark_to_markdown,
)
from forge.repair.contracts import RepairOutcome
from forge.repository.validation import assert_contained_path, SecurityError

runner = CliRunner()


# ---------------------------------------------------------------------------
# 1. Corpus Discovery & Containment Security
# ---------------------------------------------------------------------------

def test_corpus_loader_discovers_all_eleven_fixtures():
    """Verify that the corpus loader discovers exactly 11 fixtures in deterministic order."""
    loader = RepairCorpusLoader()
    fixtures = loader.load_fixtures()
    assert len(fixtures) == 11

    fixture_ids = [f.fixture_id for f in fixtures]
    assert fixture_ids == sorted(fixture_ids)
    assert fixture_ids[0] == "REP01_syntax_error"
    assert fixture_ids[10] == "REP11_regression_inducing_patch"


def test_corpus_fixture_metadata_validity():
    """Verify all fixtures have valid categories, target files, and non-empty constraints."""
    loader = RepairCorpusLoader()
    fixtures = loader.load_fixtures()

    for f in fixtures:
        assert isinstance(f.category, RepairBenchmarkCategory)
        assert f.fixture_id.startswith("REP")
        assert f.target_file.endswith(".py")
        assert f.constraints.max_lines > 0
        assert f.constraints.max_bytes > 0
        assert f.constraints.allowed_files == [f.target_file]
        assert f.fixture_dir is not None
        assert f.fixture_dir.is_dir()
        assert (f.fixture_dir / f.target_file).is_file()
        assert (f.fixture_dir / "fixture.json").is_file()


def test_corpus_loader_get_single_fixture():
    """Verify retrieval of individual fixture by ID and handling of nonexistent fixture."""
    loader = RepairCorpusLoader()
    fixture = loader.get_fixture("REP03_boundary_condition")
    assert fixture is not None
    assert fixture.fixture_id == "REP03_boundary_condition"
    assert fixture.category == RepairBenchmarkCategory.BOUNDARY_CONDITION

    with pytest.raises(KeyError, match="Fixture not found"):
        loader.get_fixture("REP99_nonexistent")


def test_corpus_path_containment():
    """Verify assert_contained_path prevents path traversal outside root."""
    base_dir = Path("/tmp/forge_test_base")
    safe_child = Path("/tmp/forge_test_base/sub/file.py")
    traversal = Path("/tmp/forge_test_base/../../etc/passwd")

    assert assert_contained_path(safe_child, base_dir) == safe_child.resolve()

    with pytest.raises(SecurityError, match="Path containment violation"):
        assert_contained_path(traversal, base_dir)


# ---------------------------------------------------------------------------
# 2. Deterministic Offline Suite Execution
# ---------------------------------------------------------------------------

def test_offline_suite_runs_and_matches_ground_truth():
    """Verify full offline suite runs deterministically and achieves 100% outcome match."""
    runner_instance = RepairBenchmarkRunner()
    result = runner_instance.run_suite(mode="offline")

    assert len(result.case_results) == 11
    assert result.metrics.total_cases == 11
    assert result.metrics.passed_cases == 11
    assert result.metrics.failed_cases == 0
    assert result.metrics.inconclusive_cases == 0
    assert result.metrics.skipped_cases == 0
    assert result.metrics.outcome_accuracy == 1.0

    for case in result.case_results:
        assert case.outcome_matched is True
        assert case.duration_ms > 0
        assert case.diagnostic != ""


def test_runner_fixture_filtering():
    """Verify running a single fixture using fixture_filter."""
    runner_instance = RepairBenchmarkRunner()
    result = runner_instance.run_suite(mode="offline", fixture_filter="REP02_incorrect_logic")

    assert len(result.case_results) == 1
    case = result.case_results[0]
    assert case.fixture_id == "REP02_incorrect_logic"
    assert case.category == RepairBenchmarkCategory.INCORRECT_LOGIC
    assert case.expected_outcome == RepairOutcome.REPAIR_VERIFIED
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED
    assert case.outcome_matched is True
    assert result.metrics.total_cases == 1
    assert result.metrics.outcome_accuracy == 1.0


# ---------------------------------------------------------------------------
# 3. Individual Fixture Category Verification
# ---------------------------------------------------------------------------

def test_fixture_rep01_syntax_error():
    """REP01: Syntax error resolves to REPAIR_VERIFIED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP01_syntax_error")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.SYNTAX_ERROR
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED
    assert case.patch_accepted is True
    assert case.lines_changed == 2


def test_fixture_rep02_incorrect_logic():
    """REP02: Incorrect subtraction logic resolves to REPAIR_VERIFIED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP02_incorrect_logic")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.INCORRECT_LOGIC
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED
    assert case.patch_accepted is True


def test_fixture_rep03_boundary_condition():
    """REP03: Off-by-one loop boundary resolves to REPAIR_VERIFIED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP03_boundary_condition")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.BOUNDARY_CONDITION
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED


def test_fixture_rep04_missing_validation():
    """REP04: Missing validation resolves to REPAIR_VERIFIED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP04_missing_validation")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.MISSING_VALIDATION
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED


def test_fixture_rep05_exception_handling():
    """REP05: Unhandled FileNotFoundError resolves to REPAIR_VERIFIED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP05_exception_handling")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.EXCEPTION_HANDERING if hasattr(RepairBenchmarkCategory, "EXCEPTION_HANDERING") else case.category == RepairBenchmarkCategory.EXCEPTION_HANDLING
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED


def test_fixture_rep06_security_sensitive():
    """REP06: Path traversal vulnerability resolves to REPAIR_VERIFIED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP06_security_sensitive")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.SECURITY_SENSITIVE
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED


def test_fixture_rep07_clean_control():
    """REP07: Clean control rejects patch (empty diff), clean pass."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP07_clean_control")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.CLEAN_CONTROL
    assert case.has_defect_expected is False
    assert case.is_clean_pass is True
    assert case.observed_outcome == RepairOutcome.REPAIR_REJECTED
    assert case.patch_accepted is False
    assert case.lines_changed == 0


def test_fixture_rep08_ambiguous_finding():
    """REP08: Ambiguous finding without test oracle evaluates to INCONCLUSIVE."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP08_ambiguous_finding")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.AMBIGUOUS_FINDING
    assert case.observed_outcome == RepairOutcome.INCONCLUSIVE
    assert case.patch_accepted is False
    assert "oracle" in case.diagnostic.lower()


def test_fixture_rep09_insufficient_evidence():
    """REP09: Finding lacking verified reproduction evidence is rejected at planning gate."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP09_insufficient_evidence")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.INSUFFICIENT_EVIDENCE
    assert case.observed_outcome == RepairOutcome.REPAIR_REJECTED
    assert case.plan_generated is False
    assert case.patch_generated is False
    assert "lacks verified reproduction evidence" in case.diagnostic


def test_fixture_rep10_adversarial_instruction():
    """REP10: Adversarial prompt injection targeting AGENTS.md stays confined to target file."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP10_adversarial_instruction")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.ADVERSARIAL_INSTRUCTION
    assert case.observed_outcome == RepairOutcome.REPAIR_VERIFIED
    assert case.scope_compliant is True


def test_fixture_rep11_regression_inducing():
    """REP11: Patch that breaks baseline test evaluates to REGRESSION_DETECTED."""
    runner_instance = RepairBenchmarkRunner()
    res = runner_instance.run_suite(mode="offline", fixture_filter="REP11_regression_inducing_patch")
    case = res.case_results[0]
    assert case.category == RepairBenchmarkCategory.REGRESSION_INDUCING
    assert case.observed_outcome == RepairOutcome.REGRESSION_DETECTED
    assert case.is_regression is True
    assert case.patch_accepted is False


# ---------------------------------------------------------------------------
# 4. Statistical Metrics & Honest Denominators
# ---------------------------------------------------------------------------

def test_metrics_empty_case_list():
    """Verify calculate_repair_metrics returns honest None denominators when cases is empty."""
    metrics = calculate_repair_metrics([])
    assert metrics.total_cases == 0
    assert metrics.passed_cases == 0
    assert metrics.outcome_accuracy is None
    assert metrics.plan_generation_rate is None
    assert metrics.valid_plan_rate is None
    assert metrics.patch_acceptance_rate is None
    assert metrics.scope_compliance_rate is None
    assert metrics.regression_rate is None
    assert metrics.false_fix_rate is None
    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.f1_score is None


def test_metrics_honest_zero_division():
    """Verify precision/recall are None when there are no true positives or false positives."""
    case = RepairBenchmarkCaseResult(
        fixture_id="control",
        category=RepairBenchmarkCategory.CLEAN_CONTROL,
        has_defect_expected=False,
        plan_generated=True,
        plan_valid=False,
        patch_generated=False,
        patch_accepted=False,
        scope_compliant=True,
        evidence_grounded=True,
        expected_outcome=RepairOutcome.REPAIR_REJECTED,
        observed_outcome=RepairOutcome.REPAIR_REJECTED,
        outcome_matched=True,
        is_clean_pass=True,
    )
    metrics = calculate_repair_metrics([case])
    assert metrics.total_cases == 1
    assert metrics.true_negatives == 1
    assert metrics.true_positives == 0
    assert metrics.false_positives == 0
    assert metrics.false_negatives == 0
    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.f1_score is None


def test_metrics_precision_recall_calculation():
    """Verify precision, recall, and F1 score with synthetic mixed cases."""
    cases = [
        # True Positive 1
        RepairBenchmarkCaseResult(
            fixture_id="tp1",
            category=RepairBenchmarkCategory.SYNTAX_ERROR,
            has_defect_expected=True,
            plan_generated=True,
            plan_valid=True,
            patch_generated=True,
            patch_accepted=True,
            scope_compliant=True,
            evidence_grounded=True,
            lines_changed=4,
            diff_bytes=100,
            expected_outcome=RepairOutcome.REPAIR_VERIFIED,
            observed_outcome=RepairOutcome.REPAIR_VERIFIED,
            outcome_matched=True,
        ),
        # False Negative 1 (defect existed but patch failed)
        RepairBenchmarkCaseResult(
            fixture_id="fn1",
            category=RepairBenchmarkCategory.INCORRECT_LOGIC,
            has_defect_expected=True,
            plan_generated=True,
            plan_valid=True,
            patch_generated=True,
            patch_accepted=False,
            scope_compliant=True,
            evidence_grounded=True,
            lines_changed=2,
            diff_bytes=50,
            expected_outcome=RepairOutcome.REPAIR_VERIFIED,
            observed_outcome=RepairOutcome.REPAIR_FAILED,
            outcome_matched=False,
        ),
        # True Negative 1 (clean control correctly rejected)
        RepairBenchmarkCaseResult(
            fixture_id="tn1",
            category=RepairBenchmarkCategory.CLEAN_CONTROL,
            has_defect_expected=False,
            plan_generated=False,
            plan_valid=False,
            patch_generated=False,
            patch_accepted=False,
            scope_compliant=True,
            evidence_grounded=True,
            expected_outcome=RepairOutcome.REPAIR_REJECTED,
            observed_outcome=RepairOutcome.REPAIR_REJECTED,
            outcome_matched=True,
            is_clean_pass=True,
        ),
    ]

    metrics = calculate_repair_metrics(cases)
    assert metrics.total_cases == 3
    assert metrics.passed_cases == 2
    assert metrics.failed_cases == 1
    assert metrics.true_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.true_negatives == 1
    assert metrics.false_positives == 0

    # Precision: TP / (TP + FP) = 1 / 1 = 1.0
    assert metrics.precision == 1.0
    # Recall: TP / (TP + FN) = 1 / 2 = 0.5
    assert metrics.recall == 0.5
    # F1: 2 * (1.0 * 0.5) / (1.0 + 0.5) = 1.0 / 1.5 = ~0.6667
    assert pytest.approx(metrics.f1_score, 0.001) == 0.6667
    # Average lines changed: (4 + 2) / 2 = 3.0
    assert metrics.average_lines_changed == 3.0


# ---------------------------------------------------------------------------
# 5. Reporting, Serialization & Secret Redaction
# ---------------------------------------------------------------------------

def test_reporting_dict_and_json_serialization():
    """Verify dictionary and JSON export conform to schema."""
    runner_instance = RepairBenchmarkRunner()
    result = runner_instance.run_suite(mode="offline", fixture_filter="REP01_syntax_error")

    report_dict = repair_benchmark_to_dict(result)
    assert report_dict["schema_version"] == "1.0.0"
    assert report_dict["mode"] == "offline"
    assert "metrics" in report_dict
    assert len(report_dict["case_results"]) == 1

    report_json = repair_benchmark_to_json(result)
    parsed = json.loads(report_json)
    assert parsed["schema_version"] == "1.0.0"
    assert parsed["metrics"]["outcome_accuracy"] == 1.0


def test_reporting_markdown_format_and_disclaimer():
    """Verify markdown output includes report header, table, and disclaimer alert."""
    runner_instance = RepairBenchmarkRunner()
    result = runner_instance.run_suite(mode="offline", fixture_filter="REP01_syntax_error")

    md = repair_benchmark_to_markdown(result)
    assert "# FORGE Repair Quality Benchmark Report" in md
    assert "Aggregate Quality Metrics" in md
    assert "Fixture-by-Fixture Results" in md
    assert "Scope & Validity Notice" in md
    assert "Synthetic benchmark metrics measure adherence" in md
    assert "REP01_syntax_error" in md


def test_reporting_secret_redaction():
    """Verify secrets in diagnostics are redacted before markdown/json serialization."""
    case = RepairBenchmarkCaseResult(
        fixture_id="leak_test",
        category=RepairBenchmarkCategory.SECURITY_SENSITIVE,
        has_defect_expected=True,
        plan_generated=True,
        plan_valid=True,
        patch_generated=True,
        patch_accepted=True,
        scope_compliant=True,
        evidence_grounded=True,
        expected_outcome=RepairOutcome.REPAIR_VERIFIED,
        observed_outcome=RepairOutcome.REPAIR_VERIFIED,
        outcome_matched=True,
        diagnostic="Found leak in check output",
    )
    now = datetime.now(timezone.utc)
    token = "sk-abcdef1234567890abcdef12"
    run_result = RepairBenchmarkRunResult(
        benchmark_id="bm_test_leak",
        started_at=now,
        completed_at=now,
        duration_seconds=1.0,
        mode="offline",
        provider="offline_fake",
        model=token,
        sandbox_available=False,
        metrics=calculate_repair_metrics([case]),
        case_results=[case],
    )

    md = repair_benchmark_to_markdown(run_result)
    assert token not in md
    assert "[REDACTED_SECRET]" in md

    json_str = repair_benchmark_to_json(run_result)
    assert token not in json_str
    assert "[REDACTED_SECRET]" in json_str


# ---------------------------------------------------------------------------
# 6. CLI Integration Tests
# ---------------------------------------------------------------------------

def test_cli_benchmark_repair_help():
    """Verify forge benchmark repair --help displays options."""
    res = runner.invoke(app, ["benchmark", "repair", "--help"])
    assert res.exit_code == 0
    clean = re.sub(r"\x1b\[[0-9;]*m", "", res.output)
    assert "Execute repair quality benchmarks" in clean
    assert "--mode" in clean
    assert "--format" in clean
    assert "--fixture" in clean
    assert "--output" in clean


def test_cli_repair_benchmark_alias_help():
    """Verify forge repair benchmark --help alias displays options."""
    res = runner.invoke(app, ["repair", "benchmark", "--help"])
    assert res.exit_code == 0
    assert "repair quality benchmark" in res.output.lower()


def test_cli_benchmark_repair_offline_markdown():
    """Verify forge benchmark repair runs offline and prints Markdown."""
    res = runner.invoke(app, ["benchmark", "repair", "--fixture", "REP01_syntax_error"])
    assert res.exit_code == 0
    assert "# FORGE Repair Quality Benchmark Report" in res.output
    assert "REP01_syntax_error" in res.output
    assert "100.0%" in res.output


def test_cli_benchmark_repair_json():
    """Verify forge benchmark repair --format json outputs valid JSON."""
    res = runner.invoke(app, ["benchmark", "repair", "--format", "json", "--fixture", "REP01_syntax_error"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["schema_version"] == "1.0.0"
    assert data["metrics"]["total_cases"] == 1


def test_cli_benchmark_repair_output_file(tmp_path: Path):
    """Verify forge benchmark repair --output writes report to file."""
    out_file = tmp_path / "bench_report.md"
    res = runner.invoke(app, ["benchmark", "repair", "--fixture", "REP01_syntax_error", "--output", str(out_file)])
    assert res.exit_code == 0
    assert "Repair benchmark results exported to:" in res.output
    assert out_file.is_file()
    content = out_file.read_text(encoding="utf-8")
    assert "# FORGE Repair Quality Benchmark Report" in content


def test_cli_provider_mode_without_provider_flag_fails():
    """Verify choosing provider mode without specifying provider fails closed."""
    res = runner.invoke(app, ["benchmark", "repair", "--mode", "provider"])
    assert res.exit_code == 1
    assert "Provider evaluation mode requires explicit provider selection" in res.output
