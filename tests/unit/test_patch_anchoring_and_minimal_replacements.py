"""Unit tests for Phase 4 Deterministic Patch Anchoring and Minimal Replacements.

Verifies:
1. Minimal unique statement-level replacement inside a function containing a docstring is accepted.
2. A larger search block that omits intervening source lines (e.g. docstring) is rejected with targeted diagnostic.
3. Missing and ambiguous search blocks are rejected with actionable feedback.
4. Exact indentation and newline preservation (no fuzzy matching, no silent trimming).
5. Stale source revisions, unauthorized paths, test file targets, and protected paths are rejected.
6. No-op replacements, whitespace-only blocks, and line-number prefixed search blocks are rejected.
7. Successful deterministic unified diff generation and in-memory applicability validation.
8. The calculator defect is resolved in an isolated scratch workspace with test passing.
9. Original fixture and historical audit records remain strictly unchanged.
10. Provider failures fail closed without cloud or mock fallback.
11. Planner retry loop injects focused anchor excerpts when search block is missing.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest
from pydantic import ValidationError

from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    construct_unified_diff_from_search_replace,
    validate_patch_applicability,
)
from forge.repair.models import (
    RepairModelResponseSchema,
    RepairPlanStatus,
)
from forge.repair.planner import (
    RepairPlanner,
    _extract_defect_line_numbers,
    _build_focused_anchor_section,
)
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.llm.provider import LLMProvider
from forge.storage.store import ForgeStore


class ScriptedProvider(LLMProvider):
    """Deterministic scripted provider for testing retry and validation sequences."""

    def __init__(self, responses: List[str], provider_name: str = "ollama", model_name: str = "qwen2.5-coder:1.5b"):
        self.responses = list(responses)
        self.call_count = 0
        self.recorded_requests: List[LLMRequest] = []
        self._provider_name = provider_name
        self._model_name = model_name

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.recorded_requests.append(request)
        if self.call_count < len(self.responses):
            resp_text = self.responses[self.call_count]
            self.call_count += 1
            return LLMResponse(
                content=resp_text,
                provider=self._provider_name,
                model=self._model_name,
            )
        raise RuntimeError("No more scripted responses available")

    def get_provider_name(self) -> str:
        return self._provider_name

    def get_model_name(self) -> str:
        return self._model_name


SAMPLE_CALCULATOR_CODE = (
    '"""Simple calculator module demonstrating a synthetic defect in division-by-zero handling."""\n'
    '\n'
    '\n'
    'def add(a: float, b: float) -> float:\n'
    '    """Return the sum of a and b."""\n'
    '    return a + b\n'
    '\n'
    '\n'
    'def subtract(a: float, b: float) -> float:\n'
    '    """Return the difference of a and b."""\n'
    '    return a - b\n'
    '\n'
    '\n'
    'def multiply(a: float, b: float) -> float:\n'
    '    """Return the product of a and b."""\n'
    '    return a * b\n'
    '\n'
    '\n'
    'def divide(a: float, b: float) -> float:\n'
    '    """Return a divided by b.\n'
    '\n'
    '    Expected behavior: raises ZeroDivisionError when b == 0.\n'
    '    Defect: returns 0.0 instead of raising ZeroDivisionError.\n'
    '    """\n'
    '    if b == 0:\n'
    '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")\n'
    '    return a / b\n'
)


