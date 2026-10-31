"""Comprehensive Phase 7 Regression and Verification Suite: Evidence-Driven Investigation Engine.

Verifies:
1. Unsupported Pydantic vulnerability claim rejection (reproducing FastAPI failure case).
2. Contradictory dependency evidence rejection.
3. Implementation contradicting an alleged defect rejection.
4. Test-name-based false positive rejection.
5. HIGH severity without demonstrated impact (speculative security rejection / calibration).
6. Evidence unrelated to finding description rejection.
7. Duplicate rejected claims prevented by per-run tracker.
8. Correctly supported findings remain accepted.
9. Insufficient evidence results in unverified outcome.
10. Final synthesis accurately reporting accepted and rejected claims.
"""

import ast
import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from forge.analysis.dependency_manifest import (
    DependencyManifest,
    find_related_workspace_files,
    parse_workspace_dependencies,
)
from forge.analysis.evidence import create_evidence
from forge.analysis.validation import (
    ConsistencyValidationError,
    ContradictionValidationError,
    DuplicateFindingError,
    InsufficientEvidenceError,
    RepeatedRejectedClaimError,
    SeverityJustificationError,
    SyntaxClaimValidationError,
    TestAssertionMisinterpretationError,
    ToolExecutionError,
    UnrelatedEvidenceError,
    ValidationOutcome,
    validate_finding_claim,
)
from forge.investigation.loop import InvestigationLoop, InvestigationResult
from forge.investigation.tools import ToolRegistry
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import (
    ChatMessage,
    LLMResponse,
    ToolCallRequest,
)
from forge.reporting.builder import ReportBuilder
from forge.schemas import (
    EventType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


def _setup_fastapi_reproduction_env(tmp_path: Path):
    """Set up test workspace replicating the exact FastAPI reproduction structure.

    Commit: 45cb2252c737906f37544f5ba6bee0295fb81468
    Run: run_8120cd03f318
    Target Finding: Upload File Invalid Pydantic V2
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # 1. pyproject.toml specifying Pydantic v2
    pyproject = workspace / "pyproject.toml"
    pyproject.write_text(
        '[project]\n'
        'name = "fastapi"\n'
        'version = "0.115.0"\n'
        'dependencies = [\n'
        '    "starlette>=0.40.0,<0.42.0",\n'
        '    "pydantic>=2.9.0,<3.0.0",\n'
        '    "typing-extensions>=4.8.0",\n'
        ']\n',
        encoding="utf-8",
    )

    # 2. fastapi/datastructures.py implementation
    fastapi_dir = workspace / "fastapi"
    fastapi_dir.mkdir(parents=True, exist_ok=True)
    datastructures = fastapi_dir / "datastructures.py"
    datastructures.write_text(
        '"""FastAPI datastructures implementation."""\n'
        'from starlette.datastructures import UploadFile as StarletteUploadFile\n'
        '\n'
        'class DefaultPlaceholder:\n'
        '    def __init__(self, value):\n'
        '        self.value = value\n'
        '\n'
        'class UploadFile(StarletteUploadFile):\n'
        '    @classmethod\n'
        '    def _validate(cls, value):\n'
        '        if not isinstance(value, StarletteUploadFile):\n'
        '            raise ValueError(f"Expected UploadFile, received: {type(value)}")\n'
        '        return value\n',
        encoding="utf-8",
    )

    # 3. tests/test_datastructures.py test assertion
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_datastructures.py"
    test_file.write_text(
        '"""Unit tests for datastructures."""\n'
        'import pytest\n'
        'from fastapi.datastructures import UploadFile\n'
        '\n'
        'def test_upload_file_invalid_pydantic_v2():\n'
        '    with pytest.raises(ValueError):\n'
        '        UploadFile._validate("invalid_string_not_upload_file")\n'
        '\n'
        'def test_upload_file_valid():\n'
        '    assert UploadFile is not None\n',
        encoding="utf-8",
    )

    db_path = tmp_path / "test.db"
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target=str(workspace), run_id="run_fastapi_repro")
    tool_reg = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    return workspace, store, run_id, tool_reg


# =========================================================================
# Regression Test 1: Unsupported Pydantic vulnerability claim (FastAPI Case)
# =========================================================================

def test_unsupported_pydantic_vulnerability_claim(tmp_path: Path):
    """1. Reproduces the FastAPI failure case: candidate finding claiming Pydantic v2 vulnerability is rejected."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    args = {
        "title": "Upload File Invalid Pydantic V2",
        "description": "Upload file uses outdated Pydantic version and may introduce security vulnerabilities",
        "affected_file": "tests/test_datastructures.py",
        "start_line": 5,
        "end_line": 7,
        "defect_category": "security",
        "severity": "high",
        "confidence": "medium",
    }

    # Execute tool through tool registry
    result_str = tool_reg.execute_tool("record_finding", args)
    result = json.loads(result_str)

    # Must be rejected, not recorded
    assert result["status"] == "rejected"
    assert result["error_type"] in (
        "ContradictionValidationError",
        "TestAssertionMisinterpretationError",
        "SeverityJustificationError",
    )
    assert "contradiction" in result["error"].lower() or "test assertion" in result["error"].lower() or "speculative" in result["error"].lower()

    # Zero findings persisted
    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 0

    # Verification event emitted
    events = store.list_run_events(run_id=run_id)
    validation_events = [e for e in events if e.event_type == EventType.FINDING_VALIDATION_FAILED]
    assert len(validation_events) >= 1


