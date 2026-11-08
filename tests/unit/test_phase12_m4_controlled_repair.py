"""Unit tests for Phase 12 M4: Controlled Autonomous Repair.

Verifies:
- Mandatory authorization & human approval invariants (digest, scope, expiry, status).
- Isolated workspace lifecycle, baseline tree digest, path containment, host protection.
- Patch generation and strict unified diff hunk syntax/boundary validation.
- Pure in-memory patch application and automatic backup reversion on failure.
- Workspace integrity verification and rollback restoration.
- Secret redaction in audit events and execution summaries.
- ToolRegistry integration of all 8 repair execution tools.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import pytest
import shutil
from typing import Any, Dict, List, Optional

from forge.repair_planning.schemas import (
    RepairApproach,
    RepairEvidence,
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
from forge.repair_execution.approval import (
    RepairApprovalValidator,
    compute_plan_digest,
)
from forge.repair_execution.events import RepairEventRecorder
from forge.repair_execution.executor import ControlledRepairExecutor
from forge.repair_execution.integrity import WorkspaceIntegrityChecker
from forge.repair_execution.patch_applier import ControlledPatchApplier
from forge.repair_execution.patch_generator import RepairPatchGenerator
from forge.repair_execution.patch_validator import (
    RepairPatchValidator,
    compute_patch_digest,
)
from forge.repair_execution.rollback import WorkspaceRollbackManager
from forge.repair_execution.schemas import (
    ApprovalStatus,
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    PatchValidationStatus,
    RepairApproval,
    RepairEvent,
    RepairEventType,
    RepairPatch,
    RepairWorkspace,
    RollbackStatus,
    WorkspaceStatus,
)
from forge.repair_execution.tools import RepairExecutionTools
from forge.repair_execution.workspace import (
    RepairWorkspaceManager,
    compute_tree_digest,
)
from forge.schemas import FindingConfidence
from forge.investigation.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

def _build_dummy_plan(
    plan_id: str = "plan_test_001",
    status: RepairPlanStatus = RepairPlanStatus.ACTIONABLE,
    file_path: str = "src/calculator.py",
    has_stale_evidence: bool = False,
    required_changes: Optional[List[RequiredChange]] = None,
) -> RepairPlan:
    """Construct a well-formed RepairPlan for testing."""
    strategy = RepairStrategy(
        strategy_id="strat_defensive_guard",
        title="Add Zero Division Check",
        description="Add guard before division",
        approach=RepairApproach.INPUT_VALIDATION,
        affected_files=[file_path],
        affected_symbols=["divide"],
        supporting_evidence=["evi_01"],
        expected_benefits=["Prevents ZeroDivisionError"],
        risks=["Raises ValueError instead"],
        complexity=StrategyComplexity.LOW,
        invasiveness=StrategyInvasiveness.MINIMAL,
        reversibility=StrategyReversibility.HIGH,
        test_requirements=["tests/test_calculator.py"],
        is_recommended=True,
        score=0.9,
    )

    req_change = RequiredChange(
        file_path=file_path,
        symbol_id="divide",
        start_line=4,
        end_line=6,
        change_type="add_guard",
        description="Add guard against b == 0",
        rationale="Prevents ZeroDivisionError",
    )

    risk = RepairRiskAssessment(
        overall_risk=RiskSeverity.LOW,
        likelihood=RiskLikelihood.LOW,
        risk_score=0.1,
        affected_components=["src.calculator"],
        potential_regressions=["None expected"],
        security_implications=["Input validation added"],
        data_integrity_implications=["None"],
        dependency_implications=["None"],
        explanation="Low risk defensive check",
        supporting_evidence=["evi_01"],
    )

    verif = VerificationPlan(
        existing_targeted_tests=["tests/test_calculator.py"],
        recommended_new_tests=["tests/test_calculator.py::test_divide_zero"],
        regression_test_scope=["tests/"],
        security_checks=[],
        static_analysis_requirements=["mypy"],
        expected_verification_criteria=["All tests pass"],
    )

    rollback = RollbackPlan(
        changes_to_reverse=["Revert guard in src/calculator.py"],
        files_and_symbols_involved=["src/calculator.py:divide"],
        dependencies_to_restore=[],
        data_configuration_considerations=[],
        rollback_triggers=["Test failure"],
        known_limitations=[],
    )

    evidence = [
        RepairEvidence(
            evidence_id="evi_01",
            evidence_source="diagnosis",
            file_path=file_path,
            symbol_reference="divide",
            start_line=4,
            end_line=6,
            evidence_type="source_excerpt",
            is_fresh=not has_stale_evidence,
            relationship_to_defect="Shows division without guard",
        )
    ]

    effective_changes = (
        required_changes
        if required_changes is not None
        else ([req_change] if status == RepairPlanStatus.ACTIONABLE else [])
    )

    plan = RepairPlan(
        plan_id=plan_id,
        finding_id="find_div_zero",
        root_cause_reference="node_div_zero",
        status=status,
        summary="Add defensive check for zero division",
        problem_statement="divide() does not guard against b == 0",
        repair_objective="Raise ValueError when b == 0",
        selected_strategy_id="strat_defensive_guard",
        candidate_strategies=[strategy],
        affected_files=[file_path],
        affected_symbols=["divide"],
        required_changes=effective_changes,
        preconditions=["Python 3.11+"],
        dependencies=[],
        risk_assessment=risk,
        expected_side_effects=[],
        verification_plan=verif,
        rollback_plan=rollback,
        supporting_evidence=evidence,
        confidence=FindingConfidence.HIGH,
        limitations="Guards division only",
        approval_required=True,
        execution_permitted=False,
    )
    return plan


def _build_dummy_approval(
    plan: RepairPlan,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    expired: bool = False,
    file_scope: list = None,
    approver: str = "engineer@example.com",
) -> RepairApproval:
    """Construct an approval contract bound to a RepairPlan."""
    digest = compute_plan_digest(plan)
    now = datetime.now(timezone.utc)
    expiry = (now - timedelta(hours=1)) if expired else (now + timedelta(hours=24))

    return RepairApproval(
        approval_id="appr_test_001",
        repair_plan_id=plan.plan_id,
        plan_digest=digest,
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=file_scope if file_scope is not None else list(plan.affected_files),
        approval_timestamp=now.isoformat(),
        approval_expiry=expiry.isoformat(),
        approver_reference=approver,
        approval_status=status,
    )


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a minimal mock repository for workspace testing."""
    repo = tmp_path / "mock_repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    calc = src / "calculator.py"
    calc.write_text(
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def divide(a, b):\n"
        "    return a / b\n"
    )
    tests = repo / "tests"
    tests.mkdir()
    t_calc = tests / "test_calculator.py"
    t_calc.write_text(
        "from src.calculator import add, divide\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
    )
    # Add a protected file to test exclusion/purging
    env_file = repo / ".env"
    env_file.write_text("SECRET_KEY=supersecret123\n")
    return repo


