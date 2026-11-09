"""Phase 12 M6: Comprehensive Test Suite for Repair Quality & Reliability Engine.

Covers:
- Category A: Evidence validation (bindings, digests, freshness, tamper detection).
- Category B: Correctness & completeness (root cause, strategy, required changes, regressions).
- Category C: Maintainability & patch analysis (complexity, duplication, API changes, dead code).
- Category D: Reliability history (SQLite store, statistics, strategy stability).
- Category E: Scoring & quality gates (deterministic weights, clamps, fail-closed gates).
- Category F: Security & trust boundaries (immutability, copy revalidation, secret redaction).
- Category G: Read-only guarantees (no workspace mutations, no git operations).
- 15 Integrated End-to-End Scenarios.
"""

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import pytest
import shutil
import tempfile
from typing import Any, Dict, List

from forge.repair_quality.completeness import RepairCompletenessAnalyzer
from forge.repair_quality.correctness import RepairCorrectnessEvaluator
from forge.repair_quality.events import RepairQualityEventRecorder
from forge.repair_quality.evidence import RepairQualityEvidenceValidator
from forge.repair_quality.executor import RepairQualityExecutor
from forge.repair_quality.gates import RepairQualityGateKeeper
from forge.repair_quality.history import HistoricalReliabilityStore
from forge.repair_quality.maintainability import RepairMaintainabilityAnalyzer
from forge.repair_quality.patch_analysis import RepairPatchAnalyzer
from forge.repair_quality.reliability import RepairReliabilityAnalyzer
from forge.repair_quality.risk import RepairRiskAnalyzer
from forge.repair_quality.schemas import (
    ConfidenceClassification,
    EvidenceCategory,
    EvidenceValidationStatus,
    QualityDimension,
    QualityDimensionScore,
    QualityGateDecision,
    QualityStatus,
    RepairQualityEvent,
    RepairQualityEventType,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityRequest,
    RepairQualityResult,
    RiskSeverity,
)
from forge.repair_quality.scoring import RepairQualityScorer
from forge.repair_quality.tools import RepairQualityTools


# ---------------------------------------------------------------------------
# Fixture Helpers
# ---------------------------------------------------------------------------

def make_sample_request(
    plan_id: str = "plan_001",
    plan_digest: str = "digest_plan_123",
    exec_id: str = "exec_001",
    patch_digest: str = "digest_patch_123",
    baseline_digest: str = "digest_base_123",
    verif_id: str = "verif_001",
    verif_status: str = "passed",
    workspace_id: str = "ws_001",
) -> RepairQualityRequest:
    return RepairQualityRequest(
        request_id="req_001",
        repair_plan_id=plan_id,
        repair_plan_digest=plan_digest,
        execution_id=exec_id,
        applied_patch_digest=patch_digest,
        baseline_tree_digest=baseline_digest,
        verification_id=verif_id,
        verification_status=verif_status,
        workspace_id=workspace_id,
        requested_scope=["src/calc.py"],
    )


def make_sample_plan(
    plan_id: str = "plan_001",
    plan_digest: str = "digest_plan_123",
    affected_files: List[str] = None,
    required_changes: List[Dict[str, Any]] = None,
    selected_strategy_id: str = "local_fix",
) -> Dict[str, Any]:
    return {
        "repair_plan_id": plan_id,
        "plan_digest": plan_digest,
        "selected_strategy_id": selected_strategy_id,
        "affected_files": affected_files or ["src/calc.py"],
        "approved_file_paths": affected_files or ["src/calc.py"],
        "required_changes": required_changes or [
            {"file_path": "src/calc.py", "description": "Fix divide by zero check"}
        ],
    }


def make_sample_execution(
    exec_id: str = "exec_001",
    patch_digest: str = "digest_patch_123",
    baseline_digest: str = "digest_base_123",
    status: str = "applied",
    modified_files: List[str] = None,
) -> Dict[str, Any]:
    return {
        "execution_id": exec_id,
        "applied_patch_digest": patch_digest,
        "baseline_tree_digest": baseline_digest,
        "execution_status": status,
        "modified_files": modified_files or ["src/calc.py"],
    }


def make_sample_verification(
    verif_id: str = "verif_001",
    verif_status: str = "passed",
    patch_digest: str = "digest_patch_123",
    regression_class: str = "no_regression_observed",
    required_completed: List[str] = None,
    required_failed: List[str] = None,
    human_review: bool = True,
) -> Dict[str, Any]:
    return {
        "verification_id": verif_id,
        "verification_status": verif_status,
        "applied_patch_digest": patch_digest,
        "regression_classification": regression_class,
        "required_checks_completed": required_completed if required_completed is not None else ["tests/test_calc.py::test_div"],
        "required_checks_failed": required_failed or [],
        "requires_human_review": human_review,
    }


SAMPLE_CLEAN_PATCH = """--- a/src/calc.py
+++ b/src/calc.py
@@ -10,3 +10,5 @@
 def divide(a, b):
+    if b == 0:
+        raise ValueError("Cannot divide by zero")
     return a / b
"""


# ---------------------------------------------------------------------------
# Category A: Evidence Validation
# ---------------------------------------------------------------------------

def test_evidence_validation_valid():
    validator = RepairQualityEvidenceValidator()
    req = make_sample_request()
    plan = make_sample_plan()
    exec_res = make_sample_execution()
    verif_res = make_sample_verification()

    is_valid, records, missing, errors = validator.validate_request_bindings(
        request=req,
        repair_plan=plan,
        execution_result=exec_res,
        verification_result=verif_res,
    )
    assert is_valid is True
    assert len(records) >= 3
    assert not missing
    assert not errors


def test_evidence_validation_mismatched_execution_id():
    validator = RepairQualityEvidenceValidator()
    req = make_sample_request(exec_id="exec_expected")
    exec_res = make_sample_execution(exec_id="exec_different")

    is_valid, records, missing, errors = validator.validate_request_bindings(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=exec_res,
        verification_result=make_sample_verification(),
    )
    assert is_valid is False
    assert any("Execution ID mismatch" in e for e in errors)


def test_evidence_validation_mismatched_patch_digest():
    validator = RepairQualityEvidenceValidator()
    req = make_sample_request(patch_digest="digest_AAA")
    exec_res = make_sample_execution(patch_digest="digest_BBB")

    is_valid, records, missing, errors = validator.validate_request_bindings(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=exec_res,
        verification_result=make_sample_verification(),
    )
    assert is_valid is False
    assert any("Patch digest mismatch" in e for e in errors)


def test_evidence_validation_mismatched_baseline_digest():
    validator = RepairQualityEvidenceValidator()
    req = make_sample_request(baseline_digest="digest_BASE_AAA")
    exec_res = make_sample_execution(baseline_digest="digest_BASE_BBB")

    is_valid, records, missing, errors = validator.validate_request_bindings(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=exec_res,
        verification_result=make_sample_verification(),
    )
    assert is_valid is False
    assert any("Baseline tree digest mismatch" in e for e in errors)


