"""Comprehensive Unit & Governance Test Suite for Phase 9 Milestone 4:
Operator Audit, Publication Ledger & End-to-End Governance.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional
import uuid

import pytest
from typer.testing import CliRunner

from forge.analysis.findings import Finding
from forge.cli import app
from forge.publication.audit import (
    ArtifactProvenanceRecord,
    AuthorizationAuditRecord,
    EventLedgerAuditor,
    LedgerIntegrityResult,
    OperationExecutionStatus,
    PublicationAuditReport,
    PublicationAuditReportBuilder,
    PublicationLifecycleStage,
    PublicationReconciliationRecord,
    PublicationReconciler,
    PublicationSecurityAuditor,
    PublicationTimelineBuilder,
    ReconciliationStatus,
    SecurityAuditItem,
    SecurityAuditSummary,
    SecurityCheckOutcome,
    TimelineEventRecord,
)
from forge.publication.audit_crypto import (
    AuditIntegrityStatus,
    canonical_event_bytes,
    compute_event_hash,
    verify_event_hash,
)
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
    PublicationAction,
    PublicationPlan,
    PublicationRequest,
    PublicationStatus,
    PullRequestRecord,
    RemoteBranchRecord,
    RemotePublicationRecord,
    RepositoryIdentity,
    StagedCommitRecord,
)
from forge.publication.storage import PublicationStore
from forge.repair.execution.models import (
    ApprovalBinding,
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionState,
    RepairRequest,
    ReviewDossier,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.schemas import FindingSeverity
from forge.storage.database import get_db_connection, init_database
from forge.storage.store import ForgeStore

runner = CliRunner()


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def git_repos(tmp_path: Path):
    """Sets up a local working repository and a bare upstream remote repository."""
    remote_dir = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)

    local_dir = tmp_path / "local_repo"
    local_dir.mkdir()
    subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=str(local_dir), check=True)
    subprocess.run(["git", "config", "user.email", "committer@test.local"], cwd=str(local_dir), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(local_dir), check=True)

    file1 = local_dir / "service.py"
    file1.write_text("def process():\n    return 'original'\n")
    subprocess.run(["git", "add", "service.py"], cwd=str(local_dir), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(local_dir), check=True)

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(local_dir), check=True, capture_output=True, text=True
    ).stdout.strip()

    remote_url = f"file://{remote_dir.resolve()}"
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=str(local_dir), check=True)
    subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=str(local_dir), check=True)

    return {
        "local_dir": local_dir,
        "remote_dir": remote_dir,
        "remote_url": remote_url,
        "base_sha": base_sha,
    }


@pytest.fixture
def m4_env(tmp_path: Path, git_repos):
    """Provisions complete M4 testing environment with verified repair and publication."""
    db_path = tmp_path / "test_m4.db"
    init_database(db_path)

    forge_store = ForgeStore(db_path=db_path)
    exec_store = RepairExecutionStore(db_path=db_path)
    pub_store = PublicationStore(db_path=db_path)

    run_id = forge_store.create_run(str(git_repos["local_dir"]))
    finding = Finding(
        finding_id=f"find_{uuid.uuid4().hex[:10]}",
        run_id=run_id,
        title="Flawed service process logic",
        description="Service returns flawed string.",
        severity=FindingSeverity.HIGH,
        affected_files=["service.py"],
    )
    forge_store.create_finding(
        run_id=run_id,
        title=finding.title,
        description=finding.description,
        severity=finding.severity,
        finding_id=finding.finding_id,
    )

    repair_id = f"rep_{uuid.uuid4().hex[:10]}"
    req = RepairRequest(
        repair_id=repair_id,
        finding_id=finding.finding_id,
        target_repo=str(git_repos["local_dir"].resolve()),
        base_commit_sha=git_repos["base_sha"],
        run_id=run_id,
        evidence_ids=["evi_sample"],
        allowed_files=["service.py"],
        state=RepairExecutionState.VERIFIED,
    )
    exec_store.save_repair_request(req)

    diff_content = "--- a/service.py\n+++ b/service.py\n@@ -1,2 +1,2 @@\n def process():\n-    return 'original'\n+    return 'fixed'\n"
    patch_sha256 = hashlib.sha256(diff_content.encode("utf-8")).hexdigest()
    patch = PatchArtifact(
        artifact_id=f"pat_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        finding_id=finding.finding_id,
        base_commit_sha=git_repos["base_sha"],
        target_file="service.py",
        diff_content=diff_content,
        content_hash=patch_sha256,
        lines_added=1,
        lines_deleted=1,
        hunks_count=1,
        affected_paths=["service.py"],
    )
    exec_store.save_patch_artifact(patch)

    dossier = ReviewDossier(
        dossier_id=f"dos_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        finding_id=finding.finding_id,
        finding_title=finding.title,
        finding_severity=finding.severity.value,
        repository_target=str(git_repos["local_dir"].resolve()),
        base_commit_sha=git_repos["base_sha"],
        patch_artifact_id=patch.artifact_id,
        patch_sha256=patch_sha256,
        unified_diff=patch.diff_content,
        validation_id="val_test",
        verification_id="ver_test",
        validation_outcome="ACCEPTED",
        verification_outcome="VERIFIED",
        workspace_id="ws_test",
    )
    exec_store.save_review_dossier(dossier)

    binding = ApprovalBinding(
        repair_id=repair_id,
        finding_id=finding.finding_id,
        repository_target=str(git_repos["local_dir"].resolve()),
        base_commit_sha=git_repos["base_sha"],
        patch_artifact_id=patch.artifact_id,
        patch_sha256=patch_sha256,
        validation_id="val_test",
        verification_id="ver_test",
        dossier_id=dossier.dossier_id,
        dossier_version="1.0.0",
    )
    decision = ApprovalDecisionRecord(
        decision_id=f"dec_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        state=ApprovalState.APPROVED,
        decision="approve",
        authorized_action=AuthorizedAction.CREATE_PR,
        actor="lead_operator",
        reason="Verified and approved for publication",
        binding=binding,
    )
    exec_store.save_approval_decision(decision)

    # Record publication events with cryptographic hash chaining
    pub_store.record_publication_event(repair_id=repair_id, action="stage_branch", actor=RepairActor.OPERATOR, payload={"action": "stage_branch"})
    pub_store.record_publication_event(repair_id=repair_id, action="create_commit", actor=RepairActor.OPERATOR, payload={"action": "create_commit"})
    pub_store.record_publication_event(repair_id=repair_id, action="push_remote_branch", actor=RepairActor.OPERATOR, payload={"action": "push_remote_branch"})
    pub_store.record_publication_event(repair_id=repair_id, action="create_pr", actor=RepairActor.OPERATOR, payload={"action": "create_pr"})
    pub_store.record_publication_event(repair_id=repair_id, action="verify_pr", actor=RepairActor.OPERATOR, payload={"action": "verify_pr"})

    # Publication request and plan
    pub_req = PublicationRequest(
        publication_id=f"pub_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        decision_id=decision.decision_id,
        action=PublicationAction.CREATE_PR,
        status=PublicationStatus.COMPLETED,
    )
    pub_store.save_publication_request(pub_req)

    plan = PublicationPlan(
        plan_id=f"ppl_{uuid.uuid4().hex[:10]}",
        publication_id=pub_req.publication_id,
        repair_id=repair_id,
        decision_id=decision.decision_id,
        finding_id=finding.finding_id,
        repository_target=str(git_repos["local_dir"].resolve()),
        base_commit_sha=git_repos["base_sha"],
        proposed_branch_name=f"forge-repair-{finding.finding_id}",
        commit_proposal=CommitProposal(
            commit_message=f"Fix {finding.finding_id}\n\nAutonomous repair verified.",
            author=CommitAuthor(name="Forge Bot", email="bot@forge.local"),
            committer=CommitAuthor(name="Forge Bot", email="bot@forge.local"),
            patch_sha256=patch_sha256,
        ),
        patch_artifact_id=patch.artifact_id,
        patch_sha256=patch_sha256,
        authorized_action=PublicationAction.CREATE_PR,
        policy_version="1.0.0",
        repository_identity=RepositoryIdentity(
            canonical_path=str(git_repos["local_dir"].resolve()),
            git_root=str(git_repos["local_dir"].resolve()),
            base_commit_sha=git_repos["base_sha"],
            is_clean=True,
        ),
    )
    pub_store.save_publication_plan(plan)

    # Staged commit
    staged_commit = StagedCommitRecord(
        staging_id=f"stg_{uuid.uuid4().hex[:10]}",
        workspace_id="ws_test",
        plan_id=plan.plan_id,
        repair_id=repair_id,
        decision_id=decision.decision_id,
        finding_id=finding.finding_id,
        branch_name=plan.proposed_branch_name,
        base_commit_sha=git_repos["base_sha"],
        commit_sha=f"c0ffee{uuid.uuid4().hex[:34]}",
        tree_sha=f"tree{uuid.uuid4().hex[:36]}",
        parent_commit_sha=git_repos["base_sha"],
        patch_artifact_id=patch.artifact_id,
        patch_sha256=patch_sha256,
        changed_files=["service.py"],
        author=CommitAuthor(name="Forge Bot", email="bot@forge.local"),
        committer=CommitAuthor(name="Forge Bot", email="bot@forge.local"),
        commit_message=f"Fix {finding.finding_id}\n\nAutonomous repair.",
        state="verified",
        workspace_path=str(git_repos["local_dir"].resolve()),
        target_repo_path=str(git_repos["local_dir"].resolve()),
    )
    pub_store.save_staged_commit(staged_commit)

    # Remote branch
    remote_branch = RemoteBranchRecord(
        remote_branch_id=f"rbr_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        publication_id=pub_req.publication_id,
        plan_id=plan.plan_id,
        staged_commit_id=staged_commit.staging_id,
        decision_id=decision.decision_id,
        remote_name="origin",
        remote_url=git_repos["remote_url"],
        branch_name=plan.proposed_branch_name,
        commit_sha=staged_commit.commit_sha,
        parent_commit_sha=git_repos["base_sha"],
        tree_sha=staged_commit.tree_sha,
        patch_sha256=patch_sha256,
        status="verified",
        verification_details={"fast_forward": True},
    )
    pub_store.save_remote_branch(remote_branch)

    # Pull request
    pr_record = PullRequestRecord(
        pr_record_id=f"prr_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        publication_id=pub_req.publication_id,
        plan_id=plan.plan_id,
        decision_id=decision.decision_id,
        staged_commit_id=staged_commit.staging_id,
        remote_branch_id=remote_branch.remote_branch_id,
        provider="github",
        repository_owner="test_owner",
        repository_name="test_repo",
        target_branch="main",
        source_branch=plan.proposed_branch_name,
        pr_number=42,
        pr_url="https://github.com/test_owner/test_repo/pull/42",
        pr_title=f"Fix {finding.finding_id}",
        pr_body="Verified repair.",
        pr_state="open",
        status="verified",
    )
    pub_store.save_pull_request(pr_record)

    # Remote publication record
    remote_pub = RemotePublicationRecord(
        remote_publication_id=f"rpr_{uuid.uuid4().hex[:10]}",
        repair_id=repair_id,
        publication_id=pub_req.publication_id,
        plan_id=plan.plan_id,
        decision_id=decision.decision_id,
        staged_commit_id=staged_commit.staging_id,
        provider="github",
        status="completed",
        remote_branch_id=remote_branch.remote_branch_id,
        pr_record_id=pr_record.pr_record_id,
    )
    pub_store.save_remote_publication(remote_pub)

    return {
        "db_path": db_path,
        "forge_store": forge_store,
        "exec_store": exec_store,
        "pub_store": pub_store,
        "repair_id": repair_id,
        "finding_id": finding.finding_id,
        "patch": patch,
        "decision": decision,
        "plan": plan,
        "staged_commit": staged_commit,
        "remote_branch": remote_branch,
        "pr_record": pr_record,
        "git_repos": git_repos,
    }


# =============================================================================
# 1. Cryptographic Event Ledger & Hash Chaining Tests
# =============================================================================

def test_hash_chain_canonical_serialization_deterministic():
    """Verify that canonical event serialization is strictly deterministic."""
    event1 = {
        "event_id": "ree_001",
        "repair_id": "rep_100",
        "sequence": 1,
        "from_state": None,
        "to_state": "planned",
        "actor": "repair_planner",
        "payload": {"b": 2, "a": 1, "nested": {"z": 9, "y": 8}},
        "timestamp": "2026-11-03T00:00:00Z",
    }
    event2 = {
        "payload": {"nested": {"y": 8, "z": 9}, "a": 1, "b": 2},
        "to_state": "planned",
        "timestamp": "2026-11-03T00:00:00Z",
        "sequence": 1,
        "actor": "repair_planner",
        "from_state": None,
        "event_id": "ree_001",
        "repair_id": "rep_100",
    }
    b1 = canonical_event_bytes("GENESIS", event1)
    b2 = canonical_event_bytes("GENESIS", event2)
    assert b1 == b2
    assert compute_event_hash("GENESIS", event1) == compute_event_hash("GENESIS", event2)


def test_hash_chain_creation_and_verification(m4_env):
    """Verify that events saved through storage receive valid cryptographic hashes."""
    auditor = EventLedgerAuditor(db_path=m4_env["db_path"])
    result = auditor.verify_event_chain(m4_env["repair_id"])
    assert result.status == AuditIntegrityStatus.VERIFIED
    assert result.total_events >= 5
    assert result.sealed_events >= 5
    assert result.legacy_events == 0
    assert result.tampered_sequence is None


def test_ledger_tampered_payload_detection(m4_env):
    """Mutating an event payload directly in SQLite must trigger TAMPERED detection."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE repair_execution_events SET payload_json = ? WHERE repair_id = ? AND sequence = 2",
            (json.dumps({"malicious": "injected_payload"}), m4_env["repair_id"]),
        )
    conn.close()

    auditor = EventLedgerAuditor(db_path=m4_env["db_path"])
    result = auditor.verify_event_chain(m4_env["repair_id"])
    assert result.status == AuditIntegrityStatus.TAMPERED
    assert result.tampered_sequence == 2
    assert "tampering detected" in str(result.details)


