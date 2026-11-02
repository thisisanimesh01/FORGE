"""Unit tests for Phase 8, Milestone 3: Evidence-Grounded Candidate Patch Generation.

Verifies:
1. Repair request and evidence validation (finding existence, state, provenance, workspace containment).
2. Evidence-grounded repair planning with typed facts vs hypotheses.
3. Candidate patch generation (minimal edits, Ollama local / provider integration, zero cloud fallback).
4. Strict patch parsing and validation (headers, hunk markers, context matching, AST, semantic invariants, budgets).
5. Safe in-workspace patch application with rollback on failure and target repository immutability.
6. Patch artifact persistence with SHA-256 verification and immutable audit event ledger.
7. Repair state machine progression (CREATED -> ... -> PATCH_VALIDATING, no premature VERIFIED).
8. End-to-end controlled repair demonstration.
"""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from forge.analysis.evidence import create_evidence
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMResponse, MessageRole
from forge.repair.execution.applier import SafePatchApplier
from forge.repair.execution.coordinator import RepairExecutionCoordinator
from forge.repair.execution.failures import (
    InsufficientEvidenceError,
    PatchApplicationError,
    PatchGenerationError,
    PatchValidationError,
    RepairPolicyViolationError,
    RepairRequestValidationError,
    ScopeContainmentViolationError,
    StaleBaseRevisionError,
    WorkspaceIntegrityViolationError,
)
from forge.repair.execution.generator import RepairPatchGenerator
from forge.repair.execution.models import (
    FailureCategory,
    FailureClassification,
    PatchArtifact,
    RepairActor,
    RepairExecutionPlan,
    RepairExecutionState,
    RepairRequest,
    ValidationResult,
)
from forge.repair.execution.planner import RepairExecutionPlanner
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.validator import PatchValidator, RepairRequestValidator
from forge.repair.execution.workspace import RepairWorkspace
from forge.schemas import EvidenceRecord, EvidenceType, Finding, FindingLifecycleState, FindingSeverity
from forge.storage.database import get_db_connection, init_database
from forge.storage.store import ForgeStore


# -----------------------------------------------------------------------------
# Fixtures & Helpers
# -----------------------------------------------------------------------------

@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_forge_m3.db"
    init_database(db_file)
    return db_file