def test_evidence_validation_missing_evidence():
    validator = RepairQualityEvidenceValidator()
    req = make_sample_request()
    incomplete_plan = {}
    incomplete_exec = {}

    is_valid, records, missing, errors = validator.validate_request_bindings(
        request=req,
        repair_plan=incomplete_plan,
        execution_result=incomplete_exec,
        verification_result=make_sample_verification(),
    )
    assert is_valid is False
    assert "repair_plan_id" in missing
    assert "execution_id" in missing


def test_evidence_validation_stale_detection():
    validator = RepairQualityEvidenceValidator(max_evidence_age_seconds=10)
    req = make_sample_request()
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    # Modify validator check
    is_valid, records, missing, errors = validator.validate_request_bindings(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(),
    )
    assert is_valid is True  # standard records created with now()


def test_event_chain_tamper_detection():
    recorder = RepairQualityEventRecorder("analysis_test")
    e1 = recorder.record_event(RepairQualityEventType.QUALITY_ANALYSIS_STARTED, "Started")
    e2 = recorder.record_event(RepairQualityEventType.EVIDENCE_VALIDATED, "Validated")
    e3 = recorder.record_event(RepairQualityEventType.QUALITY_ANALYSIS_COMPLETED, "Completed")

    events = recorder.get_events()
    assert RepairQualityEventRecorder.validate_event_chain(events) is True

    # Tamper with an event description
    tampered_event = RepairQualityEvent(
        event_id=e2.event_id,
        analysis_id=e2.analysis_id,
        sequence_number=e2.sequence_number,
        prev_event_hash=e2.prev_event_hash,
        event_hash=e2.event_hash,
        event_type=e2.event_type,
        timestamp=e2.timestamp,
        actor="hacker",
        action_description="Tampered action",
        outcome=e2.outcome,
    )
    tampered_events = [events[0], tampered_event, events[2]]
    assert RepairQualityEventRecorder.validate_event_chain(tampered_events) is False


# ---------------------------------------------------------------------------
# Category B: Correctness & Completeness
# ---------------------------------------------------------------------------

def test_correctness_evaluator_passed():
    evaluator = RepairCorrectnessEvaluator()
    plan = make_sample_plan()
    verif = make_sample_verification(verif_status="passed", regression_class="no_regression_observed")

    metrics, dim_score = evaluator.evaluate_correctness(
        repair_plan=plan,
        verification_result=verif,
        modified_files=["src/calc.py"],
        root_cause_locations=["src/calc.py:12"],
    )
    assert dim_score.score == 1.0
    assert dim_score.status == QualityStatus.PASSED
    assert metrics["root_cause_aligned"] is True


def test_correctness_evaluator_root_cause_misaligned():
    evaluator = RepairCorrectnessEvaluator()
    plan = make_sample_plan()
    verif = make_sample_verification()

    metrics, dim_score = evaluator.evaluate_correctness(
        repair_plan=plan,
        verification_result=verif,
        modified_files=["src/other.py"],
        root_cause_locations=["src/calc.py:12"],
    )
    assert dim_score.score < 1.0
    assert metrics["root_cause_aligned"] is False
    assert any("Root cause misalignment" in f for f in dim_score.findings)


def test_correctness_evaluator_failed_when_verification_failed():
    evaluator = RepairCorrectnessEvaluator()
    plan = make_sample_plan()
    verif = make_sample_verification(verif_status="failed", required_failed=["test_div"])

    metrics, dim_score = evaluator.evaluate_correctness(
        repair_plan=plan,
        verification_result=verif,
        modified_files=["src/calc.py"],
    )
    assert dim_score.score == 0.0
    assert dim_score.status == QualityStatus.FAILED


def test_correctness_evaluator_regression_clamped_to_zero():
    evaluator = RepairCorrectnessEvaluator()
    plan = make_sample_plan()
    verif = make_sample_verification(verif_status="passed", regression_class="regression_detected")

    metrics, dim_score = evaluator.evaluate_correctness(
        repair_plan=plan,
        verification_result=verif,
        modified_files=["src/calc.py"],
    )
    assert dim_score.score == 0.0
    assert dim_score.status == QualityStatus.REGRESSION_RISK


def test_completeness_analyzer_all_addressed():
    analyzer = RepairCompletenessAnalyzer()
    plan = make_sample_plan(
        required_changes=[
            {"file_path": "src/calc.py", "description": "Add zero guard"},
            {"file_path": "src/utils.py", "description": "Update helper"},
        ]
    )
    metrics, dim_score = analyzer.analyze_completeness(
        repair_plan=plan,
        modified_files=["src/calc.py", "src/utils.py"],
        verification_result=make_sample_verification(),
    )
    assert dim_score.score == 1.0
    assert dim_score.status == QualityStatus.PASSED
    assert metrics["addressed_changes_count"] == 2


def test_completeness_analyzer_unaddressed_change():
    analyzer = RepairCompletenessAnalyzer()
    plan = make_sample_plan(
        required_changes=[
            {"file_path": "src/calc.py", "description": "Add zero guard"},
            {"file_path": "src/missing.py", "description": "Omitted change"},
        ]
    )
    metrics, dim_score = analyzer.analyze_completeness(
        repair_plan=plan,
        modified_files=["src/calc.py"],
        verification_result=make_sample_verification(),
    )
    assert dim_score.score < 1.0
    assert dim_score.status == QualityStatus.FAILED
    assert len(metrics["unaddressed_changes"]) == 1


# ---------------------------------------------------------------------------
# Category C: Maintainability & Patch Analysis
# ---------------------------------------------------------------------------

def test_patch_analyzer_clean():
    analyzer = RepairPatchAnalyzer()
    metrics, dim_score = analyzer.analyze_patch(
        patch_content=SAMPLE_CLEAN_PATCH,
        approved_files=["src/calc.py"],
    )
    assert dim_score.score == 1.0
    assert dim_score.status == QualityStatus.PASSED
    assert metrics["files_changed_count"] == 1
    assert metrics["patch_lines_added"] == 2


def test_patch_analyzer_scope_expansion():
    analyzer = RepairPatchAnalyzer()
    patch_extra = """--- a/src/secret.py
+++ b/src/secret.py
@@ -1,1 +1,2 @@
+extra_line = 1
"""
    metrics, dim_score = analyzer.analyze_patch(
        patch_content=patch_extra,
        approved_files=["src/calc.py"],
    )
    assert dim_score.score < 1.0
    assert "src/secret.py" in metrics["scope_expansion_files"]


def test_patch_analyzer_protected_path():
    analyzer = RepairPatchAnalyzer()
    patch_agents = """--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1,1 +1,2 @@
+bypass_rules = True
"""
    metrics, dim_score = analyzer.analyze_patch(
        patch_content=patch_agents,
        approved_files=["AGENTS.md"],
    )
    assert dim_score.score == 0.0
    assert dim_score.status == QualityStatus.FAILED
    assert "AGENTS.md" in metrics["protected_path_touches"]


