"""Tests for Phase 4 Milestone 5: End-to-End Repair Reliability & Adversarial Testing.

Validates:
1. End-to-End Acceptance Suite Execution (26 deterministic scenarios)
2. Metric computation with honest denominators (None for empty classes)
3. Markdown and JSON report rendering with secret redaction
4. Resilient workflow recovery (unhandled exception handling, crash transitions)
5. Defense-in-depth patch revalidation (budget checks, path traversal rejection)
6. CLI commands: `forge repair test` and `forge repair acceptance`
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.repair.acceptance import (
    AcceptanceCategory,
    AcceptanceExecutionMode,
    AcceptanceMetrics,
    AcceptanceReport,
    AcceptanceScenarioDefinition,
    AcceptanceScenarioResult,
    ScenarioStatus,
    compute_acceptance_metrics,
    render_acceptance_json,
    render_acceptance_markdown,
    run_acceptance_suite,
)
from forge.repair.contracts import PatchCandidate, RepairBudget
from forge.repair.orchestrator import RepairOrchestrator
from forge.repair.orchestrator_models import (
    ALLOWED_WORKFLOW_TRANSITIONS,
    RepairOutcome,
    RepairWorkflowRecord,
    RepairWorkflowState,
)
from forge.repair.planner import RepairPlanner
from forge.repair.verifier import RepairVerifier
from forge.schemas import EvidenceRecord, EvidenceType, Finding, FindingLifecycleState
from forge.storage.store import ForgeStore

runner = CliRunner()


# =============================================================================
# Acceptance Suite Unit Tests
# =============================================================================

def test_acceptance_suite_runs_all_scenarios(tmp_path: Path):
    """Full acceptance suite runs all 26 deterministic scenarios with 100% pass rate."""
    report = run_acceptance_suite()
    assert report.metrics.total_scenarios == 26
    assert report.metrics.passed_scenarios == 26
    assert report.metrics.failed_scenarios == 0
    assert report.metrics.skipped_scenarios == 0
    assert report.metrics.pass_rate == 1.0
    assert len(report.scenarios) == 26


def test_acceptance_suite_filter_single_scenario():
    """Scenario filter isolates a specific test scenario."""
    report = run_acceptance_suite(scenario_filter="ACC01")
    assert report.metrics.total_scenarios == 1
    assert report.metrics.passed_scenarios == 1
    assert report.scenarios[0].scenario_id == "ACC01_happy_path_e2e"
    assert report.scenarios[0].status == ScenarioStatus.PASSED


def test_acceptance_suite_filter_case_insensitive():
    """Scenario filter matches case-insensitively and on full scenario ID."""
    report = run_acceptance_suite(scenario_filter="acc15")
    assert report.metrics.total_scenarios == 1
    assert report.scenarios[0].scenario_id == "ACC15_sandbox_unavailable_fail_closed"
    assert report.scenarios[0].status == ScenarioStatus.PASSED


# =============================================================================
# Metric Calculation Tests
# =============================================================================

def test_compute_acceptance_metrics_honest_denominators():
    """Empty categories and execution modes report None pass_rate rather than 0 or DivisionByZero."""
    results = [
        AcceptanceScenarioResult(
            scenario_id="ACC01",
            name="Test Happy",
            category=AcceptanceCategory.HAPPY_PATH,
            execution_mode=AcceptanceExecutionMode.SYNTHETIC_E2E,
            status=ScenarioStatus.PASSED,
            passed=True,
            expected_state="awaiting_human_review",
            actual_state="awaiting_human_review",
            target_repo_immutable=True,
        ),
        AcceptanceScenarioResult(
            scenario_id="ACC02",
            name="Test Patch",
            category=AcceptanceCategory.PATCH_SAFETY,
            execution_mode=AcceptanceExecutionMode.UNIT_BOUNDARY,
            status=ScenarioStatus.FAILED,
            passed=False,
            expected_state="rejected",
            actual_state="rejected",
            target_repo_immutable=True,
        ),
    ]

    metrics = compute_acceptance_metrics(results)
    assert metrics.total_scenarios == 2
    assert metrics.passed_scenarios == 1
    assert metrics.failed_scenarios == 1
    assert metrics.pass_rate == 0.5

    # Happy path: 1/1 = 1.0
    assert metrics.by_category[AcceptanceCategory.HAPPY_PATH.value]["pass_rate"] == 1.0
    # Patch safety: 0/1 = 0.0
    assert metrics.by_category[AcceptanceCategory.PATCH_SAFETY.value]["pass_rate"] == 0.0
    # Empty category: None
    assert metrics.by_category[AcceptanceCategory.REVISION_DRIFT.value]["total"] == 0
    assert metrics.by_category[AcceptanceCategory.REVISION_DRIFT.value]["pass_rate"] is None

    # Empty execution mode: None
    assert metrics.by_mode[AcceptanceExecutionMode.REAL_SANDBOX.value]["total"] == 0
    assert metrics.by_mode[AcceptanceExecutionMode.REAL_SANDBOX.value]["pass_rate"] is None


# =============================================================================
# Report Rendering & Redaction Tests
# =============================================================================

def test_render_acceptance_markdown():
    """Markdown report includes report metadata, metric tables, scenario matrix, and disclaimers."""
    report = run_acceptance_suite(scenario_filter="ACC01")
    md = render_acceptance_markdown(report)

    assert "# FORGE — Phase 4 M5 End-to-End Repair Reliability Acceptance Report" in md
    assert "Synthetic Scope Disclaimer" in md
    assert "Executive Summary & Verification Metrics" in md
    assert "Breakdown by Execution Mode" in md
    assert "Breakdown by Category" in md
    assert "Scenario Acceptance Matrix" in md
    assert "Security & Safety Invariants Verified" in md
    assert "ACC01_happy_path_e2e" in md


def test_render_acceptance_json_and_redaction():
    """JSON report serializes accurately and redacts any accidentally embedded secrets."""
    results = [
        AcceptanceScenarioResult(
            scenario_id="ACC_LEAK",
            name="Secret Leak Test",
            category=AcceptanceCategory.ADVERSARIAL_SECURITY,
            execution_mode=AcceptanceExecutionMode.SYNTHETIC_E2E,
            status=ScenarioStatus.PASSED,
            passed=True,
            expected_state="awaiting_human_review",
            actual_state="awaiting_human_review",
            target_repo_immutable=True,
            error_message="Error with secret: sk-1234567890abcdef1234567890abcdef",
        )
    ]
    metrics = compute_acceptance_metrics(results)
    rep = AcceptanceReport(
        report_id="rep_test_secret",
        generated_at="2026-10-26T00:00:00Z",
        real_sandbox_available=False,
        metrics=metrics,
        scenarios=results,
        synthetic_scope_disclaimer="Synthetic testing scope disclaimer",
    )

    rendered_json = render_acceptance_json(rep)
    data = json.loads(rendered_json)
    assert data["report_id"] == "rep_test_secret"
    # Verify token was redacted
    assert "sk-1234567890abcdef1234567890abcdef" not in rendered_json
    assert "[REDACTED_SECRET]" in data["scenarios"][0]["error_message"]


# =============================================================================
# Resilient Orchestrator Recovery & Exception Hardening Tests
# =============================================================================

def test_orchestrator_handles_planner_crash(tmp_path: Path):
    """Unhandled exception in planner cleanly transitions workflow to FAILED."""
    db_file = tmp_path / "test.db"
    store = ForgeStore(db_path=db_file)
    run_id = store.create_run(target="/path/to/repo")

    finding = store.create_finding(
        run_id=run_id,
        title="Sample Finding",
        description="Sample",
        finding_id="find_crash_01",
    )
    ev = EvidenceRecord(
        evidence_id="evi_crash_01",
        run_id=run_id,
        finding_id=finding.finding_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Verified reproduction",
        content="Bug reproduced",
    )
    store.attach_evidence(ev)
    store.transition_finding(finding.finding_id, FindingLifecycleState.REPRODUCED, ev.evidence_id)

    mock_planner = MagicMock(spec=RepairPlanner)
    mock_planner.plan_repair.side_effect = RuntimeError("Fatal GPU memory allocation failure")

    orch = RepairOrchestrator(store=store, planner=mock_planner)
    wf = orch.start_workflow(
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        run_id=run_id,
    )
    assert wf.state == RepairWorkflowState.CREATED

    # Advance through evidence validation to planning
    wf = orch.advance(wf.workflow_id, auto_advance=False)
    assert wf.state == RepairWorkflowState.EVIDENCE_VALIDATED

    # Advance to generate_plan which raises RuntimeError
    res = orch.advance(wf.workflow_id, auto_advance=False)
    assert res.state == RepairWorkflowState.FAILED
    assert "fatal gpu" in (res.error_message or "").lower() or "exception" in (res.error_message or "").lower()


def test_orchestrator_handles_verifier_crash(tmp_path: Path):
    """Unhandled exception in verifier cleanly transitions workflow to FAILED with REPAIR_FAILED outcome."""
    from forge.repair.contracts import compute_patch_sha256
    db_file = tmp_path / "test.db"
    store = ForgeStore(db_path=db_file)
    run_id = store.create_run(target="/path/to/repo")

    finding = store.create_finding(
        run_id=run_id,
        title="Sample Finding",
        description="Sample",
        finding_id="find_crash_02",
    )
    ev = EvidenceRecord(
        evidence_id="evi_crash_02",
        run_id=run_id,
        finding_id=finding.finding_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Verified reproduction",
        content="Bug reproduced",
    )
    store.attach_evidence(ev)
    store.transition_finding(finding.finding_id, FindingLifecycleState.REPRODUCED, ev.evidence_id)

    diff_text = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    real_digest = compute_patch_sha256(diff_text)
    patch_cand = PatchCandidate(
        patch_id="pat_crash_02",
        contract_id="contract_02",
        finding_id=finding.finding_id,
        base_commit_sha="0" * 40,
        target_files=["file.py"],
        unified_diff=diff_text,
        sha256_digest=real_digest,
        added_lines=1,
        removed_lines=1,
        hunk_count=1,
    )

    from forge.repair.models import RepairPlan, RepairPlanStatus
    plan = RepairPlan(
        plan_id="rpl_crash_02",
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="file.py",
        expected_behavior="Works",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    store.save_repair_plan(plan)

    mock_sandbox = MagicMock()
    mock_sandbox.is_available.return_value = True

    mock_verifier = MagicMock(spec=RepairVerifier)
    mock_verifier.verify_plan.side_effect = RuntimeError("Container daemon socket disconnected")

    orch = RepairOrchestrator(
        store=store,
        sandbox=mock_sandbox,
        verifier=mock_verifier,
    )

    wf = RepairWorkflowRecord(
        workflow_id="rwf_crash_02",
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        state=RepairWorkflowState.VERIFICATION_PENDING,
        plan_id=plan.plan_id,
        patch_digest=real_digest,
    )
    store.save_repair_workflow(wf)

    # Patch git HEAD check to match base_commit_sha
    with patch("forge.repair.orchestrator._extract_git_head_commit", return_value="0" * 40):
        res = orch.advance(wf.workflow_id, auto_advance=False)

    assert res.state == RepairWorkflowState.FAILED
    assert res.outcome == RepairOutcome.REPAIR_FAILED
    assert "verification execution crashed" in (res.error_message or "").lower()


def test_orchestrator_defense_in_depth_budget_rejection(tmp_path: Path):
    """Orchestrator validate_patch stage catches oversized patch exceeding RepairBudget."""
    from forge.repair.contracts import compute_patch_sha256
    db_file = tmp_path / "test.db"
    store = ForgeStore(db_path=db_file)
    run_id = store.create_run(target="/path/to/repo")

    finding = store.create_finding(
        run_id=run_id,
        title="Sample Finding",
        description="Sample",
        finding_id="find_budget_01",
    )
    ev = EvidenceRecord(
        evidence_id="evi_budget_01",
        run_id=run_id,
        finding_id=finding.finding_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Verified reproduction",
        content="Bug reproduced",
    )
    store.attach_evidence(ev)
    store.transition_finding(finding.finding_id, FindingLifecycleState.REPRODUCED, ev.evidence_id)

    diff_text = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    real_digest = compute_patch_sha256(diff_text)

    # 150 lines changed exceeding default max 100
    patch_cand = PatchCandidate(
        patch_id="pat_budget_01",
        contract_id="contract_01",
        finding_id=finding.finding_id,
        base_commit_sha="0" * 40,
        target_files=["file.py"],
        unified_diff=diff_text,
        sha256_digest=real_digest,
        added_lines=150,
        removed_lines=0,
        hunk_count=1,
    )

    from forge.repair.models import RepairPlan, RepairPlanStatus
    plan = RepairPlan(
        plan_id="rpl_budget_01",
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="file.py",
        expected_behavior="Works",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    store.save_repair_plan(plan)

    budget = RepairBudget(max_lines_changed=100)
    orch = RepairOrchestrator(store=store, budget=budget)

    wf = RepairWorkflowRecord(
        workflow_id="rwf_budget_01",
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        state=RepairWorkflowState.PLAN_GENERATED,
        plan_id=plan.plan_id,
        patch_digest=real_digest,
    )
    store.save_repair_workflow(wf)

    res = orch.advance(wf.workflow_id, auto_advance=False)
    assert res.state == RepairWorkflowState.REJECTED
    assert "budget" in (res.error_message or "").lower()


def test_orchestrator_defense_in_depth_path_traversal_rejection(tmp_path: Path):
    """Orchestrator validate_patch stage catches path traversal attempts in target_files."""
    from forge.repair.contracts import compute_patch_sha256
    db_file = tmp_path / "test.db"
    store = ForgeStore(db_path=db_file)
    run_id = store.create_run(target="/path/to/repo")

    finding = store.create_finding(
        run_id=run_id,
        title="Sample Finding",
        description="Sample",
        finding_id="find_trav_01",
    )
    ev = EvidenceRecord(
        evidence_id="evi_trav_01",
        run_id=run_id,
        finding_id=finding.finding_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Verified reproduction",
        content="Bug reproduced",
    )
    store.attach_evidence(ev)
    store.transition_finding(finding.finding_id, FindingLifecycleState.REPRODUCED, ev.evidence_id)

    diff_text = "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-old\n+new\n"
    real_digest = compute_patch_sha256(diff_text)

    patch_cand = PatchCandidate(
        patch_id="pat_trav_01",
        contract_id="contract_01",
        finding_id=finding.finding_id,
        base_commit_sha="0" * 40,
        target_files=["../../etc/passwd"],
        unified_diff=diff_text,
        sha256_digest=real_digest,
        added_lines=1,
        removed_lines=1,
        hunk_count=1,
    )

    from forge.repair.models import RepairPlan, RepairPlanStatus
    plan = RepairPlan(
        plan_id="rpl_trav_01",
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        cited_evidence_ids=[ev.evidence_id],
        root_cause="Bug",
        proposed_change="Fix",
        target_file="../../etc/passwd",
        expected_behavior="Works",
        verification_plan="pytest",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=patch_cand,
    )
    store.save_repair_plan(plan)

    orch = RepairOrchestrator(store=store)

    wf = RepairWorkflowRecord(
        workflow_id="rwf_trav_01",
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        state=RepairWorkflowState.PLAN_GENERATED,
        plan_id=plan.plan_id,
        patch_digest=real_digest,
    )
    store.save_repair_workflow(wf)

    res = orch.advance(wf.workflow_id, auto_advance=False)
    assert res.state == RepairWorkflowState.REJECTED
    assert "path traversal" in (res.error_message or "").lower()


def test_orchestrator_state_machine_transition_invariants():
    """Verify that updated state machine transitions allow recovery and retry."""
    assert RepairWorkflowState.FAILED in ALLOWED_WORKFLOW_TRANSITIONS[RepairWorkflowState.EVIDENCE_VALIDATED]
    assert RepairWorkflowState.EVIDENCE_VALIDATED in ALLOWED_WORKFLOW_TRANSITIONS[RepairWorkflowState.FAILED]
    assert RepairWorkflowState.CREATED in ALLOWED_WORKFLOW_TRANSITIONS[RepairWorkflowState.BLOCKED]


# =============================================================================
# CLI Command Integration Tests
# =============================================================================

def test_cli_repair_test_default():
    """`forge repair test` runs all scenarios and outputs Markdown with exit code 0."""
    result = runner.invoke(app, ["repair", "test"])
    assert result.exit_code == 0
    assert "FORGE — Phase 4 M5 End-to-End Repair Reliability Acceptance Report" in result.output
    assert "100.0%" in result.output


def test_cli_repair_test_scenario_filter():
    """`forge repair test -s ACC01` runs single scenario."""
    result = runner.invoke(app, ["repair", "test", "-s", "ACC01"])
    assert result.exit_code == 0
    assert "ACC01_happy_path_e2e" in result.output


def test_cli_repair_test_json_format():
    """`forge repair test --format json` outputs valid JSON."""
    result = runner.invoke(app, ["repair", "test", "-s", "ACC01", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["metrics"]["passed_scenarios"] == 1
    assert data["scenarios"][0]["scenario_id"] == "ACC01_happy_path_e2e"


def test_cli_repair_acceptance_alias():
    """`forge repair acceptance` is an exact alias for `forge repair test`."""
    result = runner.invoke(app, ["repair", "acceptance", "-s", "ACC01"])
    assert result.exit_code == 0
    assert "ACC01_happy_path_e2e" in result.output


def test_cli_repair_test_invalid_format():
    """`forge repair test --format xml` rejects unsupported format with exit code 2."""
    result = runner.invoke(app, ["repair", "test", "--format", "xml"])
    assert result.exit_code == 2
    assert "invalid format" in result.output.lower()


def test_cli_repair_test_output_file(tmp_path: Path):
    """`forge repair test --output <path>` exports report to disk."""
    out_file = tmp_path / "report.md"
    result = runner.invoke(app, ["repair", "test", "-s", "ACC01", "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "ACC01_happy_path_e2e" in content


def test_orchestrator_refuses_planning_with_failed_reproduction_evidence(tmp_path: Path):
    """Orchestrator rejects planning if finding is suspected and attached repro evidence failed."""
    db_file = tmp_path / "test.db"
    store = ForgeStore(db_path=db_file)
    run_id = store.create_run(target="/path/to/repo")

    finding = store.create_finding(
        run_id=run_id,
        title="Sample Finding",
        description="Sample",
        finding_id="find_fail_repro",
    )
    # Finding remains in SUSPECTED state
    assert finding.lifecycle_state == FindingLifecycleState.SUSPECTED

    # Attach reproduction evidence with status="failed"
    ev = EvidenceRecord(
        evidence_id="evi_failed_repro",
        run_id=run_id,
        finding_id=finding.finding_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Failed reproduction attempt",
        content="Defect could not be reproduced",
        details={"status": "failed"},
    )
    store.attach_evidence(ev)

    orch = RepairOrchestrator(store=store)
    wf = orch.start_workflow(
        finding_id=finding.finding_id,
        target_repo=str(tmp_path),
        base_commit_sha="0" * 40,
        run_id=run_id,
    )
    res = orch.advance(wf.workflow_id, auto_advance=False)
    assert res.state == RepairWorkflowState.REJECTED
    assert "lacks verified reproduction evidence" in (res.error_message or "").lower()
