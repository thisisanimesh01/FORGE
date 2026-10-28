"""Unit and regression tests for FORGE Repair Reliability & Generalization.

Validates:
1. GEN01 (Incorrect logic): Valid repair verified; plausible incorrect repair rejected by verification oracle.
2. GEN02 (Edge-case failure): Empty collection IndexError defect localized and verified cleanly in scratch workspace.
3. GEN03 (Plausible incorrect repair rejected): Clamping instead of raising exception rejected by semantic safeguards; guard stripping rejected.
4. GEN04 (Whitespace-sensitive edits): Exact 16-space indentation required; stripped indentation rejected without silent normalization.
5. GEN05 (Malformed & semantically wrong proposals): Syntax error, no-op, and out-of-scope targets rejected with actionable diagnostics.
6. Diagnostic propagation: Planner propagates anchor grounding reasons and indentation guidance in retry feedback.
7. Local Ollama evaluation: Evaluates actual local model behavior with cloud fallback disabled and records honest metrics.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional
import pytest

from forge.config import get_settings
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.llm.provider import LLMProvider
from forge.repair.anchoring import (
    AnchorResolutionStatus,
    EditAnchorContract,
    construct_patch_from_edit_anchor,
    resolve_source_anchor,
    validate_anchor_replacement_semantics,
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

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "generalization_fixtures"


def _setup_fixture_repo(tmp_path: Path, fixture_name: str) -> tuple[Path, dict, Finding, List[EvidenceRecord]]:
    """Create an isolated, pinned git repository from a generalization fixture."""
    fixture_dir = FIXTURES_DIR / fixture_name
    assert fixture_dir.is_dir(), f"Fixture directory not found: {fixture_dir}"

    metadata_path = fixture_dir / "fixture.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    repo = tmp_path / fixture_name
    repo.mkdir(parents=True)

    # Copy src files
    src_dir = fixture_dir / "src"
    target_src = repo / "src"
    target_src.mkdir(parents=True)
    for f in src_dir.glob("*.py"):
        (target_src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    # Copy tests files
    tests_dir = fixture_dir / "tests"
    target_tests = repo / "tests"
    target_tests.mkdir(parents=True)
    for f in tests_dir.glob("*.py"):
        (target_tests / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    # Initialize Git
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"Initial commit for {fixture_name}"], cwd=repo, check=True, capture_output=True)

    finding = Finding(
        finding_id=f"find_{fixture_name}",
        title=metadata["name"],
        description=metadata["description"],
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.CONFIRMED,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=[metadata["target_file"], metadata["test_file"]],
    )

    test_line = 6
    for f in tests_dir.glob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    if node.body:
                        test_line = node.body[0].lineno
                        break
        except Exception:
            pass

    evidence = [
        EvidenceRecord(
            evidence_id=f"evi_{fixture_name}",
            run_id=f"run_{fixture_name}",
            finding_id=finding.finding_id,
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description=f"Reproduction failed for {fixture_name}",
            content=(
                f"Reproduction Command: {metadata['reproduction_command']}\n"
                f"Traceback (most recent call last):\n"
                f'  File "/workspace/{metadata["test_file"]}", line {test_line}\n'
                f"  AssertionError: {metadata['expected_failure_pattern']}\n"
            ),
            details={"matched_patterns": [metadata["expected_failure_pattern"]]},
        )
    ]

    return repo, metadata, finding, evidence


# =============================================================================
# 1. GEN01: Incorrect Logic (Discount Math)
# =============================================================================

def test_gen01_incorrect_logic_valid_and_plausible_incorrect(tmp_path: Path):
    """GEN01: Valid math fix verified; plausible incorrect repair rejected by verification oracle."""
    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN01_incorrect_logic")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # 1. Deterministic anchor resolution
    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file=meta["target_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
        allowed_files=[meta["target_file"], meta["test_file"]],
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED
    assert "return price +" in anchor.anchor_text

    # 2. Valid repair construction
    valid, diff, reasons, metrics = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["valid_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid is True
    assert reasons == []
    assert "-    return price +" in diff
    assert "+    return price -" in diff

    # 3. Verify in disposable scratch workspace
    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    try:
        ok, app_err = apply_unified_diff_to_file(scratch / meta["target_file"], diff)
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

    # 4. Plausible but incorrect repair (computes discount only, yielding 20.0 instead of 80.0)
    valid_bad, diff_bad, _, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["plausible_incorrect_replace_block"],
        repo_path=repo,
    )
    assert valid_bad is True

    scratch_bad, cleanup_bad = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    try:
        apply_unified_diff_to_file(scratch_bad / meta["target_file"], diff_bad)
        res_bad = subprocess.run(
            [sys.executable, "-m", "unittest", meta["test_file"]],
            cwd=scratch_bad,
            capture_output=True,
            text=True,
        )
        # Verification oracle catches plausible incorrect repair!
        assert res_bad.returncode != 0
        assert "AssertionError" in res_bad.stderr
    finally:
        cleanup_bad()


# =============================================================================
# 2. GEN02: Edge-Case Failure (Empty Collection)
# =============================================================================

def test_gen02_edge_case_empty_collection(tmp_path: Path):
    """GEN02: Empty collection defect localized and verified cleanly in scratch workspace."""
    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN02_edge_case_failure")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file=meta["target_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED
    assert "len(skus) <= 0" in anchor.anchor_text

    valid, diff, reasons, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["valid_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid is True
    assert 'return "UNAVAILABLE"' in diff

    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    try:
        ok, _ = apply_unified_diff_to_file(scratch / meta["target_file"], diff)
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
# 3. GEN03: Plausible Incorrect Repair Rejected by Semantic Safeguards
# =============================================================================

def test_gen03_plausible_incorrect_rejected_by_semantic_safeguard(tmp_path: Path):
    """GEN03: Clamping instead of raising ValueError is rejected by semantic safeguard; guard stripping rejected."""
    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN03_plausible_incorrect_rejected")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file=meta["target_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    # Case A: Plausible incorrect repair (returns -273.15 without raising ValueError)
    valid_clamped, _, reasons_clamped, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["plausible_incorrect_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid_clamped is False
    assert any("Semantic safety violation" in r and "ValueError not raised" in r for r in reasons_clamped)

    # Case B: Unconditional return (strips the 'if' guard)
    valid_strip, _, reasons_strip, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["unconditional_return_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid_strip is False
    assert any("removing the guard entirely" in r for r in reasons_strip)

    # Case C: Valid fix (raises ValueError)
    valid_ok, diff_ok, reasons_ok, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["valid_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid_ok is True
    assert reasons_ok == []
    assert "+        raise ValueError" in diff_ok

    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    try:
        apply_unified_diff_to_file(scratch / meta["target_file"], diff_ok)
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
# 4. GEN04: Whitespace-Sensitive Edits (Nested Indentation Preservation)
# =============================================================================

def test_gen04_whitespace_sensitive_nested_indentation(tmp_path: Path):
    """GEN04: Exact 16-space indentation required; stripped indentation rejected without silent normalization."""
    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN04_whitespace_sensitive")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file=meta["target_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED
    assert anchor.anchor_text.startswith("                ")  # 16 spaces

    # Case A: Stripped 0-space indentation must be rejected without silent normalization
    valid_stripped, _, reasons_stripped, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["stripped_indentation_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid_stripped is False
    assert any("does not match anchor indentation (16 spaces)" in r for r in reasons_stripped)

    # Case B: Exact 16-space indentation accepted
    valid_ok, diff_ok, reasons_ok, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["valid_replace_block"],
        repo_path=repo,
        evidence_records=evidence,
        finding=finding,
    )
    assert valid_ok is True
    assert reasons_ok == []

    scratch, cleanup = prepare_disposable_workspace(target_repo=repo, base_commit_sha=sha, scratch_parent=tmp_path)
    try:
        apply_unified_diff_to_file(scratch / meta["target_file"], diff_ok)
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
# 5. GEN05: Malformed & Semantically Wrong Proposals
# =============================================================================

def test_gen05_malformed_proposals_fail_closed_with_diagnostics(tmp_path: Path):
    """GEN05: Syntax error, no-op, and out-of-scope targets rejected with actionable diagnostics."""
    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN05_malformed_semantically_wrong")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    anchor = resolve_source_anchor(
        repo_path=repo,
        target_file=meta["target_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )
    assert anchor.status == AnchorResolutionStatus.RESOLVED

    # Case A: Syntax error proposal (missing colon in Python syntax)
    valid_syn, _, reasons_syn, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["syntax_error_replace_block"],
        repo_path=repo,
    )
    assert valid_syn is False
    assert any("syntax error" in r.lower() for r in reasons_syn)

    # Case B: No-op proposal (identical to anchor)
    valid_noop, _, reasons_noop, _ = construct_patch_from_edit_anchor(
        anchor=anchor,
        replacement_content=meta["noop_replace_block"],
        repo_path=repo,
    )
    assert valid_noop is False
    assert any("identical" in r.lower() or "no-op" in r.lower() for r in reasons_noop)

    # Case C: Out-of-scope target file
    anchor_out = resolve_source_anchor(
        repo_path=repo,
        target_file=meta["out_of_scope_file"],
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
        allowed_files=[meta["target_file"], meta["test_file"]],
    )
    assert anchor_out.status == AnchorResolutionStatus.SCOPE_VIOLATION
    assert "outside declared allowed scope" in anchor_out.rejection_reason.lower()


# =============================================================================
# 6. Diagnostic Propagation in Planner Retry Loop
# =============================================================================

def test_planner_propagates_anchor_grounding_reasons_in_retries(tmp_path: Path):
    """Planner propagates exact anchor failure reasons (semantic safety / indentation) to attempt violations."""
    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN03_plausible_incorrect_rejected")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Response 1: Bad search block omitting docstrings, with plausible incorrect replace block (clamped)
    resp1 = json.dumps({
        "root_cause": "Sub-zero temperature handling",
        "proposed_change": "Clamp temperature",
        "target_file": meta["target_file"],
        "search_block": "def check_temperature_threshold(celsius: float) -> str:\n    return 'CLIPPED_LOW'",
        "replace_block": meta["plausible_incorrect_replace_block"],
        "change_rationale": "Clamp value",
        "expected_behavior": "Clamped temperature",
        "verification_plan": "python3 -m unittest",
        "unified_diff": None,
    })

    # Response 2: Correct search and replace block with ValueError
    resp2 = json.dumps({
        "root_cause": "Sub-zero temperature handling",
        "proposed_change": "Raise ValueError",
        "target_file": meta["target_file"],
        "search_block": meta["valid_search_block"],
        "replace_block": meta["valid_replace_block"],
        "change_rationale": "Raise ValueError on physical impossibility",
        "expected_behavior": "Raises ValueError",
        "verification_plan": "python3 -m unittest",
        "unified_diff": None,
    })

    class ScriptedProvider(LLMProvider):
        def __init__(self, responses: List[str]):
            self.responses = responses
            self.call_count = 0
            self.requests: List[LLMRequest] = []

        def generate(self, request: LLMRequest) -> LLMResponse:
            idx = min(self.call_count, len(self.responses) - 1)
            self.call_count += 1
            self.requests.append(request)
            return LLMResponse(content=self.responses[idx], model_name="test-scripted")

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

    # Check that second request received the propagated semantic safety violation!
    second_request = provider.requests[1]
    last_user_msg = second_request.messages[-1].content
    assert "Semantic safety violation" in last_user_msg or "ValueError not raised" in last_user_msg


# =============================================================================
# 7. Local Ollama Evaluation (Cloud Fallback Disabled)
# =============================================================================

def _is_local_ollama_available() -> bool:
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(
    not _is_local_ollama_available(),
    reason="Local Ollama is not accessible on this machine",
)
def test_local_ollama_evaluation_with_cloud_fallback_disabled(tmp_path: Path):
    """Evaluate local Ollama on generalization fixtures with cloud fallback strictly disabled."""
    settings = get_settings()
    assert settings.allow_cloud_fallback is False, "Cloud fallback must be disabled"

    repo, meta, finding, evidence = _setup_fixture_repo(tmp_path, "GEN01_incorrect_logic")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    planner = RepairPlanner(
        provider_name="ollama",
        model_name="qwen2.5-coder:1.5b",
        max_retries=1,
    )

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha=sha,
        finding=finding,
        evidence_records=evidence,
    )

    # The plan outcome must be either VALIDATED (if the model gave exact indentation and fix)
    # or REJECTED (if the model failed verbatim matching / indentation / semantic checks).
    # It must NEVER succeed via silent normalization or unauthorized cloud fallback.
    assert plan.status in (RepairPlanStatus.VALIDATED, RepairPlanStatus.REJECTED)
    assert plan.provider == "ollama"
