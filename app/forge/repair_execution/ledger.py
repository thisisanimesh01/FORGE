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
        """Initialize the approval ledger table in SQLite."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS repair_approval_ledger (
                        approval_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        execution_id TEXT,
                        reason TEXT,
                        recorded_at TEXT NOT NULL
                    );
                """)
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
                        status TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );
                """)
            finally:
                conn.close()

    def _load_from_db(self) -> None:
        """Load historical records from database into memory cache."""
        with self._lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT approval_id, status, execution_id, reason FROM repair_approval_ledger;")
                for app_id, status, exec_id, reason in cur.fetchall():
                    if status == "consumed" and exec_id:
                        self._memory_consumed[app_id] = exec_id
                    elif status == "revoked":
                        self._memory_revoked[app_id] = reason or "revoked"

                cur.execute(
                    "SELECT approval_id, workflow_id, repair_plan_id, plan_digest, "
                    "approved_strategy_id, approved_file_paths, approval_timestamp, "
                    "approval_expiry, approver_reference, auth_channel, status, recorded_at "
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
                        "status": row[10],
                        "recorded_at": row[11],
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
                    # Check if already present
                    cur = conn.cursor()
                    cur.execute("SELECT status FROM repair_approval_ledger WHERE approval_id = ?;", (approval_id,))
                    row = cur.fetchone()
                    if row is not None:
                        conn.execute("ROLLBACK;")
                        return False

                    conn.execute(
                        "INSERT INTO repair_approval_ledger (approval_id, status, execution_id, recorded_at) VALUES (?, 'consumed', ?, ?);",
                        (approval_id, execution_id, now),
                    )
                    conn.execute("COMMIT;")
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    conn.execute("ROLLBACK;")
                    return False
                finally:
                    conn.close()

            self._memory_consumed[approval_id] = execution_id
            return True

    def record_revoked(self, approval_id: str, reason: str = "revoked by human operator") -> bool:
        """Atomically record an approval as revoked.

        Returns:
            True if recorded successfully.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self.db_path:
                conn = self._get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        """
                        INSERT INTO repair_approval_ledger (approval_id, status, reason, recorded_at)
                        VALUES (?, 'revoked', ?, ?)
                        ON CONFLICT(approval_id) DO UPDATE SET
                            status = 'revoked',
                            reason = excluded.reason,
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
            if approval_id in self._memory_consumed:
                return True
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM repair_approval_ledger WHERE approval_id = ? AND status = 'consumed';", (approval_id,))
                    return cur.fetchone() is not None
                finally:
                    conn.close()
            return False

    def is_revoked(self, approval_id: str) -> bool:
        """Check if an approval is revoked."""
        with self._lock:
            if approval_id in self._memory_revoked:
                return True
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM repair_approval_ledger WHERE approval_id = ? AND status = 'revoked';", (approval_id,))
                    return cur.fetchone() is not None
                finally:
                    conn.close()
            return False

    def get_consumed_map(self) -> Dict[str, str]:
        """Return a copy of consumed approval mappings."""
        with self._lock:
            return dict(self._memory_consumed)

    def get_revoked_map(self) -> Dict[str, str]:
        """Return a copy of revoked approval mappings."""
        with self._lock:
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
    ) -> bool:
        """Atomically persist a human approval contract and its workflow binding.

        Returns:
            True if recorded successfully.
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
                "status": status,
                "recorded_at": now,
            }
            if self.db_path:
                conn = self._get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        """
                        INSERT INTO orchestration_approval_records (
                            approval_id, workflow_id, repair_plan_id, plan_digest,
                            approved_strategy_id, approved_file_paths, approval_timestamp,
                            approval_expiry, approver_reference, auth_channel, status, recorded_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(approval_id) DO UPDATE SET
                            workflow_id = excluded.workflow_id,
                            repair_plan_id = excluded.repair_plan_id,
                            plan_digest = excluded.plan_digest,
                            approved_strategy_id = excluded.approved_strategy_id,
                            approved_file_paths = excluded.approved_file_paths,
                            approval_timestamp = excluded.approval_timestamp,
                            approval_expiry = excluded.approval_expiry,
                            approver_reference = excluded.approver_reference,
                            auth_channel = excluded.auth_channel,
                            status = excluded.status,
                            recorded_at = excluded.recorded_at;
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
                            status,
                            now,
                        ),
                    )
                    conn.execute("COMMIT;")
                finally:
                    conn.close()

            self._memory_approvals[approval_id] = record
            return True

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve persisted approval record by approval ID."""
        with self._lock:
            if approval_id in self._memory_approvals:
                return dict(self._memory_approvals[approval_id])
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT approval_id, workflow_id, repair_plan_id, plan_digest, "
                        "approved_strategy_id, approved_file_paths, approval_timestamp, "
                        "approval_expiry, approver_reference, auth_channel, status, recorded_at "
                        "FROM orchestration_approval_records WHERE approval_id = ?;",
                        (approval_id,),
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
                            "status": row[10],
                            "recorded_at": row[11],
                        }
                finally:
                    conn.close()
            return None

    def get_workflow_for_approval(self, approval_id: str) -> Optional[str]:
        """Retrieve bound workflow ID for an approval."""
        with self._lock:
            appr = self._memory_approvals.get(approval_id)
            if appr:
                return appr.get("workflow_id")
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT workflow_id FROM orchestration_approval_records WHERE approval_id = ?;", (approval_id,))
                    row = cur.fetchone()
                    if row:
                        return row[0]
                finally:
                    conn.close()
            return None

    def get_approval_for_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest active approval record for a plan ID."""
        with self._lock:
            for appr in self._memory_approvals.values():
                if appr.get("repair_plan_id") == plan_id:
                    return dict(appr)
            if self.db_path:
                conn = self._get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT approval_id, workflow_id, repair_plan_id, plan_digest, "
                        "approved_strategy_id, approved_file_paths, approval_timestamp, "
                        "approval_expiry, approver_reference, auth_channel, status, recorded_at "
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
                            "status": row[10],
                            "recorded_at": row[11],
                        }
                finally:
                    conn.close()
            return None
