"""Unit tests for Repair Planner Reliability Milestone.

Verifies:
1. A failing test correctly identifies an implementation file when evidence supports it.
2. An intake restricted to tests cannot silently expand to implementation files.
3. A syntactically valid diff with incorrect hunk context is rejected.
4. A path outside the permitted scope is rejected.
5. A valid minimal implementation patch remains accepted.
6. Malformed provider output and exhausted retries fail closed.
7. Ambiguous root-cause evidence produces a blocker rather than a guessed fix.
8. Candidate validation never modifies the original target or its Git history.
9. No cloud or alternate-provider fallback occurs on local Ollama failure.
"""

import hashlib
import json
from pathlib import Path
import subprocess
from typing import List, Tuple
import pytest

from forge.analysis.evidence import create_evidence
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.repair.contracts import (
    RepairBudget,
    is_test_path,
    validate_patch_applicability,
)
from forge.repair.models import (
    RepairPlan,
    RepairPlanStatus,
)
from forge.repair.planner import (
    RepairPlanner,
    resolve_candidate_targets,
)
from forge.schemas import (
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)


def _init_test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Helper to initialize a real disposable git repository fixture."""
    repo = tmp_path / "target_repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=repo, check=True, capture_output=True)

    math_file = repo / "math_ops.py"
    math_file.write_text(
        '"""Mathematical operations module."""\n\n'
        "def safe_divide(a: float, b: float) -> float:\n"
        "    return a / b\n"
    )

    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_math_ops.py"
    test_file.write_text(
        "import unittest\n"
        "from math_ops import safe_divide\n\n"
        "class TestMathOps(unittest.TestCase):\n"
        "    def test_safe_divide_zero(self):\n"
        "        with self.assertRaises(ZeroDivisionError):\n"
        "            safe_divide(10, 0)\n"
    )

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return repo, head_sha


def _make_mock_json_response(
    target_file: str,
    diff_text: str,
    root_cause: str = "Root cause explanation",
    proposed_change: str = "Proposed change explanation",
) -> LLMResponse:
    payload = {
        "root_cause": root_cause,
        "proposed_change": proposed_change,
        "target_file": target_file,
        "expected_behavior": "Defect resolved without regressing callers",
        "regression_risks": ["None identified"],
        "verification_plan": "pytest tests/test_math_ops.py",
        "confidence": 0.95,
        "uncertainty_notes": None,
        "unified_diff": diff_text,
    }
    return LLMResponse(
        content=json.dumps(payload),
        finish_reason="stop",
        prompt_tokens=200,
        completion_tokens=100,
        total_tokens=300,
        model="forge-fake-model",
    )


# ---------------------------------------------------------------------------
# Test 1: Failing test correctly identifies implementation file via evidence
# ---------------------------------------------------------------------------
def test_failing_test_identifies_impl_file_with_evidence(tmp_path: Path):
    """Planner resolves the implementation target when evidence stack trace supports it."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_div_zero",
        title="ZeroDivisionError in safe_divide",
        description="safe_divide raises unhandled ZeroDivisionError",
        affected_files=["tests/test_math_ops.py", "math_ops.py"],
        supporting_evidence_ids=["evi_repro_01"],
    )
    ev = create_evidence(
        run_id="run_01",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Failing test stack trace",
        content=(
            'Traceback (most recent call last):\n'
            '  File "/workspace/tests/test_math_ops.py", line 7, in test_safe_divide_zero\n'
            '    safe_divide(10, 0)\n'
            '  File "/workspace/math_ops.py", line 4, in safe_divide\n'
            'ZeroDivisionError: division by zero\n'
        ),
        finding_id="find_div_zero",
        evidence_id="evi_repro_01",
    )

    impls, tests = resolve_candidate_targets(repo, finding, [ev])
    assert "math_ops.py" in impls
    assert "tests/test_math_ops.py" in tests

    valid_diff = (
        "--- a/math_ops.py\n"
        "+++ b/math_ops.py\n"
        "@@ -3,2 +3,4 @@\n"
        " def safe_divide(a: float, b: float) -> float:\n"
        "+    if b == 0:\n"
        "+        raise ZeroDivisionError('division by zero')\n"
        "     return a / b\n"
    )
    mock_llm = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response(target_file="math_ops.py", diff_text=valid_diff)
    ])
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_div_zero",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.target_file == "math_ops.py"
    # Ensure failing test oracle was provided as read-only context
    assert len(mock_llm.call_history) >= 1
    sent_prompt = mock_llm.call_history[0].messages[1].content
    assert "READ-ONLY TEST ORACLE" in sent_prompt
    assert "tests/test_math_ops.py" in sent_prompt