# ---------------------------------------------------------------------------
# 1. Authorization & Approval Invariant Tests
# ---------------------------------------------------------------------------

def test_approval_validation_valid():
    """Valid approval matches plan and passes all 10 invariants."""
    plan = _build_dummy_plan()
    approval = _build_dummy_approval(plan)
    validator = RepairApprovalValidator()

    result = validator.validate_approval(approval, plan)
    assert result.is_valid is True
    assert len(result.errors) == 0


def test_approval_validation_status_not_approved():
    """Approval with PENDING, REJECTED, or REVOKED status is rejected."""
    plan = _build_dummy_plan()
    validator = RepairApprovalValidator()

    for bad_status in [ApprovalStatus.PENDING, ApprovalStatus.REJECTED, ApprovalStatus.REVOKED]:
        approval = _build_dummy_approval(plan, status=bad_status)
        result = validator.validate_approval(approval, plan)
        assert result.is_valid is False
        assert any("must be 'approved'" in e for e in result.errors)


def test_approval_validation_expired():
    """Expired approval is strictly rejected."""
    plan = _build_dummy_plan()
    approval = _build_dummy_approval(plan, expired=True)
    validator = RepairApprovalValidator()

    result = validator.validate_approval(approval, plan)
    assert result.is_valid is False
    assert any("expired" in e.lower() for e in result.errors)


def test_approval_validation_digest_mismatch():
    """Tampered plan or modified digest is rejected."""
    plan = _build_dummy_plan()
    approval = _build_dummy_approval(plan)
    # Tamper with approval plan digest
    bad_approval = RepairApproval(
        approval_id="appr_tampered",
        repair_plan_id=plan.plan_id,
        plan_digest="0" * 64,
        approved_strategy_id=plan.selected_strategy_id,
        approved_file_paths=[plan.affected_files[0]],
        approval_timestamp=approval.approval_timestamp,
        approval_expiry=approval.approval_expiry,
        approver_reference="engineer@example.com",
        approval_status=ApprovalStatus.APPROVED,
    )
    validator = RepairApprovalValidator()
    result = validator.validate_approval(bad_approval, plan)
    assert result.is_valid is False
    assert any("plan digest mismatch" in e for e in result.errors)


def test_approval_validation_scope_breach():
    """Approval does not contain required change file path."""
    plan = _build_dummy_plan(file_path="src/calculator.py")
    # Authorize a different file
    approval = _build_dummy_approval(plan, file_scope=["src/other.py"])
    validator = RepairApprovalValidator()

    result = validator.validate_approval(approval, plan)
    assert result.is_valid is False
    assert any("outside approved scope" in e for e in result.errors)