def test_ledger_tampered_prev_hash_detection(m4_env):
    """Mutating prev_event_hash in SQLite must trigger BROKEN_CHAIN detection."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE repair_execution_events SET prev_event_hash = 'bogus_hash' WHERE repair_id = ? AND sequence = 3",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = EventLedgerAuditor(db_path=m4_env["db_path"])
    result = auditor.verify_event_chain(m4_env["repair_id"])
    assert result.status == AuditIntegrityStatus.BROKEN_CHAIN
    assert result.tampered_sequence == 3


def test_ledger_sequence_gap_detection(m4_env):
    """Skipping a sequence number must trigger MISSING_EVENTS detection."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "DELETE FROM repair_execution_events WHERE repair_id = ? AND sequence = 3",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = EventLedgerAuditor(db_path=m4_env["db_path"])
    result = auditor.verify_event_chain(m4_env["repair_id"])
    assert result.status == AuditIntegrityStatus.MISSING_EVENTS
    assert result.tampered_sequence == 3


def test_ledger_duplicate_sequence_detection(m4_env):
    """Duplicate sequence numbers must trigger DUPLICATE_SEQUENCE detection."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("DROP INDEX IF EXISTS uq_repair_exec_events_seq;")
        conn.execute(
            """
            INSERT INTO repair_execution_events (
                event_id, repair_id, sequence, from_state, to_state, actor, payload_json, timestamp
            ) VALUES ('dup_id', ?, 2, 'planned', 'planned', 'human_operator', '{}', '2026-11-03T00:00:00Z')
            """,
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = EventLedgerAuditor(db_path=m4_env["db_path"])
    result = auditor.verify_event_chain(m4_env["repair_id"])
    assert result.status == AuditIntegrityStatus.DUPLICATE_SEQUENCE
    assert result.tampered_sequence == 2


def test_ledger_legacy_unsealed_events(tmp_path: Path):
    """Events without hashes (historical legacy ledgers) return LEGACY_UNSEALED."""
    db_path = tmp_path / "legacy.db"
    init_database(db_path)
    conn = get_db_connection(db_path)
    with conn:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            """
            INSERT INTO repair_execution_events (
                event_id, repair_id, sequence, from_state, to_state, actor, payload_json, timestamp,
                prev_event_hash, event_hash
            ) VALUES 
            ('e1', 'rep_leg', 1, NULL, 'created', 'repair_orchestrator', '{}', '2026-11-01T00:00:00Z', NULL, NULL),
            ('e2', 'rep_leg', 2, 'created', 'planned', 'repair_planner', '{}', '2026-11-01T00:01:00Z', NULL, NULL)
            """
        )
    conn.close()

    auditor = EventLedgerAuditor(db_path=db_path)
    result = auditor.verify_event_chain("rep_leg")
    assert result.status == AuditIntegrityStatus.LEGACY_UNSEALED
    assert result.total_events == 2
    assert result.sealed_events == 0
    assert result.legacy_events == 2


def test_ledger_empty_events(tmp_path: Path):
    """Querying an unknown or empty repair ID returns verified with 0 events."""
    db_path = tmp_path / "empty.db"
    init_database(db_path)
    auditor = EventLedgerAuditor(db_path=db_path)
    result = auditor.verify_event_chain("nonexistent_repair")
    assert result.status == AuditIntegrityStatus.VERIFIED
    assert result.total_events == 0


# =============================================================================
# 2. End-to-End Publication Timeline Reconstruction Tests
# =============================================================================

def test_timeline_reconstruction_complete_lifecycle(m4_env):
    """Timeline reconstruction captures all stages from finding to PR verification."""
    builder = PublicationTimelineBuilder(db_path=m4_env["db_path"])
    timeline = builder.reconstruct_timeline(m4_env["repair_id"])

    assert len(timeline) >= 6
    stages = [e.stage for e in timeline]
    assert PublicationLifecycleStage.FINDING_CREATED in stages
    assert PublicationLifecycleStage.REPAIR_REQUESTED in stages
    assert PublicationLifecycleStage.DOSSIER_COMPILED in stages
    assert PublicationLifecycleStage.APPROVAL_RECORDED in stages
    assert PublicationLifecycleStage.COMMIT_VERIFIED in stages
    assert PublicationLifecycleStage.REMOTE_VERIFIED in stages
    assert PublicationLifecycleStage.PR_VERIFIED in stages


def _invalidate_approval(env: Dict[str, Any], reason: str = "Invalidated") -> None:
    conn = get_db_connection(env["db_path"])
    with conn:
        conn.execute(
            "UPDATE repair_approval_decisions SET state = 'invalidated', invalidation_reason = ? WHERE repair_id = ?",
            (reason, env["repair_id"]),
        )
    conn.close()


def test_timeline_reconstruction_approval_invalidation(m4_env):
    """When an approval is invalidated, timeline reflects APPROVAL_INVALIDATED."""
    _invalidate_approval(m4_env, "Patch revised")
    builder = PublicationTimelineBuilder(db_path=m4_env["db_path"])
    timeline = builder.reconstruct_timeline(m4_env["repair_id"])

    stages = [e.stage for e in timeline]
    assert PublicationLifecycleStage.APPROVAL_INVALIDATED in stages


def test_timeline_chronological_ordering(m4_env):
    """Timeline events are ordered chronologically by timestamp."""
    builder = PublicationTimelineBuilder(db_path=m4_env["db_path"])
    timeline = builder.reconstruct_timeline(m4_env["repair_id"])

    for i in range(len(timeline) - 1):
        assert timeline[i].timestamp <= timeline[i + 1].timestamp


# =============================================================================
# 3. Publication Security Auditor (14 Checks) Tests
# =============================================================================

def test_security_auditor_all_14_checks_pass(m4_env):
    """When all publication prerequisites are met, all 14 checks must PASS."""
    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])

    assert summary.total_checks == 14
    assert summary.failed == 0
    assert summary.all_passed is True
    assert summary.passed == 14


def test_security_auditor_sec01_missing_approval(m4_env):
    """Missing operator approval triggers SEC-01 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute("DELETE FROM repair_approval_decisions WHERE repair_id = ?", (m4_env["repair_id"],))
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec01 = next(c for c in summary.checks if c.check_id == "SEC-01")
    assert sec01.outcome == SecurityCheckOutcome.FAIL
    assert summary.all_passed is False