def _setup_test_repo(tmp_path: Path) -> tuple[Path, Finding, List[EvidenceRecord]]:
    """Helper to initialize an isolated git repo with calculator.py and test_calculator.py."""
    repo = tmp_path / "repo"
    repo.mkdir()

    calc = repo / "calculator.py"
    calc.write_text(SAMPLE_CALCULATOR_CODE, encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_calc = tests_dir / "test_calculator.py"
    test_calc.write_text(
        "import pytest\n"
        "from calculator import divide\n\n"
        "def test_divide_zero():\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        divide(10.0, 0.0)\n\n"
        "def test_divide_normal():\n"
        "    assert divide(10.0, 2.0) == 5.0\n",
        encoding="utf-8",
    )

    # Init git repo
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    finding = Finding(
        finding_id="find_test_div_zero",
        title="ZeroDivisionError not raised in divide",
        description="divide returns 0.0 instead of raising ZeroDivisionError",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.CONFIRMED,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=["calculator.py", "tests/test_calculator.py"],
    )

    evidence = [
        EvidenceRecord(
            evidence_id="evi_test_1",
            run_id="run_001",
            finding_id="find_test_div_zero",
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Failed test_divide_zero",
            content='File "/workspace/calculator.py", line 27, in divide\nAssertionError: Expected ZeroDivisionError but returned 0.0',
        )
    ]
    return repo, finding, evidence


def test_minimal_unique_statement_replacement_inside_function_with_docstring(tmp_path: Path):
    """A minimal unique statement-level replacement inside a function containing a docstring succeeds."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Minimal statement-level search block: only the 2-line faulty conditional
    search_block = (
        "    if b == 0:\n"
        '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")'
    )
    replace_block = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search_block,
        replace_block=replace_block,
        allowed_files=["calculator.py"],
        base_commit_sha=sha,
    )

    assert valid is True, f"Expected valid diff, got reasons: {reasons}"
    assert "--- a/calculator.py" in diff
    assert "+++ b/calculator.py" in diff
    assert '-        return 0.0' in diff
    assert '+        raise ZeroDivisionError("division by zero")' in diff
    assert metrics["added_lines"] == 1
    assert metrics["removed_lines"] == 1


def test_larger_search_block_omitting_intervening_lines_rejected(tmp_path: Path):
    """A larger search block that spans from function header to body while omitting intervening docstring is rejected."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Spans function signature and body, but omits the 5-line docstring
    omitting_docstring_search = (
        "def divide(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")\n'
        "    return a / b"
    )
    replace = (
        "def divide(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")\n'
        "    return a / b"
    )

    valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=omitting_docstring_search,
        replace_block=replace,
        allowed_files=["calculator.py"],
        base_commit_sha=sha,
    )

    assert valid is False
    assert any("intervening lines" in r.lower() for r in reasons), f"Expected intervening lines diagnosis, got: {reasons}"


def test_missing_and_ambiguous_search_blocks(tmp_path: Path):
    """Missing search blocks (count == 0) and ambiguous search blocks (count > 1) are rejected."""
    repo, finding, evidence = _setup_test_repo(tmp_path)

    # 1. Missing search block
    missing_search = "def non_existent_function(x: int) -> int:\n    return x * 42"
    replace = "def non_existent_function(x: int) -> int:\n    return x * 100"
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=missing_search,
        replace_block=replace,
    )
    assert valid is False
    assert any("search block not found" in r.lower() for r in reasons)

    # 2. Ambiguous search block: "    return a - b" if duplicated, or common statement
    # Let's add duplicate lines to calculator.py
    calc_path = repo / "calculator.py"
    calc_path.write_text(calc_path.read_text() + "\n# duplicate return\n    return a + b\n")

    ambiguous_search = "    return a + b"
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=ambiguous_search,
        replace_block="    return a + b + 1",
    )
    assert valid is False
    assert any("matches 2 locations" in r.lower() for r in reasons)


def test_exact_indentation_and_newline_preservation(tmp_path: Path):
    """Verifies that 4-space, 8-space indentations and CRLF/LF newlines are preserved verbatim without fuzzy trimming."""
    repo, finding, evidence = _setup_test_repo(tmp_path)

    # Exact 4-space if and 8-space indentation search
    search = (
        "    if b == 0:\n"
        '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")'
    )
    replace = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search,
        replace_block=replace,
    )
    assert valid is True
    # The diff must have 8 spaces before raise
    assert '+        raise ZeroDivisionError("division by zero")' in diff

    # Incorrect indentation (0-space if and 4-space return) must fail verbatim matching
    wrong_indent_search = (
        "if b == 0:\n"
        '    return 0.0  # Defect: should raise ZeroDivisionError("division by zero")'
    )
    valid_wrong, _, reasons_wrong, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=wrong_indent_search,
        replace_block=replace,
    )
    assert valid_wrong is False
    assert any("search block not found" in r.lower() for r in reasons_wrong)


