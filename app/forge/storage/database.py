"""SQLite database connection and schema initialization."""

import sqlite3
from pathlib import Path
from typing import Optional
from forge.config import get_settings


SCHEMA_VERSION = 1

INIT_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    affected_files_json TEXT NOT NULL DEFAULT '[]',
    suggested_step TEXT,
    limitations TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    finding_id TEXT,
    evidence_type TEXT NOT NULL,
    description TEXT NOT NULL,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    content TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS finding_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE,
    FOREIGN KEY(evidence_id) REFERENCES evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_findings_run_id ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_finding_id ON evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run_id ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_transitions_finding ON finding_transitions(finding_id);
CREATE INDEX IF NOT EXISTS idx_run_events_sequence ON run_events(run_id, sequence);
"""


def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create a SQLite connection with foreign keys enabled and WAL journaling."""
    path = db_path or get_settings().db_path
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_database(db_path: Optional[Path] = None) -> None:
    """Initialize SQLite database tables and apply migrations idempotently."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.executescript(INIT_SCHEMA_SQL)
            # Check migration record
            cur = conn.execute("SELECT version FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,))
            if not cur.fetchone():
                import datetime
                now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
                conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, now_str))
    finally:
        conn.close()