def test_security_auditor_sec02_action_mismatch(m4_env):
    """Remote publication with only STAGE_FOR_PR authorization triggers SEC-02 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE repair_approval_decisions SET authorized_action = 'stage_for_pr' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec02 = next(c for c in summary.checks if c.check_id == "SEC-02")
    assert sec02.outcome == SecurityCheckOutcome.FAIL
    assert "does not permit remote publication" in sec02.description


def test_security_auditor_sec03_invalidated_approval(m4_env):
    """Invalidated approval triggers SEC-03 failure."""
    _invalidate_approval(m4_env, "Invalidated by operator")

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec03 = next(c for c in summary.checks if c.check_id == "SEC-03")
    assert sec03.outcome == SecurityCheckOutcome.FAIL
    assert "invalidated" in sec03.description


def test_security_auditor_sec04_patch_sha_mismatch(m4_env):
    """Mismatch between staged commit patch hash and approval binding triggers SEC-04 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE publication_staged_commits SET patch_sha256 = 'corrupted_hash' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec04 = next(c for c in summary.checks if c.check_id == "SEC-04")
    assert sec04.outcome == SecurityCheckOutcome.FAIL


def test_security_auditor_sec08_branch_name_mismatch(m4_env):
    """Remote branch differing from authorized publication plan triggers SEC-08 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE publication_remote_branches SET branch_name = 'unauthorized-branch' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec08 = next(c for c in summary.checks if c.check_id == "SEC-08")
    assert sec08.outcome == SecurityCheckOutcome.FAIL


def test_security_auditor_sec09_remote_commit_sha_mismatch(m4_env):
    """Remote commit SHA diverged from local staged commit triggers SEC-09 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE publication_remote_branches SET commit_sha = 'diverged_commit_sha' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec09 = next(c for c in summary.checks if c.check_id == "SEC-09")
    assert sec09.outcome == SecurityCheckOutcome.FAIL


