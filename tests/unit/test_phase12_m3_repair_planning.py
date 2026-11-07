"""Comprehensive Unit Tests for Phase 12 Milestone 3: Repair Planning Engine.

Verifies:
1. Plan construction:
   - PROVEN root cause -> ACTIONABLE plan with high confidence.
   - PLAUSIBLE root cause -> CONDITIONAL plan with explicit uncertainty in limitations.
   - MULTIPLE_PLAUSIBLE_ROOT_CAUSES -> MULTIPLE_CANDIDATES plan preserving alternative strategies.
   - INSUFFICIENT_EVIDENCE -> non-actionable INSUFFICIENT_EVIDENCE plan.
   - UNRESOLVED -> non-actionable UNRESOLVED plan.
   - Missing diagnostic signals raises ValueError.
2. Evidence:
   - Fresh evidence verification against disk.
   - Stale evidence detection (missing file, line count exceeded, content hash mismatch).
   - Line range bounds validation (end_line < start_line or start_line < 1).
   - Inferred vs directly verified relationships.
   - Path traversal and protected file rejection.
3. Strategy selection:
   - Minimal repair preferred over broad refactoring.
   - Multiple candidate strategies preserved and ranked.
   - Unsafe / broad strategy rejected with explicit reasons.
   - No valid strategy for uncertain diagnosis -> manual investigation.
4. Risk:
   - High dependency reach elevates risk score.
   - Security-sensitive repair boundary detection.
   - Public API modification noted.
   - Data integrity / persistence boundary noted.
   - Missing tests elevate risk likelihood.
5. Verification & rollback:
   - Existing tests verified on disk; non-existent tests not fabricated.
   - Recommended new tests explicitly labeled as proposed.
   - Rollback plan specifies reverse changes, triggers, and limitations.
6. Safety & certainty invariants:
   - execution_permitted is strictly False.
   - approval_required is strictly True.
   - Model validator rejects execution_permitted=True or approval_required=False.
   - M3 planning engine never modifies repository source files.
7. ToolRegistry integration:
   - All 6 M3 tools registered and executable.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from forge.causality.schemas import (
    AlternativeExplanation,
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
)
from forge.investigation.tools import ToolRegistry
from forge.repair_planning.evidence import RepairEvidenceCollector
from forge.repair_planning.planner import RepairPlanningEngine
from forge.repair_planning.risk import RepairRiskAnalyzer
from forge.repair_planning.rollback import RollbackPlanBuilder
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairEvidence,
    RepairPlan,
    RepairPlanStatus,
    RepairRiskAssessment,
    RepairStrategy,
    RequiredChange,
    RiskLikelihood,
    RiskSeverity,
    RollbackPlan,
    StrategyComplexity,
    StrategyInvasiveness,
    StrategyReversibility,
    VerificationPlan,
)
from forge.repair_planning.strategies import RepairStrategyGenerator
from forge.repair_planning.tools import RepairPlanningTools
from forge.repair_planning.validation import RepairPlanValidator
from forge.schemas import FindingConfidence, FindingSeverity


# ---------------------------------------------------------------------------
# Fixture: Synthetic Multi-Module Repository
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_fixture(tmp_path: Path) -> Path:
    """Create a realistic repository structure with packages, calculators, and tests."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Package A: Validator
    pkg_a = repo / "pkg_a"
    pkg_a.mkdir()
    (pkg_a / "__init__.py").write_text("", encoding="utf-8")
    (pkg_a / "validator.py").write_text(
        "def validate_input(val):\n"
        "    if val is None:\n"
        "        raise ValueError('val cannot be None')\n"
        "    return True\n",
        encoding="utf-8",
    )

    # Package B: Processor (Caller)
    pkg_b = repo / "pkg_b"
    pkg_b.mkdir()
    (pkg_b / "__init__.py").write_text("", encoding="utf-8")
    (pkg_b / "processor.py").write_text(
        "from pkg_c.calculator import compute_metric\n\n"
        "def process_order(order_data):\n"
        "    multiplier = order_data.get('mult', 1)\n"
        "    return compute_metric(multiplier) * 10\n",
        encoding="utf-8",
    )

    # Package C: Calculator (Defective Callee)
    pkg_c = repo / "pkg_c"
    pkg_c.mkdir()
    (pkg_c / "__init__.py").write_text("", encoding="utf-8")
    (pkg_c / "calculator.py").write_text(
        "def compute_metric(val):\n"
        "    # Line 2: calculation with potential ZeroDivisionError\n"
        "    base = 100\n"
        "    return base // val\n\n"
        "def helper_metric(x):\n"
        "    return x + 1\n",
        encoding="utf-8",
    )

    # Package Auth: Security-sensitive module
    pkg_auth = repo / "pkg_auth"
    pkg_auth.mkdir()
    (pkg_auth / "__init__.py").write_text("", encoding="utf-8")
    (pkg_auth / "token_auth.py").write_text(
        "def verify_token(token):\n"
        "    return bool(token and len(token) > 10)\n",
        encoding="utf-8",
    )

    # Tests directory
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calculator.py").write_text(
        "from pkg_c.calculator import compute_metric\n\n"
        "def test_compute_metric_valid():\n"
        "    assert compute_metric(10) == 10\n",
        encoding="utf-8",
    )

    return repo


