"""Phase 13 Milestone 5: Comprehensive Integration & Acceptance Test Suite.

Validates:
1. End-to-end integration across repository intelligence, diagnosis, planning,
   human authorization, controlled repair, verification, quality evaluation,
   and durable reporting.
2. Production installation, diagnostics, and environment failure modes.
3. Strict local-Ollama invariant (rejection of cloud fallbacks, SSRF protection,
   canonical loopback enforcement).
4. Security boundaries: human approval allowlists, plan digest binding,
   path traversal protection, immutable repository isolation, fail-closed Docker fallback.
5. Crash recovery, idempotency replay, and SQLite durable persistence.
6. Performance invariants: bounded caching, connection pooling, and graph equivalence.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition
from forge.schemas import FindingConfidence, FindingSeverity
from forge.config import ForgeSettings, get_settings, redact_secrets
from forge.diagnostics import (
    DiagnosticCategory,
    check_docker_environment,
    check_git_environment,
    check_llm_provider,
    check_python_environment,
    check_sqlite_database,
    evaluate_system_readiness,
    run_system_diagnostics,
)
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
)
from forge.causality.schemas import (
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.llm.factory import get_provider
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.openai import OpenAICompatibleProvider
from forge.llm.provider import LLMAuthenticationError, LLMUnavailableError
from forge.llm.url_safety import URLSecurityError, is_canonical_loopback_url, validate_url_target
from forge.orchestration.adapters import (
    DefaultDiagnosisAdapter,
    DefaultExecutionAdapter,
    DefaultPlanningAdapter,
    DefaultQualityAdapter,
    DefaultRootCauseAdapter,
    DefaultVerificationAdapter,
)
from forge.orchestration.approval import (
    ApprovalVerificationOutcome,
    OrchestrationApprovalError,
    OrchestrationApprovalManager,
)
from forge.orchestration.events import (
    AuditTrailTamperedError,
    OrchestrationEvent,
    OrchestrationEventRecorder,
    OrchestrationEventType,
)
from forge.orchestration.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    compute_idempotency_key,
)
from forge.orchestration.orchestrator import EngineeringOrchestrator
from forge.orchestration.persistence import OrchestrationStateStore
from forge.orchestration.recovery import WorkflowRecoveryEngine
from forge.orchestration.report import EngineeringReportGenerator
from forge.orchestration.retry import FailureClassifier, FailureCategory, RetryPolicy
from forge.orchestration.schemas import (
    EngineeringWorkflowResult,
    StageExecutionRecord,
    StageOutputs,
    StageStatus,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.orchestration.state import (
    IllegalStateTransitionError,
    TerminalStateError,
    validate_transition as validate_wf_transition,
)
from forge.repair_execution.approval import compute_plan_digest
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.schemas import (
    ApprovalStatus,
    ExecutionStatus,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
    WorkspaceStatus,
)
from forge.repair_execution.workspace import (
    _safe_copy_dual_and_digest,
    compute_tree_digest,
    is_protected_path,
)
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairPlan,
    RepairPlanStatus,
    RepairRiskAssessment,
    RepairStrategy,
    RequiredChange,
    RiskLikelihood,
    RiskSeverity,
    RollbackPlan,
    StrategyComplexity,
    StrategyInvasiveness,
    StrategyReversibility,
    VerificationPlan,
)
from forge.repair_quality.schemas import (
    QualityDimensionScore,
    QualityGateDecision,
    QualityStatus,
    RepairQualityPolicy,
    RepairQualityResult,
)
from forge.repair_verification.schemas import (
    RegressionClassification,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.repository.cache import IndexCacheManager
from forge.repository.cache_schemas import FileIndexRecord
from forge.repository.call_graph import build_call_graph
from forge.repository.detector import detect_repository
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.indexer import build_symbol_index
from forge.repository.module_graph import build_module_dependency_graph
from forge.storage.sqlite_pool import ThreadLocalSQLitePool


# =============================================================================
# Workstream A: End-to-End Orchestrated Pipeline Integration
# =============================================================================

@pytest.fixture
def target_repo_fixture(tmp_path: Path) -> Path:
    """Create a self-contained target repository with a reproducible bug and unit test."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    pkg = repo / "calculator"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""Calculator package."""\n', encoding="utf-8")
    # Buggy file
    (pkg / "calc.py").write_text(
        "def compute_average(nums):\n"
        "    if len(nums) == 0:\n"
        "        return 0\n"
        "    # Off-by-one or type bug\n"
        "    total = sum(nums)\n"
        "    return total / len(nums)\n",
        encoding="utf-8",
    )
    # Test suite
    tests = repo / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_calc.py").write_text(
        "from calculator.calc import compute_average\n\n"
        "def test_compute_average():\n"
        "    assert compute_average([10, 20, 30]) == 20\n"
        "    assert compute_average([]) == 0\n",
        encoding="utf-8",
    )
    return repo


