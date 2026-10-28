"""Unit tests for Phase 4 Structured Proposal Wire-Schema Reliability.

Verifies:
1. Wire JSON schema strictly requires patch fields ('search_block', 'replace_block', 'unified_diff')
   as nullable fields so local Ollama / grammar engines never omit them.
2. Acceptance of valid structured proposals ('search_block' + 'replace_block', 'unified_diff': null).
3. Acceptance of valid legacy unified diff proposals ('unified_diff', structured blocks: null).
4. Regression test for Pilot 01 Attempt 5 failure: rejection of payloads omitting all patch representations.
5. Rejection of partial structured proposals (search_block without replace_block or vice versa).
6. Rejection of ambiguous representations (both structured blocks and unified diff provided).
7. End-to-end planner retry loop recovers when model corrects schema errors via targeted feedback.
8. Planner fails closed when retries are exhausted on persistent invalid schema output.
9. Provider failures fail closed without cloud or mock fallback.
10. Backward compatibility for legacy payloads missing search_block/replace_block dictionary keys.
"""

from pathlib import Path
from typing import Any, Dict, List
import pytest
from pydantic import ValidationError

from forge.repair.contracts import PatchCandidate, RepairBudget
from forge.repair.models import (
    RepairModelResponseSchema,
    RepairPlanStatus,
    configure_repair_response_schema,
)
from forge.repair.planner import (
    RepairPlanner,
    build_repair_response_format,
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


def _create_sample_finding_and_evidence(tmp_path: Path) -> tuple[Finding, List[EvidenceRecord], Path]:
    """Helper to create an isolated repository with calculator.py and reproduction evidence."""
    repo = tmp_path / "repo"
    repo.mkdir()
    calc = repo / "calculator.py"
    calc.write_text(
        "def divide(a: float, b: float) -> float:\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    test_calc = repo / "tests" / "test_calculator.py"
    test_calc.parent.mkdir()
    test_calc.write_text(
        "import pytest\n"
        "from calculator import divide\n\n"
        "def test_divide_zero():\n"
        "    with pytest.raises(ValueError):\n"
        "        divide(10, 0)\n",
        encoding="utf-8",
    )

    finding = Finding(
        finding_id="find_div_zero",
        title="ZeroDivisionError in divide",
        description="divide does not check for zero divisor, raising ZeroDivisionError instead of ValueError",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.CONFIRMED,
        lifecycle_state=FindingLifecycleState.REPRODUCED,
        affected_files=["calculator.py", "tests/test_calculator.py"],
    )

    evidence = [
        EvidenceRecord(
            evidence_id="evi_repro_1",
            run_id="run_test",
            finding_id="find_div_zero",
            evidence_type=EvidenceType.REPRODUCTION_RESULT,
            description="Docker reproduction failed with ZeroDivisionError",
            content='File "/workspace/calculator.py", line 2, in divide\n    return a / b\nZeroDivisionError: division by zero',
        )
    ]
    return finding, evidence, repo


def test_wire_schema_properties_and_required_fields():
    """Verify JSON schema wire contract requires patch fields and types them as nullable."""
    schema = RepairModelResponseSchema.model_json_schema()
    required = schema.get("required", [])

    # Patch representation fields MUST be in required for wire grammar enforcement
    assert "search_block" in required
    assert "replace_block" in required
    assert "unified_diff" in required

    # Core diagnostic fields must also be required
    assert "root_cause" in required
    assert "proposed_change" in required
    assert "target_file" in required
    assert "expected_behavior" in required
    assert "verification_plan" in required

    props = schema["properties"]
    # Check that search_block, replace_block, unified_diff allow null
    for field in ("search_block", "replace_block", "unified_diff"):
        prop = props[field]
        assert "anyOf" in prop, f"{field} should have anyOf for nullable type"
        types = [t.get("type") for t in prop["anyOf"]]
        assert "string" in types
        assert "null" in types

    # Check response format wrapper
    rf = build_repair_response_format()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "RepairModelResponseSchema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == schema


def test_valid_structured_proposal_accepted():
    """Verify structured proposal with search_block + replace_block is accepted."""
    payload = {
        "root_cause": "Missing zero check in divisor",
        "proposed_change": "Add check for b == 0 and raise ValueError",
        "target_file": "calculator.py",
        "search_block": "def divide(a: float, b: float) -> float:\n    return a / b",
        "replace_block": "def divide(a: float, b: float) -> float:\n    if b == 0:\n        raise ValueError('Cannot divide by zero')\n    return a / b",
        "change_rationale": "Guard against division by zero",
        "unified_diff": None,
        "expected_behavior": "Raises ValueError when b is 0",
        "verification_plan": "pytest tests/test_calculator.py",
    }
    obj = RepairModelResponseSchema.model_validate(payload)
    assert obj.search_block is not None
    assert obj.replace_block is not None
    assert obj.unified_diff is None


def test_valid_legacy_unified_diff_accepted():
    """Verify legacy unified diff proposal is accepted when structured blocks are None."""
    payload = {
        "root_cause": "Missing zero check in divisor",
        "proposed_change": "Add check for b == 0 and raise ValueError",
        "target_file": "calculator.py",
        "search_block": None,
        "replace_block": None,
        "change_rationale": None,
        "unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,4 @@\n def divide(a: float, b: float) -> float:\n+    if b == 0:\n+        raise ValueError('Cannot divide by zero')\n     return a / b\n",
        "expected_behavior": "Raises ValueError when b is 0",
        "verification_plan": "pytest tests/test_calculator.py",
    }
    obj = RepairModelResponseSchema.model_validate(payload)
    assert obj.unified_diff is not None
    assert obj.search_block is None
    assert obj.replace_block is None


def test_attempt_5_failure_regression_omitted_all_patches_rejected():
    """Regression test: Pilot 01 Attempt 5 omitted all patch representations.

    The model generated valid JSON with diagnostic fields but null/omitted patch fields.
    Must raise ValidationError specifically identifying missing patch representation.
    """
    payload = {
        "root_cause": "The divide function does not check if the denominator b is zero, causing a ZeroDivisionError.",
        "proposed_change": "Add a condition to check if b is zero and raise a ValueError with an appropriate message.",
        "target_file": "calculator.py",
        "expected_behavior": "When divide(a, 0) is called, it should raise a ValueError instead of ZeroDivisionError.",
        "regression_risks": ["Existing code relying on ZeroDivisionError might fail, but this is a bug fix."],
        "verification_plan": "Run pytest tests/test_calculator.py to verify the fix.",
        "confidence": 0.9,
        "uncertainty_notes": None,
        "search_block": None,
        "replace_block": None,
        "change_rationale": None,
        "unified_diff": None,
    }
    with pytest.raises(ValidationError) as exc_info:
        RepairModelResponseSchema.model_validate(payload)

    err_msg = str(exc_info.value)
    assert "Missing patch representation" in err_msg


def test_partial_structured_proposal_rejected():
    """Verify partial structured proposals (search without replace or vice versa) are rejected."""
    # search_block provided, replace_block is None
    payload_search_only = {
        "root_cause": "Bug",
        "proposed_change": "Fix",
        "target_file": "calculator.py",
        "expected_behavior": "Pass",
        "verification_plan": "pytest",
        "search_block": "return a / b",
        "replace_block": None,
        "unified_diff": None,
    }
    with pytest.raises(ValidationError) as exc1:
        RepairModelResponseSchema.model_validate(payload_search_only)
    assert "Partial structured proposal: 'search_block' provided without corresponding 'replace_block'" in str(exc1.value)

    # replace_block provided, search_block is None
    payload_replace_only = {
        "root_cause": "Bug",
        "proposed_change": "Fix",
        "target_file": "calculator.py",
        "expected_behavior": "Pass",
        "verification_plan": "pytest",
        "search_block": None,
        "replace_block": "return a / b if b != 0 else 0",
        "unified_diff": None,
    }
    with pytest.raises(ValidationError) as exc2:
        RepairModelResponseSchema.model_validate(payload_replace_only)
    assert "Partial structured proposal: 'replace_block' provided without corresponding 'search_block'" in str(exc2.value)

    # search_block non-empty, replace_block whitespace only
    payload_whitespace_replace = {
        "root_cause": "Bug",
        "proposed_change": "Fix",
        "target_file": "calculator.py",
        "expected_behavior": "Pass",
        "verification_plan": "pytest",
        "search_block": "return a / b",
        "replace_block": "   \n\t  ",
        "unified_diff": None,
    }
    with pytest.raises(ValidationError) as exc3:
        RepairModelResponseSchema.model_validate(payload_whitespace_replace)
    assert "Partial structured proposal: 'search_block' provided without corresponding 'replace_block'" in str(exc3.value)


def test_ambiguous_patch_representation_rejected():
    """Verify supplying BOTH structured blocks AND unified_diff is rejected as ambiguous."""
    payload = {
        "root_cause": "Bug",
        "proposed_change": "Fix",
        "target_file": "calculator.py",
        "expected_behavior": "Pass",
        "verification_plan": "pytest",
        "search_block": "return a / b",
        "replace_block": "return a / b if b != 0 else 0",
        "unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-return a / b\n+return a / b if b != 0 else 0\n",
    }
    with pytest.raises(ValidationError) as exc:
        RepairModelResponseSchema.model_validate(payload)
    assert "Ambiguous patch representation" in str(exc.value)


def test_planner_retry_loop_corrects_missing_patch_representation(tmp_path: Path):
    """Planner provides targeted feedback when model omits patch representation and succeeds on retry."""
    finding, evidence, repo = _create_sample_finding_and_evidence(tmp_path)

    # Response 1: Attempt 5 failure style (omitted patch fields)
    resp1 = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "search_block": null,\n'
        '  "replace_block": null,\n'
        '  "unified_diff": null\n'
        '}'
    )

    # Response 2: Corrected with valid search and replace blocks
    resp2 = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "search_block": "def divide(a: float, b: float) -> float:\\n    return a / b",\n'
        '  "replace_block": "def divide(a: float, b: float) -> float:\\n    if b == 0:\\n        raise ValueError(\'division by zero\')\\n    return a / b",\n'
        '  "change_rationale": "Handle division by zero gracefully",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "unified_diff": null\n'
        '}'
    )

    provider = ScriptedProvider([resp1, resp2])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha="0ba1653f3607e1dd1d73b4bc07e0b16273e6c777",
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert plan.validation is not None
    assert plan.validation.is_valid is True
    assert provider.call_count == 2

    # Verify that the retry prompt sent back contained the targeted feedback
    second_request = provider.recorded_requests[1]
    last_user_msg = second_request.messages[-1].content
    assert "Missing patch representation" in last_user_msg
    assert "CRITICAL: Missing patch representation" in last_user_msg


