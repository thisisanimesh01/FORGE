"""Phase 9 Git Publication Architecture — Persistent Storage.

Manages SQLite persistence for publication requests, publication plans,
repository identity snapshots, and sequential audit ledger integration.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.config import redact_secrets
from forge.publication.interfaces import PublicationAuditStoreProtocol
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
    PublicationAction,
    PublicationExecutionState,
    PublicationPlan,
    PublicationRequest,
    PublicationStatus,
    RepositoryIdentity,
    StagedCommitRecord,
    WorkspaceIntegritySnapshot,
)
from forge.repair.execution.models import (
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionState,
)
from forge.storage.database import get_db_connection, init_database


class PublicationStore(PublicationAuditStoreProtocol):
    """SQLite-backed storage manager for publication requests, plans, and audit events."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        init_database(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    # -------------------------------------------------------------------------
    # Publication Request Persistence
    # -------------------------------------------------------------------------

    def save_publication_request(self, request: PublicationRequest) -> None:
        """Persist or update a PublicationRequest."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_requests (
                        publication_id, repair_id, decision_id, action, status,
                        plan_id, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(publication_id) DO UPDATE SET
                        status = excluded.status,
                        plan_id = excluded.plan_id,
                        error_message = excluded.error_message,
                        updated_at = excluded.updated_at
                    """,
                    (
                        request.publication_id,
                        request.repair_id,
                        request.decision_id,
                        request.action.value,
                        request.status.value,
                        request.plan_id,
                        request.error_message,
                        request.created_at.isoformat(),
                        request.updated_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def get_publication_request(self, publication_id: str) -> Optional[PublicationRequest]:
        """Fetch a PublicationRequest by publication_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_requests WHERE publication_id = ?",
                (publication_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return PublicationRequest(
                publication_id=row["publication_id"],
                repair_id=row["repair_id"],
                decision_id=row["decision_id"],
                action=PublicationAction(row["action"]),
                status=PublicationStatus(row["status"]),
                plan_id=row["plan_id"],
                error_message=row["error_message"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        finally:
            conn.close()

    def list_publication_requests_for_repair(self, repair_id: str) -> List[PublicationRequest]:
        """List all publication requests for a given repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_requests WHERE repair_id = ? ORDER BY created_at DESC",
                (repair_id,),
            )
            return [
                PublicationRequest(
                    publication_id=row["publication_id"],
                    repair_id=row["repair_id"],
                    decision_id=row["decision_id"],
                    action=PublicationAction(row["action"]),
                    status=PublicationStatus(row["status"]),
                    plan_id=row["plan_id"],
                    error_message=row["error_message"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Publication Plan Persistence
    # -------------------------------------------------------------------------

    def save_publication_plan(self, plan: PublicationPlan) -> None:
        """Persist a PublicationPlan."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_plans (
                        plan_id, publication_id, repair_id, decision_id, finding_id,
                        repository_target, base_commit_sha, proposed_branch_name,
                        commit_proposal_json, patch_artifact_id, patch_sha256,
                        authorized_action, policy_version, repository_identity_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id) DO UPDATE SET
                        proposed_branch_name = excluded.proposed_branch_name,
                        commit_proposal_json = excluded.commit_proposal_json,
                        repository_identity_json = excluded.repository_identity_json
                    """,
                    (
                        plan.plan_id,
                        plan.publication_id,
                        plan.repair_id,
                        plan.decision_id,
                        plan.finding_id,
                        plan.repository_target,
                        plan.base_commit_sha,
                        plan.proposed_branch_name,
                        plan.commit_proposal.model_dump_json(),
                        plan.patch_artifact_id,
                        plan.patch_sha256,
                        plan.authorized_action.value,
                        plan.policy_version,
                        plan.repository_identity.model_dump_json(),
                        plan.created_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def get_publication_plan(self, plan_id: str) -> Optional[PublicationPlan]:
        """Fetch a PublicationPlan by plan_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_plans WHERE plan_id = ?",
                (plan_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return PublicationPlan(
                plan_id=row["plan_id"],
                publication_id=row["publication_id"],
                repair_id=row["repair_id"],
                decision_id=row["decision_id"],
                finding_id=row["finding_id"],
                repository_target=row["repository_target"],
                base_commit_sha=row["base_commit_sha"],
                proposed_branch_name=row["proposed_branch_name"],
                commit_proposal=CommitProposal.model_validate_json(row["commit_proposal_json"]),
                patch_artifact_id=row["patch_artifact_id"],
                patch_sha256=row["patch_sha256"],
                authorized_action=PublicationAction(row["authorized_action"]),
                policy_version=row["policy_version"],
                repository_identity=RepositoryIdentity.model_validate_json(row["repository_identity_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        finally:
            conn.close()

    def get_publication_plan_for_request(self, publication_id: str) -> Optional[PublicationPlan]:
        """Fetch the PublicationPlan associated with a publication_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_plans WHERE publication_id = ? ORDER BY created_at DESC LIMIT 1",
                (publication_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return PublicationPlan(
                plan_id=row["plan_id"],
                publication_id=row["publication_id"],
                repair_id=row["repair_id"],
                decision_id=row["decision_id"],
                finding_id=row["finding_id"],
                repository_target=row["repository_target"],
                base_commit_sha=row["base_commit_sha"],
                proposed_branch_name=row["proposed_branch_name"],
                commit_proposal=CommitProposal.model_validate_json(row["commit_proposal_json"]),
                patch_artifact_id=row["patch_artifact_id"],
                patch_sha256=row["patch_sha256"],
                authorized_action=PublicationAction(row["authorized_action"]),
                policy_version=row["policy_version"],
                repository_identity=RepositoryIdentity.model_validate_json(row["repository_identity_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        finally:
            conn.close()

    def list_publication_plans_for_repair(self, repair_id: str) -> List[PublicationPlan]:
        """List all publication plans for a given repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_plans WHERE repair_id = ? ORDER BY created_at DESC",
                (repair_id,),
            )
            return [
                PublicationPlan(
                    plan_id=row["plan_id"],
                    publication_id=row["publication_id"],
                    repair_id=row["repair_id"],
                    decision_id=row["decision_id"],
                    finding_id=row["finding_id"],
                    repository_target=row["repository_target"],
                    base_commit_sha=row["base_commit_sha"],
                    proposed_branch_name=row["proposed_branch_name"],
                    commit_proposal=CommitProposal.model_validate_json(row["commit_proposal_json"]),
                    patch_artifact_id=row["patch_artifact_id"],
                    patch_sha256=row["patch_sha256"],
                    authorized_action=PublicationAction(row["authorized_action"]),
                    policy_version=row["policy_version"],
                    repository_identity=RepositoryIdentity.model_validate_json(row["repository_identity_json"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Audit Event Ledger Integration
    # -------------------------------------------------------------------------

    def record_publication_event(
        self,
        repair_id: str,
        action: str,
        actor: RepairActor,
        payload: Dict[str, Any],
        from_state: Optional[RepairExecutionState] = None,
        to_state: Optional[RepairExecutionState] = None,
    ) -> None:
        """Record an explicit publication audit event into the repair_execution_events ledger."""
        conn = self._conn()
        try:
            with conn:
                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM repair_execution_events WHERE repair_id = ?",
                    (repair_id,),
                ).fetchone()[0]

                clean_payload = {
                    k: redact_secrets(str(v)) if isinstance(v, str) else v
                    for k, v in payload.items()
                }
                if "action" in clean_payload and clean_payload["action"] != action:
                    clean_payload["target_action"] = clean_payload["action"]
                clean_payload["action"] = action

                now_str = datetime.now(timezone.utc).isoformat()
                event_id = f"ree_{uuid.uuid4().hex[:12]}"

                # Fetch current repair state if not provided
                cur_st = to_state
                if not cur_st:
                    req_row = conn.execute(
                        "SELECT state FROM repair_executions WHERE repair_id = ?",
                        (repair_id,),
                    ).fetchone()
                    if req_row:
                        cur_st = RepairExecutionState(req_row["state"])
                    else:
                        cur_st = RepairExecutionState.VERIFIED

                actor_val = actor.value if hasattr(actor, "value") else str(actor)

                conn.execute(
                    """
                    INSERT INTO repair_execution_events (
                        event_id, repair_id, sequence, from_state, to_state, actor, payload_json, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        repair_id,
                        cur_seq,
                        from_state.value if from_state else None,
                        cur_st.value,
                        actor_val,
                        json.dumps(clean_payload),
                        now_str,
                    ),
                )
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Staged Commit Persistence (Milestone 2)
    # -------------------------------------------------------------------------

    def save_staged_commit(self, record: StagedCommitRecord) -> None:
        """Persist or update a StagedCommitRecord."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_staged_commits (
                        staging_id, workspace_id, plan_id, repair_id, decision_id,
                        finding_id, branch_name, base_commit_sha, commit_sha,
                        tree_sha, parent_commit_sha, patch_artifact_id, patch_sha256,
                        changed_files_json, author_json, committer_json, commit_message,
                        state, failure_reason, workspace_path, target_repo_path,
                        workspace_integrity_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(staging_id) DO UPDATE SET
                        commit_sha = excluded.commit_sha,
                        tree_sha = excluded.tree_sha,
                        parent_commit_sha = excluded.parent_commit_sha,
                        changed_files_json = excluded.changed_files_json,
                        state = excluded.state,
                        failure_reason = excluded.failure_reason,
                        workspace_integrity_json = excluded.workspace_integrity_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record.staging_id,
                        record.workspace_id,
                        record.plan_id,
                        record.repair_id,
                        record.decision_id,
                        record.finding_id,
                        record.branch_name,
                        record.base_commit_sha,
                        record.commit_sha,
                        record.tree_sha,
                        record.parent_commit_sha,
                        record.patch_artifact_id,
                        record.patch_sha256,
                        json.dumps(record.changed_files),
                        record.author.model_dump_json(),
                        record.committer.model_dump_json(),
                        record.commit_message,
                        record.state.value,
                        record.failure_reason,
                        record.workspace_path,
                        record.target_repo_path,
                        record.workspace_integrity.model_dump_json() if record.workspace_integrity else "{}",
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def _row_to_staged_commit(self, row: sqlite3.Row) -> StagedCommitRecord:
        """Hydrate StagedCommitRecord from sqlite3.Row."""
        integrity_obj = None
        if row["workspace_integrity_json"] and row["workspace_integrity_json"] != "{}":
            try:
                integrity_obj = WorkspaceIntegritySnapshot.model_validate_json(row["workspace_integrity_json"])
            except Exception:
                pass

        return StagedCommitRecord(
            staging_id=row["staging_id"],
            workspace_id=row["workspace_id"],
            plan_id=row["plan_id"],
            repair_id=row["repair_id"],
            decision_id=row["decision_id"],
            finding_id=row["finding_id"],
            branch_name=row["branch_name"],
            base_commit_sha=row["base_commit_sha"],
            commit_sha=row["commit_sha"],
            tree_sha=row["tree_sha"],
            parent_commit_sha=row["parent_commit_sha"],
            patch_artifact_id=row["patch_artifact_id"],
            patch_sha256=row["patch_sha256"],
            changed_files=json.loads(row["changed_files_json"]),
            author=CommitAuthor.model_validate_json(row["author_json"]),
            committer=CommitAuthor.model_validate_json(row["committer_json"]),
            commit_message=row["commit_message"],
            state=PublicationExecutionState(row["state"]),
            failure_reason=row["failure_reason"],
            workspace_path=row["workspace_path"],
            target_repo_path=row["target_repo_path"],
            workspace_integrity=integrity_obj,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_staged_commit(self, staging_id: str) -> Optional[StagedCommitRecord]:
        """Fetch StagedCommitRecord by staging_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_staged_commits WHERE staging_id = ?",
                (staging_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_staged_commit(row)
        finally:
            conn.close()

    def get_staged_commit_for_repair(self, repair_id: str) -> Optional[StagedCommitRecord]:
        """Fetch the latest StagedCommitRecord for a repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_staged_commits WHERE repair_id = ? ORDER BY created_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_staged_commit(row)
        finally:
            conn.close()

    def list_staged_commits(self, repair_id: Optional[str] = None) -> List[StagedCommitRecord]:
        """List StagedCommitRecords, optionally filtered by repair_id."""
        conn = self._conn()
        try:
            if repair_id:
                cur = conn.execute(
                    "SELECT * FROM publication_staged_commits WHERE repair_id = ? ORDER BY created_at DESC",
                    (repair_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM publication_staged_commits ORDER BY created_at DESC"
                )
            return [self._row_to_staged_commit(row) for row in cur.fetchall()]
        finally:
            conn.close()
