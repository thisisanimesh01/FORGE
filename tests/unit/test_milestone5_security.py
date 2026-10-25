"""Comprehensive test suite for Phase 2 Milestone 5: Security-Sensitive Dataflow & Secret-Exposure Detection."""

import ast
import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.config import redact_secrets
from forge.review.engine import ReviewEngine
from forge.review.models import DiffBundle, DiffFile, DiffHunk, DiffLine, FindingCategory
from forge.review.parser import parse_unified_diff
from forge.review.reporting import review_report_to_json, review_report_to_markdown
from forge.review.security_analyzer import (
    SecurityASTAnalyzer,
    RULE_HARDCODED_SECRET,
    RULE_SHELL_EXECUTION,
    RULE_PATH_TRAVERSAL,
    RULE_SECRET_EXPOSURE_LOG,
    RULE_SECRET_EXPOSURE_HTTP,
    RULE_FORMATTED_TOKEN,
    is_benign_placeholder,
    is_credential_like_value,
)
from forge.review.security_models import (
    SecurityHazardFinding,
    SecurityHazardType,
    SecuritySinkKind,
    SecuritySourceKind,
)
from forge.schemas import FindingConfidence, FindingSeverity

runner = CliRunner()


class TestSecurityASTAnalyzer:
    """Unit tests for AST-based security analysis and dataflow tracking."""

    def test_hardcoded_secret_assignment(self):
        source = """
def init_client():
    api_key = "sk-live1234567890abcdef123456"
    return api_key
"""
        analyzer = SecurityASTAnalyzer(file_path="app/auth.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == RULE_HARDCODED_SECRET
        assert f.line_number == 3
        assert f.file_path == "app/auth.py"
        assert f.severity == FindingSeverity.HIGH
        assert f.confidence == FindingConfidence.HIGH

    def test_nested_dict_and_header_credentials(self):
        source = """
def get_headers():
    headers = {"Authorization": "Bearer sk-proj999888777666555444333"}
    headers["X-API-Key"] = "ghp_111122223333444455556666777788889999"
    return headers
"""
        analyzer = SecurityASTAnalyzer(file_path="app/api.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 2
        rules = [f.rule_id for f in findings]
        assert all(r == RULE_HARDCODED_SECRET for r in rules)
        lines = [f.line_number for f in findings]
        assert 3 in lines
        assert 4 in lines

    def test_formatted_token_assembly(self):
        source = """
def assemble_auth(user_token):
    auth_header = f"Bearer {user_token}"
    return auth_header
"""
        analyzer = SecurityASTAnalyzer(file_path="app/auth.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == RULE_FORMATTED_TOKEN
        assert f.hazard_type == SecurityHazardType.TOKEN_FORMATTING
        assert f.line_number == 3

    def test_source_to_logging_sink_dataflow(self):
        source = """
def authenticate_user():
    token = "sk-auth999888777666555444333222"
    logger.info("Retrieved user token: %s", token)
"""
        analyzer = SecurityASTAnalyzer(file_path="app/auth.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 2
        types = [f.hazard_type for f in findings]
        assert SecurityHazardType.HARDCODED_SECRET in types
        assert SecurityHazardType.SECRET_EXPOSURE_LOG in types

        log_finding = next(f for f in findings if f.hazard_type == SecurityHazardType.SECRET_EXPOSURE_LOG)
        assert log_finding.dataflow_path is not None
        df = log_finding.dataflow_path
        assert df.source_kind == SecuritySourceKind.LITERAL_SECRET
        assert df.source_name == "token"
        assert df.source_line == 3
        assert df.sink_kind == SecuritySinkKind.LOGGING
        assert df.sink_line == 4

    def test_source_to_print_sink_dataflow(self):
        source = """
def debug_credentials():
    secret_key = "sk-debug1234567890abcdef1234"
    print(f"Debug secret: {secret_key}")
"""
        analyzer = SecurityASTAnalyzer(file_path="app/debug.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 2
        log_finding = next(f for f in findings if f.hazard_type == SecurityHazardType.SECRET_EXPOSURE_LOG)
        assert log_finding.dataflow_path is not None
        assert log_finding.dataflow_path.sink_kind == SecuritySinkKind.PRINT_STATEMENT

    def test_unsafe_shell_execution(self):
        source = """
import subprocess

def run_job(user_script):
    subprocess.Popen(f"bash {user_script}", shell=True)
"""
        analyzer = SecurityASTAnalyzer(file_path="app/job.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_SHELL_EXECUTION
        assert findings[0].severity == FindingSeverity.CRITICAL

    def test_uncontained_path_traversal(self):
        source = """
import os

def read_user_file(filename):
    base = "/srv/data"
    path = os.path.join(base, filename)
    return open(path).read()
"""
        analyzer = SecurityASTAnalyzer(file_path="app/storage.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_PATH_TRAVERSAL
        assert findings[0].line_number == 6

    def test_benign_placeholders_not_flagged(self):
        source = """
def sample_config():
    api_key = "<your_api_key_here>"
    token = "placeholder"
    dummy_auth = "test_token"
    password = "changeme"
    return True
"""
        analyzer = SecurityASTAnalyzer(file_path="app/config.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 0

    def test_benign_arithmetic_not_flagged(self):
        source = """
def calculate_metrics(actual, baseline):
    variance = actual - baseline
    diff = variance / baseline if baseline else 0
    return diff
"""
        analyzer = SecurityASTAnalyzer(file_path="app/metrics.py", source_code=source)
        findings = analyzer.analyze()
        assert len(findings) == 0

    def test_syntax_error_resilience(self):
        source = "def broken_func(:\n    invalid syntax !@@#"
        analyzer = SecurityASTAnalyzer(file_path="app/broken.py", source_code=source)
        findings = analyzer.analyze()
        assert findings == []

    def test_oversized_file_safely_skipped(self):
        source = "x = 1\n" * 1000
        analyzer = SecurityASTAnalyzer(file_path="app/large.py", source_code=source, max_file_chars=500)
        findings = analyzer.analyze()
        assert findings == []


class TestSecretRedactionBoundaries:
    """Verification of secret redaction across all reporting and persistence boundaries."""

    def test_redact_secrets_nested_bearer_and_tokens(self):
        raw = "headers['Authorization'] = 'Bearer sk-abcdef1234567890abcdef123456'"
        sanitized = redact_secrets(raw)
        assert "sk-" not in sanitized
        assert "[REDACTED_SECRET]" in sanitized

    def test_redact_secrets_in_json_and_markdown_reports(self):
        diff_text = """diff --git a/client.py b/client.py
--- a/client.py
+++ b/client.py
@@ -1,3 +1,4 @@
 def get_client():
+    api_key = "sk-live1234567890abcdef123456"
     return api_key
"""
        bundle = parse_unified_diff(diff_text, "HEAD~1", "HEAD")
        engine = ReviewEngine()
        report = engine.review_diff(bundle, target_path=Path("."), mode="static")

        # Test JSON serialization
        json_out = review_report_to_json(report)
        assert "sk-" not in json_out
        assert "[REDACTED_SECRET]" in json_out

        # Test Markdown serialization
        md_out = review_report_to_markdown(report)
        assert "sk-" not in md_out
        assert "[REDACTED_SECRET]" in md_out

    def test_redact_secrets_in_calibrated_evidence(self):
        from forge.review.evidence import create_calibrated_evidence
        ev = create_calibrated_evidence(
            evidence_type="static_analysis",
            description="Found sk-999888777666555444333 in file",
            content="api_key = 'sk-999888777666555444333'",
            file_path="auth.py",
            start_line=5,
        )
        assert "sk-" not in ev.description
        assert "sk-" not in ev.content_excerpt
        assert "[REDACTED_SECRET]" in ev.content_excerpt


class TestSecurityDataflowBenchmarkSuite:
    """Verification of the dedicated security dataflow benchmark corpus."""

    def test_security_corpus_discovery(self):
        from forge.benchmarks.review_corpus import ReviewCorpusLoader, DEFAULT_SECURITY_FIXTURES_DIR
        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_SECURITY_FIXTURES_DIR)
        fixtures = loader.discover_fixtures()
        assert len(fixtures) == 8
        ids = [f.fixture_id for f in fixtures]
        assert "SEC01_nested_dict_credential" in ids
        assert "SEC03_sensitive_data_logging" in ids
        assert "SEC04_harmless_placeholders" in ids
        assert "SEC07_command_shell_hazard" in ids

    def test_security_corpus_execution_metrics(self):
        from forge.benchmarks.review_corpus import ReviewCorpusLoader, DEFAULT_SECURITY_FIXTURES_DIR
        from forge.benchmarks.review_runner import ReviewBenchmarkRunner

        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_SECURITY_FIXTURES_DIR)
        runner_inst = ReviewBenchmarkRunner(loader=loader)
        result = runner_inst.run_suite(mode="static")

        assert result.metrics.total_cases == 8
        assert result.metrics.passed_cases == 8
        assert result.metrics.precision == 1.0
        assert result.metrics.recall == 1.0
        assert result.metrics.location_accuracy == 1.0
        assert result.metrics.evidence_validity_rate == 1.0
        assert result.metrics.specificity == 1.0

    def test_cli_benchmark_review_security_corpus(self):
        result = runner.invoke(app, ["benchmark", "review", "--corpus", "security", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["metrics"]["total_cases"] == 8
        assert parsed["metrics"]["passed_cases"] == 8
        assert parsed["metrics"]["precision"] == 1.0
        assert parsed["metrics"]["recall"] == 1.0
