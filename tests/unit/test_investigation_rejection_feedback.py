"""Unit and regression tests for investigation rejection feedback, anti-retry protection,
report timeline transparency, and reliable final synthesis guarantees.

Tests address:
P1: Structured, actionable rejection feedback from record_finding.
P2: Per-run rejected-claim memory and anti-retry protection.
P3: Report exporter timeline transparency for rejected claims.
P4: Reliable final synthesis turn guarantees in the investigation loop.
"""

import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from forge.analysis.evidence import create_evidence
from forge.analysis.validation import (
    FindingValidationError,
    SyntaxClaimValidationError,
    ConsistencyValidationError,
    InsufficientEvidenceError,
    RepeatedRejectedClaimError,
    RejectedClaimRecord,
    RejectedClaimTracker,
    build_rejection_directive,
    validate_finding_claim,
)
from forge.investigation.loop import InvestigationLoop, InvestigationResult
from forge.investigation.tools import ToolRegistry
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
)
from forge.reporting.builder import InvestigationReportBuilder
from forge.schemas import (
    EventType,
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


def _setup_hamilton_fixture(tmp_path: Path):
    """Set up workspace with valid Python file replicating Hamilton graph_types.py."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    hamilton_dir = workspace / "hamilton"
    hamilton_dir.mkdir(parents=True, exist_ok=True)

    graph_types = hamilton_dir / "graph_types.py"
    code = (
        '"""Hamilton graph types module."""\n'
        "\n"
        "def get_type_as_string(typ):\n"
        '    return str(typ) if typ else ""\n'
        "\n"
        "class HamiltonNode:\n"
        "    def __init__(self, name, typ, tags=None):\n"
        "        self.name = name\n"
        "        self.type = typ\n"
        "        self.tags = tags or {}\n"
        "\n"
        "    def as_dict(self):\n"
        "        return {\n"
        '            "name": self.name,\n'
        '            "output_type": (get_type_as_string(self.type) or ""),\n'
        '            "tags": self.tags,\n'
        "        }\n"
    )
    graph_types.write_text(code, encoding="utf-8")

    db_path = tmp_path / "test.db"
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target=str(workspace), run_id="run_rejection_test")
    tool_reg = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    return workspace, store, run_id, tool_reg, graph_types


# =========================================================================
# P1: Structured Actionable Rejection Feedback Tests
# =========================================================================

def test_syntax_claim_validation_error_produces_structured_rejection(tmp_path: Path):
    """P1: False positive syntax error rejected with structured JSON directive."""
    _, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    # Line 15 is valid: `"output_type": (get_type_as_string(self.type) or ""),`
    args = {
        "title": "Missing colon in HamiltonNode.as_dict()",
        "description": "Syntax error: missing colon in dictionary entry at line 15",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 15,
        "end_line": 15,
        "defect_category": "syntax_error",
        "severity": "medium",
        "confidence": "confirmed",
    }

    raw_output = tool_reg.execute_tool("record_finding", args)
    rejection = json.loads(raw_output)

    assert rejection["status"] == "rejected"
    assert rejection["rejection_type"] == "SyntaxClaimValidationError"
    assert "error" in rejection
    assert rejection["affected_file"] == "hamilton/graph_types.py"
    assert rejection["line_range"] == {"start": 15, "end": 15}
    assert rejection["defect_category"] == "syntax_error"
    assert "directive" in rejection
    assert "Do NOT re-submit" in rejection["directive"] or "Do not resubmit" in rejection["directive"]
    assert "syntax" in rejection["directive"].lower()


def test_insufficient_evidence_produces_structured_rejection(tmp_path: Path):
    """P1: Loop defect on non-loop lines rejected with targeted directive."""
    _, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    args = {
        "title": "Infinite loop in as_dict",
        "description": "Infinite loop detected during dictionary construction",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 14,
        "end_line": 16,
        "defect_category": "loop_defect",
        "severity": "high",
        "confidence": "high",
    }

    raw_output = tool_reg.execute_tool("record_finding", args)
    rejection = json.loads(raw_output)

    assert rejection["status"] == "rejected"
    assert rejection["rejection_type"] == "InsufficientEvidenceError"
    assert rejection["affected_file"] == "hamilton/graph_types.py"
    assert "for" in rejection["directive"] or "loop" in rejection["directive"]
    assert "Do not resubmit" in rejection["directive"] or "Do NOT re-submit" in rejection["directive"]


def test_tool_execution_failure_distinguishable_from_finding_rejection(tmp_path: Path):
    """P1: Tool execution failure (missing parameters) is clearly distinguishable from rejected findings."""
    _, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    raw_output = tool_reg.execute_tool("record_finding", {"title": "Incomplete finding"})
    result = json.loads(raw_output)

    assert "error" in result
    assert result.get("status") != "rejected"
    assert "rejection_type" not in result


def test_finding_validation_error_to_rejection_dict_unit():
    """P1: Unit validation of FindingValidationError and directive builder."""
    fve = FindingValidationError(
        "Custom validation message",
        affected_file="src/core.py",
        start_line=10,
        end_line=20,
        defect_category="logic_bug",
        directive="Do not submit logic bugs without unit test failure.",
        rejection_type="CustomValidationError",
    )
    d = fve.to_rejection_dict()
    assert d["status"] == "rejected"
    assert d["rejection_type"] == "CustomValidationError"
    assert d["reason"] == "Custom validation message"
    assert d["error"] == "Custom validation message"
    assert d["affected_file"] == "src/core.py"
    assert d["line_range"] == {"start": 10, "end": 20}
    assert d["defect_category"] == "logic_bug"
    assert d["directive"] == "Do not submit logic bugs without unit test failure."


# =========================================================================
# P2: Rejected Claim Memory and Anti-Retry Protection Tests
# =========================================================================

def test_rejected_claim_tracker_detects_repeated_submission_with_different_title(tmp_path: Path):
    """P2: Re-submitting rejected claim on same lines with reworded title is blocked early."""
    _, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    # First attempt: syntax claim
    args1 = {
        "title": "Missing colon in HamiltonNode.as_dict()",
        "description": "Syntax error: missing colon in dictionary entry at line 15",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 15,
        "end_line": 15,
        "defect_category": "syntax_error",
    }
    raw1 = tool_reg.execute_tool("record_finding", args1)
    res1 = json.loads(raw1)
    assert res1["status"] == "rejected"
    assert res1["rejection_type"] == "SyntaxClaimValidationError"

    # Second attempt: rephrased title and description, same file and line
    args2 = {
        "title": "Invalid dict mapping syntax in graph types",
        "description": "Syntax error: missing colon delimiter in dictionary key-value pair",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 15,
        "end_line": 15,
        "defect_category": "syntax_error",
    }
    raw2 = tool_reg.execute_tool("record_finding", args2)
    res2 = json.loads(raw2)

    assert res2["status"] == "rejected"
    assert res2["rejection_type"] == "RepeatedRejectedClaimError"
    assert "already rejected" in res2["reason"].lower() or "previously rejected" in res2["reason"].lower()
    assert "already rejected" in res2["directive"] or "repeatedly retry" in res2["directive"]


def test_rejected_claim_tracker_permits_genuinely_distinct_defects(tmp_path: Path):
    """P2: Genuinely distinct defect on different lines is not blocked by anti-retry."""
    workspace, store, run_id, tool_reg, graph_types = _setup_hamilton_fixture(tmp_path)

    # Add a genuine syntax error in a separate function in graph_types.py
    code_with_real_err = (
        '"""Hamilton graph types module."""\n'
        "\n"
        "def bad_function():\n"
        "    if True\n"  # line 4: real syntax error
        "        pass\n"
        "\n"
        "class HamiltonNode:\n"
        "    def as_dict(self):\n"
        "        return {\"output_type\": \"val\"}\n"
    )
    graph_types.write_text(code_with_real_err, encoding="utf-8")

    # Claim 1: rejected false positive on line 8
    args1 = {
        "title": "Missing colon on line 8",
        "description": "Syntax error: missing colon on line 8",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 8,
        "end_line": 8,
        "defect_category": "syntax_error",
    }
    res1 = json.loads(tool_reg.execute_tool("record_finding", args1))
    assert res1["status"] == "rejected"

    # Claim 2: genuine syntax error on line 4 (different lines, real bug)
    args2 = {
        "title": "Syntax error in bad_function",
        "description": "Missing colon after if statement header",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 4,
        "end_line": 4,
        "defect_category": "syntax_error",
    }
    res2 = json.loads(tool_reg.execute_tool("record_finding", args2))
    assert res2["finding_id"].startswith("find_")
    assert res2["title"] == "Syntax error in bad_function"


def test_rejected_claim_memory_isolated_per_run(tmp_path: Path):
    """P2: Anti-retry memory is isolated per run/ToolRegistry instance."""
    workspace, store, run_id1, tool_reg1, _ = _setup_hamilton_fixture(tmp_path)
    run_id2 = store.create_run(target=str(workspace), run_id="run_rejection_test_2")
    tool_reg2 = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id2)

    args = {
        "title": "Syntax error in dictionary",
        "description": "Syntax error at line 15",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 15,
        "end_line": 15,
        "defect_category": "syntax_error",
    }

    # Reject on run 1
    res1 = json.loads(tool_reg1.execute_tool("record_finding", args))
    assert res1["rejection_type"] == "SyntaxClaimValidationError"

    # First attempt on run 2: should be rejected by SyntaxClaimValidationError, NOT RepeatedRejectedClaimError
    res2 = json.loads(tool_reg2.execute_tool("record_finding", args))
    assert res2["rejection_type"] == "SyntaxClaimValidationError"


# =========================================================================
# P3: Report Exporter Timeline Transparency Tests
# =========================================================================

def test_report_timeline_includes_validation_failure_details(tmp_path: Path):
    """P3: Exporter timeline formats FINDING_VALIDATION_FAILED with title, file, lines, and reason."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    # Trigger rejection
    args = {
        "title": "Missing colon in as_dict",
        "description": "Syntax error at line 15",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 15,
        "end_line": 15,
        "defect_category": "syntax_error",
    }
    tool_reg.execute_tool("record_finding", args)

    builder = InvestigationReportBuilder(store=store)
    report = builder.build_report(run_id=run_id)

    failed_events = [ev for ev in report.events_timeline if ev.event_type == EventType.FINDING_VALIDATION_FAILED.value]
    assert len(failed_events) >= 1

    ev_summary = failed_events[0].summary
    assert "Finding validation failed" in ev_summary
    assert "[SyntaxClaimValidationError]" in ev_summary
    assert "Missing colon in as_dict" in ev_summary
    assert "hamilton/graph_types.py:15-15" in ev_summary or "hamilton/graph_types.py" in ev_summary


def test_report_timeline_includes_duplicate_detection_details(tmp_path: Path):
    """P3: Exporter timeline formats FINDING_DUPLICATE_DETECTED correctly."""
    _, store, run_id, _, _ = _setup_hamilton_fixture(tmp_path)

    store.append_event(
        run_id,
        EventType.FINDING_DUPLICATE_DETECTED,
        {
            "title": "Duplicate syntax error",
            "file": "hamilton/graph_types.py",
            "lines": "15-15",
            "message": "Duplicate finding already tracked as find_001",
        },
    )

    builder = InvestigationReportBuilder(store=store)
    report = builder.build_report(run_id=run_id)

    dup_events = [ev for ev in report.events_timeline if ev.event_type == EventType.FINDING_DUPLICATE_DETECTED.value]
    assert len(dup_events) == 1
    assert "Duplicate finding rejected" in dup_events[0].summary
    assert "Duplicate syntax error" in dup_events[0].summary
    assert "hamilton/graph_types.py:15-15" in dup_events[0].summary


def test_report_timeline_sanitizes_secrets_and_handles_missing_fields(tmp_path: Path):
    """P3: Exporter timeline redacts tokens and handles sparse event payloads safely."""
    _, store, run_id, _, _ = _setup_hamilton_fixture(tmp_path)

    store.append_event(
        run_id,
        EventType.FINDING_VALIDATION_FAILED,
        {
            "error_type": "SyntaxClaimValidationError",
            "message": "Invalid token sk-abcdef1234567890abcdef12 in syntax claim",
        },
    )

    builder = InvestigationReportBuilder(store=store)
    report = builder.build_report(run_id=run_id)

    failed_events = [ev for ev in report.events_timeline if ev.event_type == EventType.FINDING_VALIDATION_FAILED.value]
    assert len(failed_events) == 1
    summary = failed_events[0].summary
    assert "sk-abcdef" not in summary
    assert "REDACTED" in summary or "sk-" not in summary


# =========================================================================
# P4: Dedicated Final Synthesis Turn Tests
# =========================================================================

def test_final_turn_reserves_synthesis_opportunity(tmp_path: Path):
    """P4: On final step when max_steps >= 2, tools are withheld to guarantee final assessment."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    tool_request_history = []

    class MockProviderWithCapture(FakeLLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            tool_request_history.append(list(request.tools))
            return super().generate(request)

    responses = [
        # Step 1: LLM calls get_repository_map
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(call_id="call_1", name="get_repository_map", arguments={})
            ],
        ),
        # Step 2: LLM calls record_finding (which will be rejected)
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    call_id="call_2",
                    name="record_finding",
                    arguments={
                        "title": "Missing colon in as_dict",
                        "description": "Syntax error at line 15",
                        "affected_file": "hamilton/graph_types.py",
                        "start_line": 15,
                        "end_line": 15,
                        "defect_category": "syntax_error",
                    },
                )
            ],
        ),
        # Step 3: LLM provides final synthesis text
        LLMResponse(
            content=(
                "Final Assessment: Inspected hamilton/graph_types.py. The proposed syntax error "
                "was rejected because Python dictionary syntax is valid. 0 defects verified."
            ),
            tool_calls=[],
        ),
    ]

    provider = MockProviderWithCapture(scripted_responses=responses)
    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target="hamilton",
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "completed"
    assert result.steps_executed == 3
    assert len(result.findings) == 0
    assert result.final_response is not None
    assert "0 defects verified" in result.final_response

    # Verify that step 3 had NO tools provided (dedicated synthesis turn)
    assert len(tool_request_history) == 3
    assert len(tool_request_history[0]) > 0  # step 1 had tools
    assert len(tool_request_history[1]) > 0  # step 2 had tools
    assert len(tool_request_history[2]) == 0  # step 3 had tools withheld!


def test_truthful_synthesis_with_zero_findings_completes(tmp_path: Path):
    """P4: An investigation with 0 accepted findings and truthful synthesis completes cleanly."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    responses = [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(call_id="call_1", name="get_repository_map", arguments={})
            ],
        ),
        LLMResponse(
            content="Repository inspection complete. Verified core types; 0 findings observed.",
            tool_calls=[],
        ),
    ]
    provider = FakeLLMProvider(scripted_responses=responses)
    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target="hamilton",
        max_steps=2,
    )

    result = loop.run()
    assert result.status == "completed"
    assert len(result.findings) == 0
    assert "0 findings observed" in result.final_response


