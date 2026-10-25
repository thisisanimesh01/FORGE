"""Unit and regression tests for Phase 2 Milestone 3: Review Quality Benchmarking."""

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.benchmarks.review_corpus import ReviewCorpusLoader, ReviewCorpusError
from forge.benchmarks.review_metrics import calculate_review_metrics
from forge.benchmarks.review_models import (
    ReviewBenchmarkCategory,
    ReviewBenchmarkFixtureMeta,
    ReviewBenchmarkRunResult,
    ReviewCaseExecutionResult,
    ReviewCaseOutcome,
    ExpectedReviewOutcome,
    ExpectedReviewFinding,
)
from forge.benchmarks.review_runner import ReviewBenchmarkRunner
from forge.benchmarks.review_reporting import (
    review_benchmark_to_json,
    review_benchmark_to_markdown,
)
from forge.cli import app
from forge.review.models import FindingCategory
from forge.schemas import FindingSeverity

runner = CliRunner()


class TestReviewBenchmarkMetrics:
    """Mathematical verification of review metric formulas and invariants."""

    def test_perfect_metrics(self):
        results = [
            ReviewCaseExecutionResult(
                fixture_id="f1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.PASS,
                has_defects_expected=True,
                true_positives=1,
                false_positives=0,
                false_negatives=0,
                true_negatives=0,
                valid_evidence_count=1,
                total_evidence_count=1,
                details={"location_candidates": 1, "location_matches": 1},
            ),
            ReviewCaseExecutionResult(
                fixture_id="f2",
                category=ReviewBenchmarkCategory.CLEAN_CONTROL,
                outcome=ReviewCaseOutcome.PASS,
                has_defects_expected=False,
                true_positives=0,
                false_positives=0,
                false_negatives=0,
                true_negatives=1,
                valid_evidence_count=0,
                total_evidence_count=0,
            ),
        ]
        metrics = calculate_review_metrics(results)
        assert metrics.total_cases == 2
        assert metrics.passed_cases == 2
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0
        assert metrics.f1_score == 1.0
        assert metrics.specificity == 1.0
        assert metrics.false_positive_rate == 0.0
        assert metrics.case_pass_rate == 1.0
        assert metrics.location_accuracy == 1.0
        assert metrics.evidence_validity_rate == 1.0

    def test_zero_denominator_yields_none_not_fabricated(self):
        # Case with 0 predictions and 0 expected defects (only clean)
        results = [
            ReviewCaseExecutionResult(
                fixture_id="clean1",
                category=ReviewBenchmarkCategory.CLEAN_CONTROL,
                outcome=ReviewCaseOutcome.PASS,
                has_defects_expected=False,
                true_positives=0,
                false_positives=0,
                false_negatives=0,
                true_negatives=1,
            )
        ]
        metrics = calculate_review_metrics(results)
        # Precision = TP / (TP + FP) = 0/0 -> None
        assert metrics.precision is None
        # Recall = TP / (TP + FN) = 0/0 -> None
        assert metrics.recall is None
        assert metrics.f1_score is None
        assert metrics.specificity == 1.0  # TN / (TN + FP) = 1/1 = 1.0
        assert metrics.false_positive_rate == 0.0

    def test_imperfect_precision_and_recall(self):
        results = [
            # 1 true positive, 1 false positive
            ReviewCaseExecutionResult(
                fixture_id="f1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=True,
                true_positives=1,
                false_positives=1,
                false_negatives=1,  # 1 missed defect
                true_negatives=0,
            ),
        ]
        metrics = calculate_review_metrics(results)
        # Precision: 1 / (1 + 1) = 0.5
        assert metrics.precision == 0.5
        # Recall: 1 / (1 + 1) = 0.5
        assert metrics.recall == 0.5
        # F1 = 2 * (0.5 * 0.5) / 1.0 = 0.5
        assert metrics.f1_score == 0.5

    def test_evaluator_catches_false_positive_on_clean_fixture(self):
        results = [
            ReviewCaseExecutionResult(
                fixture_id="clean_falsely_flagged",
                category=ReviewBenchmarkCategory.CLEAN_CONTROL,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=False,
                true_positives=0,
                false_positives=1,
                false_negatives=0,
                true_negatives=0,
            )
        ]
        metrics = calculate_review_metrics(results)
        assert metrics.false_positives == 1
        assert metrics.failed_cases == 1
        assert metrics.case_pass_rate == 0.0

    def test_evaluator_catches_false_negative_missed_defect(self):
        results = [
            ReviewCaseExecutionResult(
                fixture_id="missed_defect",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=True,
                true_positives=0,
                false_positives=0,
                false_negatives=1,
                true_negatives=0,
            )
        ]
        metrics = calculate_review_metrics(results)
        assert metrics.false_negatives == 1
        assert metrics.recall == 0.0
        assert metrics.failed_cases == 1

    def test_wrong_file_attribution_counted_as_false_positive(self):
        results = [
            ReviewCaseExecutionResult(
                fixture_id="attrib_fail",
                category=ReviewBenchmarkCategory.ATTRIBUTION_CHALLENGE,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=True,
                true_positives=0,
                false_positives=1,
                false_negatives=1,
                true_negatives=0,
                wrong_file_attributions=1,
            )
        ]
        metrics = calculate_review_metrics(results)
        assert metrics.wrong_file_attributions == 1
        assert metrics.false_positives == 1

    def test_errors_and_skipped_never_become_true_negatives_or_passed(self):
        results = [
            ReviewCaseExecutionResult(
                fixture_id="err1",
                category=ReviewBenchmarkCategory.CLEAN_CONTROL,
                outcome=ReviewCaseOutcome.ERROR,
                has_defects_expected=False,
                error_message="SyntaxError in diff",
            ),
            ReviewCaseExecutionResult(
                fixture_id="skip1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.SKIPPED_UNAVAILABLE,
                has_defects_expected=True,
                error_message="Docker unavailable",
            ),
        ]
        metrics = calculate_review_metrics(results)
        assert metrics.error_cases == 1
        assert metrics.skipped_unavailable_cases == 1
        assert metrics.passed_cases == 0
        assert metrics.true_negatives == 0
        assert metrics.true_positives == 0