def test_approval_validation_non_actionable_plan():
    """Non-actionable or stale plans are rejected."""
    plan_infeasible = _build_dummy_plan(status=RepairPlanStatus.UNRESOLVED)
    approval_infeasible = _build_dummy_approval(plan_infeasible)
    validator = RepairApprovalValidator()
    res1 = validator.validate_approval(approval_infeasible, plan_infeasible)
    assert res1.is_valid is False
    assert any("ACTIONABLE" in e for e in res1.errors)

    plan_stale = _build_dummy_plan(has_stale_evidence=True)
    approval_stale = _build_dummy_approval(plan_stale)
    res2 = validator.validate_approval(approval_stale, plan_stale)
    assert res2.is_valid is False
    assert any("stale" in e for e in res2.errors)


# ---------------------------------------------------------------------------
# 2. Isolated Workspace Lifecycle & Safety Tests
# ---------------------------------------------------------------------------

def test_workspace_allocation_isolation(sample_repo: Path, tmp_path: Path):
    """Workspace manager copies files to isolated dir; original repo untouched."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)

    orig_digest_before = compute_tree_digest(sample_repo)

    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")
    assert ws.workspace_status == WorkspaceStatus.READY
    assert Path(ws.isolated_workspace_path).exists()
    assert (Path(ws.isolated_workspace_path) / "src" / "calculator.py").exists()

    # Verify protected file (.env) was excluded or purged
    assert not (Path(ws.isolated_workspace_path) / ".env").exists()

    # Original repo remained completely untouched
    orig_digest_after = compute_tree_digest(sample_repo)
    assert orig_digest_before == orig_digest_after

    # Cleanup
    assert mgr.cleanup_workspace(ws.workspace_id) is True
    assert not Path(ws.isolated_workspace_path).exists()


def test_workspace_containment_and_traversal(sample_repo: Path, tmp_path: Path):
    """Path containment asserts reject directory traversal."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")

    # Contained path succeeds
    safe_path = mgr.verify_workspace_containment(ws, "src/calculator.py")
    assert safe_path.exists()

    # Traversal raises SecurityError
    with pytest.raises(Exception):
        mgr.verify_workspace_containment(ws, "../../etc/passwd")

    mgr.cleanup_workspace(ws.workspace_id)


def test_workspace_preserve_for_inspection(sample_repo: Path, tmp_path: Path):
    """Preserved workspace is not cleaned up unless force=True."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")

    mgr.preserve_for_inspection(ws.workspace_id)
    assert ws.workspace_status == WorkspaceStatus.PRESERVED_FOR_INSPECTION

    # Regular cleanup fails
    assert mgr.cleanup_workspace(ws.workspace_id, force=False) is False
    assert Path(ws.isolated_workspace_path).exists()

    # Force cleanup succeeds
    assert mgr.cleanup_workspace(ws.workspace_id, force=True) is True
    assert not Path(ws.isolated_workspace_path).exists()


# ---------------------------------------------------------------------------
# 3. Patch Generation & Strict Validation Tests
# ---------------------------------------------------------------------------

def test_patch_generator_synthesizes_unified_diff():
    """RepairPatchGenerator generates unified diff from RepairPlan."""
    plan = _build_dummy_plan()
    gen = RepairPatchGenerator()
    patch = gen.generate_patch(plan)

    assert patch.repair_plan_id == plan.plan_id
    assert "--- a/src/calculator.py" in patch.unified_diff
    assert "+++ b/src/calculator.py" in patch.unified_diff
    assert "@@ " in patch.unified_diff
    assert patch.patch_digest == compute_patch_digest(patch.unified_diff)
    assert patch.added_lines > 0


def test_patch_validator_valid_and_invalid():
    """RepairPatchValidator accepts valid diff and rejects malformed/scope-violating diffs."""
    plan = _build_dummy_plan()
    gen = RepairPatchGenerator()
    patch = gen.generate_patch(plan)
    val = RepairPatchValidator()

    # Valid patch
    res = val.validate_patch(
        unified_diff=patch.unified_diff,
        allowed_paths=["src/calculator.py"],
        expected_digest=patch.patch_digest,
    )
    assert res.is_valid is True
    assert res.status == PatchValidationStatus.VALID

    # Scope violation
    res_scope = val.validate_patch(
        unified_diff=patch.unified_diff,
        allowed_paths=["src/other.py"],
    )
    assert res_scope.is_valid is False
    assert res_scope.status == PatchValidationStatus.SCOPE_VIOLATION

    # Traversal violation
    traversal_diff = "--- a/../../secret.py\n+++ b/../../secret.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    res_trav = val.validate_patch(
        unified_diff=traversal_diff,
        allowed_paths=["../../secret.py"],
    )
    assert res_trav.is_valid is False
    assert res_trav.status == PatchValidationStatus.PROTECTED_PATH_VIOLATION

    # Oversized diff
    res_over = val.validate_patch(
        unified_diff=patch.unified_diff,
        allowed_paths=["src/calculator.py"],
        max_bytes=10,
    )
    assert res_over.is_valid is False
    assert res_over.status == PatchValidationStatus.OVERSIZED


# ---------------------------------------------------------------------------
# 4. Pure In-Memory Patch Application & Automatic Reversion Tests
# ---------------------------------------------------------------------------

def test_patch_application_clean(sample_repo: Path, tmp_path: Path):
    """Patch is applied cleanly to isolated workspace using pure Python logic."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")

    diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -4,2 +4,4 @@\n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        raise ValueError('division by zero')\n"
        "     return a / b\n"
    )
    patch = RepairPatch(
        patch_id="pat_01",
        repair_plan_id="p1",
        strategy_id="s1",
        unified_diff=diff,
        affected_files=["src/calculator.py"],
        patch_digest=compute_patch_digest(diff),
    )

    applier = ControlledPatchApplier()
    outcome = applier.apply_patch(
        workspace_path=Path(ws.isolated_workspace_path),
        patch=patch,
        allowed_files=["src/calculator.py"],
    )

    assert outcome.success is True
    assert "src/calculator.py" in outcome.modified_files
    applied_content = (Path(ws.isolated_workspace_path) / "src" / "calculator.py").read_text()
    assert "raise ValueError('division by zero')" in applied_content

    # Original repo still untouched
    orig_content = (sample_repo / "src" / "calculator.py").read_text()
    assert "raise ValueError" not in orig_content

    mgr.cleanup_workspace(ws.workspace_id)


