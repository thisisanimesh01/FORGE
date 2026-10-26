"""SQLite database connection and schema initialization."""

import sqlite3
from pathlib import Path
from typing import Optional
from forge.config import get_settings


SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS repair_attempts (
    attempt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    finding_id TEXT NOT NULL,
    run_id TEXT,
    base_commit_sha TEXT NOT NULL,
    patch_digest TEXT NOT NULL,
    outcome TEXT NOT NULL,
    pre_repro_outcome TEXT NOT NULL,
    post_repro_outcome TEXT NOT NULL,
    exit_code INTEGER,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    sandbox_available INTEGER NOT NULL DEFAULT 1,
    oracle_verified INTEGER NOT NULL DEFAULT 1,
    diagnostic_message TEXT,
    isolation_details_json TEXT NOT NULL DEFAULT '{}',
    stdout_excerpt TEXT NOT NULL DEFAULT '',
    stderr_excerpt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES repair_plans(plan_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS repair_workflows (
    workflow_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    run_id TEXT,
    target_repo TEXT NOT NULL,
    base_commit_sha TEXT NOT NULL,
    policy_version TEXT NOT NULL DEFAULT 'v1',
    state TEXT NOT NULL,
    plan_id TEXT,
    patch_digest TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    sandbox_available INTEGER NOT NULL DEFAULT 1,
    outcome TEXT,
    verification_attempt_id TEXT,
    human_decision TEXT,
    human_notes TEXT,
    error_message TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    workflow_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES repair_plans(plan_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS repair_workflow_events (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(workflow_id) REFERENCES repair_workflows(workflow_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_intakes (
    intake_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    base_commit_sha TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    affected_files_json TEXT NOT NULL DEFAULT '[]',
    intake_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engineering_workflows (
    workflow_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    base_commit_sha TEXT NOT NULL,
    state TEXT NOT NULL,
    context_id TEXT,
    repro_case_id TEXT,
    repair_plan_id TEXT,
    verification_attempt_id TEXT,
    regression_summary_json TEXT NOT NULL DEFAULT '{}',
    human_decision TEXT,
    human_notes TEXT,
    error_message TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    workflow_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(intake_id) REFERENCES workflow_intakes(intake_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS engineering_workflow_events (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(workflow_id) REFERENCES engineering_workflows(workflow_id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_repair_attempts_plan ON repair_attempts(plan_id, attempt_number);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_attempts_plan_num ON repair_attempts(plan_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_repair_workflows_finding ON repair_workflows(finding_id);
CREATE INDEX IF NOT EXISTS idx_repair_workflows_state ON repair_workflows(state);
CREATE INDEX IF NOT EXISTS idx_repair_workflow_events_seq ON repair_workflow_events(workflow_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_workflow_events_seq ON repair_workflow_events(workflow_id, sequence);
CREATE INDEX IF NOT EXISTS idx_workflow_intakes_repo ON workflow_intakes(target_repo);
CREATE INDEX IF NOT EXISTS idx_eng_workflows_state ON engineering_workflows(state);
CREATE INDEX IF NOT EXISTS idx_eng_workflows_intake ON engineering_workflows(intake_id);
CREATE INDEX IF NOT EXISTS idx_eng_workflow_events_seq ON engineering_workflow_events(workflow_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS uq_eng_workflow_events_seq ON engineering_workflow_events(workflow_id, sequence);
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
            # Check migration records
            import datetime
            now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for ver in range(1, SCHEMA_VERSION + 1):
                cur = conn.execute("SELECT version FROM schema_migrations WHERE version = ?", (ver,))
                if not cur.fetchone():
                    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (ver, now_str))
    finally:
        conn.close()