def test_security_auditor_sec10_force_push_prohibited(m4_env):
    """Presence of force-push parameter in provider operation triggers SEC-10 failure."""
    m4_env["pub_store"].record_provider_operation(
        repair_id=m4_env["repair_id"],
        provider="github",
        operation_type="push",
        idempotency_key="force_push_test",
        request_payload={"force": True, "ref": "refs/heads/branch"},
        response_payload={},
        status="success",
    )

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec10 = next(c for c in summary.checks if c.check_id == "SEC-10")
    assert sec10.outcome == SecurityCheckOutcome.FAIL


def test_security_auditor_sec12_pr_target_branch_invalid(m4_env):
    """PR targeting non-standard / unauthorized base branch triggers SEC-12 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE publication_pull_requests SET target_branch = 'internal_staging' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec12 = next(c for c in summary.checks if c.check_id == "SEC-12")
    assert sec12.outcome == SecurityCheckOutcome.FAIL


def test_security_auditor_sec13_ledger_tampering_caught(m4_env):
    """Ledger event tampering triggers SEC-13 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE repair_execution_events SET payload_json = '{\"tampered\": true}' WHERE repair_id = ? AND sequence = 1",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec13 = next(c for c in summary.checks if c.check_id == "SEC-13")
    assert sec13.outcome == SecurityCheckOutcome.FAIL