@pytest.fixture
def mock_target_repo(tmp_path: Path) -> Path:
    """Create a minimal real git repository fixture with a bug for repair."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()

    # Create target file with a simple defect: off-by-one or missing boundary check
    code = (
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    # Defect: rate can be negative or > 1.0\n"
        "    if rate > 1.0:\n"
        "        raise ValueError('Rate too high')\n"
        "    return price * (1.0 - rate)\n"
    )
    (src_dir / "calculator.py").write_text(code, encoding="utf-8")
    (repo_dir / "README.md").write_text("# Target Repo\n", encoding="utf-8")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.local"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)

    return repo_dir


def _get_head(repo_dir: Path) -> str:
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True)
    return res.stdout.strip()


@pytest.fixture
def seeded_environment(test_db_path: Path, mock_target_repo: Path, tmp_path: Path):
    """Seed SQLite database and prepare an isolated workspace."""
    forge_store = ForgeStore(db_path=test_db_path)
    head_sha = _get_head(mock_target_repo)

    run_id = forge_store.create_run(
        target=str(mock_target_repo),
        run_id="run_m3_seed",
    )

    finding = forge_store.create_finding(
        run_id=run_id,
        title="Unchecked negative discount rate",
        description="calculate_discount does not validate that rate >= 0.0",
        severity=FindingSeverity.HIGH,
        finding_id="find_m3_001",
        affected_files=["src/calculator.py"],
    )

    # Transition finding to REPRODUCED so it is eligible
    ev_repro = create_evidence(
        run_id=run_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Defect reproduction output: negative rate produces unexpected negative discount",
        content="ValueError not raised on rate=-0.5",
        finding_id="find_m3_001",
        evidence_id="evi_m3_repro",
        file_path="src/calculator.py",
        start_line=3,
        end_line=5,
        details={
            "status": "reproduced",
            "commit_sha": head_sha,
            "repository_target": str(mock_target_repo),
        },
    )
    forge_store.attach_evidence(ev_repro)
    forge_store.transition_finding("find_m3_001", FindingLifecycleState.REPRODUCED, "evi_m3_repro")

    # Add source excerpt evidence
    ev_source = create_evidence(
        run_id=run_id,
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Missing negative rate guard",
        content="if rate > 1.0:\n    raise ValueError('Rate too high')",
        finding_id="find_m3_001",
        evidence_id="evi_m3_source",
        file_path="src/calculator.py",
        start_line=3,
        end_line=5,
        details={
            "commit_sha": head_sha,
            "repository_target": str(mock_target_repo),
        },
    )
    forge_store.attach_evidence(ev_source)

    # Create workspace
    workspace = RepairWorkspace(
        target_repo_path=mock_target_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    workspace.prepare()

    # Create RepairRequest
    repair_req = RepairRequest(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        run_id=run_id,
        target_repo=str(mock_target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_m3_repro", "evi_m3_source"],
        allowed_files=["src/calculator.py"],
        workspace_id=workspace.workspace_id,
    )

    exec_store = RepairExecutionStore(db_path=test_db_path)
    exec_store.save_repair_request(repair_req)

    return {
        "db_path": test_db_path,
        "forge_store": forge_store,
        "exec_store": exec_store,
        "target_repo": mock_target_repo,
        "head_sha": head_sha,
        "workspace": workspace,
        "request": repair_req,
        "finding": finding,
    }


# -----------------------------------------------------------------------------
# 1. Request and Evidence Validation Tests
# -----------------------------------------------------------------------------

def test_request_validation_succeeds_on_valid_inputs(seeded_environment):
    env = seeded_environment
    validator = RepairRequestValidator(store=env["forge_store"])
    finding, evidence = validator.validate_request(env["request"], env["workspace"])

    assert finding.finding_id == "find_m3_001"
    assert len(evidence) == 2
    assert evidence[0].evidence_id == "evi_m3_repro"
    env["workspace"].cleanup()


def test_request_validation_rejects_nonexistent_finding(seeded_environment):
    env = seeded_environment
    validator = RepairRequestValidator(store=env["forge_store"])
    bad_req = env["request"].model_copy(update={"finding_id": "find_nonexistent"})

    with pytest.raises(RepairRequestValidationError, match="does not exist"):
        validator.validate_request(bad_req, env["workspace"])
    env["workspace"].cleanup()


def test_request_validation_rejects_ineligible_finding_state(seeded_environment):
    env = seeded_environment
    # Manually update finding to FIXED (terminal/ineligible for repair generation)
    conn = get_db_connection(env["db_path"])
    try:
        conn.execute("UPDATE findings SET lifecycle_state = 'fixed' WHERE finding_id = 'find_m3_001'")
        conn.commit()
    finally:
        conn.close()

    validator = RepairRequestValidator(store=env["forge_store"])
    with pytest.raises(RepairRequestValidationError, match="not eligible"):
        validator.validate_request(env["request"], env["workspace"])
    env["workspace"].cleanup()


def test_request_validation_rejects_missing_evidence(seeded_environment):
    env = seeded_environment
    validator = RepairRequestValidator(store=env["forge_store"])
    bad_req = env["request"].model_copy(update={"evidence_ids": ["evi_missing_123"]})

    with pytest.raises(InsufficientEvidenceError, match="does not exist in store"):
        validator.validate_request(bad_req, env["workspace"])
    env["workspace"].cleanup()


def test_request_validation_rejects_stale_commit_in_evidence(seeded_environment):
    env = seeded_environment
    # Attach evidence with a different commit
    ev_stale = create_evidence(
        run_id=env["request"].run_id,
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Old excerpt",
        content="pass",
        finding_id="find_m3_001",
        evidence_id="evi_stale_commit",
        file_path="src/calculator.py",
        details={"commit_sha": "0000000000000000000000000000000000000000"},
    )
    env["forge_store"].attach_evidence(ev_stale)
    bad_req = env["request"].model_copy(update={"evidence_ids": ["evi_stale_commit"]})

    validator = RepairRequestValidator(store=env["forge_store"])
    with pytest.raises(StaleBaseRevisionError, match="does not match"):
        validator.validate_request(bad_req, env["workspace"])
    env["workspace"].cleanup()


def test_request_validation_rejects_missing_evidence_file_in_workspace(seeded_environment):
    env = seeded_environment
    ev_ghost = create_evidence(
        run_id=env["request"].run_id,
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Ghost excerpt",
        content="pass",
        finding_id="find_m3_001",
        evidence_id="evi_ghost_file",
        file_path="src/does_not_exist.py",
    )
    env["forge_store"].attach_evidence(ev_ghost)
    bad_req = env["request"].model_copy(update={"evidence_ids": ["evi_ghost_file"]})

    validator = RepairRequestValidator(store=env["forge_store"])
    with pytest.raises(RepairRequestValidationError, match="does not exist in workspace"):
        validator.validate_request(bad_req, env["workspace"])
    env["workspace"].cleanup()


def test_request_validation_rejects_workspace_id_mismatch(seeded_environment):
    env = seeded_environment
    validator = RepairRequestValidator(store=env["forge_store"])
    bad_req = env["request"].model_copy(update={"workspace_id": "ws_other_id"})

    with pytest.raises(RepairRequestValidationError, match="Workspace ID mismatch"):
        validator.validate_request(bad_req, env["workspace"])
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 2. Evidence-Grounded Repair Planning Tests
# -----------------------------------------------------------------------------

def test_planner_formulates_grounded_plan(seeded_environment):
    env = seeded_environment
    planner = RepairExecutionPlanner()
    policy = RepairExecutionPolicy()

    plan = planner.formulate_plan(
        request=env["request"],
        policy=policy,
        finding=env["finding"],
        evidence_records=env["forge_store"].list_evidence_for_finding("find_m3_001"),
        workspace=env["workspace"],
    )

    assert plan.target_file == "src/calculator.py"
    assert "Unchecked negative discount rate" in plan.root_cause
    assert "src/calculator.py" in plan.proposed_change
    assert plan.verification_command == "pytest"
    assert plan.confidence >= 0.8
    env["workspace"].cleanup()


def test_planner_rejects_unauthorized_target_file(seeded_environment):
    env = seeded_environment
    planner = RepairExecutionPlanner()
    policy = RepairExecutionPolicy()
    bad_req = env["request"].model_copy(update={"allowed_files": ["README.md"]})

    with pytest.raises(RepairPolicyViolationError):
        planner.formulate_plan(request=bad_req, policy=policy, workspace=env["workspace"])
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 3. Candidate Patch Generation Tests
# -----------------------------------------------------------------------------

def test_patch_generator_deterministic_replacement(seeded_environment):
    """Verify deterministic replacement builds a valid, bounded PatchArtifact."""
    env = seeded_environment
    planner = RepairExecutionPlanner()
    policy = RepairExecutionPolicy()
    plan = planner.formulate_plan(request=env["request"], policy=policy, workspace=env["workspace"])

    generator = RepairPatchGenerator()
    replacement = (
        "    if rate < 0.0 or rate > 1.0:\n"
        "        raise ValueError('Rate must be between 0.0 and 1.0')"
    )

    patch_art = generator.execute_repair(
        plan=plan,
        workspace=env["workspace"],
        policy=policy,
        replacement_content=replacement,
        evidence_records=env["forge_store"].list_evidence_for_finding("find_m3_001"),
    )

    assert patch_art.target_file == "src/calculator.py"
    assert patch_art.artifact_id.startswith("patch_")
    assert patch_art.lines_added > 0
    assert "if rate < 0.0 or rate > 1.0:" in patch_art.diff_content
    # Digest matches
    expected_digest = hashlib.sha256(patch_art.diff_content.encode("utf-8")).hexdigest()
    assert patch_art.content_hash == expected_digest

    env["workspace"].cleanup()


def test_patch_generator_with_mock_provider(seeded_environment):
    """Verify LLM provider JSON response parsing into a valid PatchArtifact."""
    env = seeded_environment
    planner = RepairExecutionPlanner()
    policy = RepairExecutionPolicy()
    plan = planner.formulate_plan(request=env["request"], policy=policy, workspace=env["workspace"])

    valid_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -3,3 +3,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    fake_json = (
        '{\n'
        '  "target_file": "src/calculator.py",\n'
        f'  "unified_diff": "{valid_diff.replace(chr(10), "\\n")}",\n'
        '  "explanation": "Add negative rate boundary check"\n'
        '}'
    )

    mock_provider = MagicMock()
    mock_provider.chat.return_value = LLMResponse(
        content=fake_json,
        finish_reason="stop",
        usage={"total_tokens": 120},
    )

    generator = RepairPatchGenerator(provider=mock_provider)
    patch_art = generator.execute_repair(
        plan=plan,
        workspace=env["workspace"],
        policy=policy,
        finding=env["finding"],
    )

    assert patch_art.target_file == "src/calculator.py"
    assert "if rate < 0.0 or rate > 1.0:" in patch_art.diff_content
    env["workspace"].cleanup()


def test_patch_generator_rejects_unauthorized_touched_file(seeded_environment):
    """Patch generator rejects diffs that attempt to modify unauthorized files."""
    env = seeded_environment
    planner = RepairExecutionPlanner()
    policy = RepairExecutionPolicy()
    plan = planner.formulate_plan(request=env["request"], policy=policy, workspace=env["workspace"])

    unauthorized_diff = (
        "--- a/src/other.py\n"
        "+++ b/src/other.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-# old\n"
        "+# hacked\n"
    )
    fake_json = (
        '{\n'
        '  "target_file": "src/other.py",\n'
        f'  "unified_diff": "{unauthorized_diff.replace(chr(10), "\\n")}"\n'
        '}'
    )

    mock_provider = MagicMock()
    mock_provider.chat.return_value = LLMResponse(content=fake_json, finish_reason="stop")

    generator = RepairPatchGenerator(provider=mock_provider)
    with pytest.raises(ScopeContainmentViolationError, match="unauthorized file"):
        generator.execute_repair(
            plan=plan,
            workspace=env["workspace"],
            policy=policy,
        )
    env["workspace"].cleanup()


def test_patch_generator_rejects_malformed_model_output(seeded_environment):
    """Patch generator raises PatchGenerationError if model returns non-diff content."""
    env = seeded_environment
    planner = RepairExecutionPlanner()
    policy = RepairExecutionPolicy()
    plan = planner.formulate_plan(request=env["request"], policy=policy, workspace=env["workspace"])

    mock_provider = MagicMock()
    mock_provider.chat.return_value = LLMResponse(
        content="I apologize, but I cannot modify this file.",
        finish_reason="stop",
    )

    generator = RepairPatchGenerator(provider=mock_provider)
    with pytest.raises(PatchGenerationError, match="did not contain a valid unified diff"):
        generator.execute_repair(plan=plan, workspace=env["workspace"], policy=policy)
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 4. Strict Patch Parsing and Validation Tests
# -----------------------------------------------------------------------------

def test_patch_validator_accepts_clean_patch(seeded_environment):
    env = seeded_environment
    valid_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -3,3 +3,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=valid_diff,
    )

    validator = PatchValidator()
    policy = RepairExecutionPolicy()
    res = validator.validate_patch(patch_art, env["request"], policy, env["workspace"])

    assert res.is_valid is True
    assert "content_hash_verified" in res.checks_passed
    assert "python_ast_valid" in res.checks_passed
    assert "semantic_invariants_preserved" in res.checks_passed
    env["workspace"].cleanup()


def test_patch_validator_rejects_path_traversal(seeded_environment):
    env = seeded_environment
    traversal_diff = (
        "--- a/src/../etc/passwd\n"
        "+++ b/src/../etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-root\n"
        "+hacked\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=traversal_diff,
    )

    validator = PatchValidator()
    policy = RepairExecutionPolicy()
    res = validator.validate_patch(patch_art, env["request"], policy, env["workspace"])

    assert res.is_valid is False
    assert any("traversal" in v for v in res.violations)
    env["workspace"].cleanup()


def test_patch_validator_rejects_protected_file_target(seeded_environment):
    env = seeded_environment
    protected_diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-# Target Repo\n"
        "+# Modified Repo\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="README.md",
        diff_content=protected_diff,
    )

    validator = PatchValidator()
    policy = RepairExecutionPolicy()
    res = validator.validate_patch(patch_art, env["request"], policy, env["workspace"])

    assert res.is_valid is False
    assert any("protected" in v.lower() or "not within declared" in v for v in res.violations)
    env["workspace"].cleanup()


def test_patch_validator_rejects_context_mismatch(seeded_environment):
    env = seeded_environment
    mismatched_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-    non_existent_function_call()\n"
        "+    valid_function_call()\n"
        "     return 0\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=mismatched_diff,
    )

    validator = PatchValidator()
    policy = RepairExecutionPolicy()
    res = validator.validate_patch(patch_art, env["request"], policy, env["workspace"])

    assert res.is_valid is False
    assert any("context mismatch" in v for v in res.violations)
    env["workspace"].cleanup()


def test_patch_validator_rejects_syntax_error_introduction(seeded_environment):
    env = seeded_environment
    bad_syntax_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -3,3 +3,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate >>> 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=bad_syntax_diff,
    )

    validator = PatchValidator()
    policy = RepairExecutionPolicy()
    res = validator.validate_patch(patch_art, env["request"], policy, env["workspace"])

    assert res.is_valid is False
    assert any("syntax error" in v for v in res.violations)
    env["workspace"].cleanup()


def test_patch_validator_enforces_budget_caps(seeded_environment):
    env = seeded_environment
    # Build oversized patch exceeding line limit
    huge_additions = "\n".join([f"+    # Extra line {i}" for i in range(150)])
    oversized_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -3,1 +3,151 @@\n"
        f"{huge_additions}\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=oversized_diff,
    )

    validator = PatchValidator()
    policy = RepairExecutionPolicy(max_lines_changed=100)
    res = validator.validate_patch(patch_art, env["request"], policy, env["workspace"])

    assert res.is_valid is False
    assert any("Total changed lines" in v for v in res.violations)
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 5. Safe In-Workspace Patch Application Tests
# -----------------------------------------------------------------------------

def test_safe_patch_applier_applies_cleanly(seeded_environment):
    env = seeded_environment
    valid_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -3,3 +3,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=valid_diff,
    )

    applier = SafePatchApplier()
    ok, err = applier.apply_patch(patch_art, env["workspace"], ["src/calculator.py"])

    assert ok is True
    assert err is None

    # Verify target file modified in workspace
    patched_code = (env["workspace"].workspace_path / "src" / "calculator.py").read_text()
    assert "if rate < 0.0 or rate > 1.0:" in patched_code

    # Verify target repository remains strictly untouched
    target_code = (env["target_repo"] / "src" / "calculator.py").read_text()
    assert "if rate < 0.0 or rate > 1.0:" not in target_code
    assert env["workspace"].check_target_immutability() is True

    env["workspace"].cleanup()


def test_safe_patch_applier_atomic_rollback_on_failure(seeded_environment):
    env = seeded_environment
    calc_path = env["workspace"].workspace_path / "src" / "calculator.py"
    original_code = calc_path.read_text()

    # Broken diff that will fail context matching
    broken_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -50,3 +50,3 @@\n"
        "-nonexistent\n"
        "+replacement\n"
    )
    patch_art = PatchArtifact.create(
        repair_id="rep_m3_001",
        finding_id="find_m3_001",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=broken_diff,
    )

    applier = SafePatchApplier()
    ok, err = applier.apply_patch(patch_art, env["workspace"], ["src/calculator.py"])

    assert ok is False
    assert "Failed to apply unified diff" in err
    # Workspace file must be restored untouched
    assert calc_path.read_text() == original_code

    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 6. Persistence & State Machine Progression Tests
# -----------------------------------------------------------------------------

def test_coordinator_end_to_end_candidate_generation(seeded_environment):
    """Full lifecycle: CREATED -> PLANNED -> VALIDATING -> READY -> EXECUTING -> PATCH_GENERATED -> PATCH_VALIDATING."""
    env = seeded_environment
    coordinator = RepairExecutionCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        db_path=env["db_path"],
    )

    replacement = (
        "    if rate < 0.0 or rate > 1.0:\n"
        "        raise ValueError('Rate must be between 0.0 and 1.0')"
    )

    req, patch_art, val_res = coordinator.execute_candidate_generation(
        request=env["request"],
        workspace=env["workspace"],
        replacement_content=replacement,
    )

    # State must be PATCH_VALIDATING (never prematurely VERIFIED)
    assert req.state == RepairExecutionState.PATCH_VALIDATING
    assert patch_art is not None
    assert val_res.is_valid is True

    # Check persistence in SQLite
    persisted_req = env["exec_store"].get_repair_request(env["request"].repair_id)
    assert persisted_req.state == RepairExecutionState.PATCH_VALIDATING
    assert persisted_req.patch_artifact_id == patch_art.artifact_id

    persisted_patch = env["exec_store"].get_patch_artifact(patch_art.artifact_id)
    assert persisted_patch is not None
    assert persisted_patch.content_hash == patch_art.content_hash

    # Digest verification
    expected_hash = hashlib.sha256(persisted_patch.diff_content.encode("utf-8")).hexdigest()
    assert persisted_patch.content_hash == expected_hash

    # Check plan persistence
    persisted_plan = env["exec_store"].get_repair_plan_for_repair(env["request"].repair_id)
    assert persisted_plan is not None
    assert persisted_plan.target_file == "src/calculator.py"

    # Check audit events trail
    events = env["exec_store"].get_events(env["request"].repair_id)
    event_states = [e.to_state.value for e in events]
    assert "created" in event_states
    assert "planned" in event_states
    assert "validating" in event_states
    assert "ready" in event_states
    assert "executing" in event_states
    assert "patch_generated" in event_states
    assert "patch_validating" in event_states

    # Guarantee target repository immutability
    assert env["workspace"].check_target_immutability() is True

    env["workspace"].cleanup()


def test_coordinator_transitions_to_failed_on_invalid_patch(seeded_environment):
    """When generated patch fails validation, coordinator transitions to FAILED state."""
    env = seeded_environment
    coordinator = RepairExecutionCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        db_path=env["db_path"],
    )

    # Replacement introducing Python syntax error
    bad_replacement = "    if rate < 0.0 or >>>:\n        pass"

    with pytest.raises(PatchValidationError):
        coordinator.execute_candidate_generation(
            request=env["request"],
            workspace=env["workspace"],
            replacement_content=bad_replacement,
        )

    # Verify state is FAILED in store
    persisted_req = env["exec_store"].get_repair_request(env["request"].repair_id)
    assert persisted_req.state == RepairExecutionState.FAILED
    assert persisted_req.failure is not None

    env["workspace"].cleanup()