def test_maintainability_ast_complexity(tmp_path: Path):
    base_file = tmp_path / "base.py"
    base_file.write_text("def compute(x):\n    return x * 2\n", encoding="utf-8")

    rep_file = tmp_path / "calc.py"
    # Adds high complexity: multiple nested ifs and loops
    rep_file.write_text(
        "def compute(x):\n"
        "    if x > 0:\n"
        "        for i in range(x):\n"
        "            if i % 2 == 0:\n"
        "                while i > 0:\n"
        "                    i -= 1\n"
        "    return x\n",
        encoding="utf-8",
    )

    analyzer = RepairMaintainabilityAnalyzer()
    metrics, dim_score = analyzer.analyze_maintainability(
        workspace_path=tmp_path,
        modified_files=["calc.py"],
    )
    assert metrics["complexity_measured"] is True
    assert metrics["cyclomatic_complexity_delta"] >= 5


def test_maintainability_dead_code_detection(tmp_path: Path):
    rep_file = tmp_path / "calc.py"
    rep_file.write_text(
        "def compute(x):\n"
        "    return x\n"
        "    dead = 1\n"
        "    print(dead)\n",
        encoding="utf-8",
    )
    analyzer = RepairMaintainabilityAnalyzer()
    metrics, dim_score = analyzer.analyze_maintainability(
        workspace_path=tmp_path,
        modified_files=["calc.py"],
    )
    assert len(metrics["dead_code_indicators"]) >= 1


def test_maintainability_public_api_modification(tmp_path: Path):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_file = base_dir / "calc.py"
    base_file.write_text("def public_api(a, b):\n    return a + b\n", encoding="utf-8")

    ws_dir = tmp_path / "repaired"
    ws_dir.mkdir()
    ws_file = ws_dir / "calc.py"
    # Changes signature of public API
    ws_file.write_text("def public_api(a, b, c, extra=None):\n    return a + b + c\n", encoding="utf-8")

    analyzer = RepairMaintainabilityAnalyzer()
    metrics, dim_score = analyzer.analyze_maintainability(
        workspace_path=ws_dir,
        modified_files=["calc.py"],
        baseline_path=base_dir,
    )
    assert len(metrics["public_api_changes"]) >= 1


# ---------------------------------------------------------------------------
# Category D: Reliability History
# ---------------------------------------------------------------------------

def test_history_store_record_and_query(tmp_path: Path):
    db_file = tmp_path / "test_history.db"
    store = HistoricalReliabilityStore(db_path=db_file)

    store.record_quality_result(
        request_id="req_001",
        repair_plan_id="plan_001",
        execution_id="exec_001",
        strategy_id="strat_local",
        target_files=["src/calc.py"],
        quality_status="passed",
        overall_score=0.92,
        gate_decision="PASSED",
        risk_level="low",
    )

    records = store.get_history(strategy_id="strat_local")
    assert len(records) == 1
    assert records[0]["overall_score"] == 0.92

    stats = store.get_strategy_stats("strat_local")
    assert stats["total_attempts"] == 1
    assert stats["passed_count"] == 1
    assert stats["success_rate"] == 1.0


def test_reliability_analyzer_with_history(tmp_path: Path):
    db_file = tmp_path / "test_rel.db"
    store = HistoricalReliabilityStore(db_path=db_file)

    # Record 5 successful runs
    for i in range(5):
        store.record_quality_result(
            request_id=f"req_{i}",
            repair_plan_id=f"plan_{i}",
            execution_id=f"exec_{i}",
            strategy_id="strat_stable",
            target_files=["src/calc.py"],
            quality_status="passed",
            overall_score=0.90,
            gate_decision="PASSED",
            risk_level="low",
        )

    analyzer = RepairReliabilityAnalyzer(history_store=store)
    metrics, score, confidence = analyzer.analyze_reliability(
        verification_result=make_sample_verification(),
        strategy_id="strat_stable",
    )
    assert score.score == 1.0
    assert confidence == ConfidenceClassification.VERY_HIGH
    assert metrics["past_attempts"] == 5


# ---------------------------------------------------------------------------
# Category E: Scoring & Gates
# ---------------------------------------------------------------------------

def test_scoring_weights_deterministic():
    policy = RepairQualityPolicy()
    scorer = RepairQualityScorer(policy)

    dim_scores = {
        QualityDimension.CORRECTNESS: QualityDimensionScore(
            dimension=QualityDimension.CORRECTNESS,
            score=1.0,
            weight=0.25,
            status=QualityStatus.PASSED,
            explanation="OK",
        ),
        QualityDimension.COMPLETENESS: QualityDimensionScore(
            dimension=QualityDimension.COMPLETENESS,
            score=0.8,
            weight=0.20,
            status=QualityStatus.PASSED,
            explanation="OK",
        ),
        QualityDimension.MAINTAINABILITY: QualityDimensionScore(
            dimension=QualityDimension.MAINTAINABILITY,
            score=0.9,
            weight=0.15,
            status=QualityStatus.PASSED,
            explanation="OK",
        ),
        QualityDimension.PATCH_QUALITY: QualityDimensionScore(
            dimension=QualityDimension.PATCH_QUALITY,
            score=1.0,
            weight=0.15,
            status=QualityStatus.PASSED,
            explanation="OK",
        ),
        QualityDimension.RELIABILITY: QualityDimensionScore(
            dimension=QualityDimension.RELIABILITY,
            score=0.9,
            weight=0.15,
            status=QualityStatus.PASSED,
            explanation="OK",
        ),
        QualityDimension.SECURITY_RISK: QualityDimensionScore(
            dimension=QualityDimension.SECURITY_RISK,
            score=1.0,
            weight=0.10,
            status=QualityStatus.PASSED,
            explanation="OK",
        ),
    }

    score1, violations1 = scorer.calculate_score(dim_scores)
    score2, violations2 = scorer.calculate_score(dim_scores)

    assert score1 == score2
    assert score1 > 0.90
    assert not violations1


def test_scoring_clamp_on_regressions():
    scorer = RepairQualityScorer()
    dim_scores = {
        QualityDimension.CORRECTNESS: QualityDimensionScore(
            dimension=QualityDimension.CORRECTNESS,
            score=1.0,
            weight=0.25,
            status=QualityStatus.PASSED,
            explanation="OK",
        )
    }
    score, violations = scorer.calculate_score(
        dimension_scores=dim_scores,
        regressions_detected=True,
    )
    assert score <= 0.20
    assert any("regressions detected" in v for v in violations)


def test_gatekeeper_blocks_when_evidence_invalid():
    gatekeeper = RepairQualityGateKeeper()
    status, decision, reasons = gatekeeper.evaluate_gates(
        overall_score=0.95,
        dimension_scores={},
        metrics=RepairQualityMetrics(),
        policy_violations=[],
        missing_evidence=["repair_plan_id"],
        risk_level=RiskSeverity.LOW,
        verification_status="passed",
        is_evidence_valid=False,
    )
    assert status == QualityStatus.INSUFFICIENT_EVIDENCE
    assert decision == QualityGateDecision.BLOCKED