# =========================================================================
# Regression Test 2: Contradictory dependency evidence
# =========================================================================

def test_contradictory_dependency_evidence(tmp_path: Path):
    """2. Verify that claims alleging missing or unsupported dependencies are rejected when manifests contradict them."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # Tool inspect_dependencies returns manifest
    dep_str = tool_reg.execute_tool("inspect_dependencies", {})
    dep_data = json.loads(dep_str)
    assert "pydantic" in dep_data["dependencies"]
    assert "starlette" in dep_data["dependencies"]

    # Claim alleging missing starlette
    args = {
        "title": "Missing starlette dependency in environment",
        "description": "Package import fails because there is no starlette declared in the project",
        "affected_file": "fastapi/datastructures.py",
        "start_line": 2,
        "end_line": 2,
        "defect_category": "missing_validation",
        "severity": "medium",
        "confidence": "high",
    }

    result = json.loads(tool_reg.execute_tool("record_finding", args))
    assert result["status"] == "rejected"
    assert result["error_type"] == "ContradictionValidationError"
    assert "declared in project dependencies" in result["error"]
    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test 3: Implementation contradicting an alleged defect
# =========================================================================

def test_implementation_contradicting_alleged_defect(tmp_path: Path):
    """3. Verify that alleging missing input validation is rejected when related implementation explicitly validates."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # Cross-file tool discovers related implementation
    rel_str = tool_reg.execute_tool("find_related_files", {"file_path": "tests/test_datastructures.py"})
    rel_data = json.loads(rel_str)
    assert "fastapi/datastructures.py" in rel_data["implementations"]

    args = {
        "title": "Missing validation for upload file",
        "description": "UploadFile accepts invalid input and does not validate input data",
        "affected_file": "tests/test_datastructures.py",
        "start_line": 5,
        "end_line": 7,
        "defect_category": "missing_validation",
        "severity": "medium",
        "confidence": "medium",
    }

    result = json.loads(tool_reg.execute_tool("record_finding", args))
    assert result["status"] == "rejected"
    assert result["error_type"] in ("ContradictionValidationError", "TestAssertionMisinterpretationError")
    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test 4: Test-name-based false positive
# =========================================================================

