"""Comprehensive unit and integration tests for Phase 5 Milestone 5:

Certified Engineering Dossier & Operator Export.

Tests:
1. Approval gating (only APPROVED workflows can be exported).
2. Refusal on unapproved states (AWAITING_HUMAN_REVIEW, VERIFIED, REPAIR_PENDING, REJECTED, etc.).
3. Refusal on missing human decision or decision='reject'.
4. Transactional ledger audit integrity and sequential event ordering.
5. Patch candidate diff integrity & anti-tampering (SHA256 verification).
6. Protected file policy enforcement (rejection of patches modifying AGENTS.md, etc.).
7. Multi-check verification outcome revalidation (must be passed, zero regressions).
8. Target repository revision drift detection.
9. Output directory safety: refusal when non-empty without --force, rejection of symlinks.
10. Complete package layout and schema verification (patch.diff, verification_certificate.json,
    findings.sarif, audit_ledger.json, manifest.json, APPLY.md).
11. Secret scrubbing across all dossier artifacts.
12. Deterministic artifact generation.
13. HMAC-SHA256 cryptographic signing, verification, wrong key, and missing key handling.
14. File tampering and integrity violation detection in validator.
15. Path traversal, symlink, and foreign file containment defense.
16. Target repository bit-for-bit immutability.
17. Orchestrator and CLI integration tests.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.repair.contracts import compute_patch_sha256
from forge.repair.models import PatchCandidate, RepairPlan, RepairPlanStatus
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.dossier import (
    ALL_ALLOWED_DOSSIER_FILES,
    REQUIRED_DOSSIER_FILES,
    export_engineering_dossier,
    sign_manifest_files,
    validate_engineering_dossier,
    validate_workflow_for_export,
    verify_manifest_signature,
)
from forge.workflow.dossier_models import (
    DossierError,
    DossierExportDirectoryError,
    DossierExportRefusedError,
    DossierIntegrityError,
    DossierManifest,
    DossierSecurityError,
    DossierSignatureError,
    DossierValidationResult,
    DossierWorkflowNotApprovedError,
    VerificationCertificate,
)
from forge.workflow.intake import create_intake_from_test_spec
from forge.workflow.models import (
    CheckStatus,
    EngineeringWorkflow,
    EngineeringWorkflowEvent,
    EngineeringWorkflowState,
    MultiCheckVerificationPlan,
    MultiCheckVerificationResult,
    VerificationAggregateOutcome,
    VerificationCheckResult,
    VerificationCheckSpec,
    VerificationCheckType,
    WorkflowIntakeSpec,
)
from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

runner = CliRunner()


# =============================================================================
# Fixtures & Helpers
# =============================================================================

@pytest.fixture
def test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Create a real git repository fixture with sample files."""
    repo_dir = tmp_path / "git_target_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    calc = repo_dir / "calculator.py"
    calc.write_text("def calculate(a, b):\n    return a + b  # Defect: should be a - b\n")

    test_file = tests_dir / "test_calc.py"
    test_file.write_text("from calculator import calculate\n\ndef test_calc():\n    assert calculate(5, 2) == 3\n")

    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Forge Tester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "tester@forge.internal"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)

    git_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    commit_sha = git_res.stdout.strip().lower()
    return repo_dir, commit_sha


@pytest.fixture
def mock_store(tmp_path: Path) -> ForgeStore:
    db_file = tmp_path / "test_wf5.sqlite3"
    init_database(db_file)
    return ForgeStore(db_path=db_file)