def test_patch_application_failure_reverts_backups(sample_repo: Path, tmp_path: Path):
    """Patch with mismatched context fails and immediately reverts file in-memory."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")

    before_content = (Path(ws.isolated_workspace_path) / "src" / "calculator.py").read_text()

    # Diff expecting text that does not exist in the file
    bad_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-nonexistent text here\n"
        "+new replacement text\n"
    )
    patch = RepairPatch(
        patch_id="pat_bad",
        repair_plan_id="p1",
        strategy_id="s1",
        unified_diff=bad_diff,
        affected_files=["src/calculator.py"],
        patch_digest=compute_patch_digest(bad_diff),
    )

    applier = ControlledPatchApplier()
    outcome = applier.apply_patch(
        workspace_path=Path(ws.isolated_workspace_path),
        patch=patch,
        allowed_files=["src/calculator.py"],
    )

    assert outcome.success is False
    after_content = (Path(ws.isolated_workspace_path) / "src" / "calculator.py").read_text()
    # Content was reverted to exact pre-application state
    assert after_content == before_content

    mgr.cleanup_workspace(ws.workspace_id)


# ---------------------------------------------------------------------------
# 5. Rollback & Integrity Verification Tests
# ---------------------------------------------------------------------------

def test_rollback_manager_restores_baseline(sample_repo: Path, tmp_path: Path):
    """Rollback manager restores workspace to match baseline tree digest."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")

    baseline_digest = ws.baseline_tree_digest
    calc_path = Path(ws.isolated_workspace_path) / "src" / "calculator.py"
    calc_path.write_text("corrupted content")
    assert compute_tree_digest(Path(ws.isolated_workspace_path)) != baseline_digest

    rollback_mgr = WorkspaceRollbackManager()
    status, restored, post_digest = rollback_mgr.rollback(
        workspace=ws,
        modified_files=["src/calculator.py"],
    )

    assert status == RollbackStatus.SUCCEEDED
    assert "src/calculator.py" in restored
    assert post_digest == baseline_digest
    assert ws.workspace_status == WorkspaceStatus.READY

    mgr.cleanup_workspace(ws.workspace_id)


