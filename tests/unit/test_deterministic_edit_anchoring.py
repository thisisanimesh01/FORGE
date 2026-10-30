"""Unit and regression tests for Deterministic Edit Anchoring & Source-Grounded Repair."""

import hashlib
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional
import pytest

from forge.config import get_settings
from forge.llm.provider import LLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.repair.anchoring import (
    AnchorResolutionStatus,
    EditAnchorContract,
    construct_patch_from_edit_anchor,
    resolve_source_anchor,
)
from forge.repair.contracts import (
    RepairBudget,
    RepairSafetyContract,
    construct_unified_diff_from_search_replace,
    parse_and_validate_unified_diff,
    validate_patch_applicability,
)
from forge.repair.models import (
    ProtectedPathPolicy,
    RepairModelResponseSchema,
    RepairPlan,
    RepairPlanStatus,
)
from forge.repair.planner import RepairPlanner
from forge.repair.verifier import apply_unified_diff_to_file, prepare_disposable_workspace
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


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

SAMPLE_TEST_CODE = (
    "import unittest\n"
    "from calculator import add, subtract, multiply, divide\n\n"
    "class TestCalculator(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_divide_normal(self):\n"
    "        self.assertEqual(divide(10, 2), 5.0)\n\n"
    "    def test_divide_by_zero(self):\n"
    '        """Demonstrates defect: divide(x, 0) should raise ZeroDivisionError."""\n'
    "        with self.assertRaises(ZeroDivisionError):\n"
    "            divide(10, 0)\n\n"
    'if __name__ == "__main__":\n'
    "    unittest.main()\n"
)


def _setup_test_repo(tmp_path: Path) -> tuple[Path, Finding, List[EvidenceRecord]]:
    """Initialize an isolated test git repository with calculator.py and test_calculator.py."""
    repo = tmp_path / "repo"
    repo.mkdir()

    calc = repo / "calculator.py"
    calc.write_text(SAMPLE_CALCULATOR_CODE, encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_calc = tests_dir / "test_calculator.py"
    test_calc.write_text(SAMPLE_TEST_CODE, encoding="utf-8")

    # Init git repo
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    finding = Finding(
        finding_id="find_div_zero_anchor",
        title="ZeroDivisionError not raised in divide",
        description="divide returns 0.0 instead of raising ZeroDivisionError when divisor is 0",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.CONFIRMED,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=["calculator.py", "tests/test_calculator.py"],
    )

    evidence = [
        EvidenceRecord(
            evidence_id="evi_repro_anchor",
            run_id="run_anchor_test",
            finding_id="find_div_zero_anchor",
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Docker reproduction failed with ZeroDivisionError not raised",
            content=(
                "Reproduction Command: python3 -m unittest tests.test_calculator.TestCalculator.test_divide_by_zero\n"
                "Traceback (most recent call last):\n"
                '  File "/workspace/tests/test_calculator.py", line 17, in test_divide_by_zero\n'
                "    with self.assertRaises(ZeroDivisionError):\n"
                "AssertionError: ZeroDivisionError not raised\n"
            ),
            details={"test_name": "TestCalculator.test_divide_by_zero"},
        )
    ]
    return repo, finding, evidence


# =============================================================================
# Test 1: Exact source anchor derived from trusted source content
# =============================================================================

def test_exact_source_anchor_derived_from_trusted_content(tmp_path: Path):
    """1. Anchor is derived deterministically from trusted repository content."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
        allowed_files=["calculator.py"],
    )

    assert anchor.status == AnchorResolutionStatus.RESOLVED
    assert anchor.target_file == "calculator.py"
    assert anchor.base_commit_sha == sha
    assert anchor.start_line == 25
    assert anchor.end_line == 26
    assert "if b == 0:" in anchor.anchor_text
    assert "return 0.0" in anchor.anchor_text
    # Anchor must not include the function header or docstring
    assert "def divide" not in anchor.anchor_text
    assert "Return a divided by b" not in anchor.anchor_text
    # Source digest must match actual content SHA-256
    expected_digest = hashlib.sha256(SAMPLE_CALCULATOR_CODE.encode("utf-8")).hexdigest()
    assert anchor.source_digest == expected_digest


# =============================================================================
# Test 2: Minimal replacement inside a function containing a docstring & comments
# =============================================================================

def test_minimal_replacement_inside_function_with_docstring_and_comments(tmp_path: Path):
    """2. Minimal statement replacement leaves docstring and comments completely untouched."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    replacement = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    valid, diff, reasons, metrics = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=replacement,
        repo_path=repo,
    )

    assert valid is True
    assert reasons == []
    assert metrics["hunk_count"] == 1
    assert metrics["added_lines"] == 1
    assert metrics["removed_lines"] == 1
    # Check that docstring lines are not modified
    assert "Defect: returns 0.0 instead of raising ZeroDivisionError." in diff
    assert '+        raise ZeroDivisionError("division by zero")' in diff
    assert '-        return 0.0' in diff