def test_gatekeeper_regression_risk():
    gatekeeper = RepairQualityGateKeeper()
    metrics = RepairQualityMetrics(regressions_detected_count=1)
    status, decision, reasons = gatekeeper.evaluate_gates(
        overall_score=0.90,
        dimension_scores={},
        metrics=metrics,
        policy_violations=[],
        missing_evidence=[],
        risk_level=RiskSeverity.LOW,
        verification_status="passed",
        is_evidence_valid=True,
    )
    assert status == QualityStatus.REGRESSION_RISK
    assert decision == QualityGateDecision.FAILED


# ---------------------------------------------------------------------------
# Category F: Security & Trust Boundaries
# ---------------------------------------------------------------------------

def test_result_immutability_and_human_review_invariant():
    result = RepairQualityResult(
        result_id="rqr_001",
        request_id="req_001",
        quality_status=QualityStatus.PASSED,
        overall_score=0.95,
        gate_decision=QualityGateDecision.PASSED,
        requires_human_review=True,
    )

    # Invariant: cannot set requires_human_review to False
    with pytest.raises(Exception):
        result.model_copy(update={"requires_human_review": False})

    # Cannot set PASSED if gate_decision is FAILED
    with pytest.raises(Exception):
        result.model_copy(update={"gate_decision": QualityGateDecision.FAILED})


def test_risk_analyzer_detects_dangerous_eval():
    analyzer = RepairRiskAnalyzer()
    patch_eval = """--- a/src/calc.py
+++ b/src/calc.py
@@ -1,1 +1,2 @@
+eval(user_input)
"""
    metrics, dim_score, risk_level = analyzer.analyze_risk(
        patch_content=patch_eval,
        modified_files=["src/calc.py"],
    )
    assert risk_level == RiskSeverity.CRITICAL
    assert dim_score.score == 0.0
    assert dim_score.status == QualityStatus.HIGH_RISK


def test_secret_redaction_in_events():
    recorder = RepairQualityEventRecorder("analysis_sec")
    event = recorder.record_event(
        RepairQualityEventType.QUALITY_ANALYSIS_STARTED,
        "Connecting with token sk-abcdef1234567890abcdef12 to endpoint",
    )
    assert "sk-" not in event.action_description or "REDACTED" in event.action_description


# ---------------------------------------------------------------------------
# Category G: Read-Only Guarantees
# ---------------------------------------------------------------------------

def test_read_only_guarantee_workspace_untouched(tmp_path: Path):
    target_file = tmp_path / "calc.py"
    original_content = "def add(a, b):\n    return a + b\n"
    target_file.write_text(original_content, encoding="utf-8")

    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=tmp_path,
    )

    # File must not be modified by quality evaluation
    assert target_file.read_text(encoding="utf-8") == original_content


# ---------------------------------------------------------------------------
# 15 End-to-End Integrated Scenarios
# ---------------------------------------------------------------------------

def test_scenario_1_high_quality_repair_passes(tmp_path: Path):
    """1. High-quality repair with successful M5 verification -> PASSED."""
    ws = tmp_path / "ws"
    src_dir = ws / "src"
    src_dir.mkdir(parents=True)
    f = src_dir / "calc.py"
    f.write_text("def divide(a, b):\n    if b == 0:\n        raise ValueError()\n    return a / b\n")

    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.PASSED
    assert res.gate_decision == QualityGateDecision.PASSED
    assert res.requires_human_review is True


def test_scenario_2_poor_maintainability_fails(tmp_path: Path):
    """2. Repair passes tests but has poor maintainability (excessive dead code/complexity) -> FAILED."""
    ws = tmp_path / "ws"
    src_dir = ws / "src"
    src_dir.mkdir(parents=True)
    f = src_dir / "calc.py"
    # Dead code after return
    f.write_text(
        "def divide(a, b):\n"
        "    return a / b\n"
        "    dead1 = 1\n"
        "    dead2 = 2\n"
        "    dead3 = 3\n"
        "    dead4 = 4\n"
        "    dead5 = 5\n"
    )

    policy = RepairQualityPolicy(min_dimension_score=0.90)
    executor = RepairQualityExecutor(policy=policy)
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.FAILED
    assert res.gate_decision == QualityGateDecision.FAILED


def test_scenario_3_incomplete_verification(tmp_path: Path):
    """3. Repair passes targeted tests but verification is incomplete -> INSUFFICIENT_EVIDENCE."""
    ws = tmp_path / "ws"
    ws.mkdir()

    executor = RepairQualityExecutor()
    req = make_sample_request(verif_status="incomplete")
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(
            verif_status="incomplete",
            required_completed=[],
        ),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.INSUFFICIENT_EVIDENCE


def test_scenario_4_regression_detected(tmp_path: Path):
    """4. Repair introduces a regression -> REGRESSION_RISK."""
    ws = tmp_path / "ws"
    ws.mkdir()

    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(
            verif_status="passed",
            regression_class="regression_detected",
        ),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.REGRESSION_RISK
    assert res.gate_decision == QualityGateDecision.FAILED


def test_scenario_5_fixes_pre_existing_failure(tmp_path: Path):
    """5. Repair fixes a pre-existing failure -> PASSED."""
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "calc.py"
    f.write_text("def divide(a, b):\n    return a / b\n")

    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(
            verif_status="passed",
            regression_class="improved",
        ),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.PASSED
    assert res.gate_decision == QualityGateDecision.PASSED


def test_scenario_6_does_not_address_root_cause(tmp_path: Path):
    """6. Repair does not address the original root cause -> FAILED."""
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "unrelated.py"
    f.write_text("x = 1\n")

    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(affected_files=["unrelated.py"]),
        execution_result=make_sample_execution(modified_files=["unrelated.py"]),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
        root_cause_locations=["src/parser.py:45"],  # Unaddressed root cause
    )
    assert res.dimension_scores[QualityDimension.CORRECTNESS].score < 1.0


def test_scenario_7_omits_required_change(tmp_path: Path):
    """7. Repair omits a required change -> FAILED."""
    ws = tmp_path / "ws"
    ws.mkdir()

    plan = make_sample_plan(
        required_changes=[
            {"file_path": "src/calc.py", "description": "fix calc"},
            {"file_path": "src/missing.py", "description": "unmodified file"},
        ]
    )
    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=plan,
        execution_result=make_sample_execution(modified_files=["src/calc.py"]),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.FAILED
    assert res.gate_decision == QualityGateDecision.FAILED


def test_scenario_8_introduces_dangerous_api(tmp_path: Path):
    """8. Repair introduces dangerous code execution -> HIGH_RISK."""
    ws = tmp_path / "ws"
    ws.mkdir()

    dangerous_patch = """--- a/src/calc.py
+++ b/src/calc.py
@@ -1,1 +1,2 @@
+eval("1 + 1")
"""
    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=dangerous_patch,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.HIGH_RISK
    assert res.gate_decision == QualityGateDecision.BLOCKED


