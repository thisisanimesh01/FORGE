"""Phase 12 M4: Persistent Approval Ledger.

Provides:
- Thread-safe and process-durable tracking of consumed and revoked approvals.
- SQLite-backed persistence surviving process restarts.
- Atomic check-and-consume invariants preventing concurrent replay attacks.
- Graceful in-memory fallback when database storage is not configured.
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Dict, Optional, Set, Tuple


class ApprovalLedger:
    """Manages persistent tracking of approval consumption and revocation."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path.resolve() if db_path else None
        self._lock = threading.Lock()
        self._memory_consumed: Dict[str, str] = {}
        self._memory_revoked: Dict[str, str] = {}

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
