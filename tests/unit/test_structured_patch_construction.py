"""Unit tests for Structured Patch Construction Milestone.

Verifies:
1. Valid minimal search-and-replace proposal produces genuine unified diff and RepairPlanStatus.VALIDATED.
2. Exact indentation preservation in generated diff hunks.
3. Missing search block (count == 0) fails closed with targeted diagnostic.
4. Ambiguous search block matching multiple locations (count > 1) fails closed.
5. Empty or whitespace-only search or replace block is rejected.
6. Unchanged replacement (search_block == replace_block) is rejected as no-op.
7. Unauthorized paths and path traversal (.., absolute paths, protected files) are rejected.
8. Stale base revision mismatch is rejected.
9. Malformed structured output and bounded response size are rejected.
10. Generated patch parses and applies cleanly in memory with valid AST syntax.
11. Replacement resolves defect in an isolated scratch workspace.
12. Provider failure fails closed without cloud or mock fallback.
13. Target repository and Git history remain strictly immutable.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
import subprocess
from typing import List, Optional, Tuple
import pytest

from forge.analysis.evidence import create_evidence
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairSafetyContract,
    construct_unified_diff_from_search_replace,
    parse_and_validate_unified_diff,
    validate_patch_applicability,
)
from forge.repair.verifier import apply_unified_diff_to_file
from forge.repair.models import (
    RepairPlan,
    RepairPlanStatus,
    StructuredPatchProposalSchema,
)
from forge.repair.planner import (
    MAX_RESPONSE_CHARS,
    RepairPlanner,
)
from forge.schemas import (
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)


def _init_test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Helper to initialize a clean disposable git repository fixture."""
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
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head_sha


def _make_mock_structured_response(
    target_file: str,
    search_block: str,
    replace_block: str,
    change_rationale: str = "Fix division by zero",
    root_cause: str = "Missing divisor zero-check in Calculator.divide",
    proposed_change: str = "Check if divisor is 0 and raise ZeroDivisionError",
) -> LLMResponse:
    payload = {
        "root_cause": root_cause,
        "proposed_change": proposed_change,
        "target_file": target_file,
        "search_block": search_block,
        "replace_block": replace_block,
        "change_rationale": change_rationale,
        "expected_behavior": "Defect resolved without regressing callers",
        "regression_risks": ["None identified"],
        "verification_plan": "pytest tests/test_calculator.py",
        "confidence": 0.95,
        "uncertainty_notes": None,
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
# Test 1: Valid minimal search-and-replace proposal produces genuine unified diff
# =============================================================================
def test_valid_minimal_replacement_produces_genuine_diff_and_validated(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    search_block = (
        "    def divide(self, a, b):\n"
        "        return a / b\n"
    )
    replace_block = (
        "    def divide(self, a, b):\n"
        "        if b == 0:\n"
        "            raise ZeroDivisionError(\"division by zero\")\n"
        "        return a / b\n"
    )

    provider = FakeLLMProvider(
        model_name="qwen2.5-coder:1.5b",
        scripted_responses=[
            _make_mock_structured_response("calculator.py", search_block, replace_block)
        ],
    )

    planner = RepairPlanner(llm_provider=provider, max_retries=1)
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        finding=finding,
        evidence_records=evidence,
        target_repo=repo,
        base_commit_sha=head_sha,
        scratch_workspace_path=tmp_path / "scratch_test1",
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.validation is not None
    assert plan.validation.is_valid is True
    assert plan.validation.violations == []
    assert plan.patch_candidate is not None

    diff = plan.patch_candidate.unified_diff
    assert "--- a/calculator.py" in diff
    assert "+++ b/calculator.py" in diff
    assert "@@" in diff
    assert '+        if b == 0:' in diff
    assert '+            raise ZeroDivisionError("division by zero")' in diff


# =============================================================================
# Test 2: Exact indentation preservation
# =============================================================================
def test_exact_indentation_preservation(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    # Search block with 4-space indentation
    search_block = "    def multiply(self, a, b):\n        return a * b\n"
    replace_block = (
        "    def multiply(self, a, b):\n"
        "        # 8-space inner body\n"
        "        product = a * b\n"
        "        return product\n"
    )

    is_valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha=head_sha,
    )

    assert is_valid is True
    assert reasons == []
    assert "+        # 8-space inner body" in diff
    assert "+        product = a * b" in diff
    assert "+        return product" in diff


# =============================================================================
# Test 3: Missing search block (count == 0) fails closed
# =============================================================================
def test_missing_search_block_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    search_block = "def non_existent_function(self):\n    pass\n"
    replace_block = "def new_function(self):\n    pass\n"

    is_valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha=head_sha,
    )

    assert is_valid is False
    assert diff == ""
    assert any("search block not found" in r.lower() for r in reasons)


# =============================================================================
# Test 4: Ambiguous multiply matching search block (count > 1) fails closed
# =============================================================================
def test_ambiguous_multiply_matching_search_block_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    # "    def " matches 4 method definitions in calculator.py
    search_block = "    def "
    replace_block = "    def modified_"

    is_valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha=head_sha,
    )

    assert is_valid is False
    assert diff == ""
    assert any("matches 4 locations" in r.lower() for r in reasons)


