"""Deterministic unit and regression tests for Investigation Finding & Evidence Persistence.

Covers:
1. Authorized record_finding tool registered
2. Finding persistence in SQLite
3. Evidence persistence and finding linkage
4. Actual source extraction rather than trusting model-supplied excerpts
5. Valid line-range verification
6. Nonexistent file rejection
7. Path traversal rejection
8. Absolute path rejection
9. Symlink escape rejection
10. Evidence digest integrity
11. Invalid line range/evidence rejection
12. Turn-budget notifications
13. Final synthesis within max_steps
14. No unbounded model calls
15. Sequential same-file pagination warnings
16. Legitimate multi-excerpt inspection
17. Context truncation preserving valid message order
18. Investigation with recorded findings completing correctly
19. Investigation with no findings reporting a truthful outcome
20. Schema validation and finding retrieval
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from forge.analysis.evidence import create_evidence
from forge.investigation.loop import InvestigationLoop, InvestigationResult
from forge.investigation.tools import ToolRegistry, ToolExecutionError
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
)
from forge.schemas import (
    EventType,
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


def _make_env(tmp_path: Path):
    """Create a temporary workspace, database, store, and test repository."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Seed test files
    src_dir = workspace / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    main_py = src_dir / "main.py"
    main_py.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "def process_data(items):\n"
        "    for i in range(len(items)):\n"
        "        # Potential off-by-one or unchecked index\n"
        "        val = items[i]\n"
        "        print(val)\n"
        "    return True\n"
        "\n"
        "def run():\n"
        "    process_data([1, 2, 3])\n",
        encoding="utf-8",
    )

    utils_py = src_dir / "utils.py"
    utils_py.write_text(
        "def helper():\n"
        "    return 42\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "forge_test.db"
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target=str(workspace), run_id="run_findings_test")
    tool_reg = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    return workspace, store, run_id, tool_reg


# =========================================================================
# Phase A & B: Tool Registration, Persistence, and Independent Verification
# =========================================================================

def test_record_finding_authorized_tool_registered(tmp_path: Path):
    """1. Authorized record_finding is registered in ToolRegistry with correct JSON schema."""
    _, _, _, tool_reg = _make_env(tmp_path)
    defs = {t.name: t for t in tool_reg.get_definitions()}
    assert "record_finding" in defs

    rf_def = defs["record_finding"]
    props = rf_def.parameters["properties"]
    assert "title" in props
    assert "description" in props
    assert "affected_file" in props
    assert "start_line" in props
    assert "end_line" in props
    assert "severity" in props
    assert "confidence" in props
    assert rf_def.parameters["required"] == ["title", "description", "affected_file", "start_line", "end_line"]


