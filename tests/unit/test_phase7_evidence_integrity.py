"""Comprehensive unit and regression tests for Phase 7 Evidence Integrity & Quality Gates.

Verifies:
1-5: validate_repository_path existence, containment, type, and security invariants.
6-9: read_source_excerpt evidence generation, content hashing, bounds, and whitespace rejection.
10: inspect_dependencies manifest tracking and evidence persistence.
11: find_related_files does not falsely record inspection.
12-15: record_finding validation against non-existent, uninspected, manifest-only, and valid files.
16-17: InvestigationPlanner strategic ordering and inspection recording integrity.
18-19: validate_synthesis_quality rejects unverified files and sweeping security guarantees.
20-21: determine_investigation_quality enforces source inspection and evidence existence gates.
"""

import hashlib
import json
from pathlib import Path
import pytest

from forge.analysis.evidence import create_evidence
from forge.analysis.validation import (
    RepositoryPathValidationError,
    InsufficientEvidenceError,
    validate_repository_path,
)
from forge.investigation.loop import (
    determine_investigation_quality,
    validate_synthesis_quality,
)
from forge.investigation.planner import InvestigationPlanner
from forge.investigation.tools import ToolRegistry, ToolExecutionError
from forge.repository.architecture import RepositoryMap
from forge.repository.detector import detect_repository
from forge.repository.indexer import build_symbol_index
from forge.schemas import (
    EventType,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


def _create_fixture_workspace(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    pyproject = ws / "pyproject.toml"
    pyproject.write_text('[project]\nname = "fastapi"\ndependencies = ["pydantic>=2.9.0"]\n', encoding="utf-8")

    fastapi_dir = ws / "fastapi"
    fastapi_dir.mkdir(parents=True, exist_ok=True)
    datastructures = fastapi_dir / "datastructures.py"
    datastructures.write_text(
        '"""Data structures module."""\n\n'
        "class UploadFile:\n"
        "    def __init__(self, filename: str):\n"
        "        self.filename = filename\n",
        encoding="utf-8",
    )

    tests_dir = ws / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_datastructures = tests_dir / "test_datastructures.py"
    test_datastructures.write_text(
        '"""Test datastructures module."""\n\n'
        "from fastapi.datastructures import UploadFile\n\n"
        "def test_upload_file():\n"
        '    f = UploadFile("test.txt")\n'
        '    assert f.filename == "test.txt"\n',
        encoding="utf-8",
    )

    empty_lines = ws / "empty.py"
    empty_lines.write_text("\n\n   \n\t\n\n", encoding="utf-8")

    db_path = tmp_path / "test.db"
    store = ForgeStore(db_path=db_path)
    run_id = store.create_run(target=str(ws), run_id="run_phase7_integrity")

    intel = detect_repository(ws)
    syms = build_symbol_index(ws)
    repo_map = RepositoryMap(
        target="https://github.com/fastapi/fastapi.git",
        resolved_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        target_branch="master",
        intelligence=intel,
        symbol_index=syms,
    )

    inspected_files = set()
    tool_reg = ToolRegistry(
        workspace_path=ws,
        repo_map=repo_map,
        store=store,
        run_id=run_id,
        inspected_files=inspected_files,
        rejection_budget=3,
    )

    return ws, store, run_id, repo_map, tool_reg, inspected_files


# ---------------------------------------------------------------------------
# Scenarios 1-5: validate_repository_path
# ---------------------------------------------------------------------------

def test_validate_repository_path_accepts_valid_relative_files(tmp_path: Path):
    """1. validate_repository_path accepts valid repository-relative files."""
    ws, _, _, _, _, _ = _create_fixture_workspace(tmp_path)
    resolved, rel = validate_repository_path(ws, "fastapi/datastructures.py")
    assert resolved == (ws / "fastapi/datastructures.py").resolve()
    assert rel == "fastapi/datastructures.py"


def test_validate_repository_path_rejects_nonexistent_files(tmp_path: Path):
    """2. validate_repository_path rejects nonexistent files with RepositoryPathValidationError."""
    ws, _, _, _, _, _ = _create_fixture_workspace(tmp_path)
    with pytest.raises(RepositoryPathValidationError) as exc:
        validate_repository_path(ws, "src/auth.py")
    assert "does not exist in repository workspace" in str(exc.value)


def test_validate_repository_path_rejects_directories(tmp_path: Path):
    """3. validate_repository_path rejects directory paths."""
    ws, _, _, _, _, _ = _create_fixture_workspace(tmp_path)
    with pytest.raises(RepositoryPathValidationError) as exc:
        validate_repository_path(ws, "fastapi")
    assert "not a regular file" in str(exc.value)


def test_validate_repository_path_rejects_path_traversal_and_absolute_paths(tmp_path: Path):
    """4. validate_repository_path rejects path traversal and absolute paths."""
    ws, _, _, _, _, _ = _create_fixture_workspace(tmp_path)
    with pytest.raises(RepositoryPathValidationError):
        validate_repository_path(ws, "../../etc/passwd")

    with pytest.raises(RepositoryPathValidationError):
        validate_repository_path(ws, "/etc/passwd")


def test_validate_repository_path_rejects_symlinks_escaping_workspace(tmp_path: Path):
    """5. validate_repository_path rejects symlinks escaping the repository."""
    ws, _, _, _, _, _ = _create_fixture_workspace(tmp_path)
    secret = tmp_path / "secret.key"
    secret.write_text("SECRET")
    sym = ws / "sym_escape.py"
    try:
        sym.symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks not supported")

    with pytest.raises(RepositoryPathValidationError):
        validate_repository_path(ws, "sym_escape.py")


# ---------------------------------------------------------------------------
# Scenarios 6-9: read_source_excerpt Evidence Generation & Bounds Checking
# ---------------------------------------------------------------------------

def test_read_source_excerpt_creates_traceable_evidence(tmp_path: Path):
    """6. read_source_excerpt creates EvidenceRecord in store with commit_sha and content_hash."""
    ws, store, run_id, _, tool_reg, inspected_files = _create_fixture_workspace(tmp_path)
    output = tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "fastapi/datastructures.py", "start_line": 3, "end_line": 5},
    )
    assert "class UploadFile:" in output
    assert "fastapi/datastructures.py" in inspected_files
    assert "fastapi/datastructures.py" in tool_reg.inspected_source_files

    evidence = store.list_evidence_for_run(run_id)
    assert len(evidence) >= 1
    ev = next(e for e in evidence if e.file_path == "fastapi/datastructures.py")
    assert ev.evidence_type == EvidenceType.SOURCE_EXCERPT
    assert ev.start_line == 3
    assert ev.end_line == 5
    assert ev.details.get("commit_sha") == "45cb2252c737906f37544f5ba6bee0295fb81468"
    assert ev.details.get("operation") == "read_source_excerpt"
    assert ev.details.get("content_hash") is not None


