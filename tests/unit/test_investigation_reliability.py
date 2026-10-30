"""Comprehensive regression tests for Investigation Engine Reliability.

Covers:
1. Native OpenAI structured tool calls still work.
2. Direct JSON tool-call extraction from message content.
3. Markdown fenced JSON extraction (```json ... ```).
4. <tool_call> tag extraction.
5. Unknown tool names are rejected.
6. Invalid JSON is rejected safely without crashing.
7. Invalid arguments are rejected safely against tool schema.
8. Ordinary explanatory JSON is not mistaken for a tool call.
9. Extracted tool calls are dispatched by the investigation loop.
10. Tool-call audit events (TOOL_CALL_REQUESTED, TOOL_CALL_COMPLETED) are recorded.
11. Investigation advances to the next turn after successful dispatch.
12. Ambiguous first response cannot produce false successful completion.
13. Maximum-turn limits are respected.
14. Legitimate final assessment can still complete an investigation.
15. Fake provider and integration tests remain valid.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from forge.investigation.loop import InvestigationLoop, InvestigationResult
from forge.investigation.tools import ToolRegistry
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
    ToolDefinition,
)
from forge.llm.openai import (
    OpenAICompatibleProvider,
    extract_content_tool_calls,
    validate_tool_arguments,
)
from forge.schemas import EventType
from forge.storage.store import ForgeStore


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

def _sample_tools() -> List[ToolDefinition]:
    """Return standard investigation tool definitions for tests."""
    return [
        ToolDefinition(
            name="get_repository_map",
            description="Get high-level repository architecture.",
            parameters={"type": "object", "properties": {}},
        ),
        ToolDefinition(
            name="search_symbols",
            description="Search indexed symbols by query.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Symbol query"},
                    "symbol_type": {"type": "string", "enum": ["class", "function", "method", "import"]},
                },
                "required": ["query"],
            },
        ),
        ToolDefinition(
            name="read_source_excerpt",
            description="Read lines from a file.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["file_path"],
            },
        ),
        ToolDefinition(
            name="run_sandbox_check",
            description="Execute isolated check.",
            parameters={
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "enum": ["pytest", "python_ast_syntax", "security_bandit"]},
                },
                "required": ["check_id"],
            },
        ),
    ]


def _make_mock_provider(mock_response_data: Dict[str, Any]) -> OpenAICompatibleProvider:
    """Create an OpenAICompatibleProvider with a mocked HTTP response."""
    prov = OpenAICompatibleProvider(api_key="mock_key", api_base="http://localhost:11434/v1", model="test-model")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_response_data

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    prov._get_client = MagicMock(return_value=mock_client)
    return prov


# ---------------------------------------------------------------------------
# 1. Native Structured Tool Calls
# ---------------------------------------------------------------------------

def test_native_structured_tool_calls_preserved():
    """Native OpenAI message.tool_calls wire format is preserved and parsed."""
    tools = _sample_tools()
    mock_data = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_native_01",
                    "type": "function",
                    "function": {
                        "name": "get_repository_map",
                        "arguments": "{}",
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    provider = _make_mock_provider(mock_data)
    req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="Audit")], tools=tools)
    resp = provider.generate(req)

    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].call_id == "call_native_01"
    assert resp.tool_calls[0].name == "get_repository_map"
    assert resp.tool_calls[0].arguments == {}
    assert resp.finish_reason == "tool_calls"
    assert resp.extraction_error is None


# ---------------------------------------------------------------------------
# 2. Direct JSON Tool-Call Extraction
# ---------------------------------------------------------------------------

def test_direct_json_tool_call_extraction():
    """Direct JSON object in message content is extracted when structured tool_calls is absent."""
    tools = _sample_tools()
    content = '{"name": "get_repository_map", "arguments": {}}'
    extracted, err = extract_content_tool_calls(content, tools)

    assert err is None
    assert len(extracted) == 1
    assert extracted[0].name == "get_repository_map"
    assert extracted[0].arguments == {}

    # Verify provider integration
    mock_data = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }],
    }
    provider = _make_mock_provider(mock_data)
    req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="Audit")], tools=tools)
    resp = provider.generate(req)

    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_repository_map"
    assert resp.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# 3. Markdown Fenced JSON Extraction
# ---------------------------------------------------------------------------

def test_markdown_fenced_json_extraction():
    """JSON inside Markdown code block is extracted."""
    tools = _sample_tools()
    content = (
        "I will search for the authentication symbol:\n"
        "```json\n"
        '{\n  "name": "search_symbols",\n  "arguments": {"query": "auth", "symbol_type": "class"}\n}\n'
        "```\n"
        "Awaiting results."
    )
    extracted, err = extract_content_tool_calls(content, tools)

    assert err is None
    assert len(extracted) == 1
    assert extracted[0].name == "search_symbols"
    assert extracted[0].arguments == {"query": "auth", "symbol_type": "class"}


# ---------------------------------------------------------------------------
# 4. <tool_call> Tag Extraction
# ---------------------------------------------------------------------------

def test_tool_call_tag_extraction():
    """Tool call inside <tool_call> tags is extracted."""
    tools = _sample_tools()
    content = (
        "Let me inspect the file:\n"
        "<tool_call>\n"
        '{"name": "read_source_excerpt", "arguments": {"file_path": "hamilton/driver.py", "start_line": 1, "end_line": 25}}\n'
        "</tool_call>"
    )
    extracted, err = extract_content_tool_calls(content, tools)

    assert err is None
    assert len(extracted) == 1
    assert extracted[0].name == "read_source_excerpt"
    assert extracted[0].arguments == {
        "file_path": "hamilton/driver.py",
        "start_line": 1,
        "end_line": 25,
    }


# ---------------------------------------------------------------------------
# 5. Unknown Tool Names Rejected
# ---------------------------------------------------------------------------

def test_unknown_tool_name_rejected():
    """Tool names not present in authorized tools are rejected with a clear diagnostic."""
    tools = _sample_tools()
    content = '{"name": "execute_shell_command", "arguments": {"cmd": "ls -la"}}'
    extracted, err = extract_content_tool_calls(content, tools)

    assert len(extracted) == 0
    assert err is not None
    assert "execute_shell_command" in err
    assert "authorized tools" in err.lower()


# ---------------------------------------------------------------------------
# 6. Invalid JSON Rejected Safely
# ---------------------------------------------------------------------------

def test_invalid_json_rejected_safely():
    """Malformed JSON inside tool_call tag is rejected without raising uncaught exceptions."""
    tools = _sample_tools()
    content = "<tool_call>{'name': get_repository_map, unquoted: true}</tool_call>"
    extracted, err = extract_content_tool_calls(content, tools)

    assert len(extracted) == 0
    assert err is not None
    assert "Malformed JSON" in err


# ---------------------------------------------------------------------------
# 7. Invalid Arguments Rejected Against Schema
# ---------------------------------------------------------------------------

def test_missing_required_arguments_rejected():
    """Tool call missing required parameters is rejected."""
    tools = _sample_tools()
    # search_symbols requires 'query'
    content = '{"name": "search_symbols", "arguments": {"symbol_type": "class"}}'
    extracted, err = extract_content_tool_calls(content, tools)

    assert len(extracted) == 0
    assert err is not None
    assert "Missing required parameter 'query'" in err


def test_invalid_argument_type_rejected():
    """Tool call with invalid argument type (e.g. integer for file_path) is rejected."""
    tools = _sample_tools()
    content = '{"name": "read_source_excerpt", "arguments": {"file_path": 12345}}'
    extracted, err = extract_content_tool_calls(content, tools)

    assert len(extracted) == 0
    assert err is not None
    assert "must be a string" in err


def test_invalid_enum_argument_rejected():
    """Tool call with disallowed enum value is rejected."""
    tools = _sample_tools()
    content = '{"name": "run_sandbox_check", "arguments": {"check_id": "unauthorized_check"}}'
    extracted, err = extract_content_tool_calls(content, tools)

    assert len(extracted) == 0
    assert err is not None
    assert "not an allowed enum value" in err


# ---------------------------------------------------------------------------
# 8. Ordinary Explanatory JSON Not Mistaken for Tool Call
# ---------------------------------------------------------------------------

def test_ordinary_explanatory_json_not_mistaken_for_tool_call():
    """Arbitrary JSON without tool call semantics is treated as ordinary prose."""
    tools = _sample_tools()
    content = (
        "Here is the database configuration found in the settings file:\n"
        '{"db_type": "postgresql", "port": 5432, "max_connections": 100}\n'
        "No further tools needed for this observation."
    )
    extracted, err = extract_content_tool_calls(content, tools)

    assert len(extracted) == 0
    assert err is None  # No extraction error because this was not an attempted tool call


# ---------------------------------------------------------------------------
# Investigation Loop Integration Tests
# ---------------------------------------------------------------------------

class TestInvestigationLoopReliability:
    """Tests for InvestigationLoop handling extracted tools and termination invariants."""

    def _make_loop(
        self,
        tmp_path: Path,
        provider: Any,
        max_steps: int = 5,
    ) -> tuple[InvestigationLoop, ForgeStore]:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        # Create a test file for read_source_excerpt
        (workspace / "test.py").write_text("print('hello')\nprint('world')\n", encoding="utf-8")

        store = ForgeStore(db_path=tmp_path / "test.db")
        run_id = store.create_run(target="local/test", run_id="run_rel")
        tool_reg = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

        loop = InvestigationLoop(
            provider=provider,
            tool_registry=tool_reg,
            store=store,
            run_id=run_id,
            target="local/test",
            max_steps=max_steps,
        )
        return loop, store

    # 9, 10, 11. Extracted tool calls dispatched, events logged, loop advances
    def test_extracted_tool_call_dispatched_and_recorded(self, tmp_path: Path):
        """Tool call extracted from content is executed, audit events recorded, and advances to step 2."""
        step1_response = LLMResponse(
            content='{"name": "get_repository_map", "arguments": {}}',
            tool_calls=[
                ToolCallRequest(call_id="call_text_1", name="get_repository_map", arguments={}),
            ],
            finish_reason="tool_calls",
        )
        step2_response = LLMResponse(
            content="Audit completed: Repository map inspected and verified clean.",
            tool_calls=[],
            finish_reason="stop",
        )

        provider = FakeLLMProvider(scripted_responses=[step1_response, step2_response])
        loop, store = self._make_loop(tmp_path, provider=provider, max_steps=5)
        result = loop.run()

        # 9. Tool dispatched
        assert result.total_tool_calls == 1
        # 11. Advanced to step 2
        assert result.steps_executed == 2
        assert result.status == "completed"

        # 10. Audit events recorded in order
        events = store.list_run_events("run_rel")
        event_types = [e.event_type for e in events]
        assert EventType.TOOL_CALL_REQUESTED in event_types
        assert EventType.TOOL_CALL_COMPLETED in event_types
        assert EventType.RUN_COMPLETED in event_types

        req_events = [e for e in events if e.event_type == EventType.TOOL_CALL_REQUESTED]
        assert req_events[0].payload["tool_name"] == "get_repository_map"

    # 12. Ambiguous first response cannot produce false successful completion
    def test_ambiguous_first_response_cannot_produce_false_completion(self, tmp_path: Path):
        """Ambiguous prose on step 1 with 0 tools fails honestly rather than claiming COMPLETED."""
        # Provider returns non-tool prose on step 1 and step 2
        provider = FakeLLMProvider(scripted_responses=[
            LLMResponse(content="I am ready to audit the repository.", tool_calls=[], finish_reason="stop"),
            LLMResponse(content="Still just talking without calling tools.", tool_calls=[], finish_reason="stop"),
        ])
        loop, store = self._make_loop(tmp_path, provider=provider, max_steps=5)
        result = loop.run()

        # Must NOT be marked completed
        assert result.status == "failed"
        assert result.total_tool_calls == 0
        assert "Zero tools executed" in (result.error_message or "")

        # Verify SQLite store record matches
        run = store.get_run("run_rel")
        assert run.status == "failed"

        # Verify RUN_FAILED event was emitted
        events = store.list_run_events("run_rel")
        assert EventType.RUN_FAILED in [e.event_type for e in events]
        assert EventType.RUN_COMPLETED not in [e.event_type for e in events]

    # 13. Maximum-turn limits are respected
    def test_maximum_turn_budget_enforced(self, tmp_path: Path):
        """Investigation halts cleanly when max_steps limit is reached."""
        always_tool = FakeLLMProvider(scripted_responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(call_id=f"c_{i}", name="get_repository_map", arguments={})],
                finish_reason="tool_calls",
            )
            for i in range(10)
        ])
        loop, store = self._make_loop(tmp_path, provider=always_tool, max_steps=2)
        result = loop.run()

        assert result.steps_executed == 2
        assert result.status == "max_steps_exceeded"
        assert "limit of 2 turns" in (result.final_response or "")

    # 14. Legitimate final assessment completes after tools
    def test_legitimate_final_assessment_completes_investigation(self, tmp_path: Path):
        """Legitimate final assessment following tool executions finishes as completed."""
        provider = FakeLLMProvider(scripted_responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(call_id="c_1", name="get_repository_map", arguments={})],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Architecture analyzed. No security defects identified in Python modules.",
                tool_calls=[],
                finish_reason="stop",
            ),
        ])
        loop, store = self._make_loop(tmp_path, provider=provider, max_steps=5)
        result = loop.run()

        assert result.status == "completed"
        assert result.steps_executed == 2
        assert result.total_tool_calls == 1
        assert "No security defects identified" in (result.final_response or "")

    # Bounded diagnostic correction of invalid tool call
    def test_malformed_tool_call_gets_correction_opportunity(self, tmp_path: Path):
        """Malformed tool call gets diagnostic feedback and model can correct it on step 2."""
        provider = FakeLLMProvider(scripted_responses=[
            LLMResponse(
                content='{"name": "search_symbols", "arguments": {}}',
                tool_calls=[],
                finish_reason="stop",
                extraction_error="Missing required parameter 'query' for tool 'search_symbols'",
            ),
            LLMResponse(
                content='{"name": "search_symbols", "arguments": {"query": "driver"}}',
                tool_calls=[
                    ToolCallRequest(call_id="c_corr", name="search_symbols", arguments={"query": "driver"}),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Audit complete after symbol search.",
                tool_calls=[],
                finish_reason="stop",
            ),
        ])
        loop, store = self._make_loop(tmp_path, provider=provider, max_steps=5)
        result = loop.run()

        # Step 1 corrected on Step 2; completed on Step 3
        assert result.status == "completed"
        assert result.steps_executed == 3
        assert result.total_tool_calls == 1