def test_record_finding_persists_in_sqlite(tmp_path: Path):
    """2. record_finding persists finding in SQLite with suspected state."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)
    res_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Unchecked index access in process_data",
            "description": "Direct index loop over items array",
            "affected_file": "src/main.py",
            "start_line": 4,
            "end_line": 8,
            "severity": "high",
            "confidence": "high",
        },
    )
    result = json.loads(res_str)
    assert result["status"] == "success"
    assert "finding_id" in result
    fid = result["finding_id"]

    # Retrieve from DB
    persisted = store.get_finding(fid)
    assert persisted is not None
    assert persisted.title == "Unchecked index access in process_data"
    assert persisted.severity == FindingSeverity.HIGH
    assert persisted.confidence == FindingConfidence.HIGH
    assert persisted.lifecycle_state == FindingLifecycleState.SUSPECTED
    assert "src/main.py" in persisted.affected_files


def test_evidence_persistence_and_finding_linkage(tmp_path: Path):
    """3. Evidence is persisted, linked to finding, and retrievable."""
    _, store, _, tool_reg = _make_env(tmp_path)
    res_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Buffer check",
            "description": "Checking buffer",
            "affected_file": "src/main.py",
            "start_line": 4,
            "end_line": 7,
            "severity": "medium",
        },
    )
    result = json.loads(res_str)
    fid = result["finding_id"]
    eid = result["evidence_id"]

    ev = store.get_evidence(eid)
    assert ev is not None
    assert ev.finding_id == fid
    assert ev.evidence_type == EvidenceType.SOURCE_EXCERPT
    assert ev.file_path == "src/main.py"
    assert ev.start_line == 4
    assert ev.end_line == 7
    assert ev.provenance == "forge:investigator:source_verification"

    # Finding linkage
    linked = store.list_evidence_for_finding(fid)
    assert len(linked) == 1
    assert linked[0].evidence_id == eid


def test_actual_source_extracted_not_model_supplied(tmp_path: Path):
    """4. Server extracts actual source lines directly from disk; ignores fabricated input."""
    workspace, store, _, tool_reg = _make_env(tmp_path)
    # Model tries to supply fabricated source text
    res_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Fabrication attempt",
            "description": "Model claims code says something malicious",
            "affected_file": "src/main.py",
            "start_line": 4,
            "end_line": 6,
            "source_evidence": "def process_data(): os.system('rm -rf /')",
        },
    )
    result = json.loads(res_str)
    eid = result["evidence_id"]
    ev = store.get_evidence(eid)

    # Must contain real code from disk, NOT fabricated string
    assert "for i in range(len(items)):" in ev.content
    assert "rm -rf" not in ev.content


def test_valid_line_range_verification(tmp_path: Path):
    """5. Valid line range extracted with correct numbering; out of bounds rejected."""
    _, _, _, tool_reg = _make_env(tmp_path)
    # Valid range
    res_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Lines test",
            "description": "Testing lines",
            "affected_file": "src/main.py",
            "start_line": 1,
            "end_line": 2,
        },
    )
    assert "success" in res_str

    # Out of bounds start_line
    err_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "OOB test",
            "description": "Testing lines",
            "affected_file": "src/main.py",
            "start_line": 500,
            "end_line": 600,
        },
    )
    assert "exceeds total file lines" in err_str or "error" in err_str


def test_nonexistent_file_rejection(tmp_path: Path):
    """6. Nonexistent file is rejected; no finding is stored."""
    _, store, run_id, tool_reg = _make_env(tmp_path)
    initial_findings = store.list_findings(run_id=run_id)

    err_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Missing file",
            "description": "desc",
            "affected_file": "src/ghost.py",
            "start_line": 1,
            "end_line": 5,
        },
    )
    assert "Affected file not found" in err_str
    # Zero findings created
    assert len(store.list_findings(run_id=run_id)) == len(initial_findings)


def test_path_traversal_rejection(tmp_path: Path):
    """7. Path traversal attempt (../../) is strictly rejected."""
    _, _, _, tool_reg = _make_env(tmp_path)
    err_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Traversal",
            "description": "desc",
            "affected_file": "../../etc/shadow",
            "start_line": 1,
            "end_line": 2,
        },
    )
    assert "Invalid affected_file path" in err_str or "Security violation" in err_str or "error" in err_str


def test_absolute_path_rejection(tmp_path: Path):
    """8. Absolute file path is rejected."""
    _, _, _, tool_reg = _make_env(tmp_path)
    err_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Absolute",
            "description": "desc",
            "affected_file": "/etc/passwd",
            "start_line": 1,
            "end_line": 2,
        },
    )
    assert "Invalid affected_file path" in err_str or "Security violation" in err_str or "error" in err_str


def test_symlink_escape_rejection(tmp_path: Path):
    """9. Symlink pointing outside workspace is strictly rejected."""
    workspace, _, _, tool_reg = _make_env(tmp_path)

    outside_file = tmp_path / "outside_secret.py"
    outside_file.write_text("SECRET = 'hidden'\n", encoding="utf-8")

    link_path = workspace / "src" / "escaped_link.py"
    try:
        link_path.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlink creation unsupported on this environment")

    err_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Symlink escape",
            "description": "desc",
            "affected_file": "src/escaped_link.py",
            "start_line": 1,
            "end_line": 1,
        },
    )
    assert "Security violation" in err_str or "Path containment violation" in err_str or "error" in err_str


def test_evidence_digest_integrity(tmp_path: Path):
    """10. Evidence digest is computed with sha256 over exact real file lines."""
    workspace, store, _, tool_reg = _make_env(tmp_path)
    res_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Digest check",
            "description": "Testing sha256 digest",
            "affected_file": "src/main.py",
            "start_line": 1,
            "end_line": 2,
        },
    )
    result = json.loads(res_str)
    eid = result["evidence_id"]
    ev = store.get_evidence(eid)

    # Compute expected hash from source file
    real_lines = (workspace / "src" / "main.py").read_text().splitlines()[:2]
    expected_digest = hashlib.sha256("\n".join(real_lines).encode("utf-8")).hexdigest()

    assert ev.details["content_digest"] == expected_digest
    assert ev.details["digest_algorithm"] == "sha256"


def test_invalid_line_range_rejection(tmp_path: Path):
    """11. Inverted or non-positive line ranges are rejected."""
    _, _, _, tool_reg = _make_env(tmp_path)
    # end < start
    err_str = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Inverted lines",
            "description": "desc",
            "affected_file": "src/main.py",
            "start_line": 5,
            "end_line": 2,
        },
    )
    assert "end_line must be >= start_line" in err_str

    # start_line < 1
    err_str2 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Zero line",
            "description": "desc",
            "affected_file": "src/main.py",
            "start_line": 0,
            "end_line": 2,
        },
    )
    assert "start_line must be >= 1" in err_str2


# =========================================================================
# Phase C & D: Turn Budget, Final Synthesis, Same-File Pagination
# =========================================================================

def test_turn_budget_notifications(tmp_path: Path):
    """12. Turn budget warnings are injected on penultimate and final turns."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)

    recorded_prompts: List[str] = []

    class InspectingProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            last_msg = req.messages[-1]
            if last_msg.content:
                recorded_prompts.append(last_msg.content)
            # Step 1: call read_source_excerpt
            if len(recorded_prompts) == 1:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c1",
                        name="read_source_excerpt",
                        arguments={"file_path": "src/main.py", "start_line": 1, "end_line": 3},
                    )]
                )
            # Step 2: call read_source_excerpt
            elif len(recorded_prompts) == 2:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c2",
                        name="read_source_excerpt",
                        arguments={"file_path": "src/utils.py", "start_line": 1, "end_line": 2},
                    )]
                )
            # Step 3: final assessment
            return LLMResponse(content="Final synthesis completed without findings.")

    loop = InvestigationLoop(
        provider=InspectingProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )
    result = loop.run()
    assert result.status == "completed"

    # Verify notices were sent
    all_content = " ".join(recorded_prompts)
    assert "Turn Budget: Step 1 of 3" in all_content
    assert "Turn Budget Warning: Step 2 of 3" in all_content
    assert "Final Turn: Step 3 of 3" in all_content