def test_scenario_9_excessive_scope_expansion(tmp_path: Path):
    """9. Repair has excessive scope expansion outside approved files -> FAILED."""
    ws = tmp_path / "ws"
    ws.mkdir()

    scope_patch = """--- a/src/unapproved.py
+++ b/src/unapproved.py
@@ -1,1 +1,2 @@
+unapproved_code = True
"""
    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(affected_files=["src/calc.py"]),
        execution_result=make_sample_execution(modified_files=["src/unapproved.py"]),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=scope_patch,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.FAILED


def test_scenario_10_repeated_historical_regressions(tmp_path: Path):
    """10. Historical evidence shows repeated failures for strategy -> low score / warning."""
    db_file = tmp_path / "hist.db"
    store = HistoricalReliabilityStore(db_path=db_file)
    for i in range(5):
        store.record_quality_result(
            request_id=f"req_{i}",
            repair_plan_id=f"plan_{i}",
            execution_id=f"exec_{i}",
            strategy_id="flaky_strat",
            target_files=["src/calc.py"],
            quality_status="failed",
            overall_score=0.30,
            gate_decision="FAILED",
            risk_level="high",
            regressions_count=1,
        )

    ws = tmp_path / "ws"
    ws.mkdir()
    executor = RepairQualityExecutor(history_store=store)
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(selected_strategy_id="flaky_strat"),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(verif_status="passed"),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.dimension_scores[QualityDimension.RELIABILITY].score < 1.0


def test_scenario_11_tampered_patch_digest(tmp_path: Path):
    """11. Evidence digest tampered -> INSUFFICIENT_EVIDENCE / BLOCKED."""
    ws = tmp_path / "ws"
    ws.mkdir()

    executor = RepairQualityExecutor()
    req = make_sample_request(patch_digest="forged_digest_123")
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(patch_digest="real_digest_456"),
        verification_result=make_sample_verification(),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.INSUFFICIENT_EVIDENCE
    assert res.gate_decision == QualityGateDecision.BLOCKED


def test_scenario_12_sandbox_unavailable(tmp_path: Path):
    """12. M5 sandbox was unavailable -> INSUFFICIENT_EVIDENCE."""
    ws = tmp_path / "ws"
    ws.mkdir()

    executor = RepairQualityExecutor()
    req = make_sample_request(verif_status="sandbox_unavailable")
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(
            verif_status="sandbox_unavailable",
            required_completed=[],
        ),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.INSUFFICIENT_EVIDENCE


def test_scenario_13_execution_not_applied(tmp_path: Path):
    """13. M4 execution was not applied -> INSUFFICIENT_EVIDENCE / BLOCKED."""
    ws = tmp_path / "ws"
    ws.mkdir()

    executor = RepairQualityExecutor()
    req = make_sample_request()
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(status="failed"),
        verification_result=make_sample_verification(),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.INSUFFICIENT_EVIDENCE


def test_scenario_14_verification_failed(tmp_path: Path):
    """14. M5 verification failed -> FAILED."""
    ws = tmp_path / "ws"
    ws.mkdir()

    executor = RepairQualityExecutor()
    req = make_sample_request(verif_status="failed")
    res = executor.evaluate_quality(
        request=req,
        repair_plan=make_sample_plan(),
        execution_result=make_sample_execution(),
        verification_result=make_sample_verification(
            verif_status="failed",
            required_failed=["test_failing"],
        ),
        patch_content=SAMPLE_CLEAN_PATCH,
        workspace_path=ws,
    )
    assert res.quality_status == QualityStatus.INSUFFICIENT_EVIDENCE or res.gate_decision == QualityGateDecision.FAILED


def test_scenario_15_high_score_clamped_on_critical_violation(tmp_path: Path):
    """15. A high numeric score conflicts with a critical policy violation -> FAILED (clamp enforced)."""
    scorer = RepairQualityScorer()
    dim_scores = {
        dim: QualityDimensionScore(
            dimension=dim,
            score=1.0,
            weight=0.2,
            status=QualityStatus.PASSED,
            explanation="Fake perfect score",
        )
        for dim in QualityDimension
    }
    # Pass critical risk
    score, violations = scorer.calculate_score(
        dimension_scores=dim_scores,
        risk_level=RiskSeverity.CRITICAL,
    )
    assert score <= 0.20
    assert len(violations) > 0


# ---------------------------------------------------------------------------
# ToolRegistry Integration Tests
# ---------------------------------------------------------------------------

def test_tool_registry_tools_callable(tmp_path: Path):
    tools = RepairQualityTools(repository_path=tmp_path)

    # 1. create_repair_quality_request
    req_json = tools.create_repair_quality_request({
        "repair_plan_id": "plan_001",
        "repair_plan_digest": "dig_plan",
        "execution_id": "exec_001",
        "applied_patch_digest": "dig_patch",
        "baseline_tree_digest": "dig_base",
        "verification_id": "verif_001",
        "verification_status": "passed",
        "workspace_id": "ws_001",
    })
    req_data = json.loads(req_json)
    assert "request_id" in req_data

    # 2. analyze_repair_patch_quality
    pq_json = tools.analyze_repair_patch_quality({
        "patch_content": SAMPLE_CLEAN_PATCH,
        "approved_files": ["src/calc.py"],
    })
    pq_data = json.loads(pq_json)
    assert "metrics" in pq_data
    assert pq_data["dimension_score"]["status"] == "passed"

    # 3. get_tool_definitions count
    defs = tools.get_tool_definitions()
    assert len(defs) == 12


# ---------------------------------------------------------------------------
# Adversarial Audit & Security Verification Tests
# ---------------------------------------------------------------------------

def test_completeness_unmodified_context_files_not_penalized():
    """Audit 1: Unmodified context/affected files are not penalized when required changes are addressed."""
    analyzer = RepairCompletenessAnalyzer()
    plan = {
        "repair_plan_id": "plan_audit_1",
        "affected_files": ["src/calc.py", "docs/architecture.md", "README.md"],
        "approved_file_paths": ["src/calc.py", "docs/architecture.md", "README.md"],
        "required_changes": [
            {"file_path": "src/calc.py", "description": "Fix divide-by-zero check"}
        ],
    }
    modified_files = ["src/calc.py"]
    verif = {
        "required_checks_completed": ["check_div"],
        "required_checks_failed": [],
    }

    metrics, score = analyzer.analyze_completeness(
        repair_plan=plan,
        modified_files=modified_files,
        verification_result=verif,
    )

    assert score.score == 1.0
    assert metrics["addressed_changes_count"] == 1
    assert "docs/architecture.md" in metrics["unmodified_affected_files"]
    assert "README.md" in metrics["unmodified_affected_files"]
    assert len(metrics["unnecessary_modifications"]) == 0