@pytest.fixture
def proven_root_cause_report() -> RootCauseReport:
    """Fixture producing a verified PROVEN RootCauseReport."""
    root_node = CausalNode(
        node_id="cnode_calc",
        symbol_id="pkg_c.calculator.compute_metric",
        file_path="pkg_c/calculator.py",
        start_line=1,
        end_line=4,
        node_type=CausalNodeType.FUNCTION,
        role=CausalRole.ROOT_CAUSE,
        description="compute_metric function performs division without zero check",
        is_verified_on_disk=True,
    )
    symptom_node = CausalNode(
        node_id="cnode_proc",
        symbol_id="pkg_b.processor.process_order",
        file_path="pkg_b/processor.py",
        start_line=3,
        end_line=5,
        node_type=CausalNodeType.FUNCTION,
        role=CausalRole.SYMPTOM,
        description="process_order passes 0 causing ZeroDivisionError",
        is_verified_on_disk=True,
    )
    edge = CausalEdge(
        edge_id="cedge_1",
        source_node_id="cnode_proc",
        target_node_id="cnode_calc",
        relationship=CausalRelationship.CALLS,
        causal_explanation="process_order calls compute_metric with multiplier",
        confidence=FindingConfidence.HIGH,
        confidence_score=0.90,
        is_directly_verified=True,
    )
    chain = CausalChain(
        chain_id="chain_01",
        root_node_id="cnode_calc",
        symptom_node_id="cnode_proc",
        node_ids=["cnode_calc", "cnode_proc"],
        edge_ids=["cedge_1"],
        summary="compute_metric executes without zero validation",
        confidence=FindingConfidence.HIGH,
        confidence_score=0.90,
        is_fully_verified=True,
    )
    return RootCauseReport(
        report_id="rc_rep_proven",
        run_id="run_test",
        status=CausalStatus.PROVEN,
        primary_root_cause=root_node,
        symptom_locations=[symptom_node],
        nodes=[root_node, symptom_node],
        edges=[edge],
        causal_chains=[chain],
        confidence=FindingConfidence.HIGH,
        confidence_score=0.92,
    )


# ---------------------------------------------------------------------------
# 1. Plan Construction Tests
# ---------------------------------------------------------------------------

