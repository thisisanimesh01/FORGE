"""Comprehensive Unit Tests for Phase 12 Milestone 1: Evidence-Driven Defect Diagnosis.

Verifies:
1. Input validation & contracts (budgets, signal requirements, extra fields forbidden).
2. Failure localization (tracebacks, error messages, test callee correlation, target symbols, change impact, security boundaries).
3. Hypothesis formulation & evaluation (deterministic categorization, contradiction detection, confidence scoring).
4. Defect confirmation invariants (model suspicion cannot confirm without grounded evidence, competing hypotheses, failure signals).
5. Freshness & stale evidence detection (sha256 mismatch detection on disk modification).
6. ForgeStore persistence (finding creation, evidence linking, SQLite retrieval).
7. Local Ollama safety (graceful fail-closed behavior, zero cloud fallback).
8. End-to-end diagnosis execution.
"""

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
import pytest

from forge.analysis.evidence import create_evidence
from forge.diagnosis.engine import EvidenceDrivenDiagnosisEngine
from forge.diagnosis.hypothesis import HypothesisEvaluator
from forge.diagnosis.localization import FailureLocalizer
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
    StaticFindingReport,
    TestFailureReport,
    MAX_DESCRIPTION_CHARS,
    MAX_DIFF_CHARS,
    MAX_ERROR_MESSAGE_CHARS,
    MAX_STACK_TRACE_CHARS,
)
from forge.llm.evidence_grounding import EvidenceGroundingEngine, verify_evidence_freshness
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.schemas import EvidenceType, FindingConfidence, FindingSeverity
from forge.storage.database import init_database
from forge.storage.store import ForgeStore


