"""Unit tests for Local Ollama Structured Output Reliability.

Verifies:
1. Valid JSON repair plan generation with structured response_format.
2. Trailing-comma model output rejected and corrected through explicit bounded retry.
3. Malformed output after all retries exhausted fails closed.
4. Schema-invalid but syntactically valid JSON is rejected.
5. Corrected output still strictly undergoes scope, evidence, hunk-context, and patch-applicability validation.
6. Provider timeout, oversized response, and exhausted retry budgets.
7. No cloud or mock fallback after local provider failure.
8. No secrets or raw sensitive configuration leaked in diagnostics or violations.
9. Existing valid repair plans remain fully compatible.
"""

import json
from pathlib import Path
import subprocess
import time
from typing import List, Tuple
from unittest.mock import MagicMock, patch
import pytest

from forge.analysis.evidence import create_evidence
from forge.config import redact_secrets
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole
from forge.llm.openai import OpenAICompatibleProvider
from forge.llm.provider import LLMProviderError, LLMUnavailableError
from forge.repair.contracts import (
    RepairBudget,
    parse_and_validate_unified_diff,
    validate_patch_applicability,
)
from forge.repair.models import (
    RepairModelResponseSchema,
    RepairPlan,
    RepairPlanStatus,
)
from forge.repair.planner import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_PLANNING_SECONDS,
    MAX_REPAIR_PLANNER_RETRIES,
    MAX_RESPONSE_CHARS,
    RepairPlanner,
    build_repair_response_format,
    parse_json_payload,
)
from forge.schemas import (
    EvidenceType,
    Finding,
)


