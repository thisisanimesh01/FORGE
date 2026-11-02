"""Phase 8 Milestone 4 Unit Test Suite — Independent Patch Validation & Scope Enforcement.

Verifies:
1. Cryptographic and provenance integrity (digest match, artifact identity, base commit,
   workspace identity, baseline manifest drift, cross-repair reuse).
2. Scope containment and boundary rules (authorized files, path traversal, absolute paths,
   protected paths, file creation/deletion, resource budget caps).
3. Adversarial diff and syntax checks (null bytes, control chars, binary diffs, duplicate/overlapping
   hunks, negative line offsets, file mode/rename tampering, secret introduction).
4. Python AST and semantic invariants (removed guards, weakened auth, removed exceptions/silencing,
   dangerous imports, dynamic execution, public API tampering, signature changes, side effects).
5. State transitions and persistence (ACCEPTED, REJECTED, INCONCLUSIVE, SQLite audit trail,
   coordinator failure handling, and strictly no premature VERIFIED state).
6. Controlled end-to-end demonstration.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import pytest

from forge.repair.execution.coordinator import RepairExecutionCoordinator
from forge.repair.execution.failures import (
    PatchAdversarialDefectError,
    PatchIntegrityError,
    PatchScopeViolationError,
    PatchSemanticInvariantError,
    PatchValidationError,
)
from forge.repair.execution.models import (
    FailureCategory,
    IndependentValidationResult,
    PatchArtifact,
    RepairExecutionState,
    RepairRequest,
    ValidationOutcome,
)
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.validator import (
    IndependentPatchValidator,
    PatchValidator,
)
from forge.repair.execution.workspace import RepairWorkspace
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_forge_m4.db"
    init_database(db_file)
    return db_file


@pytest.fixture
def mock_target_repo(tmp_path: Path) -> Path:
    """Create a minimal real git repository fixture with a bug for repair."""
    repo_dir = tmp_path / "mock_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    src_dir = repo_dir / "src"
    src_dir.mkdir()

    code = (
        "def calculate_discount(price: float, rate: float) -> float:\n"
        "    if rate > 1.0:\n"
        "        raise ValueError('Rate too high')\n"
        "    return price * (1.0 - rate)\n"
    )
    (src_dir / "calculator.py").write_text(code, encoding="utf-8")
    (repo_dir / "README.md").write_text("# Target Repo\n", encoding="utf-8")

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
        run_id="run_m4_seed",
    )

    finding = forge_store.create_finding(
        run_id=run_id,
        title="Unchecked negative discount rate",
        description="calculate_discount does not validate that rate >= 0.0",
        severity=FindingSeverity.HIGH,
        finding_id="find_m4_001",
        affected_files=["src/calculator.py"],
    )

    ev_repro = EvidenceRecord(
        evidence_id="evi_m4_repro",
        run_id=run_id,
        finding_id="find_m4_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Defect reproduction output: negative rate produces unexpected negative discount",
        content="ValueError not raised on rate=-0.5",
        file_path="src/calculator.py",
        start_line=2,
        end_line=4,
        details={"status": "reproduced", "commit_sha": head_sha, "repository_target": str(mock_target_repo)},
    )
    forge_store.attach_evidence(ev_repro)
    forge_store.transition_finding("find_m4_001", FindingLifecycleState.REPRODUCED, "evi_m4_repro")

    ev_source = EvidenceRecord(
        evidence_id="evi_m4_source",
        run_id=run_id,
        finding_id="find_m4_001",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Missing negative rate guard",
        content="if rate > 1.0:\n    raise ValueError('Rate too high')",
        file_path="src/calculator.py",
        start_line=2,
        end_line=4,
        details={"commit_sha": head_sha, "repository_target": str(mock_target_repo)},
    )
    forge_store.attach_evidence(ev_source)

    workspace = RepairWorkspace(
        target_repo_path=mock_target_repo,
        base_commit_sha=head_sha,
        scratch_root=tmp_path / "scratch",
    )
    workspace.prepare()

    repair_req = RepairRequest(
        repair_id="rep_m4_001",
        finding_id="find_m4_001",
        run_id=run_id,
        target_repo=str(mock_target_repo),
        base_commit_sha=head_sha,
        evidence_ids=["evi_m4_repro", "evi_m4_source"],
        allowed_files=["src/calculator.py"],
        workspace_id=workspace.workspace_id,
        state=RepairExecutionState.CREATED,
    )

    exec_store = RepairExecutionStore(db_path=test_db_path)
    exec_store.save_repair_request(repair_req)

    clean_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )

    clean_patch = PatchArtifact.create(
        repair_id="rep_m4_001",
        finding_id="find_m4_001",
        base_commit_sha=head_sha,
        target_file="src/calculator.py",
        diff_content=clean_diff,
    )
    exec_store.save_patch_artifact(clean_patch)

    return {
        "db_path": test_db_path,
        "repo_dir": mock_target_repo,
        "head_sha": head_sha,
        "forge_store": forge_store,
        "exec_store": exec_store,
        "workspace": workspace,
        "request": repair_req,
        "clean_diff": clean_diff,
        "clean_patch": clean_patch,
    }


# -----------------------------------------------------------------------------
# 1. Cryptographic and Provenance Integrity Tests
# -----------------------------------------------------------------------------

def test_integrity_clean_patch_accepted(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    result = validator.validate_independent(
        patch=env["clean_patch"],
        request=env["request"],
        workspace=env["workspace"],
        policy=policy,
    )

    assert result.is_valid is True
    assert result.overall_status == ValidationOutcome.ACCEPTED
    assert "content_hash_verified" in result.checks_passed
    assert "artifact_identity_verified" in result.checks_passed
    assert "base_commit_verified" in result.checks_passed
    assert "workspace_identity_verified" in result.checks_passed
    assert result.integrity_result["artifact_hash_match"] is True
    env["workspace"].cleanup()


def test_integrity_tampered_patch_content_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Create a patch with deliberately mismatched content_hash
    tampered_diff = env["clean_diff"] + " "
    fake_patch = PatchArtifact(
        artifact_id=env["clean_patch"].artifact_id,
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=tampered_diff,
        content_hash=env["clean_patch"].content_hash,  # Not matching tampered_diff
        lines_added=1,
        lines_deleted=1,
        hunks_count=1,
    )

    result = validator.validate_independent(fake_patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert result.overall_status == ValidationOutcome.REJECTED
    assert any("Content hash mismatch" in v for v in result.violations)
    env["workspace"].cleanup()


def test_integrity_artifact_identity_mismatch_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Artifact ID not matching prefix or sha
    fake_patch = PatchArtifact(
        artifact_id="patch_fakeartifactid",
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=env["clean_diff"],
        content_hash=env["clean_patch"].content_hash,
        lines_added=1,
        lines_deleted=1,
        hunks_count=1,
    )

    result = validator.validate_independent(fake_patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Artifact identity mismatch" in v for v in result.violations)
    env["workspace"].cleanup()


def test_integrity_cross_repair_reuse_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    patch_foreign_repair = PatchArtifact.create(
        repair_id="rep_m4_FOREIGN",
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=env["clean_diff"],
    )

    result = validator.validate_independent(patch_foreign_repair, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Cross-repair reuse" in v for v in result.violations)
    env["workspace"].cleanup()


def test_integrity_finding_mismatch_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    patch_foreign_finding = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id="find_m4_FOREIGN",
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=env["clean_diff"],
    )

    result = validator.validate_independent(patch_foreign_finding, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Finding association mismatch" in v for v in result.violations)
    env["workspace"].cleanup()


def test_integrity_base_commit_mismatch_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    patch_stale_commit = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha="a" * 40,
        target_file="src/calculator.py",
        diff_content=env["clean_diff"],
    )

    result = validator.validate_independent(patch_stale_commit, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Base commit mismatch" in v for v in result.violations)
    env["workspace"].cleanup()


def test_integrity_workspace_identity_mismatch_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Request specifying different workspace
    mismatched_req = env["request"].model_copy(update={"workspace_id": "ws_other_12345"})

    result = validator.validate_independent(env["clean_patch"], mismatched_req, env["workspace"], policy)
    assert result.is_valid is False
    assert any("Workspace identity mismatch" in v for v in result.violations)
    env["workspace"].cleanup()


def test_integrity_baseline_manifest_drift_detected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Tamper with workspace target file before validation
    target_disk = env["workspace"].workspace_path / "src" / "calculator.py"
    target_disk.write_text("# Drift injected into file\n" + target_disk.read_text(), encoding="utf-8")

    result = validator.validate_independent(env["clean_patch"], env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("baseline drift detected" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 2. Scope Containment & Safety Boundary Tests
# -----------------------------------------------------------------------------

def test_scope_unauthorized_file_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    unauthorized_diff = (
        "--- a/src/other.py\n"
        "+++ b/src/other.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/other.py",
        diff_content=unauthorized_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("not within declared allowed scope" in v for v in result.violations)
    env["workspace"].cleanup()


def test_scope_path_traversal_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    traversal_diff = (
        "--- a/../etc/passwd\n"
        "+++ b/../etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-root\n"
        "+evil\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="../etc/passwd",
        diff_content=traversal_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Path traversal" in v or "allowed scope" in v for v in result.violations)
    env["workspace"].cleanup()


def test_scope_absolute_path_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    abs_diff = (
        "--- /etc/passwd\n"
        "+++ /etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-root\n"
        "+evil\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="/etc/passwd",
        diff_content=abs_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Absolute paths forbidden" in v or "allowed scope" in v for v in result.violations)
    env["workspace"].cleanup()


def test_scope_file_creation_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    create_diff = (
        "--- /dev/null\n"
        "+++ b/src/calculator.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+# created\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=create_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("file creation forbidden" in v for v in result.violations)
    env["workspace"].cleanup()


def test_scope_file_deletion_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    delete_diff = (
        "--- a/src/calculator.py\n"
        "+++ /dev/null\n"
        "@@ -1,4 +0,0 @@\n"
        "-code\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=delete_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("file deletion forbidden" in v for v in result.violations)
    env["workspace"].cleanup()


def test_scope_budget_caps_enforced(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    # Tight policy with max 2 lines changed
    strict_policy = RepairExecutionPolicy(max_lines_changed=2)

    diff_large = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,4 @@\n"
        "-    if rate > 1.0:\n"
        "-        raise ValueError('Rate too high')\n"
        "+    if rate < 0.0:\n"
        "+        raise ValueError('Negative')\n"
        "+    if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=diff_large,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], strict_policy)
    assert result.is_valid is False
    assert any("exceeds policy max of 2" in v for v in result.violations)
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 3. Adversarial Diff & Syntax Checks
# -----------------------------------------------------------------------------

def test_adversarial_null_bytes_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    null_diff = env["clean_diff"].replace("Rate too high", "Rate\x00too high")
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=null_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("null bytes" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_control_characters_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    ctrl_diff = env["clean_diff"].replace("Rate too high", "Rate\x1b[31mtoo high")
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=ctrl_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("control character" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_binary_patch_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    binary_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "GIT binary patch\n"
        "literal 12\n"
        "zc${5u00001000\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=binary_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Binary patches are strictly forbidden" in v for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_duplicate_hunks_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    dup_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,2 +2,2 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate > 1.0:\n"
        "@@ -2,2 +2,2 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate > 1.0:\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=dup_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Duplicate hunk detected" in v for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_overlapping_hunks_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    overlap_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def calculate_discount(price: float, rate: float) -> float:\n"
        "-    if rate > 1.0:\n"
        "+    if rate > 1.0:\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=overlap_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Overlapping hunks detected" in v for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_negative_line_offset_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    bad_offset_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -0,3 +0,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate > 1.0:\n"
        "     return 0\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=bad_offset_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("invalid non-positive line offset" in v for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_file_mode_tampering_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    mode_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "new file mode 100755\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=mode_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("File mode modification forbidden" in v for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_file_rename_tampering_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    rename_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "rename from src/old_calculator.py\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if rate < 0.0 or rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=rename_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("File rename/copy operations forbidden" in v for v in result.violations)
    env["workspace"].cleanup()


def test_adversarial_secret_introduction_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    secret_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,4 @@\n"
        "+    API_KEY = 'sk-1234567890abcdef1234567890abcdef'\n"
        "     if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=secret_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Secret or credential detected" in v for v in result.violations)
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 4. Python AST and Semantic Invariants Tests
# -----------------------------------------------------------------------------

def test_semantic_removed_validation_guard_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Strips the rate > 1.0 check entirely
    bypass_guard_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,1 @@\n"
        "-    if rate > 1.0:\n"
        "-        raise ValueError('Rate too high')\n"
        "     return price * (1.0 - rate)\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=bypass_guard_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Semantic invariant violation" in v for v in result.violations)
    assert any("guard" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


def test_semantic_weakened_auth_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Replaces check with True
    weak_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,3 @@\n"
        "-    if rate > 1.0:\n"
        "+    if True:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=weak_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Weakened authorization" in v or "constant True" in v for v in result.violations)
    env["workspace"].cleanup()


def test_semantic_dangerous_import_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    danger_import_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+import subprocess\n"
        " def calculate_discount(price: float, rate: float) -> float:\n"
        "     if rate > 1.0:\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=danger_import_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("dangerous module" in v.lower() or "subprocess" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


def test_semantic_dynamic_execution_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    eval_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -2,3 +2,4 @@\n"
        "+    eval('price * 2')\n"
        "     if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=eval_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("Dynamic execution" in v or "eval" in v for v in result.violations)
    env["workspace"].cleanup()


def test_semantic_signature_change_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Removes 'rate' parameter from public function
    sig_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-def calculate_discount(price: float, rate: float) -> float:\n"
        "+def calculate_discount(price: float) -> float:\n"
        "     if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=sig_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("signature altered" in v.lower() or "parameter" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


def test_semantic_removed_annotation_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Drops return type annotation -> float
    annot_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-def calculate_discount(price: float, rate: float) -> float:\n"
        "+def calculate_discount(price: float, rate: float):\n"
        "     if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=annot_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("return type annotation" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


def test_semantic_module_level_side_effect_rejected(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    side_effect_diff = (
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+print('executing on import')\n"
        " def calculate_discount(price: float, rate: float) -> float:\n"
        "     if rate > 1.0:\n"
        "         raise ValueError('Rate too high')\n"
    )
    patch = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/calculator.py",
        diff_content=side_effect_diff,
    )

    result = validator.validate_independent(patch, env["request"], env["workspace"], policy)
    assert result.is_valid is False
    assert any("side-effect" in v.lower() for v in result.violations)
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 5. State Transitions, Storage Persistence, and Full Audit Integration
# -----------------------------------------------------------------------------

def test_independent_validation_persistence_and_retrieval(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()
    exec_store: RepairExecutionStore = env["exec_store"]

    result = validator.validate_independent(
        patch=env["clean_patch"],
        request=env["request"],
        workspace=env["workspace"],
        policy=policy,
    )
    assert result.is_valid is True

    # Persist in SQLite
    exec_store.save_validation_result(result)

    # Retrieve by ID
    persisted = exec_store.get_validation_result(result.validation_id)
    assert persisted is not None
    assert persisted.validation_id == result.validation_id
    assert persisted.patch_artifact_id == env["clean_patch"].artifact_id
    assert persisted.patch_sha256 == result.patch_sha256
    assert persisted.overall_status == ValidationOutcome.ACCEPTED
    assert persisted.is_valid is True
    assert persisted.scope_result["within_scope"] is True
    assert persisted.integrity_result["artifact_hash_match"] is True

    # Retrieve by patch artifact ID
    by_patch = exec_store.get_validation_result_for_patch(env["clean_patch"].artifact_id)
    assert by_patch is not None
    assert by_patch.validation_id == result.validation_id

    # List for repair
    val_list = exec_store.list_validation_results_for_repair(env["request"].repair_id)
    assert len(val_list) >= 1
    assert val_list[0].validation_id == result.validation_id

    # Check full execution record attaches validation
    full_rec = exec_store.get_full_execution_record(env["request"].repair_id)
    assert full_rec is not None
    assert full_rec.validation is not None
    assert full_rec.validation.is_valid is True

    env["workspace"].cleanup()


def test_validation_outcome_inconclusive_when_target_missing(seeded_environment):
    env = seeded_environment
    validator = IndependentPatchValidator()
    policy = RepairExecutionPolicy()

    # Request with nonexistent file target
    missing_file_diff = (
        "--- a/src/missing.py\n"
        "+++ b/src/missing.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-a = 1\n"
        "+a = 2\n"
    )
    req_missing = env["request"].model_copy(update={"allowed_files": ["src/missing.py"]})
    patch_missing = PatchArtifact.create(
        repair_id=env["request"].repair_id,
        finding_id=env["request"].finding_id,
        base_commit_sha=env["head_sha"],
        target_file="src/missing.py",
        diff_content=missing_file_diff,
    )

    result = validator.validate_independent(patch_missing, req_missing, env["workspace"], policy)
    assert result.is_valid is False
    assert result.overall_status == ValidationOutcome.INCONCLUSIVE
    env["workspace"].cleanup()


# -----------------------------------------------------------------------------
# 6. Controlled End-to-End Demonstration
# -----------------------------------------------------------------------------

def test_coordinator_end_to_end_demonstration(seeded_environment):
    """End-to-end demonstration: clean patch passes; at least 3 adversarial patches fail."""
    env = seeded_environment
    coordinator = RepairExecutionCoordinator(
        execution_store=env["exec_store"],
        forge_store=env["forge_store"],
        db_path=env["db_path"],
    )

    # 1. Clean valid patch accepted and applied
    valid_replacement = "    if rate < 0.0 or rate > 1.0:\n        raise ValueError('Rate too high')"
    updated_req, patch, val_res = coordinator.execute_candidate_generation(
        request=env["request"],
        workspace=env["workspace"],
        replacement_content=valid_replacement,
    )

    assert val_res.is_valid is True
    assert updated_req.state == RepairExecutionState.PATCH_VALIDATING
    # Guarantee no premature transition to VERIFIED
    assert updated_req.state != RepairExecutionState.VERIFIED

    # Check persistence of validation result
    stored_val = env["exec_store"].get_validation_result_for_patch(patch.artifact_id)
    assert stored_val is not None
    assert stored_val.overall_status == ValidationOutcome.ACCEPTED

    # 2. Adversarial Patch 1: Weakened Authorization (constant True)
    ws_adv1 = RepairWorkspace(
        target_repo_path=env["repo_dir"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["workspace"].workspace_path.parent / "scratch_adv1",
    )
    ws_adv1.prepare()
    adv1_replacement = "    if True:\n        raise ValueError('Rate too high')"
    req_adv1 = env["request"].model_copy(update={
        "repair_id": "rep_m4_adv1",
        "workspace_id": ws_adv1.workspace_id,
        "state": RepairExecutionState.CREATED,
    })
    env["exec_store"].save_repair_request(req_adv1)

    with pytest.raises((PatchSemanticInvariantError, PatchValidationError)):
        coordinator.execute_candidate_generation(
            request=req_adv1,
            workspace=ws_adv1,
            replacement_content=adv1_replacement,
        )
    persisted_adv1 = env["exec_store"].get_repair_request("rep_m4_adv1")
    assert persisted_adv1.state == RepairExecutionState.FAILED
    assert persisted_adv1.failure.category in (FailureCategory.SEMANTIC_VIOLATION, FailureCategory.SYNTAX_ERROR)
    ws_adv1.cleanup()

    # 3. Adversarial Patch 2: Dangerous Import (subprocess)
    ws_adv2 = RepairWorkspace(
        target_repo_path=env["repo_dir"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["workspace"].workspace_path.parent / "scratch_adv2",
    )
    ws_adv2.prepare()
    adv2_replacement = "import subprocess\n    if rate > 1.0:\n        raise ValueError('Rate too high')"
    req_adv2 = env["request"].model_copy(update={
        "repair_id": "rep_m4_adv2",
        "workspace_id": ws_adv2.workspace_id,
        "state": RepairExecutionState.CREATED,
    })
    env["exec_store"].save_repair_request(req_adv2)

    with pytest.raises((PatchSemanticInvariantError, PatchValidationError)):
        coordinator.execute_candidate_generation(
            request=req_adv2,
            workspace=ws_adv2,
            replacement_content=adv2_replacement,
        )
    persisted_adv2 = env["exec_store"].get_repair_request("rep_m4_adv2")
    assert persisted_adv2.state == RepairExecutionState.FAILED
    ws_adv2.cleanup()

    # 4. Adversarial Patch 3: Secret Injection (credential in code)
    ws_adv3 = RepairWorkspace(
        target_repo_path=env["repo_dir"],
        base_commit_sha=env["head_sha"],
        scratch_root=env["workspace"].workspace_path.parent / "scratch_adv3",
    )
    ws_adv3.prepare()
    adv3_replacement = "    SECRET = 'sk-1234567890abcdef1234567890abcdef'\n    if rate > 1.0:\n        raise ValueError('Rate too high')"
    req_adv3 = env["request"].model_copy(update={
        "repair_id": "rep_m4_adv3",
        "workspace_id": ws_adv3.workspace_id,
        "state": RepairExecutionState.CREATED,
    })
    env["exec_store"].save_repair_request(req_adv3)

    with pytest.raises((PatchAdversarialDefectError, PatchValidationError)):
        coordinator.execute_candidate_generation(
            request=req_adv3,
            workspace=ws_adv3,
            replacement_content=adv3_replacement,
        )
    persisted_adv3 = env["exec_store"].get_repair_request("rep_m4_adv3")
    assert persisted_adv3.state == RepairExecutionState.FAILED
    ws_adv3.cleanup()

    # Guarantee target repository immutability
    assert env["workspace"].check_target_immutability() is True
    env["workspace"].cleanup()