# ---------------------------------------------------------------------------
# Fixture: Synthetic Repository for Diagnosis
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_fixture(tmp_path: Path) -> Path:
    """Create a structured repository for testing localization and diagnosis."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Package structure: src/calc
    src_dir = repo / "src" / "calc"
    src_dir.mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "__init__.py").write_text("", encoding="utf-8")

    math_file = src_dir / "math_ops.py"
    math_file.write_text(
        "def divide(a: float, b: float) -> float:\n"
        "    # Potential zero division\n"
        "    return a / b\n"
        "\n"
        "def safe_divide(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        "        raise ValueError('b cannot be zero')\n"
        "    return a / b\n",
        encoding="utf-8",
    )

    data_file = src_dir / "data_handler.py"
    data_file.write_text(
        "def get_user_value(data: dict, key: str) -> str:\n"
        "    # Missing key lookup\n"
        "    return data[key]\n",
        encoding="utf-8",
    )

    # Tests directory
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")

    test_file = tests_dir / "test_math.py"
    test_file.write_text(
        "from src.calc.math_ops import divide\n"
        "\n"
        "def test_divide_zero():\n"
        "    divide(10.0, 0.0)\n",
        encoding="utf-8",
    )

    return repo


# ---------------------------------------------------------------------------
# 1. Input Validation & Contracts
# ---------------------------------------------------------------------------

def test_diagnosis_input_requires_at_least_one_signal():
    """Empty input without any signal must raise ValidationError."""
    with pytest.raises(ValueError, match="requires at least one diagnostic input signal"):
        DiagnosisInput()


def test_diagnosis_input_extra_fields_forbidden():
    """Extra attributes outside schema are strictly forbidden."""
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        DiagnosisInput(
            user_description="Valid description",
            unexpected_field="disallowed",  # type: ignore
        )


def test_diagnosis_input_character_budgets_enforced():
    """Inputs exceeding character budgets raise ValidationError."""
    oversized_desc = "x" * (MAX_DESCRIPTION_CHARS + 1)
    with pytest.raises(ValueError, match="user_description exceeds"):
        DiagnosisInput(user_description=oversized_desc)

    oversized_trace = "x" * (MAX_STACK_TRACE_CHARS + 1)
    with pytest.raises(ValueError, match="stack_trace exceeds"):
        DiagnosisInput(stack_trace=oversized_trace)

    oversized_diff = "x" * (MAX_DIFF_CHARS + 1)
    with pytest.raises(ValueError, match="diff exceeds"):
        DiagnosisInput(diff=oversized_diff)

    oversized_err = "x" * (MAX_ERROR_MESSAGE_CHARS + 1)
    with pytest.raises(ValueError, match="error_message exceeds"):
        DiagnosisInput(error_message=oversized_err)


def test_test_failure_report_truncation():
    """Test failure report truncates oversized error messages and traces."""
    long_msg = "e" * 6000
    report = TestFailureReport(
        test_name="test_foo",
        failure_message=long_msg,
    )
    assert len(report.failure_message) <= MAX_ERROR_MESSAGE_CHARS + 20
    assert "[TRUNCATED]" in report.failure_message


def test_candidate_location_line_ordering():
    """Candidate location rejects end_line < start_line."""
    with pytest.raises(ValueError, match="cannot be less than start_line"):
        CandidateLocation(
            file_path="src/calc/math_ops.py",
            start_line=10,
            end_line=5,
            source=LocationSource.STACK_TRACE,
            confidence_weight=0.8,
            reason="Traceback frame",
        )


# ---------------------------------------------------------------------------
# 2. Failure Localization
# ---------------------------------------------------------------------------

def test_localization_from_traceback(repo_fixture: Path):
    """Localizer extracts frames matching repository files."""
    localizer = FailureLocalizer(repo_fixture)
    traceback_str = (
        'Traceback (most recent call last):\n'
        '  File "tests/test_math.py", line 4, in test_divide_zero\n'
        '    divide(10.0, 0.0)\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    input_data = DiagnosisInput(stack_trace=traceback_str)
    locations = localizer.localize_candidates(input_data)

    assert len(locations) >= 2
    # The crash frame (math_ops.py line 3) should have highest priority
    crash_loc = locations[0]
    assert crash_loc.file_path == "src/calc/math_ops.py"
    assert crash_loc.start_line == 3
    assert crash_loc.is_verified_on_disk is True
    assert crash_loc.confidence_weight >= 0.85


def test_localization_from_error_message(repo_fixture: Path):
    """Localizer extracts file and line references from error messages."""
    localizer = FailureLocalizer(repo_fixture)
    error_msg = 'Error in File "src/calc/data_handler.py", line 3: KeyError: "user_id"'
    input_data = DiagnosisInput(error_message=error_msg)
    locations = localizer.localize_candidates(input_data)

    assert len(locations) >= 1
    assert locations[0].file_path == "src/calc/data_handler.py"
    assert locations[0].start_line == 3
    assert locations[0].is_verified_on_disk is True


def test_localization_from_target_symbols(repo_fixture: Path):
    """Localizer resolves explicit target symbol names via SymbolIndex."""
    index = IncrementalRepositoryIndex(repo_fixture)
    symbols, _, _, _ = index.build()

    localizer = FailureLocalizer(repo_fixture, symbol_index=symbols)
    input_data = DiagnosisInput(target_symbols=["divide"])
    locations = localizer.localize_candidates(input_data)

    assert len(locations) >= 1
    matched = [loc for loc in locations if "math_ops.py" in loc.file_path and loc.symbol_id == "divide"]
    assert len(matched) == 1
    assert matched[0].start_line == 1
    assert matched[0].is_verified_on_disk is True


def test_localization_security_rejections(repo_fixture: Path):
    """Localizer rejects traversal paths, outside paths, and protected files."""
    localizer = FailureLocalizer(repo_fixture)
    traversal_trace = (
        'Traceback (most recent call last):\n'
        '  File "../../etc/passwd", line 1, in secret\n'
        '    root:x:0:0\n'
        '  File ".git/config", line 5, in git_internal\n'
        '    bare = false\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
    )
    input_data = DiagnosisInput(stack_trace=traversal_trace)
    locations = localizer.localize_candidates(input_data)

    paths = [loc.file_path for loc in locations]
    assert "../../etc/passwd" not in paths
    assert ".git/config" not in paths
    assert "src/calc/math_ops.py" in paths


# ---------------------------------------------------------------------------
# 3. Hypothesis Formulation & Evaluation
# ---------------------------------------------------------------------------

def test_deterministic_category_inference(repo_fixture: Path):
    """Evaluator infers defect categories deterministically from symptoms."""
    evaluator = HypothesisEvaluator(repo_fixture)

    cat1 = evaluator.infer_category("ZeroDivisionError: division by zero", None)
    assert cat1 == DefectCategory.LOGIC_ERROR

    cat2 = evaluator.infer_category("KeyError: 'missing'", None)
    assert cat2 == DefectCategory.INCORRECT_DATA_HANDLING

    cat3 = evaluator.infer_category("ValueError: invalid literal", None)
    assert cat3 == DefectCategory.INPUT_VALIDATION_FAILURE

    cat4 = evaluator.infer_category("FileNotFoundError: no such file", None)
    assert cat4 == DefectCategory.RESOURCE_MANAGEMENT_ISSUE

    cat5 = evaluator.infer_category("ImportError: cannot import name", None)
    assert cat5 == DefectCategory.DEPENDENCY_INTEGRATION_DEFECT

    cat6 = evaluator.infer_category("TimeoutError: operation timed out", None)
    assert cat6 == DefectCategory.PERFORMANCE_DEFECT

    cat7 = evaluator.infer_category("Potential SQL Injection vulnerability", None)
    assert cat7 == DefectCategory.SECURITY_VULNERABILITY


def test_contradiction_detection_when_guard_exists(repo_fixture: Path):
    """Evaluator detects contradiction if hypothesis claims missing guard but guard exists on disk."""
    evaluator = HypothesisEvaluator(repo_fixture)
    grounding = EvidenceGroundingEngine(repo_fixture)

    # safe_divide in math_ops.py has lines 5-8: 'if b == 0: raise ValueError...'
    candidate = CandidateLocation(
        file_path="src/calc/math_ops.py",
        start_line=5,
        end_line=8,
        source=LocationSource.ERROR_MESSAGE,
        confidence_weight=0.8,
        reason="Claimed missing check",
        is_verified_on_disk=True,
    )
    input_data = DiagnosisInput(error_message="Missing check for b == 0 in safe_divide")
    hypotheses, contras = evaluator.evaluate(input_data, [candidate], grounding, run_id="run_test")

    assert len(hypotheses) >= 1
    # Contradiction should be detected because the disk content actually contains 'if b == 0:'
    assert len(contras) >= 1
    assert any("contradiction" in c.description.lower() or "guard" in c.description.lower() for c in contras)


def test_unverified_candidate_drops_confidence_score(repo_fixture: Path):
    """Candidate locations not verified on disk result in zero confidence score."""
    evaluator = HypothesisEvaluator(repo_fixture)
    grounding = EvidenceGroundingEngine(repo_fixture)

    unverified_candidate = CandidateLocation(
        file_path="src/calc/non_existent.py",
        start_line=10,
        end_line=15,
        source=LocationSource.USER_SPECIFIED,
        confidence_weight=0.5,
        reason="User guess",
        is_verified_on_disk=False,
    )
    input_data = DiagnosisInput(user_description="Something broke in non_existent.py")
    hypotheses, _ = evaluator.evaluate(input_data, [unverified_candidate], grounding, run_id="run_test")

    assert len(hypotheses) >= 1
    assert hypotheses[0].confidence_score == 0.0
    assert hypotheses[0].confidence == FindingConfidence.LOW


# ---------------------------------------------------------------------------
# 4. Defect Confirmation Invariants
# ---------------------------------------------------------------------------

def test_model_suspicion_without_failure_signal_cannot_confirm(repo_fixture: Path):
    """A diagnosis input with only a user description remains SUSPECTED, never CONFIRMED."""
    engine = EvidenceDrivenDiagnosisEngine(repo_fixture)
    input_data = DiagnosisInput(
        user_description="I think divide() might fail if someone passes zero."
    )
    result = engine.diagnose(input_data, run_id="run_001")

    assert result.status != DiagnosisStatus.CONFIRMED
    assert result.status in (DiagnosisStatus.SUSPECTED, DiagnosisStatus.UNRESOLVED)


def test_verified_location_and_crash_signal_can_confirm(repo_fixture: Path):
    """Concrete failure signal with verified location, grounded evidence, and no contradictions achieves CONFIRMED."""
    engine = EvidenceDrivenDiagnosisEngine(repo_fixture)
    trace = (
        'Traceback (most recent call last):\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    input_data = DiagnosisInput(
        stack_trace=trace,
        error_message="ZeroDivisionError: division by zero",
    )
    result = engine.diagnose(input_data, run_id="run_002")

    assert result.status == DiagnosisStatus.CONFIRMED
    assert result.defect_category == DefectCategory.LOGIC_ERROR
    assert result.confidence == FindingConfidence.HIGH
    assert result.confidence_score >= 0.75
    assert len(result.supporting_evidence_ids) >= 1
    assert len(result.contradicting_evidence_ids) == 0


def test_competing_hypotheses_prevent_confirmation(repo_fixture: Path):
    """If competing plausible hypotheses exist, diagnosis remains SUSPECTED."""
    engine = EvidenceDrivenDiagnosisEngine(repo_fixture)

    # Construct two valid candidates with high weights
    loc1 = CandidateLocation(
        file_path="src/calc/math_ops.py",
        start_line=1,
        end_line=3,
        source=LocationSource.USER_SPECIFIED,
        confidence_weight=0.85,
        reason="First suspect",
        is_verified_on_disk=True,
    )
    loc2 = CandidateLocation(
        file_path="src/calc/data_handler.py",
        start_line=1,
        end_line=3,
        source=LocationSource.USER_SPECIFIED,
        confidence_weight=0.85,
        reason="Second suspect",
        is_verified_on_disk=True,
    )

    with patch.object(engine.localizer, "localize_candidates", return_value=[loc1, loc2]):
        input_data = DiagnosisInput(
            user_description="Either math_ops or data_handler has a logic bug",
            error_message="Runtime error observed",
        )
        result = engine.diagnose(input_data, run_id="run_competing")

        # Since both hypotheses have close scores and competing explanations, it must not confirm
        assert result.status != DiagnosisStatus.CONFIRMED
        assert result.status in (DiagnosisStatus.SUSPECTED, DiagnosisStatus.UNRESOLVED)


# ---------------------------------------------------------------------------
# 5. Stale Evidence Detection
# ---------------------------------------------------------------------------

def test_verify_evidence_freshness(repo_fixture: Path):
    """Modified file on disk invalidates evidence freshness."""
    grounding = EvidenceGroundingEngine(repo_fixture)
    evidence = grounding.create_source_evidence(
        file_path="src/calc/math_ops.py",
        start_line=1,
        end_line=3,
        description="divide function excerpt",
    )

    # Immediately check freshness: should be fresh
    is_fresh, reason = verify_evidence_freshness(repo_fixture, evidence)
    assert is_fresh is True
    assert reason is None

    # Modify the file on disk
    math_file = repo_fixture / "src" / "calc" / "math_ops.py"
    original_text = math_file.read_text(encoding="utf-8")
    math_file.write_text(original_text + "\n# Modified line\n", encoding="utf-8")

    # Re-check freshness: should detect modification
    is_fresh_after, reason_after = verify_evidence_freshness(repo_fixture, evidence)
    # The sha256 of the 3-line slice might still match if lines 1-3 didn't change,
    # so let's modify lines 1-3 specifically
    math_file.write_text("def divide(x: float, y: float) -> float:\n    return x / y\n", encoding="utf-8")
    is_fresh_mod, reason_mod = verify_evidence_freshness(repo_fixture, evidence)
    assert is_fresh_mod is False
    assert "has changed" in reason_mod


# ---------------------------------------------------------------------------
# 6. ForgeStore Persistence Integration
# ---------------------------------------------------------------------------

def test_store_diagnosis_finding_persistence(repo_fixture: Path, tmp_path: Path):
    """Engine creates and persists Finding and Evidence records in ForgeStore."""
    db_path = tmp_path / "forge_test.db"
    init_database(db_path)
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target="local_repo", run_id="run_persist_01")

    engine = EvidenceDrivenDiagnosisEngine(repo_fixture, store=store)
    trace = (
        'Traceback (most recent call last):\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    input_data = DiagnosisInput(
        stack_trace=trace,
        error_message="ZeroDivisionError: division by zero",
    )
    result = engine.diagnose(input_data, run_id=run_id, persist_finding=True)

    assert result.associated_finding_id is not None
    # Verify in DB
    finding = store.get_finding(result.associated_finding_id)
    assert finding is not None
    assert finding.title == "ZeroDivisionError: division by zero"
    assert finding.finding_id == result.associated_finding_id

    # Verify attached evidence in DB
    evidence_list = store.list_evidence_for_finding(result.associated_finding_id)
    assert len(evidence_list) >= 1


# ---------------------------------------------------------------------------
# 7. Local Ollama Safety
# ---------------------------------------------------------------------------

def test_ollama_failure_graceful_fallback(repo_fixture: Path):
    """If Ollama LLM provider fails or times out, engine gracefully completes with heuristic diagnosis."""
    mock_provider = MagicMock()
    mock_provider.chat.side_effect = RuntimeError("Ollama connection refused at localhost:11434")

    engine = EvidenceDrivenDiagnosisEngine(repo_fixture, llm_provider=mock_provider)
    trace = (
        'Traceback (most recent call last):\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    input_data = DiagnosisInput(
        stack_trace=trace,
        error_message="ZeroDivisionError: division by zero",
    )

    # Must complete without unhandled exception and without calling external services
    result = engine.diagnose(input_data, run_id="run_fallback")
    assert result is not None
    assert result.defect_category == DefectCategory.LOGIC_ERROR
    assert result.status in (DiagnosisStatus.CONFIRMED, DiagnosisStatus.SUSPECTED)


# ---------------------------------------------------------------------------
# 8. End-to-End Test Failure Localization
# ---------------------------------------------------------------------------

def test_end_to_end_test_failure_diagnosis(repo_fixture: Path):
    """End-to-end diagnosis with TestFailureReport accurately identifies production defect."""
    engine = EvidenceDrivenDiagnosisEngine(repo_fixture)
    failure_report = TestFailureReport(
        test_name="tests/test_math.py::test_divide_zero",
        test_file="tests/test_math.py",
        failure_message="ZeroDivisionError: division by zero",
        stack_trace=(
            'Traceback (most recent call last):\n'
            '  File "tests/test_math.py", line 4, in test_divide_zero\n'
            '    divide(10.0, 0.0)\n'
            '  File "src/calc/math_ops.py", line 3, in divide\n'
            '    return a / b\n'
            'ZeroDivisionError: division by zero\n'
        ),
        line_number=4,
    )
    input_data = DiagnosisInput(test_failure=failure_report)
    result = engine.diagnose(input_data, run_id="run_e2e")

    assert result.status == DiagnosisStatus.CONFIRMED
    assert len(result.root_cause_locations) >= 1
    primary_loc = result.root_cause_locations[0]
    assert primary_loc.file_path == "src/calc/math_ops.py"
    assert primary_loc.start_line == 3
    assert result.defect_category == DefectCategory.LOGIC_ERROR
    assert "divide" in result.failing_behavior.lower() or "zerodivision" in result.failing_behavior.lower()


# ---------------------------------------------------------------------------
# 9. Extended Localization, Severity, and LLM Enrichment Tests
# ---------------------------------------------------------------------------

def test_static_finding_localization(repo_fixture: Path):
    """Localizer extracts location from static finding report."""
    localizer = FailureLocalizer(repo_fixture)
    static_rep = StaticFindingReport(
        rule_id="SEC001",
        file_path="src/calc/data_handler.py",
        line_number=2,
        message="Direct dictionary lookup without key validation",
        severity=FindingSeverity.HIGH,
    )
    input_data = DiagnosisInput(static_finding=static_rep)
    locations = localizer.localize_candidates(input_data)

    assert len(locations) >= 1
    loc = locations[0]
    assert loc.file_path == "src/calc/data_handler.py"
    assert loc.start_line == 2
    assert loc.source == LocationSource.STATIC_ANALYSIS


def test_target_files_localization(repo_fixture: Path):
    """Localizer accepts explicit target files."""
    localizer = FailureLocalizer(repo_fixture)
    input_data = DiagnosisInput(target_files=["src/calc/math_ops.py"])
    locations = localizer.localize_candidates(input_data)

    assert len(locations) >= 1
    loc = locations[0]
    assert loc.file_path == "src/calc/math_ops.py"
    assert loc.source == LocationSource.USER_SPECIFIED


def test_severity_assignment_mapping(repo_fixture: Path):
    """Engine assigns severity according to defect category impact."""
    engine = EvidenceDrivenDiagnosisEngine(repo_fixture)

    # Security vulnerability -> CRITICAL
    sev_sec = engine._determine_severity(DefectCategory.SECURITY_VULNERABILITY, DiagnosisInput(user_description="SQL injection"), [])
    assert sev_sec == FindingSeverity.CRITICAL

    # Resource management -> HIGH
    sev_res = engine._determine_severity(DefectCategory.RESOURCE_MANAGEMENT_ISSUE, DiagnosisInput(user_description="unclosed file descriptor"), [])
    assert sev_res == FindingSeverity.HIGH

    # Performance defect -> LOW
    sev_perf = engine._determine_severity(DefectCategory.PERFORMANCE_DEFECT, DiagnosisInput(user_description="slow query"), [])
    assert sev_perf == FindingSeverity.LOW


def test_llm_hypothesis_enrichment_structured(repo_fixture: Path):
    """Local Ollama provider enriches hypothesis descriptions with reasoning."""
    fake_provider = FakeLLMProvider(
        scripted_responses=[
            LLMResponse(
                content='{"src/calc/math_ops.py": "Missing zero divisor guard before division operation produces ZeroDivisionError."}',
                finish_reason="stop",
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            )
        ]
    )

    engine = EvidenceDrivenDiagnosisEngine(repo_fixture, provider=fake_provider)
    trace = (
        'Traceback (most recent call last):\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    input_data = DiagnosisInput(stack_trace=trace)
    result = engine.diagnose(input_data, run_id="run_enriched")

    assert result.status == DiagnosisStatus.CONFIRMED
    assert len(result.hypotheses) >= 1
    top_h = result.hypotheses[0]
    assert "LLM Analysis" in top_h.description
    assert "zero divisor guard" in top_h.description


def test_llm_malformed_response_handled_gracefully(repo_fixture: Path):
    """Malformed LLM response does not crash the diagnosis pipeline."""
    fake_provider = FakeLLMProvider(
        scripted_responses=[
            LLMResponse(
                content='Not valid JSON output from model! Just chatter...',
                finish_reason="stop",
                prompt_tokens=50,
                completion_tokens=10,
                total_tokens=60,
            )
        ]
    )

    engine = EvidenceDrivenDiagnosisEngine(repo_fixture, provider=fake_provider)
    trace = (
        'Traceback (most recent call last):\n'
        '  File "src/calc/math_ops.py", line 3, in divide\n'
        '    return a / b\n'
        'ZeroDivisionError: division by zero\n'
    )
    input_data = DiagnosisInput(stack_trace=trace)
    result = engine.diagnose(input_data, run_id="run_malformed")

    assert result is not None
    assert result.status == DiagnosisStatus.CONFIRMED
    assert result.defect_category == DefectCategory.LOGIC_ERROR