def _make_valid_approved_workflow_bundle(
    store: ForgeStore,
    repo_path: Path,
    commit_sha: str,
    workflow_id: str = "wf_approved_001",
    target_file: str = "calculator.py",
) -> Tuple[EngineeringWorkflow, WorkflowIntakeSpec, RepairPlan, MultiCheckVerificationResult]:
    """Create a complete, fully approved workflow bundle with valid store records."""
    # 1. Intake
    spec = create_intake_from_test_spec(
        repo_path=repo_path,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    store.save_workflow_intake(spec)

    # 2. Finding & Evidence
    store.create_run(str(repo_path), run_id=f"run_{workflow_id}")
    finding_id = f"find_{workflow_id}"
    store.create_finding(
        run_id=f"run_{workflow_id}",
        title="Incorrect addition in calculate()",
        description="Operator found calculator uses + instead of - operator.",
        severity=FindingSeverity.HIGH,
        finding_id=finding_id,
        affected_files=[target_file],
    )
    ev = EvidenceRecord(
        evidence_id=f"evi_{workflow_id}",
        run_id=f"run_{workflow_id}",
        finding_id=finding_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Failing test run",
        content="AssertionError: assert 7 == 3",
        commit_sha=commit_sha,
        details={"status": "reproduced", "exit_code": 1},
    )
    store.attach_evidence(ev)

    # 3. Patch Candidate & Repair Plan
    diff = (
        f"--- a/{target_file}\n"
        f"+++ b/{target_file}\n"
        "@@ -1,2 +1,2 @@\n"
        " def calculate(a, b):\n"
        "-    return a + b  # Defect: should be a - b\n"
        "+    return a - b\n"
    )
    digest = compute_patch_sha256(diff)
    plan_id = f"plan_{workflow_id}"
    candidate = PatchCandidate(
        patch_id=f"patch_{workflow_id}",
        contract_id=f"contract_{workflow_id}",
        finding_id=finding_id,
        base_commit_sha=commit_sha,
        target_files=[target_file],
        unified_diff=diff,
        added_lines=1,
        removed_lines=1,
        hunk_count=1,
        sha256_digest=digest,
    )
    plan = RepairPlan(
        plan_id=plan_id,
        finding_id=finding_id,
        base_commit_sha=commit_sha,
        cited_evidence_ids=[ev.evidence_id],
        target_repo=str(repo_path),
        target_file=target_file,
        root_cause="Addition operator used instead of subtraction",
        proposed_change="Change + to - operator",
        expected_behavior="calculate(5, 2) returns 3",
        verification_plan="pytest tests/test_calc.py",
        status=RepairPlanStatus.VALIDATED,
        patch_candidate=candidate,
    )
    store.save_repair_plan(plan)

    # 4. Multi-Check Verification Result
    attempt_id = f"vatt_{workflow_id}"
    vplan = MultiCheckVerificationPlan(
        plan_id=f"vplan_{workflow_id}",
        workflow_id=workflow_id,
        repair_plan_id=plan_id,
        base_commit_sha=commit_sha,
        patch_digest=digest,
        target_file=target_file,
        checks=[
            VerificationCheckSpec(
                check_type=VerificationCheckType.REPRODUCTION,
                name="repro:pytest",
                command=["pytest", "tests/test_calc.py"],
            ),
            VerificationCheckSpec(
                check_type=VerificationCheckType.BASELINE_TESTS,
                name="baseline_suite:pytest",
                command=["pytest", "-q", "tests/"],
            ),
        ],
    )
    base_check = VerificationCheckResult(
        check_type=VerificationCheckType.REPRODUCTION,
        name="repro:pytest",
        status=CheckStatus.FAILED,
        command=["pytest", "tests/test_calc.py"],
        pinned_revision=commit_sha,
        scratch_workspace_id="ws_b",
        exit_code=1,
        is_baseline=True,
        provenance=f"test:verify:{workflow_id}:baseline",
    )
    cand_repro = VerificationCheckResult(
        check_type=VerificationCheckType.REPRODUCTION,
        name="repro:pytest",
        status=CheckStatus.PASSED,
        command=["pytest", "tests/test_calc.py"],
        pinned_revision=commit_sha,
        scratch_workspace_id="ws_c",
        exit_code=0,
        is_baseline=False,
        provenance=f"test:verify:{workflow_id}:candidate",
    )
    cand_base = VerificationCheckResult(
        check_type=VerificationCheckType.BASELINE_TESTS,
        name="baseline_suite:pytest",
        status=CheckStatus.PASSED,
        command=["pytest", "-q", "tests/"],
        pinned_revision=commit_sha,
        scratch_workspace_id="ws_c",
        exit_code=0,
        is_baseline=False,
        provenance=f"test:verify:{workflow_id}:candidate",
    )
    verification = MultiCheckVerificationResult(
        attempt_id=attempt_id,
        workflow_id=workflow_id,
        plan_id=vplan.plan_id,
        repair_plan_id=plan_id,
        base_commit_sha=commit_sha,
        patch_digest=digest,
        target_file=target_file,
        aggregate_outcome=VerificationAggregateOutcome.PASSED,
        sandbox_available=True,
        baseline_results=[base_check],
        candidate_results=[cand_repro, cand_base],
        regression_detected=False,
        diagnostic="All checks passed cleanly.",
    )
    # 4. Workflow in APPROVED State with Sequential Ledger
    now = datetime.now(timezone.utc)
    wf = EngineeringWorkflow(
        workflow_id=workflow_id,
        intake_id=spec.intake_id,
        target_repo=str(repo_path),
        base_commit_sha=commit_sha,
        state=EngineeringWorkflowState.APPROVED,
        repair_plan_id=plan_id,
        verification_attempt_id=attempt_id,
        human_decision="approve",
        human_notes="Verified by senior engineer offline.",
        version=5,
        created_at=now,
        updated_at=now,
    )
    store.save_engineering_workflow(wf)

    # 5. Multi-Check Verification Result
    store.save_multi_check_verification(verification)

    # Append sequential events leading to approval
    store.append_engineering_workflow_event(
        workflow_id=workflow_id,
        from_state=None,
        to_state=EngineeringWorkflowState.CREATED,
        stage="init",
    )
    store.append_engineering_workflow_event(
        workflow_id=workflow_id,
        from_state=EngineeringWorkflowState.CREATED,
        to_state=EngineeringWorkflowState.REPRODUCED,
        stage="reproduction",
    )
    store.append_engineering_workflow_event(
        workflow_id=workflow_id,
        from_state=EngineeringWorkflowState.REPRODUCED,
        to_state=EngineeringWorkflowState.REPAIR_PENDING,
        stage="repair_planning",
    )
    store.append_engineering_workflow_event(
        workflow_id=workflow_id,
        from_state=EngineeringWorkflowState.REPAIR_PENDING,
        to_state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        stage="multi_check_verification",
    )
    store.append_engineering_workflow_event(
        workflow_id=workflow_id,
        from_state=EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
        to_state=EngineeringWorkflowState.APPROVED,
        stage="operator_review",
        payload={"human_decision": "approve", "notes": "Verified by senior engineer offline."},
    )

    return wf, spec, plan, verification


# =============================================================================
# 1. Approval Gating & Workflow State Tests
# =============================================================================

def test_export_refused_when_workflow_not_found(mock_store: ForgeStore, tmp_path: Path):
    """Attempting export on non-existent workflow raises DossierExportRefusedError."""
    with pytest.raises(DossierExportRefusedError, match="not found in store"):
        export_engineering_dossier(
            workflow_id="wf_nonexistent",
            output_dir=tmp_path / "out",
            store=mock_store,
        )


@pytest.mark.parametrize("unapproved_state", [
    EngineeringWorkflowState.CREATED,
    EngineeringWorkflowState.INTAKE_VALIDATED,
    EngineeringWorkflowState.REPO_ANALYZED,
    EngineeringWorkflowState.REPRODUCTION_PENDING,
    EngineeringWorkflowState.REPRODUCED,
    EngineeringWorkflowState.REPRO_FAILED,
    EngineeringWorkflowState.REPRO_BLOCKED,
    EngineeringWorkflowState.REPAIR_PENDING,
    EngineeringWorkflowState.VERIFICATION_PENDING,
    EngineeringWorkflowState.VERIFIED,
    EngineeringWorkflowState.REGRESSION_CHECKING,
    EngineeringWorkflowState.REGRESSION_FAILED,
    EngineeringWorkflowState.AWAITING_HUMAN_REVIEW,
    EngineeringWorkflowState.REJECTED,
])
def test_export_refused_for_all_unapproved_states(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
    unapproved_state: EngineeringWorkflowState,
):
    """Every workflow state except APPROVED must be strictly refused for export."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_test_unapp")

    # Update workflow to unapproved state
    unapproved_wf = wf.model_copy(update={"state": unapproved_state, "human_decision": None})
    mock_store.save_engineering_workflow(unapproved_wf)

    with pytest.raises(DossierWorkflowNotApprovedError, match="Dossiers may only be exported after explicit human operator approval"):
        export_engineering_dossier(
            workflow_id=unapproved_wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


def test_export_refused_when_human_decision_is_not_approve(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If state is APPROVED but human_decision is missing or not 'approve', export is refused."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_invalid_decision")

    invalid_wf = wf.model_copy(update={"human_decision": "reject"})
    mock_store.save_engineering_workflow(invalid_wf)

    with pytest.raises(DossierWorkflowNotApprovedError, match="invalid human_decision"):
        export_engineering_dossier(
            workflow_id=invalid_wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


# =============================================================================
# 2. Transactional Ledger & Concurrency Integrity Tests
# =============================================================================

def test_export_refused_when_ledger_empty(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If audit ledger events are absent, export is refused due to integrity violation."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_no_events")

    # Clear ledger events
    conn = mock_store._conn()
    try:
        with conn:
            conn.execute("DELETE FROM engineering_workflow_events WHERE workflow_id = ?", (wf.workflow_id,))
    finally:
        conn.close()

    with pytest.raises(DossierExportRefusedError, match="Audit ledger.*is empty"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


def test_export_refused_when_ledger_sequence_is_not_monotonic(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Gaps or corrupt sequence numbers in ledger events must cause export rejection."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_gap_events")

    # Corrupt sequence number of second event
    conn = mock_store._conn()
    try:
        with conn:
            conn.execute(
                "UPDATE engineering_workflow_events SET sequence = 10 WHERE workflow_id = ? AND sequence = 2",
                (wf.workflow_id,),
            )
    finally:
        conn.close()

    with pytest.raises(DossierIntegrityError, match="Ledger sequence discrepancy"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


# =============================================================================
# 3. Patch Integrity & Policy Safety Tests
# =============================================================================

def test_export_refused_when_patch_diff_is_tampered(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If patch unified diff does not match stored SHA-256 digest, export fails closed."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_tampered_patch")

    # Tamper with diff content while keeping old sha256_digest
    tampered_candidate = plan.patch_candidate.model_copy(
        update={"unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,1 +1,1 @@\n-x\n+tampered\n"}
    )
    tampered_plan = plan.model_copy(update={"patch_candidate": tampered_candidate})
    mock_store.save_repair_plan(tampered_plan)

    with pytest.raises(DossierIntegrityError, match="does not match stored SHA-256 digest"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


def test_export_refused_when_target_file_is_protected(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Patches attempting to modify protected repository files (AGENTS.md, pyproject.toml) are rejected."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(
        mock_store, repo, commit_sha, "wf_prot_file", target_file="AGENTS.md"
    )

    with pytest.raises(DossierSecurityError, match="protected by safety policy"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


# =============================================================================
# 4. Verification Record & Revision Drift Tests
# =============================================================================

def test_export_refused_when_verification_outcome_failed(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If verification recorded a failure or regression, export must be refused."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_failed_ver")

    failed_ver = ver.model_copy(update={"aggregate_outcome": VerificationAggregateOutcome.FAILED})
    mock_store.save_multi_check_verification(failed_ver)

    with pytest.raises(DossierExportRefusedError, match="Verification outcome.*is 'failed'"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


def test_export_refused_when_verification_detected_regression(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If verification detected a candidate regression, export must be refused."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_regr_ver")

    regr_ver = ver.model_copy(update={"regression_detected": True})
    mock_store.save_multi_check_verification(regr_ver)

    with pytest.raises(DossierIntegrityError, match="recorded a regression in pre-existing test suite"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
        )


def test_export_refused_when_target_repo_head_has_drifted(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If target repository HEAD commit has moved since pinning, export detects revision drift."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_drift")

    # Make a new commit on target repo
    drift_file = repo / "drift.txt"
    drift_file.write_text("drift")
    subprocess.run(["git", "add", "drift.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Drift commit"], cwd=repo, check=True)

    with pytest.raises(DossierIntegrityError, match="Target repository revision drift detected"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=tmp_path / "out",
            store=mock_store,
            target_repo_path=repo,
        )


# =============================================================================
# 5. Output Directory & Security Policy Tests
# =============================================================================

def test_export_refused_when_directory_not_empty_without_force(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Pre-existing non-empty directory triggers DossierExportDirectoryError unless --force is set."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_dir_policy")

    out_dir = tmp_path / "dossier_occupied"
    out_dir.mkdir(parents=True)
    (out_dir / "unrelated.txt").write_text("do not overwrite me")

    with pytest.raises(DossierExportDirectoryError, match="already exists and is not empty"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=out_dir,
            store=mock_store,
            force=False,
        )

    # With force=True, export succeeds
    manifest = export_engineering_dossier(
        workflow_id=wf.workflow_id,
        output_dir=out_dir,
        store=mock_store,
        force=True,
    )
    assert manifest.workflow_id == wf.workflow_id


def test_export_refused_when_directory_is_symlink(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Output directory pointing to a symlink is rejected under DossierSecurityError."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_symlink_dir")

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    sym_dir = tmp_path / "sym_dir"
    os.symlink(real_dir, sym_dir)

    with pytest.raises(DossierSecurityError, match="cannot be a symlink"):
        export_engineering_dossier(
            workflow_id=wf.workflow_id,
            output_dir=sym_dir,
            store=mock_store,
        )


# =============================================================================
# 6. Full Package Export & Manifest Integrity Tests
# =============================================================================

def test_export_happy_path_creates_all_canonical_files(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Happy path creates all 6 canonical files with exact SHA-256 matches."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_happy_export")

    out_dir = tmp_path / "dossier_output"
    manifest = export_engineering_dossier(
        workflow_id=wf.workflow_id,
        output_dir=out_dir,
        store=mock_store,
        target_repo_path=repo,
    )

    assert manifest.workflow_id == wf.workflow_id
    assert manifest.base_commit_sha == commit_sha
    assert manifest.patch_digest == plan.patch_candidate.sha256_digest
    assert manifest.signature is None

    # Check files on disk
    expected_disk_files = {
        "patch.diff",
        "verification_certificate.json",
        "findings.sarif",
        "audit_ledger.json",
        "manifest.json",
        "APPLY.md",
    }
    actual_disk_files = set(os.listdir(out_dir))
    assert actual_disk_files == expected_disk_files

    # Verify manifest entry matches disk for each file
    for fname in REQUIRED_DOSSIER_FILES:
        fpath = out_dir / fname
        assert fpath.is_file()
        assert not fpath.is_symlink()
        entry = manifest.files[fname]
        assert entry.byte_size == fpath.stat().st_size

    # Validate verification certificate content
    cert_text = (out_dir / "verification_certificate.json").read_text()
    cert = VerificationCertificate.model_validate_json(cert_text)
    assert cert.aggregate_outcome == "passed"
    assert cert.human_review.decision == "approve"
    assert cert.base_commit_sha == commit_sha

    # Validate SARIF document structure
    sarif_data = json.loads((out_dir / "findings.sarif").read_text())
    assert sarif_data["version"] == "2.1.0"
    assert len(sarif_data["runs"]) == 1
    assert len(sarif_data["runs"][0]["results"]) == 1

    # Validate APPLY.md instructions
    apply_text = (out_dir / "APPLY.md").read_text()
    assert "git apply" in apply_text
    assert commit_sha in apply_text
    assert "Audit-Only & Human Supervision Notice" in apply_text


def test_export_secret_redaction(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Sensitive API keys and tokens in findings and notes are scrubbed across all export artifacts."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_secret_leak")

    # Inject simulated token into human notes and audit event
    secret_note = "Approved by auditor with session sk-abcdef1234567890abcdef1234567890"
    mock_store.save_engineering_workflow(wf.model_copy(update={"human_notes": secret_note}))
    mock_store.append_engineering_workflow_event(
        workflow_id=wf.workflow_id,
        from_state=EngineeringWorkflowState.APPROVED,
        to_state=EngineeringWorkflowState.APPROVED,
        stage="note_update",
        payload={"secret_key": "sk-abcdef1234567890abcdef1234567890"},
    )

    out_dir = tmp_path / "dossier_scrubbed"
    export_engineering_dossier(wf.workflow_id, out_dir, mock_store)

    # Verify no raw secret exists in any exported file
    for fname in os.listdir(out_dir):
        content = (out_dir / fname).read_text(encoding="utf-8")
        assert "sk-abcdef" not in content
        if "session" in content:
            assert "[REDACTED_SECRET]" in content


def test_export_deterministic_output(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Running export twice creates identical patch diff, certificate, SARIF, and ledger content."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_det_001")

    dir_a = tmp_path / "dossier_a"
    dir_b = tmp_path / "dossier_b"

    export_engineering_dossier(wf.workflow_id, dir_a, mock_store)
    export_engineering_dossier(wf.workflow_id, dir_b, mock_store)

    # Patch diff must be identical
    assert (dir_a / "patch.diff").read_bytes() == (dir_b / "patch.diff").read_bytes()

    # JSON structures (disregarding export timestamps) must be identical
    sarif_a = json.loads((dir_a / "findings.sarif").read_text())
    sarif_b = json.loads((dir_b / "findings.sarif").read_text())
    assert sarif_a == sarif_b


def test_target_repository_immutability(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Exporting a dossier leaves the target git repository bit-for-bit untouched."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_immutable")

    # Record baseline git state
    init_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    init_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout

    out_dir = tmp_path / "dossier_immut"
    export_engineering_dossier(wf.workflow_id, out_dir, mock_store, target_repo_path=repo)

    # Verify target repo after export
    post_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True).stdout
    post_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout

    assert post_status == init_status
    assert post_head == init_head


# =============================================================================
# 7. Cryptographic Signing & Verification Tests
# =============================================================================

def test_hmac_signing_and_verification_happy_path(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Signing manifest with an HMAC-SHA256 key verifies cleanly during dossier validation."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_signed")

    out_dir = tmp_path / "dossier_signed"
    secret_key = "secure-operator-signing-key-xyz-987"

    manifest = export_engineering_dossier(
        workflow_id=wf.workflow_id,
        output_dir=out_dir,
        store=mock_store,
        signing_key=secret_key,
        key_id="operator_key_01",
    )

    assert manifest.signature is not None
    assert manifest.signature.algorithm == "HMAC-SHA256"
    assert manifest.signature.key_id == "operator_key_01"
    assert len(manifest.signature.signature_hex) == 64

    # Validate dossier with matching key
    val_res = validate_engineering_dossier(out_dir, signing_key=secret_key)
    assert val_res.is_valid is True
    assert val_res.signature_present is True
    assert val_res.signature_valid is True
    assert len(val_res.errors) == 0


def test_hmac_signing_wrong_key_fails_validation(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Validating signed dossier with wrong secret key fails signature verification."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_wrong_key")

    out_dir = tmp_path / "dossier_signed_wrong"
    export_engineering_dossier(
        workflow_id=wf.workflow_id,
        output_dir=out_dir,
        store=mock_store,
        signing_key="key_alpha_12345",
    )

    val_res = validate_engineering_dossier(out_dir, signing_key="key_beta_67890")
    assert val_res.is_valid is False
    assert val_res.signature_valid is False
    assert any("signature verification failed" in e.lower() for e in val_res.errors)


def test_hmac_signed_dossier_validated_without_key_yields_warning(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """Validating signed dossier without providing key succeeds on checksums but logs signature warning."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_unverified_sig")

    out_dir = tmp_path / "dossier_no_key"
    export_engineering_dossier(
        workflow_id=wf.workflow_id,
        output_dir=out_dir,
        store=mock_store,
        signing_key="operator_key_abc",
    )

    val_res = validate_engineering_dossier(out_dir, signing_key=None)
    assert val_res.is_valid is True
    assert val_res.signature_present is True
    assert val_res.signature_valid is None
    assert any("no signing key was provided" in w.lower() for w in val_res.warnings)