def test_integrity_checker_detects_unauthorized_modification(sample_repo: Path, tmp_path: Path):
    """Integrity checker catches modification outside approved scope."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    ws = mgr.allocate_workspace(allowed_paths=["src/calculator.py"], plan_id="p1")

    # Tamper with an unapproved file
    test_path = Path(ws.isolated_workspace_path) / "tests" / "test_calculator.py"
    test_path.write_text("tampered test")

    checker = WorkspaceIntegrityChecker()
    report = checker.verify_integrity(ws, allowed_paths=["src/calculator.py"])

    assert report.is_valid is False
    assert "tests/test_calculator.py" in report.unexpected_files
    assert any("Unauthorized file modified" in v for v in report.violations)

    mgr.cleanup_workspace(ws.workspace_id)


# ---------------------------------------------------------------------------
# 6. Audit Events & Secret Redaction Tests
# ---------------------------------------------------------------------------

def test_audit_event_recorder_and_redaction():
    """Event recorder redacts secrets and enforces sequential ordering."""
    recorder = RepairEventRecorder()
    ev = recorder.record_event(
        execution_id="rex_001",
        event_type=RepairEventType.APPROVAL_VERIFIED,
        action_description="Verified with token sk-abcdef1234567890abcdef123456",
    )

    assert ev.execution_id == "rex_001"
    assert "sk-" not in ev.action_description or "REDACTED" in ev.action_description
    assert ev.event_id == "rev_rex_001_0001"

    events = recorder.get_events("rex_001")
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 7. ControlledRepairExecutor End-to-End Orchestration Tests
# ---------------------------------------------------------------------------

def test_executor_end_to_end_success(sample_repo: Path, tmp_path: Path):
    """Executor runs complete lifecycle: approval -> workspace -> patch -> integrity -> APPLIED."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    executor = ControlledRepairExecutor(repo_path=sample_repo, workspace_manager=mgr)

    plan = _build_dummy_plan()
    approval = _build_dummy_approval(plan)

    diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -4,2 +4,4 @@\n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        raise ValueError('division by zero')\n"
        "     return a / b\n"
    )
    patch = RepairPatch(
        patch_id="pat_test",
        repair_plan_id=plan.plan_id,
        strategy_id=plan.selected_strategy_id,
        unified_diff=diff,
        affected_files=["src/calculator.py"],
        patch_digest=compute_patch_digest(diff),
    )

    result = executor.execute_repair(
        plan=plan,
        approval=approval,
        custom_patch=patch,
    )

    assert result.execution_status == ExecutionStatus.APPLIED
    assert result.verification_pending is True
    assert "src/calculator.py" in result.modified_files
    assert result.rollback_status == RollbackStatus.NOT_TRIGGERED
    assert result.failure_classification == FailureClassification.NONE
    assert len(result.execution_events) >= 5

    # Target repo was not modified
    orig_content = (sample_repo / "src" / "calculator.py").read_text()
    assert "raise ValueError" not in orig_content


def test_executor_blocked_on_unauthorized_execution(sample_repo: Path, tmp_path: Path):
    """Executor blocks execution if approval is expired."""
    ws_base = tmp_path / "workspaces"
    mgr = RepairWorkspaceManager(original_repo_path=sample_repo, base_workspaces_dir=ws_base)
    executor = ControlledRepairExecutor(repo_path=sample_repo, workspace_manager=mgr)

    plan = _build_dummy_plan()
    expired_approval = _build_dummy_approval(plan, expired=True)

    result = executor.execute_repair(plan=plan, approval=expired_approval)

    assert result.execution_status == ExecutionStatus.BLOCKED
    assert result.failure_classification == FailureClassification.APPROVAL_EXPIRED
    assert result.rollback_status == RollbackStatus.NOT_TRIGGERED


# ---------------------------------------------------------------------------
# 8. ToolRegistry Integration Tests
# ---------------------------------------------------------------------------

def test_tool_registry_repair_execution_tools(sample_repo: Path):
    """All 8 repair execution tools are registered and executable via ToolRegistry."""
    registry = ToolRegistry(workspace_path=sample_repo)

    expected_tools = [
        "prepare_repair_workspace",
        "validate_repair_approval",
        "generate_repair_patch",
        "validate_repair_patch",
        "apply_approved_repair",
        "inspect_repair_execution",
        "rollback_repair_workspace",
        "get_repair_execution_history",
    ]

    tool_names = [td.name for td in registry.get_definitions()]
    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Tool '{tool_name}' missing from ToolRegistry"

    # Test tool invocation: prepare_repair_workspace
    prep_res = registry.execute_tool(
        "prepare_repair_workspace",
        {
            "repository_path": str(sample_repo),
            "plan_id": "plan_001",
            "allowed_file_paths": ["src/calculator.py"],
        },
    )
    prep_data = json.loads(prep_res)
    assert "workspace_id" in prep_data
    ws_id = prep_data["workspace_id"]

    # Test tool invocation: rollback_repair_workspace
    rb_res = registry.execute_tool(
        "rollback_repair_workspace",
        {
            "workspace_id": ws_id,
        },
    )
    rb_data = json.loads(rb_res)
    assert rb_data["rollback_status"] == "succeeded"


# ---------------------------------------------------------------------------
# 9. Adversarial Security & Correctness Audit Regression Tests
# ---------------------------------------------------------------------------

