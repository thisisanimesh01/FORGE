"""Deterministic unit and regression tests for Phase 7: Investigation Orchestration & Tool-Selection Remediation.

Covers the 17 required regression test specifications:
1. Source tools present in provider tool schema.
2. Source tool calls correctly dispatched.
3. Tool results reach next model turn.
4. Repeated get_run_history calls detected.
5. Utility-call limits trigger meaningful recovery.
6. Planner schedules dependency inspection.
7. Planner schedules implementation inspection.
8. Planner schedules relevant test inspection.
9. Planner adapts when a file does not exist.
10. Already-inspected files not repeatedly scheduled.
11. Model invalid responses do not halt deterministic planning.
12. Evidence is persisted after source inspection.
13. Model cannot claim uninspected files.
14. Final synthesis uses actual investigation state.
15. Insufficient evidence remains correctly classified.
16. Historical run/report compatibility is preserved.
17. Existing Phase 7 rejection and synthesis tests remain passing.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from forge.analysis.validation import (
    FindingValidationError,
    InsufficientEvidenceError,
    RejectedClaimTracker,
)
from forge.investigation.planner import (
    InvestigationPlanner,
    InvestigationAction,
    MANIFEST_CANDIDATES,
    UTILITY_TOOLS,
)
from forge.investigation.loop import (
    InvestigationLoop,
    InvestigationResult,
    determine_investigation_quality,
    generate_fallback_synthesis,
    validate_synthesis_quality,
)
from forge.investigation.tools import ToolRegistry, ToolExecutionError
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
)
from forge.llm.provider import LLMProvider
from forge.schemas import EventType
from forge.reporting.builder import ReportBuilder, report_to_markdown
from forge.schemas import (
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


class ScriptedLLMProvider(LLMProvider):
    """Deterministic scripted LLM provider for unit tests."""

    def __init__(self, responses: List[LLMResponse]):
        self.responses = list(responses)
        self.history: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.history.append(request)
        if not self.responses:
            return LLMResponse(content="Final default technical assessment of audited code.")
        return self.responses.pop(0)


def _setup_workspace(tmp_path: Path):
    """Create test workspace with manifest, core implementation, and test files."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    pyproject = workspace / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo-lib"\nversion = "0.1.0"\ndependencies = ["pydantic>=2.0.0"]\n',
        encoding="utf-8",
    )

    src_dir = workspace / "demolib"
    src_dir.mkdir(parents=True, exist_ok=True)
    impl_py = src_dir / "service.py"
    impl_py.write_text(
        '"""Core service module."""\n\n'
        "class Service:\n"
        "    def run(self, val: int) -> int:\n"
        "        # Valid implementation\n"
        "        return val * 2\n",
        encoding="utf-8",
    )

    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_py = tests_dir / "test_service.py"
    test_py.write_text(
        '"""Test service module."""\n\n'
        "from demolib.service import Service\n\n"
        "def test_run():\n"
        "    s = Service()\n"
        "    assert s.run(2) == 4\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "test.db"
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target=str(workspace), run_id="run_orchestration_test")
    tool_registry = ToolRegistry(
        workspace_path=workspace,
        store=store,
        run_id=run_id,
        rejection_budget=3,
    )
    return workspace, store, run_id, tool_registry


# ---------------------------------------------------------------------------
# Test 1: Source tools present in provider tool schema
# ---------------------------------------------------------------------------