def test_security_auditor_sec14_credential_exposure_caught(m4_env):
    """Simulated credentials in PR body triggers SEC-14 failure."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE publication_pull_requests SET pr_body = 'Leaked token: ghp_123456789012345678901234567890123456' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    auditor = PublicationSecurityAuditor(db_path=m4_env["db_path"])
    summary = auditor.audit_publication(m4_env["repair_id"])
    sec14 = next(c for c in summary.checks if c.check_id == "SEC-14")
    assert sec14.outcome == SecurityCheckOutcome.FAIL


# =============================================================================
# 4. Publication Reconciler Tests
# =============================================================================

def test_reconciliation_confirmed_success(m4_env):
    """Complete publication records result in CONFIRMED_SUCCESS reconciliation."""
    reconciler = PublicationReconciler(db_path=m4_env["db_path"])
    record = reconciler.reconcile_publication(m4_env["repair_id"])
    assert record.status == ReconciliationStatus.CONFIRMED_SUCCESS
    assert record.is_consistent is True
    assert record.pr_number == 42


def test_reconciliation_partial_staged_commit_only(m4_env):
    """When only local commit is staged, reconciliation reports PARTIAL_SUCCESS and push action."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute("DELETE FROM publication_pull_requests WHERE repair_id = ?", (m4_env["repair_id"],))
        conn.execute("DELETE FROM publication_remote_branches WHERE repair_id = ?", (m4_env["repair_id"],))
    conn.close()

    reconciler = PublicationReconciler(db_path=m4_env["db_path"])
    record = reconciler.reconcile_publication(m4_env["repair_id"])
    assert record.status == ReconciliationStatus.PARTIAL_SUCCESS
    assert record.is_consistent is False
    assert "Push staged branch" in record.recommended_action