# =============================================================================
# Test 3: Model proposal that omits intervening lines
# =============================================================================

def test_model_proposal_omitting_intervening_lines_handled_via_anchor(tmp_path: Path):
    """3. When a model proposes a search block omitting docstrings, anchor fallback recovers safely."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Model payload that tries to span from def divide down to return a / b but omits docstring
    flawed_search_block = (
        "def divide(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        '        return 0.0  # Defect: should raise ZeroDivisionError("division by zero")\n'
        "    return a / b"
    )
    model_replace_block = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    class OmittedLinesProvider(LLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            payload = {
                "root_cause": "Missing ZeroDivisionError",
                "proposed_change": "Raise ZeroDivisionError when b is 0",
                "target_file": "calculator.py",
                "search_block": flawed_search_block,
                "replace_block": model_replace_block,
                "change_rationale": "Fix division by zero defect",
                "unified_diff": None,
                "expected_behavior": "Raises ZeroDivisionError on 0 divisor",
                "verification_plan": "python3 -m unittest",
            }
            import json
            return LLMResponse(content=json.dumps(payload), model_name="fake-coder")

        def get_provider_name(self) -> str:
            return "fake"

    planner = RepairPlanner(llm_provider=OmittedLinesProvider(), model_name="fake-coder")
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.patch_candidate is not None
    assert plan.patch_candidate.added_lines == 1
    assert plan.patch_candidate.removed_lines == 1
    assert '+        raise ZeroDivisionError("division by zero")' in plan.patch_candidate.unified_diff


# =============================================================================
# Test 4: Missing, ambiguous, stale, or digest-mismatched anchors
# =============================================================================

def test_missing_ambiguous_stale_and_digest_mismatched_anchors(tmp_path: Path):
    """4. Confirms typed AnchorResolutionStatus for missing, ambiguous, stale, and digest mismatches."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # 4a. Missing target file
    anchor_missing = resolve_source_anchor(
        repo_path=repo,
        target_file="nonexistent.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor_missing.status == AnchorResolutionStatus.ANCHOR_UNRESOLVED
    assert "does not exist" in anchor_missing.rejection_reason

    # 4b. Stale base revision
    anchor_stale = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha="0000000000000000000000000000000000000000",
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor_stale.status == AnchorResolutionStatus.STALE_REVISION
    assert "revision" in anchor_stale.rejection_reason.lower() or "stale" in anchor_stale.rejection_reason.lower()

    # 4c. Digest mismatch
    anchor_digest = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
        expected_source_digest="badc0ffeebadc0ffeebadc0ffeebadc0ffeebadc0ffeebadc0ffeebadc0ffee",
    )
    assert anchor_digest.status == AnchorResolutionStatus.DIGEST_MISMATCH
    assert "digest mismatch" in anchor_digest.rejection_reason.lower()

    # 4d. Ambiguous anchor (duplicate identical functions in file)
    calc_path = repo / "calculator.py"
    # Duplicate calculator.py completely so function and surrounding context are duplicated identically
    calc_path.write_text(SAMPLE_CALCULATOR_CODE + "\n\n" + SAMPLE_CALCULATOR_CODE)
    # Commit changes so git sha matches
    subprocess.run(["git", "commit", "-am", "Add duplicate block"], cwd=repo, check=True, capture_output=True)
    new_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor_ambig = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=new_sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor_ambig.status == AnchorResolutionStatus.ANCHOR_AMBIGUOUS
    assert "matches 2 locations" in anchor_ambig.rejection_reason.lower()