def test_read_source_excerpt_populates_inspected_sets(tmp_path: Path):
    """7. read_source_excerpt populates both inspected_source_files and inspected_files."""
    ws, _, _, _, tool_reg, inspected_files = _create_fixture_workspace(tmp_path)
    tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "tests/test_datastructures.py", "start_line": 1, "end_line": 7},
    )
    assert "tests/test_datastructures.py" in tool_reg.inspected_source_files
    assert "tests/test_datastructures.py" in inspected_files


def test_read_source_excerpt_empty_slice_rejected(tmp_path: Path):
    """8. read_source_excerpt on empty or whitespace-only slice does NOT create evidence or record inspection."""
    ws, store, run_id, _, tool_reg, inspected_files = _create_fixture_workspace(tmp_path)
    output = tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "empty.py", "start_line": 1, "end_line": 4},
    )
    assert "contains only empty lines or whitespace" in output
    assert "empty.py" not in tool_reg.inspected_source_files
    assert "empty.py" not in inspected_files
    assert len(store.list_evidence_for_run(run_id)) == 0


def test_read_source_excerpt_out_of_bounds_does_not_create_evidence(tmp_path: Path):
    """9. read_source_excerpt with out of bounds start line returns notice and creates no evidence."""
    ws, store, run_id, _, tool_reg, inspected_files = _create_fixture_workspace(tmp_path)
    output = tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "fastapi/datastructures.py", "start_line": 500, "end_line": 520},
    )
    assert "is out of bounds" in output
    assert "fastapi/datastructures.py" not in tool_reg.inspected_source_files
    assert len(store.list_evidence_for_run(run_id)) == 0


# ---------------------------------------------------------------------------
# Scenarios 10-11: inspect_dependencies & find_related_files
# ---------------------------------------------------------------------------