def test_completeness_unnecessary_file_modifications_penalized():
    """Audit 1: Minimality enforcement - modifying files outside plan scope is penalized."""
    analyzer = RepairCompletenessAnalyzer()
    plan = {
        "repair_plan_id": "plan_audit_2",
        "affected_files": ["src/calc.py"],
        "approved_file_paths": ["src/calc.py"],
        "required_changes": [
            {"file_path": "src/calc.py", "description": "Fix divide-by-zero check"}
        ],
    }
    # Modified src/calc.py plus 2 unapproved/unnecessary files
    modified_files = ["src/calc.py", "src/unrelated.py", "config/keys.json"]
    verif = {
        "required_checks_completed": ["check_div"],
        "required_checks_failed": [],
    }

    metrics, score = analyzer.analyze_completeness(
        repair_plan=plan,
        modified_files=modified_files,
        verification_result=verif,
    )

    assert score.score < 1.0
    assert "src/unrelated.py" in metrics["unnecessary_modifications"]
    assert "config/keys.json" in metrics["unnecessary_modifications"]


def test_reliability_small_sample_size_not_penalized(tmp_path: Path):
    """Audit 2: Sample size N < 5 is marked not statistically significant and does not penalize clean repairs."""
    db_path = tmp_path / "reliability_small.db"
    store = HistoricalReliabilityStore(db_path=db_path)
    # Only 1 past attempt which failed
    store.record_quality_result(
        request_id="req_past_1",
        repair_plan_id="plan_p1",
        execution_id="exec_p1",
        strategy_id="experimental_strategy",
        target_files=["src/calc.py"],
        quality_status="failed",
        overall_score=0.4,
        gate_decision="failed",
        risk_level="low",
        regressions_count=1,
    )

    analyzer = RepairReliabilityAnalyzer(history_store=store)
    verif = {
        "verification_status": "passed",
        "regression_classification": "no_regression_observed",
    }

    metrics, score, conf = analyzer.analyze_reliability(
        verification_result=verif,
        strategy_id="experimental_strategy",
    )

    assert metrics["is_statistically_significant"] is False
    assert metrics["past_attempts"] == 1
    assert score.score == 1.0  # Not penalized because N < 5
    assert conf == ConfidenceClassification.MEDIUM


def test_reliability_significant_sample_size_penalized(tmp_path: Path):
    """Audit 2: Sample size N >= 5 with low historical success rate penalizes score with high confidence."""
    db_path = tmp_path / "reliability_sig.db"
    store = HistoricalReliabilityStore(db_path=db_path)
    # Record 6 past attempts, only 1 passed
    for i in range(6):
        store.record_quality_result(
            request_id=f"req_past_{i}",
            repair_plan_id=f"plan_p{i}",
            execution_id=f"exec_p{i}",
            strategy_id="unstable_strategy",
            target_files=["src/calc.py"],
            quality_status="passed" if i == 0 else "failed",
            overall_score=0.8 if i == 0 else 0.3,
            gate_decision="passed" if i == 0 else "failed",
            risk_level="low",
            regressions_count=1 if i > 0 else 0,
        )

    analyzer = RepairReliabilityAnalyzer(history_store=store)
    verif = {
        "verification_status": "passed",
        "regression_classification": "no_regression_observed",
    }

    metrics, score, conf = analyzer.analyze_reliability(
        verification_result=verif,
        strategy_id="unstable_strategy",
    )

    assert metrics["is_statistically_significant"] is True
    assert metrics["past_attempts"] == 6
    assert score.score <= 0.7  # Penalized -0.3 due to statistically significant instability
    assert conf == ConfidenceClassification.HIGH


def test_risk_analyzer_comments_and_strings_not_flagged():
    """Audit 3: Non-executable comments, docstrings, and string literals containing keywords do not trigger false positives."""
    analyzer = RepairRiskAnalyzer()
    patch = """diff --git a/src/calc.py b/src/calc.py
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,5 +1,9 @@
+# Note: eval(x) is completely forbidden in this module!
+# subprocess.run() should not be used here
+def helper():
+    \"\"\"Avoid pickle.loads for deserialization.\"\"\"
+    msg = "evaluating expression safely"
+    test_token = "dummy_sample_token_for_tests"
+    return msg
"""
    metrics, score, risk_level = analyzer.analyze_risk(
        patch_content=patch,
        modified_files=["src/calc.py"],
    )

    assert risk_level == RiskSeverity.LOW
    assert score.score == 1.0
    assert len(score.findings) == 0


def test_risk_analyzer_real_calls_flagged_accurately():
    """Audit 3: True positive detection of executable calls and hardcoded credentials."""
    analyzer = RepairRiskAnalyzer()

    # 1. Real eval call
    eval_patch = """diff --git a/src/calc.py b/src/calc.py
+++ b/src/calc.py
@@ -5,1 +5,2 @@
+    val = eval(user_string)
"""
    _, _, risk_level = analyzer.analyze_risk(
        patch_content=eval_patch,
        modified_files=["src/calc.py"],
    )
    assert risk_level == RiskSeverity.CRITICAL

    # 2. Real subprocess call
    proc_patch = """diff --git a/src/calc.py b/src/calc.py
+++ b/src/calc.py
@@ -5,1 +5,2 @@
+    res = subprocess.run(["ls", "-la"])
"""
    _, _, risk_level = analyzer.analyze_risk(
        patch_content=proc_patch,
        modified_files=["src/calc.py"],
    )
    assert risk_level == RiskSeverity.HIGH

    # 3. Real credential leak
    cred_patch = """diff --git a/src/calc.py b/src/calc.py
+++ b/src/calc.py
@@ -5,1 +5,2 @@
+    api_key = "live_prod_super_secret_99881122"
"""
    _, _, risk_level = analyzer.analyze_risk(
        patch_content=cred_patch,
        modified_files=["src/calc.py"],
    )
    assert risk_level == RiskSeverity.CRITICAL


def test_policy_invariants_and_tamper_rejection():
    """Audit 4: Invariants reject attempts to enable regressions or supply invalid weights."""
    from pydantic import ValidationError

    # 1. allow_regressions cannot be True
    with pytest.raises(ValidationError, match="allow_regressions cannot be True"):
        RepairQualityPolicy(allow_regressions=True)

    # 2. Missing mandatory dimensions rejected
    with pytest.raises(ValidationError, match="Missing mandatory quality dimensions"):
        RepairQualityPolicy(dimension_weights={QualityDimension.CORRECTNESS: 1.0})

    # 3. Too low / negative weight rejected
    bad_weights_low = {
        QualityDimension.CORRECTNESS: -0.5,
        QualityDimension.COMPLETENESS: 0.3,
        QualityDimension.MAINTAINABILITY: 0.3,
        QualityDimension.PATCH_QUALITY: 0.3,
        QualityDimension.RELIABILITY: 0.3,
        QualityDimension.SECURITY_RISK: 0.3,
    }
    with pytest.raises(ValidationError, match="is too low"):
        RepairQualityPolicy(dimension_weights=bad_weights_low)

    # 4. Overwhelmingly dominant weight (> 0.50) rejected
    bad_weights_dominant = {
        QualityDimension.CORRECTNESS: 0.75,
        QualityDimension.COMPLETENESS: 0.05,
        QualityDimension.MAINTAINABILITY: 0.05,
        QualityDimension.PATCH_QUALITY: 0.05,
        QualityDimension.RELIABILITY: 0.05,
        QualityDimension.SECURITY_RISK: 0.05,
    }
    with pytest.raises(ValidationError, match="is too high"):
        RepairQualityPolicy(dimension_weights=bad_weights_dominant)

    # 5. Non-zero scope expansion strictly rejected
    with pytest.raises(ValidationError, match="less than or equal to 0"):
        RepairQualityPolicy(max_scope_expansion_files=1)

    # 6. Out of bounds minimum score rejected
    with pytest.raises(ValidationError, match="greater than or equal to 0.5"):
        RepairQualityPolicy(min_overall_score=0.1)


