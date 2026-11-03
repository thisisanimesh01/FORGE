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
from forge.publication.audit_crypto import compute_event_hash
from forge.publication.interfaces import PublicationAuditStoreProtocol
from forge.publication.models import (
    CommitAuthor,
    CommitProposal,
    ProviderOperationRecord,
    PublicationAction,
    PublicationExecutionState,
    PublicationPlan,
    PublicationRequest,
    PublicationStatus,
    PullRequestRecord,
    RemoteBranchRecord,
    RemotePublicationRecord,
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

    def get_publication_plan_for_repair(self, repair_id: str) -> Optional[PublicationPlan]:
        """Fetch the latest PublicationPlan for a given repair_id."""
        plans = self.list_publication_plans_for_repair(repair_id)
        return plans[0] if plans else None

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
                from_str = from_state.value if from_state else None
                to_str = cur_st.value if hasattr(cur_st, "value") else str(cur_st)

                cur_prev = conn.execute(
                    "SELECT event_hash FROM repair_execution_events WHERE repair_id = ? ORDER BY sequence DESC LIMIT 1",
                    (repair_id,),
                ).fetchone()
                prev_hash = cur_prev["event_hash"] if cur_prev and cur_prev["event_hash"] else None

                event_data = {
                    "event_id": event_id,
                    "repair_id": repair_id,
                    "sequence": cur_seq,
                    "from_state": from_str,
                    "to_state": to_str,
                    "actor": actor_val,
                    "payload": clean_payload,
                    "timestamp": now_str,
                }
                event_hash = compute_event_hash(prev_hash, event_data)

                conn.execute(
                    """
                    INSERT INTO repair_execution_events (
                        event_id, repair_id, sequence, from_state, to_state, actor, payload_json, timestamp,
                        prev_event_hash, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        repair_id,
                        cur_seq,
                        from_str,
                        to_str,
                        actor_val,
                        json.dumps(clean_payload),
                        now_str,
                        prev_hash,
                        event_hash,
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

    # --- Phase 9 M3: Remote Branch Records ---

    def save_remote_branch(self, record: RemoteBranchRecord) -> None:
        """Persist or update a RemoteBranchRecord."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_remote_branches (
                        remote_branch_id, repair_id, publication_id, plan_id,
                        staged_commit_id, decision_id, remote_name, remote_url,
                        branch_name, commit_sha, parent_commit_sha, tree_sha,
                        patch_sha256, pushed_at, verified_at, status,
                        verification_details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(remote_branch_id) DO UPDATE SET
                        status = excluded.status,
                        verified_at = excluded.verified_at,
                        verification_details_json = excluded.verification_details_json
                    """,
                    (
                        record.remote_branch_id,
                        record.repair_id,
                        record.publication_id,
                        record.plan_id,
                        record.staged_commit_id,
                        record.decision_id,
                        record.remote_name,
                        record.remote_url,
                        record.branch_name,
                        record.commit_sha,
                        record.parent_commit_sha,
                        record.tree_sha,
                        record.patch_sha256,
                        record.pushed_at.isoformat(),
                        record.verified_at.isoformat() if record.verified_at else None,
                        record.status,
                        json.dumps(record.verification_details),
                    ),
                )
        finally:
            conn.close()

    def _row_to_remote_branch(self, row: sqlite3.Row) -> RemoteBranchRecord:
        """Hydrate RemoteBranchRecord from sqlite3.Row."""
        return RemoteBranchRecord(
            remote_branch_id=row["remote_branch_id"],
            repair_id=row["repair_id"],
            publication_id=row["publication_id"],
            plan_id=row["plan_id"],
            staged_commit_id=row["staged_commit_id"],
            decision_id=row["decision_id"],
            remote_name=row["remote_name"],
            remote_url=row["remote_url"],
            branch_name=row["branch_name"],
            commit_sha=row["commit_sha"],
            parent_commit_sha=row["parent_commit_sha"],
            tree_sha=row["tree_sha"],
            patch_sha256=row["patch_sha256"],
            pushed_at=datetime.fromisoformat(row["pushed_at"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
            status=row["status"],
            verification_details=json.loads(row["verification_details_json"] or "{}"),
        )

    def get_remote_branch(self, remote_branch_id: str) -> Optional[RemoteBranchRecord]:
        """Fetch RemoteBranchRecord by ID."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_remote_branches WHERE remote_branch_id = ?",
                (remote_branch_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_remote_branch(row)
        finally:
            conn.close()

    def get_remote_branch_for_repair(self, repair_id: str) -> Optional[RemoteBranchRecord]:
        """Fetch the latest RemoteBranchRecord for a repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_remote_branches WHERE repair_id = ? ORDER BY pushed_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_remote_branch(row)
        finally:
            conn.close()

    def list_remote_branches(self, repair_id: Optional[str] = None) -> List[RemoteBranchRecord]:
        """List RemoteBranchRecords, optionally filtered by repair_id."""
        conn = self._conn()
        try:
            if repair_id:
                cur = conn.execute(
                    "SELECT * FROM publication_remote_branches WHERE repair_id = ? ORDER BY pushed_at DESC",
                    (repair_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM publication_remote_branches ORDER BY pushed_at DESC"
                )
            return [self._row_to_remote_branch(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # --- Phase 9 M3: Pull Request Records ---

    def save_pull_request(self, record: PullRequestRecord) -> None:
        """Persist or update a PullRequestRecord."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_pull_requests (
                        pr_record_id, repair_id, publication_id, plan_id,
                        decision_id, staged_commit_id, remote_branch_id, provider,
                        repository_owner, repository_name, target_branch, source_branch,
                        pr_number, pr_url, pr_title, pr_body, pr_state, is_draft,
                        created_at, verified_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pr_record_id) DO UPDATE SET
                        pr_state = excluded.pr_state,
                        verified_at = excluded.verified_at,
                        status = excluded.status
                    """,
                    (
                        record.pr_record_id,
                        record.repair_id,
                        record.publication_id,
                        record.plan_id,
                        record.decision_id,
                        record.staged_commit_id,
                        record.remote_branch_id,
                        record.provider,
                        record.repository_owner,
                        record.repository_name,
                        record.target_branch,
                        record.source_branch,
                        record.pr_number,
                        record.pr_url,
                        record.pr_title,
                        record.pr_body,
                        record.pr_state,
                        1 if record.is_draft else 0,
                        record.created_at.isoformat(),
                        record.verified_at.isoformat() if record.verified_at else None,
                        record.status,
                    ),
                )
        finally:
            conn.close()

    def _row_to_pull_request(self, row: sqlite3.Row) -> PullRequestRecord:
        """Hydrate PullRequestRecord from sqlite3.Row."""
        return PullRequestRecord(
            pr_record_id=row["pr_record_id"],
            repair_id=row["repair_id"],
            publication_id=row["publication_id"],
            plan_id=row["plan_id"],
            decision_id=row["decision_id"],
            staged_commit_id=row["staged_commit_id"],
            remote_branch_id=row["remote_branch_id"],
            provider=row["provider"],
            repository_owner=row["repository_owner"],
            repository_name=row["repository_name"],
            target_branch=row["target_branch"],
            source_branch=row["source_branch"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            pr_title=row["pr_title"],
            pr_body=row["pr_body"],
            pr_state=row["pr_state"],
            is_draft=bool(row["is_draft"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
            status=row["status"],
        )

    def get_pull_request(self, pr_record_id: str) -> Optional[PullRequestRecord]:
        """Fetch PullRequestRecord by ID."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_pull_requests WHERE pr_record_id = ?",
                (pr_record_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_pull_request(row)
        finally:
            conn.close()

    def get_pull_request_for_repair(self, repair_id: str) -> Optional[PullRequestRecord]:
        """Fetch latest PullRequestRecord for a repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_pull_requests WHERE repair_id = ? ORDER BY created_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_pull_request(row)
        finally:
            conn.close()

    def list_pull_requests(self, repair_id: Optional[str] = None) -> List[PullRequestRecord]:
        """List PullRequestRecords, optionally filtered by repair_id."""
        conn = self._conn()
        try:
            if repair_id:
                cur = conn.execute(
                    "SELECT * FROM publication_pull_requests WHERE repair_id = ? ORDER BY created_at DESC",
                    (repair_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM publication_pull_requests ORDER BY created_at DESC"
                )
            return [self._row_to_pull_request(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # --- Phase 9 M3: Provider Operations ---

    def save_provider_operation(self, record: ProviderOperationRecord) -> None:
        """Persist or update a ProviderOperationRecord."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_provider_operations (
                        operation_id, repair_id, provider, operation_type,
                        idempotency_key, status, request_payload_json,
                        response_payload_json, error_message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        status = excluded.status,
                        response_payload_json = excluded.response_payload_json,
                        error_message = excluded.error_message
                    """,
                    (
                        record.operation_id,
                        record.repair_id,
                        record.provider,
                        record.operation_type,
                        record.idempotency_key,
                        record.status,
                        json.dumps(record.request_payload),
                        json.dumps(record.response_payload),
                        record.error_message,
                        record.created_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def record_provider_operation(
        self,
        repair_id: str,
        provider: str,
        operation_type: str,
        idempotency_key: str,
        request_payload: Dict[str, Any],
        response_payload: Dict[str, Any],
        status: str = "success",
        error_message: Optional[str] = None,
    ) -> None:
        """Create and persist a ProviderOperationRecord."""
        rec = ProviderOperationRecord(
            operation_id=f"pop_{uuid.uuid4().hex[:12]}",
            repair_id=repair_id,
            provider=provider,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=response_payload,
            status=status,
            error_message=error_message,
        )
        self.save_provider_operation(rec)

    def _row_to_provider_operation(self, row: sqlite3.Row) -> ProviderOperationRecord:
        """Hydrate ProviderOperationRecord from sqlite3.Row."""
        return ProviderOperationRecord(
            operation_id=row["operation_id"],
            repair_id=row["repair_id"],
            provider=row["provider"],
            operation_type=row["operation_type"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            request_payload=json.loads(row["request_payload_json"] or "{}"),
            response_payload=json.loads(row["response_payload_json"] or "{}"),
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_provider_operation(self, operation_id: str) -> Optional[ProviderOperationRecord]:
        """Fetch ProviderOperationRecord by ID."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_provider_operations WHERE operation_id = ?",
                (operation_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_provider_operation(row)
        finally:
            conn.close()

    def get_provider_operation_by_key(self, idempotency_key: str) -> Optional[ProviderOperationRecord]:
        """Fetch ProviderOperationRecord by idempotency_key."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_provider_operations WHERE idempotency_key = ? ORDER BY created_at DESC LIMIT 1",
                (idempotency_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_provider_operation(row)
        finally:
            conn.close()

    def list_provider_operations(self, repair_id: Optional[str] = None) -> List[ProviderOperationRecord]:
        """List ProviderOperationRecords, optionally filtered by repair_id."""
        conn = self._conn()
        try:
            if repair_id:
                cur = conn.execute(
                    "SELECT * FROM publication_provider_operations WHERE repair_id = ? ORDER BY created_at DESC",
                    (repair_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM publication_provider_operations ORDER BY created_at DESC"
                )
            return [self._row_to_provider_operation(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # --- Phase 9 M3: Remote Publication Lifecycle Records ---

    def save_remote_publication(self, record: RemotePublicationRecord) -> None:
        """Persist or update a RemotePublicationRecord."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO publication_remote_records (
                        remote_publication_id, repair_id, publication_id, plan_id,
                        decision_id, staged_commit_id, provider, status,
                        remote_branch_id, pr_record_id, failure_classification,
                        failure_reason, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(remote_publication_id) DO UPDATE SET
                        status = excluded.status,
                        remote_branch_id = excluded.remote_branch_id,
                        pr_record_id = excluded.pr_record_id,
                        failure_classification = excluded.failure_classification,
                        failure_reason = excluded.failure_reason,
                        updated_at = excluded.updated_at,
                        completed_at = excluded.completed_at
                    """,
                    (
                        record.remote_publication_id,
                        record.repair_id,
                        record.publication_id,
                        record.plan_id,
                        record.decision_id,
                        record.staged_commit_id,
                        record.provider,
                        record.status,
                        record.remote_branch_id,
                        record.pr_record_id,
                        record.failure_classification,
                        record.failure_reason,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                        record.completed_at.isoformat() if record.completed_at else None,
                    ),
                )
        finally:
            conn.close()

    def _row_to_remote_publication(self, row: sqlite3.Row) -> RemotePublicationRecord:
        """Hydrate RemotePublicationRecord from sqlite3.Row."""
        return RemotePublicationRecord(
            remote_publication_id=row["remote_publication_id"],
            repair_id=row["repair_id"],
            publication_id=row["publication_id"],
            plan_id=row["plan_id"],
            decision_id=row["decision_id"],
            staged_commit_id=row["staged_commit_id"],
            provider=row["provider"],
            status=row["status"],
            remote_branch_id=row["remote_branch_id"],
            pr_record_id=row["pr_record_id"],
            failure_classification=row["failure_classification"],
            failure_reason=row["failure_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        )

    def get_remote_publication(self, remote_publication_id: str) -> Optional[RemotePublicationRecord]:
        """Fetch RemotePublicationRecord by ID."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_remote_records WHERE remote_publication_id = ?",
                (remote_publication_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_remote_publication(row)
        finally:
            conn.close()

    def get_remote_publication_for_repair(self, repair_id: str) -> Optional[RemotePublicationRecord]:
        """Fetch the latest RemotePublicationRecord for a repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_remote_records WHERE repair_id = ? ORDER BY created_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_remote_publication(row)
        finally:
            conn.close()

    def list_remote_publications(self, repair_id: Optional[str] = None) -> List[RemotePublicationRecord]:
        """List RemotePublicationRecords, optionally filtered by repair_id."""
        conn = self._conn()
        try:
            if repair_id:
                cur = conn.execute(
                    "SELECT * FROM publication_remote_records WHERE repair_id = ? ORDER BY created_at DESC",
                    (repair_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM publication_remote_records ORDER BY created_at DESC"
                )
            return [self._row_to_remote_publication(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Audit Verification & Reconciliation Persistence (Milestone 4)
    # -------------------------------------------------------------------------

    def save_audit_verification(
        self,
        verification_id: str,
        repair_id: str,
        status: str,
        total_events: int,
        sealed_events: int,
        legacy_events: int,
        is_tampered: bool,
        details: Dict[str, Any],
        verified_at: Optional[datetime] = None,
    ) -> None:
        """Persist or update a publication audit verification."""
        conn = self._conn()
        try:
            with conn:
                v_at = (verified_at or datetime.now(timezone.utc)).isoformat()
                conn.execute(
                    """
                    INSERT INTO publication_audit_verifications (
                        verification_id, repair_id, status, total_events, sealed_events,
                        legacy_events, is_tampered, details_json, verified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(verification_id) DO UPDATE SET
                        status = excluded.status,
                        total_events = excluded.total_events,
                        sealed_events = excluded.sealed_events,
                        legacy_events = excluded.legacy_events,
                        is_tampered = excluded.is_tampered,
                        details_json = excluded.details_json,
                        verified_at = excluded.verified_at
                    """,
                    (
                        verification_id,
                        repair_id,
                        status,
                        total_events,
                        sealed_events,
                        legacy_events,
                        1 if is_tampered else 0,
                        json.dumps(details),
                        v_at,
                    ),
                )
        finally:
            conn.close()

    def get_latest_audit_verification_for_repair(self, repair_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the most recent audit verification record for a repair execution."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_audit_verifications WHERE repair_id = ? ORDER BY verified_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "verification_id": row["verification_id"],
                "repair_id": row["repair_id"],
                "status": row["status"],
                "total_events": row["total_events"],
                "sealed_events": row["sealed_events"],
                "legacy_events": row["legacy_events"],
                "is_tampered": bool(row["is_tampered"]),
                "details": json.loads(row["details_json"]),
                "verified_at": datetime.fromisoformat(row["verified_at"]),
            }
        finally:
            conn.close()

    def save_reconciliation(
        self,
        reconciliation_id: str,
        repair_id: str,
        status: str,
        local_commit_sha: Optional[str] = None,
        remote_commit_sha: Optional[str] = None,
        pr_number: Optional[int] = None,
        pr_state: Optional[str] = None,
        is_consistent: bool = True,
        details: Optional[Dict[str, Any]] = None,
        reconciled_at: Optional[datetime] = None,
    ) -> None:
        """Persist or update a publication reconciliation record."""
        conn = self._conn()
        try:
            with conn:
                r_at = (reconciled_at or datetime.now(timezone.utc)).isoformat()
                conn.execute(
                    """
                    INSERT INTO publication_reconciliations (
                        reconciliation_id, repair_id, status, local_commit_sha, remote_commit_sha,
                        pr_number, pr_state, is_consistent, details_json, reconciled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(reconciliation_id) DO UPDATE SET
                        status = excluded.status,
                        local_commit_sha = excluded.local_commit_sha,
                        remote_commit_sha = excluded.remote_commit_sha,
                        pr_number = excluded.pr_number,
                        pr_state = excluded.pr_state,
                        is_consistent = excluded.is_consistent,
                        details_json = excluded.details_json,
                        reconciled_at = excluded.reconciled_at
                    """,
                    (
                        reconciliation_id,
                        repair_id,
                        status,
                        local_commit_sha,
                        remote_commit_sha,
                        pr_number,
                        pr_state,
                        1 if is_consistent else 0,
                        json.dumps(details or {}),
                        r_at,
                    ),
                )
        finally:
            conn.close()

    def get_latest_reconciliation_for_repair(self, repair_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the most recent publication reconciliation record for a repair execution."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM publication_reconciliations WHERE repair_id = ? ORDER BY reconciled_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "reconciliation_id": row["reconciliation_id"],
                "repair_id": row["repair_id"],
                "status": row["status"],
                "local_commit_sha": row["local_commit_sha"],
                "remote_commit_sha": row["remote_commit_sha"],
                "pr_number": row["pr_number"],
                "pr_state": row["pr_state"],
                "is_consistent": bool(row["is_consistent"]),
                "details": json.loads(row["details_json"]),
                "reconciled_at": datetime.fromisoformat(row["reconciled_at"]),
            }
        finally:
            conn.close()

    def list_publication_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List historical publications correlating requests, staged commits, and remote records."""
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                SELECT
                    pr.publication_id,
                    pr.repair_id,
                    pr.action,
                    pr.status,
                    pr.created_at,
                    pr.updated_at,
                    sc.branch_name,
                    sc.commit_sha,
                    ppr.pr_number,
                    ppr.pr_url,
                    ppr.pr_state
                FROM publication_requests pr
                LEFT JOIN publication_staged_commits sc ON pr.repair_id = sc.repair_id
                LEFT JOIN publication_pull_requests ppr ON pr.repair_id = ppr.repair_id
                ORDER BY pr.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            records = []
            for row in cur.fetchall():
                records.append({
                    "publication_id": row["publication_id"],
                    "repair_id": row["repair_id"],
                    "action": row["action"],
                    "status": row["status"],
                    "branch_name": row["branch_name"],
                    "commit_sha": row["commit_sha"],
                    "pr_number": row["pr_number"],
                    "pr_url": row["pr_url"],
                    "pr_state": row["pr_state"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })
            return records
        finally:
            conn.close()
