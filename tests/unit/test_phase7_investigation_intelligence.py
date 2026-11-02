"""Deterministic unit and regression tests for Phase 7: Investigation Intelligence & Synthesis Remediation.

Covers the 17 required regression test specifications:
1. Invalid evidence rejection followed by useful recovery.
2. Exact duplicate rejected claim.
3. Semantically equivalent rejected claim with a different title.
4. Corrected claim with genuinely new evidence.
5. Rejection budget exhaustion.
6. Agent switches from finding submission to source inspection.
7. Dependency manifest inspection.
8. Implementation and test relationship inspection.
9. Generic apology rejected as final synthesis.
10. Structured synthesis retry.
11. Deterministic fallback synthesis.
12. Fallback does not fabricate inspected files or findings.
13. Insufficient evidence quality classification.
14. Meaningful zero-finding investigation classification.
15. Completed execution with failed quality gate.
16. JSON and Markdown quality-status reporting.
17. Existing historical report compatibility.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from forge.analysis.validation import (
    FindingValidationError,
    InsufficientEvidenceError,
    RejectionBudgetExceededError,
    RepeatedRejectedClaimError,
    RejectedClaimTracker,
    validate_finding_claim,
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
from forge.reporting.builder import ReportBuilder, report_to_markdown
from forge.reporting.models import InvestigationReport
from forge.schemas import (
    EventType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


def _setup_test_repo(tmp_path: Path):
    """Setup workspace with manifest, implementation, and test files."""
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
        '"""Core service module."""\n'
        "\n"
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
        '"""Test service module."""\n'
        "\n"
        "from demolib.service import Service\n"
        "\n"
        "\n"  # line 5, 6 empty
        "def test_run():\n"
        "    s = Service()\n"
        "    assert s.run(2) == 4\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "test_intelligence.db"
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target=str(workspace), run_id="run_intel_001")
    tool_reg = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id, rejection_budget=3)

    return workspace, store, run_id, tool_reg


# =========================================================================
# 1. Invalid evidence rejection followed by useful recovery
# =========================================================================
def test_invalid_evidence_rejection_followed_by_useful_recovery(tmp_path: Path):
    """1. Invalid evidence rejection returns directive; recovery succeeds."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    # First attempt: invalid empty line evidence
    raw_res1 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Loop defect in tests",
            "description": "Found loop issue",
            "affected_file": "tests/test_service.py",
            "start_line": 4,
            "end_line": 5,
            "defect_category": "loop_defect",
        },
    )
    res1 = json.loads(raw_res1)
    assert res1.get("status") == "rejected"
    assert res1.get("rejection_type") == "InsufficientEvidenceError"
    assert "directive" in res1

    # Useful recovery: model calls read_source_excerpt on valid file
    raw_res2 = tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "demolib/service.py", "start_line": 1, "end_line": 6},
    )
    assert "class Service" in raw_res2


# =========================================================================
# 2. Exact duplicate rejected claim
# =========================================================================
def test_exact_duplicate_rejected_claim(tmp_path: Path):
    """2. Exact duplicate rejected claim is caught and rejected."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    args = {
        "title": "Whitespace syntax error",
        "description": "Empty whitespace error",
        "affected_file": "tests/test_service.py",
        "start_line": 5,
        "end_line": 5,
        "defect_category": "syntax_error",
    }
    raw_res1 = tool_reg.execute_tool("record_finding", args)
    res1 = json.loads(raw_res1)
    assert res1.get("status") == "rejected"
    assert res1.get("rejection_type") == "InsufficientEvidenceError"

    # Second submission: exact same claim
    raw_res2 = tool_reg.execute_tool("record_finding", args)
    res2 = json.loads(raw_res2)
    assert res2.get("status") == "rejected"
    assert res2.get("rejection_type") == "RepeatedRejectedClaimError"


# =========================================================================
# 3. Semantically equivalent rejected claim with different title
# =========================================================================
def test_semantically_equivalent_rejected_claim_with_different_title(tmp_path: Path):
    """3. Semantically equivalent rejected claim with rephrased title is caught."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    raw_res1 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Loop error in test",
            "description": "Loop problem",
            "affected_file": "tests/test_service.py",
            "start_line": 5,
            "end_line": 5,
            "defect_category": "loop_defect",
        },
    )
    res1 = json.loads(raw_res1)
    assert res1.get("status") == "rejected"

    # Semantically equivalent with different title on same empty lines
    raw_res2 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Unbounded iteration loop in test_service",
            "description": "Loop problem rephrased",
            "affected_file": "tests/test_service.py",
            "start_line": 5,
            "end_line": 5,
            "defect_category": "loop_defect",
        },
    )
    res2 = json.loads(raw_res2)
    assert res2.get("status") == "rejected"
    assert res2.get("rejection_type") == "RepeatedRejectedClaimError"


