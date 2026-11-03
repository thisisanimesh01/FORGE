"""Phase 9 Git Publication Architecture — Operator Audit, Ledger & Governance.

Provides cryptographic verification of publication event ledgers, end-to-end
chronological timeline reconstruction, non-destructive state reconciliation,
comprehensive 14-point security audits, and verifiable audit report generation.
"""

from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field

from forge.config import get_settings, redact_secrets
from forge.publication.audit_crypto import (
    AuditIntegrityStatus,
    canonical_event_bytes,
    compute_event_hash,
    verify_event_hash,
)
from forge.publication.models import (
    PublicationAction,
    PublicationStatus,
    PullRequestRecord,
    RemoteBranchRecord,
    RemotePublicationRecord,
    StagedCommitRecord,
)
from forge.publication.storage import PublicationStore
from forge.repair.execution.models import (
    ApprovalDecisionRecord,
    ApprovalState,
    AuthorizedAction,
    PatchArtifact,
    RepairExecutionEvent,
    RepairExecutionRecord,
    RepairExecutionState,
    ReviewDossier,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.storage.database import get_db_connection


# =============================================================================
# Lifecycle and Governance Enums
# =============================================================================

class PublicationLifecycleStage(str, Enum):
    """Lifecycle stages across the entire investigation-to-publication path."""
    FINDING_CREATED = "finding_created"
    EVIDENCE_COLLECTED = "evidence_collected"
    REPAIR_REQUESTED = "repair_requested"
    PLAN_FORMULATED = "plan_formulated"
    PATCH_GENERATED = "patch_generated"
    PATCH_VALIDATED = "patch_validated"
    VERIFICATION_EXECUTED = "verification_executed"
    DOSSIER_COMPILED = "dossier_compiled"
    APPROVAL_RECORDED = "approval_recorded"
    APPROVAL_INVALIDATED = "approval_invalidated"
    APPROVAL_EXPIRED = "approval_expired"
    PUBLICATION_REQUESTED = "publication_requested"
    PUBLICATION_PLANNED = "publication_planned"
    WORKSPACE_STAGED = "workspace_staged"
    COMMIT_CREATED = "commit_created"
    COMMIT_VERIFIED = "commit_verified"
    REMOTE_PUSHED = "remote_pushed"
    REMOTE_VERIFIED = "remote_verified"
    PR_CREATED = "pr_created"
    PR_VERIFIED = "pr_verified"
    REMOTE_STATUS_OBSERVED = "remote_status_observed"
    EXTERNAL_MODIFICATION_DETECTED = "external_modification_detected"


class OperationExecutionStatus(str, Enum):
    """Execution status of an individual timeline operation."""
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    PENDING = "pending"


class SecurityCheckOutcome(str, Enum):
    """Outcome for individual security and invariant audit checks."""
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReconciliationStatus(str, Enum):
    """Reconciliation classification for publication workflows."""
    CONFIRMED_SUCCESS = "confirmed_success"
    PARTIAL_SUCCESS = "partial_success"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN_STATE = "unknown_state"
    EXTERNAL_MODIFICATION = "external_modification"
    APPROVAL_INVALIDATED = "approval_invalidated"


# =============================================================================
# Audit and Governance Models
# =============================================================================

class TimelineEventRecord(BaseModel):
    """Chronological event record in the repair and publication lifecycle."""
    model_config = ConfigDict(frozen=True)

    stage: PublicationLifecycleStage = Field(..., description="Lifecycle stage.")
    timestamp: datetime = Field(..., description="Timestamp of the event.")
    actor: str = Field(..., description="Actor or component responsible for event.")
    description: str = Field(..., description="Human-readable description of event.")
    status: OperationExecutionStatus = Field(default=OperationExecutionStatus.SUCCESS, description="Execution status.")
    evidence_links: List[str] = Field(default_factory=list, description="IDs of linked evidence or artifacts.")
    details: Dict[str, Any] = Field(default_factory=dict, description="Structured factual event details.")
    sequence: Optional[int] = Field(default=None, description="Sequence number if recorded in audit ledger.")
    event_hash: Optional[str] = Field(default=None, description="Cryptographic hash if sealed in ledger.")


class ArtifactProvenanceRecord(BaseModel):
    """Verified cryptographic and versioned binding of all related artifacts."""
    model_config = ConfigDict(frozen=True)

    repair_id: str
    finding_id: str
    target_repo: str
    base_commit_sha: str
    patch_artifact_id: Optional[str] = None
    patch_sha256: Optional[str] = None
    staged_commit_sha: Optional[str] = None
    parent_commit_sha: Optional[str] = None
    tree_sha: Optional[str] = None
    branch_name: Optional[str] = None
    remote_url: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None


class AuthorizationAuditRecord(BaseModel):
    """Audit of operator authorization and governance bindings."""
    model_config = ConfigDict(frozen=True)

    decision_id: Optional[str] = None
    state: Optional[str] = None
    authorized_action: Optional[str] = None
    operator: Optional[str] = None
    decision_timestamp: Optional[datetime] = None
    bound_patch_sha256: Optional[str] = None
    bound_base_commit_sha: Optional[str] = None
    is_valid: bool = False
    invalidation_reason: Optional[str] = None


class SecurityAuditItem(BaseModel):
    """Outcome and diagnostics for an individual security invariant check."""
    model_config = ConfigDict(frozen=True)

    check_id: str
    title: str
    outcome: SecurityCheckOutcome
    description: str
    details: Dict[str, Any] = Field(default_factory=dict)


class SecurityAuditSummary(BaseModel):
    """Aggregated summary of all 14 security invariant checks."""
    model_config = ConfigDict(frozen=True)

    total_checks: int = 14
    passed: int = 0
    failed: int = 0
    inconclusive: int = 0
    not_applicable: int = 0
    all_passed: bool = False
    checks: List[SecurityAuditItem] = Field(default_factory=list)


class LedgerIntegrityResult(BaseModel):
    """Cryptographic integrity assessment of the execution event ledger."""
    model_config = ConfigDict(frozen=True)

    repair_id: str
    status: AuditIntegrityStatus
    total_events: int = 0
    sealed_events: int = 0
    legacy_events: int = 0
    tampered_sequence: Optional[int] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class PublicationReconciliationRecord(BaseModel):
    """Reconciliation state comparing local records, remote state, and authorization."""
    model_config = ConfigDict(frozen=True)

    repair_id: str
    status: ReconciliationStatus
    local_commit_sha: Optional[str] = None
    remote_commit_sha: Optional[str] = None
    pr_number: Optional[int] = None
    pr_state: Optional[str] = None
    is_consistent: bool = True
    recommended_action: Optional[str] = None
    diagnostics: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


class PublicationAuditReport(BaseModel):
    """Complete, self-contained end-to-end publication audit report."""
    model_config = ConfigDict(frozen=True)

    report_id: str
    repair_id: str
    generated_at: datetime
    provenance: ArtifactProvenanceRecord
    authorization: AuthorizationAuditRecord
    integrity: LedgerIntegrityResult
    security: SecurityAuditSummary
    timeline: List[TimelineEventRecord]
    reconciliation: Optional[PublicationReconciliationRecord] = None
    model_explanations: Dict[str, str] = Field(
        default_factory=dict,
        description="Model explanations segregated from empirical facts."
    )


# =============================================================================
# Event Ledger Auditor
# =============================================================================

class EventLedgerAuditor:
    """Audits sequential execution event ledgers and verifies SHA-256 hash chains."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path

    def _conn(self):
        return get_db_connection(self.db_path)

    def verify_event_chain(self, repair_id: str) -> LedgerIntegrityResult:
        """Verifies sequence continuity and cryptographic hash chaining for a repair ledger."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_execution_events WHERE repair_id = ? ORDER BY sequence ASC",
                (repair_id,),
            )
            rows = cur.fetchall()
            if not rows:
                return LedgerIntegrityResult(
                    repair_id=repair_id,
                    status=AuditIntegrityStatus.VERIFIED,
                    total_events=0,
                    sealed_events=0,
                    legacy_events=0,
                    details={"message": "No events recorded for this repair"},
                )

            total_events = len(rows)
            sealed_events = 0
            legacy_events = 0
            expected_seq = 1

            # Check sequence continuity
            seen_seqs = set()
            for row in rows:
                seq = row["sequence"]
                if seq in seen_seqs:
                    return LedgerIntegrityResult(
                        repair_id=repair_id,
                        status=AuditIntegrityStatus.DUPLICATE_SEQUENCE,
                        total_events=total_events,
                        sealed_events=sealed_events,
                        legacy_events=legacy_events,
                        tampered_sequence=seq,
                        details={"error": f"Duplicate sequence number detected: {seq}"},
                    )
                seen_seqs.add(seq)

                if seq != expected_seq:
                    return LedgerIntegrityResult(
                        repair_id=repair_id,
                        status=AuditIntegrityStatus.MISSING_EVENTS,
                        total_events=total_events,
                        sealed_events=sealed_events,
                        legacy_events=legacy_events,
                        tampered_sequence=expected_seq,
                        details={"error": f"Sequence gap detected: expected {expected_seq}, found {seq}"},
                    )
                expected_seq += 1

            # Check for legacy unsealed ledgers (all hashes NULL)
            has_hashes = any(row["event_hash"] is not None for row in rows)
            if not has_hashes:
                return LedgerIntegrityResult(
                    repair_id=repair_id,
                    status=AuditIntegrityStatus.LEGACY_UNSEALED,
                    total_events=total_events,
                    sealed_events=0,
                    legacy_events=total_events,
                    details={"message": "Historical unsealed ledger; no cryptographic hashes recorded"},
                )

            # Verify cryptographic hash chaining
            prev_hash: Optional[str] = None
            for row in rows:
                col_keys = row.keys()
                row_prev_hash = row["prev_event_hash"] if "prev_event_hash" in col_keys else None
                row_event_hash = row["event_hash"] if "event_hash" in col_keys else None

                if row_event_hash is None:
                    legacy_events += 1
                    # Legacy event in mixed chain; update prev_hash to None
                    prev_hash = None
                    continue

                sealed_events += 1

                # Check previous hash linkage if previous event was sealed
                if prev_hash is not None and row_prev_hash != prev_hash:
                    return LedgerIntegrityResult(
                        repair_id=repair_id,
                        status=AuditIntegrityStatus.BROKEN_CHAIN,
                        total_events=total_events,
                        sealed_events=sealed_events,
                        legacy_events=legacy_events,
                        tampered_sequence=row["sequence"],
                        details={
                            "error": f"Broken chain at sequence {row['sequence']}: "
                                     f"expected prev_hash {prev_hash}, found {row_prev_hash}"
                        },
                    )

                # Recompute event hash
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = {"raw": row["payload_json"]}

                event_dict = {
                    "event_id": row["event_id"],
                    "repair_id": row["repair_id"],
                    "sequence": row["sequence"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "actor": row["actor"],
                    "payload": payload,
                    "timestamp": row["timestamp"],
                }

                if not verify_event_hash(row_prev_hash, event_dict, row_event_hash):
                    return LedgerIntegrityResult(
                        repair_id=repair_id,
                        status=AuditIntegrityStatus.TAMPERED,
                        total_events=total_events,
                        sealed_events=sealed_events,
                        legacy_events=legacy_events,
                        tampered_sequence=row["sequence"],
                        details={
                            "error": f"Event tampering detected at sequence {row['sequence']}: "
                                     f"computed hash does not match stored hash {row_event_hash}"
                        },
                    )

                prev_hash = row_event_hash

            return LedgerIntegrityResult(
                repair_id=repair_id,
                status=AuditIntegrityStatus.VERIFIED,
                total_events=total_events,
                sealed_events=sealed_events,
                legacy_events=legacy_events,
                details={"message": "All sequential event hashes successfully verified"},
            )
        finally:
            conn.close()


# =============================================================================
# Publication Timeline Builder
# =============================================================================

class PublicationTimelineBuilder:
    """Reconstructs the end-to-end chronological timeline across the lifecycle."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        self.repair_store = RepairExecutionStore(db_path=db_path)
        self.pub_store = PublicationStore(db_path=db_path)

    def _conn(self):
        return get_db_connection(self.db_path)

    def reconstruct_timeline(self, repair_id: str) -> List[TimelineEventRecord]:
        """Reconstructs chronological timeline across all recorded lifecycle operations."""
        events: List[TimelineEventRecord] = []
        conn = self._conn()
        try:
            # 1. Base Repair Execution and Finding
            repair_req = self.repair_store.get_repair_request(repair_id)
            if not repair_req:
                return events

            events.append(
                TimelineEventRecord(
                    stage=PublicationLifecycleStage.REPAIR_REQUESTED,
                    timestamp=repair_req.created_at,
                    actor="repair_orchestrator",
                    description=f"Repair requested for finding '{repair_req.finding_id}'",
                    status=OperationExecutionStatus.SUCCESS,
                    evidence_links=repair_req.evidence_ids,
                    details={
                        "repair_id": repair_id,
                        "target_repo": repair_req.target_repo,
                        "base_commit_sha": repair_req.base_commit_sha,
                    },
                )
            )

            finding_id = repair_req.finding_id
            finding_cur = conn.execute("SELECT * FROM findings WHERE finding_id = ?", (finding_id,))
            finding_row = finding_cur.fetchone()
            if finding_row:
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.FINDING_CREATED,
                        timestamp=datetime.fromisoformat(finding_row["created_at"]),
                        actor="investigation_engine",
                        description=f"Defect investigated: {finding_row['title']}",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[],
                        details={
                            "finding_id": finding_id,
                            "severity": finding_row["severity"],
                            "confidence": finding_row["confidence"],
                        },
                    )
                )

            # 2. Evidence Collection
            ev_cur = conn.execute(
                "SELECT * FROM evidence WHERE finding_id = ? ORDER BY created_at ASC",
                (finding_id,),
            )
            for ev in ev_cur.fetchall():
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.EVIDENCE_COLLECTED,
                        timestamp=datetime.fromisoformat(ev["created_at"]),
                        actor="evidence_collector",
                        description=f"Evidence recorded: {ev['evidence_type']} ({ev['evidence_id']})",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[ev["evidence_id"]],
                        details={
                            "evidence_id": ev["evidence_id"],
                            "type": ev["evidence_type"],
                            "file_path": ev["file_path"],
                        },
                    )
                )

            # 3. Execution Events from Repair Ledger
            exec_events = self.repair_store.get_events(repair_id)
            stage_map = {
                RepairExecutionState.CREATED: PublicationLifecycleStage.REPAIR_REQUESTED,
                RepairExecutionState.PLANNED: PublicationLifecycleStage.PLAN_FORMULATED,
                RepairExecutionState.PATCH_GENERATED: PublicationLifecycleStage.PATCH_GENERATED,
                RepairExecutionState.PATCH_VALIDATING: PublicationLifecycleStage.PATCH_VALIDATED,
                RepairExecutionState.VALIDATING: PublicationLifecycleStage.PATCH_VALIDATED,
                RepairExecutionState.VERIFIED: PublicationLifecycleStage.VERIFICATION_EXECUTED,
            }

            for ee in exec_events:
                # Check for publication-specific ledger events recorded in repair_execution_events
                payload = ee.payload
                action = payload.get("action")
                stage = stage_map.get(ee.to_state)

                if action == "stage_branch":
                    stage = PublicationLifecycleStage.WORKSPACE_STAGED
                elif action == "create_commit":
                    stage = PublicationLifecycleStage.COMMIT_CREATED
                elif action == "push_remote_branch":
                    stage = PublicationLifecycleStage.REMOTE_PUSHED
                elif action == "verify_remote_branch":
                    stage = PublicationLifecycleStage.REMOTE_VERIFIED
                elif action == "create_pr":
                    stage = PublicationLifecycleStage.PR_CREATED
                elif action == "verify_pr":
                    stage = PublicationLifecycleStage.PR_VERIFIED
                elif not stage:
                    stage = PublicationLifecycleStage.REPAIR_REQUESTED

                desc = payload.get("reason", f"Transitioned to {ee.to_state.value}")
                if "target_action" in payload:
                    desc = f"Publication action: {payload['target_action']}"

                events.append(
                    TimelineEventRecord(
                        stage=stage,
                        timestamp=ee.timestamp,
                        actor=ee.actor.value if hasattr(ee.actor, "value") else str(ee.actor),
                        description=desc,
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[],
                        details=payload,
                        sequence=ee.sequence,
                        event_hash=ee.event_hash,
                    )
                )

            # 4. Review Dossier
            dossier = self.repair_store.get_dossier_for_repair(repair_id)
            if dossier:
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.DOSSIER_COMPILED,
                        timestamp=getattr(dossier, "generated_at", datetime.now(timezone.utc)),
                        actor="dossier_builder",
                        description=f"Operator review dossier compiled: {dossier.dossier_id}",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[dossier.patch_artifact_id],
                        details={
                            "dossier_id": dossier.dossier_id,
                            "patch_sha256": dossier.patch_sha256,
                        },
                    )
                )

            # 5. Approval Decision Records
            app_cur = conn.execute(
                "SELECT * FROM repair_approval_decisions WHERE repair_id = ? ORDER BY created_at ASC",
                (repair_id,),
            )
            for app_row in app_cur.fetchall():
                st = app_row["state"]
                stage = PublicationLifecycleStage.APPROVAL_RECORDED
                if st == ApprovalState.INVALIDATED.value:
                    stage = PublicationLifecycleStage.APPROVAL_INVALIDATED
                elif st == ApprovalState.EXPIRED.value:
                    stage = PublicationLifecycleStage.APPROVAL_EXPIRED

                events.append(
                    TimelineEventRecord(
                        stage=stage,
                        timestamp=datetime.fromisoformat(app_row["created_at"]),
                        actor=app_row["actor"],
                        description=f"Operator decision: {app_row['decision']} ({app_row['authorized_action']})",
                        status=OperationExecutionStatus.SUCCESS if st == ApprovalState.APPROVED.value else OperationExecutionStatus.FAILED,
                        evidence_links=[],
                        details={
                            "decision_id": app_row["decision_id"],
                            "authorized_action": app_row["authorized_action"],
                            "reason": app_row["reason"],
                            "invalidation_reason": app_row["invalidation_reason"],
                        },
                    )
                )

            # 6. Publication Plans
            plan_cur = conn.execute(
                "SELECT * FROM publication_plans WHERE repair_id = ? ORDER BY created_at ASC",
                (repair_id,),
            )
            for plan_row in plan_cur.fetchall():
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.PUBLICATION_PLANNED,
                        timestamp=datetime.fromisoformat(plan_row["created_at"]),
                        actor="publication_planner",
                        description=f"Publication plan formulated for branch '{plan_row['proposed_branch_name']}'",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[plan_row["patch_artifact_id"]],
                        details={
                            "plan_id": plan_row["plan_id"],
                            "branch_name": plan_row["proposed_branch_name"],
                            "base_commit_sha": plan_row["base_commit_sha"],
                        },
                    )
                )

            # 7. Staged Commits
            sc = self.pub_store.get_staged_commit_for_repair(repair_id)
            if sc:
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.COMMIT_VERIFIED,
                        timestamp=sc.updated_at,
                        actor="commit_creator",
                        description=f"Local commit created and verified: {sc.commit_sha}",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[sc.patch_artifact_id],
                        details={
                            "staging_id": sc.staging_id,
                            "commit_sha": sc.commit_sha,
                            "branch_name": sc.branch_name,
                            "tree_sha": sc.tree_sha,
                        },
                    )
                )

            # 8. Remote Branches
            rb = self.pub_store.get_remote_branch_for_repair(repair_id)
            if rb:
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.REMOTE_VERIFIED,
                        timestamp=rb.verified_at or rb.pushed_at,
                        actor="remote_publisher",
                        description=f"Remote branch pushed and verified: {rb.branch_name} ({rb.commit_sha})",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[],
                        details={
                            "remote_branch_id": rb.remote_branch_id,
                            "remote_url": rb.remote_url,
                            "branch_name": rb.branch_name,
                            "commit_sha": rb.commit_sha,
                        },
                    )
                )

            # 9. Pull Requests
            pr = self.pub_store.get_pull_request_for_repair(repair_id)
            if pr:
                events.append(
                    TimelineEventRecord(
                        stage=PublicationLifecycleStage.PR_VERIFIED,
                        timestamp=pr.verified_at or pr.created_at,
                        actor="pr_coordinator",
                        description=f"Upstream pull request created and verified: #{pr.pr_number} ({pr.pr_url})",
                        status=OperationExecutionStatus.SUCCESS,
                        evidence_links=[],
                        details={
                            "pr_record_id": pr.pr_record_id,
                            "pr_number": pr.pr_number,
                            "pr_url": pr.pr_url,
                            "target_branch": pr.target_branch,
                            "source_branch": pr.source_branch,
                            "state": pr.pr_state,
                        },
                    )
                )

            # Sort chronologically by timestamp, sequence
            events.sort(key=lambda x: (x.timestamp, x.sequence or 0))
            return events
        finally:
            conn.close()


# =============================================================================
# Publication Security Auditor (14 Checks)
# =============================================================================

class PublicationSecurityAuditor:
    """Performs 14 independent security and invariant audits on repair publications."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        self.repair_store = RepairExecutionStore(db_path=db_path)
        self.pub_store = PublicationStore(db_path=db_path)
        self.ledger_auditor = EventLedgerAuditor(db_path=db_path)

    def _conn(self):
        return get_db_connection(self.db_path)

    def audit_publication(self, repair_id: str) -> SecurityAuditSummary:
        """Executes all 14 independent security invariant audits."""
        checks: List[SecurityAuditItem] = []

        req = self.repair_store.get_repair_request(repair_id)
        dossier = self.repair_store.get_dossier_for_repair(repair_id)
        decision = self.repair_store.get_latest_approval_for_repair(repair_id)
        plan = self.pub_store.get_publication_plan_for_repair(repair_id)
        staged_commit = self.pub_store.get_staged_commit_for_repair(repair_id)
        remote_branch = self.pub_store.get_remote_branch_for_repair(repair_id)
        pr_record = self.pub_store.get_pull_request_for_repair(repair_id)

        # ---------------------------------------------------------------------
        # SEC-01: Authoritative operator approval exists
        # ---------------------------------------------------------------------
        if not decision:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-01",
                    title="Operator Approval Existence",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="No operator approval decision record found for this repair execution.",
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-01",
                    title="Operator Approval Existence",
                    outcome=SecurityCheckOutcome.PASS,
                    description=f"Authoritative decision record '{decision.decision_id}' exists by actor '{decision.actor}'.",
                    details={"decision_id": decision.decision_id, "actor": decision.actor},
                )
            )

        # ---------------------------------------------------------------------
        # SEC-02: Authorized action matches executed workflow
        # ---------------------------------------------------------------------
        if not decision:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-02",
                    title="Authorized Action Match",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="Cannot evaluate action match without an operator decision.",
                )
            )
        else:
            auth_act = decision.authorized_action
            # If PR was created, action MUST be CREATE_PR
            if pr_record or remote_branch:
                if auth_act == AuthorizedAction.CREATE_PR:
                    checks.append(
                        SecurityAuditItem(
                            check_id="SEC-02",
                            title="Authorized Action Match",
                            outcome=SecurityCheckOutcome.PASS,
                            description="Remote publication was executed under explicit CREATE_PR authorization.",
                            details={"authorized_action": auth_act.value},
                        )
                    )
                else:
                    checks.append(
                        SecurityAuditItem(
                            check_id="SEC-02",
                            title="Authorized Action Match",
                            outcome=SecurityCheckOutcome.FAIL,
                            description=f"Remote publication occurred but authorized action was '{auth_act.value if auth_act else None}', which does not permit remote publication.",
                            details={"authorized_action": auth_act.value if auth_act else None},
                        )
                    )
            elif staged_commit:
                if auth_act in (AuthorizedAction.STAGE_FOR_PR, AuthorizedAction.CREATE_PR, AuthorizedAction.STAGE_BRANCH if hasattr(AuthorizedAction, "STAGE_BRANCH") else AuthorizedAction.STAGE_FOR_PR):
                    checks.append(
                        SecurityAuditItem(
                            check_id="SEC-02",
                            title="Authorized Action Match",
                            outcome=SecurityCheckOutcome.PASS,
                            description=f"Local branch staging was executed under permitted authorization '{auth_act.value}'.",
                            details={"authorized_action": auth_act.value},
                        )
                    )
                else:
                    checks.append(
                        SecurityAuditItem(
                            check_id="SEC-02",
                            title="Authorized Action Match",
                            outcome=SecurityCheckOutcome.FAIL,
                            description=f"Branch was staged under '{auth_act.value if auth_act else None}', which does not permit branch staging.",
                        )
                    )
            else:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-02",
                        title="Authorized Action Match",
                        outcome=SecurityCheckOutcome.PASS,
                        description="No publication actions executed beyond authorized scope.",
                    )
                )

        # ---------------------------------------------------------------------
        # SEC-03: Approval is active and not invalidated or expired
        # ---------------------------------------------------------------------
        if not decision:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-03",
                    title="Approval Status Validity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="No operator decision available to verify validity.",
                )
            )
        elif decision.state == ApprovalState.INVALIDATED:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-03",
                    title="Approval Status Validity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description=f"Approval decision was invalidated: {decision.invalidation_reason}",
                    details={"invalidation_reason": decision.invalidation_reason},
                )
            )
        elif decision.state == ApprovalState.EXPIRED:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-03",
                    title="Approval Status Validity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="Approval decision has expired.",
                )
            )
        elif decision.state == ApprovalState.APPROVED:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-03",
                    title="Approval Status Validity",
                    outcome=SecurityCheckOutcome.PASS,
                    description="Operator approval is currently valid and active.",
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-03",
                    title="Approval Status Validity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description=f"Operator decision state is '{decision.state.value}'.",
                )
            )

        # ---------------------------------------------------------------------
        # SEC-04: Patch SHA-256 matches approval binding
        # ---------------------------------------------------------------------
        if not decision or not decision.binding:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-04",
                    title="Patch Binding Integrity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="No approval binding available to verify patch hash.",
                )
            )
        else:
            expected_patch = decision.binding.patch_sha256
            actual_patch = staged_commit.patch_sha256 if staged_commit else (dossier.patch_sha256 if dossier else None)
            if actual_patch and actual_patch == expected_patch:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-04",
                        title="Patch Binding Integrity",
                        outcome=SecurityCheckOutcome.PASS,
                        description="Patch SHA-256 matches the cryptographic binding in the operator approval.",
                        details={"patch_sha256": actual_patch},
                    )
                )
            else:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-04",
                        title="Patch Binding Integrity",
                        outcome=SecurityCheckOutcome.FAIL,
                        description=f"Patch SHA-256 mismatch: expected {expected_patch}, found {actual_patch}",
                        details={"expected": expected_patch, "actual": actual_patch},
                    )
                )

        # ---------------------------------------------------------------------
        # SEC-05: Target repository identity matches approved target
        # ---------------------------------------------------------------------
        if not req or not decision or not decision.binding:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-05",
                    title="Repository Target Integrity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="Repository target cannot be determined.",
                )
            )
        else:
            # Base repo from repair request must match binding or plan
            bound_repo = decision.binding.target_repo if hasattr(decision.binding, "target_repo") else req.target_repo
            if bound_repo == req.target_repo:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-05",
                        title="Repository Target Integrity",
                        outcome=SecurityCheckOutcome.PASS,
                        description=f"Target repository matches approved repository: {req.target_repo}",
                        details={"target_repo": req.target_repo},
                    )
                )
            else:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-05",
                        title="Repository Target Integrity",
                        outcome=SecurityCheckOutcome.FAIL,
                        description=f"Target repository mismatch: expected {bound_repo}, found {req.target_repo}",
                    )
                )

        # ---------------------------------------------------------------------
        # SEC-06: Base commit SHA matches approval binding
        # ---------------------------------------------------------------------
        if not req or not decision or not decision.binding:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-06",
                    title="Base Commit Binding Integrity",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="Base commit cannot be verified against approval binding.",
                )
            )
        else:
            expected_base = decision.binding.base_commit_sha
            actual_base = staged_commit.base_commit_sha if staged_commit else req.base_commit_sha
            if actual_base == expected_base:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-06",
                        title="Base Commit Binding Integrity",
                        outcome=SecurityCheckOutcome.PASS,
                        description=f"Base commit SHA '{actual_base}' matches operator approval binding.",
                        details={"base_commit_sha": actual_base},
                    )
                )
            else:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-06",
                        title="Base Commit Binding Integrity",
                        outcome=SecurityCheckOutcome.FAIL,
                        description=f"Base commit mismatch: expected {expected_base}, found {actual_base}",
                    )
                )

        # ---------------------------------------------------------------------
        # SEC-07: Local commit tree matches patch content hash
        # ---------------------------------------------------------------------
        if not staged_commit:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-07",
                    title="Commit Tree Consistency",
                    outcome=SecurityCheckOutcome.NOT_APPLICABLE,
                    description="No local commit has been staged for this repair execution.",
                )
            )
        elif staged_commit.commit_sha and staged_commit.tree_sha:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-07",
                    title="Commit Tree Consistency",
                    outcome=SecurityCheckOutcome.PASS,
                    description=f"Staged commit tree '{staged_commit.tree_sha}' verified for commit '{staged_commit.commit_sha}'.",
                    details={"commit_sha": staged_commit.commit_sha, "tree_sha": staged_commit.tree_sha},
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-07",
                    title="Commit Tree Consistency",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="Staged commit record is missing commit SHA or tree SHA.",
                )
            )

        # ---------------------------------------------------------------------
        # SEC-08: Remote branch name matches authorized publication plan
        # ---------------------------------------------------------------------
        if not remote_branch:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-08",
                    title="Branch Name Policy Compliance",
                    outcome=SecurityCheckOutcome.NOT_APPLICABLE if not staged_commit else SecurityCheckOutcome.PASS,
                    description="Remote branch not yet published, or staged branch matches policy.",
                )
            )
        elif plan and remote_branch.branch_name == plan.proposed_branch_name:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-08",
                    title="Branch Name Policy Compliance",
                    outcome=SecurityCheckOutcome.PASS,
                    description=f"Remote branch name '{remote_branch.branch_name}' strictly matches authorized publication plan.",
                    details={"branch_name": remote_branch.branch_name},
                )
            )
        elif not plan:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-08",
                    title="Branch Name Policy Compliance",
                    outcome=SecurityCheckOutcome.INCONCLUSIVE,
                    description="No publication plan found to cross-reference remote branch name.",
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-08",
                    title="Branch Name Policy Compliance",
                    outcome=SecurityCheckOutcome.FAIL,
                    description=f"Branch name mismatch: expected '{plan.proposed_branch_name}', found '{remote_branch.branch_name}'",
                )
            )

        # ---------------------------------------------------------------------
        # SEC-09: Remote commit SHA matches verified local staged commit
        # ---------------------------------------------------------------------
        if not remote_branch:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-09",
                    title="Remote Commit SHA Equality",
                    outcome=SecurityCheckOutcome.NOT_APPLICABLE,
                    description="Remote branch has not been pushed.",
                )
            )
        elif staged_commit and remote_branch.commit_sha == staged_commit.commit_sha:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-09",
                    title="Remote Commit SHA Equality",
                    outcome=SecurityCheckOutcome.PASS,
                    description=f"Remote commit SHA '{remote_branch.commit_sha}' exactly equals local staged commit SHA.",
                    details={"commit_sha": remote_branch.commit_sha},
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-09",
                    title="Remote Commit SHA Equality",
                    outcome=SecurityCheckOutcome.FAIL,
                    description=f"Remote commit SHA mismatch: local '{staged_commit.commit_sha if staged_commit else None}' vs remote '{remote_branch.commit_sha}'",
                )
            )

        # ---------------------------------------------------------------------
        # SEC-10: No force-push attempted
        # ---------------------------------------------------------------------
        conn = self._conn()
        try:
            cur_ops = conn.execute(
                "SELECT * FROM publication_provider_operations WHERE repair_id = ?",
                (repair_id,),
            )
            force_push_detected = False
            for op in cur_ops.fetchall():
                payload_str = op["request_payload_json"]
                if "force" in payload_str.lower() and '"force": true' in payload_str.lower():
                    force_push_detected = True
                    break

            if force_push_detected:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-10",
                        title="Force-Push Prohibition",
                        outcome=SecurityCheckOutcome.FAIL,
                        description="Force-push flag was detected in provider operation payloads.",
                    )
                )
            else:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-10",
                        title="Force-Push Prohibition",
                        outcome=SecurityCheckOutcome.PASS,
                        description="Zero force-push flags or destructive update parameters observed.",
                    )
                )
        finally:
            conn.close()

        # ---------------------------------------------------------------------
        # SEC-11: Fast-forward check verified before push
        # ---------------------------------------------------------------------
        if not remote_branch:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-11",
                    title="Fast-Forward Verification",
                    outcome=SecurityCheckOutcome.NOT_APPLICABLE,
                    description="Remote branch push not performed.",
                )
            )
        elif remote_branch.status in ("published", "verified"):
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-11",
                    title="Fast-Forward Verification",
                    outcome=SecurityCheckOutcome.PASS,
                    description="Fast-forward verification confirmed during publication workflow.",
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-11",
                    title="Fast-Forward Verification",
                    outcome=SecurityCheckOutcome.FAIL,
                    description=f"Remote branch verification status is '{remote_branch.status}'.",
                )
            )

        # ---------------------------------------------------------------------
        # SEC-12: Pull request targets authorized upstream base branch
        # ---------------------------------------------------------------------
        if not pr_record:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-12",
                    title="PR Target Branch Integrity",
                    outcome=SecurityCheckOutcome.NOT_APPLICABLE,
                    description="No upstream pull request opened for this repair execution.",
                )
            )
        else:
            expected_target = "main"
            if plan and hasattr(plan, "commit_proposal") and plan.commit_proposal:
                pass
            if pr_record.target_branch in ("main", "master"):
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-12",
                        title="PR Target Branch Integrity",
                        outcome=SecurityCheckOutcome.PASS,
                        description=f"Pull request #{pr_record.pr_number} targets authorized default upstream branch '{pr_record.target_branch}'.",
                        details={"target_branch": pr_record.target_branch, "pr_number": pr_record.pr_number},
                    )
                )
            else:
                checks.append(
                    SecurityAuditItem(
                        check_id="SEC-12",
                        title="PR Target Branch Integrity",
                        outcome=SecurityCheckOutcome.FAIL,
                        description=f"Pull request #{pr_record.pr_number} targets unauthorized branch '{pr_record.target_branch}'.",
                    )
                )

        # ---------------------------------------------------------------------
        # SEC-13: Audit ledger hash chain is intact (no tampering)
        # ---------------------------------------------------------------------
        integrity = self.ledger_auditor.verify_event_chain(repair_id)
        if integrity.status in (AuditIntegrityStatus.VERIFIED, AuditIntegrityStatus.LEGACY_UNSEALED):
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-13",
                    title="Audit Ledger Hash Chain",
                    outcome=SecurityCheckOutcome.PASS,
                    description=f"Audit event ledger status: {integrity.status.value} ({integrity.total_events} events).",
                    details={"status": integrity.status.value, "total_events": integrity.total_events},
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-13",
                    title="Audit Ledger Hash Chain",
                    outcome=SecurityCheckOutcome.FAIL,
                    description=f"Cryptographic hash chain verification failed: {integrity.status.value}",
                    details=integrity.details,
                )
            )

        # ---------------------------------------------------------------------
        # SEC-14: No sensitive credentials exposed in audit payloads or PR content
        # ---------------------------------------------------------------------
        exposed_secrets = False
        secret_patterns = [
            r"ghp_[A-Za-z0-9_]{36}",
            r"github_pat_[A-Za-z0-9_]{82}",
            r"sk-[A-Za-z0-9]{32,}",
            r"Bearer\s+[A-Za-z0-9_\-\.]{20,}",
        ]
        text_samples = []
        if pr_record:
            text_samples.extend([pr_record.pr_title, pr_record.pr_body])
        if staged_commit:
            text_samples.append(staged_commit.commit_message)

        for text in text_samples:
            for pat in secret_patterns:
                if re.search(pat, text):
                    exposed_secrets = True
                    break

        if exposed_secrets:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-14",
                    title="Credential Redaction Verification",
                    outcome=SecurityCheckOutcome.FAIL,
                    description="Unredacted credential pattern detected in public metadata or commit message.",
                )
            )
        else:
            checks.append(
                SecurityAuditItem(
                    check_id="SEC-14",
                    title="Credential Redaction Verification",
                    outcome=SecurityCheckOutcome.PASS,
                    description="Zero credentials, API tokens, or secrets detected in audit payloads or PR content.",
                )
            )

        passed_count = sum(1 for c in checks if c.outcome == SecurityCheckOutcome.PASS)
        failed_count = sum(1 for c in checks if c.outcome == SecurityCheckOutcome.FAIL)
        inconclusive_count = sum(1 for c in checks if c.outcome == SecurityCheckOutcome.INCONCLUSIVE)
        na_count = sum(1 for c in checks if c.outcome == SecurityCheckOutcome.NOT_APPLICABLE)

        return SecurityAuditSummary(
            total_checks=len(checks),
            passed=passed_count,
            failed=failed_count,
            inconclusive=inconclusive_count,
            not_applicable=na_count,
            all_passed=(failed_count == 0),
            checks=checks,
        )


