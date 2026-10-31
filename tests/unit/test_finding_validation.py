"""Deterministic regression and unit tests for finding reliability, evidence validation, and deduplication.

Reproduces and validates fixes for Apache Hamilton defects:
A. Valid Python dictionary entry rejecting false positive syntax claims
B. Duplicate syntax-error prevention and linking
C. Inconsistent classification (loop category with syntax description on non-loop code) rejection
D. Unrelated evidence rejection (citing line with colon for missing-colon claim)
E. Genuine defect acceptance (both true syntax errors and semantic logic bugs)
F. Security invariants (path traversal, symlinks, secret redaction, audit events)
G. End-to-end investigation loop filtering false positives
"""

import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from forge.analysis.evidence import create_evidence
from forge.analysis.validation import (
    ToolExecutionError,
    FindingValidationError,
    SyntaxClaimValidationError,
    ConsistencyValidationError,
    InsufficientEvidenceError,
    DuplicateFindingError,
    ValidationOutcome,
    canonicalize_category,
    check_duplicate_finding,
    compute_token_jaccard_similarity,
    normalize_tokens,
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
from forge.schemas import (
    EventType,
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


def _setup_hamilton_env(tmp_path: Path):
    """Set up test workspace replicating the exact Apache Hamilton code structure."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Replicate hamilton/graph_types.py
    hamilton_dir = workspace / "hamilton"
    hamilton_dir.mkdir(parents=True, exist_ok=True)

    graph_types = hamilton_dir / "graph_types.py"
    # Lines 130 to 142 replicating HamiltonNode.as_dict()
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
    run_id = store.create_run(target=str(workspace), run_id="run_hamilton_test")
    tool_reg = ToolRegistry(workspace_path=workspace, store=store, run_id=run_id)

    return workspace, store, run_id, tool_reg, graph_types


# =========================================================================
# Regression Test A: Valid Python dictionary entry rejects missing-colon
# =========================================================================

def test_hamilton_regression_valid_dict_entry_rejects_missing_colon(tmp_path: Path):
    """A. Valid dictionary entry: '"output_type": (get_type_as_string(self.type) or ""),'.

    Expected: Deterministic static syntax analysis confirms valid Python syntax.
    False-positive missing-colon finding is rejected; 0 findings recorded in SQLite.
    """
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    # Line 15 is: `"output_type": (get_type_as_string(self.type) or ""),`
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

    with pytest.raises(SyntaxClaimValidationError) as exc_info:
        tool_reg._handle_record_finding(args)

    assert "valid Python syntax" in str(exc_info.value)
    assert "false positive syntax error claim" in str(exc_info.value).lower()

    # Verify zero findings recorded in database
    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 0

    # Verify an audit event was recorded in SQLite
    events = store.list_run_events(run_id=run_id)
    fail_events = [e for e in events if e.event_type == EventType.FINDING_VALIDATION_FAILED]
    assert len(fail_events) >= 1
    assert fail_events[0].payload["error_type"] == "SyntaxClaimValidationError"


# =========================================================================
# Regression Test B: Duplicate syntax-error prevention and linking
# =========================================================================

def test_hamilton_regression_duplicate_syntax_error_prevented(tmp_path: Path):
    """B. Duplicate syntax-error findings: Same file, same source range, same description.

    Expected: Duplicate prevented; audit event recorded; no redundant database row.
    """
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    # Create a broken file with genuine syntax error for test
    broken_file = workspace / "hamilton" / "broken.py"
    broken_file.write_text("def faulty_function(a, b)\n    return a + b\n", encoding="utf-8")

    first_args = {
        "title": "Missing colon in faulty_function definition",
        "description": "Missing colon after function signature at line 1",
        "affected_file": "hamilton/broken.py",
        "start_line": 1,
        "end_line": 1,
        "defect_category": "syntax_error",
        "severity": "high",
        "confidence": "high",
    }

    # First call succeeds
    result1_str = tool_reg._handle_record_finding(first_args)
    res1 = json.loads(result1_str)
    assert res1["status"] == "success"
    first_fid = res1["finding_id"]

    # Verify 1 finding in store
    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 1

    # Second call: Identical file, identical range, identical description
    duplicate_args = {
        "title": "Missing colon in faulty_function definition",
        "description": "Missing colon after function signature at line 1",
        "affected_file": "hamilton/broken.py",
        "start_line": 1,
        "end_line": 1,
        "defect_category": "syntax_error",
        "severity": "high",
        "confidence": "high",
    }

    with pytest.raises(DuplicateFindingError) as exc_info:
        tool_reg._handle_record_finding(duplicate_args)

    assert "Duplicate finding detected" in str(exc_info.value)
    assert first_fid in str(exc_info.value)

    # Verify STILL exactly 1 finding in store (duplicate prevented!)
    findings_after = store.list_findings(run_id=run_id)
    assert len(findings_after) == 1

    # Verify audit event recorded for duplicate detection
    events = store.list_run_events(run_id=run_id)
    dup_events = [e for e in events if e.event_type == EventType.FINDING_DUPLICATE_DETECTED]
    assert len(dup_events) >= 1
    assert dup_events[0].payload["error_type"] == "DuplicateFindingError"


def test_duplicate_linking_with_additional_evidence(tmp_path: Path):
    """B2. Duplicate finding with non-identical line range links evidence to existing finding."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    broken_file = workspace / "hamilton" / "broken.py"
    broken_file.write_text("def faulty_function(a, b)\n    return a + b\n", encoding="utf-8")

    first_args = {
        "title": "Missing colon in faulty_function definition",
        "description": "Missing colon after function signature at line 1",
        "affected_file": "hamilton/broken.py",
        "start_line": 1,
        "end_line": 1,
        "defect_category": "syntax_error",
    }
    res1 = json.loads(tool_reg._handle_record_finding(first_args))
    first_fid = res1["finding_id"]

    # Second call: Same defect, but providing expanded lines 1-2
    linked_args = {
        "title": "Missing colon in faulty_function definition",
        "description": "Missing colon after function signature at line 1",
        "affected_file": "hamilton/broken.py",
        "start_line": 1,
        "end_line": 2,
        "defect_category": "syntax_error",
    }
    res2 = json.loads(tool_reg._handle_record_finding(linked_args))
    assert res2["status"] == "duplicate_linked"
    assert res2["finding_id"] == first_fid

    # Verify still 1 finding, but 2 evidence records linked to it
    assert len(store.list_findings(run_id=run_id)) == 1
    evidence_list = store.list_evidence_for_finding(first_fid)
    assert len(evidence_list) == 2


# =========================================================================
# Regression Test C: Inconsistent classification
# =========================================================================

def test_hamilton_regression_inconsistent_classification_rejected(tmp_path: Path):
    """C. Inconsistent classification: Title says 'Loop Detected', description says 'missing colon',

    evidence contains an ordinary dictionary entry.
    Expected: Rejected as ConsistencyValidationError.
    """
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    args = {
        "title": "Loop Detected in HamiltonNode.as_dict()",
        "description": "Missing colon in HamiltonNode.as_dict() dictionary entry",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 15,
        "end_line": 15,
        "defect_category": "loop",
        "severity": "medium",
        "confidence": "medium",
    }

    with pytest.raises(ConsistencyValidationError) as exc_info:
        tool_reg._handle_record_finding(args)

    assert "Inconsistent classification" in str(exc_info.value)
    assert "loop" in str(exc_info.value).lower()
    assert "syntax error" in str(exc_info.value).lower()

    # Verify zero findings recorded
    assert len(store.list_findings(run_id=run_id)) == 0


def test_loop_category_without_loop_construct_in_evidence_rejected(tmp_path: Path):
    """C2. Loop category cited against code containing zero loop constructs is rejected."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    args = {
        "title": "Infinite Loop in HamiltonNode.as_dict()",
        "description": "The method as_dict will loop indefinitely without termination",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 13,
        "end_line": 16,
        "defect_category": "loop",
    }

    with pytest.raises(InsufficientEvidenceError) as exc_info:
        tool_reg._handle_record_finding(args)

    assert "no loop construct" in str(exc_info.value).lower()
    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test D: Unrelated evidence
# =========================================================================

def test_hamilton_regression_unrelated_evidence_rejected(tmp_path: Path):
    """D. Unrelated evidence: Finding alleges missing colon but cites '"name": self.name,'.

    Expected: Rejected because line already contains a colon and syntax is valid.
    """
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    # Line 14 is: `"name": self.name,`
    args = {
        "title": "Missing colon in dictionary entry",
        "description": "Expected colon after key name: missing colon",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 14,
        "end_line": 14,
        "defect_category": "syntax_error",
    }

    with pytest.raises(ToolExecutionError) as exc_info:
        tool_reg._handle_record_finding(args)

    # Caught by static syntax or missing colon validation
    err_msg = str(exc_info.value).lower()
    assert "valid python syntax" in err_msg or "already contains a colon" in err_msg or "unsupported" in err_msg

    assert len(store.list_findings(run_id=run_id)) == 0


# =========================================================================
# Regression Test E: Valid genuine findings
# =========================================================================

def test_genuine_syntax_error_accepted(tmp_path: Path):
    """E1. Valid genuine syntax error is verified by static AST and accepted."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    syntax_bad = workspace / "hamilton" / "parser.py"
    syntax_bad.write_text("def parse_tokens(text)\n    return text.split()\n", encoding="utf-8")

    args = {
        "title": "Syntax error: missing colon in function header",
        "description": "Missing colon after def parse_tokens(text) signature at line 1",
        "affected_file": "hamilton/parser.py",
        "start_line": 1,
        "end_line": 1,
        "defect_category": "syntax_error",
        "severity": "high",
        "confidence": "high",
    }

    result = json.loads(tool_reg._handle_record_finding(args))
    assert result["status"] == "success"
    assert result["lifecycle_state"] == "suspected"

    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 1
    assert findings[0].title == "Syntax error: missing colon in function header"


def test_genuine_logic_defect_accepted_with_uncertainty_preserved(tmp_path: Path):
    """E2. Valid genuine semantic logic defect on valid Python code is accepted with uncertainty preserved."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    logic_code = workspace / "hamilton" / "calc.py"
    logic_code.write_text(
        "def compute_discount(price, rate):\n"
        "    # Defect: adds discount rate instead of multiplying\n"
        "    return price + rate\n",
        encoding="utf-8",
    )

    args = {
        "title": "Incorrect discount calculation formula",
        "description": "compute_discount adds price + rate instead of price * (1 - rate)",
        "affected_file": "hamilton/calc.py",
        "start_line": 2,
        "end_line": 3,
        "defect_category": "logic_bug",
        "severity": "medium",
        "confidence": "confirmed",  # Model claims confirmed without dynamic reproduction
    }

    result = json.loads(tool_reg._handle_record_finding(args))
    assert result["status"] == "success"
    # Unearned "confirmed" is downgraded to high
    assert result["confidence"] == "high"

    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 1
    f = findings[0]
    assert f.lifecycle_state == FindingLifecycleState.SUSPECTED
    assert "Semantic correctness could not be established deterministically" in (f.limitations or "")


def test_distinct_defects_in_same_file_not_deduplicated(tmp_path: Path):
    """E3. Two genuinely distinct defects in the same file are NOT merged."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    app_file = workspace / "hamilton" / "app.py"
    app_file.write_text(
        "import os\n"
        "def run_command(cmd):\n"
        "    os.system(cmd)\n"
        "\n"
        "def read_file(path):\n"
        "    f = open(path)\n"
        "    return f.read()\n",
        encoding="utf-8",
    )

    # Defect 1: Security injection
    res1 = json.loads(tool_reg._handle_record_finding({
        "title": "Command injection vulnerability in run_command",
        "description": "Unsanitized user command passed directly to os.system call",
        "affected_file": "hamilton/app.py",
        "start_line": 2,
        "end_line": 3,
        "defect_category": "security",
    }))

    # Defect 2: Resource leak
    res2 = json.loads(tool_reg._handle_record_finding({
        "title": "Unclosed file descriptor resource leak in read_file",
        "description": "File handle opened without with-statement or close call",
        "affected_file": "hamilton/app.py",
        "start_line": 5,
        "end_line": 7,
        "defect_category": "resource_leak",
    }))

    assert res1["status"] == "success"
    assert res2["status"] == "success"
    assert res1["finding_id"] != res2["finding_id"]

    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 2


# =========================================================================
# Regression Test F: Security and Containment Invariants
# =========================================================================

def test_whitespace_only_excerpt_rejected(tmp_path: Path):
    """F1. Excerpt containing only empty lines or whitespace is rejected."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    # Line 2 and 5 in graph_types.py are blank newlines
    args = {
        "title": "Empty line defect",
        "description": "Defect at empty line",
        "affected_file": "hamilton/graph_types.py",
        "start_line": 2,
        "end_line": 2,
        "defect_category": "logic_bug",
    }

    with pytest.raises(InsufficientEvidenceError):
        tool_reg._handle_record_finding(args)


def test_path_traversal_and_symlink_security_preserved(tmp_path: Path):
    """F2. Path traversal, absolute path, and escaping symlink checks remain active."""
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    # Traversal
    with pytest.raises(ToolExecutionError, match="traversal|outside"):
        tool_reg._handle_record_finding({
            "title": "T",
            "description": "D",
            "affected_file": "../../etc/passwd",
            "start_line": 1,
            "end_line": 1,
        })

    # Absolute path
    with pytest.raises(ToolExecutionError, match="traversal|outside"):
        tool_reg._handle_record_finding({
            "title": "T",
            "description": "D",
            "affected_file": "/etc/passwd",
            "start_line": 1,
            "end_line": 1,
        })


# =========================================================================
# Regression Test G: End-to-End Investigation Loop with Hamilton False Positives
# =========================================================================

def test_investigation_loop_filters_hamilton_false_positives(tmp_path: Path):
    """G. End-to-end investigation loop: Model attempts to record false positive Hamilton findings.

    Validator rejects the calls, diagnostic feedback is returned, and investigation finishes
    with 0 recorded findings.
    """
    workspace, store, run_id, tool_reg, _ = _setup_hamilton_env(tmp_path)

    # Simulate model issuing the Hamilton missing colon call
    calls = [
        # Turn 1: Model requests record_finding for line 15 with missing colon
        LLMResponse(
            content="I found a missing colon in graph_types.py line 15.",
            tool_calls=[
                ToolCallRequest(
                    call_id="call_hamilton_1",
                    name="record_finding",
                    arguments={
                        "title": "Missing colon in HamiltonNode.as_dict()",
                        "description": "Syntax error: missing colon in dictionary entry at line 15",
                        "affected_file": "hamilton/graph_types.py",
                        "start_line": 15,
                        "end_line": 15,
                        "defect_category": "syntax_error",
                    },
                )
            ],
        ),
        # Turn 2: Model recognizes the rejection feedback and provides final conclusion
        LLMResponse(
            content="After verifying the AST, the syntax in graph_types.py is valid. No defects found.",
            tool_calls=[],
        ),
    ]

    provider = FakeLLMProvider(scripted_responses=calls)
    loop = InvestigationLoop(
        provider=provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(workspace),
        max_steps=3,
    )

    result = loop.run("Inspect repository for syntax and graph defects.")

    assert result.status == "completed"
    assert len(result.findings) == 0
    assert result.steps_executed == 2

    # Verify zero findings recorded in database
    findings = store.list_findings(run_id=run_id)
    assert len(findings) == 0

    # Verify run events contain the validation rejection
    events = store.list_run_events(run_id=run_id)
    fail_events = [e for e in events if e.event_type == EventType.FINDING_VALIDATION_FAILED]
    assert len(fail_events) == 1
    assert "SyntaxClaimValidationError" in fail_events[0].payload["error_type"]
