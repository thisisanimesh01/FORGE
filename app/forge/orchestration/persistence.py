"""Phase 13 M2: Durable Workflow State Persistence and Schema Management.

Provides transactional SQLite persistence for engineering workflows, stage records,
cryptographic event chains, tasks, recovery checkpoints, and mutation reconciliations.
Enforces schema versioning, fail-closed loading validation, and concurrency safety.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from forge.config import get_settings, redact_secrets
from forge.orchestration.events import OrchestrationEvent, OrchestrationEventType
from forge.orchestration.schemas import (
    StageExecutionRecord,
    StageOutputs,
    StageStatus,
    WorkflowContext,
    WorkflowStage,
    WorkflowState,
)
from forge.storage.sqlite_pool import ThreadLocalSQLitePool

ORCHESTRATION_SCHEMA_VERSION = 1


class PersistenceError(Exception):
    """Base exception for persistence and storage failures."""


class CorruptedStateError(PersistenceError):
    """Raised when persisted workflow state fails validation or is corrupted."""


class IncompatibleSchemaError(PersistenceError):
    """Raised when persisted state has an incompatible schema version."""


class OrchestrationStateStore:
    """Manages transactional SQLite persistence for autonomous engineering workflows."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or get_settings().db_path).resolve()
        self._lock = threading.Lock()
        self._pool = ThreadLocalSQLitePool(self.db_path, owner=self)
        self._init_db()

    def _get_connection(self) -> Any:
        """Return a pooled thread-local SQLite connection with WAL journaling and busy timeout."""
        return self._pool.get_connection()

    def close(self) -> None:
        """Close all pooled SQLite connections."""
        self._pool.close_all()

    def _init_db(self) -> None:
        """Initialize tables and indexes idempotently with schema tracking."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_workflows (
                        workflow_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        repository_path TEXT NOT NULL,
                        target_branch TEXT,
                        commit_sha TEXT,
                        run_id TEXT,
                        problem_description TEXT,
                        trusted_approvers_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        current_stage TEXT,
                        active_repair_plan_id TEXT,
                        active_approval_id TEXT,
                        requires_human_review INTEGER NOT NULL DEFAULT 1,
                        reconciliation_required INTEGER NOT NULL DEFAULT 0,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_stage_records (
                        record_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        error_message TEXT,
                        outputs_json TEXT NOT NULL DEFAULT '{}',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        updated_at TEXT NOT NULL,
                        UNIQUE (workflow_id, stage),
                        FOREIGN KEY (workflow_id) REFERENCES orchestration_workflows(workflow_id) ON DELETE CASCADE
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_events (
                        event_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        stage TEXT,
                        state TEXT NOT NULL,
                        action_description TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        prev_event_hash TEXT NOT NULL,
                        event_hash TEXT NOT NULL,
                        UNIQUE (workflow_id, sequence),
                        FOREIGN KEY (workflow_id) REFERENCES orchestration_workflows(workflow_id) ON DELETE CASCADE
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_recovery_checkpoints (
                        checkpoint_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        state TEXT NOT NULL,
                        last_event_sequence INTEGER NOT NULL,
                        last_event_hash TEXT NOT NULL,
                        outputs_digest TEXT NOT NULL,
                        is_valid INTEGER NOT NULL DEFAULT 1,
                        reconciliation_needed INTEGER NOT NULL DEFAULT 0,
                        reason TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (workflow_id) REFERENCES orchestration_workflows(workflow_id) ON DELETE CASCADE
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_tasks (
                        task_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        state TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 0,
                        idempotency_key TEXT NOT NULL,
                        dependencies_json TEXT NOT NULL DEFAULT '[]',
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 3,
                        failure_category TEXT,
                        error_message TEXT,
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        UNIQUE (workflow_id, idempotency_key),
                        FOREIGN KEY (workflow_id) REFERENCES orchestration_workflows(workflow_id) ON DELETE CASCADE
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_reconciliations (
                        reconciliation_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        resolution TEXT NOT NULL,
                        reconciled_by TEXT NOT NULL,
                        operator_notes TEXT,
                        status_before TEXT NOT NULL,
                        status_after TEXT NOT NULL,
                        reconciled_at TEXT NOT NULL,
                        operator_principal TEXT NOT NULL DEFAULT '',
                        auth_channel TEXT NOT NULL DEFAULT 'interactive_cli',
                        FOREIGN KEY (workflow_id) REFERENCES orchestration_workflows(workflow_id) ON DELETE CASCADE
                    );
                """)

                # Ensure migration columns for reconciliation identity
                cur = conn.execute("PRAGMA table_info(orchestration_reconciliations);")
                col_names = {r["name"] for r in cur.fetchall()}
                if "operator_principal" not in col_names:
                    conn.execute("ALTER TABLE orchestration_reconciliations ADD COLUMN operator_principal TEXT NOT NULL DEFAULT '';")
                if "auth_channel" not in col_names:
                    conn.execute("ALTER TABLE orchestration_reconciliations ADD COLUMN auth_channel TEXT NOT NULL DEFAULT 'interactive_cli';")

                # Create indexes
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_wf_state ON orchestration_workflows(state);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_wf_repo ON orchestration_workflows(repository_path);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_stage_wf ON orchestration_stage_records(workflow_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_events_wf ON orchestration_events(workflow_id, sequence);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_checkpoints_wf ON orchestration_recovery_checkpoints(workflow_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_checkpoints_wf_latest ON orchestration_recovery_checkpoints(workflow_id, created_at DESC);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_tasks_wf ON orchestration_tasks(workflow_id, state);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_orch_tasks_key ON orchestration_tasks(idempotency_key);")

                # Record migration version
                cur = conn.execute(
                    "SELECT version FROM orchestration_schema_migrations WHERE version = ?",
                    (ORCHESTRATION_SCHEMA_VERSION,),
                )
                if not cur.fetchone():
                    now_str = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "INSERT INTO orchestration_schema_migrations (version, applied_at) VALUES (?, ?)",
                        (ORCHESTRATION_SCHEMA_VERSION, now_str),
                    )
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Workflow Persistence
    # -----------------------------------------------------------------------

    def save_workflow(
        self,
        context: WorkflowContext,
        state: WorkflowState,
        current_stage: Optional[WorkflowStage] = None,
        active_repair_plan_id: Optional[str] = None,
        active_approval_id: Optional[str] = None,
        completed_at: Optional[datetime] = None,
        reconciliation_required: bool = False,
        requires_human_review: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically persist or update workflow state and metadata."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                approvers = list(context.trusted_approvers) if context.trusted_approvers else []
                meta_json = json.dumps(metadata or {}, separators=(",", ":"))

                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT INTO orchestration_workflows (
                        workflow_id, schema_version, state, repository_path, target_branch,
                        commit_sha, run_id, problem_description, trusted_approvers_json,
                        created_at, updated_at, completed_at, current_stage, active_repair_plan_id,
                        active_approval_id, requires_human_review, reconciliation_required, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id) DO UPDATE SET
                        state = excluded.state,
                        updated_at = excluded.updated_at,
                        completed_at = excluded.completed_at,
                        current_stage = excluded.current_stage,
                        active_repair_plan_id = excluded.active_repair_plan_id,
                        active_approval_id = excluded.active_approval_id,
                        requires_human_review = excluded.requires_human_review,
                        reconciliation_required = excluded.reconciliation_required,
                        metadata_json = excluded.metadata_json;
                    """,
                    (
                        context.workflow_id,
                        ORCHESTRATION_SCHEMA_VERSION,
                        state.value,
                        str(context.repository_path),
                        getattr(context, "target_branch", None),
                        context.commit_sha,
                        context.run_id,
                        redact_secrets(getattr(context, "problem_description", None) or (context.defect_input.user_description if context.defect_input else "") or ""),
                        json.dumps(approvers),
                        context.created_at.isoformat() if hasattr(context.created_at, "isoformat") else str(context.created_at),
                        now_str,
                        completed_at.isoformat() if completed_at else None,
                        current_stage.value if current_stage else None,
                        active_repair_plan_id,
                        active_approval_id,
                        1 if requires_human_review else 0,
                        1 if reconciliation_required else 0,
                        meta_json,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception as e:
                conn.execute("ROLLBACK;")
                raise PersistenceError(f"Failed to save workflow '{context.workflow_id}': {e}") from e
            finally:
                conn.close()

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Load and validate persisted workflow metadata."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_workflows WHERE workflow_id = ?",
                    (workflow_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                d = dict(row)
                if d["schema_version"] > ORCHESTRATION_SCHEMA_VERSION:
                    raise IncompatibleSchemaError(
                        f"Incompatible schema version {d['schema_version']} > {ORCHESTRATION_SCHEMA_VERSION}"
                    )
                if not d["workflow_id"] or not d["repository_path"] or not d["state"]:
                    raise CorruptedStateError(f"Workflow '{workflow_id}' row is incomplete or corrupted.")

                d["trusted_approvers"] = set(json.loads(d["trusted_approvers_json"]))
                d["metadata"] = json.loads(d["metadata_json"])
                d["requires_human_review"] = bool(d["requires_human_review"])
                d["reconciliation_required"] = bool(d["reconciliation_required"])
                return d
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Stage Records & Outputs Persistence
    # -----------------------------------------------------------------------

    def save_stage_record(
        self,
        workflow_id: str,
        stage: WorkflowStage,
        record: StageExecutionRecord,
        output_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a stage execution record and its serialized output payload atomically."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                record_id = f"stg_{workflow_id[-6:]}_{stage.value}"
                out_json = json.dumps(output_payload or {}, separators=(",", ":"))
                meta_json = json.dumps(record.metadata or {}, separators=(",", ":"))

                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT INTO orchestration_stage_records (
                        record_id, workflow_id, stage, status, started_at, completed_at,
                        error_message, outputs_json, metadata_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, stage) DO UPDATE SET
                        status = excluded.status,
                        completed_at = excluded.completed_at,
                        error_message = excluded.error_message,
                        outputs_json = excluded.outputs_json,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        record_id,
                        workflow_id,
                        stage.value,
                        record.status.value,
                        record.started_at.isoformat() if hasattr(record.started_at, "isoformat") else str(record.started_at),
                        record.completed_at.isoformat() if record.completed_at else None,
                        record.error_message,
                        out_json,
                        meta_json,
                        now_str,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception as e:
                conn.execute("ROLLBACK;")
                raise PersistenceError(f"Failed to save stage record '{stage.value}' for '{workflow_id}': {e}") from e
            finally:
                conn.close()

    def get_stage_records(self, workflow_id: str) -> Dict[str, StageExecutionRecord]:
        """Load all stage execution records for a workflow."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_stage_records WHERE workflow_id = ?",
                    (workflow_id,),
                )
                records: Dict[str, StageExecutionRecord] = {}
                for row in cur.fetchall():
                    rec = StageExecutionRecord(
                        stage=WorkflowStage(row["stage"]),
                        status=StageStatus(row["status"]),
                        started_at=datetime.fromisoformat(row["started_at"]),
                        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
                        error_message=row["error_message"],
                        metadata=json.loads(row["metadata_json"]),
                    )
                    records[row["stage"]] = rec
                return records
            finally:
                conn.close()

    def get_stage_outputs_raw(self, workflow_id: str) -> Dict[str, Dict[str, Any]]:
        """Retrieve raw serialized output payloads for all completed stages."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT stage, outputs_json FROM orchestration_stage_records WHERE workflow_id = ?",
                    (workflow_id,),
                )
                outputs: Dict[str, Dict[str, Any]] = {}
                for row in cur.fetchall():
                    outputs[row["stage"]] = json.loads(row["outputs_json"])
                return outputs
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Cryptographic Audit Events Persistence
    # -----------------------------------------------------------------------

    def save_event(self, event: OrchestrationEvent) -> None:
        """Persist an audit event into the append-only SQLite log."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO orchestration_events (
                        event_id, workflow_id, sequence, timestamp, event_type,
                        stage, state, action_description, details_json, prev_event_hash, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        event.event_id,
                        event.workflow_id,
                        event.sequence,
                        event.timestamp.isoformat(),
                        event.event_type.value,
                        event.stage.value if event.stage else None,
                        event.state.value,
                        event.action_description,
                        json.dumps(event.details, separators=(",", ":")),
                        event.prev_event_hash,
                        event.event_hash,
                    ),
                )
            except sqlite3.IntegrityError as ie:
                raise PersistenceError(f"Sequence conflict or duplicate event in workflow '{event.workflow_id}': {ie}") from ie
            finally:
                conn.close()

    def get_events(self, workflow_id: str) -> List[OrchestrationEvent]:
        """Load the ordered cryptographic event chain for a workflow."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_events WHERE workflow_id = ? ORDER BY sequence ASC",
                    (workflow_id,),
                )
                events: List[OrchestrationEvent] = []
                for row in cur.fetchall():
                    ev = OrchestrationEvent(
                        event_id=row["event_id"],
                        workflow_id=row["workflow_id"],
                        sequence=row["sequence"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        event_type=OrchestrationEventType(row["event_type"]),
                        stage=WorkflowStage(row["stage"]) if row["stage"] else None,
                        state=WorkflowState(row["state"]),
                        action_description=row["action_description"],
                        details=json.loads(row["details_json"]),
                        prev_event_hash=row["prev_event_hash"],
                        event_hash=row["event_hash"],
                    )
                    events.append(ev)
                return events
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Checkpoints
    # -----------------------------------------------------------------------

    def save_checkpoint(
        self,
        workflow_id: str,
        stage: WorkflowStage,
        state: WorkflowState,
        last_event_sequence: int,
        last_event_hash: str,
        outputs_digest: str,
        is_valid: bool = True,
        reconciliation_needed: bool = False,
        reason: Optional[str] = None,
    ) -> str:
        """Persist a recovery checkpoint."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                checkpoint_id = f"chk_{workflow_id[-6:]}_{stage.value}_{last_event_sequence:03d}"
                conn.execute(
                    """
                    INSERT INTO orchestration_recovery_checkpoints (
                        checkpoint_id, workflow_id, stage, state, last_event_sequence,
                        last_event_hash, outputs_digest, is_valid, reconciliation_needed, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        checkpoint_id,
                        workflow_id,
                        stage.value,
                        state.value,
                        last_event_sequence,
                        last_event_hash,
                        outputs_digest,
                        1 if is_valid else 0,
                        1 if reconciliation_needed else 0,
                        reason,
                        now_str,
                    ),
                )
                return checkpoint_id
            finally:
                conn.close()

    def get_latest_checkpoint(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the most recent valid recovery checkpoint."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    """
                    SELECT * FROM orchestration_recovery_checkpoints
                    WHERE workflow_id = ? AND is_valid = 1
                    ORDER BY last_event_sequence DESC LIMIT 1;
                    """,
                    (workflow_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["is_valid"] = bool(d["is_valid"])
                d["reconciliation_needed"] = bool(d["reconciliation_needed"])
                return d
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Tasks & Coordination
    # -----------------------------------------------------------------------

    def save_task(self, task_dict: Dict[str, Any]) -> None:
        """Persist or update a coordination task atomically."""
        with self._lock:
            conn = self._get_connection()
            try:
                deps_json = json.dumps(task_dict.get("dependencies", []))
                res_json = json.dumps(task_dict.get("result")) if task_dict.get("result") is not None else None
                conn.execute(
                    """
                    INSERT INTO orchestration_tasks (
                        task_id, workflow_id, name, stage, state, priority, idempotency_key,
                        dependencies_json, retry_count, max_retries, failure_category,
                        error_message, result_json, created_at, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, idempotency_key) DO UPDATE SET
                        state = excluded.state,
                        retry_count = excluded.retry_count,
                        failure_category = excluded.failure_category,
                        error_message = excluded.error_message,
                        result_json = excluded.result_json,
                        started_at = excluded.started_at,
                        completed_at = excluded.completed_at;
                    """,
                    (
                        task_dict["task_id"],
                        task_dict["workflow_id"],
                        task_dict["name"],
                        task_dict["stage"],
                        task_dict["state"],
                        task_dict.get("priority", 0),
                        task_dict["idempotency_key"],
                        deps_json,
                        task_dict.get("retry_count", 0),
                        task_dict.get("max_retries", 3),
                        task_dict.get("failure_category"),
                        task_dict.get("error_message"),
                        res_json,
                        task_dict["created_at"],
                        task_dict.get("started_at"),
                        task_dict.get("completed_at"),
                    ),
                )
            finally:
                conn.close()

    def get_tasks(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Retrieve all coordination tasks for a workflow."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_tasks WHERE workflow_id = ? ORDER BY priority DESC, created_at ASC",
                    (workflow_id,),
                )
                tasks: List[Dict[str, Any]] = []
                for row in cur.fetchall():
                    d = dict(row)
                    d["dependencies"] = json.loads(d["dependencies_json"])
                    d["result"] = json.loads(d["result_json"]) if d["result_json"] else None
                    tasks.append(d)
                return tasks
            finally:
                conn.close()

    def get_task_by_key(self, workflow_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve a task by its idempotency key."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_tasks WHERE workflow_id = ? AND idempotency_key = ?",
                    (workflow_id, idempotency_key),
                )
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["dependencies"] = json.loads(d["dependencies_json"])
                d["result"] = json.loads(d["result_json"]) if d["result_json"] else None
                return d
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Reconciliations
    # -----------------------------------------------------------------------

    def record_reconciliation(
        self,
        reconciliation_id: str,
        workflow_id: str,
        resolution: str,
        reconciled_by: str,
        status_before: str,
        status_after: str,
        notes: Optional[str] = None,
        operator_principal: str = "",
        auth_channel: str = "interactive_cli",
    ) -> None:
        """Record an operator reconciliation action with authenticated operator binding."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO orchestration_reconciliations (
                        reconciliation_id, workflow_id, resolution, reconciled_by,
                        operator_notes, status_before, status_after, reconciled_at,
                        operator_principal, auth_channel
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        reconciliation_id,
                        workflow_id,
                        resolution,
                        reconciled_by,
                        notes,
                        status_before,
                        status_after,
                        now_str,
                        operator_principal,
                        auth_channel,
                    ),
                )
            finally:
                conn.close()

    def get_latest_reconciliation(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the latest reconciliation record for a workflow."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_reconciliations WHERE workflow_id = ? ORDER BY reconciled_at DESC LIMIT 1",
                    (workflow_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