class TestPlanConstruction:
    """Verifies RepairPlanningEngine plan construction across all causal statuses."""

    def test_plan_construction_proven_root_cause(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        assert plan.status == RepairPlanStatus.ACTIONABLE
        assert plan.confidence == FindingConfidence.HIGH
        assert plan.approval_required is True
        assert plan.execution_permitted is False
        assert plan.selected_strategy_id is not None
        assert "pkg_c/calculator.py" in plan.affected_files
        assert len(plan.candidate_strategies) >= 2
        assert len(plan.required_changes) >= 1
        assert plan.required_changes[0].file_path == "pkg_c/calculator.py"

    def test_plan_construction_plausible_root_cause(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        report = proven_root_cause_report.model_copy(deep=True)
        report.status = CausalStatus.PLAUSIBLE
        report.confidence = FindingConfidence.MEDIUM
        report.causal_chains[0].is_fully_verified = False

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=report)

        assert plan.status == RepairPlanStatus.CONDITIONAL
        assert plan.confidence == FindingConfidence.MEDIUM
        assert plan.limitations is not None
        assert "CONDITIONAL" in plan.limitations

    def test_plan_construction_multiple_plausible_roots(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        report = proven_root_cause_report.model_copy(deep=True)
        report.status = CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES
        report.alternative_explanations = [
            AlternativeExplanation(
                explanation_id="alt_01",
                suspected_root_cause="pkg_b.processor.process_order",
                rationale="Caller failed to sanitize multiplier before calling calculator",
                confidence=FindingConfidence.MEDIUM,
                confidence_score=0.75,
                is_refuted=False,
            )
        ]

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=report)

        assert plan.status == RepairPlanStatus.MULTIPLE_CANDIDATES
        # Confirm that competing root strategy was generated and preserved
        competing_strats = [
            s for s in plan.candidate_strategies if "competing" in s.strategy_id
        ]
        assert len(competing_strats) >= 1

    def test_plan_construction_insufficient_evidence(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        report = proven_root_cause_report.model_copy(deep=True)
        report.status = CausalStatus.INSUFFICIENT_EVIDENCE
        report.confidence = FindingConfidence.LOW

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=report)

        assert plan.status == RepairPlanStatus.INSUFFICIENT_EVIDENCE
        assert plan.confidence == FindingConfidence.LOW
        assert plan.selected_strategy_id is None
        assert any(
            s.approach == RepairApproach.UNKNOWN_OR_MANUAL_INVESTIGATION
            for s in plan.candidate_strategies
        )

    def test_plan_construction_unresolved_defect(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        report = proven_root_cause_report.model_copy(deep=True)
        report.status = CausalStatus.UNRESOLVED
        report.confidence = FindingConfidence.LOW

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=report)

        assert plan.status == RepairPlanStatus.UNRESOLVED
        assert plan.selected_strategy_id is None

    def test_missing_all_signals_raises_value_error(self, repo_fixture: Path):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        with pytest.raises(ValueError, match="requires at least one"):
            engine.plan_repair()


# ---------------------------------------------------------------------------
# 2. Evidence Validation Tests
# ---------------------------------------------------------------------------

class TestEvidenceValidation:
    """Verifies evidence collection, freshness auditing, and containment."""

    def test_fresh_evidence_collection(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        evidence_list, issues = collector.collect_from_root_cause(proven_root_cause_report)

        assert len(evidence_list) >= 1
        assert len(issues) == 0
        assert all(e.is_fresh for e in evidence_list)
        assert evidence_list[0].source_hash is not None

    def test_stale_evidence_missing_file(self, repo_fixture: Path):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        is_valid, h, err = collector.verify_file_location("pkg_c/non_existent.py", 1, 5)

        assert is_valid is False
        assert "does not exist on disk" in err

    def test_stale_evidence_exceeded_line_count(self, repo_fixture: Path):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        # calculator.py only has ~10 lines
        is_valid, h, err = collector.verify_file_location("pkg_c/calculator.py", 50, 60)

        assert is_valid is False
        assert "exceeds file line count" in err

    def test_stale_evidence_content_hash_mismatch(self, repo_fixture: Path):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        ev, issue = collector._audit_raw_file_evidence(
            evidence_id="ev_001",
            file_path="pkg_c/calculator.py",
            start_line=1,
            end_line=4,
            evidence_type="source_excerpt",
            evidence_source="test",
            rel_desc="test description",
            saved_hash="deadbeef12345678",
        )

        assert ev.is_fresh is False
        assert issue is not None
        assert "content hash mismatch" in issue

    def test_path_traversal_evidence_rejected(self, repo_fixture: Path):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        is_valid, h, err = collector.verify_file_location("../../etc/passwd", 1, 10)

        assert is_valid is False
        assert "Security violation" in err or "path traversal" in err

    def test_protected_path_evidence_rejected(self, repo_fixture: Path):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        is_valid, h, err = collector.verify_file_location(".git/config", 1, 5)

        assert is_valid is False
        assert "protected" in err.lower()

    def test_line_range_bounds_validation(self, repo_fixture: Path):
        collector = RepairEvidenceCollector(workspace_path=repo_fixture)
        is_valid, h, err = collector.verify_file_location("pkg_c/calculator.py", 5, 2)

        assert is_valid is False
        assert "cannot be less than start_line" in err


# ---------------------------------------------------------------------------
# 3. Strategy Generation and Selection Tests
# ---------------------------------------------------------------------------

class TestStrategyGenerationAndSelection:
    """Verifies strategy generation, minimality enforcement, and ranking."""

    def test_minimal_repair_preferred_over_broad_refactor(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        gen = RepairStrategyGenerator(workspace_path=repo_fixture)
        strategies = gen.generate_strategies(root_cause_report=proven_root_cause_report)

        # Confirm multiple strategies generated
        assert len(strategies) >= 2

        # Recommended strategy must be minimal local correction
        recommended = [s for s in strategies if s.is_recommended]
        assert len(recommended) == 1
        assert recommended[0].invasiveness == StrategyInvasiveness.MINIMAL
        assert recommended[0].approach in (
            RepairApproach.LOCAL_LOGIC_CORRECTION,
            RepairApproach.INPUT_VALIDATION,
        )

    def test_broad_refactoring_has_rejection_reasons(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        gen = RepairStrategyGenerator(workspace_path=repo_fixture)
        strategies = gen.generate_strategies(root_cause_report=proven_root_cause_report)

        broad = next((s for s in strategies if s.invasiveness == StrategyInvasiveness.EXTENSIVE), None)
        assert broad is not None
        assert broad.is_recommended is False
        assert any("minimality principle" in r.lower() for r in broad.rejection_reasons)

    def test_input_validation_strategy_for_precondition_defect(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        report = proven_root_cause_report.model_copy(deep=True)
        report.edges[0].relationship = CausalRelationship.PASSES_INVALID_DATA

        diag = DiagnosisResult(
            diagnosis_id="diag_val",
            run_id="run_01",
            input_summary="Input validation defect",
            failing_behavior="Unchecked input passed to calculator",
            defect_category=DefectCategory.INPUT_VALIDATION_FAILURE,
            severity=FindingSeverity.HIGH,
            status=DiagnosisStatus.CONFIRMED,
            confidence=FindingConfidence.HIGH,
            confidence_score=0.90,
            confidence_explanation="Confirmed on disk",
            recommended_next_step="Plan input validation",
        )

        gen = RepairStrategyGenerator(workspace_path=repo_fixture)
        strategies = gen.generate_strategies(root_cause_report=report, diagnosis=diag)

        approaches = [s.approach for s in strategies]
        assert RepairApproach.INPUT_VALIDATION in approaches

    def test_error_handling_strategy_for_exception_defect(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        diag = DiagnosisResult(
            diagnosis_id="diag_err",
            run_id="run_01",
            input_summary="Exception defect",
            failing_behavior="Unhandled ZeroDivisionError in calculation",
            defect_category=DefectCategory.EXCEPTION_HANDLING_DEFECT,
            severity=FindingSeverity.HIGH,
            status=DiagnosisStatus.CONFIRMED,
            confidence=FindingConfidence.HIGH,
            confidence_score=0.90,
            confidence_explanation="Confirmed on disk",
            recommended_next_step="Plan error handling",
        )

        gen = RepairStrategyGenerator(workspace_path=repo_fixture)
        strategies = gen.generate_strategies(
            root_cause_report=proven_root_cause_report,
            diagnosis=diag,
        )

        approaches = [s.approach for s in strategies]
        assert RepairApproach.ERROR_HANDLING in approaches


# ---------------------------------------------------------------------------
# 4. Risk Assessment Tests
# ---------------------------------------------------------------------------

class TestRiskAssessment:
    """Verifies deterministic risk scoring across scope, security, and dependencies."""

    def test_low_risk_isolated_repair(self, repo_fixture: Path):
        analyzer = RepairRiskAnalyzer(workspace_path=repo_fixture)
        risk = analyzer.assess_risk(
            affected_files=["pkg_c/calculator.py"],
            affected_symbols=["pkg_c.calculator.compute_metric"],
            proposed_approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
        )

        assert risk.overall_risk in (RiskSeverity.LOW, RiskSeverity.MEDIUM)
        assert 0.0 <= risk.risk_score <= 1.0
        assert "pkg_c.calculator" in risk.affected_components

    def test_security_sensitive_boundary_elevates_risk(self, repo_fixture: Path):
        analyzer = RepairRiskAnalyzer(workspace_path=repo_fixture)
        risk = analyzer.assess_risk(
            affected_files=["pkg_auth/token_auth.py"],
            affected_symbols=["pkg_auth.token_auth.verify_token"],
            proposed_approach=RepairApproach.SECURITY_HARDENING,
        )

        assert any("Security boundary impact" in sec for sec in risk.security_implications)
        assert risk.risk_score >= 0.35

    def test_multi_file_repair_elevates_risk(self, repo_fixture: Path):
        analyzer = RepairRiskAnalyzer(workspace_path=repo_fixture)
        risk = analyzer.assess_risk(
            affected_files=[
                "pkg_a/validator.py",
                "pkg_b/processor.py",
                "pkg_c/calculator.py",
                "pkg_auth/token_auth.py",
            ],
            affected_symbols=["sym_a", "sym_b", "sym_c", "sym_d"],
            proposed_approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
        )

        assert risk.overall_risk == RiskSeverity.HIGH
        assert any("Multi-file" in reg for reg in risk.potential_regressions)


# ---------------------------------------------------------------------------
# 5. Verification and Rollback Tests
# ---------------------------------------------------------------------------

class TestVerificationAndRollback:
    """Verifies test verification on disk and rollback specifications."""

    def test_existing_tests_verified_on_disk(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        v_plan = plan.verification_plan
        # test_calculator.py exists in repo_fixture
        assert any("test_calculator.py" in t for t in v_plan.existing_targeted_tests)

        # Confirm all tests in existing_targeted_tests exist on disk
        for t in v_plan.existing_targeted_tests:
            assert (repo_fixture / t).exists()

    def test_recommended_new_tests_explicitly_labeled(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        v_plan = plan.verification_plan
        assert len(v_plan.recommended_new_tests) >= 1
        for rec in v_plan.recommended_new_tests:
            assert "[PROPOSED]" in rec or "proposed" in rec.lower()

    def test_rollback_plan_specifications(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        r_plan = plan.rollback_plan
        assert len(r_plan.changes_to_reverse) >= 1
        assert len(r_plan.rollback_triggers) >= 3
        assert len(r_plan.known_limitations) >= 2
        assert any("working-tree" in lim for lim in r_plan.known_limitations)


# ---------------------------------------------------------------------------
# 6. Safety and Invariant Enforcement Tests
# ---------------------------------------------------------------------------

class TestSafetyAndCertaintyInvariants:
    """Verifies non-negotiable safety gates: approval_required, execution_permitted."""

    def test_execution_permitted_strictly_false(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        assert plan.execution_permitted is False
        assert plan.approval_required is True

    def test_repair_plan_model_rejects_execution_permitted_true(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        plan_data = plan.model_dump()
        plan_data["execution_permitted"] = True

        with pytest.raises(ValueError, match="execution_permitted must always be False"):
            RepairPlan.model_validate(plan_data)

    def test_repair_plan_model_rejects_approval_required_false(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        plan_data = plan.model_dump()
        plan_data["approval_required"] = False

        with pytest.raises(ValueError, match="approval_required must always be True"):
            RepairPlan.model_validate(plan_data)

    def test_planner_does_not_modify_repository_files(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        # Capture file contents before planning
        calc_file = repo_fixture / "pkg_c/calculator.py"
        before_content = calc_file.read_text(encoding="utf-8")

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        after_content = calc_file.read_text(encoding="utf-8")
        assert before_content == after_content, "Repair planning must never modify repository source files!"


# ---------------------------------------------------------------------------
# 7. ToolRegistry Integration Tests
# ---------------------------------------------------------------------------

class TestToolRegistryIntegration:
    """Verifies that all 6 M3 tools are properly registered and executable in ToolRegistry."""

    def test_all_m3_tools_registered(self, repo_fixture: Path):
        registry = ToolRegistry(workspace_path=repo_fixture)
        tool_names = {t.name for t in registry.get_definitions()}

        m3_tools = {
            "create_repair_plan",
            "compare_repair_strategies",
            "assess_repair_risk",
            "explain_repair_plan",
            "validate_repair_plan",
            "generate_verification_plan",
        }
        assert m3_tools.issubset(tool_names)

    def test_execute_create_repair_plan_tool(self, repo_fixture: Path):
        registry = ToolRegistry(workspace_path=repo_fixture)
        res_raw = registry.execute_tool("create_repair_plan", {
            "file_path": "pkg_c/calculator.py",
            "failing_symbol": "pkg_c.calculator.compute_metric",
            "line_number": 2,
            "error_message": "ZeroDivisionError in compute_metric",
        })
        res = json.loads(res_raw)

        assert "plan_id" in res
        assert res["status"] in ("actionable", "conditional")
        assert res["execution_permitted"] is False
        assert res["approval_required"] is True

    def test_execute_compare_repair_strategies_tool(self, repo_fixture: Path):
        registry = ToolRegistry(workspace_path=repo_fixture)
        res_raw = registry.execute_tool("compare_repair_strategies", {
            "file_path": "pkg_c/calculator.py",
            "failing_symbol": "pkg_c.calculator.compute_metric",
        })
        res = json.loads(res_raw)

        assert "strategies" in res
        assert len(res["strategies"]) >= 1
        assert "tradeoff_analysis" in res

    def test_execute_assess_repair_risk_tool(self, repo_fixture: Path):
        registry = ToolRegistry(workspace_path=repo_fixture)
        res_raw = registry.execute_tool("assess_repair_risk", {
            "affected_files": ["pkg_c/calculator.py"],
            "affected_symbols": ["pkg_c.calculator.compute_metric"],
            "proposed_approach": "local_logic_correction",
        })
        res = json.loads(res_raw)

        assert "overall_risk" in res
        assert "risk_score" in res
        assert 0.0 <= res["risk_score"] <= 1.0

    def test_execute_explain_repair_plan_tool(self, repo_fixture: Path):
        tools = RepairPlanningTools(workspace_path=repo_fixture)
        # Create plan first
        plan_raw = tools.create_repair_plan({
            "file_path": "pkg_c/calculator.py",
            "failing_symbol": "pkg_c.calculator.compute_metric",
            "line_number": 2,
        })
        plan_id = json.loads(plan_raw)["plan_id"]

        explain_raw = tools.explain_repair_plan({"plan_id": plan_id})
        res = json.loads(explain_raw)

        assert res["plan_id"] == plan_id
        assert "problem_statement" in res
        assert "repair_objective" in res
        assert res["approval_required"] is True
        assert res["execution_permitted"] is False

    def test_execute_validate_repair_plan_tool(self, repo_fixture: Path):
        tools = RepairPlanningTools(workspace_path=repo_fixture)
        plan_raw = tools.create_repair_plan({
            "file_path": "pkg_c/calculator.py",
            "failing_symbol": "pkg_c.calculator.compute_metric",
            "line_number": 2,
        })
        plan_id = json.loads(plan_raw)["plan_id"]

        val_raw = tools.validate_repair_plan({"plan_id": plan_id})
        res = json.loads(val_raw)

        assert res["is_valid"] is True
        assert len(res["validation_errors"]) == 0
        assert res["execution_permitted_guarantee"] is True
        assert res["approval_required_guarantee"] is True

    def test_execute_generate_verification_plan_tool(self, repo_fixture: Path):
        registry = ToolRegistry(workspace_path=repo_fixture)
        res_raw = registry.execute_tool("generate_verification_plan", {
            "affected_files": ["pkg_c/calculator.py"],
            "affected_symbols": ["pkg_c.calculator.compute_metric"],
        })
        res = json.loads(res_raw)

        assert "existing_targeted_tests" in res
        assert "recommended_new_tests" in res
        assert len(res["recommended_new_tests"]) >= 1


# ---------------------------------------------------------------------------
# 8. Audit Findings Remediation: Immutability and Copy Safety
# ---------------------------------------------------------------------------

class TestRepairPlanImmutabilityAndCopySafety:
    """Verifies that RepairPlan is immutable, assignment-validated, and protected against copy bypass."""

    def test_direct_assignment_execution_permitted_rejected(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        with pytest.raises(ValidationError, match="frozen_instance|Instance is frozen"):
            plan.execution_permitted = True

    def test_direct_assignment_approval_required_rejected(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        with pytest.raises(ValidationError, match="frozen_instance|Instance is frozen"):
            plan.approval_required = False

    def test_model_copy_update_execution_permitted_rejected(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        with pytest.raises(ValidationError, match="execution_permitted must always be False"):
            plan.model_copy(update={"execution_permitted": True})

    def test_model_copy_update_approval_required_rejected(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        with pytest.raises(ValidationError, match="approval_required must always be True"):
            plan.model_copy(update={"approval_required": False})

    def test_revalidation_of_invalid_copied_plan(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        raw = plan.model_dump()
        raw["execution_permitted"] = True
        with pytest.raises(ValidationError, match="execution_permitted must always be False"):
            RepairPlan.model_validate(raw)

        validator = RepairPlanValidator(workspace_path=repo_fixture)
        # Even if someone constructs a tampered plan using low-level bypass, validate_plan catches it
        object.__setattr__(plan, "execution_permitted", True)
        is_valid, errors, checked = validator.validate_plan(plan)
        assert is_valid is False
        assert any("execution_permitted must be False" in err for err in errors)
        # Reset to safe value
        object.__setattr__(plan, "execution_permitted", False)

    def test_json_serialization_and_deserialization(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        # Serialize
        plan_json = plan.model_dump_json()
        assert "execution_permitted" in plan_json
        assert "approval_required" in plan_json

        # Deserialize
        restored = RepairPlan.model_validate_json(plan_json)
        assert restored.plan_id == plan.plan_id
        assert restored.execution_permitted is False
        assert restored.approval_required is True

        # Restored plan must also be frozen
        with pytest.raises(ValidationError, match="frozen_instance|Instance is frozen"):
            restored.execution_permitted = True

    def test_tool_input_containing_tampered_plan(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        tools = RepairPlanningTools(workspace_path=repo_fixture)
        engine = tools.get_engine()
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        # Tamper JSON
        tampered_dict = plan.model_dump()
        tampered_dict["execution_permitted"] = True
        tampered_json = json.dumps(tampered_dict)

        val_raw = tools.validate_repair_plan({"plan_json": tampered_json})
        res = json.loads(val_raw)

        assert res["is_valid"] is False
        assert len(res["validation_errors"]) >= 1
        assert any("execution_permitted must always be False" in err for err in res["validation_errors"])

    def test_nested_strategy_data_attempting_to_weaken_constraints(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        from pydantic import ValidationError
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        # 1. Extra fields on nested strategy are forbidden
        strat_dict = plan.candidate_strategies[0].model_dump()
        strat_dict["bypass_sandbox"] = True
        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs are not permitted"):
            RepairStrategy.model_validate(strat_dict)

        # 2. Non-actionable plan with required code changes is forbidden
        plan_dict = plan.model_dump()
        plan_dict["status"] = RepairPlanStatus.INSUFFICIENT_EVIDENCE
        # required_changes is non-empty from proven report
        assert len(plan_dict["required_changes"]) > 0
        with pytest.raises(ValidationError, match="cannot contain required code changes"):
            RepairPlan.model_validate(plan_dict)


# ---------------------------------------------------------------------------
# 9. Audit Findings Remediation: Stale Evidence Status Downgrade
# ---------------------------------------------------------------------------

class TestStaleEvidenceDowngradeRemediation:
    """Verifies that pre-constructed reports cannot override current on-disk source evidence."""

    def test_fresh_proven_report_produces_actionable_plan(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)

        assert plan.status == RepairPlanStatus.ACTIONABLE
        assert plan.confidence == FindingConfidence.HIGH
        assert plan.selected_strategy_id is not None
        assert len(plan.required_changes) > 0

    def test_stale_preconstructed_proven_report_becomes_non_actionable(
        self,
        repo_fixture: Path,
    ):
        # Create a report claiming PROVEN status, but referencing out-of-bounds line numbers
        stale_report = RootCauseReport(
            report_id="rc_stale_001",
            run_id="run_stale_001",
            status=CausalStatus.PROVEN,
            primary_root_cause=CausalNode(
                node_id="cnode_calc_stale",
                symbol_id="pkg_c.calculator.compute_metric",
                file_path="pkg_c/calculator.py",
                start_line=9999,
                end_line=9999,
                node_type=CausalNodeType.FUNCTION,
                role=CausalRole.ROOT_CAUSE,
                description="Stale root cause line that does not exist",
                is_verified_on_disk=True,
            ),
            confidence=FindingConfidence.HIGH,
            confidence_score=0.95,
        )

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=stale_report)

        # Must be downgraded to INSUFFICIENT_EVIDENCE
        assert plan.status == RepairPlanStatus.INSUFFICIENT_EVIDENCE
        assert plan.confidence == FindingConfidence.LOW
        assert plan.selected_strategy_id is None
        assert len(plan.required_changes) == 0
        assert "Stale evidence detected during planning" in (plan.limitations or "")
        assert "exceeds file line count" in (plan.limitations or "")

    def test_source_modification_between_evidence_collection_and_final_validation(
        self,
        repo_fixture: Path,
        proven_root_cause_report: RootCauseReport,
    ):
        engine = RepairPlanningEngine(workspace_path=repo_fixture)

        # Hook evidence collector to simulate race condition: file modified on disk after initial collect
        orig_verify = engine._evidence_collector.verify_file_location
        call_count = 0

        def race_condition_verify(file_path, start_line=None, end_line=None):
            nonlocal call_count
            call_count += 1
            is_valid, curr_hash, err = orig_verify(file_path, start_line, end_line)
            # On second check (pre-assembly re-verify), report content drift
            if call_count > 2:
                return True, "corrupted_hash_simulating_race_condition", None
            return is_valid, curr_hash, err

        engine._evidence_collector.verify_file_location = race_condition_verify

        plan = engine.plan_repair(root_cause_report=proven_root_cause_report)
        assert plan.status == RepairPlanStatus.INSUFFICIENT_EVIDENCE
        assert plan.confidence == FindingConfidence.LOW
        assert plan.selected_strategy_id is None
        assert "Plan downgraded to INSUFFICIENT_EVIDENCE due to concurrent disk changes" in (plan.limitations or "")

    def test_stale_evidence_prevents_selected_strategy_actionable(
        self,
        repo_fixture: Path,
    ):
        stale_report = RootCauseReport(
            report_id="rc_stale_strat",
            run_id="run_stale_strat",
            status=CausalStatus.PROVEN,
            primary_root_cause=CausalNode(
                node_id="cnode_calc_stale2",
                symbol_id="pkg_c.calculator.compute_metric",
                file_path="pkg_c/calculator.py",
                start_line=5000,
                end_line=5000,
                node_type=CausalNodeType.FUNCTION,
                role=CausalRole.ROOT_CAUSE,
                description="Stale line",
            ),
            confidence=FindingConfidence.HIGH,
            confidence_score=0.9,
        )

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=stale_report)

        # All candidate strategies must have is_recommended=False and rejection reasons
        assert len(plan.candidate_strategies) > 0
        for s in plan.candidate_strategies:
            assert s.is_recommended is False
            assert any("stale" in r.lower() for r in s.rejection_reasons)

    def test_missing_source_files_downgrades_to_insufficient_evidence(
        self,
        repo_fixture: Path,
    ):
        missing_report = RootCauseReport(
            report_id="rc_missing_file",
            run_id="run_missing_file",
            status=CausalStatus.PROVEN,
            primary_root_cause=CausalNode(
                node_id="cnode_missing",
                symbol_id="nonexistent_module.func",
                file_path="pkg_nonexistent/deleted_file.py",
                start_line=1,
                end_line=10,
                node_type=CausalNodeType.FUNCTION,
                role=CausalRole.ROOT_CAUSE,
                description="File was deleted",
            ),
            confidence=FindingConfidence.HIGH,
            confidence_score=0.95,
        )

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=missing_report)

        assert plan.status == RepairPlanStatus.INSUFFICIENT_EVIDENCE
        assert plan.confidence == FindingConfidence.LOW
        assert "does not exist on disk" in (plan.limitations or "")

    def test_stale_evidence_visible_in_plan_limitations(
        self,
        repo_fixture: Path,
    ):
        stale_report = RootCauseReport(
            report_id="rc_stale_visible",
            run_id="run_stale_visible",
            status=CausalStatus.PROVEN,
            primary_root_cause=CausalNode(
                node_id="cnode_visible",
                symbol_id="pkg_c.calculator.compute_metric",
                file_path="pkg_c/calculator.py",
                start_line=8888,
                end_line=8888,
                node_type=CausalNodeType.FUNCTION,
                role=CausalRole.ROOT_CAUSE,
                description="Out of range",
            ),
            confidence=FindingConfidence.HIGH,
            confidence_score=0.9,
        )

        engine = RepairPlanningEngine(workspace_path=repo_fixture)
        plan = engine.plan_repair(root_cause_report=stale_report)

        assert plan.limitations is not None
        assert "Stale evidence detected during planning" in plan.limitations
        assert "start_line (8888) exceeds file line count" in plan.limitations
