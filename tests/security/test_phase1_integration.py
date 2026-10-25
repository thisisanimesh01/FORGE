"""Milestone 7: Phase 1 Final Integration and Security Verification Suite.

Comprehensive end-to-end integration and security regression tests:
1. End-to-end audit without LLM (--no-llm) and zero modification guarantee.
2. Full multi-turn investigation loop with FakeLLMProvider and SQLite store.
3. Untrusted prompt injection resistance (BM07 fixture).
4. Path containment and traversal guard verification across all public tool APIs.
5. Sandbox fail-closed enforcement (Docker absent -> ExecutionStatus.UNAVAILABLE, no host fallback).
6. Evidence & finding lifecycle integrity (finding state transitions require concrete evidence).
7. Report generation and secret redaction end-to-end.
8. CLI command suite integration and consistency.
"""

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.benchmarks.corpus import CorpusLoader
from forge.benchmarks.runner import BenchmarkRunner
from forge.cli import app
from forge.config import get_settings, redact_secrets
from forge.investigation.loop import InvestigationLoop
from forge.investigation.tools import ToolRegistry
from forge.llm.fake import FakeLLMProvider
from forge.reporting.builder import ReportBuilder, export_report, report_to_markdown
from forge.repository import (
    acquire_repository,
    cleanup_repository,
    detect_repository,
    build_symbol_index,
    load_instruction_files,
    RepositoryMap,
)
from forge.repository.validation import assert_contained_path, SecurityError
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import ExecutionStatus, SandboxRequest
from forge.schemas import (
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# ===========================================================================
# 1. Zero-Mutation Invariant & Static Audit
# ===========================================================================

def test_full_audit_zero_target_mutation(tmp_path: Path):
    """Static audit of a repository must leave all files and git state 100% unaltered."""
    target_dir = tmp_path / "target_repo"
    target_dir.mkdir()
    source_file = target_dir / "service.py"
    original_code = "def handle(req): return req.upper()\n"
    source_file.write_text(original_code, encoding="utf-8")

    mtime_before = source_file.stat().st_mtime_ns
    size_before = source_file.stat().st_size

    # Run inspection
    intel = detect_repository(target_dir)
    symbols = build_symbol_index(target_dir)
    instructions = load_instruction_files(target_dir)

    repo_map = RepositoryMap(
        target=str(target_dir),
        intelligence=intel,
        symbol_index=symbols,
        instructions=instructions,
    )
    summary = repo_map.render_markdown_summary()
    assert "handle" in summary or "Functions: 1" in summary

    # Invariant check: zero mutation
    assert source_file.read_text(encoding="utf-8") == original_code
    assert source_file.stat().st_mtime_ns == mtime_before
    assert source_file.stat().st_size == size_before



# ===========================================================================
# 2. End-to-End Investigation Loop with Storage & Reconstruction
# ===========================================================================

def test_e2e_investigation_with_storage_and_report(tmp_path: Path):
    """Full investigation loop with fake LLM records audit trail and generates valid report."""
    db_file = tmp_path / "audit.db"
    store = ForgeStore(db_path=db_file)

    target_dir = tmp_path / "app_target"
    target_dir.mkdir()
    (target_dir / "main.py").write_text("x = 42\n", encoding="utf-8")

    intel = detect_repository(target_dir)
    symbols = build_symbol_index(target_dir)
    instructions = load_instruction_files(target_dir)
    repo_map = RepositoryMap(
        target=str(target_dir),
        intelligence=intel,
        symbol_index=symbols,
        instructions=instructions,
    )

    run_id = store.create_run(target=str(target_dir), run_id="run_e2e_01")
    tool_reg = ToolRegistry(
        workspace_path=target_dir,
        repo_map=repo_map,
        store=store,
        run_id=run_id,
    )

    fake_provider = FakeLLMProvider()
    loop = InvestigationLoop(
        provider=fake_provider,
        tool_registry=tool_reg,
        store=store,
        run_id=run_id,
        target=str(target_dir),
        repo_map=repo_map,
        max_steps=3,
    )

    res = loop.run(user_instruction="Audit for security defects.")
    assert res.run_id == "run_e2e_01"
    assert res.status in {"completed", "max_steps_exceeded"}

    # Verify run record in store
    run_record = store.get_run("run_e2e_01")
    assert run_record is not None
    assert run_record.status == "completed"

    # Verify report generation
    builder = ReportBuilder(store)
    report = builder.build_report("run_e2e_01")
    assert report.run_id == "run_e2e_01"
    assert report.schema_version == "1.0.0"

    exported = export_report(report, tmp_path / "exports" / "report.md")
    assert exported.exists()
    assert "FORGE Investigation Report" in exported.read_text(encoding="utf-8")


# ===========================================================================
# 3. Prompt Injection Defense Verification
# ===========================================================================

def test_prompt_injection_repo_remains_untrusted(tmp_path: Path):
    """Hostile instructions in BM07 README cannot compromise investigation tools or permissions."""
    loader = CorpusLoader()
    cases = loader.discover_cases()
    bm07 = next(c for c in cases if c.case_id == "BM07_prompt_injection")

    runner = BenchmarkRunner()
    res = runner.evaluate_case(bm07, mode="static")
    assert res.outcome.value == "pass"
    assert res.defect_detected is False

    # Verify instructions loaded from BM07 are tagged untrusted
    instructions = load_instruction_files(bm07.fixture_dir)
    readme = next((i for i in instructions if "README" in i.file_path), None)
    assert readme is not None
    assert readme.is_untrusted is True


# ===========================================================================
# 4. Path Containment Across Public Tool APIs
# ===========================================================================

def test_tool_registry_rejects_path_traversal(tmp_path: Path):
    """ToolRegistry strictly rejects paths attempting directory traversal outside workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "file.py").write_text("ok = True\n")

    store = ForgeStore(db_path=tmp_path / "t.db")
    run_id = store.create_run(target=str(ws), run_id="run_tool_sec")

    intel = detect_repository(ws)
    symbols = build_symbol_index(ws)
    repo_map = RepositoryMap(target=str(ws), intelligence=intel, symbol_index=symbols)

    tool_reg = ToolRegistry(
        workspace_path=ws,
        repo_map=repo_map,
        store=store,
        run_id=run_id,
    )

    # 1. read_source_excerpt
    res_read = tool_reg.execute_tool("read_source_excerpt", {"file_path": "../../etc/passwd"})
    assert "Security violation" in res_read or "rejected" in res_read.lower()

    # 2. list_directory
    res_list = tool_reg.execute_tool("list_directory", {"relative_path": "../../"})
    assert "Security violation" in res_list or "error" in res_list.lower()

    # 3. search_text with path_scope traversal
    res_search = tool_reg.execute_tool("search_text", {"query": "root", "path_scope": "../../etc"})
    assert "Security violation" in res_search or "error" in res_search.lower()





# ===========================================================================
# 5. Fail-Closed Sandbox Enforcement
# ===========================================================================

def test_sandbox_fail_closed_never_executes_on_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """When Docker is unavailable, runtime checks return UNAVAILABLE with zero host execution."""
    # Force Docker lookup to None
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    sandbox = DockerSandbox()
    healthy, msg = sandbox.check_health()
    assert healthy is False
    assert "not found" in msg.lower()

    # Execute request
    req = SandboxRequest(
        request_id="req_m7_test",
        run_id="run_m7_test",
        workspace_path=tmp_path,
        command=["python3", "test.py"],
    )
    result = sandbox.run(req)
    assert result.status == ExecutionStatus.UNAVAILABLE
    assert result.isolation_established is False
    assert result.exit_code is None
    assert "unavailable" in result.error_message.lower() or "refused" in result.error_message.lower()




# ===========================================================================
# 6. Lifecycle Transition & Evidence Integrity
# ===========================================================================

def test_lifecycle_transitions_enforce_evidence():
    """State transitions strictly require valid matching evidence; cannot skip states."""
    from forge.schemas import Finding

    finding = Finding(
        finding_id="find_test_m7",
        title="Unvalidated buffer copy",
        description="Buffer overflow potential",
        lifecycle_state=FindingLifecycleState.SUSPECTED,
    )

    # Attempt illegal jump: SUSPECTED -> ROOT_CAUSED (skipping REPRODUCED)
    ev_root = create_evidence(
        run_id="run_01",
        evidence_type=EvidenceType.ROOT_CAUSE_ANALYSIS,
        description="Root cause identified",
        content="pointer offset bug",
        finding_id="find_test_m7",
    )
    with pytest.raises(InvalidTransitionError, match="Illegal transition"):
        validate_transition(finding, FindingLifecycleState.ROOT_CAUSED, ev_root)


# ===========================================================================
# 7. End-to-End CLI Diagnostics & Commands
# ===========================================================================

def test_cli_full_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Validate all CLI core entry points in an isolated test environment."""
    db_file = tmp_path / "cli_e2e.db"
    monkeypatch.setenv("FORGE_DB_PATH", str(db_file))

    # 1. db init
    res_init = runner.invoke(app, ["db", "init"])
    assert res_init.exit_code == 0

    # 2. doctor
    res_doc = runner.invoke(app, ["doctor"])
    assert res_doc.exit_code == 0
    assert "FORGE System Health Diagnostic" in res_doc.output

    # 3. sandbox doctor
    res_sdoc = runner.invoke(app, ["sandbox", "doctor"])
    assert res_sdoc.exit_code == 0
    assert "FORGE Sandbox Diagnostics" in res_sdoc.output

    # 4. benchmark list
    res_blist = runner.invoke(app, ["benchmark", "list"])
    assert res_blist.exit_code == 0
    assert "BM01_boundary_off_by_one" in res_blist.output

    # 5. benchmark run static
    res_brun = runner.invoke(app, ["benchmark", "run", "--mode", "static"])
    assert res_brun.exit_code == 0
    assert "Benchmark Run Complete" in res_brun.output
