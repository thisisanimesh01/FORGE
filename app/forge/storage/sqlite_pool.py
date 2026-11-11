"""Thread-safe, per-thread SQLite connection pool for FORGE persistent ledgers and stores.

Eliminates repeated sqlite3.connect() and PRAGMA roundtrips across queries/writes while
preserving WAL durability, foreign keys, busy timeouts, explicit transaction semantics,
and deterministic cleanup via weakref.finalize().
"""

from pathlib import Path
import sqlite3
import threading
from typing import Any, List, Optional
import weakref


class _PooledConnection:
    """Lightweight connection proxy keeping the underlying thread-local handle open on close()."""

    def __init__(self, raw_conn: sqlite3.Connection) -> None:
        self._raw = raw_conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self._raw.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        return self._raw.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        return self._raw.executescript(sql_script)

    def close(self) -> None:
        """Reset any dangling aborted transaction without destroying the thread-local handle."""
        try:
            if self._raw.in_transaction:
                self._raw.rollback()
        except sqlite3.Error:
            pass


class ThreadLocalSQLitePool:
    """Maintains one initialized WAL SQLite connection per OS thread for a database file."""

    def __init__(self, db_path: Path, owner: Optional[object] = None) -> None:
        self.db_path = Path(db_path).resolve()
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_conns: List[sqlite3.Connection] = []
        self._closed = False
        self._dir_ensured = False

        if owner is not None:
            weakref.finalize(owner, _close_connections, self._all_conns, self._lock)

    def _open_connection(self) -> sqlite3.Connection:
        if not self._dir_ensured:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._dir_ensured = True

        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")

        with self._lock:
            self._all_conns.append(conn)
        return conn

    def get_connection(self) -> _PooledConnection:
        """Return the active thread-local SQLite connection wrapped for safe close()."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                # Validate handle hasn't been closed externally
                if conn.in_transaction:
                    conn.rollback()
                return _PooledConnection(conn)
            except sqlite3.Error:
                self._local.conn = None

        conn = self._open_connection()
        self._local.conn = conn
        return _PooledConnection(conn)

    def close_all(self) -> None:
        """Close all tracked thread-local SQLite connections cleanly."""
        self._local = threading.local()
        _close_connections(self._all_conns, self._lock)


def _close_connections(conns: List[sqlite3.Connection], lock: threading.Lock) -> None:
    with lock:
        to_close = list(conns)
        conns.clear()
    for c in to_close:
        try:
            if c.in_transaction:
                c.rollback()
        except sqlite3.Error:
            pass
        try:
            c.close()
        except sqlite3.Error:
            pass
