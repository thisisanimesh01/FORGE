"""Smoke and subprocess regression tests for CLI startup, import graph, and storage decoupling.

Guarantees that no circular import cycles exist between forge.cli, forge.diagnostics,
forge.storage, forge.analysis, and forge.investigation.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from forge.analysis.validation import (
    FindingEvidenceProvider,
    check_duplicate_finding,
    validate_finding_claim,
)
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
PYTHON_EXE = sys.executable


def _run_subprocess_code(code: str) -> subprocess.CompletedProcess:
    """Run Python code snippet in a fresh subprocess with PYTHONPATH=app."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR / "app")
    return subprocess.run(
        [PYTHON_EXE, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
        env=env,
    )


def _run_subprocess_cli(*args: str) -> subprocess.CompletedProcess:
    """Run forge CLI entrypoint in a fresh subprocess with PYTHONPATH=app."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR / "app")
    forge_cli = ROOT_DIR / ".venv" / "bin" / "forge"
    cmd = [str(forge_cli)] if forge_cli.exists() else [PYTHON_EXE, "-m", "forge.cli"]
    return subprocess.run(
        cmd + list(args),
        capture_output=True,
        text=True,
        cwd=str(ROOT_DIR),
        env=env,
    )


# ---------------------------------------------------------------------------
# Fresh-Process Import Smoke Tests (Guarantees No Circular Imports)
# ---------------------------------------------------------------------------

def test_import_forge_cli_in_fresh_process():
    """Importing forge.cli must not trigger circular import errors."""
    proc = _run_subprocess_code("from forge.cli import app; print('CLI_IMPORT_OK')")
    assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "CLI_IMPORT_OK" in proc.stdout


def test_import_forge_storage_store_in_fresh_process():
    """Importing forge.storage.store must not trigger circular import errors."""
    proc = _run_subprocess_code("from forge.storage.store import ForgeStore; print('STORE_IMPORT_OK')")
    assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "STORE_IMPORT_OK" in proc.stdout


def test_import_forge_analysis_validation_in_fresh_process():
    """Importing forge.analysis.validation must not trigger circular import errors."""
    proc = _run_subprocess_code(
        "from forge.analysis.validation import validate_finding_claim; print('VALIDATION_IMPORT_OK')"
    )
    assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "VALIDATION_IMPORT_OK" in proc.stdout


def test_import_packages_order_invariance_in_fresh_process():
    """Importing forge.analysis before forge.storage and vice versa works cleanly."""
    proc1 = _run_subprocess_code("import forge.analysis; import forge.storage; print('ORDER1_OK')")
    assert proc1.returncode == 0, f"STDOUT: {proc1.stdout}\nSTDERR: {proc1.stderr}"
    assert "ORDER1_OK" in proc1.stdout

    proc2 = _run_subprocess_code("import forge.storage; import forge.analysis; print('ORDER2_OK')")
    assert proc2.returncode == 0, f"STDOUT: {proc2.stdout}\nSTDERR: {proc2.stderr}"
    assert "ORDER2_OK" in proc2.stdout


def test_import_diagnostics_without_eager_store_load():
    """Importing forge.diagnostics must succeed without circular import."""
    proc = _run_subprocess_code("from forge.diagnostics import run_system_diagnostics; print('DIAG_OK')")
    assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "DIAG_OK" in proc.stdout


# ---------------------------------------------------------------------------
# CLI Command Execution Smoke Tests
# ---------------------------------------------------------------------------

def test_cli_command_version():
    """'forge version' command succeeds with exit code 0."""
    proc = _run_subprocess_cli("version")
    assert proc.returncode == 0, f"STDERR: {proc.stderr}"
    assert "FORGE version:" in proc.stdout


def test_cli_command_doctor():
    """'forge doctor' diagnostic command executes cleanly without import error."""
    proc = _run_subprocess_cli("doctor")
    assert proc.returncode == 0, f"STDERR: {proc.stderr}"
    assert "FORGE System Health Diagnostic" in proc.stdout


def test_cli_command_investigate_help():
    """'forge investigate --help' displays usage information."""
    proc = _run_subprocess_cli("investigate", "--help")
    assert proc.returncode == 0, f"STDERR: {proc.stderr}"
    assert "Usage:" in proc.stdout
    assert "investigate" in proc.stdout


def test_cli_command_findings_help():
    """'forge findings --help' displays finding commands."""
    proc = _run_subprocess_cli("findings", "--help")
    assert proc.returncode == 0, f"STDERR: {proc.stderr}"
    assert "Finding inspection commands" in proc.stdout


def test_cli_command_db_help():
    """'forge db --help' displays database management commands."""
    proc = _run_subprocess_cli("db", "--help")
    assert proc.returncode == 0, f"STDERR: {proc.stderr}"
    assert "Database management commands" in proc.stdout


# ---------------------------------------------------------------------------
# Architectural Decoupling: Protocol & Pure In-Memory Verification
# ---------------------------------------------------------------------------

def test_forge_store_satisfies_finding_evidence_provider_protocol():
    """ForgeStore structurally satisfies FindingEvidenceProvider protocol."""
    assert issubclass(ForgeStore, FindingEvidenceProvider)


class MockFindingProvider:
    """Test double implementing FindingEvidenceProvider without SQLite."""

    def __init__(self, findings: List[Finding], evidence: Dict[str, List[EvidenceRecord]]):
        self._findings = findings
        self._evidence = evidence

    def list_findings(self, run_id: Optional[str] = None) -> List[Finding]:
        if run_id:
            return [f for f in self._findings if f.finding_id.startswith(run_id)]
        return self._findings

    def list_evidence_for_finding(self, finding_id: str) -> List[EvidenceRecord]:
        return self._evidence.get(finding_id, [])


def test_duplicate_detection_pure_in_memory_injection():
    """check_duplicate_finding works with pure in-memory collections without SQLite."""
    existing_finding = Finding(
        finding_id="find_001",
        title="Syntax error in parser",
        description="Missing colon in function definition line 10",
        lifecycle_state=FindingLifecycleState.SUSPECTED,
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        affected_files=["src/parser.py"],
    )
    existing_evidence = EvidenceRecord(
        evidence_id="evi_001",
        run_id="run_test",
        finding_id="find_001",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Excerpt showing line 10",
        file_path="src/parser.py",
        start_line=10,
        end_line=12,
        content="def parse_data()\n    return True\n",
        details={
            "defect_category": "syntax_error",
            "content_digest": "abcdef123456",
        },
    )

    # 1. Exact duplicate match with pure in-memory injection
    match = check_duplicate_finding(
        norm_path="src/parser.py",
        start_line=10,
        end_line=12,
        defect_category="syntax_error",
        title="Syntax error in parser",
        description="Missing colon in function definition line 10",
        content_digest="abcdef123456",
        existing_findings=[existing_finding],
        evidence_by_finding={"find_001": [existing_evidence]},
    )
    assert match is not None
    f_match, is_identical = match
    assert f_match.finding_id == "find_001"
    assert is_identical is True

    # 2. Non-duplicate on different file
    no_match = check_duplicate_finding(
        norm_path="src/other.py",
        start_line=10,
        end_line=12,
        defect_category="syntax_error",
        title="Syntax error in other",
        description="Missing colon in function definition line 10",
        content_digest="abcdef123456",
        existing_findings=[existing_finding],
        evidence_by_finding={"find_001": [existing_evidence]},
    )
    assert no_match is None


def test_duplicate_detection_with_mock_provider():
    """check_duplicate_finding works with a mock FindingEvidenceProvider."""
    existing_finding = Finding(
        finding_id="run_mock_001",
        title="Off by one error in loop",
        description="Index exceeds array bounds in loop iteration",
        lifecycle_state=FindingLifecycleState.SUSPECTED,
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.MEDIUM,
        affected_files=["src/loop.py"],
    )
    existing_evidence = EvidenceRecord(
        evidence_id="evi_mock_001",
        run_id="run_mock",
        finding_id="run_mock_001",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Loop bounds",
        file_path="src/loop.py",
        start_line=20,
        end_line=25,
        content="for i in range(len(arr) + 1):",
        details={
            "defect_category": "loop_defect",
            "content_digest": "digest789",
        },
    )

    mock_provider = MockFindingProvider(
        findings=[existing_finding],
        evidence={"run_mock_001": [existing_evidence]},
    )
    assert isinstance(mock_provider, FindingEvidenceProvider)

    match = check_duplicate_finding(
        finding_provider=mock_provider,
        run_id="run_mock",
        norm_path="src/loop.py",
        start_line=20,
        end_line=25,
        defect_category="loop_defect",
        title="Off by one error in loop",
        description="Index exceeds array bounds in loop iteration",
        content_digest="digest789",
    )
    assert match is not None
    f_match, is_identical = match
    assert f_match.finding_id == "run_mock_001"
    assert is_identical is True