def test_budget_exhaustion_without_synthesis_marks_max_steps_exceeded(tmp_path: Path):
    """P4: If LLM returns tool calls on turn 1 and empty text on final turn, status is max_steps_exceeded."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    responses = [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(call_id="call_1", name="get_repository_map", arguments={})
            ],
        ),
        LLMResponse(
            content="",  # Empty content on final turn
            tool_calls=[],
        ),
    ]
    provider = FakeLLMProvider(scripted_responses=responses)
    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target="hamilton",
        max_steps=2,
    )

    result = loop.run()
    assert result.status == "max_steps_exceeded"
    assert "maximum execution step limit" in result.final_response


def test_zero_activity_investigation_marks_failed(tmp_path: Path):
    """P4: Turn 1 text response with 0 tools executed and 0 findings cannot falsely claim completed."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_fixture(tmp_path)

    responses = [
        LLMResponse(
            content="I am done, no bugs here.",
            tool_calls=[],
        ),
        LLMResponse(
            content="Still done.",
            tool_calls=[],
        ),
    ]
    provider = FakeLLMProvider(scripted_responses=responses)
    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target="hamilton",
        max_steps=2,
    )

    result = loop.run()
    assert result.status == "failed"
    assert "Zero tools executed and zero findings recorded" in result.error_message