# =============================================================================
# Test 5: Unauthorized paths, path traversal, symlinks, and protected files
# =============================================================================

def test_unauthorized_paths_traversal_symlinks_and_protected_files(tmp_path: Path):
    """5. Rejects unauthorized scope, path traversal, symlinks, and protected files."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # 5a. Path traversal
    res_trav = resolve_source_anchor(
        repo_path=repo,
        target_file="../other.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert res_trav.status == AnchorResolutionStatus.SCOPE_VIOLATION

    # 5b. Protected path (e.g. AGENTS.md, README.md, .gitignore)
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("# Rules\n")
    res_prot = resolve_source_anchor(
        repo_path=repo,
        target_file="AGENTS.md",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert res_prot.status == AnchorResolutionStatus.PROTECTED_PATH
    assert "protected" in res_prot.rejection_reason.lower()

    # 5c. Test file targeting
    res_test = resolve_source_anchor(
        repo_path=repo,
        target_file="tests/test_calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert res_test.status == AnchorResolutionStatus.SCOPE_VIOLATION
    assert "test file" in res_test.rejection_reason.lower()

    # 5d. Unauthorized scope (not in allowed_files)
    res_scope = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
        allowed_files=["other_file.py"],
    )
    assert res_scope.status == AnchorResolutionStatus.SCOPE_VIOLATION
    assert "outside declared allowed scope" in res_scope.rejection_reason.lower()


# =============================================================================
# Test 6: Valid deterministic diff with correct addition/deletion markers
# =============================================================================

def test_valid_deterministic_diff_syntax(tmp_path: Path):
    """6. Verifies generated diff headers, hunk coordinates, and column 0 +/- markers."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    replacement = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )

    valid, diff, reasons, metrics = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=replacement,
        repo_path=repo,
    )

    assert valid is True
    # Verify standard unified diff structure
    lines = diff.splitlines()
    assert lines[0] == "--- a/calculator.py"
    assert lines[1] == "+++ b/calculator.py"
    assert lines[2].startswith("@@ -")
    # Every hunk line must begin with +, -, or space
    for line in lines[3:]:
        assert line.startswith("+") or line.startswith("-") or line.startswith(" ") or line == ""


# =============================================================================
# Test 7: No-op and unrelated replacements
# =============================================================================

def test_noop_and_empty_replacements_rejected(tmp_path: Path):
    """7. Rejects no-op identical replacements and empty/whitespace replacements."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    # Identical replacement / no-op
    valid_noop, _, reasons_noop, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=anchor.anchor_text,
        repo_path=repo,
    )
    assert valid_noop is False
    assert any("unchanged replacement" in r.lower() or "no-op" in r.lower() for r in reasons_noop)

    # Empty replacement
    valid_empty, _, reasons_empty, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content="   \n  ",
        repo_path=repo,
    )
    assert valid_empty is False
    assert any("empty or whitespace" in r.lower() for r in reasons_empty)


# =============================================================================
# Test 8: Candidate resolves calculator reproduction in scratch workspace
# =============================================================================

def test_candidate_resolves_calculator_reproduction_in_scratch(tmp_path: Path):
    """8. Splicing candidate into isolated scratch workspace resolves defect oracle."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    replacement = (
        "    if b == 0:\n"
        '        raise ZeroDivisionError("division by zero")'
    )
    valid, diff, _, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=replacement,
        repo_path=repo,
    )
    assert valid is True

    # Prepare disposable scratch workspace
    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)

    # Pre-patch: test_divide_by_zero fails
    res_before = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/test_calculator.py"],
        cwd=scratch,
        capture_output=True,
        text=True,
    )
    assert res_before.returncode != 0

    # Apply patch
    apply_unified_diff_to_file(scratch / "calculator.py", diff)

    # Post-patch: test_divide_by_zero passes
    res_after = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/test_calculator.py"],
        cwd=scratch,
        capture_output=True,
        text=True,
    )
    assert res_after.returncode == 0


# =============================================================================
# Test 9: Candidate verification failure and baseline regression
# =============================================================================