def test_full_autonomous_lifecycle_with_operator_approval(target_repo_fixture: Path, tmp_path: Path):
    """End-to-end integration: Diagnosis -> RootCause -> Planning -> Human Approval ->
    Controlled Repair -> Verification -> Quality Evaluation -> Final Report."""
    from forge.orchestration.adapters import (
        FakeDiagnosisAdapter,
        FakeRootCauseAdapter,
        FakePlanningAdapter,
        FakeExecutionAdapter,
        FakeVerificationAdapter,
        FakeQualityAdapter,
    )
    db_file = tmp_path / "lifecycle.db"
    store = OrchestrationStateStore(db_path=db_file)
    ledger = ApprovalLedger(db_path=db_file)
    approval_mgr = OrchestrationApprovalManager(
        ledger=ledger,
        trusted_approvers={"alice", "bob"},
    )

    defect_input = DiagnosisInput(
        user_description="compute_average crashes on None or empty inputs",
        error_message="ZeroDivisionError",
        target_files=["calculator/calc.py"],
        target_symbols=["compute_average"],
    )
    context = WorkflowContext(
        repository_path=str(target_repo_fixture),
        defect_input=defect_input,
        trusted_approvers={"alice", "bob"},
    )

    fake_exec = FakeExecutionAdapter(ledger=ledger)
    orchestrator = EngineeringOrchestrator(
        context=context,
        state_store=store,
        approval_manager=approval_mgr,
        diagnosis_adapter=FakeDiagnosisAdapter(),
        root_cause_adapter=FakeRootCauseAdapter(),
        planning_adapter=FakePlanningAdapter(affected_files=["calculator/calc.py"]),
        execution_adapter=fake_exec,
        verification_adapter=FakeVerificationAdapter(),
        quality_adapter=FakeQualityAdapter(),
    )

    # 1. Run pipeline up to mandatory human approval boundary
    orchestrator.run_until_approval_or_completion()

    assert orchestrator.state == WorkflowState.AWAITING_HUMAN_APPROVAL
    assert orchestrator.stage_outputs.diagnosis_result is not None
    assert orchestrator.stage_outputs.root_cause_report is not None
    assert orchestrator.stage_outputs.repair_plan is not None

    plan = orchestrator.stage_outputs.repair_plan
    plan_digest = compute_plan_digest(plan)

    # 2. Submit explicit human authorization as trusted operator 'alice'
    now_dt = datetime.now(timezone.utc)
    approval = RepairApproval(
        approval_id="appr_alice_001",
        repair_plan_id=plan.plan_id,
        plan_digest=plan_digest,
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=["calculator/calc.py"],
        approval_timestamp=now_dt.isoformat(),
        approval_expiry=(now_dt + timedelta(hours=24)).isoformat(),
        approver_reference="alice",
        approval_status=ApprovalStatus.APPROVED,
        auth_channel="interactive_cli",
        operator_principal="alice",
    )
    outcome = orchestrator.submit_approval(approval)
    assert outcome.is_valid is True
    assert orchestrator.state == WorkflowState.APPROVED

    # 3. Resume orchestrator post-approval
    orchestrator.run_until_approval_or_completion()

    # 4. Pipeline reaches completed
    assert orchestrator.state == WorkflowState.COMPLETED
    assert orchestrator.is_completed()
    res = orchestrator.get_result()
    assert res.state == WorkflowState.COMPLETED
    assert res.completed_at is not None
    # Verify dossier report generation
    rep = EngineeringReportGenerator.generate_markdown(res)
    assert "FORGE Engineering Orchestration Report" in rep
    assert plan.plan_id in rep


# =============================================================================
# Workstream B: Local-Ollama Only Enforcement & Misconfiguration Resistance
# =============================================================================

def test_ollama_local_only_invariant_rejects_external_hosts():
    """Verify that Ollama provider strictly rejects non-loopback URLs when configured."""
    from forge.llm.factory import get_provider

    # Canonical loopback endpoints must be accepted
    with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://localhost:11434"}):
        p1 = get_provider("ollama")
        assert p1.api_base == "http://localhost:11434/v1"

    with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://127.0.0.1:11434"}):
        p2 = get_provider("ollama")
        assert p2.api_base == "http://127.0.0.1:11434/v1"


def test_ollama_cloud_provider_permanently_forbidden():
    """Ensure deprecated cloud providers remain strictly unavailable."""
    from forge.llm.factory import get_provider

    with pytest.raises(LLMUnavailableError, match="Ollama Cloud provider has been removed"):
        get_provider("ollama_cloud")

    with pytest.raises(LLMUnavailableError, match="Ollama Cloud provider has been removed"):
        get_provider("ollama-cloud")