# ---------------------------------------------------------------------------
# Test 2: Intake restricted to tests cannot silently expand to implementation files
# ---------------------------------------------------------------------------
def test_intake_restricted_to_tests_cannot_silently_expand(tmp_path: Path):
    """An intake whose scope is strictly restricted to tests must fail with scope restriction blocker."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_restricted",
        title="ZeroDivisionError test failure",
        description="Failing test reports defect",
        affected_files=["tests/test_math_ops.py"],  # ONLY test file declared
        supporting_evidence_ids=["evi_repro_02"],
    )
    ev = create_evidence(
        run_id="run_02",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Stack trace points to math_ops.py",
        content=(
            'Traceback (most recent call last):\n'
            '  File "/workspace/tests/test_math_ops.py", line 7, in test_safe_divide_zero\n'
            '    safe_divide(10, 0)\n'
            '  File "/workspace/math_ops.py", line 4, in safe_divide\n'
            'ZeroDivisionError: division by zero\n'
        ),
        finding_id="find_restricted",
        evidence_id="evi_repro_02",
    )

    mock_llm = FakeLLMProvider()
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_restricted",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert any("Scope restriction" in v for v in plan.validation.violations)
    assert any("math_ops.py" in v for v in plan.validation.violations)
    assert any("Modifying test files to mask defects is forbidden" in v for v in plan.validation.violations)
    # LLM was never called because scope restriction caught it preflight
    assert len(mock_llm.call_history) == 0


# ---------------------------------------------------------------------------
# Test 3: Syntactically valid diff with incorrect hunk context is rejected
# ---------------------------------------------------------------------------
def test_diff_with_incorrect_hunk_context_is_rejected(tmp_path: Path):
    """A diff matching standard unified diff regex but with wrong context lines is rejected."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_mismatch",
        title="Defect with hallucinated context",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_repro_03"],
    )
    ev = create_evidence(
        run_id="run_03",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Source excerpt",
        content="return a / b",
        finding_id="find_mismatch",
        evidence_id="evi_repro_03",
    )

    # Context lines do not match math_ops.py ("def non_existent_function" instead of real content)
    hallucinated_diff = (
        "--- a/math_ops.py\n"
        "+++ b/math_ops.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def non_existent_function():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    mock_llm = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response(target_file="math_ops.py", diff_text=hallucinated_diff)
    ])
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_mismatch",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert any("context" in v.lower() for v in plan.validation.violations)


# ---------------------------------------------------------------------------
# Test 4: Path outside permitted scope is rejected
# ---------------------------------------------------------------------------
def test_diff_outside_permitted_scope_is_rejected(tmp_path: Path):
    """A diff attempting to modify a file outside the authorized finding scope is rejected."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_scope_escape",
        title="Scope escape attempt",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_repro_04"],
    )
    ev = create_evidence(
        run_id="run_04",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Source excerpt",
        content="safe_divide",
        finding_id="find_scope_escape",
        evidence_id="evi_repro_04",
    )

    diff_targeting_other = (
        "--- a/tests/test_math_ops.py\n"
        "+++ b/tests/test_math_ops.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-import unittest\n"
        "+# bypassed\n"
    )
    mock_llm = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response(target_file="tests/test_math_ops.py", diff_text=diff_targeting_other)
    ])
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_scope_escape",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert any("scope is restricted" in v for v in plan.validation.violations)


# ---------------------------------------------------------------------------
# Test 5: Valid minimal implementation patch accepted
# ---------------------------------------------------------------------------
def test_valid_minimal_implementation_patch_accepted(tmp_path: Path):
    """A correct minimal patch with accurate context lines and syntax is VALIDATED."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_clean",
        title="Fix division by zero",
        description="Add guard in safe_divide",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_repro_05"],
    )
    ev = create_evidence(
        run_id="run_05",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Source excerpt",
        content="return a / b",
        finding_id="find_clean",
        evidence_id="evi_repro_05",
    )

    clean_diff = (
        "--- a/math_ops.py\n"
        "+++ b/math_ops.py\n"
        "@@ -3,2 +3,4 @@\n"
        " def safe_divide(a: float, b: float) -> float:\n"
        "+    if b == 0.0:\n"
        "+        raise ValueError('Cannot divide by zero')\n"
        "     return a / b\n"
    )
    mock_llm = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response(target_file="math_ops.py", diff_text=clean_diff)
    ])
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_clean",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.validation.is_valid is True
    assert plan.patch_candidate is not None
    assert plan.patch_candidate.unified_diff == clean_diff