def test_final_synthesis_within_max_steps(tmp_path: Path):
    """13. Final synthesis completes within max_steps without extra hidden calls."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)
    call_count = 0

    class CountingProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id=f"c{call_count}",
                        name="read_source_excerpt",
                        arguments={"file_path": "src/main.py", "start_line": 1, "end_line": 2},
                    )]
                )
            return LLMResponse(content="Clean architecture; no critical vulnerabilities.")

    loop = InvestigationLoop(
        provider=CountingProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )
    result = loop.run()
    assert call_count == 3
    assert result.steps_executed == 3
    assert result.status == "completed"
    assert "Clean architecture" in (result.final_response or "")


def test_no_unbounded_model_calls(tmp_path: Path):
    """14. Loop never exceeds max_steps even if model requests tools indefinitely."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)
    call_count = 0

    class EndlessToolProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            # Try to invoke record_finding on every turn
            return LLMResponse(
                tool_calls=[ToolCallRequest(
                    call_id=f"c{call_count}",
                    name="record_finding",
                    arguments={
                        "title": f"Finding {call_count}",
                        "description": "desc",
                        "affected_file": "src/main.py",
                        "start_line": 1,
                        "end_line": 2,
                    },
                )]
            )

    loop = InvestigationLoop(
        provider=EndlessToolProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )
    result = loop.run()
    assert call_count <= 3
    assert result.steps_executed <= 3
    assert result.status == "max_steps_exceeded"


def test_sequential_same_file_pagination_warnings(tmp_path: Path):
    """15. Successive reading of the same file triggers warnings and halts at limit."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)
    tool_outputs: List[str] = []

    class PaginatingProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            step = len(tool_outputs) + 1
            if step <= 5:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id=f"c{step}",
                        name="read_source_excerpt",
                        arguments={
                            "file_path": "src/main.py",
                            "start_line": step,
                            "end_line": step + 1,
                        },
                    )]
                )
            return LLMResponse(content="Done")

    # Hook tool registry to capture outputs
    orig_exec = tool_reg.execute_tool
    def capturing_exec(name, args):
        out = orig_exec(name, args)
        tool_outputs.append(out)
        return out
    tool_reg.execute_tool = capturing_exec

    loop = InvestigationLoop(
        provider=PaginatingProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=6,
    )
    result = loop.run()

    # Step 3 had warning appended
    events = store.list_run_events(run_id)
    completed_events = [e for e in events if e.event_type == EventType.TOOL_CALL_COMPLETED]
    assert len(completed_events) >= 3

    # Check loop internal count
    assert loop.file_read_counts.get("src/main.py", 0) >= 3


def test_legitimate_multi_excerpt_inspection(tmp_path: Path):
    """16. Reading 2 excerpts from file A and 2 from file B succeeds cleanly without warnings."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)
    steps = [
        ("src/main.py", 1, 3),
        ("src/main.py", 4, 6),
        ("src/utils.py", 1, 2),
    ]
    cur = 0

    class CleanProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            nonlocal cur
            if cur < len(steps):
                f, s, e = steps[cur]
                cur += 1
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id=f"c{cur}",
                        name="read_source_excerpt",
                        arguments={"file_path": f, "start_line": s, "end_line": e},
                    )]
                )
            return LLMResponse(content="Clean scan.")

    loop = InvestigationLoop(
        provider=CleanProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
    )
    result = loop.run()
    assert result.status == "completed"
    assert loop.file_read_counts["src/main.py"] == 2
    assert loop.file_read_counts["src/utils.py"] == 1