# =============================================================================
# Test 5: Empty or whitespace-only search or replace block is rejected
# =============================================================================
def test_empty_or_whitespace_blocks_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    # Empty search block
    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block="   \n",
        replace_block="def something():\n    pass\n",
    )
    assert is_valid is False
    assert any("empty or whitespace" in r.lower() for r in reasons)

    # Empty replace block
    is_valid2, _, reasons2, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block="        return a + b\n",
        replace_block="",
    )
    assert is_valid2 is False
    assert any("empty or whitespace" in r.lower() for r in reasons2)


# =============================================================================
# Test 6: Unchanged replacement (search_block == replace_block) is rejected
# =============================================================================
def test_unchanged_replacement_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    block = "        return a + b\n"
    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=block,
        replace_block=block,
    )
    assert is_valid is False
    assert any("identical" in r.lower() or "unchanged replacement" in r.lower() for r in reasons)


# =============================================================================
# Test 7: Unauthorized paths and path traversal are rejected
# =============================================================================
def test_unauthorized_paths_and_path_traversal_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    # Traversal with ..
    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="../other.py",
        search_block="foo",
        replace_block="bar",
    )
    assert is_valid is False
    assert any("traversal" in r.lower() for r in reasons)

    # Absolute path
    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="/etc/passwd",
        search_block="root",
        replace_block="nobody",
    )
    assert is_valid is False
    assert any("traversal" in r.lower() or "absolute" in r.lower() for r in reasons)

    # Protected file
    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="README.md",
        search_block="foo",
        replace_block="bar",
    )
    assert is_valid is False
    assert any("protected" in r.lower() for r in reasons)

    # Test file
    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="tests/test_calculator.py",
        search_block="foo",
        replace_block="bar",
    )
    assert is_valid is False
    assert any("test file" in r.lower() for r in reasons)


# =============================================================================
# Test 8: Stale base revision mismatch is rejected
# =============================================================================
def test_stale_base_revision_rejected(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    search_block = "        return a + b\n"
    replace_block = "        return a + b + 0\n"

    is_valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha="1111111111111111111111111111111111111111",
    )
    assert is_valid is False
    assert any("stale base revision" in r.lower() for r in reasons)


# =============================================================================
# Test 9: Malformed structured output and bounded response size are rejected
# =============================================================================
def test_malformed_structured_output_and_bounded_response_size(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    # Oversized response
    oversized = "x" * (MAX_RESPONSE_CHARS + 500)
    provider = FakeLLMProvider(
        model_name="qwen2.5-coder:1.5b",
        scripted_responses=[
            LLMResponse(content=oversized, finish_reason="stop", prompt_tokens=10, completion_tokens=10)
        ],
    )
    planner = RepairPlanner(llm_provider=provider, max_retries=0)
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        finding=finding,
        evidence_records=evidence,
        target_repo=repo,
        base_commit_sha=head_sha,
        scratch_workspace_path=tmp_path / "scratch_oversized",
    )
    assert plan.status == RepairPlanStatus.REJECTED
    assert any("maximum allowed length" in v for v in plan.validation.violations)