def test_test_name_based_false_positive(tmp_path: Path):
    """4. Verify that deriving a defect claim solely from a test function name is rejected."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # Write a test with a distinct name unrelated to pydantic
    parser_test = workspace / "tests" / "test_parser.py"
    parser_test.write_text(
        "def test_malformed_xml_payload_handling():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    args = {
        "title": "Malformed XML Payload Handling",
        "description": "Function test_malformed_xml_payload_handling indicates malformed xml payload handling defect",
        "affected_file": "tests/test_parser.py",
        "start_line": 1,
        "end_line": 1,
        "defect_category": "logic_bug",
        "severity": "medium",
        "confidence": "medium",
    }

    result = json.loads(tool_reg.execute_tool("record_finding", args))
    assert result["status"] == "rejected"
    assert result["error_type"] == "TestAssertionMisinterpretationError"
    assert "test name false positive" in result["error"].lower()
    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test 5: HIGH severity without demonstrated impact
# =========================================================================

def test_high_severity_without_demonstrated_impact(tmp_path: Path):
    """5. Verify that candidate findings claiming HIGH/CRITICAL severity without concrete failure mode are rejected or calibrated."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # 5A: Speculative security claim with HIGH severity is rejected
    speculative_args = {
        "title": "Potential Security Vulnerability in Datastructures",
        "description": "This implementation may introduce security vulnerabilities in downstream applications",
        "affected_file": "fastapi/datastructures.py",
        "start_line": 7,
        "end_line": 10,
        "defect_category": "security",
        "severity": "high",
        "confidence": "medium",
    }
    res_a = json.loads(tool_reg.execute_tool("record_finding", speculative_args))
    assert res_a["status"] == "rejected"
    assert res_a["error_type"] == "SeverityJustificationError"
    assert "speculative" in res_a["error"].lower()

    # 5B: Test file claiming CRITICAL severity without production exploitability is rejected
    test_crit_args = {
        "title": "Critical Defect in Test Suite",
        "description": "Test failure causes major issue",
        "affected_file": "tests/test_datastructures.py",
        "start_line": 9,
        "end_line": 10,
        "defect_category": "logic_bug",
        "severity": "critical",
        "confidence": "high",
    }
    res_b = json.loads(tool_reg.execute_tool("record_finding", test_crit_args))
    assert res_b["status"] == "rejected"
    assert res_b["error_type"] == "SeverityJustificationError"
    assert "test files cannot support high or critical" in res_b["error"].lower()

    # 5C: Non-speculative security claim without concrete severe impact is downgraded to MEDIUM with limitation recorded
    prod_sec_args = {
        "title": "Security token comparison check",
        "description": "Token comparison in helper function",
        "affected_file": "fastapi/datastructures.py",
        "start_line": 4,
        "end_line": 6,
        "defect_category": "security",
        "severity": "high",
        "confidence": "medium",
    }
    res_c = json.loads(tool_reg.execute_tool("record_finding", prod_sec_args))
    assert res_c["status"] == "success"
    assert res_c["severity"] == "medium"
    assert "Severity adjusted" in res_c["limitations"]


# =========================================================================
# Regression Test 6: Evidence unrelated to finding description
# =========================================================================

def test_evidence_unrelated_to_finding_description(tmp_path: Path):
    """6. Verify that citing evidence unrelated to the alleged defect type (e.g. SQL injection on class definition) is rejected."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # Citing `class DefaultPlaceholder:` while alleging SQL injection
    args = {
        "title": "SQL Injection in DefaultPlaceholder",
        "description": "SQL injection flaw allows attackers to manipulate SQL database query execution",
        "affected_file": "fastapi/datastructures.py",
        "start_line": 4,
        "end_line": 6,
        "defect_category": "security",
        "severity": "high",
        "confidence": "high",
    }

    result = json.loads(tool_reg.execute_tool("record_finding", args))
    assert result["status"] == "rejected"
    assert result["error_type"] == "UnrelatedEvidenceError"
    assert "unrelated to the finding description" in result["error"].lower()
    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test 7: Duplicate rejected claims
# =========================================================================

def test_duplicate_rejected_claims(tmp_path: Path):
    """7. Verify that re-submitting a previously rejected claim with altered wording is rejected as a repeated submission."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # First attempt: Rejected due to test assertion misinterpretation
    first_args = {
        "title": "Upload File Invalid Pydantic V2",
        "description": "Upload file uses outdated Pydantic version and may introduce security vulnerabilities",
        "affected_file": "tests/test_datastructures.py",
        "start_line": 5,
        "end_line": 7,
        "defect_category": "security",
        "severity": "high",
        "confidence": "medium",
    }
    res1 = json.loads(tool_reg.execute_tool("record_finding", first_args))
    assert res1["status"] == "rejected"

    # Second attempt: Same file range, slightly rephrased title and description
    retry_args = {
        "title": "Pydantic V2 Invalid Upload File Handling Defect",
        "description": "Upload file uses outdated Pydantic version and introduces security risk in v2",
        "affected_file": "tests/test_datastructures.py",
        "start_line": 5,
        "end_line": 7,
        "defect_category": "security",
        "severity": "high",
        "confidence": "medium",
    }
    res2 = json.loads(tool_reg.execute_tool("record_finding", retry_args))
    assert res2["status"] == "rejected"
    assert res2["error_type"] == "RepeatedRejectedClaimError"
    assert "Repeated rejected finding submission" in res2["error"]