class TestReviewBenchmarkCorpus:
    """Fixture integrity and manifest schema validation."""

    def test_discover_default_fixtures(self):
        loader = ReviewCorpusLoader()
        fixtures = loader.discover_fixtures()
        assert len(fixtures) == 10
        fixture_ids = [f.fixture_id for f in fixtures]
        assert fixture_ids == sorted(fixture_ids)  # Deterministic sorting
        assert "RBM01_path_traversal" in fixture_ids
        assert "RBM07_prompt_injection" in fixture_ids
        assert "RBM10_empty_boundary" in fixture_ids

    def test_duplicate_fixture_id_rejected(self, tmp_path: Path):
        f1 = tmp_path / "fix1"
        f1.mkdir()
        (f1 / "fixture.json").write_text(json.dumps({
            "fixture_id": "DUP01",
            "name": "Dup 1",
            "description": "d",
            "category": "genuine_security",
            "expected": {"has_defects": False}
        }), encoding="utf-8")

        f2 = tmp_path / "fix2"
        f2.mkdir()
        (f2 / "fixture.json").write_text(json.dumps({
            "fixture_id": "DUP01",
            "name": "Dup 2",
            "description": "d",
            "category": "genuine_security",
            "expected": {"has_defects": False}
        }), encoding="utf-8")

        loader = ReviewCorpusLoader(fixtures_dir=tmp_path)
        with pytest.raises(ReviewCorpusError, match="Duplicate review fixture ID"):
            loader.discover_fixtures()

    def test_malformed_fixture_json_rejected(self, tmp_path: Path):
        f = tmp_path / "bad"
        f.mkdir()
        (f / "fixture.json").write_text("not json content", encoding="utf-8")

        loader = ReviewCorpusLoader(fixtures_dir=tmp_path)
        with pytest.raises(ReviewCorpusError, match="Malformed JSON"):
            loader.discover_fixtures()