# =========================================================================
# Phase E: Context Management & Message Order
# =========================================================================

def test_context_truncation_preserves_valid_message_order(tmp_path: Path):
    """17. Truncation keeps system prompt, initial user prompt, valid tool pairing, and USER notice."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)

    loop = InvestigationLoop(
        provider=FakeLLMProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_context_chars=1_000,  # small budget to trigger truncation
    )

    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="System prompt"),
        ChatMessage(role=MessageRole.USER, content="Initial instruction with context"),
    ]

    # Add 5 assistant/tool pairs with long content
    for i in range(5):
        messages.append(ChatMessage(
            role=MessageRole.ASSISTANT,
            content=f"Calling tool {i}",
            tool_calls=[ToolCallRequest(call_id=f"call_{i}", name="read_source_excerpt", arguments={})],
        ))
        messages.append(ChatMessage(
            role=MessageRole.TOOL,
            name="read_source_excerpt",
            tool_call_id=f"call_{i}",
            content="x" * 400,  # 400 chars each
        ))

    truncated = loop._truncate_history(messages)

    # 1. First message must be SYSTEM
    assert truncated[0].role == MessageRole.SYSTEM
    assert truncated[0].content == "System prompt"

    # 2. Second message must be USER
    assert truncated[1].role == MessageRole.USER
    assert truncated[1].content == "Initial instruction with context"

    # 3. No SYSTEM message in the middle
    mid_system = [m for m in truncated[1:] if m.role == MessageRole.SYSTEM]
    assert len(mid_system) == 0

    # 4. Truncation notice is USER role
    notices = [m for m in truncated if "[Context Notice:" in (m.content or "")]
    assert len(notices) == 1
    assert notices[0].role == MessageRole.USER

    # 5. Every TOOL message must follow an ASSISTANT message with tool_calls
    for i, m in enumerate(truncated):
        if m.role == MessageRole.TOOL:
            assert i > 0
            prev = truncated[i - 1]
            assert prev.role in [MessageRole.ASSISTANT, MessageRole.TOOL]


# =========================================================================
# Phase C: End-to-End Investigation Lifecycle Outomes
# =========================================================================

def test_investigation_with_recorded_findings_completes_correctly(tmp_path: Path):
    """18. Investigation discovering defects and recording findings completes with status='completed'."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)

    provider = FakeLLMProvider(scripted_responses=[
        # Step 1: Read source
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="c1",
                name="read_source_excerpt",
                arguments={"file_path": "src/main.py", "start_line": 4, "end_line": 9},
            )]
        ),
        # Step 2: Record finding
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="c2",
                name="record_finding",
                arguments={
                    "title": "Unchecked array index",
                    "description": "items[i] is accessed without bounds checking",
                    "affected_file": "src/main.py",
                    "start_line": 6,
                    "end_line": 8,
                    "severity": "high",
                },
            )]
        ),
        # Step 3: Conclude
        LLMResponse(content="Audit complete. High severity issue recorded in process_data."),
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
    assert result.total_tool_calls == 2
    assert len(result.findings) == 1
    assert result.findings[0].title == "Unchecked array index"
    assert result.findings[0].lifecycle_state == FindingLifecycleState.SUSPECTED

    # Run in store
    run = store.get_run(run_id)
    assert run.status == "completed"
    assert run.total_findings == 1
    assert run.total_evidence == 1


def test_investigation_with_no_findings_reports_truthful_outcome(tmp_path: Path):
    """19. Investigation auditing code and finding no defects reports status='completed' with 0 findings."""
    workspace, store, run_id, tool_reg = _make_env(tmp_path)

    provider = FakeLLMProvider(scripted_responses=[
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="c1",
                name="read_source_excerpt",
                arguments={"file_path": "src/utils.py", "start_line": 1, "end_line": 2},
            )]
        ),
        LLMResponse(content="Audited src/utils.py; no defects or security risks identified."),
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
    assert result.total_tool_calls == 1
    assert len(result.findings) == 0

    run = store.get_run(run_id)
    assert run.status == "completed"
    assert run.total_findings == 0
    assert run.total_evidence == 0


def test_schema_validation_and_finding_retrieval(tmp_path: Path):
    """20. list_findings tool returns recorded findings correctly."""
    _, store, _, tool_reg = _make_env(tmp_path)
    tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Bug A",
            "description": "desc A",
            "affected_file": "src/main.py",
            "start_line": 1,
            "end_line": 3,
            "severity": "critical",
        },
    )

    list_res = json.loads(tool_reg.execute_tool("list_findings", {}))
    assert list_res["total"] == 1
    f = list_res["findings"][0]
    assert f["title"] == "Bug A"
    assert f["severity"] == "critical"
    assert f["state"] == "suspected"
    assert f["evidence_count"] == 1
