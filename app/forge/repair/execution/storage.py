"""Phase 8 Repair Execution Architecture — Storage and Audit Integration.

Provides SQLite persistence for RepairRequests, PatchArtifacts, and
immutable RepairExecutionEvents with secret redaction.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional
import uuid

from forge.config import redact_secrets
from forge.repair.execution.interfaces import RepairEventStoreProtocol
from forge.repair.execution.models import (
    FailureCategory,
    FailureClassification,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionRecord,
    RepairExecutionState,
    RepairRequest,
)
from forge.repair.execution.state import validate_repair_transition
from forge.storage.database import get_db_connection, init_database


class RepairExecutionStoreError(Exception):
    """Raised when a storage operation fails or a resource is missing."""


class RepairExecutionStore(RepairEventStoreProtocol):
    """SQLite-backed store managing repair execution entities, artifacts, and audit events."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        init_database(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    # -------------------------------------------------------------------------
    # Repair Request Persistence
    # -------------------------------------------------------------------------

    def save_repair_request(self, request: RepairRequest) -> None:
        """Persist or insert an initial RepairRequest."""
        conn = self._conn()
        try:
            with conn:
                failure_json = request.failure.model_dump_json() if request.failure else None
                conn.execute(
                    """
                    INSERT INTO repair_executions (
                        repair_id, finding_id, run_id, target_repo, base_commit_sha,
                        state, evidence_ids_json, allowed_files_json, workspace_id,
                        patch_artifact_id, validation_status, verification_status,
                        failure_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repair_id) DO UPDATE SET
                        state = excluded.state,
                        workspace_id = excluded.workspace_id,
                        patch_artifact_id = excluded.patch_artifact_id,
                        validation_status = excluded.validation_status,
                        verification_status = excluded.verification_status,
                        failure_json = excluded.failure_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        request.repair_id,
                        request.finding_id,
                        request.run_id,
                        request.target_repo,
                        request.base_commit_sha,
                        request.state.value,
                        json.dumps(request.evidence_ids),
                        json.dumps(request.allowed_files),
                        request.workspace_id,
                        request.patch_artifact_id,
                        request.validation_status,
                        request.verification_status,
                        failure_json,
                        request.created_at.isoformat(),
                        request.updated_at.isoformat(),
                    ),
                )

                # Ensure initial CREATED event exists
                cur = conn.execute(
                    "SELECT 1 FROM repair_execution_events WHERE repair_id = ?",
                    (request.repair_id,),
                )
                if not cur.fetchone():
                    event_id = f"ree_{uuid.uuid4().hex[:12]}"
                    initial_payload = {
                        "finding_id": request.finding_id,
                        "target_repo": request.target_repo,
                        "base_commit_sha": request.base_commit_sha,
                        "evidence_ids": request.evidence_ids,
                        "allowed_files": request.allowed_files,
                    }
                    self._append_event_locked(
                        conn,
                        event_id=event_id,
                        repair_id=request.repair_id,
                        from_state=None,
                        to_state=request.state,
                        actor=RepairActor.ORCHESTRATOR,
                        payload=initial_payload,
                    )
        finally:
            conn.close()

    def get_repair_request(self, repair_id: str) -> Optional[RepairRequest]:
        """Fetch a RepairRequest by ID."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_executions WHERE repair_id = ?",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            failure = None
            if row["failure_json"]:
                failure = FailureClassification.model_validate_json(row["failure_json"])

            return RepairRequest(
                repair_id=row["repair_id"],
                finding_id=row["finding_id"],
                target_repo=row["target_repo"],
                base_commit_sha=row["base_commit_sha"],
                evidence_ids=json.loads(row["evidence_ids_json"]),
                allowed_files=json.loads(row["allowed_files_json"]),
                run_id=row["run_id"],
                workspace_id=row["workspace_id"],
                patch_artifact_id=row["patch_artifact_id"],
                state=RepairExecutionState(row["state"]),
                validation_status=row["validation_status"],
                verification_status=row["verification_status"],
                failure=failure,
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        finally:
            conn.close()

    def list_repair_requests(self, finding_id: Optional[str] = None) -> List[RepairRequest]:
        """List all repair requests, optionally filtered by finding_id."""
        conn = self._conn()
        try:
            if finding_id:
                cur = conn.execute(
                    "SELECT * FROM repair_executions WHERE finding_id = ? ORDER BY created_at ASC",
                    (finding_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM repair_executions ORDER BY created_at ASC"
                )

            requests = []
            for row in cur.fetchall():
                failure = None
                if row["failure_json"]:
                    failure = FailureClassification.model_validate_json(row["failure_json"])

                requests.append(
                    RepairRequest(
                        repair_id=row["repair_id"],
                        finding_id=row["finding_id"],
                        target_repo=row["target_repo"],
                        base_commit_sha=row["base_commit_sha"],
                        evidence_ids=json.loads(row["evidence_ids_json"]),
                        allowed_files=json.loads(row["allowed_files_json"]),
                        run_id=row["run_id"],
                        workspace_id=row["workspace_id"],
                        patch_artifact_id=row["patch_artifact_id"],
                        state=RepairExecutionState(row["state"]),
                        validation_status=row["validation_status"],
                        verification_status=row["verification_status"],
                        failure=failure,
                        created_at=datetime.fromisoformat(row["created_at"]),
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                    )
                )
            return requests
        finally:
            conn.close()

    def transition_repair_state(
        self,
        repair_id: str,
        to_state: RepairExecutionState,
        actor: RepairActor,
        context: Optional[Dict[str, Any]] = None,
    ) -> RepairRequest:
        """Atomically validate and execute a state transition, updating SQLite and logging an audit event."""
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "SELECT * FROM repair_executions WHERE repair_id = ?",
                    (repair_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise RepairExecutionStoreError(f"RepairRequest '{repair_id}' does not exist")

                current_state = RepairExecutionState(row["state"])
                ctx = context or {}

                # Guarded state transition check
                validate_repair_transition(current_state, to_state, actor, ctx)

                now = datetime.now(timezone.utc)
                now_str = now.isoformat()

                # Process contextual updates
                patch_id = row["patch_artifact_id"]
                val_status = row["validation_status"]
                ver_status = row["verification_status"]
                failure_json = row["failure_json"]

                if to_state == RepairExecutionState.PATCH_GENERATED:
                    patch = ctx.get("patch")
                    if patch and isinstance(patch, PatchArtifact):
                        patch_id = patch.artifact_id
                        self._save_patch_artifact_locked(conn, patch)

                elif to_state == RepairExecutionState.READY:
                    val = ctx.get("validation")
                    val_status = "valid" if (val and getattr(val, "is_valid", False)) else "invalid"

                elif to_state == RepairExecutionState.VERIFIED:
                    ver_status = "passed"

                elif to_state == RepairExecutionState.FAILED:
                    failure = ctx.get("failure")
                    if failure and isinstance(failure, FailureClassification):
                        failure_json = failure.model_dump_json()
                    elif ctx.get("error_message"):
                        f_obj = FailureClassification(
                            category=ctx.get("failure_category", FailureCategory.INTERNAL_ERROR),
                            message=str(ctx["error_message"]),
                            details=ctx.get("details", {}),
                        )
                        failure_json = f_obj.model_dump_json()

                # Update repair_executions table
                conn.execute(
                    """
                    UPDATE repair_executions
                    SET state = ?, patch_artifact_id = ?, validation_status = ?,
                        verification_status = ?, failure_json = ?, updated_at = ?
                    WHERE repair_id = ?
                    """,
                    (
                        to_state.value,
                        patch_id,
                        val_status,
                        ver_status,
                        failure_json,
                        now_str,
                        repair_id,
                    ),
                )

                # Record state transition event
                event_id = f"ree_{uuid.uuid4().hex[:12]}"
                event_payload = {
                    "reason": ctx.get("reason", f"Transitioned to {to_state.value}"),
                    "details": ctx.get("details", {}),
                }
                self._append_event_locked(
                    conn,
                    event_id=event_id,
                    repair_id=repair_id,
                    from_state=current_state,
                    to_state=to_state,
                    actor=actor,
                    payload=event_payload,
                )

            # Return fresh request instance
            req = self.get_repair_request(repair_id)
            if not req:
                raise RepairExecutionStoreError(f"Failed to reload repair request '{repair_id}'")
            return req
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Patch Artifact Persistence
    # -------------------------------------------------------------------------

    def save_patch_artifact(self, patch: PatchArtifact) -> None:
        """Persist a candidate PatchArtifact."""
        conn = self._conn()
        try:
            with conn:
                self._save_patch_artifact_locked(conn, patch)
        finally:
            conn.close()

    def _save_patch_artifact_locked(self, conn: sqlite3.Connection, patch: PatchArtifact) -> None:
        conn.execute(
            """
            INSERT INTO patch_artifacts (
                artifact_id, repair_id, finding_id, base_commit_sha, target_file,
                diff_content, content_hash, lines_added, lines_deleted, hunks_count,
                affected_paths_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO NOTHING
            """,
            (
                patch.artifact_id,
                patch.repair_id,
                patch.finding_id,
                patch.base_commit_sha,
                patch.target_file,
                patch.diff_content,
                patch.content_hash,
                patch.lines_added,
                patch.lines_deleted,
                patch.hunks_count,
                json.dumps(patch.affected_paths),
                patch.created_at.isoformat(),
            ),
        )

    def get_patch_artifact(self, artifact_id: str) -> Optional[PatchArtifact]:
        """Fetch a PatchArtifact by ID."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM patch_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            return PatchArtifact(
                artifact_id=row["artifact_id"],
                repair_id=row["repair_id"],
                finding_id=row["finding_id"],
                base_commit_sha=row["base_commit_sha"],
                target_file=row["target_file"],
                diff_content=row["diff_content"],
                content_hash=row["content_hash"],
                lines_added=row["lines_added"],
                lines_deleted=row["lines_deleted"],
                hunks_count=row["hunks_count"],
                affected_paths=json.loads(row["affected_paths_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Audit Event Ledger Persistence
    # -------------------------------------------------------------------------

    def record_event(self, event: RepairExecutionEvent) -> None:
        """Record an explicit audit event into the ledger."""
        conn = self._conn()
        try:
            with conn:
                self._append_event_locked(
                    conn,
                    event_id=event.event_id,
                    repair_id=event.repair_id,
                    from_state=event.from_state,
                    to_state=event.to_state,
                    actor=event.actor,
                    payload=event.payload,
                )
        finally:
            conn.close()

    def _append_event_locked(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        repair_id: str,
        from_state: Optional[RepairExecutionState],
        to_state: RepairExecutionState,
        actor: RepairActor,
        payload: Dict[str, Any],
    ) -> None:
        cur_seq = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM repair_execution_events WHERE repair_id = ?",
            (repair_id,),
        ).fetchone()[0]

        # Sanitize payload secrets before serialization
        clean_payload = {k: redact_secrets(str(v)) if isinstance(v, str) else v for k, v in payload.items()}
        now_str = datetime.now(timezone.utc).isoformat()

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
                to_state.value,
                actor.value,
                json.dumps(clean_payload),
                now_str,
            ),
        )

    def get_events(self, repair_id: str) -> List[RepairExecutionEvent]:
        """Fetch all sequential audit events for a repair execution."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_execution_events WHERE repair_id = ? ORDER BY sequence ASC",
                (repair_id,),
            )
            events = []
            for row in cur.fetchall():
                from_st = RepairExecutionState(row["from_state"]) if row["from_state"] else None
                to_st = RepairExecutionState(row["to_state"])
                actor = RepairActor(row["actor"])
                events.append(
                    RepairExecutionEvent(
                        event_id=row["event_id"],
                        repair_id=row["repair_id"],
                        sequence=row["sequence"],
                        from_state=from_st,
                        to_state=to_st,
                        actor=actor,
                        payload=json.loads(row["payload_json"]),
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                    )
                )
            return events
        finally:
            conn.close()

    def get_full_execution_record(self, repair_id: str) -> Optional[RepairExecutionRecord]:
        """Assemble full snapshot of a repair execution."""
        req = self.get_repair_request(repair_id)
        if not req:
            return None

        patch = None
        if req.patch_artifact_id:
            patch = self.get_patch_artifact(req.patch_artifact_id)

        events = self.get_events(repair_id)

        return RepairExecutionRecord(
            request=req,
            patch=patch,
            events=events,
            failure=req.failure,
        )