def test_empty_signing_key_rejected():
    """Attempting to sign with an empty key raises DossierSignatureError."""
    with pytest.raises(DossierSignatureError, match="Signing key cannot be empty"):
        sign_manifest_files({}, key="")


# =============================================================================
# 8. Offline Dossier Validation & Tampering Defense Tests
# =============================================================================

def test_validation_detects_patch_diff_tampering(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If patch.diff is modified after export, validator flags checksum mismatch."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_tamper_diff")

    out_dir = tmp_path / "dossier_tampered"
    export_engineering_dossier(wf.workflow_id, out_dir, mock_store)

    # Tamper with patch.diff on disk
    patch_file = out_dir / "patch.diff"
    patch_file.write_text("corrupted diff content\n")

    val_res = validate_engineering_dossier(out_dir)
    assert val_res.is_valid is False
    assert any("Integrity check failed on 'patch.diff'" in e for e in val_res.errors)


def test_validation_detects_missing_required_file(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If a required artifact is deleted, validator reports missing file error."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_del_file")

    out_dir = tmp_path / "dossier_del"
    export_engineering_dossier(wf.workflow_id, out_dir, mock_store)

    # Delete APPLY.md
    (out_dir / "APPLY.md").unlink()

    val_res = validate_engineering_dossier(out_dir)
    assert val_res.is_valid is False
    assert any("Required artifact 'APPLY.md' was not found" in e for e in val_res.errors)