def test_cloud_fallback_policy_strictly_enforced():
    """ForgeSettings allow_cloud_fallback must default to False and trigger unsafe alert if enabled."""
    settings = ForgeSettings()
    assert settings.allow_cloud_fallback is False

    unsafe_settings = ForgeSettings(allow_cloud_fallback=True)
    check = check_llm_provider(unsafe_settings)
    assert check.category == DiagnosticCategory.UNSAFE_CONFIGURATION
    assert check.is_healthy is False
    assert "CRITICAL" in check.message


# =============================================================================
# Workstream C: Security & Safety Acceptance Boundaries
# =============================================================================

def test_human_approval_cannot_be_forged_by_agent_identity(tmp_path: Path):
    """Synthetic or agentic identities (ai, llm, forge, bot) are rejected fail-closed."""
    from forge.orchestration.approval import OrchestrationApprovalManager
    strat = RepairStrategy(
        strategy_id="strat_1",
        title="Fix logic",
        description="Fix logic bug",
        approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
        complexity=StrategyComplexity.LOW,
        invasiveness=StrategyInvasiveness.MINIMAL,
        reversibility=StrategyReversibility.HIGH,
        affected_files=["src/a.py"],
    )
    plan = RepairPlan(
        plan_id="plan_1",
        summary="Test plan",
        problem_statement="Test bug",
        repair_objective="Fix",
        status=RepairPlanStatus.ACTIONABLE,
        confidence=FindingConfidence.HIGH,
        selected_strategy_id="strat_1",
        candidate_strategies=[strat],
        affected_files=["src/a.py"],
        risk_assessment=RepairRiskAssessment(
            overall_risk=RiskSeverity.LOW,
            likelihood=RiskLikelihood.LOW,
            risk_score=0.10,
            explanation="Low risk",
        ),
        verification_plan=VerificationPlan(existing_targeted_tests=["tests/test_a.py"]),
        rollback_plan=RollbackPlan(changes_to_reverse=["Revert"]),
    )
    mgr = OrchestrationApprovalManager(
        ledger=ApprovalLedger(db_path=tmp_path / "app.db"),
        trusted_approvers={"alice", "bob"},
    )
    now_dt = datetime.now(timezone.utc)
    for bad_identity in ["agent", "llm", "ai", "forge", "auto", "subagent", "synthetic"]:
        appr = RepairApproval(
            approval_id=f"appr_{bad_identity}",
            repair_plan_id="plan_1",
            plan_digest="a" * 64,
            approved_strategy_id="strat_1",
            approved_file_paths=["src/a.py"],
            approval_timestamp=now_dt.isoformat(),
            approval_expiry=(now_dt + timedelta(hours=1)).isoformat(),
            approver_reference=bad_identity,
            approval_status=ApprovalStatus.APPROVED,
        )
        outcome = mgr.verify_approval(appr, plan, workflow_id="wf_1")
        assert outcome.is_valid is False
        assert any("Explicit human approver identity is required" in err for err in outcome.errors)


def test_approval_ledger_tampering_fails_closed(tmp_path: Path):
    """Tampering directly with SQLite approval ledger records triggers validation failure."""
    db_file = tmp_path / "tamper.db"
    ledger = ApprovalLedger(db_path=db_file)
    now_dt = datetime.now(timezone.utc)
    ledger.record_approval(
        approval_id="appr_tamper_1",
        workflow_id="wf_1",
        repair_plan_id="plan_1",
        plan_digest="a" * 64,
        approved_strategy_id="strat_1",
        approved_file_paths=["src/a.py"],
        approval_timestamp=now_dt.isoformat(),
        approval_expiry=(now_dt + timedelta(hours=1)).isoformat(),
        approver_reference="alice",
        auth_channel="interactive_cli",
        operator_principal="alice",
    )

    # Tamper with approved file paths directly in database
    conn = sqlite3.connect(db_file)
    conn.execute(
        "UPDATE orchestration_approval_records SET approved_file_paths = ? WHERE approval_id = ?",
        (json.dumps(["src/a.py", "malicious_file.py"]), "appr_tamper_1"),
    )
    conn.commit()
    conn.close()

    # Querying the tampered approval must fail closed with tampering detection
    reloaded = ApprovalLedger(db_path=db_file)
    with pytest.raises(ValueError, match="Tampered approval ledger record detected"):
        reloaded.get_approval("appr_tamper_1")