# =========================================================================
# 4. Corrected claim with genuinely new evidence
# =========================================================================
def test_corrected_claim_with_genuinely_new_evidence(tmp_path: Path):
    """4. Corrected claim with genuinely non-empty code lines is evaluated freshly."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    # First attempt: rejected on empty lines 4-5
    raw_res1 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Service logic issue",
            "description": "Issue description",
            "affected_file": "tests/test_service.py",
            "start_line": 4,
            "end_line": 5,
            "defect_category": "other",
        },
    )
    res1 = json.loads(raw_res1)
    assert res1.get("status") == "rejected"
    assert res1.get("rejection_type") == "InsufficientEvidenceError"

    # Second attempt: corrected with actual implementation lines (lines 6-8)
    raw_res2 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Service test assertion verification",
            "description": "Verifying test logic in test_service",
            "affected_file": "tests/test_service.py",
            "start_line": 6,
            "end_line": 8,
            "defect_category": "other",
            "severity": "low",
        },
    )
    res2 = json.loads(raw_res2)
    assert res2.get("rejection_type") != "RepeatedRejectedClaimError"
    assert res2.get("status") == "success"


# =========================================================================
# 5. Rejection budget exhaustion
# =========================================================================
def test_rejection_budget_exhaustion(tmp_path: Path):
    """5. Reaching rejection budget marks budget exhausted and blocks findings."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)
    tool_reg.rejected_tracker.rejection_budget = 3

    # Generate 3 distinct rejections (syntax errors on valid python lines)
    for i in range(1, 4):
        raw_res = tool_reg.execute_tool(
            "record_finding",
            {
                "title": f"Syntax error claim {i}",
                "description": f"Deterministic syntax error claim {i}",
                "affected_file": "demolib/service.py",
                "start_line": i * 2,
                "end_line": i * 2,
                "defect_category": "syntax_error",
            },
        )
        res = json.loads(raw_res)
        assert res.get("status") == "rejected"

    assert tool_reg.rejected_tracker.is_budget_exhausted()

    # 4th attempt should be rejected with RejectionBudgetExceededError
    raw_res4 = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Another claim",
            "description": "Should fail budget",
            "affected_file": "demolib/service.py",
            "start_line": 1,
            "end_line": 5,
            "defect_category": "other",
        },
    )
    res4 = json.loads(raw_res4)
    assert res4.get("status") == "rejected"
    assert res4.get("rejection_type") == "RejectionBudgetExceededError"