def test_stale_source_revisions_and_unauthorized_paths(tmp_path: Path):
    """Rejects stale base commit revisions, unauthorized file paths, and test file targets."""
    repo, finding, evidence = _setup_test_repo(tmp_path)

    search = "    if b == 0:\n        return 0.0"
    replace = "    if b == 0:\n        raise ZeroDivisionError()"

    # Stale base commit
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search,
        replace_block=replace,
        base_commit_sha="0000000000000000000000000000000000000000",
    )
    assert valid is False
    assert any("revision" in r.lower() or "stale" in r.lower() for r in reasons)

    # Path traversal
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="../other.py",
        search_block=search,
        replace_block=replace,
    )
    assert valid is False
    assert any("path traversal" in r.lower() for r in reasons)

    # Test file target
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="tests/test_calculator.py",
        search_block="assert True",
        replace_block="assert False",
    )
    assert valid is False
    assert any("test file" in r.lower() for r in reasons)

    # Protected path (AGENTS.md)
    (repo / "AGENTS.md").write_text("# Policy", encoding="utf-8")
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="AGENTS.md",
        search_block="# Policy",
        replace_block="# Modified Policy",
    )
    assert valid is False
    assert any("protected" in r.lower() for r in reasons)


def test_no_op_replacements_and_malformed_proposals(tmp_path: Path):
    """Rejects no-op replacements (identical blocks), empty blocks, and line number prefixed search blocks."""
    repo, finding, evidence = _setup_test_repo(tmp_path)

    # No-op replacement
    search = "    return a / b"
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search,
        replace_block=search,
    )
    assert valid is False
    assert any("identical" in r.lower() or "no-op" in r.lower() for r in reasons)

    # Empty search block
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block="   \n\t  ",
        replace_block="something",
    )
    assert valid is False
    assert any("empty" in r.lower() for r in reasons)

    # Line number prefix in search block
    line_num_search = "  27:         return 0.0  # Defect"
    valid, _, reasons, _ = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=line_num_search,
        replace_block="        raise ZeroDivisionError()",
    )
    assert valid is False
    assert any("line number prefix" in r.lower() for r in reasons)


def test_successful_deterministic_diff_generation_and_in_memory_application(tmp_path: Path):
    """Valid minimal statement replacement produces standard unified diff that applies cleanly in-memory."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    search = (
        "    if b == 0:\n"
        '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")'
    )
    replace = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search,
        replace_block=replace,
        base_commit_sha=sha,
    )
    assert valid is True

    # Test in-memory applicability directly
    appl_valid, appl_reasons = validate_patch_applicability(repo, diff, base_commit_sha=sha)
    assert appl_valid is True, f"Applicability failed: {appl_reasons}"


def test_calculator_defect_fixed_in_isolated_scratch_workspace(tmp_path: Path):
    """The generated patch candidate fixes the defect in an isolated scratch workspace and tests pass."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Pre-check: failing test before fix
    res_before = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_calculator.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert res_before.returncode != 0, "Test should fail before patch"

    search = (
        "    if b == 0:\n"
        '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")'
    )
    replace = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    valid, diff, reasons, metrics = construct_unified_diff_from_search_replace(
        repo_path=repo,
        target_file="calculator.py",
        search_block=search,
        replace_block=replace,
        base_commit_sha=sha,
    )
    assert valid is True

    # Create isolated scratch workspace
    scratch = tmp_path / "scratch_test"
    shutil.copytree(repo, scratch)

    # Apply diff to scratch workspace file
    calc_scratch = scratch / "calculator.py"
    orig_code = calc_scratch.read_text(encoding="utf-8")
    patched_code = orig_code.replace(search, replace, 1)
    calc_scratch.write_text(patched_code, encoding="utf-8")

    # Post-check: run pytest in scratch
    res_after = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_calculator.py"],
        cwd=scratch,
        capture_output=True,
        text=True,
    )
    assert res_after.returncode == 0, f"Pytest should pass after patch: {res_after.stdout}"