class TestReviewBenchmarkRunner:
    """Benchmark execution harness and integrity guarantees."""

    def test_runner_runs_static_suite_deterministically(self):
        runner = ReviewBenchmarkRunner()
        res1 = runner.run_suite(mode="static")
        res2 = runner.run_suite(mode="static")

        assert res1.benchmark_run_id == res2.benchmark_run_id
        assert res1.metrics.total_cases == res2.metrics.total_cases
        assert res1.metrics.passed_cases == res2.metrics.passed_cases
        assert res1.metrics.true_positives == res2.metrics.true_positives

    def test_runner_fixture_filter(self):
        runner = ReviewBenchmarkRunner()
        res = runner.run_suite(mode="static", fixture_filter="RBM01_path_traversal")
        assert len(res.case_results) == 1
        assert res.case_results[0].fixture_id == "RBM01_path_traversal"
        assert res.case_results[0].outcome == ReviewCaseOutcome.PASS

    def test_skipped_when_docker_required_and_unavailable(self, tmp_path: Path):
        fix_dir = tmp_path / "fix_dock"
        fix_dir.mkdir()
        meta = ReviewBenchmarkFixtureMeta(
            fixture_id="DOCK01",
            name="Requires Docker",
            description="desc",
            category=ReviewBenchmarkCategory.GENUINE_SECURITY,
            expected=ExpectedReviewOutcome(
                has_defects=True,
                requires_docker=True,
            ),
            fixture_dir=fix_dir,
        )

        runner = ReviewBenchmarkRunner()
        case_res = runner.evaluate_fixture(
            fixture=meta,
            mode="static",
            docker_available=False,
        )
        assert case_res.outcome == ReviewCaseOutcome.SKIPPED_UNAVAILABLE
        assert "Docker unavailable" in case_res.error_message

    def test_prompt_injection_does_not_modify_execution_permissions(self):
        # Fixture RBM07 contains adversarial system override text
        runner = ReviewBenchmarkRunner()
        res = runner.run_suite(mode="static", fixture_filter="RBM07_prompt_injection")
        assert len(res.case_results) == 1
        case = res.case_results[0]
        # Should detect the prompt injection as a finding, not follow its directives
        assert case.outcome == ReviewCaseOutcome.PASS
        assert case.defects_detected >= 1