def test_planner_retry_loop_corrects_partial_structured_proposal(tmp_path: Path):
    """Planner provides targeted feedback when model outputs partial structured proposal and succeeds on retry."""
    finding, evidence, repo = _create_sample_finding_and_evidence(tmp_path)

    # Response 1: search_block provided, replace_block is null
    resp1 = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "search_block": "def divide(a: float, b: float) -> float:\\n    return a / b",\n'
        '  "replace_block": null,\n'
        '  "unified_diff": null\n'
        '}'
    )

    # Response 2: Both provided
    resp2 = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "search_block": "def divide(a: float, b: float) -> float:\\n    return a / b",\n'
        '  "replace_block": "def divide(a: float, b: float) -> float:\\n    if b == 0:\\n        raise ValueError(\'division by zero\')\\n    return a / b",\n'
        '  "change_rationale": "Handle division by zero gracefully",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "unified_diff": null\n'
        '}'
    )

    provider = ScriptedProvider([resp1, resp2])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha="0ba1653f3607e1dd1d73b4bc07e0b16273e6c777",
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert provider.call_count == 2
    second_request = provider.recorded_requests[1]
    last_user_msg = second_request.messages[-1].content
    assert "Partial structured proposal" in last_user_msg
    assert "CRITICAL: Partial structured proposal" in last_user_msg