def test_reconciliation_partial_branch_pushed_only(m4_env):
    """When branch is pushed but no PR opened, reports PARTIAL_SUCCESS and PR action."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute("DELETE FROM publication_pull_requests WHERE repair_id = ?", (m4_env["repair_id"],))
    conn.close()

    reconciler = PublicationReconciler(db_path=m4_env["db_path"])
    record = reconciler.reconcile_publication(m4_env["repair_id"])
    assert record.status == ReconciliationStatus.PARTIAL_SUCCESS
    assert record.is_consistent is False
    assert "opening upstream pull request" in record.recommended_action


def test_reconciliation_approval_invalidated(m4_env):
    """When approval was invalidated, reports APPROVAL_INVALIDATED and discard advice."""
    _invalidate_approval(m4_env, "Requirements changed")
    reconciler = PublicationReconciler(db_path=m4_env["db_path"])
    record = reconciler.reconcile_publication(m4_env["repair_id"])
    assert record.status == ReconciliationStatus.APPROVAL_INVALIDATED
    assert record.is_consistent is False
    assert "Discard local publication branch" in record.recommended_action


def test_reconciliation_external_modification_detected(m4_env):
    """When remote provider reports a different PR state, reports EXTERNAL_MODIFICATION."""
    class MockProvider:
        def get_pull_request(self, owner, repo, pr_number):
            class MockPR:
                state = "closed"
            return MockPR()

    reconciler = PublicationReconciler(db_path=m4_env["db_path"])
    record = reconciler.reconcile_publication(m4_env["repair_id"], provider_client=MockProvider())
    assert record.status == ReconciliationStatus.EXTERNAL_MODIFICATION
    assert record.is_consistent is False
    assert "externally" in str(record.diagnostics)


def test_reconciliation_non_destructive_guarantee(m4_env):
    """Reconciliation must never mutate, delete, or destroy local or remote database rows."""
    reconciler = PublicationReconciler(db_path=m4_env["db_path"])
    reconciler.reconcile_publication(m4_env["repair_id"])

    # Verify rows still exist
    conn = get_db_connection(m4_env["db_path"])
    count_sc = conn.execute("SELECT count(*) FROM publication_staged_commits WHERE repair_id = ?", (m4_env["repair_id"],)).fetchone()[0]
    count_rb = conn.execute("SELECT count(*) FROM publication_remote_branches WHERE repair_id = ?", (m4_env["repair_id"],)).fetchone()[0]
    count_pr = conn.execute("SELECT count(*) FROM publication_pull_requests WHERE repair_id = ?", (m4_env["repair_id"],)).fetchone()[0]
    conn.close()
    assert count_sc == 1
    assert count_rb == 1
    assert count_pr == 1


# =============================================================================
# 5. Publication Audit Report Builder Tests
# =============================================================================

def test_report_builder_markdown_generation_and_facts_segregation(m4_env):
    """Report builder generates structured markdown segregating facts from model text."""
    builder = PublicationAuditReportBuilder(db_path=m4_env["db_path"])
    report = builder.build_report(m4_env["repair_id"])

    markdown = builder.render_markdown(report)
    assert "# FORGE Publication Audit & Governance Report" in markdown
    assert "## 1. Executive Governance Summary" in markdown
    assert "## 2. Artifact Provenance & Cryptographic Bindings" in markdown
    assert "## 3. Operator Authorization Audit" in markdown
    assert "## 4. Security Invariants Audit (14 Independent Checks)" in markdown
    assert "## 5. End-to-End Chronological Timeline" in markdown
    assert "## 6. Workflow Reconciliation & Consistency Diagnostics" in markdown
    assert report.provenance.finding_id in markdown
    assert report.provenance.patch_sha256 in markdown
    assert "ALL CHECKS PASSED" not in markdown or "[PASS]" in markdown


def test_report_builder_json_serialization(m4_env):
    """Report builder renders valid, parseable JSON conforming to model."""
    builder = PublicationAuditReportBuilder(db_path=m4_env["db_path"])
    report = builder.build_report(m4_env["repair_id"])

    json_str = builder.render_json(report)
    data = json.loads(json_str)
    assert data["repair_id"] == m4_env["repair_id"]
    assert data["security"]["all_passed"] is True
    assert len(data["security"]["checks"]) == 14
    assert len(data["timeline"]) >= 5


def test_report_secret_redaction(m4_env):
    """Secrets present in any field are redacted in generated reports."""
    conn = get_db_connection(m4_env["db_path"])
    with conn:
        conn.execute(
            "UPDATE publication_requests SET error_message = 'Failed with token: ghp_1234567890abcdefghijklmnopqrstuvwxyz' WHERE repair_id = ?",
            (m4_env["repair_id"],),
        )
    conn.close()

    builder = PublicationAuditReportBuilder(db_path=m4_env["db_path"])
    report = builder.build_report(m4_env["repair_id"])
    markdown = builder.render_markdown(report)
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz" not in markdown


# =============================================================================
# 6. CLI Integration Tests
# =============================================================================

def test_cli_publish_audit_pass(m4_env, monkeypatch):
    """forge repair publish audit exits 0 when all 14 checks pass."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m4_env["db_path"]))
    result = runner.invoke(app, ["repair", "publish", "audit", m4_env["repair_id"]])
    assert result.exit_code == 0
    assert "Publication Security Audit" in result.output
    assert "ALL CHECKS PASSED" in result.output