class TestReviewBenchmarkCLI:
    """CLI testing for `forge benchmark review` command."""

    def test_cli_benchmark_review_markdown(self):
        result = runner.invoke(app, ["benchmark", "review", "--format", "markdown"])
        assert result.exit_code == 0
        assert "FORGE Review Quality Benchmark Report" in result.output
        assert "Summary Metrics" in result.output

    def test_cli_benchmark_review_json(self):
        result = runner.invoke(app, ["benchmark", "review", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["schema_version"] == "1.0.0"
        assert parsed["metrics"]["total_cases"] == 10
        assert parsed["metrics"]["passed_cases"] == 10

    def test_cli_benchmark_review_export_file(self, tmp_path: Path):
        out_file = tmp_path / "export" / "bm_review.json"
        result = runner.invoke(app, [
            "benchmark",
            "review",
            "--format",
            "json",
            "-o",
            str(out_file),
        ])
        assert result.exit_code == 0
        assert out_file.exists()
        parsed = json.loads(out_file.read_text(encoding="utf-8"))
        assert parsed["metrics"]["total_cases"] == 10

    def test_cli_benchmark_review_invalid_mode_fails(self):
        result = runner.invoke(app, ["benchmark", "review", "--mode", "invalid_mode"])
        assert result.exit_code == 1
        assert "Invalid mode" in result.output

    def test_cli_benchmark_review_heldout_corpus(self):
        result = runner.invoke(app, ["benchmark", "review", "--corpus", "heldout", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["metrics"]["total_cases"] == 8
        assert parsed["metrics"]["case_true_positives"] == 6
        assert parsed["metrics"]["case_true_negatives"] == 2
        assert parsed["metrics"]["case_false_negatives"] == 0


class TestAdversarialHeldoutBenchmark:
    """Evaluation on held-out adversarial corpus."""

    def test_heldout_fixtures_discovered_deterministically(self):
        from forge.benchmarks.review_corpus import ReviewCorpusLoader, DEFAULT_ADVERSARIAL_FIXTURES_DIR

        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_ADVERSARIAL_FIXTURES_DIR)
        fixtures = loader.discover_fixtures()
        assert len(fixtures) == 8
        ids = [f.fixture_id for f in fixtures]
        assert ids == sorted(ids)
        assert "ADV01_novel_shell_subproc" in ids
        assert "ADV08_renamed_file_clean" in ids

    def test_heldout_evaluation_determinism(self):
        from forge.benchmarks.review_corpus import ReviewCorpusLoader, DEFAULT_ADVERSARIAL_FIXTURES_DIR

        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_ADVERSARIAL_FIXTURES_DIR)
        runner = ReviewBenchmarkRunner(loader=loader)
        res1 = runner.run_suite(mode="static")
        res2 = runner.run_suite(mode="static")

        assert res1.benchmark_run_id == res2.benchmark_run_id
        assert res1.metrics.total_cases == res2.metrics.total_cases
        assert res1.metrics.case_true_positives == res2.metrics.case_true_positives
        assert res1.metrics.case_true_negatives == res2.metrics.case_true_negatives
        assert res1.metrics.finding_true_positives == res2.metrics.finding_true_positives
        assert res1.metrics.finding_false_negatives == res2.metrics.finding_false_negatives

    def test_heldout_secret_redaction_in_report(self):
        from forge.benchmarks.review_corpus import ReviewCorpusLoader, DEFAULT_ADVERSARIAL_FIXTURES_DIR
        from forge.benchmarks.review_reporting import review_benchmark_to_json, review_benchmark_to_markdown

        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_ADVERSARIAL_FIXTURES_DIR)
        runner = ReviewBenchmarkRunner(loader=loader)
        res = runner.run_suite(mode="static", fixture_filter="ADV07_redacted_auth_token")

        json_out = review_benchmark_to_json(res)
        md_out = review_benchmark_to_markdown(res)

        # Raw secret value sk-999888777666555444333222111 must never appear
        assert "sk-999888777666555444333222111" not in json_out
        assert "sk-999888777666555444333222111" not in md_out


class TestEvaluatorSelfTests:
    """Proves that intentionally injected conditions change metrics as mathematically expected."""

    def test_injected_false_positive_decreases_precision(self):
        # Baseline: 1 TP, 0 FP -> Precision 1.0
        base_res = [
            ReviewCaseExecutionResult(
                fixture_id="c1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.PASS,
                has_defects_expected=True,
                true_positives=1,
                false_positives=0,
                false_negatives=0,
                true_negatives=0,
            )
        ]
        m_base = calculate_review_metrics(base_res)
        assert m_base.precision == 1.0

        # Injected FP: 1 TP, 1 FP -> Precision drops to 0.5
        injected_res = [
            ReviewCaseExecutionResult(
                fixture_id="c1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=True,
                true_positives=1,
                false_positives=1,
                false_negatives=0,
                true_negatives=0,
            )
        ]
        m_injected = calculate_review_metrics(injected_res)
        assert m_injected.precision == 0.5
        assert m_injected.precision < m_base.precision

    def test_injected_false_negative_decreases_recall(self):
        # Baseline: 2 TP, 0 FN -> Recall 1.0
        base_res = [
            ReviewCaseExecutionResult(
                fixture_id="c1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.PASS,
                has_defects_expected=True,
                true_positives=2,
                false_positives=0,
                false_negatives=0,
                true_negatives=0,
            )
        ]
        m_base = calculate_review_metrics(base_res)
        assert m_base.recall == 1.0

        # Injected FN: 1 TP, 1 FN -> Recall drops to 0.5
        injected_res = [
            ReviewCaseExecutionResult(
                fixture_id="c1",
                category=ReviewBenchmarkCategory.GENUINE_SECURITY,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=True,
                true_positives=1,
                false_positives=0,
                false_negatives=1,
                true_negatives=0,
            )
        ]
        m_injected = calculate_review_metrics(injected_res)
        assert m_injected.recall == 0.5
        assert m_injected.recall < m_base.recall

    def test_injected_duplicate_prediction_does_not_inflate_tp(self):
        # Deduplication check in ReviewBenchmarkRunner
        runner = ReviewBenchmarkRunner()
        # RBM09 has two distinct nearby defects. If deduplicated properly, len is 2
        res = runner.run_suite(mode="static", fixture_filter="RBM09_duplicate_and_nearby")
        assert len(res.case_results) == 1
        case = res.case_results[0]
        assert case.true_positives == 2
        assert case.false_positives == 0

    def test_injected_wrong_file_attribution_penalizes_fp_and_wrong_file_count(self):
        res = [
            ReviewCaseExecutionResult(
                fixture_id="attrib_test",
                category=ReviewBenchmarkCategory.ATTRIBUTION_CHALLENGE,
                outcome=ReviewCaseOutcome.FAIL,
                has_defects_expected=True,
                true_positives=0,
                false_positives=1,
                false_negatives=1,
                true_negatives=0,
                wrong_file_attributions=1,
            )
        ]
        m = calculate_review_metrics(res)
        assert m.wrong_file_attributions == 1
        assert m.false_positives == 1
        assert m.precision == 0.0

    def test_injected_invalid_evidence_decreases_validity_rate(self):
        # 1 valid evidence, 1 invalid evidence
        res = [
            ReviewCaseExecutionResult(
                fixture_id="ev_test",
                category=ReviewBenchmarkCategory.EVIDENCE_CALIBRATION,
                outcome=ReviewCaseOutcome.PASS,
                has_defects_expected=True,
                true_positives=1,
                false_positives=0,
                false_negatives=0,
                true_negatives=0,
                valid_evidence_count=1,
                total_evidence_count=2,
            )
        ]
        m = calculate_review_metrics(res)
        assert m.evidence_validity_rate == 0.5