def test_planner_retry_loop_corrects_ambiguous_representation(tmp_path: Path):
    """Planner provides targeted feedback when model outputs ambiguous representation and succeeds on retry."""
    finding, evidence, repo = _create_sample_finding_and_evidence(tmp_path)

    # Response 1: Both structured blocks AND unified_diff provided
    resp1 = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "search_block": "return a / b",\n'
        '  "replace_block": "if b == 0: raise ValueError()\\nreturn a / b",\n'
        '  "unified_diff": "--- a/calculator.py\\n+++ b/calculator.py\\n@@ -2,1 +2,2 @@\\n+ if b == 0: raise ValueError()\\n  return a / b\\n"\n'
        '}'
    )

    # Response 2: Ambiguity resolved (unified_diff set to null)
    resp2 = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "search_block": "def divide(a: float, b: float) -> float:\\n    return a / b",\n'
        '  "replace_block": "def divide(a: float, b: float) -> float:\\n    if b == 0:\\n        raise ValueError(\'division by zero\')\\n    return a / b",\n'
        '  "change_rationale": "Handle division by zero gracefully",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "unified_diff": null\n'
        '}'
    )

    provider = ScriptedProvider([resp1, resp2])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha="0ba1653f3607e1dd1d73b4bc07e0b16273e6c777",
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.VALIDATED
    assert provider.call_count == 2
    second_request = provider.recorded_requests[1]
    last_user_msg = second_request.messages[-1].content
    assert "Ambiguous patch representation" in last_user_msg
    assert "CRITICAL: Ambiguous patch representation" in last_user_msg