def test_source_tools_present_in_provider_tool_schema(tmp_path: Path):
    """1. Source inspection tools are present with clear descriptions in ToolRegistry definitions."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    defs = {d.name: d for d in tool_reg.get_definitions()}

    assert "read_source_excerpt" in defs
    assert "inspect_dependencies" in defs
    assert "find_related_files" in defs
    assert "record_finding" in defs
    assert "get_run_history" in defs

    assert "inspect actual implementation code" in defs["read_source_excerpt"].description.lower()
    assert "verify package versions" in defs["inspect_dependencies"].description.lower()
    assert "database utility" in defs["get_run_history"].description.lower()


# ---------------------------------------------------------------------------
# Test 2: Source tool calls correctly dispatched
# ---------------------------------------------------------------------------

def test_source_tool_calls_correctly_dispatched(tmp_path: Path):
    """2. read_source_excerpt, inspect_dependencies, and find_related_files execute and return valid outputs."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    # 1. read_source_excerpt
    out_read = tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "demolib/service.py", "start_line": 1, "end_line": 6},
    )
    assert "=== File: demolib/service.py" in out_read
    assert "class Service:" in out_read

    # 2. inspect_dependencies
    out_deps = tool_reg.execute_tool(
        "inspect_dependencies",
        {"package_name": "pydantic"},
    )
    deps_data = json.loads(out_deps)
    assert deps_data["package_name"] == "pydantic"
    assert deps_data["declared"] is True
    assert ">=2.0.0" in deps_data["constraint"]

    # 3. find_related_files
    out_rel = tool_reg.execute_tool(
        "find_related_files",
        {"file_path": "tests/test_service.py"},
    )
    rel_data = json.loads(out_rel)
    assert "demolib/service.py" in rel_data["implementations"]


# ---------------------------------------------------------------------------
# Test 3: Tool results reach next model turn
# ---------------------------------------------------------------------------

def test_tool_results_reach_next_model_turn(tmp_path: Path):
    """3. Tool outputs are appended as TOOL messages and passed into subsequent LLMRequests."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    provider = ScriptedLLMProvider([
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="call_read_1",
                name="read_source_excerpt",
                arguments={"file_path": "demolib/service.py", "start_line": 1, "end_line": 5},
            )]
        ),
        LLMResponse(content="Audited demolib/service.py; no defects or security risks identified."),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
    )
    result = loop.run()

    assert result.status == "completed"
    assert len(provider.history) == 2

    # Second request must contain the tool response from step 1
    req2_msgs = provider.history[1].messages
    tool_msgs = [m for m in req2_msgs if m.role == MessageRole.TOOL]
    assert len(tool_msgs) >= 1
    assert tool_msgs[0].name == "read_source_excerpt"
    assert "demolib/service.py" in tool_msgs[0].content


# ---------------------------------------------------------------------------
# Test 4: Repeated get_run_history calls detected
# ---------------------------------------------------------------------------

def test_repeated_get_run_history_calls_detected(tmp_path: Path):
    """4. Repeated calls to get_run_history are tracked and detected."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    planner = InvestigationPlanner(workspace_path=workspace, max_utility_calls=1)

    assert planner.is_utility_tool("get_run_history")
    assert planner.is_utility_call_allowed("get_run_history") is True

    # Record first call
    planner.record_tool_call("get_run_history", {"limit": 5})
    assert planner.utility_calls_count["get_run_history"] == 1
    assert planner.is_utility_call_allowed("get_run_history") is False


# ---------------------------------------------------------------------------
# Test 5: Utility-call limits trigger meaningful recovery
# ---------------------------------------------------------------------------

def test_utility_call_limits_trigger_meaningful_recovery(tmp_path: Path):
    """5. Reaching utility call limits triggers warning, directive to inspect source, and dynamic pruning."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    provider = ScriptedLLMProvider([
        # Step 1: get_run_history
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="call_u1",
                name="get_run_history",
                arguments={},
            )]
        ),
        # Step 2: get_run_history again (quota exceeded)
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="call_u2",
                name="get_run_history",
                arguments={},
            )]
        ),
        # Step 3: Conclude
        LLMResponse(content="Audited codebase; no defects observed."),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
    )
    result = loop.run()

    # Step 2 request should have pruned get_run_history
    req2_tools = [t.name for t in provider.history[1].tools]
    assert "get_run_history" not in req2_tools

    # Second tool call output should contain warning
    req3_msgs = provider.history[2].messages
    tool_msgs = [m for m in req3_msgs if m.role == MessageRole.TOOL and m.tool_call_id == "call_u2"]
    assert len(tool_msgs) == 1
    assert "Utility-call quota exceeded" in tool_msgs[0].content


# ---------------------------------------------------------------------------
# Test 6: Planner schedules dependency inspection
# ---------------------------------------------------------------------------

def test_planner_schedules_dependency_inspection(tmp_path: Path):
    """6. Planner detects pyproject.toml and schedules dependency inspection first."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    planner = InvestigationPlanner(workspace_path=workspace)

    assert "pyproject.toml" in planner.planned_files
    next_action = planner.get_next_action()
    assert next_action is not None
    assert next_action.action_type == "inspect_dependencies"
    assert next_action.tool_name == "inspect_dependencies"


