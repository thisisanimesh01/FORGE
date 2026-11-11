"""Phase 12 M4: Persistent Approval Ledger.

Provides:
- Thread-safe and process-durable tracking of consumed and revoked approvals.
- SQLite-backed persistence surviving process restarts.
- Atomic check-and-consume invariants preventing concurrent replay attacks.
- Graceful in-memory fallback when database storage is not configured.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Set, Tuple


class ApprovalLedger:
    """Manages persistent tracking of approval consumption, revocation, and workflow bindings."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path.resolve() if db_path else None
        self._lock = threading.Lock()
        self._memory_consumed: Dict[str, str] = {}
        self._memory_revoked: Dict[str, str] = {}
        self._memory_approvals: Dict[str, Dict[str, Any]] = {}

        if self.db_path:
            self._init_db()
            self._load_from_db()

    def _get_connection(self) -> sqlite3.Connection:
        if not self.db_path:
            raise RuntimeError("Database path is not configured.")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_db(self) -> None:
        """Initialize the approval ledger table in SQLite with safe migrations."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS repair_approval_ledger (
                        approval_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        execution_id TEXT,
                        reason TEXT,
                        revoked INTEGER NOT NULL DEFAULT 0,
                        recorded_at TEXT NOT NULL
                    );
                """)
                cur = conn.execute("PRAGMA table_info(repair_approval_ledger);")
                ledger_cols = {row[1] for row in cur.fetchall()}
                if "revoked" not in ledger_cols:
                    conn.execute("ALTER TABLE repair_approval_ledger ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0;")

                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orchestration_approval_records (
                        approval_id TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        repair_plan_id TEXT NOT NULL,
                        plan_digest TEXT NOT NULL,
                        approved_strategy_id TEXT NOT NULL,
                        approved_file_paths TEXT NOT NULL,
                        approval_timestamp TEXT NOT NULL,
                        approval_expiry TEXT NOT NULL,
                        approver_reference TEXT NOT NULL,
                        auth_channel TEXT NOT NULL,
                        operator_principal TEXT,
                        status TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );
                """)
                cur = conn.execute("PRAGMA table_info(orchestration_approval_records);")
                rec_cols = {row[1] for row in cur.fetchall()}
                if "operator_principal" not in rec_cols:
                    conn.execute("ALTER TABLE orchestration_approval_records ADD COLUMN operator_principal TEXT;")
            finally:
                conn.close()

    def _load_from_db(self) -> None:
        """Load historical records from database into memory cache."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT approval_id, status, execution_id, reason, revoked FROM repair_approval_ledger;")
                for app_id, status, exec_id, reason, revoked in cur.fetchall():
                    if status == "consumed" and exec_id:
                        self._memory_consumed[app_id] = exec_id
                    if status == "revoked" or revoked == 1:
                        self._memory_revoked[app_id] = reason or "revoked"

                cur.execute(
                    "SELECT approval_id, workflow_id, repair_plan_id, plan_digest, "
                    "approved_strategy_id, approved_file_paths, approval_timestamp, "
                    "approval_expiry, approver_reference, auth_channel, operator_principal, status, recorded_at "
                    "FROM orchestration_approval_records;"
                )
                for row in cur.fetchall():
                    try:
                        files = json.loads(row[5])
                    except Exception:
                        files = []
                    self._memory_approvals[row[0]] = {
                        "approval_id": row[0],
                        "workflow_id": row[1],
                        "repair_plan_id": row[2],
                        "plan_digest": row[3],
                        "approved_strategy_id": row[4],
                        "approved_file_paths": files,
                        "approval_timestamp": row[6],
                        "approval_expiry": row[7],
                        "approver_reference": row[8],
                        "auth_channel": row[9],
                        "operator_principal": row[10],
                        "status": row[11],
                        "recorded_at": row[12],
                    }
            finally:
                conn.close()

    def record_consumed(self, approval_id: str, execution_id: str) -> bool:
        """Atomically record an approval as consumed.

        Returns:
            True if recorded successfully; False if already consumed or revoked.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if approval_id in self._memory_consumed or approval_id in self._memory_revoked:
                return False

            if self.db_path:
                conn = self._get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT status, revoked FROM repair_approval_ledger WHERE approval_id = ?;",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        conn.execute("ROLLBACK;")
                        return False

                    conn.execute(
                        "INSERT INTO repair_approval_ledger (approval_id, status, execution_id, revoked, recorded_at) VALUES (?, 'consumed', ?, 0, ?);",
                        (approval_id, execution_id, now),
                    )
                    conn.execute("COMMIT;")
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                    return False
                finally:
                    conn.close()

            self._memory_consumed[approval_id] = execution_id
            return True

    def record_revoked(self, approval_id: str, reason: str = "revoked by human operator") -> bool:
        """Atomically record an approval as revoked without erasing any prior 'consumed' status.

        Returns:
            True if recorded successfully.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cur = conn.execute(
                        "SELECT status FROM repair_approval_ledger WHERE approval_id = ?;",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and row[0] == "consumed":
                        # Never overwrite status='consumed' with 'revoked'; mark revoked=1 alongside consumed
                        conn.execute(
                            "UPDATE repair_approval_ledger SET revoked = 1, reason = ? WHERE approval_id = ?;",
                            (reason, approval_id),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO repair_approval_ledger (approval_id, status, reason, revoked, recorded_at)
                            VALUES (?, 'revoked', ?, 1, ?)
                            ON CONFLICT(approval_id) DO UPDATE SET
                                status = 'revoked',
                                reason = excluded.reason,
                                revoked = 1,
                                recorded_at = excluded.recorded_at;
                            """,
                            (approval_id, reason, now),
                        )
                    conn.execute("COMMIT;")
                finally:
                    conn.close()

            self._memory_revoked[approval_id] = reason
            return True

    def is_consumed(self, approval_id: str) -> bool:
        """Check if an approval is consumed."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT execution_id FROM repair_approval_ledger WHERE approval_id = ? AND status = 'consumed';",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        self._memory_consumed[approval_id] = row[0] or "recorded_execution"
                        return True
                    return False
                finally:
                    conn.close()
            return approval_id in self._memory_consumed

    def is_revoked(self, approval_id: str) -> bool:
        """Check if an approval is revoked."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT reason FROM repair_approval_ledger WHERE approval_id = ? AND (status = 'revoked' OR revoked = 1);",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        self._memory_revoked[approval_id] = row[0] or "revoked"
                        return True
                    return False
                finally:
                    conn.close()
            return approval_id in self._memory_revoked

    def get_consumed_map(self) -> Dict[str, str]:
        """Return a copy of consumed approval mappings (live from SQLite when configured)."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.execute(
                        "SELECT approval_id, execution_id FROM repair_approval_ledger WHERE status = 'consumed';"
                    )
                    res = {row[0]: (row[1] or "recorded_execution") for row in cur.fetchall()}
                    self._memory_consumed = dict(res)
                    return res
                finally:
                    conn.close()
            return dict(self._memory_consumed)

    def get_revoked_map(self) -> Dict[str, str]:
        """Return a copy of revoked approval mappings (live from SQLite when configured)."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.execute(
                        "SELECT approval_id, reason FROM repair_approval_ledger WHERE status = 'revoked' OR revoked = 1;"
                    )
                    res = {row[0]: (row[1] or "revoked") for row in cur.fetchall()}
                    self._memory_revoked = dict(res)
                    return res
                finally:
                    conn.close()
            return dict(self._memory_revoked)

    def record_approval(
        self,
        approval_id: str,
        workflow_id: str,
        repair_plan_id: str,
        plan_digest: str,
        approved_strategy_id: str,
        approved_file_paths: List[str],
        approval_timestamp: str,
        approval_expiry: str,
        approver_reference: str,
        auth_channel: str = "interactive_cli",
        status: str = "approved",
        operator_principal: Optional[str] = None,
    ) -> bool:
        """Atomically persist an immutable human approval contract and its workflow binding.

        Rejects conflicting updates that attempt to rebind an existing approval_id
        to a different workflow, plan, digest, strategy, or approver.

        Returns:
            True if recorded or identically present; raises ValueError on conflicting mutation.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            record = {
                "approval_id": approval_id,
                "workflow_id": workflow_id,
                "repair_plan_id": repair_plan_id,
                "plan_digest": plan_digest,
                "approved_strategy_id": approved_strategy_id,
                "approved_file_paths": list(approved_file_paths),
                "approval_timestamp": approval_timestamp,
                "approval_expiry": approval_expiry,
                "approver_reference": approver_reference,
                "auth_channel": auth_channel,
                "operator_principal": operator_principal,
                "status": status,
                "recorded_at": now,
            }
            if self.db_path:
                conn = self._get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cur = conn.execute(
                        "SELECT workflow_id, repair_plan_id, plan_digest, approved_strategy_id, approver_reference "
                        "FROM orchestration_approval_records WHERE approval_id = ?;",
                        (approval_id,),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        if (
                            existing[0] != workflow_id
                            or existing[1] != repair_plan_id
                            or existing[2] != plan_digest
                            or existing[3] != approved_strategy_id
                            or existing[4] != approver_reference
                        ):
                            conn.execute("ROLLBACK;")
                            raise ValueError(
                                f"Immutable approval conflict: approval_id '{approval_id}' is already bound "
                                f"to workflow '{existing[0]}' and plan '{existing[1]}' and cannot be overwritten."
                            )
                        conn.execute("COMMIT;")
                        self._memory_approvals[approval_id] = record
                        return True

                    conn.execute(
                        """
                        INSERT INTO orchestration_approval_records (
                            approval_id, workflow_id, repair_plan_id, plan_digest,
                            approved_strategy_id, approved_file_paths, approval_timestamp,
                            approval_expiry, approver_reference, auth_channel, operator_principal, status, recorded_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            approval_id,
                            workflow_id,
                            repair_plan_id,
                            plan_digest,
                            approved_strategy_id,
                            json.dumps(approved_file_paths),
                            approval_timestamp,
                            approval_expiry,
                            approver_reference,
                            auth_channel,
                            operator_principal,
                            status,
                            now,
                        ),
                    )
                    conn.execute("COMMIT;")
                finally:
                    conn.close()
            elif approval_id in self._memory_approvals:
                existing_mem = self._memory_approvals[approval_id]
                if (
                    existing_mem["workflow_id"] != workflow_id
                    or existing_mem["repair_plan_id"] != repair_plan_id
                    or existing_mem["plan_digest"] != plan_digest
                    or existing_mem["approved_strategy_id"] != approved_strategy_id
                    or existing_mem["approver_reference"] != approver_reference
                ):
                    raise ValueError(
                        f"Immutable approval conflict: approval_id '{approval_id}' is already bound "
                        f"to workflow '{existing_mem['workflow_id']}' and cannot be overwritten."
                    )

            self._memory_approvals[approval_id] = record
            return True

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve persisted approval record by approval ID."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT approval_id, workflow_id, repair_plan_id, plan_digest, "
                        "approved_strategy_id, approved_file_paths, approval_timestamp, "
                        "approval_expiry, approver_reference, auth_channel, operator_principal, status, recorded_at "
                        "FROM orchestration_approval_records WHERE approval_id = ?;",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        rec = {
                            "approval_id": row[0],
                            "workflow_id": row[1],
                            "repair_plan_id": row[2],
                            "plan_digest": row[3],
                            "approved_strategy_id": row[4],
                            "approved_file_paths": json.loads(row[5]),
                            "approval_timestamp": row[6],
                            "approval_expiry": row[7],
                            "approver_reference": row[8],
                            "auth_channel": row[9],
                            "operator_principal": row[10],
                            "status": row[11],
                            "recorded_at": row[12],
                        }
                        self._memory_approvals[approval_id] = rec
                        return dict(rec)
                    return None
                finally:
                    conn.close()
            if approval_id in self._memory_approvals:
                return dict(self._memory_approvals[approval_id])
            return None

    def get_workflow_for_approval(self, approval_id: str) -> Optional[str]:
        """Retrieve bound workflow ID for an approval."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT workflow_id FROM orchestration_approval_records WHERE approval_id = ?;", (approval_id,))
                    row = cur.fetchone()
                    if row:
                        return row[0]
                    return None
                finally:
                    conn.close()
            appr = self._memory_approvals.get(approval_id)
            if appr:
                return appr.get("workflow_id")
            return None

    def get_approval_for_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest active approval record for a plan ID."""
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT approval_id, workflow_id, repair_plan_id, plan_digest, "
                        "approved_strategy_id, approved_file_paths, approval_timestamp, "
                        "approval_expiry, approver_reference, auth_channel, operator_principal, status, recorded_at "
                        "FROM orchestration_approval_records WHERE repair_plan_id = ? ORDER BY recorded_at DESC LIMIT 1;",
                        (plan_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        return {
                            "approval_id": row[0],
                            "workflow_id": row[1],
                            "repair_plan_id": row[2],
                            "plan_digest": row[3],
                            "approved_strategy_id": row[4],
                            "approved_file_paths": json.loads(row[5]),
                            "approval_timestamp": row[6],
                            "approval_expiry": row[7],
                            "approver_reference": row[8],
                            "auth_channel": row[9],
                            "operator_principal": row[10],
                            "status": row[11],
                            "recorded_at": row[12],
                        }
                    return None
                finally:
                    conn.close()
            for appr in self._memory_approvals.values():
                if appr.get("repair_plan_id") == plan_id:
                    return dict(appr)
            return None
