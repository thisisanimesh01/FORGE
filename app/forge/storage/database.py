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

CREATE TABLE IF NOT EXISTS reproduction_cases (
    case_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    target_commit_sha TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0,
    retry_policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reproduction_attempts (
    attempt_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    exit_code INTEGER,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    stdout_digest TEXT,
    stderr_digest TEXT,
    stdout_excerpt TEXT NOT NULL DEFAULT '',
    stderr_excerpt TEXT NOT NULL DEFAULT '',
    stdout_truncated INTEGER NOT NULL DEFAULT 0,
    stderr_truncated INTEGER NOT NULL DEFAULT 0,
    matched_patterns_json TEXT NOT NULL DEFAULT '[]',
    evidence_id TEXT,
    isolation_details_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    diagnostic_message TEXT,
    cleanup_status TEXT NOT NULL DEFAULT 'cleaned',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES reproduction_cases(case_id) ON DELETE CASCADE,
    FOREIGN KEY(evidence_id) REFERENCES evidence(evidence_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS reproduction_classifications (
    classification_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    attempt_id TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    explanation TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    is_reproduced INTEGER NOT NULL DEFAULT 0,
    oracle_present INTEGER NOT NULL DEFAULT 0,
    oracle_verified INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    details_json TEXT NOT NULL DEFAULT '{}',
    classified_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES reproduction_cases(case_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS repair_plans (
    plan_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    run_id TEXT,
    base_commit_sha TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    status TEXT NOT NULL,
    target_file TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    patch_diff TEXT,
    validation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_findings_run_id ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_finding_id ON evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run_id ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_transitions_finding ON finding_transitions(finding_id);
CREATE INDEX IF NOT EXISTS idx_run_events_sequence ON run_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_reproduction_cases_finding ON reproduction_cases(finding_id);
CREATE INDEX IF NOT EXISTS idx_reproduction_attempts_case ON reproduction_attempts(case_id, attempt_number);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reproduction_attempts_case_num ON reproduction_attempts(case_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_reproduction_classifications_case ON reproduction_classifications(case_id);
CREATE INDEX IF NOT EXISTS idx_repair_plans_finding ON repair_plans(finding_id);
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