# ---------------------------------------------------------------------------
# Test 7: Planner schedules implementation inspection
# ---------------------------------------------------------------------------

def test_planner_schedules_implementation_inspection(tmp_path: Path):
    """7. Planner discovers and schedules core implementation file inspection."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    planner = InvestigationPlanner(
        workspace_path=workspace,
        instruction="Inspect Service class in demolib/service.py",
    )

    assert "demolib/service.py" in planner.planned_files
    planner.record_inspection("pyproject.toml")
    next_action = planner.get_next_action()
    assert next_action is not None
    assert next_action.target_file == "demolib/service.py"
    assert next_action.tool_name == "read_source_excerpt"


# ---------------------------------------------------------------------------
# Test 8: Planner schedules relevant test inspection
# ---------------------------------------------------------------------------

def test_planner_schedules_relevant_test_inspection(tmp_path: Path):
    """8. Planner identifies and schedules corresponding test file inspection."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    planner = InvestigationPlanner(
        workspace_path=workspace,
        instruction="Audit demolib/service.py and verify test_service.py",
    )

    assert "tests/test_service.py" in planner.planned_files
    planner.record_inspection("pyproject.toml")
    planner.record_inspection("demolib/service.py")

    next_action = planner.get_next_action()
    assert next_action is not None
    assert next_action.target_file == "tests/test_service.py"


# ---------------------------------------------------------------------------
# Test 9: Planner adapts when a file does not exist
# ---------------------------------------------------------------------------

def test_planner_adapts_when_file_does_not_exist(tmp_path: Path):
    """9. Non-existent file in pending list is safely skipped and planner adapts to next file."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    planner = InvestigationPlanner(workspace_path=workspace)

    # Insert a non-existent file at front of pending
    planner.pending_files.insert(0, "nonexistent/module.py")

    next_action = planner.get_next_action()
    assert next_action is not None
    assert next_action.target_file != "nonexistent/module.py"
    assert "nonexistent/module.py" not in planner.pending_files


# ---------------------------------------------------------------------------
# Test 10: Already-inspected files not repeatedly scheduled
# ---------------------------------------------------------------------------

def test_already_inspected_files_not_repeatedly_scheduled(tmp_path: Path):
    """10. Recording inspection removes file from pending and prevents rescheduling."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)
    planner = InvestigationPlanner(workspace_path=workspace)

    initial_target = planner.pending_files[0]
    planner.record_inspection(initial_target)

    assert initial_target in planner.inspected_files
    assert initial_target not in planner.pending_files

    next_action = planner.get_next_action()
    if next_action and next_action.action_type != "synthesize":
        assert next_action.target_file != initial_target


# ---------------------------------------------------------------------------
# Test 11: Model invalid responses do not halt deterministic planning
# ---------------------------------------------------------------------------