def test_inspect_dependencies_creates_evidence_and_manifest_tracking(tmp_path: Path):
    """10. inspect_dependencies populates inspected_manifests and creates EvidenceRecord."""
    ws, store, run_id, _, tool_reg, inspected_files = _create_fixture_workspace(tmp_path)
    out = tool_reg.execute_tool("inspect_dependencies", {})
    data = json.loads(out)
    assert "pyproject.toml" in data["manifest_sources"]
    assert "pyproject.toml" in tool_reg.inspected_manifests
    assert "pyproject.toml" in inspected_files
    assert "pyproject.toml" not in tool_reg.inspected_source_files

    evidence = store.list_evidence_for_run(run_id)
    manifest_ev = next(e for e in evidence if e.evidence_type == EvidenceType.STATIC_ANALYSIS)
    assert manifest_ev.details.get("operation") == "inspect_dependencies"


def test_find_related_files_does_not_falsely_mark_inspection(tmp_path: Path):
    """11. find_related_files returns related files without marking target or discovered files as inspected."""
    ws, _, _, _, tool_reg, inspected_files = _create_fixture_workspace(tmp_path)
    out = tool_reg.execute_tool("find_related_files", {"file_path": "tests/test_datastructures.py"})
    data = json.loads(out)
    assert "fastapi/datastructures.py" in data["implementations"]
    assert "tests/test_datastructures.py" not in inspected_files
    assert "tests/test_datastructures.py" not in tool_reg.inspected_source_files
    assert "fastapi/datastructures.py" not in inspected_files
    assert "fastapi/datastructures.py" not in tool_reg.inspected_source_files


# ---------------------------------------------------------------------------
# Scenarios 12-15: record_finding Validation Integrity
# ---------------------------------------------------------------------------

def test_record_finding_nonexistent_file_produces_structured_rejection(tmp_path: Path):
    """12. record_finding on non-existent file produces RepositoryPathValidationError and counts toward budget."""
    ws, store, run_id, _, tool_reg, _ = _create_fixture_workspace(tmp_path)
    out = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Auth Vulnerability",
            "description": "Hardcoded token in auth file",
            "affected_file": "src/auth.py",
            "start_line": 1,
            "end_line": 5,
        },
    )
    data = json.loads(out)
    assert data["status"] == "rejected"
    assert data["rejection_type"] == "RepositoryPathValidationError"
    assert tool_reg.rejected_tracker.rejection_count == 1

    events = store.list_run_events(run_id)
    fail_ev = [e for e in events if e.event_type == EventType.FINDING_VALIDATION_FAILED]
    assert len(fail_ev) == 1
    assert fail_ev[0].payload["error_type"] == "RepositoryPathValidationError"


def test_record_finding_uninspected_file_rejected(tmp_path: Path):
    """13. record_finding on uninspected file is rejected with InsufficientEvidenceError."""
    ws, store, run_id, _, tool_reg, _ = _create_fixture_workspace(tmp_path)
    out = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Uninspected Bug",
            "description": "Speculative bug without reading code",
            "affected_file": "fastapi/datastructures.py",
            "start_line": 3,
            "end_line": 6,
        },
    )
    data = json.loads(out)
    assert data["status"] == "rejected"
    assert data["rejection_type"] == "InsufficientEvidenceError"
    assert "has not been inspected with 'read_source_excerpt'" in data["reason"]
    assert tool_reg.rejected_tracker.rejection_count == 1


def test_record_finding_manifest_without_source_reading_rejected(tmp_path: Path):
    """14. record_finding on pyproject.toml when only inspect_dependencies was run is rejected."""
    ws, _, _, _, tool_reg, _ = _create_fixture_workspace(tmp_path)
    tool_reg.execute_tool("inspect_dependencies", {})
    assert "pyproject.toml" in tool_reg.inspected_manifests
    assert "pyproject.toml" not in tool_reg.inspected_source_files

    out = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "Outdated Pydantic",
            "description": "Manifest has insecure version",
            "affected_file": "pyproject.toml",
            "start_line": 1,
            "end_line": 3,
        },
    )
    data = json.loads(out)
    assert data["status"] == "rejected"
    assert data["rejection_type"] == "InsufficientEvidenceError"


def test_record_finding_on_inspected_file_persists_finding_and_evidence(tmp_path: Path):
    """15. record_finding on inspected file persists finding, creates verified evidence with commit_sha."""
    ws, store, run_id, _, tool_reg, _ = _create_fixture_workspace(tmp_path)
    tool_reg.execute_tool(
        "read_source_excerpt",
        {"file_path": "fastapi/datastructures.py", "start_line": 3, "end_line": 6},
    )

    out = tool_reg.execute_tool(
        "record_finding",
        {
            "title": "UploadFile Constructor Logic",
            "description": "Missing file handle closing in constructor",
            "affected_file": "fastapi/datastructures.py",
            "start_line": 3,
            "end_line": 6,
            "severity": "medium",
        },
    )
    data = json.loads(out)
    assert data["status"] == "success"
    finding_id = data["finding_id"]
    evidence_id = data["evidence_id"]

    finding = store.get_finding(finding_id)
    assert finding is not None
    assert finding.lifecycle_state == FindingLifecycleState.SUSPECTED

    ev = store.get_evidence(evidence_id)
    assert ev is not None
    assert ev.finding_id == finding_id
    assert ev.details.get("commit_sha") == "45cb2252c737906f37544f5ba6bee0295fb81468"
    assert ev.details.get("operation") == "record_finding"