def test_syntax_error_and_baseline_regression_detected(tmp_path: Path):
    """9. Rejects broken syntax or patches that break unrelated baseline tests."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    # 9a. Syntax error replacement
    syntax_err_replacement = "    if b == 0: def invalid syntax {{}"
    valid_syntax, _, reasons_syntax, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=syntax_err_replacement,
        repo_path=repo,
    )
    assert valid_syntax is False
    assert any("syntax" in r.lower() or "ast" in r.lower() for r in reasons_syntax)

    # 9b. Regression-inducing replacement (raises ZeroDivisionError unconditionally for all b)
    regress_replacement = "    raise ZeroDivisionError('unconditional')"
    valid_reg, diff_reg, _, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=regress_replacement,
        repo_path=repo,
    )
    assert valid_reg is True

    # In scratch workspace, regression breaks test_divide_normal
    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    apply_unified_diff_to_file(scratch / "calculator.py", diff_reg)

    res_reg = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/test_calculator.py"],
        cwd=scratch,
        capture_output=True,
        text=True,
    )
    assert res_reg.returncode != 0
    assert "ZeroDivisionError" in res_reg.stderr or "FAIL" in res_reg.stderr


# =============================================================================
# Test 10: Local-provider failures without cloud or mock fallback
# =============================================================================

def test_provider_failures_without_cloud_fallback(tmp_path: Path):
    """10. Provider connection failures fail closed without attempting cloud fallback."""
    repo, finding, evidence = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    class FailingOllamaProvider(LLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise ConnectionError("Ollama endpoint at http://localhost:11434 unreachable")

        def get_provider_name(self) -> str:
            return "ollama"

    planner = RepairPlanner(llm_provider=FailingOllamaProvider(), model_name="qwen2.5-coder:1.5b")
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert any("provider generation failed" in v.lower() for v in plan.validation.violations)
    # Cloud fallback must remain disabled
    assert get_settings().allow_cloud_fallback is False


# =============================================================================
# Test 11: Original fixture immutability and historical audit preservation
# =============================================================================

def test_original_fixture_and_historical_audit_records_unchanged():
    """11. Verifies test fixture and historical SQLite database remain untouched."""
    fixture_path = Path("/private/tmp/forge_pilot_01")
    if fixture_path.is_dir():
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
            assert git_status == "", "Fixture worktree must remain clean"

            # Check hashes
            calc_bytes = (fixture_path / "calculator.py").read_bytes()
            assert hashlib.sha256(calc_bytes).hexdigest() == "aca03f0e6868184e939567a34927b5c76001dbf6bc4a1c4a1e6ada685fd58bf6"

    # SQLite historical records (if database exists in environment)
    settings = get_settings()
    if settings.db_path.exists():
        store = ForgeStore()
        wfs = store.list_engineering_workflows()
        assert len(wfs) >= 5, "Historical workflow records in SQLite must be preserved"


# =============================================================================
# Test 12: Fail-closed behavior when source localization cannot be established
# =============================================================================

def test_fail_closed_when_source_localization_cannot_be_established(tmp_path: Path):
    """12. Fails closed with ANCHOR_UNRESOLVED when evidence and source cannot narrow anchor."""
    repo, finding, _ = _setup_test_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Evidence with contradictory or nonexistent symbol
    contradictory_evidence = [
        EvidenceRecord(
            evidence_id="evi_unknown",
            run_id="run_unknown",
            finding_id=finding.finding_id,
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Vague test error",
            content="Error occurred somewhere in non_existent_function at line 9999",
            details={},
        )
    ]

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file="calculator.py",
        base_commit_sha=sha,
        finding=Finding(
            finding_id="find_unresolvable",
            title="Unresolvable bug in unknown_feature",
            description="No symbol or line available",
            severity=FindingSeverity.LOW,
            confidence=FindingConfidence.LOW,
            lifecycle_state=FindingLifecycleState.REPRODUCED,
            affected_files=["calculator.py"],
        ),
        evidence_records=contradictory_evidence,
    )

    assert anchor.status == AnchorResolutionStatus.ANCHOR_UNRESOLVED
    assert "unable to deterministically locate defect anchor" in anchor.rejection_reason.lower()