# =========================================================================
# 6. Agent switches from finding submission to source inspection
# =========================================================================
def test_agent_switches_from_finding_submission_to_source_inspection(tmp_path: Path):
    """6. When rejection budget is exhausted, loop dynamically removes record_finding."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    class SwitchingProvider:
        def __init__(self):
            self.turns = 0

        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            self.turns += 1
            step = self.turns
            if step <= 3:
                # 3 rejected finding submissions
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id=f"c{step}",
                        name="record_finding",
                        arguments={
                            "title": f"Syntax error {step}",
                            "description": f"Syntax error {step}",
                            "affected_file": "demolib/service.py",
                            "start_line": step * 2,
                            "end_line": step * 2,
                            "defect_category": "syntax_error",
                        },
                    )]
                )
            elif step == 4:
                # Turn 4: check tools offered to model
                tool_names = [t.name for t in (req.tools or [])]
                assert "record_finding" not in tool_names
                # Model calls source inspection instead
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id=f"c{step}",
                        name="read_source_excerpt",
                        arguments={"file_path": "demolib/service.py", "start_line": 1, "end_line": 5},
                    )]
                )
            return LLMResponse(content="Repository inspected thoroughly. Clean implementation without verified defects.")

    loop = InvestigationLoop(
        provider=SwitchingProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=5,
        rejection_budget=3,
    )
    result = loop.run()
    assert result.status == "completed"
    assert "demolib/service.py" in loop.inspected_files


# =========================================================================
# 7. Dependency manifest inspection
# =========================================================================
def test_dependency_manifest_inspection(tmp_path: Path):
    """7. Dependency manifest inspection tracks manifest files in files_inspected."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    class ManifestProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            step = len(req.messages)
            if step <= 2:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="call_dep",
                        name="inspect_dependencies",
                        arguments={"ecosystem": "python"},
                    )]
                )
            return LLMResponse(content="Dependencies verified against pyproject.toml specifications.")

    loop = InvestigationLoop(
        provider=ManifestProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )
    result = loop.run()
    assert result.status == "completed"
    assert "pyproject.toml" in loop.inspected_files


# =========================================================================
# 8. Implementation and test relationship inspection
# =========================================================================
def test_implementation_and_test_relationship_inspection(tmp_path: Path):
    """8. Both implementation and test files inspected are tracked."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)
    step_num = 0

    class RelationalProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            nonlocal step_num
            step_num += 1
            if step_num == 1:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c1",
                        name="read_source_excerpt",
                        arguments={"file_path": "demolib/service.py", "start_line": 1, "end_line": 5},
                    )]
                )
            elif step_num == 2:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c2",
                        name="read_source_excerpt",
                        arguments={"file_path": "tests/test_service.py", "start_line": 1, "end_line": 5},
                    )]
                )
            return LLMResponse(content="Both service implementation and tests verified for consistency.")

    loop = InvestigationLoop(
        provider=RelationalProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )
    result = loop.run()
    assert result.status == "completed"
    assert "demolib/service.py" in loop.inspected_files
    assert "tests/test_service.py" in loop.inspected_files


# =========================================================================
# 9. Generic apology rejected as final synthesis
# =========================================================================
def test_generic_apology_rejected_as_final_synthesis():
    """9. Conversational apologies and non-technical responses are rejected."""
    apologies = [
        "I apologize for the confusion. Please provide me with the necessary information to assist you further.",
        "I'm sorry, as an AI language model I cannot help.",
        "How can I assist you today?",
        "Please let me know if you need anything else.",
        "Sorry for the misunderstanding.",
    ]
    for apology in apologies:
        assert not validate_synthesis_quality(apology), f"Failed to reject: {apology}"

    # Raw JSON tool calls or code dumps are rejected as synthesis
    raw_tool_calls = [
        '{"name": "record_finding", "arguments": {"title": "Loop detected", "affected_file": "tests/test.py"}}',
        '```json\n{"name": "read_source_excerpt", "arguments": {"file_path": "main.py"}}\n```',
        '<tool_call>{"name": "search_symbols"}</tool_call>',
    ]
    for rtc in raw_tool_calls:
        assert not validate_synthesis_quality(rtc), f"Failed to reject raw tool call: {rtc}"

    # Valid technical synthesis should pass
    assert validate_synthesis_quality("Clean architecture with no verified security vulnerabilities detected.")
    assert validate_synthesis_quality("Inspected service and test files; dependencies in pyproject.toml confirmed valid.")


# =========================================================================
# 10. Structured synthesis retry
# =========================================================================
def test_structured_synthesis_retry(tmp_path: Path):
    """10. Loop detects apology, triggers retry with structured instructions, and accepts technical answer."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)
    calls = 0

    class RetryProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Step 1: execute a valid tool so run has activity
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c1",
                        name="read_source_excerpt",
                        arguments={"file_path": "demolib/service.py", "start_line": 1, "end_line": 5},
                    )]
                )
            elif calls == 2:
                # Step 2: final turn returns apology
                return LLMResponse(content="I apologize for the confusion. How can I assist you?")
            # Call 3: retry turn returns structured technical answer
            return LLMResponse(content="Codebase inspection completed. No vulnerabilities identified in service.")

    loop = InvestigationLoop(
        provider=RetryProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=2,
    )
    result = loop.run()
    assert calls == 3  # Step 1 tool + Step 2 apology + Synthesis retry
    assert result.status == "completed"
    assert not result.used_fallback_synthesis
    assert "No vulnerabilities identified" in (result.final_response or "")


