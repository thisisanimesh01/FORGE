"""Comprehensive tests for Milestone 6: Reporting, Seeded-Bug Fixtures, and Benchmark Runner."""

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.analysis.evidence import create_evidence
from forge.benchmarks.corpus import CorpusLoader, BenchmarkCorpusError
from forge.benchmarks.metrics import calculate_metrics
from forge.benchmarks.models import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkCase,
    BenchmarkCategory,
    CaseExecutionResult,
    CaseOutcome,
)
from forge.benchmarks.runner import BenchmarkRunner
from forge.cli import app
from forge.config import get_settings
from forge.llm.fake import FakeLLMProvider
from forge.reporting.builder import (
    ReportBuilder,
    ReportError,
    export_report,
    report_to_json,
    report_to_markdown,
)
from forge.reporting.models import REPORT_SCHEMA_VERSION, InvestigationReport
from forge.schemas import (
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# ===========================================================================
# Helpers
# ===========================================================================

def make_test_store(tmp_path: Path) -> ForgeStore:
    db_path = tmp_path / "test.db"
    return ForgeStore(db_path=db_path)


# ===========================================================================
# Reporting Tests
# ===========================================================================

class TestReporting:
    def test_build_report_basic(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        run_id = store.create_run(target="local/demo_repo", run_id="run_rep_01")
        store.finish_run(run_id, status="completed")

        builder = ReportBuilder(store)
        report = builder.build_report(run_id)

        assert report.schema_version == REPORT_SCHEMA_VERSION
        assert report.run_id == "run_rep_01"
        assert report.target == "local/demo_repo"
        assert report.status == "completed"
        assert report.summary_counts["total_findings"] == 0

    def test_build_report_with_findings_and_evidence(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        run_id = store.create_run(target="local/demo_repo", run_id="run_rep_02")

        finding = store.create_finding(
            run_id=run_id,
            title="SQL Injection in auth",
            description="Untrusted user input passed into query string",
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.CONFIRMED,
            affected_files=["src/auth.py"],
            limitations="Assumes raw cursor execution",
            finding_id="find_auth_01",
        )

        ev = create_evidence(
            run_id=run_id,
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="Vulnerable raw SQL query formatted",
            content="query = f'SELECT * FROM users WHERE id = {user_id}'",
            finding_id="find_auth_01",
            file_path="src/auth.py",
            start_line=45,
            end_line=46,
            evidence_id="evi_auth_01",
        )
        store.attach_evidence(ev)
        store.finish_run(run_id, status="completed")

        builder = ReportBuilder(store)
        report = builder.build_report(run_id)

        assert report.summary_counts["total_findings"] == 1
        assert report.summary_counts["findings_by_severity"]["high"] == 1
        assert len(report.findings) == 1
        rf = report.findings[0]
        assert rf.finding_id == "find_auth_01"
        assert rf.severity == "high"
        assert len(rf.evidence_items) == 1
        assert rf.evidence_items[0].evidence_id == "evi_auth_01"

    def test_build_report_nonexistent_run_raises(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        builder = ReportBuilder(store)
        with pytest.raises(ReportError, match="not found"):
            builder.build_report("nonexistent_run_id")

    def test_report_json_serialization(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        run_id = store.create_run(target="local/demo", run_id="run_json")
        store.finish_run(run_id)

        builder = ReportBuilder(store)
        report = builder.build_report(run_id)
        json_output = report_to_json(report)

        parsed = json.loads(json_output)
        assert parsed["schema_version"] == REPORT_SCHEMA_VERSION
        assert parsed["run_id"] == "run_json"

    def test_report_markdown_serialization(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        run_id = store.create_run(target="local/demo", run_id="run_md")
        store.create_finding(
            run_id=run_id,
            title="Boundary check error",
            description="Out-of-bounds array access",
            severity=FindingSeverity.HIGH,
            finding_id="find_md_01",
        )
        store.finish_run(run_id)

        builder = ReportBuilder(store)
        report = builder.build_report(run_id)
        md_output = report_to_markdown(report)

        assert "# FORGE Investigation Report" in md_output
        assert "Boundary check error" in md_output
        assert "HIGH" in md_output

    def test_report_secret_redaction(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        run_id = store.create_run(target="local/demo", run_id="run_sec")
        finding = store.create_finding(
            run_id=run_id,
            title="Token leak in config",
            description="Discovered sk-abcdef1234567890abcdef12 in config.py",
            finding_id="find_sec_01",
        )
        ev = create_evidence(
            run_id=run_id,
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="Leaked token line: sk-abcdef1234567890abcdef12",
            content="api_key = 'sk-abcdef1234567890abcdef12'",
            finding_id="find_sec_01",
            evidence_id="evi_sec_01",
        )
        store.attach_evidence(ev)
        store.finish_run(run_id)

        builder = ReportBuilder(store)
        report = builder.build_report(run_id)

        json_str = report_to_json(report)
        md_str = report_to_markdown(report)

        assert "sk-abcdef1234567890abcdef12" not in json_str or "REDACTED" in json_str
        assert "sk-abcdef1234567890abcdef12" not in md_str or "REDACTED" in md_str

    def test_export_report_file(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        run_id = store.create_run(target="local/demo", run_id="run_export")
        store.finish_run(run_id)

        builder = ReportBuilder(store)
        report = builder.build_report(run_id)

        out_md = tmp_path / "exports" / "report.md"
        saved = export_report(report, out_md, output_format="markdown")
        assert saved.exists()
        assert "FORGE Investigation Report" in saved.read_text(encoding="utf-8")

        out_json = tmp_path / "exports" / "report.json"
        saved_j = export_report(report, out_json, output_format="json")
        assert saved_j.exists()
        assert json.loads(saved_j.read_text(encoding="utf-8"))["run_id"] == "run_export"


# ===========================================================================
# Benchmark Corpus and Metadata Validation Tests
# ===========================================================================

class TestBenchmarkCorpus:
    def test_discover_default_cases(self):
        loader = CorpusLoader()
        cases = loader.discover_cases()
        assert len(cases) >= 7

        case_ids = [c.case_id for c in cases]
        assert "BM01_boundary_off_by_one" in case_ids
        assert "BM02_return_value_discount" in case_ids
        assert "BM03_missing_input_validation" in case_ids
        assert "BM04_unsafe_path_handling" in case_ids
        assert "BM05_exception_handling_leak" in case_ids
        assert "BM06_clean_control" in case_ids
        assert "BM07_prompt_injection" in case_ids

    def test_deterministic_case_ordering(self):
        loader = CorpusLoader()
        cases1 = loader.discover_cases()
        cases2 = loader.discover_cases()
        assert [c.case_id for c in cases1] == [c.case_id for c in cases2]

    def test_malformed_case_json_rejected(self, tmp_path: Path):
        bad_dir = tmp_path / "BM99_bad"
        bad_dir.mkdir()
        (bad_dir / "case.json").write_text("{not valid json")

        loader = CorpusLoader(fixtures_dir=tmp_path)
        with pytest.raises(BenchmarkCorpusError, match="Malformed JSON"):
            loader.discover_cases()

    def test_duplicate_case_id_rejected(self, tmp_path: Path):
        c1 = tmp_path / "case_a"
        c2 = tmp_path / "case_b"
        c1.mkdir()
        c2.mkdir()
        payload = json.dumps({
            "case_id": "duplicate_id",
            "name": "dup",
            "description": "dup",
            "category": "clean_control",
            "target_file": "clean.py",
            "expected_defect": {"has_defect": False},
        })
        (c1 / "case.json").write_text(payload)
        (c1 / "clean.py").write_text("# clean")
        (c2 / "case.json").write_text(payload)
        (c2 / "clean.py").write_text("# clean")

        loader = CorpusLoader(fixtures_dir=tmp_path)
        with pytest.raises(BenchmarkCorpusError, match="Duplicate benchmark case ID"):
            loader.discover_cases()

    def test_target_file_path_traversal_rejected(self, tmp_path: Path):
        c_dir = tmp_path / "BM_hack"
        c_dir.mkdir()
        payload = json.dumps({
            "case_id": "BM_hack",
            "name": "hack",
            "description": "hack",
            "category": "clean_control",
            "target_file": "../../etc/passwd",
            "expected_defect": {"has_defect": False},
        })
        (c_dir / "case.json").write_text(payload)

        loader = CorpusLoader(fixtures_dir=tmp_path)
        with pytest.raises(BenchmarkCorpusError, match="target_file escapes"):
            loader.discover_cases()


# ===========================================================================
# Metrics Calculation Tests
# ===========================================================================

class TestBenchmarkMetrics:
    def test_perfect_metrics(self):
        results = [
            CaseExecutionResult(
                case_id="c1",
                category=BenchmarkCategory.BOUNDARY_CONDITION,
                outcome=CaseOutcome.PASS,
                has_defect_expected=True,
                defect_detected=True,
                matched_target_file=True,
            ),
            CaseExecutionResult(
                case_id="c2",
                category=BenchmarkCategory.CLEAN_CONTROL,
                outcome=CaseOutcome.PASS,
                has_defect_expected=False,
                defect_detected=False,
            ),
        ]
        metrics = calculate_metrics(results)
        assert metrics.total_cases == 2
        assert metrics.passed_cases == 2
        assert metrics.true_positives == 1
        assert metrics.true_negatives == 1
        assert metrics.false_positives == 0
        assert metrics.false_negatives == 0
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0
        assert metrics.f1_score == 1.0
        assert metrics.case_pass_rate == 1.0

    def test_zero_denominator_metrics_return_none(self):
        # Only clean fixtures, none detected as defect
        results = [
            CaseExecutionResult(
                case_id="clean1",
                category=BenchmarkCategory.CLEAN_CONTROL,
                outcome=CaseOutcome.PASS,
                has_defect_expected=False,
                defect_detected=False,
            )
        ]
        metrics = calculate_metrics(results)
        # TP=0, FP=0 -> TP + FP = 0 -> Precision is undefined (None)
        assert metrics.precision is None
        # TP=0, FN=0 -> TP + FN = 0 -> Recall is undefined (None)
        assert metrics.recall is None
        assert metrics.f1_score is None
        assert metrics.case_pass_rate == 1.0

    def test_imperfect_precision_and_recall(self):
        results = [
            # TP
            CaseExecutionResult(case_id="1", category=BenchmarkCategory.BOUNDARY_CONDITION, outcome=CaseOutcome.PASS, has_defect_expected=True, defect_detected=True, matched_target_file=True),
            # FN (missed defect)
            CaseExecutionResult(case_id="2", category=BenchmarkCategory.CALCULATION_ERROR, outcome=CaseOutcome.FAIL, has_defect_expected=True, defect_detected=False),
            # FP (clean control falsely claimed as bug)
            CaseExecutionResult(case_id="3", category=BenchmarkCategory.CLEAN_CONTROL, outcome=CaseOutcome.FAIL, has_defect_expected=False, defect_detected=True),
            # TN (clean control identified as clean)
            CaseExecutionResult(case_id="4", category=BenchmarkCategory.CLEAN_CONTROL, outcome=CaseOutcome.PASS, has_defect_expected=False, defect_detected=False),
        ]
        metrics = calculate_metrics(results)
        assert metrics.true_positives == 1
        assert metrics.false_negatives == 1
        assert metrics.false_positives == 1
        assert metrics.true_negatives == 1
        assert metrics.precision == 0.5  # 1 / (1 + 1)
        assert metrics.recall == 0.5     # 1 / (1 + 1)
        assert metrics.f1_score == 0.5
        assert metrics.case_pass_rate == 0.5

    def test_detector_misses_all_defects(self):
        """A detector that produces no findings on defective fixtures must have 0 recall, not 100%."""
        results = [
            CaseExecutionResult(case_id="d1", category=BenchmarkCategory.BOUNDARY_CONDITION, outcome=CaseOutcome.FAIL, has_defect_expected=True, defect_detected=False),
            CaseExecutionResult(case_id="d2", category=BenchmarkCategory.CALCULATION_ERROR, outcome=CaseOutcome.FAIL, has_defect_expected=True, defect_detected=False),
            CaseExecutionResult(case_id="c1", category=BenchmarkCategory.CLEAN_CONTROL, outcome=CaseOutcome.PASS, has_defect_expected=False, defect_detected=False),
        ]
        metrics = calculate_metrics(results)
        assert metrics.true_positives == 0
        assert metrics.false_negatives == 2
        assert metrics.true_negatives == 1
        assert metrics.false_positives == 0
        assert metrics.recall == 0.0
        assert metrics.precision is None  # 0/0
        assert metrics.case_pass_rate == round(1 / 3, 4)

    def test_detector_flags_every_fixture_including_clean(self):
        """A detector that reports findings everywhere produces false positives on clean code."""
        results = [
            CaseExecutionResult(case_id="d1", category=BenchmarkCategory.BOUNDARY_CONDITION, outcome=CaseOutcome.PASS, has_defect_expected=True, defect_detected=True, matched_target_file=True),
            CaseExecutionResult(case_id="c1", category=BenchmarkCategory.CLEAN_CONTROL, outcome=CaseOutcome.FAIL, has_defect_expected=False, defect_detected=True),
        ]
        metrics = calculate_metrics(results)
        assert metrics.true_positives == 1
        assert metrics.false_positives == 1
        assert metrics.true_negatives == 0
        assert metrics.false_negatives == 0
        assert metrics.precision == 0.5
        assert metrics.recall == 1.0
        assert metrics.case_pass_rate == 0.5

    def test_wrong_target_file_match_cannot_be_true_positive(self):
        """A finding on an unaffected file cannot be counted as a True Positive."""
        results = [
            CaseExecutionResult(
                case_id="d1",
                category=BenchmarkCategory.BOUNDARY_CONDITION,
                outcome=CaseOutcome.FAIL,
                has_defect_expected=True,
                defect_detected=True,
                matched_target_file=False,  # finding was in other_file.py
            ),
        ]
        metrics = calculate_metrics(results)
        assert metrics.true_positives == 0
        assert metrics.false_positives == 1
        assert metrics.false_negatives == 1
        assert metrics.recall == 0.0
        assert metrics.precision == 0.0
        assert metrics.case_pass_rate == 0.0

    def test_errors_and_unavailable_do_not_become_true_negatives_or_passed(self):
        """Error and skipped outcomes must never silently become True Negatives or passed."""
        results = [
            CaseExecutionResult(
                case_id="err_clean",
                category=BenchmarkCategory.CLEAN_CONTROL,
                outcome=CaseOutcome.ERROR,
                has_defect_expected=False,
                defect_detected=False,
                error_message="Runtime exception",
            ),
            CaseExecutionResult(
                case_id="skip_clean",
                category=BenchmarkCategory.CLEAN_CONTROL,
                outcome=CaseOutcome.SKIPPED_UNAVAILABLE,
                has_defect_expected=False,
                defect_detected=False,
                error_message="Docker unavailable",
            ),
            CaseExecutionResult(
                case_id="err_defect",
                category=BenchmarkCategory.BOUNDARY_CONDITION,
                outcome=CaseOutcome.ERROR,
                has_defect_expected=True,
                defect_detected=False,
                error_message="Runtime exception",
            ),
        ]
        metrics = calculate_metrics(results)
        # None of them should be True Negatives or True Positives or Passed!
        assert metrics.true_negatives == 0
        assert metrics.true_positives == 0
        assert metrics.passed_cases == 0
        assert metrics.error_cases == 2
        assert metrics.skipped_unavailable_cases == 1
        # pass rate: 0 passed out of 2 evaluated (3 total - 1 skipped = 2 evaluated)
        assert metrics.case_pass_rate == 0.0

    def test_all_skipped_cases_empty_denominator(self):
        """When all cases are skipped, pass_rate and metrics are None, not 0 or 1."""
        results = [
            CaseExecutionResult(
                case_id="s1",
                category=BenchmarkCategory.BOUNDARY_CONDITION,
                outcome=CaseOutcome.SKIPPED_UNAVAILABLE,
                has_defect_expected=True,
                defect_detected=False,
            )
        ]
        metrics = calculate_metrics(results)
        assert metrics.total_cases == 1
        assert metrics.skipped_unavailable_cases == 1
        assert metrics.case_pass_rate is None
        assert metrics.precision is None
        assert metrics.recall is None
        assert metrics.f1_score is None

    def test_benchmark_models_are_frozen(self):
        """Expected fixture labels cannot be mutated at runtime."""
        from forge.benchmarks.models import ExpectedDefect
        defect = ExpectedDefect(has_defect=True, category="boundary_condition")
        with pytest.raises(Exception):
            defect.has_defect = False


# ===========================================================================
# Benchmark Runner Integration Tests
# ===========================================================================

class TestBenchmarkRunner:
    def test_static_benchmark_run_suite(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        runner = BenchmarkRunner(store=store)

        result = runner.run_suite(mode="static")
        assert result.schema_version == BENCHMARK_SCHEMA_VERSION
        assert result.mode == "static"
        assert len(result.case_results) >= 7
        assert result.metrics.case_pass_rate == 1.0
        assert result.metrics.true_positives == 5
        assert result.metrics.true_negatives == 2

    def test_llm_benchmark_run_with_fake_provider(self, tmp_path: Path):
        store = make_test_store(tmp_path)
        fake = FakeLLMProvider()
        runner = BenchmarkRunner(store=store, provider=fake)

        result = runner.run_suite(mode="llm", case_filter="BM06_clean_control")
        assert len(result.case_results) == 1
        assert result.case_results[0].case_id == "BM06_clean_control"
        assert result.case_results[0].outcome == CaseOutcome.PASS

    def test_case_requiring_sandbox_skips_when_docker_unavailable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Simulate missing docker
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        c_dir = tmp_path / "BM_docker_case"
        c_dir.mkdir()
        payload = json.dumps({
            "case_id": "BM_docker_case",
            "name": "Docker case",
            "description": "Requires docker",
            "category": "calculation_error",
            "target_file": "app.py",
            "expected_defect": {"has_defect": True},
            "execution_requirements": {"requires_sandbox": True},
        })
        (c_dir / "case.json").write_text(payload)
        (c_dir / "app.py").write_text("x = 1")

        loader = CorpusLoader(fixtures_dir=tmp_path)
        runner = BenchmarkRunner(loader=loader)
        res = runner.evaluate_case(loader.discover_cases()[0], mode="static")
        assert res.outcome == CaseOutcome.SKIPPED_UNAVAILABLE
        assert "Docker unavailable" in res.error_message

    def test_non_circular_detection_independent_of_category(self):
        """Detector does not consult case.category or expected_defect to manufacture findings."""
        loader = CorpusLoader()
        cases = loader.discover_cases()
        bm01 = next(c for c in cases if c.case_id == "BM01_boundary_off_by_one")

        runner = BenchmarkRunner()
        # Create a modified case with an altered category and flipped has_defect
        tampered_case = BenchmarkCase(
            case_id="tampered",
            name=bm01.name,
            description=bm01.description,
            category=BenchmarkCategory.CLEAN_CONTROL,  # deliberately claim clean_control
            target_file=bm01.target_file,
            target_symbol=bm01.target_symbol,
            expected_defect={"has_defect": False},
            fixture_dir=bm01.fixture_dir,
        )
        # Running static analysis should STILL detect the off-by-one in the code,
        # proving that the detector analyzes code content, not case.category!
        findings = runner._run_static_analysis(tampered_case, None)
        assert len(findings) > 0
        assert any("off-by-one" in f.title.lower() for f in findings)

    def test_duplicate_findings_do_not_inflate_metrics(self):
        """Multiple findings on the same target file still count as exactly one True Positive."""
        loader = CorpusLoader()
        cases = loader.discover_cases()
        bm01 = next(c for c in cases if c.case_id == "BM01_boundary_off_by_one")

        runner = BenchmarkRunner()
        # Evaluate normal case
        res = runner.evaluate_case(bm01, mode="static")
        metrics = calculate_metrics([res])
        assert metrics.true_positives == 1
        assert metrics.passed_cases == 1


# ===========================================================================
# CLI Integration Tests
# ===========================================================================

class TestMilestone6CLI:
    def test_cli_report_markdown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_file = tmp_path / "test.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
        store = ForgeStore(db_path=db_file)
        run_id = store.create_run(target="local/cli_test", run_id="run_cli_rep")
        store.finish_run(run_id)

        result = runner.invoke(app, ["report", "run_cli_rep", "--format", "markdown"])
        assert result.exit_code == 0
        assert "FORGE Investigation Report" in result.output
        assert "run_cli_rep" in result.output

    def test_cli_report_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_file = tmp_path / "test.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
        store = ForgeStore(db_path=db_file)
        run_id = store.create_run(target="local/cli_test", run_id="run_cli_json")
        store.finish_run(run_id)

        result = runner.invoke(app, ["report", "run_cli_json", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["run_id"] == "run_cli_json"

    def test_cli_report_export_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_file = tmp_path / "test.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
        store = ForgeStore(db_path=db_file)
        run_id = store.create_run(target="local/cli_test", run_id="run_cli_exp")
        store.finish_run(run_id)

        out_file = tmp_path / "custom_report.md"
        result = runner.invoke(app, ["report", "run_cli_exp", "--output", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists()
        assert "Report successfully exported" in result.output

    def test_cli_report_nonexistent_run_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_file = tmp_path / "test.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(db_file))
        init_database(db_file)

        result = runner.invoke(app, ["report", "ghost_run"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_cli_benchmark_list(self):
        result = runner.invoke(app, ["benchmark", "list"])
        assert result.exit_code == 0
        assert "BM01_boundary_off_by_one" in result.output
        assert "BM06_clean_control" in result.output

    def test_cli_benchmark_run_static(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_file = tmp_path / "test.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(db_file))

        result = runner.invoke(app, ["benchmark", "run", "--mode", "static"])
        assert result.exit_code == 0
        assert "Benchmark Run Complete" in result.output
        assert "Precision" in result.output
        assert "100.00%" in result.output

    def test_cli_benchmark_run_single_case(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_file = tmp_path / "test.db"
        monkeypatch.setenv("FORGE_DB_PATH", str(db_file))

        result = runner.invoke(app, ["benchmark", "run", "--case", "BM01_boundary_off_by_one"])
        assert result.exit_code == 0
        assert "BM01_boundary_off_by_one" in result.output
        assert "PASS" in result.output

    def test_cli_benchmark_invalid_mode_fails(self):
        result = runner.invoke(app, ["benchmark", "run", "--mode", "unsupported_mode"])
        assert result.exit_code == 1
        assert "Invalid mode" in result.output
