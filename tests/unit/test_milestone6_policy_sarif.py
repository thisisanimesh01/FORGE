"""Comprehensive test suite for Phase 2 Milestone 6: CI Review Policies and SARIF Export."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.config import redact_secrets
from forge.review.evidence import (
    CalibratedEvidence,
    EvidenceCalibrationStatus,
    create_calibrated_evidence,
)
from forge.review.models import (
    FindingCategory,
    ReviewFileStatus,
    ReviewFinding,
    ReviewReport,
    REVIEW_SCHEMA_VERSION,
)
from forge.review.policy import (
    evaluate_review_policy,
    load_policy_from_file,
    PolicyConfigurationError,
)
from forge.review.policy_models import (
    FindingPolicyAction,
    FindingPolicyEvaluation,
    PolicyEvaluationReport,
    PolicyEvidenceHandling,
    ReviewPolicy,
    RuleSuppression,
)
from forge.review.reporting import export_review_report
from forge.review.sarif import (
    review_report_to_sarif,
    SARIF_SCHEMA_URI,
    SARIF_VERSION,
    SEVERITY_TO_SARIF_LEVEL,
)
from forge.schemas import FindingConfidence, FindingLifecycleState, FindingSeverity

runner = CliRunner()


def _make_dummy_finding(
    finding_id: str = "rev_test_001",
    rule_id: str = "REV_SEC_SHELL_EXECUTION",
    category: FindingCategory = FindingCategory.SECURITY,
    severity: FindingSeverity = FindingSeverity.HIGH,
    confidence: FindingConfidence = FindingConfidence.HIGH,
    file_path: str = "src/runner.py",
    line_number: int = 15,
    evidence_status: EvidenceCalibrationStatus = EvidenceCalibrationStatus.VERIFIED,
    with_evidence: bool = True,
    dataflow_path: dict = None,
) -> ReviewFinding:
    ev_list = []
    if with_evidence:
        ev = create_calibrated_evidence(
            evidence_type="static_analysis",
            description=f"Evidence for {rule_id}",
            content="subprocess.Popen(cmd, shell=True)",
            file_path=file_path,
            start_line=line_number,
            status=evidence_status,
        )
        ev_list.append(ev)

    return ReviewFinding(
        finding_id=finding_id,
        category=category,
        severity=severity,
        confidence=confidence,
        lifecycle_state=FindingLifecycleState.SUSPECTED,
        title=f"Test hazard: {rule_id}",
        explanation=f"Observed {rule_id} at {file_path}:{line_number}",
        observed_evidence=f"Code at {line_number} violates {rule_id}",
        inference="Arbitrary code execution risk",
        recommendation="Pass typed list with shell=False",
        file_path=file_path,
        line_number=line_number,
        rule_id=rule_id,
        code_snippet="subprocess.Popen(cmd, shell=True)",
        calibrated_evidence=ev_list,
        evidence_references=[e.evidence_id for e in ev_list],
        evidence_status=evidence_status,
        dataflow_path=dataflow_path,
    )


def _make_dummy_report(findings=None, files=None) -> ReviewReport:
    return ReviewReport(
        schema_version=REVIEW_SCHEMA_VERSION,
        review_id="rev_test_review",
        target="/tmp/test_repo",
        base_revision="HEAD~1",
        head_revision="HEAD",
        mode="static",
        created_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=0.05,
        diff_summary={"total_files_changed": 1, "total_additions": 10, "total_deletions": 0},
        summary_counts={"total_findings": len(findings or [])},
        findings=findings or [],
        files_reviewed=files or ["src/runner.py"],
    )


class TestReviewPolicyEvaluation:
    """Tests for deterministic CI review policy evaluation."""

    def test_default_policy_passes_clean_report(self):
        report = _make_dummy_report(findings=[])
        eval_report = evaluate_review_policy(report)
        assert eval_report.passed is True
        assert eval_report.blocking_findings_count == 0
        assert eval_report.total_findings_evaluated == 0

    def test_severity_threshold_blocks_high_or_critical(self):
        f_med = _make_dummy_finding(finding_id="f_med", severity=FindingSeverity.MEDIUM)
        f_high = _make_dummy_finding(finding_id="f_high", severity=FindingSeverity.HIGH)
        f_crit = _make_dummy_finding(finding_id="f_crit", severity=FindingSeverity.CRITICAL)

        report = _make_dummy_report(findings=[f_med, f_high, f_crit])
        policy = ReviewPolicy(min_fail_severity=FindingSeverity.HIGH)
        eval_report = evaluate_review_policy(report, policy)

        assert eval_report.passed is False
        assert eval_report.blocking_findings_count == 2
        actions = {d.finding_id: d.action for d in eval_report.decisions}
        assert actions["f_med"] == FindingPolicyAction.PASSED
        assert actions["f_high"] == FindingPolicyAction.BLOCKED
        assert actions["f_crit"] == FindingPolicyAction.BLOCKED

    def test_confidence_threshold_filtering(self):
        f_low_conf = _make_dummy_finding(finding_id="f1", severity=FindingSeverity.CRITICAL, confidence=FindingConfidence.LOW)
        f_high_conf = _make_dummy_finding(finding_id="f2", severity=FindingSeverity.CRITICAL, confidence=FindingConfidence.HIGH)

        report = _make_dummy_report(findings=[f_low_conf, f_high_conf])
        policy = ReviewPolicy(min_fail_severity=FindingSeverity.HIGH, min_enforce_confidence=FindingConfidence.HIGH)
        eval_report = evaluate_review_policy(report, policy)

        assert eval_report.passed is False
        assert eval_report.blocking_findings_count == 1
        actions = {d.finding_id: d.action for d in eval_report.decisions}
        assert actions["f1"] == FindingPolicyAction.PASSED
        assert actions["f2"] == FindingPolicyAction.BLOCKED

    def test_category_inclusion_and_exclusion(self):
        f_sec = _make_dummy_finding(finding_id="f_sec", category=FindingCategory.SECURITY)
        f_def = _make_dummy_finding(finding_id="f_def", category=FindingCategory.DEFECT)

        report = _make_dummy_report(findings=[f_sec, f_def])

        # Exclude security
        pol_ex = ReviewPolicy(excluded_categories=[FindingCategory.SECURITY])
        eval_ex = evaluate_review_policy(report, pol_ex)
        actions_ex = {d.finding_id: d.action for d in eval_ex.decisions}
        assert actions_ex["f_sec"] == FindingPolicyAction.SKIPPED
        assert actions_ex["f_def"] == FindingPolicyAction.BLOCKED

        # Include security only
        pol_inc = ReviewPolicy(included_categories=[FindingCategory.SECURITY])
        eval_inc = evaluate_review_policy(report, pol_inc)
        actions_inc = {d.finding_id: d.action for d in eval_inc.decisions}
        assert actions_inc["f_sec"] == FindingPolicyAction.BLOCKED
        assert actions_inc["f_def"] == FindingPolicyAction.SKIPPED

    def test_narrowly_scoped_suppression_with_reason(self):
        f_supp = _make_dummy_finding(finding_id="f_supp", rule_id="REV_SEC_SHELL_EXECUTION", file_path="src/job.py")
        f_other = _make_dummy_finding(finding_id="f_other", rule_id="REV_SEC_SHELL_EXECUTION", file_path="src/other.py")

        supp = RuleSuppression(
            rule_id="REV_SEC_SHELL_EXECUTION",
            file_path="src/job.py",
            reason="Verified isolated script in test sandbox",
            author="sec-team",
        )
        policy = ReviewPolicy(suppressions=[supp])
        report = _make_dummy_report(findings=[f_supp, f_other])
        eval_report = evaluate_review_policy(report, policy)

        assert eval_report.passed is False
        assert eval_report.suppressed_findings_count == 1
        assert eval_report.blocking_findings_count == 1

        actions = {d.finding_id: d.action for d in eval_report.decisions}
        assert actions["f_supp"] == FindingPolicyAction.SUPPRESSED
        assert actions["f_other"] == FindingPolicyAction.BLOCKED

    def test_expired_suppression_fails_closed(self):
        f = _make_dummy_finding(finding_id="f1", rule_id="REV_SEC_SHELL_EXECUTION", file_path="src/job.py")
        expired_supp = RuleSuppression(
            rule_id="REV_SEC_SHELL_EXECUTION",
            file_path="src/job.py",
            reason="Temporary suppression",
            expires_at="2020-01-01",  # Past date
        )
        assert expired_supp.is_expired is True

        policy = ReviewPolicy(suppressions=[expired_supp])
        report = _make_dummy_report(findings=[f])
        eval_report = evaluate_review_policy(report, policy)

        assert eval_report.passed is False
        assert eval_report.suppressed_findings_count == 0
        assert eval_report.blocking_findings_count == 1
        assert eval_report.decisions[0].action == FindingPolicyAction.BLOCKED

    def test_evidence_handling_modes(self):
        f_unverified = _make_dummy_finding(
            finding_id="f_unbacked",
            evidence_status=EvidenceCalibrationStatus.INSUFFICIENT,
            with_evidence=False,
        )
        report = _make_dummy_report(findings=[f_unverified])

        # Mode A: report_only (default)
        pol_report = ReviewPolicy(evidence_handling=PolicyEvidenceHandling.REPORT_ONLY)
        res_report = evaluate_review_policy(report, pol_report)
        assert res_report.unbacked_findings_count == 1
        assert res_report.passed is False

        # Mode B: ignore
        pol_ignore = ReviewPolicy(evidence_handling=PolicyEvidenceHandling.IGNORE)
        res_ignore = evaluate_review_policy(report, pol_ignore)
        assert res_ignore.unbacked_findings_count == 1
        assert res_ignore.passed is True
        assert res_ignore.decisions[0].action == FindingPolicyAction.SKIPPED

        # Mode C: fail_closed
        pol_fail_closed = ReviewPolicy(evidence_handling=PolicyEvidenceHandling.FAIL_CLOSED)
        res_fail_closed = evaluate_review_policy(report, pol_fail_closed)
        assert res_fail_closed.unbacked_findings_count == 1
        assert res_fail_closed.passed is False
        assert res_fail_closed.decisions[0].action == FindingPolicyAction.BLOCKED

    def test_load_policy_from_file_valid_and_invalid(self, tmp_path: Path):
        valid_file = tmp_path / "policy.json"
        valid_file.write_text(json.dumps({
            "policy_id": "test_pol",
            "min_fail_severity": "critical",
            "suppressions": [{"rule_id": "R1", "reason": "Justification for R1"}]
        }), encoding="utf-8")

        policy = load_policy_from_file(valid_file)
        assert policy.policy_id == "test_pol"
        assert policy.min_fail_severity == FindingSeverity.CRITICAL
        assert len(policy.suppressions) == 1

        # Invalid file
        invalid_file = tmp_path / "bad.json"
        invalid_file.write_text("invalid json", encoding="utf-8")
        with pytest.raises(PolicyConfigurationError):
            load_policy_from_file(invalid_file)


class TestSARIFExporter:
    """Tests for standards-compliant SARIF 2.1.0 exporter."""

    def test_sarif_basic_structure_and_schema(self):
        f1 = _make_dummy_finding(finding_id="f1", rule_id="REV_SEC_SHELL_EXECUTION", severity=FindingSeverity.CRITICAL)
        f2 = _make_dummy_finding(finding_id="f2", rule_id="REV_DEF_OFF_BY_ONE_BOUND", severity=FindingSeverity.MEDIUM)
        report = _make_dummy_report(findings=[f1, f2], files=["src/runner.py", "src/loop.py"])

        sarif_json_str = review_report_to_sarif(report)
        sarif = json.loads(sarif_json_str)

        assert sarif["version"] == SARIF_VERSION
        assert sarif["$schema"] == SARIF_SCHEMA_URI
        assert len(sarif["runs"]) == 1

        run = sarif["runs"][0]
        assert run["tool"]["driver"]["name"] == "FORGE Review"
        assert len(run["tool"]["driver"]["rules"]) == 2
        assert len(run["results"]) == 2

        # Check severity-to-level mapping
        r1 = next(r for r in run["results"] if r["ruleId"] == "REV_SEC_SHELL_EXECUTION")
        assert r1["level"] == "error"
        r2 = next(r for r in run["results"] if r["ruleId"] == "REV_DEF_OFF_BY_ONE_BOUND")
        assert r2["level"] == "warning"

    def test_sarif_locations_and_regions(self):
        f = _make_dummy_finding(file_path="app/core.py", line_number=42)
        report = _make_dummy_report(findings=[f])

        sarif = json.loads(review_report_to_sarif(report))
        res = sarif["runs"][0]["results"][0]

        assert len(res["locations"]) == 1
        loc = res["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "app/core.py"
        assert loc["region"]["startLine"] == 42
        assert loc["region"]["startColumn"] == 1

    def test_sarif_missing_location_handled_safely(self):
        f = _make_dummy_finding(file_path="<commit_message>", line_number=None)
        report = _make_dummy_report(findings=[f])

        sarif = json.loads(review_report_to_sarif(report))
        res = sarif["runs"][0]["results"][0]
        assert "locations" not in res  # Omitted because it's not a valid repository file

    def test_sarif_suppression_metadata_included(self):
        f = _make_dummy_finding(finding_id="find_supp_1", rule_id="REV_SEC_SHELL_EXECUTION")
        report = _make_dummy_report(findings=[f])

        supp = RuleSuppression(rule_id="REV_SEC_SHELL_EXECUTION", reason="Sandbox contained")
        policy = ReviewPolicy(suppressions=[supp])
        eval_report = evaluate_review_policy(report, policy)

        sarif = json.loads(review_report_to_sarif(report, policy_report=eval_report))
        res = sarif["runs"][0]["results"][0]
        assert "suppressions" in res
        assert res["suppressions"][0]["kind"] == "external"
        assert res["suppressions"][0]["justification"] == "Sandbox contained"

    def test_sarif_redaction_boundary(self):
        f = _make_dummy_finding(
            finding_id="f_sec",
            rule_id="REV_SEC_HARDCODED_SECRET",
            file_path="auth.py",
            line_number=5,
        )
        report = _make_dummy_report(findings=[f])
        sarif_str = review_report_to_sarif(report)
        assert "sk-" not in sarif_str
        assert "Bearer sk-" not in sarif_str

    def test_sarif_export_to_file(self, tmp_path: Path):
        report = _make_dummy_report(findings=[_make_dummy_finding()])
        out_file = tmp_path / "out.sarif"
        export_review_report(report, out_file, output_format="sarif")

        assert out_file.exists()
        parsed = json.loads(out_file.read_text(encoding="utf-8"))
        assert parsed["version"] == SARIF_VERSION


class TestPolicyCLIIntegration:
    """CLI integration tests for review --format sarif and policy check."""

    def test_cli_review_format_sarif(self, tmp_path: Path):
        repo_dir = tmp_path / "sarif_repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "file.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "c1"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "file.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "c2"], cwd=repo_dir, check=True, capture_output=True)

        result = runner.invoke(app, ["review", str(repo_dir), "--base", "HEAD~1", "--head", "HEAD", "--format", "sarif", "--no-llm"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["version"] == "2.1.0"
        assert parsed["runs"][0]["tool"]["driver"]["name"] == "FORGE Review"

    def test_cli_policy_check_pass_and_fail(self, tmp_path: Path):
        # Create a sample review report JSON
        clean_report = _make_dummy_report(findings=[])
        clean_file = tmp_path / "clean_report.json"
        clean_file.write_text(json.dumps(clean_report.model_dump(mode="json")), encoding="utf-8")

        # Test clean report passes policy check (exit code 0)
        res_pass = runner.invoke(app, ["policy", "check", str(clean_file)])
        assert res_pass.exit_code == 0
        assert "PASSED" in res_pass.output

        # Create a report with a blocking finding
        blocking_report = _make_dummy_report(findings=[_make_dummy_finding(severity=FindingSeverity.HIGH)])
        block_file = tmp_path / "block_report.json"
        block_file.write_text(json.dumps(blocking_report.model_dump(mode="json")), encoding="utf-8")

        # Test blocking report fails policy check (exit code 1)
        res_fail = runner.invoke(app, ["policy", "check", str(block_file)])
        assert res_fail.exit_code == 1
        assert "FAILED" in res_fail.output
        assert "[BLOCKED]" in res_fail.output

    def test_cli_policy_check_json_output(self, tmp_path: Path):
        clean_report = _make_dummy_report(findings=[])
        clean_file = tmp_path / "clean_report.json"
        clean_file.write_text(json.dumps(clean_report.model_dump(mode="json")), encoding="utf-8")

        res = runner.invoke(app, ["policy", "check", str(clean_file), "--format", "json"])
        assert res.exit_code == 0
        parsed = json.loads(res.output)
        assert parsed["passed"] is True
        assert parsed["blocking_findings_count"] == 0

    def test_cli_policy_check_invalid_input_exits_2(self):
        res = runner.invoke(app, ["policy", "check", "nonexistent_file_path.json"])
        assert res.exit_code == 2