# ---------------------------------------------------------------------------
# Test 6: Malformed provider output and exhausted retries fail closed
# ---------------------------------------------------------------------------
def test_malformed_provider_output_and_exhausted_retries_fail_closed(tmp_path: Path):
    """Unparseable model output across all retry attempts results in a fail-closed REJECTED plan."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_malformed",
        title="Unparseable LLM response",
        description="Model returns text instead of JSON",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_repro_06"],
    )
    ev = create_evidence(
        run_id="run_06",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="content",
        finding_id="find_malformed",
        evidence_id="evi_repro_06",
    )

    # 3 invalid responses (initial + 2 retries)
    garbage_responses = [
        LLMResponse(content="Sorry, I cannot help with that.", model="fake"),
        LLMResponse(content="```markdown\nHere is what you should do\n```", model="fake"),
        LLMResponse(content="{bad_json: True}", model="fake"),
    ]
    mock_llm = FakeLLMProvider(scripted_responses=garbage_responses)
    planner = RepairPlanner(llm_provider=mock_llm, max_retries=2)
    plan = planner.plan_repair(
        finding_id="find_malformed",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.patch_candidate is None
    assert any("not valid json" in v.lower() or "schema" in v.lower() for v in plan.validation.violations)
    assert len(mock_llm.call_history) == 3


# ---------------------------------------------------------------------------
# Test 7: Ambiguous root-cause evidence produces a blocker
# ---------------------------------------------------------------------------
def test_ambiguous_root_cause_evidence_produces_blocker(tmp_path: Path):
    """When multiple candidate implementation files exist and none is explicitly declared, block repair."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    # Add a second implementation file
    extra_file = repo / "extra_ops.py"
    extra_file.write_text("def extra(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add extra file"], cwd=repo, check=True, capture_output=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Finding mentions both symbols but declares no affected files
    finding = Finding(
        finding_id="find_ambiguous",
        title="Ambiguous defect in safe_divide and extra",
        description="Both safe_divide and extra symbols are referenced ambiguously",
        affected_files=[],  # No declared file
        supporting_evidence_ids=["evi_repro_07"],
    )
    ev = create_evidence(
        run_id="run_07",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Generic excerpt",
        content="General error without file location",
        finding_id="find_ambiguous",
        evidence_id="evi_repro_07",
    )

    mock_llm = FakeLLMProvider()
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_ambiguous",
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert any("Ambiguous root cause" in v for v in plan.validation.violations)
    assert len(mock_llm.call_history) == 0


# ---------------------------------------------------------------------------
# Test 8: Candidate validation never modifies the original target or its Git history
# ---------------------------------------------------------------------------
def test_candidate_validation_never_modifies_target_or_git_history(tmp_path: Path):
    """Repair planning and diff validation must leave the target repository byte-for-byte unchanged."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    math_file = repo / "math_ops.py"
    initial_math_bytes = math_file.read_bytes()
    initial_test_bytes = (repo / "tests" / "test_math_ops.py").read_bytes()

    finding = Finding(
        finding_id="find_immutable",
        title="Target repository immutability check",
        description="Ensure no side effects during planning",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_repro_08"],
    )
    ev = create_evidence(
        run_id="run_08",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_immutable",
        evidence_id="evi_repro_08",
    )

    valid_diff = (
        "--- a/math_ops.py\n"
        "+++ b/math_ops.py\n"
        "@@ -3,2 +3,4 @@\n"
        " def safe_divide(a: float, b: float) -> float:\n"
        "+    if b == 0:\n"
        "+        raise ZeroDivisionError()\n"
        "     return a / b\n"
    )
    mock_llm = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response(target_file="math_ops.py", diff_text=valid_diff)
    ])
    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_immutable",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.VALIDATED

    # Verify repository files and git commit are identical
    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert current_head == commit_sha
    assert math_file.read_bytes() == initial_math_bytes
    assert (repo / "tests" / "test_math_ops.py").read_bytes() == initial_test_bytes
    status_output = subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert status_output == ""


# ---------------------------------------------------------------------------
# Test 9: No cloud or alternate-provider fallback occurs on local Ollama failure
# ---------------------------------------------------------------------------
def test_no_cloud_or_alternate_provider_fallback_on_local_ollama_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When the local Ollama provider fails, FORGE must fail closed without silent cloud fallback."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    monkeypatch.setenv("FORGE_ALLOW_CLOUD_FALLBACK", "false")
    monkeypatch.setenv("FORGE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:54321/v1")  # Unreachable port

    finding = Finding(
        finding_id="find_ollama_fail",
        title="Ollama unreachable failure",
        description="Local Ollama is offline",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_repro_09"],
    )
    ev = create_evidence(
        run_id="run_09",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_ollama_fail",
        evidence_id="evi_repro_09",
    )

    # Instantiate planner without injecting mock LLM to test provider resolution and error handling
    planner = RepairPlanner(provider_name="ollama")
    plan = planner.plan_repair(
        finding_id="find_ollama_fail",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert any("provider generation failed" in v.lower() for v in plan.validation.violations)