def test_cli_publish_audit_fail(m4_env, monkeypatch):
    """forge repair publish audit exits 1 when a check fails."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m4_env["db_path"]))
    # Invalidate approval
    _invalidate_approval(m4_env, "Revoked")

    result = runner.invoke(app, ["repair", "publish", "audit", m4_env["repair_id"]])
    assert result.exit_code == 1
    assert "SECURITY INVARIANT FAILURES DETECTED" in result.output


def test_cli_publish_timeline(m4_env, monkeypatch):
    """forge repair publish timeline displays chronological table."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m4_env["db_path"]))
    result = runner.invoke(app, ["repair", "publish", "timeline", m4_env["repair_id"]])
    assert result.exit_code == 0
    assert "End-to-End Publication Timeline" in result.output
    assert "SUCCESS" in result.output

    # JSON mode
    json_result = runner.invoke(app, ["repair", "publish", "timeline", m4_env["repair_id"], "--json"])
    assert json_result.exit_code == 0
    parsed = json.loads(json_result.output)
    assert isinstance(parsed, list)
    assert len(parsed) >= 5


def test_cli_publish_audit_verify(m4_env, monkeypatch):
    """forge repair publish audit-verify exits 0 on valid chain."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m4_env["db_path"]))
    result = runner.invoke(app, ["repair", "publish", "audit-verify", m4_env["repair_id"]])
    assert result.exit_code == 0
    assert "Cryptographic Ledger Verification" in result.output
    assert "VERIFIED" in result.output


def test_cli_publish_report(m4_env, monkeypatch, tmp_path: Path):
    """forge repair publish report produces report and writes to output file."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m4_env["db_path"]))
    out_file = tmp_path / "report.md"
    result = runner.invoke(app, ["repair", "publish", "report", m4_env["repair_id"], "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# FORGE Publication Audit & Governance Report" in content


def test_cli_publish_history(m4_env, monkeypatch):
    """forge repair publish history displays historical publication rows."""
    monkeypatch.setenv("FORGE_DB_PATH", str(m4_env["db_path"]))
    result = runner.invoke(app, ["repair", "publish", "history"])
    assert result.exit_code == 0
    assert "Publication Workflow History" in result.output
    assert m4_env["repair_id"] in result.output

    # JSON mode
    json_result = runner.invoke(app, ["repair", "publish", "history", "--json"])
    assert json_result.exit_code == 0
    parsed = json.loads(json_result.output)
    assert isinstance(parsed, list)
    assert len(parsed) >= 1
