"""Comprehensive tests for Milestone 3: Evidence, Findings, Storage, and CLI."""

import pytest
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typer.testing import CliRunner

from forge.schemas import (
    EvidenceType,
    EvidenceRecord,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
    FindingConfidence,
    EventType,
    RunEvent,
    RunSummary,
)
from forge.analysis.evidence import (
    create_evidence,
    normalize_relative_path,
    EvidenceValidationError,
    MAX_CONTENT_CHARS,
)
from forge.analysis.findings import (
    validate_transition,
    InvalidTransitionError,
    ALLOWED_TRANSITIONS,
    REQUIRED_EVIDENCE_TYPE,
)
from forge.analysis.reconstruction import (
    reconstruct_finding_lifecycle,
    ReconstructionError,
)
from forge.storage.database import init_database, get_db_connection, SCHEMA_VERSION
from forge.storage.store import ForgeStore, StoreError
from forge.cli import app

runner = CliRunner()


# -------------------------------------------------------
# Evidence Validation Tests
# -------------------------------------------------------

def test_create_evidence_basic():
    """Create a valid evidence record."""
    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Found suspicious loop",
        content="for i in range(len(arr)+1):",
        file_path="src/parser.py",
        start_line=10,
        end_line=15,
    )
    assert ev.run_id == "run_001"
    assert ev.evidence_type == EvidenceType.SOURCE_EXCERPT
    assert ev.file_path == "src/parser.py"
    assert ev.start_line == 10
    assert ev.end_line == 15
    assert ev.evidence_id.startswith("evi_")


def test_create_evidence_empty_run_id():
    """Empty run_id must raise."""
    with pytest.raises(EvidenceValidationError, match="run_id cannot be empty"):
        create_evidence(
            run_id="",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="test",
            content="test",
        )


def test_create_evidence_empty_description():
    """Empty description must raise."""
    with pytest.raises(EvidenceValidationError, match="description cannot be empty"):
        create_evidence(
            run_id="run_001",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="",
            content="test",
        )


def test_create_evidence_path_traversal_rejected():
    """Path traversal attempts must raise."""
    with pytest.raises(EvidenceValidationError, match="path traversal"):
        create_evidence(
            run_id="run_001",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="test",
            content="test",
            file_path="../../etc/passwd",
        )


def test_create_evidence_absolute_path_rejected():
    """Absolute file paths must raise."""
    with pytest.raises(EvidenceValidationError, match="path traversal"):
        create_evidence(
            run_id="run_001",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="test",
            content="test",
            file_path="/etc/passwd",
        )


def test_create_evidence_content_truncation():
    """Content exceeding MAX_CONTENT_CHARS is truncated."""
    long_content = "x" * (MAX_CONTENT_CHARS + 1000)
    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.COMMAND_RESULT,
        description="Long output",
        content=long_content,
    )
    assert len(ev.content) < len(long_content)
    assert "TRUNCATED" in ev.content


def test_create_evidence_secret_redaction():
    """Secrets in content are redacted."""
    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.COMMAND_RESULT,
        description="Check output",
        content="Found token: sk-abcdef1234567890abcdef12 in config",
    )
    assert "sk-" not in ev.content or "REDACTED" in ev.content


def test_create_evidence_invalid_lines():
    """Invalid line numbers must raise."""
    with pytest.raises(EvidenceValidationError, match="start_line must be >= 1"):
        create_evidence(
            run_id="run_001",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="test",
            content="test",
            start_line=0,
        )


def test_create_evidence_end_before_start():
    """end_line < start_line must raise."""
    with pytest.raises(EvidenceValidationError, match="end_line.*cannot be less than start_line"):
        create_evidence(
            run_id="run_001",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="test",
            content="test",
            start_line=10,
            end_line=5,
        )


def test_normalize_path_valid():
    """Valid relative paths are normalized."""
    assert normalize_relative_path("src/parser.py") == "src/parser.py"
    assert normalize_relative_path("  src\\windows\\file.py  ") == "src/windows/file.py"
    assert normalize_relative_path(None) is None
    assert normalize_relative_path("") is None


def test_evidence_type_backward_compat():
    """Backward compatibility aliases resolve correctly."""
    assert EvidenceType.SOURCE_LOCATION == EvidenceType.SOURCE_EXCERPT
    assert EvidenceType.SOURCE_LOCATION.value == "source_excerpt"
    assert EvidenceType.STATIC_CHECK == EvidenceType.STATIC_ANALYSIS
    assert EvidenceType.COMMAND_OUTPUT == EvidenceType.COMMAND_RESULT