# =========================================================================
# Regression Test 8: Correctly supported findings remain accepted
# =========================================================================

def test_correctly_supported_findings_remain_accepted(tmp_path: Path):
    """8. Verify that genuine defects supported by verified evidence are accepted with preserved uncertainty."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # Create a production file with a genuine defect (broken syntax / missing colon)
    parser_file = workspace / "fastapi" / "broken_parser.py"
    parser_file.write_text(
        "def parse_header(header_value)\n"
        "    return header_value.strip()\n",
        encoding="utf-8",
    )

    args = {
        "title": "Syntax error in parse_header function header",
        "description": "Missing colon after parse_header(header_value) signature at line 1",
        "affected_file": "fastapi/broken_parser.py",
        "start_line": 1,
        "end_line": 1,
        "defect_category": "syntax_error",
        "severity": "high",
        "confidence": "high",
    }

    result = json.loads(tool_reg.execute_tool("record_finding", args))
    assert result["status"] == "success"
    assert result["lifecycle_state"] == "suspected"
    assert result["finding_id"].startswith("find_")

    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 1
    f = findings[0]
    assert f.title == "Syntax error in parse_header function header"
    assert "Semantic correctness could not be established deterministically" in (f.limitations or "")


# =========================================================================
# Regression Test 9: Insufficient evidence results in unverified outcome
# =========================================================================

def test_insufficient_evidence_results_in_unverified_outcome(tmp_path: Path):
    """9. Verify that insufficient evidence (whitespace only or non-existent files) results in zero fabricated findings."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    # Whitespace only line
    blank_file = workspace / "fastapi" / "empty.py"
    blank_file.write_text("\n\n   \n\n", encoding="utf-8")

    args = {
        "title": "Defect in empty file",
        "description": "Whitespace lines have a defect",
        "affected_file": "fastapi/empty.py",
        "start_line": 2,
        "end_line": 3,
        "defect_category": "logic_bug",
        "severity": "low",
        "confidence": "low",
    }

    result = json.loads(tool_reg.execute_tool("record_finding", args))
    assert result["status"] == "rejected"
    assert result["error_type"] == "InsufficientEvidenceError"

    # Store remains unpolluted
    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test 10: Final synthesis accurately reporting accepted and rejected claims
# =========================================================================

def test_final_synthesis_accurately_reporting_accepted_and_rejected_claims(tmp_path: Path):
    """10. Verify that the investigation loop and report builder accurately synthesize inspected files, accepted findings, and rejected claims."""
    workspace, store, run_id, tool_reg = _setup_fastapi_reproduction_env(tmp_path)

    provider = FakeLLMProvider(scripted_responses=[
        # Turn 1: Inspect dependencies
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="c1",
                name="inspect_dependencies",
                arguments={},
            )]
        ),
        # Turn 2: Attempt flawed finding on test file
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="c2",
                name="record_finding",
                arguments={
                    "title": "Upload File Invalid Pydantic V2",
                    "description": "Upload file uses outdated Pydantic version and may introduce security vulnerabilities",
                    "affected_file": "tests/test_datastructures.py",
                    "start_line": 5,
                    "end_line": 7,
                    "defect_category": "security",
                    "severity": "high",
                },
            )]
        ),
        # Turn 3: Find related files
        LLMResponse(
            tool_calls=[ToolCallRequest(
                call_id="c3",
                name="find_related_files",
                arguments={"file_path": "tests/test_datastructures.py"},
            )]
        ),
        # Turn 4: Final synthesis
        LLMResponse(
            content="Investigation complete. No defects found. The suspected Pydantic vulnerability was contradicted by pyproject.toml."
        ),
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

    # Loop asserts
    assert result.status == "completed"
    assert result.steps_executed == 4
    assert result.total_tool_calls == 3
    assert len(result.findings) == 0
    assert len(result.rejected_claims) == 1
    assert result.rejected_claims[0]["file"] == "tests/test_datastructures.py"
    assert "tests/test_datastructures.py" in result.inspected_files

    # Report builder synthesis
    from forge.reporting.builder import report_to_markdown
    builder = ReportBuilder(store=store)
    report = builder.build_report(run_id=run_id)
    report_md = report_to_markdown(report)
    assert "Rejected / Disproven Claims" in report_md
    assert "**Total Findings**: 0" in report_md