def test_planner_fails_closed_when_schema_retries_exhausted(tmp_path: Path):
    """Planner fails closed with rejected plan when model persistently violates wire schema."""
    finding, evidence, repo = _create_sample_finding_and_evidence(tmp_path)

    bad_resp = (
        '{\n'
        '  "root_cause": "No zero check in divide",\n'
        '  "proposed_change": "Add check for zero divisor",\n'
        '  "target_file": "calculator.py",\n'
        '  "expected_behavior": "Raises ValueError",\n'
        '  "verification_plan": "pytest tests/test_calculator.py",\n'
        '  "search_block": null,\n'
        '  "replace_block": null,\n'
        '  "unified_diff": null\n'
        '}'
    )

    provider = ScriptedProvider([bad_resp, bad_resp, bad_resp])
    planner = RepairPlanner(llm_provider=provider, max_retries=2)

    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha="0ba1653f3607e1dd1d73b4bc07e0b16273e6c777",
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("Missing patch representation" in v for v in plan.validation.violations)
    assert provider.call_count == 3


def test_provider_failure_fails_closed_without_cloud_fallback(tmp_path: Path):
    """When the provider raises an error, planner fails closed without falling back to cloud."""
    finding, evidence, repo = _create_sample_finding_and_evidence(tmp_path)

    class FailingProvider(LLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise ConnectionError("Connection refused to local Ollama http://localhost:11434/v1")

        def get_provider_name(self) -> str:
            return "ollama"

        def get_model_name(self) -> str:
            return "qwen2.5-coder:1.5b"

    planner = RepairPlanner(llm_provider=FailingProvider(), max_retries=2)
    plan = planner.plan_repair(
        finding_id=finding.finding_id,
        target_repo=repo,
        base_commit_sha="0ba1653f3607e1dd1d73b4bc07e0b16273e6c777",
        finding=finding,
        evidence_records=evidence,
    )

    assert plan.status == RepairPlanStatus.REJECTED
    assert plan.validation is not None
    assert plan.validation.is_valid is False
    assert any("LLM provider generation failed" in v for v in plan.validation.violations)


def test_backward_compatibility_model_validate_with_missing_keys():
    """Verify legacy payloads that omit search_block and replace_block keys validate cleanly."""
    legacy_payload = {
        "root_cause": "Missing zero check",
        "proposed_change": "Raise ValueError",
        "target_file": "calculator.py",
        "expected_behavior": "Raises ValueError",
        "verification_plan": "pytest tests/test_calculator.py",
        "unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-a / b\n+a / b if b != 0 else 0\n",
    }
    # Notice 'search_block' and 'replace_block' are not even present as keys in the dict
    obj = RepairModelResponseSchema.model_validate(legacy_payload)
    assert obj.search_block is None
    assert obj.replace_block is None
    assert obj.unified_diff is not None