# -------------------------------------------------------
# Finding State Machine Tests
# -------------------------------------------------------

def _make_finding(state: FindingLifecycleState = FindingLifecycleState.SUSPECTED) -> Finding:
    return Finding(
        finding_id="find_test",
        title="Test finding",
        description="A test",
        lifecycle_state=state,
    )


def _make_evidence(
    ev_type: EvidenceType = EvidenceType.REPRODUCTION_RESULT,
    finding_id: str = "find_test",
    details: dict = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="evi_test",
        run_id="run_test",
        finding_id=finding_id,
        evidence_type=ev_type,
        description="Test evidence",
        content="test content",
        details=details or {},
    )


def test_valid_transition_suspected_to_reproduced():
    """Valid: suspected -> reproduced with reproduction_result evidence."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    evidence = _make_evidence(EvidenceType.REPRODUCTION_RESULT)
    validate_transition(finding, FindingLifecycleState.REPRODUCED, evidence)


def test_illegal_transition_suspected_to_fixed():
    """Illegal: cannot skip states."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    evidence = _make_evidence(EvidenceType.PATCH_REFERENCE)
    with pytest.raises(InvalidTransitionError, match="Illegal transition"):
        validate_transition(finding, FindingLifecycleState.FIXED, evidence)


def test_wrong_evidence_type_for_transition():
    """Wrong evidence type for target state."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    # Should need REPRODUCTION_RESULT but providing SOURCE_EXCERPT
    evidence = _make_evidence(EvidenceType.SOURCE_EXCERPT)
    with pytest.raises(InvalidTransitionError, match="requires evidence of type"):
        validate_transition(finding, FindingLifecycleState.REPRODUCED, evidence)


def test_idempotent_transition_rejected():
    """Same state transition is rejected."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    evidence = _make_evidence(EvidenceType.SOURCE_EXCERPT)
    with pytest.raises(InvalidTransitionError, match="already in state"):
        validate_transition(finding, FindingLifecycleState.SUSPECTED, evidence)


def test_mismatched_finding_id_rejected():
    """Evidence for a different finding is rejected."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    evidence = _make_evidence(EvidenceType.REPRODUCTION_RESULT, finding_id="other_finding")
    with pytest.raises(InvalidTransitionError, match="belongs to finding"):
        validate_transition(finding, FindingLifecycleState.REPRODUCED, evidence)


def test_failed_reproduction_rejected():
    """Cannot mark as reproduced with failed reproduction status."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    evidence = _make_evidence(
        EvidenceType.REPRODUCTION_RESULT,
        details={"status": "failed"},
    )
    with pytest.raises(InvalidTransitionError, match="failed reproduction"):
        validate_transition(finding, FindingLifecycleState.REPRODUCED, evidence)


def test_verified_transition_blocked_by_failure():
    """Cannot verify with failed verification evidence."""
    finding = _make_finding(FindingLifecycleState.FIXED)
    evidence = _make_evidence(
        EvidenceType.VERIFICATION_RESULT,
        details={"status": "failed"},
    )
    with pytest.raises(InvalidTransitionError, match="failed verification"):
        validate_transition(finding, FindingLifecycleState.VERIFIED, evidence)


def test_allowed_transitions_coverage():
    """All lifecycle states are covered in ALLOWED_TRANSITIONS."""
    for state in FindingLifecycleState:
        assert state in ALLOWED_TRANSITIONS, f"Missing state: {state}"


def test_terminal_state_has_no_transitions():
    """VERIFIED is terminal with no outgoing transitions."""
    assert ALLOWED_TRANSITIONS[FindingLifecycleState.VERIFIED] == []


# -------------------------------------------------------
# SQLite Database Tests
# -------------------------------------------------------

def test_init_database_creates_tables(tmp_path: Path):
    """init_database creates all required tables."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = sorted([r["name"] for r in cur.fetchall()])
        assert "evidence" in tables
        assert "findings" in tables
        assert "finding_transitions" in tables
        assert "run_events" in tables
        assert "runs" in tables
        assert "schema_migrations" in tables
    finally:
        conn.close()


def test_init_database_idempotent(tmp_path: Path):
    """Calling init_database twice does not fail."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    init_database(db_path)  # Should not raise