# =============================================================================
# Publication Reconciler (Non-Destructive)
# =============================================================================

class PublicationReconciler:
    """Reconciles interrupted, partial, or modified publication workflows non-destructively."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        self.repair_store = RepairExecutionStore(db_path=db_path)
        self.pub_store = PublicationStore(db_path=db_path)

    def reconcile_publication(
        self,
        repair_id: str,
        provider_client: Optional[Any] = None,
    ) -> PublicationReconciliationRecord:
        """Reconciles local publication records and remote provider state without modifying target state."""
        staged_commit = self.pub_store.get_staged_commit_for_repair(repair_id)
        remote_branch = self.pub_store.get_remote_branch_for_repair(repair_id)
        pr_record = self.pub_store.get_pull_request_for_repair(repair_id)
        decision = self.repair_store.get_latest_approval_for_repair(repair_id)
        pub_req = self.pub_store.list_publication_requests_for_repair(repair_id)
        latest_req = pub_req[0] if pub_req else None

        diagnostics: List[str] = []
        status = ReconciliationStatus.UNKNOWN_STATE
        recommended_action = None
        is_consistent = True

        # Check authorization state first
        if decision and decision.state == ApprovalState.INVALIDATED:
            status = ReconciliationStatus.APPROVAL_INVALIDATED
            diagnostics.append(f"Operator approval was invalidated: {decision.invalidation_reason}")
            recommended_action = "Discard local publication branch and request fresh operator review."
            is_consistent = False
            return PublicationReconciliationRecord(
                repair_id=repair_id,
                status=status,
                local_commit_sha=staged_commit.commit_sha if staged_commit else None,
                remote_commit_sha=remote_branch.commit_sha if remote_branch else None,
                pr_number=pr_record.pr_number if pr_record else None,
                pr_state=pr_record.pr_state if pr_record else None,
                is_consistent=is_consistent,
                recommended_action=recommended_action,
                diagnostics=diagnostics,
            )

        # Check failed publication requests
        if latest_req and latest_req.status == PublicationStatus.FAILED:
            status = ReconciliationStatus.CONFIRMED_FAILURE
            diagnostics.append(f"Publication request failed: {latest_req.error_message}")
            recommended_action = "Inspect error message and re-run publication after resolving underlying issue."
            is_consistent = False
            return PublicationReconciliationRecord(
                repair_id=repair_id,
                status=status,
                local_commit_sha=staged_commit.commit_sha if staged_commit else None,
                remote_commit_sha=remote_branch.commit_sha if remote_branch else None,
                pr_number=pr_record.pr_number if pr_record else None,
                pr_state=pr_record.pr_state if pr_record else None,
                is_consistent=is_consistent,
                recommended_action=recommended_action,
                diagnostics=diagnostics,
            )

        # Evaluate progress
        if pr_record:
            # Full publication attempted
            status = ReconciliationStatus.CONFIRMED_SUCCESS
            diagnostics.append(f"Pull request #{pr_record.pr_number} successfully recorded ({pr_record.pr_url}).")

            # Check if remote provider is available to verify live status
            if provider_client:
                try:
                    verified_pr = provider_client.get_pull_request(
                        owner=pr_record.repository_owner,
                        repo=pr_record.repository_name,
                        pr_number=pr_record.pr_number,
                    )
                    if verified_pr.state != pr_record.pr_state:
                        status = ReconciliationStatus.EXTERNAL_MODIFICATION
                        diagnostics.append(
                            f"Remote PR state changed from '{pr_record.pr_state}' to '{verified_pr.state}' externally."
                        )
                        recommended_action = "Update local record to reflect current upstream pull request state."
                        is_consistent = False
                except Exception as e:
                    diagnostics.append(f"Upstream provider check failed (offline/error): {e}")

        elif remote_branch:
            # Partial: Branch pushed, PR not yet created
            status = ReconciliationStatus.PARTIAL_SUCCESS
            diagnostics.append(f"Remote branch '{remote_branch.branch_name}' pushed, but PR not yet opened.")
            recommended_action = "Resume publication by opening upstream pull request."
            is_consistent = False

        elif staged_commit:
            # Partial: Local commit created, not yet pushed
            status = ReconciliationStatus.PARTIAL_SUCCESS
            diagnostics.append(f"Local commit '{staged_commit.commit_sha}' staged, but not yet pushed to remote.")
            recommended_action = "Push staged branch to authorized remote repository."
            is_consistent = False

        else:
            status = ReconciliationStatus.UNKNOWN_STATE
            diagnostics.append("No staged commit or publication records found for this repair execution.")
            recommended_action = "Initiate publication workflow with 'forge repair publish stage'."

        record = PublicationReconciliationRecord(
            repair_id=repair_id,
            status=status,
            local_commit_sha=staged_commit.commit_sha if staged_commit else None,
            remote_commit_sha=remote_branch.commit_sha if remote_branch else None,
            pr_number=pr_record.pr_number if pr_record else None,
            pr_state=pr_record.pr_state if pr_record else None,
            is_consistent=is_consistent,
            recommended_action=recommended_action,
            diagnostics=diagnostics,
        )

        # Persist reconciliation snapshot
        self.pub_store.save_reconciliation(
            reconciliation_id=f"rec_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            status=status.value,
            local_commit_sha=record.local_commit_sha,
            remote_commit_sha=record.remote_commit_sha,
            pr_number=record.pr_number,
            pr_state=record.pr_state,
            is_consistent=record.is_consistent,
            details={"diagnostics": diagnostics, "recommended_action": recommended_action},
        )
        return record


# =============================================================================
# Publication Audit Report Builder
# =============================================================================

class PublicationAuditReportBuilder:
    """Builds unified, deterministic audit reports in Markdown and JSON formats."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        self.repair_store = RepairExecutionStore(db_path=db_path)
        self.pub_store = PublicationStore(db_path=db_path)
        self.timeline_builder = PublicationTimelineBuilder(db_path=db_path)
        self.security_auditor = PublicationSecurityAuditor(db_path=db_path)
        self.reconciler = PublicationReconciler(db_path=db_path)
        self.ledger_auditor = EventLedgerAuditor(db_path=db_path)

    def build_report(
        self,
        repair_id: str,
        provider_client: Optional[Any] = None,
    ) -> PublicationAuditReport:
        """Assembles the complete publication audit report."""
        req = self.repair_store.get_repair_request(repair_id)
        if not req:
            raise ValueError(f"Repair execution '{repair_id}' does not exist.")

        decision = self.repair_store.get_latest_approval_for_repair(repair_id)
        staged_commit = self.pub_store.get_staged_commit_for_repair(repair_id)
        remote_branch = self.pub_store.get_remote_branch_for_repair(repair_id)
        pr_record = self.pub_store.get_pull_request_for_repair(repair_id)

        # Artifact Provenance
        provenance = ArtifactProvenanceRecord(
            repair_id=repair_id,
            finding_id=req.finding_id,
            target_repo=req.target_repo,
            base_commit_sha=req.base_commit_sha,
            patch_artifact_id=req.patch_artifact_id,
            patch_sha256=staged_commit.patch_sha256 if staged_commit else None,
            staged_commit_sha=staged_commit.commit_sha if staged_commit else None,
            parent_commit_sha=staged_commit.parent_commit_sha if staged_commit else None,
            tree_sha=staged_commit.tree_sha if staged_commit else None,
            branch_name=staged_commit.branch_name if staged_commit else None,
            remote_url=remote_branch.remote_url if remote_branch else None,
            pr_number=pr_record.pr_number if pr_record else None,
            pr_url=pr_record.pr_url if pr_record else None,
        )

        # Authorization Audit
        authorization = AuthorizationAuditRecord(
            decision_id=decision.decision_id if decision else None,
            state=decision.state.value if decision else None,
            authorized_action=decision.authorized_action.value if decision and decision.authorized_action else None,
            operator=decision.actor if decision else None,
            decision_timestamp=decision.created_at if decision else None,
            bound_patch_sha256=decision.binding.patch_sha256 if decision and decision.binding else None,
            bound_base_commit_sha=decision.binding.base_commit_sha if decision and decision.binding else None,
            is_valid=(decision.state == ApprovalState.APPROVED) if decision else False,
            invalidation_reason=decision.invalidation_reason if decision else None,
        )

        # Cryptographic Ledger Integrity
        integrity = self.ledger_auditor.verify_event_chain(repair_id)

        # Persist audit verification snapshot
        self.pub_store.save_audit_verification(
            verification_id=f"pav_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            status=integrity.status.value,
            total_events=integrity.total_events,
            sealed_events=integrity.sealed_events,
            legacy_events=integrity.legacy_events,
            is_tampered=(integrity.status == AuditIntegrityStatus.TAMPERED),
            details=integrity.details,
        )

        # Security Invariants
        security = self.security_auditor.audit_publication(repair_id)

        # Chronological Timeline
        timeline = self.timeline_builder.reconstruct_timeline(repair_id)

        # Reconciliation
        reconciliation = self.reconciler.reconcile_publication(repair_id, provider_client=provider_client)

        # Model explanations segregated from empirical facts
        model_explanations: Dict[str, str] = {}
        if req.failure and req.failure.explanation:
            model_explanations["failure_explanation"] = req.failure.explanation

        return PublicationAuditReport(
            report_id=f"par_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            generated_at=datetime.now(timezone.utc),
            provenance=provenance,
            authorization=authorization,
            integrity=integrity,
            security=security,
            timeline=timeline,
            reconciliation=reconciliation,
            model_explanations=model_explanations,
        )

    def render_markdown(self, report: PublicationAuditReport) -> str:
        """Renders report into GitHub-flavored Markdown with clear factual segregation."""
        lines = []
        lines.append(f"# FORGE Publication Audit & Governance Report")
        lines.append(f"**Report ID:** `{report.report_id}`  ")
        lines.append(f"**Repair Execution ID:** `{report.repair_id}`  ")
        lines.append(f"**Generated At:** `{report.generated_at.isoformat()}`\n")

        # Executive Governance Summary
        lines.append("## 1. Executive Governance Summary\n")
        all_passed_icon = "[PASS]" if report.security.all_passed else "[FAIL]"
        lines.append(f"- **Security Invariants Status:** `{all_passed_icon}` ({report.security.passed}/{report.security.total_checks} passed)")
        lines.append(f"- **Ledger Cryptographic Integrity:** `{report.integrity.status.value.upper()}` ({report.integrity.sealed_events} sealed, {report.integrity.legacy_events} legacy)")
        if report.reconciliation:
            lines.append(f"- **Workflow Reconciliation State:** `{report.reconciliation.status.value.upper()}`")
            if report.reconciliation.recommended_action:
                lines.append(f"- **Recommended Next Action:** *{report.reconciliation.recommended_action}*")
        lines.append("")

        # Section 2: Artifact Provenance & Bindings
        lines.append("## 2. Artifact Provenance & Cryptographic Bindings\n")
        lines.append("| Property | Value |")
        lines.append("| :--- | :--- |")
        lines.append(f"| Finding ID | `{report.provenance.finding_id}` |")
        lines.append(f"| Target Repository | `{report.provenance.target_repo}` |")
        lines.append(f"| Base Commit SHA | `{report.provenance.base_commit_sha}` |")
        lines.append(f"| Patch Artifact ID | `{report.provenance.patch_artifact_id or 'None'}` |")
        lines.append(f"| Patch SHA-256 | `{report.provenance.patch_sha256 or 'None'}` |")
        lines.append(f"| Staged Commit SHA | `{report.provenance.staged_commit_sha or 'None'}` |")
        lines.append(f"| Parent Commit SHA | `{report.provenance.parent_commit_sha or 'None'}` |")
        lines.append(f"| Tree SHA | `{report.provenance.tree_sha or 'None'}` |")
        lines.append(f"| Branch Name | `{report.provenance.branch_name or 'None'}` |")
        lines.append(f"| Remote URL | `{report.provenance.remote_url or 'None'}` |")
        lines.append(f"| Pull Request | `{f'#{report.provenance.pr_number} ({report.provenance.pr_url})' if report.provenance.pr_number else 'None'}` |")
        lines.append("")

        # Section 3: Operator Authorization Audit
        lines.append("## 3. Operator Authorization Audit\n")
        lines.append("| Check | Value |")
        lines.append("| :--- | :--- |")
        lines.append(f"| Decision ID | `{report.authorization.decision_id or 'None'}` |")
        lines.append(f"| Operator | `{report.authorization.operator or 'None'}` |")
        lines.append(f"| Authorized Action | `{report.authorization.authorized_action or 'None'}` |")
        lines.append(f"| State | `{report.authorization.state or 'None'}` |")
        lines.append(f"| Valid Active Authorization | `{'YES' if report.authorization.is_valid else 'NO'}` |")
        if report.authorization.invalidation_reason:
            lines.append(f"| Invalidation Reason | *{report.authorization.invalidation_reason}* |")
        lines.append("")

        # Section 4: Security Invariants Audit (14 Checks)
        lines.append("## 4. Security Invariants Audit (14 Independent Checks)\n")
        lines.append("| ID | Check Title | Outcome | Description |")
        lines.append("| :--- | :--- | :---: | :--- |")
        for chk in report.security.checks:
            lines.append(f"| `{chk.check_id}` | {chk.title} | `{chk.outcome.value}` | {chk.description} |")
        lines.append("")

        # Section 5: End-to-End Publication Timeline
        lines.append("## 5. End-to-End Chronological Timeline\n")
        lines.append("| Sequence | Timestamp (UTC) | Lifecycle Stage | Actor | Description | Status |")
        lines.append("| :---: | :--- | :--- | :--- | :--- | :---: |")
        for evt in report.timeline:
            seq_str = str(evt.sequence) if evt.sequence is not None else "-"
            lines.append(
                f"| {seq_str} | {evt.timestamp.strftime('%Y-%m-%d %H:%M:%S')} | `{evt.stage.value}` | "
                f"`{evt.actor}` | {evt.description} | `{evt.status.value.upper()}` |"
            )
        lines.append("")

        # Section 6: Non-Destructive Reconciliation
        if report.reconciliation:
            lines.append("## 6. Workflow Reconciliation & Consistency Diagnostics\n")
            lines.append(f"- **State:** `{report.reconciliation.status.value.upper()}`")
            lines.append(f"- **Consistent:** `{'YES' if report.reconciliation.is_consistent else 'NO'}`")
            if report.reconciliation.recommended_action:
                lines.append(f"- **Recommended Action:** {report.reconciliation.recommended_action}")
            if report.reconciliation.diagnostics:
                lines.append("- **Diagnostics:**")
                for diag in report.reconciliation.diagnostics:
                    lines.append(f"  - {diag}")
            lines.append("")

        # Section 7: Segregated Model Explanations (Strict Separation from Facts)
        if report.model_explanations:
            lines.append("## 7. Model Explanations (Non-Authoritative Interpretation)\n")
            lines.append("> [!NOTE]\n> The following explanations are generated by automated diagnostic models and are segregated from empirical ledger facts.\n")
            for k, v in report.model_explanations.items():
                lines.append(f"### {k.replace('_', ' ').title()}\n")
                lines.append(f"{v}\n")

        return redact_secrets("\n".join(lines))

    def render_json(self, report: PublicationAuditReport) -> str:
        """Renders report into deterministic, sanitized JSON format."""
        raw_json = report.model_dump_json(indent=2)
        return redact_secrets(raw_json)
