"""Unit tests for Patch Generation Reliability Milestone.

Verifies:
1. Valid minimal unified diff against pinned base is accepted.
2. Context-only hunk (no additions or deletions) is rejected.
3. Unchanged replacement/no-op patch is rejected.
4. Valid-looking code snippet without diff markers is rejected.
5. Valid patch with incorrect hunk context is rejected.
6. Patch targeting an unauthorized file is rejected.
7. Bounded retry succeeds when a fake provider corrects a context-only diff to a valid diff.
8. Exhausted retries fail closed without creating a valid candidate.
9. Candidate must actually change the pinned source and resolve defect.
10. Original fixture and Git history remain unchanged.
11. Local provider failure cannot trigger cloud or mock fallback.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import List, Tuple
import pytest

from forge.analysis.evidence import create_evidence
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairSafetyContract,
    parse_and_validate_unified_diff,
    validate_patch_applicability,
)
from forge.repair.models import (
    RepairPlan,
    RepairPlanStatus,
)
from forge.repair.planner import (
    RepairPlanner,
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

    math_file = repo / "calculator.py"
    math_file.write_text(
        "class Calculator:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "    def subtract(self, a, b):\n"
        "        return a - b\n"
        "    def multiply(self, a, b):\n"
        "        return a * b\n"
        "    def divide(self, a, b):\n"
        "        return a / b\n"
    )

    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_calculator.py"
    test_file.write_text(
        "import pytest\n"
        "from calculator import Calculator\n\n"
        "def test_divide_zero():\n"
        "    calc = Calculator()\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        calc.divide(10, 0)\n"
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
        "verification_plan": "pytest tests/test_calculator.py",
        "confidence": 0.95,
        "uncertainty_notes": None,
        "unified_diff": diff_text,
    }
    return LLMResponse(
        content=json.dumps(payload),
        finish_reason="stop",
        prompt_tokens=200,
        completion_tokens=100,
    )


def _make_finding_and_evidence(head_sha: str) -> Tuple[Finding, List]:
    finding = Finding(
        finding_id="find_div_zero",
        title="Division by zero in Calculator.divide",
        description="divide does not validate that divisor is non-zero, raising raw ZeroDivisionError",
        severity=FindingSeverity.HIGH,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=["calculator.py"],
    )
    evidence = [
        create_evidence(
            run_id="run_test",
            finding_id="find_div_zero",
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="test_divide_zero reproduces ZeroDivisionError",
            content='File "calculator.py", line 10, in divide\n    return a / b\nZeroDivisionError: division by zero',
            details={"status": "reproduced", "commit": head_sha},
        )
    ]
    return finding, evidence


# =============================================================================
# Test 1: Valid minimal unified diff against pinned base is accepted
# =============================================================================
def test_valid_minimal_unified_diff_accepted(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    valid_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,4 @@\n"
        "     def divide(self, a, b):\n"
        "+        if b == 0:\n"
        '+            raise ZeroDivisionError("division by zero")\n'
        "         return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", valid_diff)])
    planner = RepairPlanner(llm_provider=provider)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.validation is not None
    assert plan.validation.is_valid is True
    assert plan.validation.violations == []
    assert plan.patch_candidate is not None
    assert plan.patch_candidate.added_lines == 2
    assert plan.patch_candidate.removed_lines == 0
    assert plan.patch_candidate.target_files == ["calculator.py"]


# =============================================================================
# Test 2: Context-only hunk (no additions or deletions) is rejected
# =============================================================================
def test_context_only_hunk_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Diff containing only context lines (spaces), no '+' or '-' lines
    context_only_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,2 @@\n"
        "     def divide(self, a, b):\n"
        "         return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", context_only_diff)])
    planner = RepairPlanner(llm_provider=provider, max_retries=0)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("no additions or deletions" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 3: Unchanged replacement/no-op patch is rejected
# =============================================================================
def test_unchanged_replacement_patch_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Diff that removes and adds the exact same line (no-op replacement)
    noop_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -9,1 +9,1 @@\n"
        "-        return a / b\n"
        "+        return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", noop_diff)])
    planner = RepairPlanner(llm_provider=provider, max_retries=0)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("unchanged replacement" in v.lower() or "no net changes" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 4: Valid-looking code snippet without diff markers is rejected
# =============================================================================
def test_code_snippet_without_diff_markers_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Valid Python code snippet instead of unified diff
    raw_code = (
        "def divide(self, a, b):\n"
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")\n'
        "    return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", raw_code)])
    planner = RepairPlanner(llm_provider=provider, max_retries=0)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("malformed diff" in v.lower() or "no @@ hunk headers" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 5: Valid patch with incorrect hunk context is rejected
# =============================================================================
def test_valid_patch_with_incorrect_hunk_context_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Valid diff syntax, but context lines do not match actual calculator.py content
    mismatched_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -100,2 +100,4 @@\n"
        "     def totally_nonexistent_function():\n"
        "+        if b == 0:\n"
        '+            raise ZeroDivisionError("division by zero")\n'
        "         return None\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", mismatched_diff)])
    planner = RepairPlanner(llm_provider=provider, max_retries=0)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("context mismatch" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 6: Patch targeting an unauthorized file is rejected
# =============================================================================
def test_patch_targeting_unauthorized_file_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Patch attempting to modify test file or unauthorized file
    unauthorized_diff = (
        "--- a/tests/test_calculator.py\n"
        "+++ b/tests/test_calculator.py\n"
        "@@ -4,3 +4,3 @@\n"
        "-def test_divide_zero():\n"
        "+def test_divide_zero_modified():\n"
        "     calc = Calculator()\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("tests/test_calculator.py", unauthorized_diff)])
    planner = RepairPlanner(llm_provider=provider, max_retries=0)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("scope" in v.lower() or "allowed scope" in v.lower() or "restricted" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 7: Bounded retry succeeds when a fake provider corrects a context-only diff
# =============================================================================
def test_bounded_retry_succeeds_when_context_only_diff_corrected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Attempt 1: Context-only diff with zero additions/removals
    attempt1_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,2 @@\n"
        "     def divide(self, a, b):\n"
        "         return a / b\n"
    )

    # Attempt 2: Corrected diff with explicit '+' addition markers
    attempt2_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,4 @@\n"
        "     def divide(self, a, b):\n"
        "+        if b == 0:\n"
        '+            raise ZeroDivisionError("division by zero")\n'
        "         return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response("calculator.py", attempt1_diff),
        _make_mock_json_response("calculator.py", attempt2_diff),
    ])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.validation is not None
    assert plan.validation.is_valid is True
    assert plan.patch_candidate is not None
    assert plan.patch_candidate.added_lines == 2


# =============================================================================
# Test 8: Exhausted retries fail closed without creating a valid candidate
# =============================================================================
def test_exhausted_retries_fail_closed(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    bad_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,2 @@\n"
        "     def divide(self, a, b):\n"
        "         return a / b\n"
    )

    # Provider keeps returning the empty diff across all attempts
    provider = FakeLLMProvider(scripted_responses=[
        _make_mock_json_response("calculator.py", bad_diff),
        _make_mock_json_response("calculator.py", bad_diff),
        _make_mock_json_response("calculator.py", bad_diff),
    ])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("no additions or deletions" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 9: Candidate must actually change the pinned source and resolve defect
# =============================================================================
def test_candidate_actually_changes_pinned_source_and_resolves_defect(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    valid_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,4 @@\n"
        "     def divide(self, a, b):\n"
        "+        if b == 0:\n"
        '+            raise ZeroDivisionError("division by zero")\n'
        "         return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", valid_diff)])
    planner = RepairPlanner(llm_provider=provider)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.patch_candidate is not None

    # Apply candidate patch to a scratch copy and verify test passes
    scratch_dir = tmp_path / "scratch_work"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-R", f"{repo}/.", str(scratch_dir)], check=True)

    # Dry-run patch check
    is_applicable, reasons = validate_patch_applicability(scratch_dir, plan.patch_candidate.unified_diff, head_sha)
    assert is_applicable is True
    assert reasons == []

    # Apply diff to calculator.py in scratch workspace
    calc_path = scratch_dir / "calculator.py"
    calc_content = calc_path.read_text()
    assert "if b == 0:" not in calc_content

    # Manually splice or use patch command in scratch dir
    p = subprocess.run(["patch", "-p1"], input=plan.patch_candidate.unified_diff, cwd=scratch_dir, text=True, capture_output=True)
    assert p.returncode == 0

    new_calc_content = calc_path.read_text()
    assert "if b == 0:" in new_calc_content

    # Run tests in scratch directory to prove defect is resolved
    env = {**os.environ, "PYTHONPATH": str(scratch_dir)}
    res = subprocess.run(["pytest", "tests/test_calculator.py"], cwd=scratch_dir, capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "1 passed" in res.stdout


# =============================================================================
# Test 10: Original fixture and Git history remain unchanged
# =============================================================================
def test_original_fixture_and_git_history_remain_unchanged(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Record baseline state of repository fixture
    calc_file = repo / "calculator.py"
    initial_content = calc_file.read_text()
    initial_hash = hashlib.sha256(initial_content.encode("utf-8")).hexdigest()

    valid_diff = (
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -8,2 +8,4 @@\n"
        "     def divide(self, a, b):\n"
        "+        if b == 0:\n"
        '+            raise ZeroDivisionError("division by zero")\n'
        "         return a / b\n"
    )

    provider = FakeLLMProvider(scripted_responses=[_make_mock_json_response("calculator.py", valid_diff)])
    planner = RepairPlanner(llm_provider=provider)

    # Execute repair planning
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert plan.status == RepairPlanStatus.VALIDATED

    # Verify original repository is untouched
    final_content = calc_file.read_text()
    final_hash = hashlib.sha256(final_content.encode("utf-8")).hexdigest()
    assert final_hash == initial_hash

    # Verify git status is clean and HEAD commit is identical
    status_out = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    assert status_out == ""

    current_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    assert current_sha == head_sha


# =============================================================================
# Test 11: Local provider failure cannot trigger cloud or mock fallback
# =============================================================================
def test_local_provider_failure_cannot_trigger_cloud_or_mock_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    monkeypatch.setenv("FORGE_ALLOW_CLOUD_FALLBACK", "false")

    class FailingProvider:
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise ConnectionError("Connection to local Ollama endpoint failed: Connection refused")

    planner = RepairPlanner(llm_provider=FailingProvider(), provider_name="ollama")

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=head_sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("LLM provider generation failed" in v for v in plan.validation.violations)
    # Ensure provider is not secretly switched to cloud or fake
    assert plan.provider == "ollama"