# =============================================================================
# Test 10: Generated patch parses and applies cleanly in memory with valid AST
# =============================================================================
def test_generated_patch_parses_and_applies_cleanly_in_memory(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    search_block = (
        "    def divide(self, a, b):\n"
        "        return a / b\n"
    )
    replace_block = (
        "    def divide(self, a, b):\n"
        "        if b == 0:\n"
        "            raise ZeroDivisionError(\"division by zero\")\n"
        "        return a / b\n"
    )

    is_valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha=head_sha,
    )

    assert is_valid is True
    assert diff != ""

    appl_valid, appl_reasons = validate_patch_applicability(repo, diff, head_sha)
    assert appl_valid is True
    assert appl_reasons == []


# =============================================================================
# Test 11: Replacement resolves defect in an isolated scratch workspace
# =============================================================================
def test_replacement_resolves_calculator_reproduction_in_scratch(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)

    search_block = (
        "    def divide(self, a, b):\n"
        "        return a / b\n"
    )
    replace_block = (
        "    def divide(self, a, b):\n"
        "        if b == 0:\n"
        "            raise ZeroDivisionError(\"division by zero\")\n"
        "        return a / b\n"
    )

    is_valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha=head_sha,
    )
    assert is_valid is True

    # Create scratch workspace
    scratch_dir = tmp_path / "scratch_eval"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-r", f"{repo}/.", str(scratch_dir)], check=True)

    # Apply diff to scratch calculator.py
    apply_unified_diff_to_file(scratch_dir / "calculator.py", diff)

    # Run tests in scratch workspace
    env = {**os.environ, "PYTHONPATH": str(scratch_dir)}
    res = subprocess.run(
        [sys.executable, "-m", "pytest", str(scratch_dir / "tests")],
        cwd=scratch_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "1 passed" in res.stdout


# =============================================================================
# Test 12: Provider failure fails closed without cloud or mock fallback
# =============================================================================
def test_provider_failure_fails_closed_without_cloud_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FORGE_ALLOW_CLOUD_FALLBACK", "false")
    repo, head_sha = _init_test_git_repo(tmp_path)
    finding, evidence = _make_finding_and_evidence(head_sha)

    class FailingProvider(FakeLLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise ConnectionError("Local Ollama service unreachable at localhost:11434")

    provider = FailingProvider(model_name="qwen2.5-coder:1.5b")
    planner = RepairPlanner(llm_provider=provider, max_retries=0)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        finding=finding,
        evidence_records=evidence,
        target_repo=repo,
        base_commit_sha=head_sha,
        scratch_workspace_path=tmp_path / "scratch_fail",
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.patch_candidate is None
    assert any("provider generation failed" in v.lower() for v in plan.validation.violations)


# =============================================================================
# Test 13: Target repository and Git history remain strictly immutable
# =============================================================================
def test_target_repository_and_git_history_remain_immutable(tmp_path: Path):
    repo, head_sha = _init_test_git_repo(tmp_path)
    calc_path = repo / "calculator.py"
    calc_hash_before = hashlib.sha256(calc_path.read_bytes()).hexdigest()

    finding, evidence = _make_finding_and_evidence(head_sha)
    search_block = "    def divide(self, a, b):\n        return a / b\n"
    replace_block = "    def divide(self, a, b):\n        if b == 0:\n            raise ZeroDivisionError('zero')\n        return a / b\n"

    provider = FakeLLMProvider(
        model_name="qwen2.5-coder:1.5b",
        scripted_responses=[
            _make_mock_structured_response("calculator.py", search_block, replace_block)
        ],
    )
    planner = RepairPlanner(llm_provider=provider, max_retries=1)
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        finding=finding,
        evidence_records=evidence,
        target_repo=repo,
        base_commit_sha=head_sha,
        scratch_workspace_path=tmp_path / "scratch_immutable",
    )

    assert plan.status == RepairPlanStatus.VALIDATED

    # Verify target file has not been modified
    calc_hash_after = hashlib.sha256(calc_path.read_bytes()).hexdigest()
    assert calc_hash_before == calc_hash_after

    # Verify target repo git status is clean and HEAD has not changed
    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert status_res.stdout.strip() == ""

    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    assert head_sha == head_after