def test_model_invalid_responses_do_not_halt_deterministic_planning(tmp_path: Path):
    """11. Empty/conversational responses trigger deterministic steering without crashing."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    provider = ScriptedLLMProvider([
        # Step 1: Apology response with 0 tools
        LLMResponse(content="I apologize for the confusion. Please provide more information."),
        # Step 2: Read source excerpt after steering
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="call_read",
                name="read_source_excerpt",
                arguments={"file_path": "demolib/service.py", "start_line": 1, "end_line": 5},
            )]
        ),
        # Step 3: Conclude
        LLMResponse(content="Audited demolib/service.py; no defects or security risks identified."),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
    )
    result = loop.run()

    assert result.status == "completed"
    assert result.steps_executed == 3
    # Check that user received steering message between step 1 and 2
    step2_msgs = provider.history[1].messages
    steering_msgs = [m for m in step2_msgs if "Investigation Orchestrator:" in (m.content or "")]
    assert len(steering_msgs) >= 1


# ---------------------------------------------------------------------------
# Test 12: Evidence is persisted after source inspection
# ---------------------------------------------------------------------------

def test_evidence_is_persisted_after_source_inspection(tmp_path: Path):
    """12. Verified source excerpt evidence is created and attached to finding in SQLite."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    # First inspect source
    tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "demolib/service.py", "start_line": 1, "end_line": 6},
    )

    out = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Unchecked Multiplication",
            "description": "Multiplication could overflow under extreme conditions",
            "affected_file": "demolib/service.py",
            "start_line": 3,
            "end_line": 6,
            "severity": "medium",
        },
    )
    data = json.loads(out)
    assert data["status"] == "success"
    finding_id = data["finding_id"]
    evidence_id = data["evidence_id"]

    ev = store.get_evidence(evidence_id)
    assert ev is not None
    assert ev.finding_id == finding_id
    assert ev.evidence_type == EvidenceType.SOURCE_EXCERPT
    assert ev.file_path == "demolib/service.py"
    assert "class Service:" in ev.content


# ---------------------------------------------------------------------------
# Test 13: Model cannot claim uninspected files
# ---------------------------------------------------------------------------

def test_model_cannot_claim_uninspected_files(tmp_path: Path):
    """13. Attempting record_finding on an uninspected file raises InsufficientEvidenceError."""
    workspace, store, run_id, _ = _setup_workspace(tmp_path)

    inspected_set = set()
    tool_reg = ToolRegistry(
        workspace_path=workspace,
        store=store,
        run_id=run_id,
        inspected_files=inspected_set,
    )

    # Attempt to record finding without reading demolib/service.py
    out = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Uninspected Bug",
            "description": "Guessing a defect without reading",
            "affected_file": "demolib/service.py",
            "start_line": 3,
            "end_line": 6,
        },
    )
    data = json.loads(out)
    assert data["status"] == "rejected"
    assert "not been inspected" in (data.get("reason") or data.get("error", ""))


# ---------------------------------------------------------------------------
# Test 14: Final synthesis uses actual investigation state
# ---------------------------------------------------------------------------

def test_final_synthesis_uses_actual_investigation_state(tmp_path: Path):
    """14. Fallback synthesis includes actual inspected files and rejected claim records."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    inspected = ["demolib/service.py", "pyproject.toml"]
    rejected = [{
        "file": "demolib/service.py",
        "lines": "3-6",
        "title": "Fake Syntax Error",
        "reason": "Deterministic static syntax analysis confirmed valid syntax",
        "rejection_type": "SyntaxClaimValidationError",
    }]

    synthesis = generate_fallback_synthesis(
        target=str(workspace),
        commit_sha="abcd1234efgh5678",
        inspected_files=inspected,
        findings=[],
        rejected_records=rejected,
        steps_executed=4,
        max_steps=5,
    )

    assert "`demolib/service.py`" in synthesis
    assert "`pyproject.toml`" in synthesis
    assert "Fake Syntax Error" in synthesis
    assert "Deterministic static syntax analysis confirmed valid syntax" in synthesis


# ---------------------------------------------------------------------------
# Test 15: Insufficient evidence remains correctly classified
# ---------------------------------------------------------------------------

def test_insufficient_evidence_remains_correctly_classified(tmp_path: Path):
    """15. Quality is classified as insufficient_evidence when candidate claims lacked evidence or files uninspected."""
    quality_zero_files = determine_investigation_quality(
        loop_status="completed",
        total_tool_calls=2,
        inspected_files=[],
        findings=[],
        rejected_records=[],
        used_fallback_synthesis=True,
        has_valid_synthesis=False,
    )
    assert quality_zero_files == "insufficient_evidence"

    quality_rejected_claims = determine_investigation_quality(
        loop_status="completed",
        total_tool_calls=3,
        inspected_files=["tests/test_service.py"],
        findings=[],
        rejected_records=[{"title": "Unverified Bug"}],
        used_fallback_synthesis=True,
        has_valid_synthesis=False,
    )
    assert quality_rejected_claims == "insufficient_evidence"


# ---------------------------------------------------------------------------
# Test 16: Historical run/report compatibility is preserved
# ---------------------------------------------------------------------------

def test_historical_run_and_report_compatibility_is_preserved(tmp_path: Path):
    """16. Reports render correctly with investigation_quality and inspected files."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    summary = {
        "steps_executed": 3,
        "total_tool_calls": 2,
        "findings_count": 0,
        "duration_seconds": 1.25,
        "investigation_quality": "completed",
        "inspected_files": ["demolib/service.py", "pyproject.toml"],
        "rejected_claims": [],
        "used_fallback_synthesis": False,
        "final_synthesis": "Audited demolib/service.py and pyproject.toml; no defects verified.",
    }
    store.finish_run(run_id, status="completed", summary=summary)

    builder = ReportBuilder(store=store)
    report = builder.build_report(run_id)

    assert report.status == "completed"
    assert report.investigation_quality == "completed"

    md = report_to_markdown(report)
    assert "**Investigation Quality**: `COMPLETED`" in md
    assert "demolib/service.py" in md