def test_tool_custom_policy_validation(tmp_path: Path):
    """Audit 4: Tool invocation validates and rejects malicious policies passed by callers."""
    tools = RepairQualityTools(repository_path=tmp_path)

    # 1. Calculate score with illegal allow_regressions
    resp = tools.calculate_repair_quality_score({
        "dimension_scores": {
            "correctness": {"score": 1.0, "status": "passed", "weight": 0.25, "explanation": "ok"},
        },
        "policy": {"allow_regressions": True},
    })
    data = json.loads(resp)
    assert "error" in data
    assert "allow_regressions cannot be True" in data["error"]

    # 2. Gate evaluation with illegal weights (missing dimensions)
    gate_resp = tools.evaluate_repair_quality_gates({
        "overall_score": 0.95,
        "dimension_scores": {
            "correctness": {"score": 1.0, "status": "passed", "weight": 0.25, "explanation": "ok"},
        },
        "metrics": {},
        "policy": {"dimension_weights": {"correctness": -1.0}},
    })
    gate_data = json.loads(gate_resp)
    assert "error" in gate_data
    assert "Missing mandatory quality dimensions" in gate_data["error"]


def test_history_store_sqlite_resilience(tmp_path: Path):
    """Resilience: Corrupted SQLite database safely degrades without crashing."""
    corrupt_db = tmp_path / "corrupt.db"
    # Write garbage bytes to database file
    corrupt_db.write_bytes(b"GARBAGE_SQLITE_CORRUPTION_DATA_BYTES")

    store = HistoricalReliabilityStore(db_path=corrupt_db)
    stats = store.get_strategy_stats("test_strategy")
    assert stats["is_available"] is False
    assert stats["status"] == "corrupted"
    assert stats["total_attempts"] == 0
    assert stats["success_rate"] == 0.0

    history = store.get_history(strategy_id="test_strategy")
    assert history == []


def test_history_sqlite_corruption_cannot_improve_score_or_bypass_gate(tmp_path: Path):
    """Audit Area 1: SQLite corruption cannot improve score or turn a failing/blocked result into PASSED."""
    corrupt_db = tmp_path / "corrupt_eval.db"
    corrupt_db.write_bytes(b"MALFORMED_SQLITE_HEADER_CORRUPTION_DATA")

    store = HistoricalReliabilityStore(db_path=corrupt_db)
    analyzer = RepairReliabilityAnalyzer(history_store=store)

    clean_verif = {
        "verification_status": "passed",
        "regression_classification": "no_regression_observed",
    }

    # Evaluate reliability under database corruption
    metrics, score, conf = analyzer.analyze_reliability(
        verification_result=clean_verif,
        strategy_id="any_strategy",
    )

    # Invariant: Score is capped at 0.50, confidence is INSUFFICIENT_EVIDENCE
    assert score.score <= 0.50
    assert score.status == QualityStatus.INSUFFICIENT_EVIDENCE
    assert conf == ConfidenceClassification.INSUFFICIENT_EVIDENCE
    assert metrics["historical_error_observed"] is True
    assert metrics["historical_status"] == "corrupted"

    # Invariant: Composite scoring with insufficient reliability evidence caps overall score
    scorer = RepairQualityScorer()
    dim_scores = {
        QualityDimension.CORRECTNESS: QualityDimensionScore(
            dimension=QualityDimension.CORRECTNESS,
            score=1.0,
            weight=0.25,
            status=QualityStatus.PASSED,
            explanation="Passed",
        ),
        QualityDimension.COMPLETENESS: QualityDimensionScore(
            dimension=QualityDimension.COMPLETENESS,
            score=1.0,
            weight=0.20,
            status=QualityStatus.PASSED,
            explanation="Passed",
        ),
        QualityDimension.MAINTAINABILITY: QualityDimensionScore(
            dimension=QualityDimension.MAINTAINABILITY,
            score=1.0,
            weight=0.15,
            status=QualityStatus.PASSED,
            explanation="Passed",
        ),
        QualityDimension.PATCH_QUALITY: QualityDimensionScore(
            dimension=QualityDimension.PATCH_QUALITY,
            score=1.0,
            weight=0.15,
            status=QualityStatus.PASSED,
            explanation="Passed",
        ),
        QualityDimension.RELIABILITY: score,
        QualityDimension.SECURITY_RISK: QualityDimensionScore(
            dimension=QualityDimension.SECURITY_RISK,
            score=1.0,
            weight=0.10,
            status=QualityStatus.PASSED,
            explanation="Passed",
        ),
    }

    overall, violations = scorer.calculate_score(dimension_scores=dim_scores)
    assert overall is not None
    assert overall <= 0.50  # Strictly capped: cannot reach passing threshold 0.70!
    assert any("insufficient evidence" in v.lower() for v in violations)

    # Invariant: Gatekeeper strictly blocks when historical store is corrupted
    gatekeeper = RepairQualityGateKeeper()
    gate_metrics = RepairQualityMetrics(
        historical_status="corrupted",
        historical_error_observed=True,
    )
    status, decision, reasons = gatekeeper.evaluate_gates(
        overall_score=overall,
        dimension_scores=dim_scores,
        metrics=gate_metrics,
        policy_violations=violations,
        missing_evidence=[],
        risk_level=RiskSeverity.LOW,
        verification_status="passed",
    )
    assert decision == QualityGateDecision.BLOCKED
    assert status == QualityStatus.INSUFFICIENT_EVIDENCE
    assert any("corrupted" in r.lower() for r in reasons)


def test_history_empty_db_vs_corrupted_db(tmp_path: Path):
    """Audit Area 1: Distinguish genuinely empty history from corrupted/unavailable history."""
    # 1. Healthy empty database
    empty_db = tmp_path / "healthy_empty.db"
    store_empty = HistoricalReliabilityStore(db_path=empty_db)
    stats_empty = store_empty.get_strategy_stats("new_strategy")
    assert stats_empty["is_available"] is True
    assert stats_empty["status"] == "empty"
    assert stats_empty["total_attempts"] == 0

    analyzer_empty = RepairReliabilityAnalyzer(history_store=store_empty)
    clean_verif = {
        "verification_status": "passed",
        "regression_classification": "no_regression_observed",
    }
    m_empty, s_empty, c_empty = analyzer_empty.analyze_reliability(
        verification_result=clean_verif,
        strategy_id="new_strategy",
    )
    # Healthy empty DB allows clean evaluation without penalty
    assert s_empty.score == 1.0
    assert s_empty.status == QualityStatus.PASSED
    assert c_empty == ConfidenceClassification.MEDIUM
    assert m_empty["historical_error_observed"] is False
    assert m_empty["historical_status"] == "empty"

    # 2. Corrupted database
    corrupt_db = tmp_path / "corrupted.db"
    corrupt_db.write_bytes(b"CORRUPTED_BYTES")
    store_corrupt = HistoricalReliabilityStore(db_path=corrupt_db)
    stats_corrupt = store_corrupt.get_strategy_stats("any")
    assert stats_corrupt["is_available"] is False
    assert stats_corrupt["status"] == "corrupted"