def test_schema_version_recorded(tmp_path: Path):
    """Schema version is recorded in migrations table."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT version FROM schema_migrations")
        versions = [r["version"] for r in cur.fetchall()]
        assert SCHEMA_VERSION in versions
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path: Path):
    """Foreign keys are enabled on connections."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    conn = get_db_connection(db_path)
    try:
        result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert result == 1
    finally:
        conn.close()


# -------------------------------------------------------
# ForgeStore Persistence Tests
# -------------------------------------------------------

def test_store_create_and_get_run(tmp_path: Path):
    """Create a run and retrieve it."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    run_id = store.create_run("https://github.com/example/repo.git", run_id="run_001")
    assert run_id == "run_001"
    run = store.get_run("run_001")
    assert run is not None
    assert run.target == "https://github.com/example/repo.git"
    assert run.status == "running"


def test_store_finish_run(tmp_path: Path):
    """Finish a run and verify status update."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_002")
    store.finish_run("run_002", status="completed", summary={"total": 5})
    run = store.get_run("run_002")
    assert run.status == "completed"
    assert run.completed_at is not None


def test_store_finish_nonexistent_run(tmp_path: Path):
    """Finishing a non-existent run raises StoreError."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    with pytest.raises(StoreError, match="not found"):
        store.finish_run("nonexistent")


def test_store_list_runs(tmp_path: Path):
    """List runs returns all created runs."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target_a", run_id="run_a")
    store.create_run("target_b", run_id="run_b")
    runs = store.list_runs()
    assert len(runs) == 2


def test_store_create_finding(tmp_path: Path):
    """Create a finding and verify initial state."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    finding = store.create_finding(
        run_id="run_001",
        title="Off-by-one error",
        description="Loop exceeds bounds",
        severity=FindingSeverity.HIGH,
        finding_id="find_001",
    )
    assert finding.finding_id == "find_001"
    assert finding.lifecycle_state == FindingLifecycleState.SUSPECTED

    persisted = store.get_finding("find_001")
    assert persisted is not None
    assert persisted.title == "Off-by-one error"


def test_store_create_finding_no_run(tmp_path: Path):
    """Creating a finding for a non-existent run raises StoreError."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    with pytest.raises(StoreError, match="does not exist"):
        store.create_finding(run_id="nonexistent", title="t", description="d")


def test_store_attach_and_retrieve_evidence(tmp_path: Path):
    """Attach evidence and retrieve it."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="t", description="d", finding_id="find_001")

    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Found bug",
        content="x = arr[len(arr)]",
        finding_id="find_001",
        evidence_id="evi_001",
    )
    store.attach_evidence(ev)

    retrieved = store.get_evidence("evi_001")
    assert retrieved is not None
    assert retrieved.evidence_type == EvidenceType.SOURCE_EXCERPT
    assert retrieved.finding_id == "find_001"


def test_store_list_evidence_for_finding(tmp_path: Path):
    """List evidence returns all evidence for a finding."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="t", description="d", finding_id="find_001")

    for i in range(3):
        ev = create_evidence(
            run_id="run_001",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description=f"Evidence {i}",
            content=f"content {i}",
            finding_id="find_001",
            evidence_id=f"evi_{i:03d}",
        )
        store.attach_evidence(ev)

    evidence_list = store.list_evidence_for_finding("find_001")
    assert len(evidence_list) == 3


# -------------------------------------------------------
# State Transition Persistence Tests
# -------------------------------------------------------

def test_store_transition_finding(tmp_path: Path):
    """Transition a finding and verify persisted state."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="Bug", description="d", finding_id="find_001")

    # Attach reproduction evidence
    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Reproduced the bug",
        content="Error: IndexError at line 42",
        finding_id="find_001",
        evidence_id="evi_repro",
        details={"status": "reproduced"},
    )
    store.attach_evidence(ev)

    updated = store.transition_finding("find_001", FindingLifecycleState.REPRODUCED, "evi_repro")
    assert updated.lifecycle_state == FindingLifecycleState.REPRODUCED

    # Verify persistence
    persisted = store.get_finding("find_001")
    assert persisted.lifecycle_state == FindingLifecycleState.REPRODUCED


def test_store_transition_creates_event(tmp_path: Path):
    """Transition emits FINDING_STATE_TRANSITIONED event."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="Bug", description="d", finding_id="find_001")

    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="Error output",
        finding_id="find_001",
        evidence_id="evi_repro",
    )
    store.attach_evidence(ev)
    store.transition_finding("find_001", FindingLifecycleState.REPRODUCED, "evi_repro")

    events = store.list_run_events("run_001")
    transition_events = [e for e in events if e.event_type == EventType.FINDING_STATE_TRANSITIONED]
    assert len(transition_events) >= 1
    assert transition_events[0].payload["from_state"] == "suspected"
    assert transition_events[0].payload["to_state"] == "reproduced"