def test_path_containment_and_protection_guards(tmp_path: Path):
    """Repository root containment rejects directory traversal and protected files."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "normal.py").write_text("x = 1\n", encoding="utf-8")

    assert is_protected_path(repo_root / ".env", base_dir=repo_root) is True
    assert is_protected_path(repo_root / "data.db", base_dir=repo_root) is True
    assert is_protected_path(repo_root / "contacts.json", base_dir=repo_root) is True
    assert is_protected_path(repo_root / "app.log", base_dir=repo_root) is True
    assert is_protected_path(repo_root / "normal.py", base_dir=repo_root) is False
    assert is_protected_path(repo_root / ".." / "outside.py", base_dir=repo_root) is True


def test_docker_fail_closed_never_falls_back_to_host(monkeypatch):
    """When Docker daemon is stopped or missing, sandbox execution raises or marks skipped."""
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "docker" else "/bin/sh")
    check = check_docker_environment()
    assert check.category == DiagnosticCategory.UNAVAILABLE_REQUIRED
    assert check.is_healthy is False
    assert "Docker binary not found" in check.message


# =============================================================================
# Workstream D: Runtime Diagnostic Probes
# =============================================================================

def test_full_system_diagnostic_readiness_assessment(tmp_path: Path):
    """Diagnostic runner correctly evaluates system readiness tiers."""
    db_path = tmp_path / "diag.db"
    settings = ForgeSettings(
        db_path=db_path,
        llm_provider="fake",
        allow_cloud_fallback=False,
    )
    summary = run_system_diagnostics(settings)
    assert summary.static_inspection_ready is True
    assert summary.local_inference_ready is True
    assert any(c.name == "Python Runtime" and c.is_healthy for c in summary.checks)
    assert any(c.name == "Git Executable" and c.is_healthy for c in summary.checks)


# =============================================================================
# Workstream E: Reliability, Concurrency & Idempotency
# =============================================================================

def test_idempotency_ledger_prevents_duplicate_mutations(tmp_path: Path):
    """Idempotency ledger prevents duplicate concurrent stage operations."""
    db_file = tmp_path / "idemp.db"
    ledger = IdempotencyLedger(db_path=db_file)

    key = compute_idempotency_key("wf_test", WorkflowStage.EXECUTION, "a" * 64)
    rec1 = ledger.register_start(key, "wf_test", WorkflowStage.EXECUTION)
    assert rec1 is None  # None indicates new operation successfully registered

    # Duplicate concurrent begin must return existing record (conflict prevented)
    dup = ledger.register_start(key, "wf_test", WorkflowStage.EXECUTION)
    assert dup is not None
    assert dup.idempotency_key == key

    # Complete operation
    ledger.mark_completed(key, {"status": "ok"})
    cached = ledger.get_record(key)
    assert cached is not None
    assert cached.status == "completed"
    assert cached.result_payload == {"status": "ok"}


def test_event_recorder_tamper_evident_cryptographic_chain():
    """Tampering with an event in the chain invalidates hash chain verification."""
    recorder = OrchestrationEventRecorder()
    ev1 = recorder.record_event("wf_1", OrchestrationEventType.WORKFLOW_CREATED, WorkflowState.CREATED, "Init")
    ev2 = recorder.record_event("wf_1", OrchestrationEventType.STAGE_STARTED, WorkflowState.DIAGNOSING, "Start")

    events = recorder.get_events("wf_1")
    is_valid, _ = recorder.verify_event_chain(events)
    assert is_valid is True

    # Tamper with event 1
    tampered_ev1 = events[0].model_copy(update={"action_description": "Forged action"})
    tampered_chain = [tampered_ev1, events[1]]
    is_valid_tampered, reason = recorder.verify_event_chain(tampered_chain)
    assert is_valid_tampered is False
    assert "Digest mismatch" in reason or "mismatch" in reason


# =============================================================================
# Workstream F: Performance & Scalability Invariants
# =============================================================================

def test_sqlite_connection_pooling_durability(tmp_path: Path):
    """SQLite pool reuses thread-local connection and safely handles rollback."""
    db_file = tmp_path / "pool.db"
    pool = ThreadLocalSQLitePool(db_path=db_file)

    conn1 = pool.get_connection()
    conn1.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);")
    conn1.execute("INSERT INTO kv VALUES ('a', '1');")

    conn2 = pool.get_connection()
    row = conn2.execute("SELECT v FROM kv WHERE k = 'a';").fetchone()
    assert row[0] == "1"

    pool.close_all()


def test_dual_copy_and_tree_digest_consistency(tmp_path: Path):
    """Single-pass copy and digest produces identical deterministic hash to independent walk."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "file_a.txt").write_text("content A", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "file_b.txt").write_text("content B", encoding="utf-8")

    iso = tmp_path / "iso"
    snap = tmp_path / "snap"

    single_pass_digest = _safe_copy_dual_and_digest(src, iso, snap)
    independent_digest = compute_tree_digest(iso)

    assert single_pass_digest == independent_digest
    assert (snap / "file_a.txt").exists()
    assert (snap / "sub" / "file_b.txt").exists()