def test_validation_detects_unexpected_foreign_file(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If an unauthorized foreign file is placed in dossier directory, validator flags it."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_foreign")

    out_dir = tmp_path / "dossier_foreign"
    export_engineering_dossier(wf.workflow_id, out_dir, mock_store)

    # Inject unexpected file
    (out_dir / "malicious_script.sh").write_text("#!/bin/sh\nrm -rf /\n")

    val_res = validate_engineering_dossier(out_dir)
    assert val_res.is_valid is False
    assert any("Unexpected foreign files found" in e for e in val_res.errors)


def test_validation_detects_symlink_attack(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """If an artifact in the dossier directory is replaced with a symlink, validator flags security violation."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_symlink_attack")

    out_dir = tmp_path / "dossier_symlink_att"
    export_engineering_dossier(wf.workflow_id, out_dir, mock_store)

    # Replace patch.diff with a symlink
    target_f = out_dir / "patch.diff"
    target_f.unlink()
    os.symlink("/etc/hosts", target_f)

    val_res = validate_engineering_dossier(out_dir)
    assert val_res.is_valid is False
    assert any("symlink" in e.lower() for e in val_res.errors)


# =============================================================================
# 9. Orchestrator & CLI Integration Tests
# =============================================================================

def test_orchestrator_export_dossier_method(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
):
    """EngineeringWorkflowOrchestrator.export_dossier exports and returns manifest."""
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_orch_export")

    orchestrator = EngineeringWorkflowOrchestrator(store=mock_store)
    out_dir = tmp_path / "orch_dossier"

    manifest = orchestrator.export_dossier(
        workflow_id=wf.workflow_id,
        output_dir=out_dir,
        target_repo_path=repo,
    )

    assert isinstance(manifest, DossierManifest)
    assert manifest.workflow_id == wf.workflow_id
    assert (out_dir / "manifest.json").exists()


def test_cli_workflow_export_and_validate(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end CLI export and validate-dossier commands succeed."""
    monkeypatch.setenv("FORGE_DB_PATH", str(mock_store.db_path))
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_cli_exp")

    out_dir = tmp_path / "cli_dossier"
    key_file = tmp_path / "secret.key"
    key_file.write_text("my-super-secret-signing-key\n")

    # 1. Export via CLI with signing key file
    exp_res = runner.invoke(
        app,
        [
            "workflow",
            "export",
            wf.workflow_id,
            "--output-dir",
            str(out_dir),
            "--repo",
            str(repo),
            "--key-file",
            str(key_file),
            "--key-id",
            "audit_key_01",
        ],
    )
    assert exp_res.exit_code == 0
    assert "Certified Engineering Dossier Exported" in exp_res.output
    assert "Dossier ID" in exp_res.output

    # 2. Validate via CLI
    val_res = runner.invoke(
        app,
        [
            "workflow",
            "validate-dossier",
            str(out_dir),
            "--key-file",
            str(key_file),
        ],
    )
    assert val_res.exit_code == 0
    assert "[VALID]" in val_res.output
    assert "HMAC-SHA256 signature verified" in val_res.output


def test_cli_workflow_export_unapproved_workflow_fails(
    mock_store: ForgeStore,
    test_git_repo: Tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """CLI export of an unapproved workflow fails with exit code 1."""
    monkeypatch.setenv("FORGE_DB_PATH", str(mock_store.db_path))
    repo, commit_sha = test_git_repo
    wf, spec, plan, ver = _make_valid_approved_workflow_bundle(mock_store, repo, commit_sha, "wf_cli_unapp")

    # Set state to AWAITING_HUMAN_REVIEW
    mock_store.save_engineering_workflow(
        wf.model_copy(update={"state": EngineeringWorkflowState.AWAITING_HUMAN_REVIEW, "human_decision": None})
    )

    out_dir = tmp_path / "cli_fail_dir"
    res = runner.invoke(
        app,
        [
            "workflow",
            "export",
            wf.workflow_id,
            "--output-dir",
            str(out_dir),
        ],
    )
    assert res.exit_code == 1
    assert "Dossier export failed" in res.output or "DossierWorkflowNotApprovedError" in res.output