def test_store_illegal_transition_rejected(tmp_path: Path):
    """Illegal state transition does not modify finding."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="Bug", description="d", finding_id="find_001")

    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.PATCH_REFERENCE,
        description="Patch",
        content="diff",
        finding_id="find_001",
        evidence_id="evi_patch",
    )
    store.attach_evidence(ev)

    with pytest.raises(InvalidTransitionError):
        store.transition_finding("find_001", FindingLifecycleState.FIXED, "evi_patch")

    # Verify state unchanged
    finding = store.get_finding("find_001")
    assert finding.lifecycle_state == FindingLifecycleState.SUSPECTED


# -------------------------------------------------------
# Event History Tests
# -------------------------------------------------------

def test_events_sequential_ordering(tmp_path: Path):
    """Events are recorded with sequential sequence numbers."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")

    store.append_event("run_001", EventType.BASELINE_STARTED, {"check": "ast"})
    store.append_event("run_001", EventType.BASELINE_COMPLETED, {"check": "ast", "passed": True})

    events = store.list_run_events("run_001")
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)  # all unique


def test_events_secret_redaction(tmp_path: Path):
    """Secrets in event payloads are redacted."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")

    store.append_event("run_001", EventType.TOOL_CALL_COMPLETED, {
        "output": "Found token: sk-abcdef1234567890abcdef12 in env",
    })

    events = store.list_run_events("run_001")
    tool_events = [e for e in events if e.event_type == EventType.TOOL_CALL_COMPLETED]
    assert len(tool_events) == 1
    payload_str = str(tool_events[0].payload)
    assert "sk-" not in payload_str or "REDACTED" in payload_str


def test_run_creates_initial_event(tmp_path: Path):
    """Creating a run emits RUN_CREATED event."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    events = store.list_run_events("run_001")
    assert len(events) >= 1
    assert events[0].event_type == EventType.RUN_CREATED


def test_finish_run_emits_event(tmp_path: Path):
    """Finishing a run emits RUN_COMPLETED or RUN_FAILED event."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.finish_run("run_001", status="completed")
    events = store.list_run_events("run_001")
    event_types = [e.event_type for e in events]
    assert EventType.RUN_COMPLETED in event_types


# -------------------------------------------------------
# Reconstruction Tests
# -------------------------------------------------------

def test_reconstruct_finding_no_transitions(tmp_path: Path):
    """Reconstruction of a finding with no transitions returns SUSPECTED."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="Bug", description="d", finding_id="find_001")

    state = reconstruct_finding_lifecycle("find_001", store)
    assert state == FindingLifecycleState.SUSPECTED


def test_reconstruct_finding_with_transition(tmp_path: Path):
    """Reconstruction follows transitions correctly."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="Bug", description="d", finding_id="find_001")

    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="Error",
        finding_id="find_001",
        evidence_id="evi_repro",
    )
    store.attach_evidence(ev)
    store.transition_finding("find_001", FindingLifecycleState.REPRODUCED, "evi_repro")

    state = reconstruct_finding_lifecycle("find_001", store)
    assert state == FindingLifecycleState.REPRODUCED


def test_reconstruct_nonexistent_finding(tmp_path: Path):
    """Reconstruction of nonexistent finding raises."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    with pytest.raises(ReconstructionError, match="not found"):
        reconstruct_finding_lifecycle("nonexistent", store)


# -------------------------------------------------------
# CLI Integration Tests
# -------------------------------------------------------

def test_cli_db_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge db init creates the database."""
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "cli_test.db"))
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0
    assert "initialized successfully" in result.output.lower() or "Initializing" in result.output


def test_cli_runs_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge runs with no data shows no runs."""
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "cli_test.db"))
    init_database(tmp_path / "cli_test.db")
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0
    assert "No runs found" in result.output or "Run ID" in result.output


def test_cli_findings_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge findings list with no data shows no findings."""
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "cli_test.db"))
    init_database(tmp_path / "cli_test.db")
    result = runner.invoke(app, ["findings", "list"])
    assert result.exit_code == 0
    assert "No findings found" in result.output or "Finding ID" in result.output


def test_cli_findings_show_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """forge findings show with unknown ID fails."""
    monkeypatch.setenv("FORGE_DB_PATH", str(tmp_path / "cli_test.db"))
    init_database(tmp_path / "cli_test.db")
    result = runner.invoke(app, ["findings", "show", "nonexistent"])
    assert result.exit_code == 1


def test_cli_version():
    """forge version works."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "FORGE version:" in result.output


