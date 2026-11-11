"""Phase 13 M2: Idempotency Key Computation and Execution Safeguards.

Ensures at-most-once execution for state-mutating engineering operations,
preventing duplicate repairs, duplicate task executions, and cross-workflow
result substitution.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from forge.config import get_settings
from forge.orchestration.persistence import CorruptedStateError
from forge.orchestration.schemas import WorkflowStage


class OperationStatus(str, Enum):
    """Lifecycle states of an idempotent operation."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class IdempotencyConflictError(Exception):
    """Raised when a duplicate or conflicting operation is detected."""


class UncertainExecutionError(Exception):
    """Raised when an operation was interrupted in an uncertain mutation state."""


def compute_idempotency_key(
    workflow_id: str,
    stage: WorkflowStage,
    input_digest: str,
    discriminator: Optional[str] = None,
) -> str:
    """Compute a canonical SHA-256 idempotency key bound to workflow, stage, and payload digest.

    Args:
        workflow_id: Unique workflow identifier.
        stage: Workflow stage executing the operation.
        input_digest: SHA-256 digest of canonical input data.
        discriminator: Optional discriminator (e.g. task name or attempt count).

    Returns:
        64-character hex SHA-256 string.
    """
    canonical = {
        "workflow_id": workflow_id,
        "stage": stage.value,
        "input_digest": input_digest,
        "discriminator": discriminator or "",
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyRecord(BaseModel):
    """Durable record tracking operation execution state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str
    workflow_id: str
    stage: WorkflowStage
    status: OperationStatus
    created_at: str
    completed_at: Optional[str] = None
    result_digest: Optional[str] = None
    result_payload: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class IdempotencyLedger:
    """Thread-safe SQLite ledger tracking operation idempotency."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or get_settings().db_path).resolve()
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_idempotency_records (
                        idempotency_key TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        result_digest TEXT,
                        result_payload_json TEXT,
                        error_message TEXT
                    );
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_idemp_wf ON orchestration_idempotency_records(workflow_id);"
                )
            finally:
                conn.close()

    def register_start(
        self,
        idempotency_key: str,
        workflow_id: str,
        stage: WorkflowStage,
    ) -> Optional[IdempotencyRecord]:
        """Attempt to register the start of an operation with concurrency safety.

        Returns:
            None if registration succeeded (operation is new).
            IdempotencyRecord if operation was already recorded.
        """
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.execute(
                    "SELECT * FROM orchestration_idempotency_records WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                row = cur.fetchone()
                if row:
                    conn.execute("COMMIT;")
                    return self._row_to_record(row)

                now_str = datetime.now(timezone.utc).isoformat()
                try:
                    conn.execute(
                        """
                        INSERT INTO orchestration_idempotency_records (
                            idempotency_key, workflow_id, stage, status, created_at
                        ) VALUES (?, ?, ?, ?, ?);
                        """,
                        (
                            idempotency_key,
                            workflow_id,
                            stage.value,
                            OperationStatus.STARTED.value,
                            now_str,
                        ),
                    )
                    conn.execute("COMMIT;")
                    return None
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK;")
                    cur = conn.execute(
                        "SELECT * FROM orchestration_idempotency_records WHERE idempotency_key = ?",
                        (idempotency_key,),
                    )
                    row = cur.fetchone()
                    if row:
                        return self._row_to_record(row)
                    raise
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def mark_completed(
        self,
        idempotency_key: str,
        result_payload: Dict[str, Any],
        result_digest: Optional[str] = None,
    ) -> None:
        """Mark operation as completed with cached result."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                payload_json = json.dumps(result_payload, separators=(",", ":"))
                conn.execute(
                    """
                    UPDATE orchestration_idempotency_records SET
                        status = ?,
                        completed_at = ?,
                        result_digest = ?,
                        result_payload_json = ?
                    WHERE idempotency_key = ?;
                    """,
                    (
                        OperationStatus.COMPLETED.value,
                        now_str,
                        result_digest,
                        payload_json,
                        idempotency_key,
                    ),
                )
            finally:
                conn.close()

    def mark_failed(self, idempotency_key: str, error_message: str) -> None:
        """Mark operation as failed."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE orchestration_idempotency_records SET
                        status = ?,
                        completed_at = ?,
                        error_message = ?
                    WHERE idempotency_key = ?;
                    """,
                    (
                        OperationStatus.FAILED.value,
                        now_str,
                        error_message,
                        idempotency_key,
                    ),
                )
            finally:
                conn.close()

    def mark_uncertain(self, idempotency_key: str, reason: str) -> None:
        """Mark operation as uncertain (interrupted during mutation)."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE orchestration_idempotency_records SET
                        status = ?,
                        completed_at = ?,
                        error_message = ?
                    WHERE idempotency_key = ?;
                    """,
                    (
                        OperationStatus.UNCERTAIN.value,
                        now_str,
                        reason,
                        idempotency_key,
                    ),
                )
            finally:
                conn.close()

    def get_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Fetch record by idempotency key."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_idempotency_records WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                row = cur.fetchone()
                return self._row_to_record(row) if row else None
            finally:
                conn.close()

    get = get_record

    def get_records_for_workflow(self, workflow_id: str) -> List[IdempotencyRecord]:
        """Fetch all records for a given workflow ID."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "SELECT * FROM orchestration_idempotency_records WHERE workflow_id = ? ORDER BY created_at ASC",
                    (workflow_id,),
                )
                return [self._row_to_record(row) for row in cur.fetchall()]
            finally:
                conn.close()

    def mark_failed_for_workflow(self, workflow_id: str, stage: WorkflowStage, error_message: str) -> None:
        """Mark active (started or uncertain) records for workflow at given stage as failed."""
        with self._lock:
            conn = self._get_connection()
            try:
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE orchestration_idempotency_records SET
                        status = ?,
                        completed_at = ?,
                        error_message = ?
                    WHERE workflow_id = ? AND stage = ? AND status IN (?, ?);
                    """,
                    (
                        OperationStatus.FAILED.value,
                        now_str,
                        error_message,
                        workflow_id,
                        stage.value,
                        OperationStatus.STARTED.value,
                        OperationStatus.UNCERTAIN.value,
                    ),
                )
            finally:
                conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> IdempotencyRecord:
        raw_payload = row["result_payload_json"]
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError as exc:
                raise CorruptedStateError(
                    f"Corrupted idempotency payload JSON for key '{row['idempotency_key']}': {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise CorruptedStateError(
                    f"Corrupted idempotency payload for key '{row['idempotency_key']}': expected dict."
                )
        else:
            payload = None
        return IdempotencyRecord(
            idempotency_key=row["idempotency_key"],
            workflow_id=row["workflow_id"],
            stage=WorkflowStage(row["stage"]),
            status=OperationStatus(row["status"]),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            result_digest=row["result_digest"],
            result_payload=payload,
            error_message=row["error_message"],
        )