def test_original_fixture_and_historical_audit_records_unchanged():
    """Verify that the immutable test fixture and historical SQLite database remain unchanged."""
    fixture_path = Path("/private/tmp/forge_pilot_01")
    if fixture_path.is_dir():
        # Check git HEAD and status
        git_res = subprocess.run(
            ["git", "-C", str(fixture_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if git_res.returncode == 0:
            assert git_res.stdout.strip() == "0ba1653f3607e1dd1d73b4bc07e0b16273e6c777"
            git_status = subprocess.run(
                ["git", "-C", str(fixture_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert git_status == "", "Fixture worktree must remain 100% clean"

    # Check store records exist
    store = ForgeStore()
    wfs = store.list_engineering_workflows()
    assert len(wfs) >= 5, "Historical workflow records in SQLite must be preserved"


def test_provider_failures_without_cloud_or_mock_fallback(tmp_path: Path):
    """When the provider raises an error, planner fails closed without falling back to cloud."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    class ConnectionFailingProvider(LLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise ConnectionError("Ollama daemon unreachable at http://localhost:11434")

        def get_provider_name(self) -> str:
            return "ollama"

        def get_model_name(self) -> str:
            return "qwen2.5-coder:1.5b"

    planner = RepairPlanner(llm_provider=ConnectionFailingProvider(), max_retries=1)
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("LLM provider generation failed" in v for v in plan.validation.violations)


def test_planner_retry_loop_provides_focused_anchor_excerpt(tmp_path: Path):
    """Planner provides focused anchor lines in retry guidance when initial search block is missing."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Response 1: Bad search block omitting docstrings (Attempt 6 style)
    resp1 = (
        '{\n'
        '  "root_cause": "divide returns 0.0 instead of raising ZeroDivisionError",\n'
        '  "proposed_change": "Modify divide to raise ZeroDivisionError",\n'
        '  "target_file": "calculator.py",\n'
        '  "search_block": "def divide(a: float, b: float) -> float:\\n    if b == 0:\\n        return 0.0\\n    return a / b",\n'
        '  "replace_block": "def divide(a: float, b: float) -> float:\\n    if b == 0:\\n        raise ZeroDivisionError()\\n    return a / b",\n'
        '  "change_rationale": "Handle division by zero",\n'
        '  "expected_behavior": "Raises ZeroDivisionError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "unified_diff": null\n'
        '}'
    )

    # Response 2: Corrected minimal statement-level search block
    resp2 = (
        '{\n'
        '  "root_cause": "divide returns 0.0 instead of raising ZeroDivisionError",\n'
        '  "proposed_change": "Modify divide to raise ZeroDivisionError",\n'
        '  "target_file": "calculator.py",\n'
        '  "search_block": "    if b == 0:\\n        return 0.0  # Defect: should raise ZeroDivisionError(\\"division by zero\\")",\n'
        '  "replace_block": "    if b == 0:\\n        raise ZeroDivisionError(\\"division by zero\\")",\n'
        '  "change_rationale": "Handle division by zero",\n'
        '  "expected_behavior": "Raises ZeroDivisionError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "unified_diff": null\n'
        '}'
    )

    provider = ScriptedProvider([resp1, resp2])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert provider.call_count == 2

    # Check that second request contains the retry guidance and anchor lines
    retry_msg = provider.recorded_requests[1].messages[-1].content
    assert "intervening lines" in retry_msg.lower() or "search_block was not found" in retry_msg
    assert "Target a SMALLER, statement-level block" in retry_msg