def test_adversarial_rollback_immune_to_original_repo_mutation(tmp_path: Path):
    """Rollback restores pristine baseline even when the original repository is modified after allocation."""
    repo_dir = tmp_path / "orig_repo"
    repo_dir.mkdir()
    target_file = repo_dir / "service.py"
    target_file.write_text("def handle():\n    return 'original_baseline'\n")

    ws_dir = tmp_path / "workspaces"
    ws_mgr = RepairWorkspaceManager(original_repo_path=repo_dir, base_workspaces_dir=ws_dir)
    ws = ws_mgr.allocate_workspace(allowed_paths=["service.py"], plan_id="p1")

    baseline_digest = ws.baseline_tree_digest
    assert ws.baseline_snapshot_path is not None
    assert Path(ws.baseline_snapshot_path).exists()

    # Adversarial mutation: Modify original repo after workspace allocation
    target_file.write_text("def handle():\n    # UNEXPECTED EXTERNAL MUTATION\n    return 'tampered_repo'\n")

    # Apply patch in workspace
    patch_diff = """--- a/service.py
+++ b/service.py
@@ -1,2 +1,2 @@
 def handle():
-    return 'original_baseline'
+    return 'patched_workspace'
"""
    applier = ControlledPatchApplier()
    outcome = applier.apply_patch(Path(ws.isolated_workspace_path), unified_diff=patch_diff, allowed_files=["service.py"])
    assert outcome.success is True

    # Trigger rollback without in-memory backups (simulating tool or crash recovery)
    rb_mgr = WorkspaceRollbackManager()
    status, restored, post_digest = rb_mgr.rollback(ws, modified_files=["service.py"], backups=None)

    assert status == RollbackStatus.SUCCEEDED
    assert post_digest == baseline_digest
    ws_content = (Path(ws.isolated_workspace_path) / "service.py").read_text()
    assert "original_baseline" in ws_content
    assert "tampered_repo" not in ws_content
    assert ws.workspace_status == WorkspaceStatus.READY


def test_adversarial_integrity_check_immune_to_original_repo_mutation(tmp_path: Path):
    """Integrity check remains valid on untouched workspace even if original repo changes."""
    repo_dir = tmp_path / "orig_repo"
    repo_dir.mkdir()
    (repo_dir / "mod.py").write_text("v1 = 1\n")

    ws_dir = tmp_path / "workspaces"
    ws_mgr = RepairWorkspaceManager(original_repo_path=repo_dir, base_workspaces_dir=ws_dir)
    ws = ws_mgr.allocate_workspace(allowed_paths=["mod.py"], plan_id="p1")

    # Adversarially modify original repo and add files
    (repo_dir / "mod.py").write_text("v1 = 9999\n")
    (repo_dir / "unrelated_new.py").write_text("secret = True\n")

    # Check integrity of workspace (which was not modified)
    checker = WorkspaceIntegrityChecker()
    report = checker.verify_integrity(ws, allowed_paths=["mod.py"])

    assert report.is_valid is True
    assert report.violations == []
    assert report.modified_files == []
    assert report.unexpected_files == []


def test_adversarial_symlink_escape_rejected_during_allocation(tmp_path: Path):
    """Escaping symlinks pointing outside the repository are never copied into the workspace."""
    repo_dir = tmp_path / "orig_repo"
    repo_dir.mkdir()
    (repo_dir / "safe.py").write_text("x = 1\n")

    outside_secret = tmp_path / "secret.env"
    outside_secret.write_text("PRIVATE_KEY=supersecret\n")

    # Create an escaping symlink pointing outside repo
    try:
        (repo_dir / "escaped_link").symlink_to(outside_secret)
    except OSError:
        pytest.skip("Symlinks not supported on this filesystem")

    ws_dir = tmp_path / "workspaces"
    ws_mgr = RepairWorkspaceManager(original_repo_path=repo_dir, base_workspaces_dir=ws_dir)
    ws = ws_mgr.allocate_workspace(allowed_paths=["safe.py"], plan_id="p1")

    ws_root = Path(ws.isolated_workspace_path)
    assert not (ws_root / "escaped_link").exists()
    assert (ws_root / "safe.py").exists()


def test_adversarial_patch_bad_hunk_counts_rejected():
    """Hunk header line counts that disagree with actual line counts are rejected fail-closed."""
    validator = RepairPatchValidator()
    # Header says 5 lines old, but diff only provides 3 old lines
    bad_diff = """--- a/foo.py
+++ b/foo.py
@@ -1,5 +1,2 @@
 line1
-line2
+newline2
 line3
"""
    res = validator.validate_patch(bad_diff, allowed_paths=["foo.py"])
    assert res.is_valid is False
    assert res.status == PatchValidationStatus.INVALID_SYNTAX
    assert any("line count mismatch" in err for err in res.errors)


