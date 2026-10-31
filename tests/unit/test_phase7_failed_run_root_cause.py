"""12 Comprehensive regression tests for Phase 7 failed run root-cause investigation and remediation.

Covers:
1. Ollama request timeout (httpx timeout -> LLMTimeoutError -> structured failure event)
2. Ollama connection failure (network/endpoint unreachable -> connection_error)
3. Empty model response (repeated empty content handling and failure capture)
4. Malformed model response (invalid JSON / malformed payload handling)
5. Invalid tool-call response (extraction errors and model feedback loop)
6. Tool dispatch exception (graceful handling of tool execution crashes)
7. Investigation-loop unexpected exception (outer catch-all guarantees no dangling runs)
8. Final synthesis failure (guarantee that run cannot be marked completed if synthesis fails)
9. Failure-event persistence (structured failure payload written to SQLite run_events)
10. Report rendering of failure details (JSON and Markdown report exposure)
11. Secret redaction in failure messages (zero secret leakage in failure payloads)
12. Preservation of partial investigation history (partial progress preserved upon late failure)
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from forge.investigation.loop import InvestigationLoop, InvestigationResult
from forge.investigation.tools import ToolRegistry
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
    ToolDefinition,
)
from forge.llm.openai import OpenAICompatibleProvider
from forge.llm.provider import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from forge.reporting.builder import ReportBuilder, report_to_json, report_to_markdown
from forge.schemas import EventType, Finding, FindingSeverity, FindingConfidence
from forge.storage.store import ForgeStore


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

class ScriptedLLMProvider:
    """Mock LLM provider with scripted step-by-step responses or exceptions."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.call_count = 0
        self.requests: List[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            if isinstance(resp, Exception):
                raise resp
            return resp
        return LLMResponse(content="Final scripted default assessment.", tool_calls=[])


@pytest.fixture
def test_env(tmp_path: Path):
    """Create test workspace and database."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("def hello():\n    return 'world'\n")
    db_path = tmp_path / "test.db"
    store = ForgeStore(db_path=db_path)
    return workspace, db_path, store


# ---------------------------------------------------------------------------
# Test 1: Ollama request timeout
# ---------------------------------------------------------------------------

def test_ollama_request_timeout(test_env):
    """Ollama request timeout raises LLMTimeoutError and logs structured failure event."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        LLMTimeoutError("LLM request timed out after 60.0s: timed out")
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.failure_details is not None
    assert result.failure_details["failure_category"] == "timeout"
    assert result.failure_details["stage"] == "llm_generation"
    assert result.failure_details["retryable"] is True
    assert "timed out" in result.failure_details["error_message"]

    # Verify run record in store
    run_summary = store.get_run_summary_dict(run_id)
    assert run_summary["failure_details"]["failure_category"] == "timeout"


# ---------------------------------------------------------------------------
# Test 2: Ollama connection failure
# ---------------------------------------------------------------------------

def test_ollama_connection_failure(test_env):
    """Network connection failure to Ollama is classified as connection_error and is retryable."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        LLMUnavailableError("Network error connecting to LLM provider at http://localhost:11434/v1: Connection refused")
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.failure_details is not None
    assert result.failure_details["failure_category"] == "connection_error"
    assert result.failure_details["retryable"] is True
    assert "Connection refused" in result.failure_details["error_message"]


# ---------------------------------------------------------------------------
# Test 3: Empty model response
# ---------------------------------------------------------------------------

def test_empty_model_response(test_env):
    """Repeated empty model responses are handled safely and terminate with failure details."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    # Empty content across turns
    provider = ScriptedLLMProvider([
        LLMResponse(content="", tool_calls=[]),
        LLMResponse(content="   ", tool_calls=[]),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=2,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.failure_details is not None
    assert result.failure_details["failure_category"] in ("synthesis_error", "no_activity", "empty_response")


# ---------------------------------------------------------------------------
# Test 4: Malformed model response
# ---------------------------------------------------------------------------

def test_malformed_model_response(test_env):
    """Malformed JSON returned by provider is classified as malformed_response and non-retryable."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        LLMProviderError("Malformed JSON returned by LLM provider: Expecting value: line 1 column 1")
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.failure_details is not None
    assert result.failure_details["failure_category"] == "malformed_response"
    assert result.failure_details["retryable"] is False


# ---------------------------------------------------------------------------
# Test 5: Invalid tool-call response
# ---------------------------------------------------------------------------

def test_invalid_tool_call_response(test_env):
    """Model message with extraction error receives correction feedback and recovers or terminates safely."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        # Step 1: malformed extraction error
        LLMResponse(
            content='{"invalid_json": true}',
            tool_calls=[],
            extraction_error="Invalid arguments for tool 'read_source_excerpt': Missing required parameter 'file_path'",
        ),
        # Step 2: model recovers by calling a valid tool
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(call_id="call_1", name="get_run_history", arguments={})],
        ),
        # Step 3 (synthesis): concludes
        LLMResponse(content="Completed audit synthesis assessment with verified boundaries.", tool_calls=[]),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "completed"
    assert result.total_tool_calls == 1
    # Verify feedback message was injected in history
    assert any("Tool invocation error" in m.content for m in provider.requests[1].messages if m.content)


# ---------------------------------------------------------------------------
# Test 6: Tool dispatch exception
# ---------------------------------------------------------------------------

def test_tool_dispatch_exception(test_env):
    """Unexpected exception raised during tool dispatch is caught and recorded as tool_dispatch_error."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    # Force execute_tool to raise an unexpected runtime error
    tools.execute_tool = MagicMock(side_effect=RuntimeError("Filesystem I/O failure during execution"))

    provider = ScriptedLLMProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(call_id="call_1", name="get_run_history", arguments={})],
        )
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.failure_details is not None
    assert result.failure_details["failure_category"] == "tool_dispatch_error"
    assert result.failure_details["stage"] == "tool_dispatch"
    assert result.failure_details["retryable"] is False
    assert "Filesystem I/O failure" in result.failure_details["error_message"]


# ---------------------------------------------------------------------------
# Test 7: Investigation-loop unexpected exception
# ---------------------------------------------------------------------------

def test_investigation_loop_unexpected_exception(test_env):
    """Unexpected crash inside the loop is caught and recorded without leaving dangling run state."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        RuntimeError("Fatal internal memory corruption")
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.failure_details is not None
    assert result.failure_details["failure_category"] == "unexpected_exception"

    # Verify run status in SQLite is updated to failed
    run_row = store.get_run(run_id)
    assert run_row.status == "failed"


# ---------------------------------------------------------------------------
# Test 8: Final synthesis failure
# ---------------------------------------------------------------------------

def test_final_synthesis_failure(test_env):
    """Failure during final synthesis step preserves partial progress and prevents false completion."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    # Turn 1: tool call succeeds. Turn 2 (final step): synthesis times out.
    provider = ScriptedLLMProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(call_id="call_1", name="get_run_history", arguments={})],
        ),
        LLMTimeoutError("Request timed out during final synthesis after 60.0s: timed out"),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=2,
    )

    result = loop.run()

    # Invariant: Must NOT be completed if synthesis failed
    assert result.status == "failed"
    assert result.total_tool_calls == 1
    assert result.steps_executed == 2
    assert result.failure_details is not None
    assert result.failure_details["stage"] == "synthesis"
    assert result.failure_details["failure_category"] == "timeout"


# ---------------------------------------------------------------------------
# Test 9: Failure-event persistence
# ---------------------------------------------------------------------------

def test_failure_event_persistence(test_env):
    """Structured failure event is persisted to SQLite run_events with all required fields."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        LLMTimeoutError("LLM request timed out after 60.0s: timed out")
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    loop.run()

    events = store.list_run_events(run_id)
    failed_events = [e for e in events if e.event_type == EventType.RUN_FAILED]
    assert len(failed_events) >= 1

    payload = failed_events[0].payload
    assert "failure_category" in payload
    assert payload["failure_category"] == "timeout"
    assert "error_message" in payload
    assert "stage" in payload
    assert payload["stage"] == "llm_generation"
    assert "step" in payload
    assert payload["step"] == 1
    assert "retryable" in payload
    assert payload["retryable"] is True
    assert "timestamp" in payload


# ---------------------------------------------------------------------------
# Test 10: Report rendering of failure details
# ---------------------------------------------------------------------------

def test_report_rendering_of_failure_details(test_env):
    """ReportBuilder extracts failure details and renders them in Markdown and JSON."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        LLMTimeoutError("LLM request timed out after 60.0s: timed out")
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    loop.run()

    builder = ReportBuilder(store)
    report = builder.build_report(run_id)

    assert report.status == "failed"
    assert report.failure_details is not None
    assert report.failure_details["category"] == "timeout"

    # Verify JSON rendering
    json_out = report_to_json(report)
    parsed_json = json.loads(json_out)
    assert parsed_json["status"] == "failed"
    assert parsed_json["failure_details"]["category"] == "timeout"

    # Verify Markdown rendering
    md_out = report_to_markdown(report)
    assert "## Investigation Failure Details" in md_out
    assert "**Failure Category**: `timeout`" in md_out
    assert "**Execution Stage**: `llm_generation`" in md_out
    assert "**Retryable**: Yes" in md_out


# ---------------------------------------------------------------------------
# Test 11: Secret redaction in failure messages
# ---------------------------------------------------------------------------

def test_secret_redaction_in_failure_messages(test_env):
    """Secrets in provider error messages are scrubbed from events, summaries, and reports."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    raw_secret_error = (
        "Failed connecting with Authorization: Bearer sk-secretkey1234567890abcdef123456 and token ghp_abcdefghijklmnopqrstuvwxyz123456"
    )
    provider = ScriptedLLMProvider([
        LLMAuthenticationError(raw_secret_error)
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run()

    # Check InvestigationResult
    assert "sk-secretkey" not in result.error_message
    assert "ghp_abcdef" not in result.error_message
    assert "[REDACTED_SECRET]" in result.error_message

    # Check SQLite event payload
    events = store.list_run_events(run_id)
    failed_ev = [e for e in events if e.event_type == EventType.RUN_FAILED][0]
    payload_str = json.dumps(failed_ev.payload)
    assert "sk-secretkey" not in payload_str
    assert "ghp_abcdef" not in payload_str
    assert "[REDACTED_SECRET]" in payload_str

    # Check generated report
    builder = ReportBuilder(store)
    report = builder.build_report(run_id)
    md_out = report_to_markdown(report)
    assert "sk-secretkey" not in md_out
    assert "ghp_abcdef" not in md_out


# ---------------------------------------------------------------------------
# Test 12: Preservation of partial investigation history
# ---------------------------------------------------------------------------

def test_preservation_of_partial_investigation_history(test_env):
    """When a failure occurs late in a run, all prior findings, evidence, and tool calls are preserved."""
    workspace, db_path, store = test_env
    run_id = store.create_run(str(workspace))
    tools = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    provider = ScriptedLLMProvider([
        # Step 1: tool call
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(call_id="call_1", name="get_run_history", arguments={})],
        ),
        # Step 2: tool call
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(call_id="call_2", name="get_repository_map", arguments={})],
        ),
        # Step 3: late failure (e.g. timeout on turn 3 like in run_ce01ff96700b)
        LLMTimeoutError("LLM request timed out after 60.0s: timed out on turn 3"),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tools,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
    )

    result = loop.run()

    assert result.status == "failed"
    assert result.steps_executed == 3
    assert result.total_tool_calls == 2

    # Verify all prior tool events exist in SQLite
    events = store.list_run_events(run_id)
    tool_completed = [e for e in events if e.event_type == EventType.TOOL_CALL_COMPLETED]
    assert len(tool_completed) == 2

    # Verify report includes partial history
    builder = ReportBuilder(store)
    report = builder.build_report(run_id)
    assert report.status == "failed"
    assert len(report.events_timeline) >= 5  # run_created, 3x llm_started, 2x tool_req, 2x tool_comp, run_failed
    assert report.failure_details is not None
    assert report.failure_details["category"] == "timeout"
    assert report.failure_details["step"] == 3
