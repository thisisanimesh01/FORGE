"""Unit and integration tests for Deterministic Repair Construction Architecture.

Verifies:
1. GroundedRepairRepresentation is strongly typed, AST-grounded, and records exact statement coordinates.
2. Correct grounded edits synthesize standard unified diffs strictly anchored to statement lines.
3. Surrounding docstrings, parameter comments, and validation guards are strictly preserved.
4. Scope boundary defense: rejection of proposals that re-declare function/class headers when anchor is a statement.
5. Indentation defense: rejection of improperly indented replacement lines without silent mutation.
6. Rejection of no-op, identical, and whitespace-only replacements.
7. Patched AST syntax verification fails closed on malformed replacements.
8. Semantic invariant verification rejects unreachable statements and bypassed guards.
9. Stale source digests and workspace traversal attempts fail closed.
10. GEN07 fixture: nested statement anchoring, out-of-scope rejection, and verified scratch workspace execution.
11. End-to-end RepairPlanner integration with coordinate ownership and retry feedback guidance.
"""

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import pytest

from forge.analysis.evidence import create_evidence
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.llm.provider import LLMProvider
from forge.repair.anchoring import AnchorResolutionStatus, resolve_source_anchor
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    parse_and_validate_unified_diff,
    validate_patch_applicability,
)
from forge.repair.deterministic_repair import (
    DeterministicRepairResult,
    GroundedRepairRepresentation,
    build_grounded_repair_representation,
    construct_deterministic_repair,
)
from forge.repair.models import (
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

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "generalization_fixtures"


def _setup_repo_with_file(tmp_path: Path, filename: str, content: str) -> Tuple[Path, str]:
    """Initialize a git repo with a single committed file."""
    repo = tmp_path / "target_repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=repo, check=True, capture_output=True)

    file_disk = repo / filename
    file_disk.parent.mkdir(parents=True, exist_ok=True)
    file_disk.write_text(content, encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return repo, head_sha


def _setup_gen07_repo(tmp_path: Path) -> Tuple[Path, dict, Finding, List[EvidenceRecord]]:
    """Create an isolated git repository from GEN07 fixture."""
    fixture_dir = FIXTURES_DIR / "GEN07_nested_statement_anchor_scope"
    metadata = json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))

    repo = tmp_path / "gen07_repo"
    repo.mkdir(parents=True)

    target_src = repo / "src"
    target_src.mkdir(parents=True)
    for f in (fixture_dir / "src").glob("*.py"):
        (target_src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    target_tests = repo / "tests"
    target_tests.mkdir(parents=True)
    for f in (fixture_dir / "tests").glob("*.py"):
        (target_tests / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    finding = Finding(
        finding_id="find_gen07",
        title=metadata["name"],
        description=metadata["description"],
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.CONFIRMED,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=[metadata["target_file"], metadata["test_file"]],
    )

    evidence = [
        EvidenceRecord(
            evidence_id="evi_gen07",
            run_id="run_gen07",
            finding_id=finding.finding_id,
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Reproduction failed for GEN07",
            content=(
                f"Reproduction Command: {metadata['reproduction_command']}\n"
                f"Traceback (most recent call last):\n"
                f'  File "/workspace/{metadata["test_file"]}", line 8, in test_exponential_progression\n'
                f"  AssertionError: 2.0 != 4.0\n"
            ),
            details={"matched_patterns": ["AssertionError"]},
        )
    ]
    return repo, metadata, finding, evidence


# =============================================================================
# 1. GroundedRepairRepresentation Construction & Schema Tests
# =============================================================================

def test_grounded_representation_creation_and_attributes(tmp_path: Path):
    """Verify build_grounded_repair_representation creates a complete typed representation."""
    code = (
        "def discount(price: float) -> float:\n"
        '    """Apply discount."""\n'
        "    if price < 0:\n"
        '        raise ValueError("negative")\n'
        "    return price + 10.0\n"
    )
    repo, sha = _setup_repo_with_file(tmp_path, "pricing.py", code)

    finding = Finding(
        finding_id="find_discount",
        title="Incorrect discount calculation",
        description="discount adds 10 instead of subtracting",
        severity=FindingSeverity.HIGH,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=["pricing.py"],
    )
    evidence = [
        create_evidence(
            run_id="run_1",
            finding_id="find_discount",
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Failed test",
            content='File "pricing.py", line 5, in discount\n    return price + 10.0\nAssertionError',
        )
    ]

    rep = build_grounded_repair_representation(
        repo_path=repo,
        target_file="pricing.py",
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert rep.is_resolved is True
    assert rep.target_file == "pricing.py"
    assert rep.base_commit_sha == sha
    assert rep.ast_node_type == "Return"
    assert rep.symbol_scope == "discount"
    assert rep.start_line == 5
    assert rep.end_line == 5
    assert rep.original_statement == "    return price + 10.0"
    assert rep.allowed_replacement_scope == "statement_level"
    assert rep.confidence == 1.0


def test_grounded_representation_unresolved_anchor(tmp_path: Path):
    """Verify build_grounded_repair_representation fails cleanly when anchor is unresolvable."""
    repo, sha = _setup_repo_with_file(tmp_path, "empty.py", "x = 1\n")

    finding = Finding(
        finding_id="find_none",
        title="Nonexistent bug",
        description="description",
        severity=FindingSeverity.LOW,
        lifecycle_state=FindingLifecycleState.SUSPECTED,
        affected_files=["nonexistent.py"],
    )

    rep = build_grounded_repair_representation(
        repo_path=repo,
        target_file="nonexistent.py",
        base_commit_sha=sha,
        finding=finding,
    )

    assert rep.is_resolved is False
    assert len(rep.rejection_reasons) > 0
    assert rep.confidence == 0.0


# =============================================================================
# 2. Correct Grounded Edits & Patch Candidate Construction
# =============================================================================

def test_correct_grounded_edit_synthesizes_unified_diff(tmp_path: Path):
    """Verify construct_deterministic_repair produces a valid unified diff and candidate."""
    code = (
        "def compute_total(a: int, b: int) -> int:\n"
        '    """Compute total."""\n'
        "    result = a - b\n"
        "    return result\n"
    )
    repo, sha = _setup_repo_with_file(tmp_path, "calc.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="calc.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Assign",
        ast_node_id="calc.py:compute_total:Assign:3",
        symbol_scope="compute_total",
        start_line=3,
        end_line=3,
        original_statement="    result = a - b",
        finding_id="find_calc",
    )

    result = construct_deterministic_repair(
        grounded_rep=rep,
        replacement_content="    result = a + b",
        repo_path=repo,
    )

    assert result.is_valid is True
    assert result.unified_diff is not None
    assert "-    result = a - b" in result.unified_diff
    assert "+    result = a + b" in result.unified_diff
    assert result.ast_valid is True
    assert result.semantic_invariants_valid is True
    assert result.patch_candidate is not None
    assert result.patch_candidate.added_lines == 1
    assert result.patch_candidate.removed_lines == 1


# =============================================================================
# 3. Surrounding Code, Comments, and Guards Preservation
# =============================================================================

def test_preserves_surrounding_docstrings_and_guards(tmp_path: Path):
    """Ensure replacing anchored statement does not touch surrounding docstrings or validation guards."""
    code = (
        "def safe_divide(x: float, y: float) -> float:\n"
        '    """Safely divide x by y.\n'
        "\n"
        "    Guarantees: y != 0.\n"
        '    """\n'
        "    if y == 0:\n"
        '        raise ValueError("Divisor cannot be 0")\n'
        "    # Perform division\n"
        "    return x * y  # Defect: multiplied instead of divided\n"
    )
    repo, sha = _setup_repo_with_file(tmp_path, "math_utils.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="math_utils.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Return",
        ast_node_id="math_utils.py:safe_divide:Return:9",
        symbol_scope="safe_divide",
        start_line=9,
        end_line=9,
        original_statement="    return x * y  # Defect: multiplied instead of divided",
        finding_id="find_div",
    )

    result = construct_deterministic_repair(
        grounded_rep=rep,
        replacement_content="    return x / y",
        repo_path=repo,
    )

    assert result.is_valid is True
    diff = result.unified_diff
    # Docstring and guard must NOT appear as added/removed in diff
    assert "raise ValueError" in diff  # Context line preserved
    assert "Safely divide x by y" not in [line for line in diff.splitlines() if line.startswith(("+", "-"))]
    assert "+    raise ValueError" not in diff
    assert "-    raise ValueError" not in diff
    assert "-    return x * y" in diff
    assert "+    return x / y" in diff


# =============================================================================
# 4. Scope Violation Defense: Reject Function Header Redeclaration
# =============================================================================

def test_rejects_function_header_redeclaration_for_statement_anchor(tmp_path: Path):
    """Verify that proposing a 'def ...' block for a statement anchor is strictly rejected as a scope violation."""
    code = (
        "def evaluate(val: int) -> int:\n"
        "    if val < 0:\n"
        "        return 0\n"
        "    return val * 2\n"
    )
    repo, sha = _setup_repo_with_file(tmp_path, "eval.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="eval.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Return",
        ast_node_id="eval.py:evaluate:Return:3",
        symbol_scope="evaluate",
        start_line=3,
        end_line=3,
        original_statement="        return 0",
        finding_id="find_eval",
    )

    # Model attempts to redeclare the entire function
    faulty_replacement = (
        "def evaluate(val: int) -> int:\n"
        "    if val < 0:\n"
        '        raise ValueError("negative")\n'
        "    return val * 2"
    )

    result = construct_deterministic_repair(
        grounded_rep=rep,
        replacement_content=faulty_replacement,
        repo_path=repo,
    )

    assert result.is_valid is False
    assert any("scope violation" in r.lower() for r in result.rejection_reasons)
    assert any("defines a function/class" in r.lower() for r in result.rejection_reasons)


# =============================================================================
# 5. Indentation Preservation Defense
# =============================================================================

def test_rejects_improper_indentation_without_normalization(tmp_path: Path):
    """Verify that stripped or unindented replacement content is rejected without silent normalization."""
    code = (
        "def nested_worker(items: list) -> list:\n"
        "    result = []\n"
        "    for item in items:\n"
        "        if item > 0:\n"
        "            result.append(item * 1)\n"
        "    return result\n"
    )
    repo, sha = _setup_repo_with_file(tmp_path, "worker.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="worker.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Expr",
        ast_node_id="worker.py:nested_worker:Expr:5",
        symbol_scope="nested_worker",
        start_line=5,
        end_line=5,
        original_statement="            result.append(item * 1)",
        finding_id="find_worker",
    )

    # Only 4 spaces instead of required 12
    unindented_replacement = "    result.append(item * 2)"

    result = construct_deterministic_repair(
        grounded_rep=rep,
        replacement_content=unindented_replacement,
        repo_path=repo,
    )

    assert result.is_valid is False
    assert any("indentation mismatch" in r.lower() for r in result.rejection_reasons)


# =============================================================================
# 6. No-Op, Identical, and Whitespace-Only Rejection
# =============================================================================

def test_rejects_empty_and_noop_replacements(tmp_path: Path):
    """Verify rejection of empty, whitespace, and identical replacements."""
    code = "def f(x):\n    return x + 1\n"
    repo, sha = _setup_repo_with_file(tmp_path, "f.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="f.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Return",
        ast_node_id="f.py:f:Return:2",
        symbol_scope="f",
        start_line=2,
        end_line=2,
        original_statement="    return x + 1",
        finding_id="find_f",
    )

    # Empty
    res_empty = construct_deterministic_repair(rep, "", repo)
    assert res_empty.is_valid is False
    assert any("empty" in r.lower() for r in res_empty.rejection_reasons)

    # Whitespace only
    res_ws = construct_deterministic_repair(rep, "   \n\t ", repo)
    assert res_ws.is_valid is False
    assert any("empty or whitespace" in r.lower() for r in res_ws.rejection_reasons)

    # Identical (no-op)
    res_noop = construct_deterministic_repair(rep, "    return x + 1", repo)
    assert res_noop.is_valid is False
    assert any("identical" in r.lower() or "no-op" in r.lower() for r in res_noop.rejection_reasons)


# =============================================================================
# 7. Syntax Verification & Semantic Invariants
# =============================================================================

def test_rejects_malformed_syntax(tmp_path: Path):
    """Verify rejection when replacement content introduces a Python syntax error."""
    code = "def g(x):\n    return x\n"
    repo, sha = _setup_repo_with_file(tmp_path, "g.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="g.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Return",
        ast_node_id="g.py:g:Return:2",
        symbol_scope="g",
        start_line=2,
        end_line=2,
        original_statement="    return x",
        finding_id="find_g",
    )

    res_syntax = construct_deterministic_repair(rep, "    return x + * 3", repo)
    assert res_syntax.is_valid is False
    assert res_syntax.ast_valid is False
    assert any("syntax" in r.lower() for r in res_syntax.rejection_reasons)


def test_rejects_semantic_invariant_violations(tmp_path: Path):
    """Verify rejection when replacement introduces unreachable code."""
    code = (
        "def process(x: int) -> int:\n"
        "    val = x * 2\n"
        "    return val\n"
    )
    repo, sha = _setup_repo_with_file(tmp_path, "p.py", code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

    rep = GroundedRepairRepresentation(
        target_file="p.py",
        base_commit_sha=sha,
        source_digest=digest,
        ast_node_type="Assign",
        ast_node_id="p.py:process:Assign:2",
        symbol_scope="process",
        start_line=2,
        end_line=2,
        original_statement="    val = x * 2",
        finding_id="find_p",
    )

    # Replacing val = x * 2 with return 0 makes return val unreachable
    unreachable_repl = "    return 0"
    res_unreach = construct_deterministic_repair(rep, unreachable_repl, repo)
    assert res_unreach.is_valid is False
    assert res_unreach.semantic_invariants_valid is False
    assert any("unreachable" in r.lower() or "semantic invariant" in r.lower() for r in res_unreach.rejection_reasons)


# =============================================================================
# 8. Stale Source Digest & Revision Defense
# =============================================================================

def test_rejects_stale_source_digest(tmp_path: Path):
    """Verify that constructing repair against modified file fails closed with stale revision error."""
    code = "def h(x):\n    return x\n"
    repo, sha = _setup_repo_with_file(tmp_path, "h.py", code)

    rep = GroundedRepairRepresentation(
        target_file="h.py",
        base_commit_sha=sha,
        source_digest="0000000000000000000000000000000000000000000000000000000000000000",
        ast_node_type="Return",
        ast_node_id="h.py:h:Return:2",
        symbol_scope="h",
        start_line=2,
        end_line=2,
        original_statement="    return x",
        finding_id="find_h",
    )

    res = construct_deterministic_repair(rep, "    return x * 2", repo)
    assert res.is_valid is False
    assert any("stale revision" in r.lower() for r in res.rejection_reasons)


# =============================================================================
# 9. GEN07 Generalization Fixture: Nested Statement Anchoring
# =============================================================================

def test_gen07_nested_statement_repair_and_scope_rejection(tmp_path: Path):
    """Test GEN07 fixture: statement grounding, out-of-scope rejection, and verified scratch execution."""
    repo, meta, finding, evidence = _setup_gen07_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # 1. Ground the repair representation
    rep = build_grounded_repair_representation(
        repo_path=repo,
        target_file=meta["target_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert rep.is_resolved is True
    assert rep.symbol_scope == "BackoffPolicy::compute_delay"
    assert rep.ast_node_type == "Assign"
    assert "calculated_delay = self.base_delay * attempt" in rep.original_statement

    # 2. Out-of-scope proposal (model attempts to redeclare def compute_delay)
    out_of_scope_res = construct_deterministic_repair(
        grounded_rep=rep,
        replacement_content=meta["out_of_scope_replace_block"],
        repo_path=repo,
    )
    assert out_of_scope_res.is_valid is False
    assert any("scope violation" in r.lower() for r in out_of_scope_res.rejection_reasons)

    # 3. Valid statement-level replacement
    valid_res = construct_deterministic_repair(
        grounded_rep=rep,
        replacement_content=meta["valid_replace_block"],
        repo_path=repo,
    )
    assert valid_res.is_valid is True
    assert valid_res.unified_diff is not None

    # 4. Verify in disposable scratch workspace
    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    try:
        ok, err = apply_unified_diff_to_file(scratch / meta["target_file"], valid_res.unified_diff)
        assert ok is True

        res = subprocess.run(
            [sys.executable, "-m", "unittest", meta["test_file"]],
            cwd=scratch,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        assert "OK" in res.stderr
    finally:
        cleanup()


# =============================================================================
# 10. End-to-End RepairPlanner Integration & Retry Feedback
# =============================================================================

def test_planner_uses_grounded_rep_and_recovers_on_retry(tmp_path: Path):
    """Verify RepairPlanner uses deterministic construction and guides model past scope violations."""
    repo, meta, finding, evidence = _setup_gen07_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Attempt 1 returns out-of-scope function header
    resp1_payload = {
        "root_cause": "Linear instead of exponential backoff",
        "proposed_change": "Redeclare method with exponential formula",
        "target_file": meta["target_file"],
        "search_block": "def compute_delay(self, attempt: int) -> float:\n    pass",
        "replace_block": meta["out_of_scope_replace_block"],
        "expected_behavior": "Exponential backoff progression",
        "regression_risks": [],
        "verification_plan": "python3 -m unittest tests/test_backoff.py",
        "confidence": 0.9,
    }

    # Attempt 2 responds to feedback and returns statement-level replacement
    resp2_payload = {
        "root_cause": "Linear instead of exponential backoff",
        "proposed_change": "Fix statement with exponential power formula",
        "target_file": meta["target_file"],
        "search_block": meta["valid_search_block"],
        "replace_block": meta["valid_replace_block"],
        "expected_behavior": "Exponential backoff progression",
        "regression_risks": [],
        "verification_plan": "python3 -m unittest tests/test_backoff.py",
        "confidence": 0.95,
    }

    class TwoStageProvider(LLMProvider):
        def __init__(self):
            self.call_count = 0

        def generate(self, request: LLMRequest) -> LLMResponse:
            self.call_count += 1
            if self.call_count == 1:
                return LLMResponse(content=json.dumps(resp1_payload), model_name="two-stage-test")
            return LLMResponse(content=json.dumps(resp2_payload), model_name="two-stage-test")

        def get_provider_name(self) -> str:
            return "two-stage-test"

    provider = TwoStageProvider()
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    assert provider.call_count == 2
    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.patch_candidate is not None
    assert "-        calculated_delay = self.base_delay * attempt" in plan.patch_candidate.unified_diff
    assert "+        calculated_delay = self.base_delay * (2 ** attempt)" in plan.patch_candidate.unified_diff