def test_adversarial_patch_overlapping_hunks_rejected():
    """Overlapping hunks targeting the same line ranges are rejected fail-closed."""
    validator = RepairPatchValidator()
    overlap_diff = """--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 line1
-line2
+mod2
 line3
@@ -2,3 +2,3 @@
-line2
-line3
+other2
+other3
 line4
"""
    res = validator.validate_patch(overlap_diff, allowed_paths=["foo.py"])
    assert res.is_valid is False
    assert res.status == PatchValidationStatus.INVALID_SYNTAX
    assert any("Overlapping hunks" in err for err in res.errors)


def test_adversarial_patch_reordered_hunks_rejected():
    """Reordered hunks out of line number order are rejected fail-closed."""
    validator = RepairPatchValidator()
    reordered_diff = """--- a/foo.py
+++ b/foo.py
@@ -10,1 +10,1 @@
-line10
+mod10
@@ -2,1 +2,1 @@
-line2
+mod2
"""
    res = validator.validate_patch(reordered_diff, allowed_paths=["foo.py"])
    assert res.is_valid is False
    assert res.status == PatchValidationStatus.INVALID_SYNTAX
    assert any("Reordered hunks" in err for err in res.errors)


def test_adversarial_patch_empty_hunk_rejected():
    """Empty hunks with zero line modifications are rejected fail-closed."""
    validator = RepairPatchValidator()
    empty_diff = """--- a/foo.py
+++ b/foo.py
@@ -1,0 +1,0 @@
"""
    res = validator.validate_patch(empty_diff, allowed_paths=["foo.py"])
    assert res.is_valid is False
    assert res.status == PatchValidationStatus.INVALID_SYNTAX
    assert any("Empty hunk" in err for err in res.errors)


def test_adversarial_patch_duplicate_file_rejected():
    """Multiple diff sections targeting the same file are rejected fail-closed."""
    validator = RepairPatchValidator()
    dup_diff = """--- a/foo.py
+++ b/foo.py
@@ -1,1 +1,1 @@
-a
+b
--- a/foo.py
+++ b/foo.py
@@ -3,1 +3,1 @@
-c
+d
"""
    res = validator.validate_patch(dup_diff, allowed_paths=["foo.py"])
    assert res.is_valid is False
    assert res.status == PatchValidationStatus.INVALID_SYNTAX
    assert any("Duplicate file entry" in err for err in res.errors)


def test_adversarial_patch_renames_and_git_modes_rejected():
    """File rename diffs and git file mode changes are rejected fail-closed."""
    validator = RepairPatchValidator()

    # Rename
    rename_diff = """--- a/foo.py
+++ b/bar.py
@@ -1,1 +1,1 @@
-a
+b
"""
    res1 = validator.validate_patch(rename_diff, allowed_paths=["foo.py", "bar.py"])
    assert res1.is_valid is False
    assert any("File rename" in err for err in res1.errors)

    # Mode change
    mode_diff = """old mode 100644
new mode 100755
--- a/foo.py
+++ b/foo.py
@@ -1,1 +1,1 @@
-a
+b
"""
    res2 = validator.validate_patch(mode_diff, allowed_paths=["foo.py"])
    assert res2.is_valid is False
    assert any("Forbidden git metadata" in err for err in res2.errors)


def test_adversarial_approval_replay_rejected(sample_repo: Path):
    """An approved RepairApproval is strictly single-use; subsequent replay attempts are blocked."""
    plan = _build_dummy_plan()
    approval = _build_dummy_approval(plan)

    ws_mgr = RepairWorkspaceManager(original_repo_path=sample_repo)
    executor = ControlledRepairExecutor(repo_path=sample_repo, workspace_manager=ws_mgr)

    diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -4,2 +4,4 @@\n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        raise ValueError('division by zero')\n"
        "     return a / b\n"
    )
    patch = RepairPatch(
        patch_id="pat_replay",
        repair_plan_id=plan.plan_id,
        strategy_id=plan.selected_strategy_id,
        unified_diff=diff,
        patch_digest=compute_patch_digest(diff),
        affected_files=["src/calculator.py"],
        validation_status=PatchValidationStatus.VALID,
    )

    # First execution: succeeds
    res1 = executor.execute_repair(plan=plan, approval=approval, custom_patch=patch)
    assert res1.execution_status == ExecutionStatus.APPLIED
    assert executor.is_approval_consumed(approval.approval_id) is True

    # Second execution using identical approval: BLOCKED as replay attempt
    res2 = executor.execute_repair(plan=plan, approval=approval, custom_patch=patch)
    assert res2.execution_status == ExecutionStatus.BLOCKED
    assert res2.failure_classification == FailureClassification.UNAUTHORIZED_EXECUTION
    assert "already been consumed" in res2.diff_summary