# =========================================================================
# 11. Deterministic fallback synthesis
# =========================================================================
def test_deterministic_fallback_synthesis(tmp_path: Path):
    """11. If retry fails to provide technical synthesis, system fallback is used."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)
    calls = 0

    class StubbornApologyProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Step 1: execute tool so run has activity
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c1",
                        name="read_source_excerpt",
                        arguments={"file_path": "demolib/service.py", "start_line": 1, "end_line": 5},
                    )]
                )
            # Step 2 and subsequent retry: return apology
            return LLMResponse(content="I apologize for the confusion. Please provide more info.")

    loop = InvestigationLoop(
        provider=StubbornApologyProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=2,
    )
    result = loop.run()
    assert result.status == "completed"
    assert result.used_fallback_synthesis
    assert "[System-Generated Fallback Synthesis]" in (result.final_response or "")


# =========================================================================
# 12. Fallback does not fabricate inspected files or findings
# =========================================================================
def test_fallback_does_not_fabricate_inspected_files_or_findings():
    """12. Fallback synthesis accurately reflects actual inspected files and recorded findings."""
    inspected = ["demolib/service.py", "pyproject.toml"]
    rejected = [{"title": "Loop bug", "file": "demolib/service.py", "reason": "empty lines"}]

    fallback = generate_fallback_synthesis(
        target="demolib",
        commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        inspected_files=inspected,
        findings=[],
        rejected_records=rejected,
        steps_executed=5,
        max_steps=10,
    )
    assert "[System-Generated Fallback Synthesis]" in fallback
    assert "demolib/service.py" in fallback
    assert "pyproject.toml" in fallback
    assert "Zero defects were verified" in fallback or "0 findings" in fallback
    # Should not fabricate non-existent files or findings
    assert "fabricated_file.py" not in fallback
    assert "Critical Security Flaw" not in fallback


# =========================================================================
# 13. Insufficient evidence quality classification
# =========================================================================
def test_insufficient_evidence_quality_classification():
    """13. Zero findings with rejected claims yields insufficient_evidence quality."""
    quality = determine_investigation_quality(
        loop_status="completed",
        total_tool_calls=4,
        inspected_files=["demolib/service.py"],
        findings=[],
        rejected_records=[{"title": "Claim 1"}],
        used_fallback_synthesis=True,
        has_valid_synthesis=False,
    )
    assert quality == "insufficient_evidence"


# =========================================================================
# 14. Meaningful zero-finding investigation classification
# =========================================================================
def test_meaningful_zero_finding_investigation_classification():
    """14. Broad inspection with 0 rejected claims and valid synthesis yields completed."""
    quality = determine_investigation_quality(
        loop_status="completed",
        total_tool_calls=5,
        inspected_files=["demolib/service.py", "tests/test_service.py", "pyproject.toml"],
        findings=[],
        rejected_records=[],
        used_fallback_synthesis=False,
        has_valid_synthesis=True,
    )
    assert quality == "completed"


# =========================================================================
# 15. Completed execution with failed quality gate
# =========================================================================
def test_completed_execution_with_failed_quality_gate(tmp_path: Path):
    """15. Execution finishes cleanly as completed, but investigation quality is insufficient_evidence."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    class OnlyRejectedClaimsProvider:
        def is_available(self) -> bool:
            return True

        def generate(self, req: LLMRequest) -> LLMResponse:
            step = len(req.messages)
            if step <= 2:
                return LLMResponse(
                    tool_calls=[ToolCallRequest(
                        call_id="c1",
                        name="record_finding",
                        arguments={
                            "title": "Bad finding",
                            "description": "empty",
                            "affected_file": "tests/test_service.py",
                            "start_line": 5,
                            "end_line": 5,
                            "defect_category": "syntax_error",
                        },
                    )]
                )
            return LLMResponse(content="Investigation concluded without verified evidence.")

    loop = InvestigationLoop(
        provider=OnlyRejectedClaimsProvider(),
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=2,
    )
    result = loop.run()
    assert result.status == "completed"
    assert result.investigation_quality == "insufficient_evidence"