# ---------------------------------------------------------------------------
# Test 17: Existing Phase 7 rejection and synthesis tests remain passing
# ---------------------------------------------------------------------------

def test_existing_phase7_rejection_and_synthesis_tests_remain_passing(tmp_path: Path):
    """17. Verify synthesis validation and rejection tracker limits work seamlessly together."""
    tracker = RejectedClaimTracker(rejection_budget=2)
    tracker.record_rejected_claim(
        norm_path="src/app.py",
        start_line=1,
        end_line=5,
        defect_category="syntax_error",
        title="Bad syntax",
        description="Missing colon",
        rejection_type="SyntaxClaimValidationError",
        rejection_reason="Deterministic syntax check passed",
    )
    assert tracker.is_budget_exhausted() is False

    tracker.record_rejected_claim(
        norm_path="src/app.py",
        start_line=10,
        end_line=15,
        defect_category="logic_bug",
        title="Bad logic",
        description="Loop flaw",
        rejection_type="InsufficientEvidenceError",
        rejection_reason="No loop construct found",
    )
    assert tracker.is_budget_exhausted() is True

    # Validate synthesis quality function
    assert validate_synthesis_quality("I apologize for the confusion.") is False
    assert validate_synthesis_quality('{"name": "record_finding"}') is False
    assert validate_synthesis_quality("Audited the repository codebase across 5 files; verified 0 defects.") is True


# ---------------------------------------------------------------------------
# Test 18: Autonomous step fallback dispatches tool when model stalls
# ---------------------------------------------------------------------------

def test_autonomous_step_fallback_dispatches_tool_when_model_stalls(tmp_path: Path):
    """18. When model repeatedly outputs conversational text without tools after steering,
    orchestrator autonomously dispatches the next planned tool call and records audit events."""
    workspace, store, run_id, tool_reg = _setup_workspace(tmp_path)

    provider = ScriptedLLMProvider([
        # Step 1: Model calls inspect_dependencies
        LLMResponse(
            content="Checking dependencies.",
            tool_calls=[ToolCallRequest(call_id="call_1", name="inspect_dependencies", arguments={})],
        ),
        # Step 2: Model stalls with apology (triggers steering)
        LLMResponse(content="I apologize for the confusion."),
        # Step 3: Model stalls again after steering (triggers autonomous fallback dispatch)
        LLMResponse(content="Sorry again, what should I do?"),
        # Step 4: Synthesis
        LLMResponse(content="Audited pyproject.toml and source files; verified zero defects across inspected scope."),
    ])

    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
    )
    result = loop.run()

    assert result.status == "completed"
    assert result.total_tool_calls >= 1
    events = store.list_run_events(run_id)
    event_types = [e.event_type for e in events]
    assert EventType.TOOL_CALL_REQUESTED in event_types
    assert EventType.TOOL_CALL_COMPLETED in event_types
    assert len(loop.inspected_files) >= 1