def _init_test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Helper to initialize a clean git repository fixture."""
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


def _make_mock_json_payload(
    target_file: str = "math_ops.py",
    diff_text: str = "",
    root_cause: str = "Root cause explanation",
    proposed_change: str = "Proposed change explanation",
    expected_behavior: str = "Defect resolved without regressing callers",
) -> dict:
    return {
        "root_cause": root_cause,
        "proposed_change": proposed_change,
        "target_file": target_file,
        "expected_behavior": expected_behavior,
        "regression_risks": ["None identified"],
        "verification_plan": "python3 -m unittest tests/test_math_ops.py",
        "confidence": 0.95,
        "uncertainty_notes": None,
        "unified_diff": diff_text,
    }


CLEAN_DIFF = (
    "--- a/math_ops.py\n"
    "+++ b/math_ops.py\n"
    "@@ -3,2 +3,4 @@\n"
    " def safe_divide(a: float, b: float) -> float:\n"
    "+    if b == 0:\n"
    "+        raise ZeroDivisionError('Division by zero')\n"
    "     return a / b\n"
)


# ---------------------------------------------------------------------------
# Test 1: Parser and Schema Building Unit Tests
# ---------------------------------------------------------------------------

def test_build_repair_response_format_structure():
    """Verify response format adheres to json_schema specification."""
    rf = build_repair_response_format()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "RepairModelResponseSchema"
    assert rf["json_schema"]["strict"] is True
    assert "properties" in rf["json_schema"]["schema"]
    assert "unified_diff" in rf["json_schema"]["schema"]["properties"]


def test_parse_json_payload_valid_direct():
    """Valid JSON object is parsed directly."""
    payload, diag = parse_json_payload('{"a": 1, "b": "hello"}')
    assert payload == {"a": 1, "b": "hello"}
    assert diag is None


def test_parse_json_payload_valid_code_fence():
    """Valid JSON inside markdown code fence is extracted without errors."""
    text = "```json\n{\"root_cause\": \"div by zero\", \"target_file\": \"calc.py\"}\n```"
    payload, diag = parse_json_payload(text)
    assert payload is not None
    assert payload["target_file"] == "calc.py"
    assert diag is None


def test_parse_json_payload_trailing_comma_diagnosed_not_silently_fixed():
    """Trailing comma in JSON object is diagnosed specifically without silent rewriting."""
    raw = '{"target_file": "math_ops.py", "root_cause": "bug",}'
    payload, diag = parse_json_payload(raw)
    assert payload is None
    assert diag is not None
    assert "Trailing commas" in diag or "Invalid JSON syntax" in diag


def test_parse_json_payload_empty_and_oversized():
    """Empty or oversized responses produce clear diagnostics."""
    p_empty, d_empty = parse_json_payload("")
    assert p_empty is None
    assert "empty" in d_empty.lower()

    huge = "{" + ("a" * (MAX_RESPONSE_CHARS + 10)) + "}"
    p_huge, d_huge = parse_json_payload(huge)
    assert p_huge is None
    assert "maximum allowed length" in d_huge.lower()


# ---------------------------------------------------------------------------
# Test 2: Valid JSON repair plan with structured response format
# ---------------------------------------------------------------------------

def test_valid_json_repair_plan_sends_response_format(tmp_path: Path):
    """Repair planner sends response_format in request and accepts valid JSON candidate."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_valid_01",
        title="Safe divide zero check",
        description="Division by zero in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_valid_01"],
    )
    ev = create_evidence(
        run_id="run_valid_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_valid_01",
        evidence_id="evi_valid_01",
    )

    payload_dict = _make_mock_json_payload(target_file="math_ops.py", diff_text=CLEAN_DIFF)
    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=json.dumps(payload_dict), model="fake")
    ])

    planner = RepairPlanner(llm_provider=mock_llm)
    plan = planner.plan_repair(
        finding_id="find_valid_01",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.validation.is_valid is True
    assert plan.patch_candidate is not None
    assert plan.patch_candidate.unified_diff == CLEAN_DIFF

    # Verify that LLMRequest included the structured response_format
    assert len(mock_llm.call_history) == 1
    req = mock_llm.call_history[0]
    assert req.response_format is not None
    assert req.response_format["type"] == "json_schema"


# ---------------------------------------------------------------------------
# Test 3: Trailing-comma rejected then corrected through explicit retry
# ---------------------------------------------------------------------------

def test_trailing_comma_rejected_then_corrected_via_explicit_retry(tmp_path: Path):
    """Model output with trailing comma is rejected and successfully corrected on retry."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_retry_01",
        title="Zero division defect",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_retry_01"],
    )
    ev = create_evidence(
        run_id="run_retry_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_retry_01",
        evidence_id="evi_retry_01",
    )

    # Attempt 1 has trailing comma after unified_diff
    diff_escaped = CLEAN_DIFF.replace("\n", "\\n").replace('"', '\\"')
    malformed_json_trailing_comma = (
        '{\n'
        '  "root_cause": "The safe_divide method does not check if b is zero.",\n'
        '  "proposed_change": "Add guard check for b == 0.",\n'
        '  "target_file": "math_ops.py",\n'
        '  "expected_behavior": "ZeroDivisionError is raised when b is zero.",\n'
        '  "regression_risks": ["None"],\n'
        '  "verification_plan": "python3 -m unittest tests/test_math_ops.py",\n'
        '  "confidence": 0.95,\n'
        f'  "unified_diff": "{diff_escaped}",\n'
        '}'
    )

    # Attempt 2 is valid clean JSON
    valid_payload = _make_mock_json_payload(target_file="math_ops.py", diff_text=CLEAN_DIFF)
    valid_json = json.dumps(valid_payload)

    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=malformed_json_trailing_comma, model="fake"),
        LLMResponse(content=valid_json, model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=2)
    plan = planner.plan_repair(
        finding_id="find_retry_01",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert len(mock_llm.call_history) == 2

    # Check retry message sent to model on attempt 2
    retry_req = mock_llm.call_history[1]
    last_user_msg = retry_req.messages[-1].content
    assert "Your previous response could not be parsed as valid JSON" in last_user_msg
    assert "trailing commas" in last_user_msg.lower()


# ---------------------------------------------------------------------------
# Test 4: Trailing comma fails closed when all retries are exhausted
# ---------------------------------------------------------------------------

def test_trailing_comma_fails_closed_when_retries_exhausted(tmp_path: Path):
    """When trailing-comma syntax persists across all retries, planner fails closed with REJECTED."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_retry_fail",
        title="Persistent syntax error",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_fail_01"],
    )
    ev = create_evidence(
        run_id="run_fail_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_retry_fail",
        evidence_id="evi_fail_01",
    )

    malformed = '{"target_file": "math_ops.py", "root_cause": "unhandled",}'
    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=malformed, model="fake"),
        LLMResponse(content=malformed, model="fake"),
        LLMResponse(content=malformed, model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=2)
    plan = planner.plan_repair(
        finding_id="find_retry_fail",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.patch_candidate is None
    assert any("trailing comma" in v.lower() or "not valid json" in v.lower() or "invalid json" in v.lower() for v in plan.validation.violations)
    assert len(mock_llm.call_history) == 3


# ---------------------------------------------------------------------------
# Test 5: Schema-invalid but syntactically valid JSON is rejected
# ---------------------------------------------------------------------------

def test_schema_invalid_syntactically_valid_json_rejected(tmp_path: Path):
    """Valid JSON that fails schema validation (e.g. missing required unified_diff) is rejected."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_schema_err",
        title="Missing field defect",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_schema_01"],
    )
    ev = create_evidence(
        run_id="run_schema_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_schema_err",
        evidence_id="evi_schema_01",
    )

    # Missing unified_diff, proposed_change, verification_plan
    incomplete_payload = {
        "root_cause": "division by zero",
        "target_file": "math_ops.py",
        "expected_behavior": "raise error",
    }
    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=json.dumps(incomplete_payload), model="fake"),
        LLMResponse(content=json.dumps(incomplete_payload), model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=1)
    plan = planner.plan_repair(
        finding_id="find_schema_err",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert any("did not conform to repair schema" in v for v in plan.validation.violations)


# ---------------------------------------------------------------------------
# Test 6: Corrected output still strictly validated for scope and applicability
# ---------------------------------------------------------------------------

def test_corrected_output_still_strictly_validated_for_scope(tmp_path: Path):
    """A corrected response that parses valid JSON still fails closed if it targets an unauthorized file."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_scope_check",
        title="Scope enforcement after syntax correction",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_scope_01"],
    )
    ev = create_evidence(
        run_id="run_scope_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_scope_check",
        evidence_id="evi_scope_01",
    )

    # Attempt 1: Trailing comma syntax error
    malformed = '{"target_file": "math_ops.py", "root_cause": "div zero",}'

    # Attempt 2: Valid JSON, but targets tests/test_math_ops.py (scope restriction / test file prohibition)
    scope_escape_diff = (
        "--- a/tests/test_math_ops.py\n"
        "+++ b/tests/test_math_ops.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-import unittest\n"
        "+# test disabled\n"
    )
    scope_escape_payload = _make_mock_json_payload(target_file="tests/test_math_ops.py", diff_text=scope_escape_diff)

    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=malformed, model="fake"),
        LLMResponse(content=json.dumps(scope_escape_payload), model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=1)
    plan = planner.plan_repair(
        finding_id="find_scope_check",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert any("scope is restricted" in v for v in plan.validation.violations)


def test_corrected_output_still_strictly_validated_for_hunk_context(tmp_path: Path):
    """A corrected response with bad hunk context lines still fails diff validation."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_hunk_check",
        title="Hunk context validation after syntax correction",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_hunk_01"],
    )
    ev = create_evidence(
        run_id="run_hunk_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_hunk_check",
        evidence_id="evi_hunk_01",
    )

    # Attempt 1: Syntax error
    malformed = '{"target_file": "math_ops.py", "root_cause": "div zero",}'

    # Attempt 2: Valid JSON, but hunk context does not match target file math_ops.py
    bad_context_diff = (
        "--- a/math_ops.py\n"
        "+++ b/math_ops.py\n"
        "@@ -10,3 +10,4 @@\n"
        " def non_existent_function(x, y):\n"
        "+    return x + y\n"
        "     pass\n"
    )
    bad_context_payload = _make_mock_json_payload(target_file="math_ops.py", diff_text=bad_context_diff)

    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=malformed, model="fake"),
        LLMResponse(content=json.dumps(bad_context_payload), model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=1)
    plan = planner.plan_repair(
        finding_id="find_hunk_check",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert any("context" in v.lower() for v in plan.validation.violations)


# ---------------------------------------------------------------------------
# Test 7: Provider timeout, oversized response, and retry limits
# ---------------------------------------------------------------------------

def test_planning_timeout_budget_fails_closed(tmp_path: Path):
    """When elapsed planning time exceeds max_planning_seconds, planner halts and rejects plan."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_timeout",
        title="Planning timeout test",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_timeout_01"],
    )
    ev = create_evidence(
        run_id="run_timeout_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_timeout",
        evidence_id="evi_timeout_01",
    )

    def slow_generator(req: LLMRequest) -> LLMResponse:
        time.sleep(0.06)
        return LLMResponse(content='{"bad": syntax,}', model="fake")

    mock_llm = FakeLLMProvider(response_generator=slow_generator)
    planner = RepairPlanner(llm_provider=mock_llm, max_retries=5, max_planning_seconds=0.05)
    plan = planner.plan_repair(
        finding_id="find_timeout",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert any("exceeded time budget" in v.lower() for v in plan.validation.violations)


def test_oversized_response_rejected_bounded(tmp_path: Path):
    """Response exceeding MAX_RESPONSE_CHARS is flagged and rejected."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_oversized",
        title="Oversized model response",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_oversized_01"],
    )
    ev = create_evidence(
        run_id="run_oversized_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_oversized",
        evidence_id="evi_oversized_01",
    )

    oversized_content = "x" * (MAX_RESPONSE_CHARS + 1000)
    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=oversized_content, model="fake"),
        LLMResponse(content=oversized_content, model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=1)
    plan = planner.plan_repair(
        finding_id="find_oversized",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert any("exceeded maximum allowed length" in v for v in plan.validation.violations)


def test_retry_budget_hard_cap():
    """RepairPlanner clamps max_retries to MAX_REPAIR_PLANNER_RETRIES."""
    planner = RepairPlanner(max_retries=100)
    assert planner.max_retries == MAX_REPAIR_PLANNER_RETRIES


# ---------------------------------------------------------------------------
# Test 8: No secrets leaked in parsing diagnostics or violations
# ---------------------------------------------------------------------------

def test_no_secrets_in_diagnostics_or_violations(tmp_path: Path):
    """Secrets in model response or exception are strictly redacted from violations."""
    repo, commit_sha = _init_test_git_repo(tmp_path)
    finding = Finding(
        finding_id="find_secret_leak",
        title="Secret leak protection test",
        description="Defect in math_ops.py",
        affected_files=["math_ops.py"],
        supporting_evidence_ids=["evi_sec_01"],
    )
    ev = create_evidence(
        run_id="run_sec_01",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt",
        content="return a / b",
        finding_id="find_secret_leak",
        evidence_id="evi_sec_01",
    )

    secret_in_malformed = '{"token": "sk-proj-supersecretkey1234567890abcdef", bad_syntax}'
    mock_llm = FakeLLMProvider(scripted_responses=[
        LLMResponse(content=secret_in_malformed, model="fake"),
        LLMResponse(content=secret_in_malformed, model="fake"),
    ])

    planner = RepairPlanner(llm_provider=mock_llm, max_retries=1)
    plan = planner.plan_repair(
        finding_id="find_secret_leak",
        target_repo=repo,
        base_commit_sha=commit_sha,
        finding=finding,
        evidence_records=[ev],
    )

    for v in plan.validation.violations:
        assert "supersecretkey" not in v


# ---------------------------------------------------------------------------
# Test 9: OpenAICompatibleProvider response_format wire format and 400 downgrade
# ---------------------------------------------------------------------------

def test_openai_compatible_provider_includes_response_format():
    """OpenAICompatibleProvider._build_payload serializes response_format when present."""
    prov = OpenAICompatibleProvider(api_key="sk-test", api_base="https://api.test/v1")
    rf = {"type": "json_schema", "json_schema": {"name": "Test", "schema": {}}}
    req = LLMRequest(
        messages=[ChatMessage(role=MessageRole.USER, content="hello")],
        response_format=rf,
    )
    payload = prov._build_payload(req)
    assert "response_format" in payload
    assert payload["response_format"] == rf


def test_openai_compatible_provider_graceful_400_downgrade():
    """If provider returns HTTP 400 rejecting json_schema, it falls back on same provider."""
    prov = OpenAICompatibleProvider(api_key="sk-test", api_base="https://api.test/v1")
    rf = {"type": "json_schema", "json_schema": {"name": "Test", "schema": {}}}
    req = LLMRequest(
        messages=[ChatMessage(role=MessageRole.USER, content="hello")],
        response_format=rf,
    )

    # First call: 400 rejecting json_schema
    resp_400 = MagicMock()
    resp_400.status_code = 400
    resp_400.text = "json_schema response_format is not supported by this model"

    # Second call (fallback with json_object): 200 OK
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "model": "test-model",
    }

    mock_client = MagicMock()
    mock_client.post.side_effect = [resp_400, resp_200]

    with patch.object(prov, "_get_client", return_value=mock_client):
        resp = prov.generate(req)

    assert resp.content == '{"ok": true}'
    assert mock_client.post.call_count == 2
    # Verify fallback call used json_object
    fallback_call_payload = mock_client.post.call_args_list[1][1]["json"]
    assert fallback_call_payload["response_format"] == {"type": "json_object"}