# =========================================================================
# 16. JSON and Markdown quality-status reporting
# =========================================================================
def test_json_and_markdown_quality_status_reporting(tmp_path: Path):
    """16. Reports accurately expose investigation quality, inspected files, and rejected claims."""
    workspace, store, run_id, tool_reg = _setup_test_repo(tmp_path)

    # Attach summary dict with quality status
    store.finish_run(
        run_id=run_id,
        status="completed",
        summary={
            "investigation_quality": "insufficient_evidence",
            "inspected_files": ["demolib/service.py", "tests/test_service.py"],
            "rejected_claims": [{
                "title": "Whitespace syntax error",
                "file": "tests/test_service.py",
                "lines": "5-5",
                "reason": "empty line",
                "rejection_type": "InsufficientEvidenceError",
            }],
            "final_synthesis": "Clean codebase; prior claims were unsupported.",
            "limitations": "Model attempted invalid evidence on whitespace.",
        },
    )

    builder = ReportBuilder(store=store)
    report = builder.build_report(run_id)

    assert report.investigation_quality == "insufficient_evidence"
    assert "demolib/service.py" in report.files_inspected
    assert len(report.rejected_claims) == 1
    assert report.limitations is not None

    md = report_to_markdown(report)
    assert "**Investigation Quality**: `INSUFFICIENT_EVIDENCE`" in md
    assert "demolib/service.py" in md
    assert "Rejected & Disproven Claims" in md
    assert "Whitespace syntax error" in md


# =========================================================================
# 17. Existing historical report compatibility
# =========================================================================
def test_existing_historical_report_compatibility(tmp_path: Path):
    """17. Historical runs without new Phase 7 fields build reports without error."""
    db_path = tmp_path / "historical.db"
    store = ForgeStore(db_path=db_path)
    old_run_id = store.create_run(target="https://github.com/old/repo.git", run_id="run_hist_001")

    # Complete run with legacy summary structure
    store.finish_run(
        run_id=old_run_id,
        status="completed",
        summary={"total_findings": 0, "final_synthesis": "Legacy run finished."},
    )

    builder = ReportBuilder(store=store)
    report = builder.build_report(old_run_id)

    # Defaults are used safely
    assert report.investigation_quality == "completed"
    assert report.files_inspected == []
    assert report.rejected_claims == []

    # Markdown export does not fail
    md = report_to_markdown(report)
    assert "**Execution Status**: `COMPLETED`" in md
    assert "**Investigation Quality**: `COMPLETED`" in md