def test_adversarial_approval_revocation_blocked(sample_repo: Path):
    """Revoking an approval prevents execution even if the approval object states APPROVED."""
    plan = _build_dummy_plan()
    approval = _build_dummy_approval(plan)

    ws_mgr = RepairWorkspaceManager(original_repo_path=sample_repo)
    executor = ControlledRepairExecutor(repo_path=sample_repo, workspace_manager=ws_mgr)

    # Explicitly revoke approval before execution
    executor.revoke_approval(approval.approval_id, reason="Security vulnerability discovered")

    res = executor.execute_repair(plan=plan, approval=approval)
    assert res.execution_status == ExecutionStatus.BLOCKED
    assert res.failure_classification == FailureClassification.APPROVAL_REVOKED
    assert "has been revoked" in res.diff_summary


def test_adversarial_approval_plan_description_change_invalidates_digest():
    """Modifying change description or rationale alters canonical plan digest."""
    chg1 = RequiredChange(
        file_path="src/calculator.py",
        start_line=10,
        end_line=15,
        change_type="modify_condition",
        description="Original description",
        rationale="Original rationale",
    )
    plan1 = _build_dummy_plan(required_changes=[chg1])
    d1 = compute_plan_digest(plan1)

    chg2 = RequiredChange(
        file_path="src/calculator.py",
        start_line=10,
        end_line=15,
        change_type="modify_condition",
        description="Tampered description",
        rationale="Original rationale",
    )
    plan2 = _build_dummy_plan(required_changes=[chg2])
    d2 = compute_plan_digest(plan2)

    assert d1 != d2


def test_adversarial_event_hash_chain_tamper_detection():
    """Audit events form a cryptographic hash chain; tampering is immediately detected."""
    recorder = RepairEventRecorder()
    exec_id = "rex_audit_01"

    ev1 = recorder.record_event(exec_id, RepairEventType.WORKSPACE_PREPARED, "Workspace allocated")
    ev2 = recorder.record_event(exec_id, RepairEventType.APPROVAL_VERIFIED, "Approval verified")
    ev3 = recorder.record_event(exec_id, RepairEventType.PATCH_APPLICATION_SUCCEEDED, "Patch applied")

    assert ev1.sequence_number == 1
    assert ev2.sequence_number == 2
    assert ev3.sequence_number == 3

    assert ev2.prev_event_hash == ev1.event_hash
    assert ev3.prev_event_hash == ev2.event_hash

    is_valid, err = recorder.verify_event_chain(exec_id)
    assert is_valid is True
    assert err is None

    # Adversarial tampering: replace an event in recorder's internal list
    tampered_ev = RepairEvent(
        event_id=ev2.event_id,
        execution_id=ev2.execution_id,
        sequence_number=2,
        prev_event_hash=ev1.event_hash,
        event_hash="forged_hash",
        event_type=RepairEventType.APPROVAL_VERIFIED,
        timestamp=ev2.timestamp,
        actor=ev2.actor,
        action_description="Tampered description",
        related_evidence=[],
        outcome="success",
    )
    recorder._events_by_execution[exec_id][1] = tampered_ev

    is_valid_after, err_after = recorder.verify_event_chain(exec_id)
    assert is_valid_after is False
    assert "event_hash tampered" in err_after


def test_adversarial_crlf_and_no_newline_preservation(tmp_path: Path):
    """In-memory patch applier preserves CRLF endings and no-newline EOF markers."""
    ws = tmp_path / "ws"
    ws.mkdir()

    # 1. CRLF file
    crlf_file = ws / "crlf.py"
    crlf_file.write_bytes(b"line1\r\nline2\r\n")

    crlf_diff = """--- a/crlf.py
+++ b/crlf.py
@@ -1,2 +1,2 @@
 line1
-line2
+line2_patched
"""
    applier = ControlledPatchApplier()
    res1 = applier.apply_patch(ws, unified_diff=crlf_diff, allowed_files=["crlf.py"])
    assert res1.success is True
    assert crlf_file.read_bytes() == b"line1\r\nline2_patched\r\n"

    # 2. No newline at EOF
    no_nl_file = ws / "no_nl.py"
    no_nl_file.write_bytes(b"line1\nline2")

    no_nl_diff = """--- a/no_nl.py
+++ b/no_nl.py
@@ -1,2 +1,2 @@
 line1
-line2
\\ No newline at end of file
+line2_patched
\\ No newline at end of file
"""
    res2 = applier.apply_patch(ws, unified_diff=no_nl_diff, allowed_files=["no_nl.py"])
    assert res2.success is True
    assert no_nl_file.read_bytes() == b"line1\nline2_patched"