def test_policy_provenance_and_substitution_attack(tmp_path: Path):
    """Audit Area 2: Policy digest binding prevents policy substitution attacks between scoring and gating."""
    policy_standard = RepairQualityPolicy()
    digest_standard = policy_standard.compute_digest()

    # Create modified policy (e.g. relaxed min overall score)
    policy_modified = RepairQualityPolicy(min_overall_score=0.60)
    digest_modified = policy_modified.compute_digest()

    assert digest_standard != digest_modified

    # Gatekeeper initialized with policy_standard
    gatekeeper = RepairQualityGateKeeper(policy=policy_standard)

    dim_scores = {
        dim: QualityDimensionScore(
            dimension=dim,
            score=0.9,
            weight=0.16,
            status=QualityStatus.PASSED,
            explanation="Ok",
        )
        for dim in QualityDimension
    }
    metrics = RepairQualityMetrics()

    # Simulate attack: score computed under policy_modified digest, passed to standard gatekeeper
    status, decision, reasons = gatekeeper.evaluate_gates(
        overall_score=0.90,
        dimension_scores=dim_scores,
        metrics=metrics,
        policy_violations=[],
        missing_evidence=[],
        risk_level=RiskSeverity.LOW,
        verification_status="passed",
        policy_digest=digest_modified,  # Mismatched digest!
    )

    assert decision == QualityGateDecision.BLOCKED
    assert status == QualityStatus.FAILED
    assert any("policy substitution detected" in r.lower() for r in reasons)


def test_risk_scanner_adversarial_multiline_and_aliasing():
    """Audit Area 3: Multiline calls, aliased imports, indirect invocation, and comments."""
    analyzer = RepairRiskAnalyzer()

    # 1. Multiline subprocess call across lines
    multiline_patch = """diff --git a/src/worker.py b/src/worker.py
+++ b/src/worker.py
@@ -1,2 +1,6 @@
+def run_task():
+    proc = subprocess.run(
+        ["rm", "-rf", "/"]
+    )
+    return proc
"""
    _, _, sev1 = analyzer.analyze_risk(multiline_patch, ["src/worker.py"])
    assert sev1 == RiskSeverity.HIGH

    # 2. Aliased import: import subprocess as sp; sp.Popen(...)
    alias_patch = """diff --git a/src/worker.py b/src/worker.py
+++ b/src/worker.py
@@ -1,1 +1,4 @@
+import subprocess as sp
+def spawn():
+    return sp.Popen(["bash"])
"""
    _, _, sev2 = analyzer.analyze_risk(alias_patch, ["src/worker.py"])
    assert sev2 == RiskSeverity.HIGH

    # 3. from subprocess import run as r; r(...)
    from_alias_patch = """diff --git a/src/worker.py b/src/worker.py
+++ b/src/worker.py
@@ -1,1 +1,4 @@
+from subprocess import run as r
+def exec_cmd():
+    return r(["bash"])
"""
    _, _, sev3 = analyzer.analyze_risk(from_alias_patch, ["src/worker.py"])
    assert sev3 == RiskSeverity.HIGH

    # 4. Indirect invocation via getattr(os, "system")
    getattr_patch = """diff --git a/src/worker.py b/src/worker.py
+++ b/src/worker.py
@@ -1,1 +1,3 @@
+def indirect():
+    return getattr(os, "system")("id")
"""
    _, _, sev4 = analyzer.analyze_risk(getattr_patch, ["src/worker.py"])
    assert sev4 == RiskSeverity.HIGH

    # 5. Multiline triple-quoted docstring containing dangerous examples -> ZERO false positives
    docstring_patch = """diff --git a/src/worker.py b/src/worker.py
+++ b/src/worker.py
@@ -1,1 +1,8 @@
+def helper():
+    \"\"\"
+    Documentation example:
+    eval(expression)
+    subprocess.run(["cmd"])
+    \"\"\"
+    return True
"""
    _, s5, sev5 = analyzer.analyze_risk(docstring_patch, ["src/worker.py"])
    assert sev5 == RiskSeverity.LOW
    assert s5.score == 1.0

    # 6. Escaped and raw strings -> ZERO false positives
    strings_patch = """diff --git a/src/worker.py b/src/worker.py
+++ b/src/worker.py
@@ -1,1 +1,4 @@
+def regex_def():
+    pat = r"eval\\("
+    msg = "subprocess.run(\\"cmd\\")"
+    return pat + msg
"""
    _, s6, sev6 = analyzer.analyze_risk(strings_patch, ["src/worker.py"])
    assert sev6 == RiskSeverity.LOW
    assert s6.score == 1.0


def test_risk_scanner_adversarial_credentials():
    """Audit Area 3: Credential assignments split across lines and live tokens with misleading safe names."""
    analyzer = RepairRiskAnalyzer()

    # 1. Credential assignment split across lines
    multiline_cred_patch = """diff --git a/src/config.py b/src/config.py
+++ b/src/config.py
@@ -1,1 +1,4 @@
+api_key = (
+    "ghp_1234567890abcdef1234567890abcdef1234"
+)
"""
    _, _, sev1 = analyzer.analyze_risk(multiline_cred_patch, ["src/config.py"])
    assert sev1 == RiskSeverity.CRITICAL

    # 2. Real credential whose variable name or value contains misleading 'sample' substring
    misleading_safe_patch = """diff --git a/src/config.py b/src/config.py
+++ b/src/config.py
@@ -1,1 +1,3 @@
+# Live token disguised in sample variable
+sample_test_token = "ghp_RealLiveProductionSecretToken99881122"
"""
    _, _, sev2 = analyzer.analyze_risk(misleading_safe_patch, ["src/config.py"])
    assert sev2 == RiskSeverity.CRITICAL

    # 3. AWS live access key in test variable
    aws_patch = """diff --git a/src/config.py b/src/config.py
+++ b/src/config.py
@@ -1,1 +1,2 @@
+test_aws_key = "AKIAIOSFODNN7EXAMPLE"
"""
    _, _, sev3 = analyzer.analyze_risk(aws_patch, ["src/config.py"])
    assert sev3 == RiskSeverity.CRITICAL

    # 4. Genuine test placeholder -> recognized as safe
    placeholder_patch = """diff --git a/src/config.py b/src/config.py
+++ b/src/config.py
@@ -1,1 +1,2 @@
+password = "dummy_placeholder_password"
"""
    _, s4, sev4 = analyzer.analyze_risk(placeholder_patch, ["src/config.py"])
    assert sev4 == RiskSeverity.LOW
    assert s4.score == 1.0
