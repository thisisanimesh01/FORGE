"""Unit tests for Milestone 1: Config, Schemas, and CLI."""

import pytest
from typer.testing import CliRunner
from forge.cli import app
from forge.config import ForgeSettings, redact_secrets, get_settings
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
    FindingConfidence,
    RunEvent,
    EventType,
    BaselineCheckResult,
    CheckStatus,
    RepositoryMetadata,
)

runner = CliRunner()


def test_redact_secrets():
    raw = "Here is my secret token: sk-abcdef1234567890abcdef12345678 and api_key='secret12345'"
    redacted = redact_secrets(raw)
    assert "sk-" not in redacted or "[REDACTED_SECRET]" in redacted
    assert "secret12345" not in redacted


def test_forge_settings_sanitized():
    settings = ForgeSettings(llm_api_key="super_secret_test_key")
    sanitized = settings.get_sanitized_dict()
    assert sanitized["llm_api_key"] == "[REDACTED]"
    assert sanitized["env"] == "development"
    assert sanitized["max_steps"] == 10


def test_evidence_record_model():
    record = EvidenceRecord(
        evidence_id="ev_001",
        evidence_type=EvidenceType.SOURCE_LOCATION,
        description="Identified suspicious loop condition",
        file_path="src/parser.py",
        start_line=10,
        end_line=15,
        content="for i in range(len(arr) + 1):",
    )
    assert record.evidence_id == "ev_001"
    assert record.start_line == 10
    # Frozen check
    with pytest.raises(Exception):
        record.start_line = 20


def test_finding_lifecycle_and_evidence():
    finding = Finding(
        finding_id="f_001",
        title="Off-by-one boundary defect",
        description="Loop extends beyond slice bounds",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        lifecycle_state=FindingLifecycleState.SUSPECTED,
        affected_files=["src/parser.py"],
        supporting_evidence_ids=["ev_001"],
    )
    assert finding.lifecycle_state == FindingLifecycleState.SUSPECTED
    assert "ev_001" in finding.supporting_evidence_ids


def test_run_event_schema():
    event = RunEvent(
        event_id="evt_001",
        run_id="run_123",
        sequence=1,
        event_type=EventType.RUN_CREATED,
        payload={"target": "https://github.com/example/repo.git"},
    )
    assert event.sequence == 1
    assert event.event_type == EventType.RUN_CREATED


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "FORGE version:" in result.output


def test_cli_doctor():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "FORGE System Health Diagnostic" in result.output
    assert "Python Version:" in result.output
    assert "SQLite target path:" in result.output