def test_cli_doctor():
    """forge doctor includes database status."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "FORGE System Health Diagnostic" in result.output


def test_cli_missing_db_shows_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI commands fail cleanly when database has not been initialized."""
    missing_db = tmp_path / "does_not_exist.sqlite3"
    monkeypatch.setenv("FORGE_DB_PATH", str(missing_db))
    
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 1
    assert "Run 'forge db init' first" in result.output

    result_findings = runner.invoke(app, ["findings", "list"])
    assert result_findings.exit_code == 1
    assert "Run 'forge db init' first" in result_findings.output


# -------------------------------------------------------
# Milestone 3 Security & State Machine Hardening Tests
# -------------------------------------------------------

def test_transition_blocked_by_truncated_evidence():
    """Truncated evidence cannot authorize reproduction or verification."""
    finding = _make_finding(FindingLifecycleState.SUSPECTED)
    ev = create_evidence(
        run_id="run_test",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="x" * (MAX_CONTENT_CHARS + 100),
        finding_id="find_test",
    )
    assert "[TRUNCATED:" in ev.content
    with pytest.raises(InvalidTransitionError, match="truncated evidence"):
        validate_transition(finding, FindingLifecycleState.REPRODUCED, ev)


def test_transition_run_id_mismatch_rejected():
    """Evidence from another run cannot authorize transition."""
    finding = Finding(
        finding_id="find_01",
        run_id="run_A",
        title="Bug",
        description="d",
        lifecycle_state=FindingLifecycleState.SUSPECTED,
    )
    ev = create_evidence(
        run_id="run_B",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="reproduced error",
        finding_id="find_01",
    )
    with pytest.raises(InvalidTransitionError, match="which does not match finding"):
        validate_transition(finding, FindingLifecycleState.REPRODUCED, ev)


def test_verification_rejected_with_untrusted_provenance():
    """Verification transition requires trusted execution provenance."""
    finding = _make_finding(FindingLifecycleState.FIXED)
    ev = create_evidence(
        run_id="run_test",
        evidence_type=EvidenceType.VERIFICATION_RESULT,
        description="Passed",
        content="PASSED test_suite",
        finding_id="find_test",
        provenance="untrusted:caller_claim",
    )
    with pytest.raises(InvalidTransitionError, match="Untrusted verification evidence provenance"):
        validate_transition(finding, FindingLifecycleState.VERIFIED, ev)


def test_verification_allowed_with_trusted_provenance():
    """Verification transition succeeds with trusted verifier provenance."""
    finding = _make_finding(FindingLifecycleState.FIXED)
    ev = create_evidence(
        run_id="run_test",
        evidence_type=EvidenceType.VERIFICATION_RESULT,
        description="Passed",
        content="PASSED test_suite",
        finding_id="find_test",
        provenance="forge:verifier",
    )
    validate_transition(finding, FindingLifecycleState.VERIFIED, ev)


def test_evidence_oversized_details_rejected():
    """Oversized structured details dictionaries are rejected."""
    huge_details = {"key_" + str(i): "val_" * 100 for i in range(1000)}
    with pytest.raises(EvidenceValidationError, match="exceeds maximum allowed size"):
        create_evidence(
            run_id="run_test",
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description="desc",
            content="content",
            details=huge_details,
        )


def test_reconstruction_detects_event_tampering(tmp_path: Path):
    """Reconstruction detects when run_events stream differs from transition rows."""
    store = ForgeStore(db_path=tmp_path / "test.db")
    store.create_run("target", run_id="run_001")
    store.create_finding(run_id="run_001", title="Bug", description="d", finding_id="find_001")

    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="Error",
        finding_id="find_001",
        evidence_id="evi_repro",
    )
    store.attach_evidence(ev)
    store.transition_finding("find_001", FindingLifecycleState.REPRODUCED, "evi_repro")

    # Manually tamper with the run_events table (simulating event drop/divergence)
    conn = store._conn()
    conn.execute("DELETE FROM run_events WHERE event_type = ?", (EventType.FINDING_STATE_TRANSITIONED.value,))
    conn.commit()
    conn.close()

    with pytest.raises(ReconstructionError, match="Event log mismatch"):
        reconstruct_finding_lifecycle("find_001", store)