# ---------------------------------------------------------------------------
# Scenarios 16-17: InvestigationPlanner Ordering & Tracking
# ---------------------------------------------------------------------------

def test_planner_schedules_manifests_implementations_tests(tmp_path: Path):
    """16. InvestigationPlanner strategically orders manifests, implementations, and tests."""
    ws, store, _, repo_map, _, _ = _create_fixture_workspace(tmp_path)
    planner = InvestigationPlanner(
        workspace_path=ws,
        repo_map=repo_map,
        instruction="Audit datastructures and test assertions",
        store=store,
        target="https://github.com/fastapi/fastapi.git",
    )
    assert planner.planned_files[0] == "pyproject.toml"
    assert any("datastructures.py" in f for f in planner.planned_files[1:])


def test_planner_does_not_record_inspection_on_finding_or_discovery(tmp_path: Path):
    """17. Planner does not record inspection on find_related_files or record_finding."""
    ws, _, _, _, _, _ = _create_fixture_workspace(tmp_path)
    planner = InvestigationPlanner(workspace_path=ws)
    planner.record_tool_call("find_related_files", {"file_path": "tests/test_datastructures.py"})
    assert "tests/test_datastructures.py" not in planner.inspected_files

    planner.record_tool_call("record_finding", {"affected_file": "fastapi/datastructures.py"})
    assert "fastapi/datastructures.py" not in planner.inspected_files


# ---------------------------------------------------------------------------
# Scenarios 18-19: validate_synthesis_quality
# ---------------------------------------------------------------------------

def test_validate_synthesis_quality_rejects_unverified_files():
    """18. validate_synthesis_quality rejects references to hallucinated/unverified files."""
    verified = {"fastapi/datastructures.py", "tests/test_datastructures.py", "pyproject.toml"}
    hallucinated_text = (
        "Audited the repository. In db_connection.py, connection pooling is securely configured. "
        "In src/auth.py, JWT authentication is properly implemented."
    )
    assert validate_synthesis_quality(hallucinated_text, verified_files=verified) is False

    truthful_text = (
        "Audited fastapi/datastructures.py and tests/test_datastructures.py alongside pyproject.toml. "
        "No security defects were verified in the inspected code."
    )
    assert validate_synthesis_quality(truthful_text, verified_files=verified) is True


def test_validate_synthesis_quality_rejects_sweeping_security_claims():
    """19. validate_synthesis_quality rejects sweeping security guarantees."""
    assert validate_synthesis_quality("The repository is completely secure and free from all vulnerabilities.") is False
    assert validate_synthesis_quality("The codebase is secure and guaranteed to be secure.") is False
    assert validate_synthesis_quality("No defects were verified across the inspected scope.") is True


# ---------------------------------------------------------------------------
# Scenarios 20-21: determine_investigation_quality Quality Gates
# ---------------------------------------------------------------------------

def test_determine_investigation_quality_zero_source_files_cannot_be_completed():
    """20. Zero source files inspected cannot yield 'completed' quality even if manifests were read."""
    quality = determine_investigation_quality(
        loop_status="completed",
        total_tool_calls=3,
        inspected_files=["pyproject.toml"],
        findings=[],
        rejected_records=[],
        used_fallback_synthesis=False,
        has_valid_synthesis=True,
        inspected_source_files=[],
        total_evidence_count=1,
    )
    assert quality == "insufficient_evidence"


def test_determine_investigation_quality_zero_evidence_cannot_be_completed():
    """21. Zero persisted evidence cannot yield 'completed' quality."""
    quality = determine_investigation_quality(
        loop_status="completed",
        total_tool_calls=3,
        inspected_files=["fastapi/datastructures.py", "pyproject.toml"],
        findings=[],
        rejected_records=[],
        used_fallback_synthesis=False,
        has_valid_synthesis=True,
        inspected_source_files=["fastapi/datastructures.py"],
        total_evidence_count=0,
    )
    assert quality == "insufficient_evidence"
