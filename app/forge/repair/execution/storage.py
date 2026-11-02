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
    IndependentValidationResult,
    PatchArtifact,
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionPlan,
    RepairExecutionRecord,
    RepairExecutionState,
    RepairRequest,
    ValidationOutcome,
    ValidationResult,
    WorkspaceRecord,
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

                if to_state == RepairExecutionState.PLANNED:
                    plan = ctx.get("plan")
                    if plan and isinstance(plan, RepairExecutionPlan):
                        self._save_repair_plan_locked(conn, plan)

                elif to_state == RepairExecutionState.PATCH_GENERATED:
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
    # Repair Execution Plan Persistence
    # -------------------------------------------------------------------------

    def save_repair_plan(self, plan: RepairExecutionPlan) -> None:
        """Persist or insert a RepairExecutionPlan."""
        conn = self._conn()
        try:
            with conn:
                self._save_repair_plan_locked(conn, plan)
        finally:
            conn.close()

    def _save_repair_plan_locked(self, conn: sqlite3.Connection, plan: RepairExecutionPlan) -> None:
        conn.execute(
            """
            INSERT INTO repair_execution_plans (
                plan_id, repair_id, target_file, root_cause, proposed_change,
                expected_behavior, verification_command, confidence, rationale, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                proposed_change = excluded.proposed_change,
                expected_behavior = excluded.expected_behavior,
                verification_command = excluded.verification_command,
                confidence = excluded.confidence,
                rationale = excluded.rationale
            """,
            (
                plan.plan_id,
                plan.repair_id,
                plan.target_file,
                plan.root_cause,
                plan.proposed_change,
                plan.expected_behavior,
                plan.verification_command,
                plan.confidence,
                plan.rationale,
                plan.created_at.isoformat(),
            ),
        )

    def get_repair_plan(self, plan_id: str) -> Optional[RepairExecutionPlan]:
        """Fetch a RepairExecutionPlan by plan_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_execution_plans WHERE plan_id = ?",
                (plan_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            return RepairExecutionPlan(
                plan_id=row["plan_id"],
                repair_id=row["repair_id"],
                target_file=row["target_file"],
                root_cause=row["root_cause"],
                proposed_change=row["proposed_change"],
                expected_behavior=row["expected_behavior"],
                verification_command=row["verification_command"],
                confidence=row["confidence"],
                rationale=row["rationale"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        finally:
            conn.close()

    def get_repair_plan_for_repair(self, repair_id: str) -> Optional[RepairExecutionPlan]:
        """Fetch the most recent RepairExecutionPlan for a repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_execution_plans WHERE repair_id = ? ORDER BY created_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            return RepairExecutionPlan(
                plan_id=row["plan_id"],
                repair_id=row["repair_id"],
                target_file=row["target_file"],
                root_cause=row["root_cause"],
                proposed_change=row["proposed_change"],
                expected_behavior=row["expected_behavior"],
                verification_command=row["verification_command"],
                confidence=row["confidence"],
                rationale=row["rationale"],
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

        validation = None
        if patch:
            validation = self.get_validation_result_for_patch(patch.artifact_id)
        if not validation:
            val_list = self.list_validation_results_for_repair(repair_id)
            if val_list:
                validation = val_list[0]

        plan = self.get_repair_plan_for_repair(repair_id)
        events = self.get_events(repair_id)

        return RepairExecutionRecord(
            request=req,
            plan=plan,
            patch=patch,
            validation=validation,
            events=events,
            failure=req.failure,
        )

    # -------------------------------------------------------------------------
    # Workspace Record Persistence
    # -------------------------------------------------------------------------

    def save_workspace_record(self, record: WorkspaceRecord) -> None:
        """Persist or insert a WorkspaceRecord."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO repair_workspaces (
                        workspace_id, repair_id, target_repo, base_commit_sha,
                        workspace_path, provisioning_status, isolation_verified,
                        cleanup_status, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_id) DO UPDATE SET
                        provisioning_status = excluded.provisioning_status,
                        isolation_verified = excluded.isolation_verified,
                        cleanup_status = excluded.cleanup_status,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record.workspace_id,
                        record.repair_id,
                        record.target_repo,
                        record.base_commit_sha,
                        record.workspace_path,
                        record.provisioning_status,
                        1 if record.isolation_verified else 0,
                        record.cleanup_status,
                        json.dumps(record.metadata),
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def get_workspace_record(self, workspace_id: str) -> Optional[WorkspaceRecord]:
        """Fetch a WorkspaceRecord by workspace_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return WorkspaceRecord(
                workspace_id=row["workspace_id"],
                repair_id=row["repair_id"],
                target_repo=row["target_repo"],
                base_commit_sha=row["base_commit_sha"],
                workspace_path=row["workspace_path"],
                provisioning_status=row["provisioning_status"],
                isolation_verified=bool(row["isolation_verified"]),
                cleanup_status=row["cleanup_status"],
                metadata=json.loads(row["metadata_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        finally:
            conn.close()

    def get_workspace_record_for_repair(self, repair_id: str) -> Optional[WorkspaceRecord]:
        """Fetch the WorkspaceRecord associated with a repair_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_workspaces WHERE repair_id = ? ORDER BY created_at DESC LIMIT 1",
                (repair_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return WorkspaceRecord(
                workspace_id=row["workspace_id"],
                repair_id=row["repair_id"],
                target_repo=row["target_repo"],
                base_commit_sha=row["base_commit_sha"],
                workspace_path=row["workspace_path"],
                provisioning_status=row["provisioning_status"],
                isolation_verified=bool(row["isolation_verified"]),
                cleanup_status=row["cleanup_status"],
                metadata=json.loads(row["metadata_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        finally:
            conn.close()

    def update_workspace_record(self, workspace_id: str, updates: Dict[str, Any]) -> None:
        """Update fields of an existing WorkspaceRecord."""
        conn = self._conn()
        try:
            with conn:
                allowed_cols = {
                    "provisioning_status",
                    "isolation_verified",
                    "cleanup_status",
                    "metadata_json",
                }
                set_clauses = []
                values = []
                for k, v in updates.items():
                    if k == "metadata":
                        set_clauses.append("metadata_json = ?")
                        values.append(json.dumps(v))
                    elif k == "isolation_verified":
                        set_clauses.append("isolation_verified = ?")
                        values.append(1 if v else 0)
                    elif k in allowed_cols:
                        set_clauses.append(f"{k} = ?")
                        values.append(v)

                if not set_clauses:
                    return

                set_clauses.append("updated_at = ?")
                values.append(datetime.now(timezone.utc).isoformat())
                values.append(workspace_id)

                sql = f"UPDATE repair_workspaces SET {', '.join(set_clauses)} WHERE workspace_id = ?"
                conn.execute(sql, tuple(values))
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Independent Validation Result Persistence
    # -------------------------------------------------------------------------

    def save_validation_result(self, result: IndependentValidationResult) -> None:
        """Persist an independent patch validation result."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO repair_validation_results (
                        validation_id, patch_artifact_id, repair_id, finding_id,
                        repository_target, base_commit_sha, workspace_id, patch_sha256,
                        validated_file_paths_json, validated_hunk_count, lines_added,
                        lines_deleted, patch_bytes, scope_result_json, integrity_result_json,
                        semantic_result_json, overall_status, rejection_reasons_json,
                        policy_version, is_valid, violations_json, checks_passed_json,
                        validated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(validation_id) DO UPDATE SET
                        overall_status = excluded.overall_status,
                        is_valid = excluded.is_valid,
                        violations_json = excluded.violations_json,
                        checks_passed_json = excluded.checks_passed_json,
                        rejection_reasons_json = excluded.rejection_reasons_json
                    """,
                    (
                        result.validation_id,
                        result.patch_artifact_id,
                        result.repair_id,
                        result.finding_id,
                        result.repository_target,
                        result.base_commit_sha,
                        result.workspace_id,
                        result.patch_sha256,
                        json.dumps(result.validated_file_paths),
                        result.validated_hunk_count,
                        result.lines_added,
                        result.lines_deleted,
                        result.patch_bytes,
                        json.dumps(result.scope_result),
                        json.dumps(result.integrity_result),
                        json.dumps(result.semantic_result),
                        result.overall_status.value if isinstance(result.overall_status, ValidationOutcome) else str(result.overall_status),
                        json.dumps(result.rejection_reasons),
                        result.policy_version,
                        1 if result.is_valid else 0,
                        json.dumps(result.violations),
                        json.dumps(result.checks_passed),
                        result.validated_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def _row_to_validation_result(self, row: sqlite3.Row) -> IndependentValidationResult:
        return IndependentValidationResult(
            validation_id=row["validation_id"],
            patch_artifact_id=row["patch_artifact_id"],
            repair_id=row["repair_id"],
            finding_id=row["finding_id"],
            repository_target=row["repository_target"],
            base_commit_sha=row["base_commit_sha"],
            workspace_id=row["workspace_id"],
            patch_sha256=row["patch_sha256"],
            validated_file_paths=json.loads(row["validated_file_paths_json"]),
            validated_hunk_count=row["validated_hunk_count"],
            lines_added=row["lines_added"],
            lines_deleted=row["lines_deleted"],
            patch_bytes=row["patch_bytes"],
            scope_result=json.loads(row["scope_result_json"]),
            integrity_result=json.loads(row["integrity_result_json"]),
            semantic_result=json.loads(row["semantic_result_json"]),
            overall_status=ValidationOutcome(row["overall_status"]),
            rejection_reasons=json.loads(row["rejection_reasons_json"]),
            policy_version=row["policy_version"],
            is_valid=bool(row["is_valid"]),
            violations=json.loads(row["violations_json"]),
            checks_passed=json.loads(row["checks_passed_json"]),
            validated_at=datetime.fromisoformat(row["validated_at"]),
        )

    def get_validation_result(self, validation_id: str) -> Optional[IndependentValidationResult]:
        """Fetch an IndependentValidationResult by validation_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_validation_results WHERE validation_id = ?",
                (validation_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_validation_result(row)
        finally:
            conn.close()

    def get_validation_result_for_patch(self, patch_artifact_id: str) -> Optional[IndependentValidationResult]:
        """Fetch the latest IndependentValidationResult for a patch_artifact_id."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_validation_results WHERE patch_artifact_id = ? ORDER BY validated_at DESC LIMIT 1",
                (patch_artifact_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_validation_result(row)
        finally:
            conn.close()

    def list_validation_results_for_repair(self, repair_id: str) -> List[IndependentValidationResult]:
        """List all IndependentValidationResults for a repair_id ordered by validated_at DESC."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_validation_results WHERE repair_id = ? ORDER BY validated_at DESC",
                (repair_id,),
            )
            return [self._row_to_validation_result(r) for r in cur.fetchall()]
        finally:
            conn.close()
