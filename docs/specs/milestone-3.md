# Milestone 3 Specification: Evidence Model, Finding State Machine & SQLite Event History

## 1. Goal
Implement a robust, typed evidence recording system, strict finding lifecycle state machine, and append-only SQLite persistence layer for audit runs, findings, evidence, and event history in FORGE.

## 2. Architecture & Design

### A. Extended Schemas (`app/forge/schemas.py`)
- Evidence types:
  - `source_excerpt` (formerly `source_location`)
  - `symbol_reference`
  - `static_analysis` (formerly `static_check`)
  - `test_result`
  - `command_result` (formerly `command_output`)
  - `reproduction_result`
  - `root_cause_analysis`
  - `patch_reference`
  - `verification_result`
  - Backward compatibility aliases retained.
- `EvidenceRecord`:
  - `evidence_id`: str (e.g. `evi_<uuid>`)
  - `run_id`: str
  - `finding_id`: Optional[str]
  - `evidence_type`: EvidenceType
  - `description`: str (concise summary)
  - `file_path`: Optional[str] (validated relative path, strictly bounded)
  - `start_line`: Optional[int]
  - `end_line`: Optional[int]
  - `content`: str (capped at 50,000 characters, secrets redacted)
  - `details`: Dict[str, Any]
  - `provenance`: str (e.g. `forge:baseline_check`, `forge:ast_indexer`, `forge:reproducer`)
  - `created_at`: datetime (UTC)
- Finding Lifecycle:
  - `suspected -> reproduced -> root_caused -> fixed -> verified`
- Transition Events & Run Events:
  - Added event types: `finding_state_transitioned`, `verification_passed`, `verification_failed`.

### B. Finding State Machine & Transition Rules (`app/forge/analysis/findings.py`)
- Strictly validated lifecycle transitions:
  - Initial state for every new finding: `suspected`.
  - `suspected -> reproduced`: Requires evidence of type `reproduction_result` associated with the finding and run, indicating successful reproduction.
  - `reproduced -> root_caused`: Requires evidence of type `root_cause_analysis` associated with the finding and run.
  - `root_caused -> fixed`: Requires evidence of type `patch_reference` associated with the finding and run.
  - `fixed -> verified`: Requires evidence of type `verification_result` with status indicating success.
- Direct state manipulation is blocked; transitions must go through `transition_finding()`.
- Backward transitions are rejected.
- Failed verification cannot transition to `verified`.
- Evidence must match the exact finding ID and run ID.
- Mismatched or absent evidence raises `InvalidTransitionError`.

### C. SQLite Persistence Layer (`app/forge/storage/`)
- Database connection manager with WAL mode, foreign key enforcement (`PRAGMA foreign_keys = ON;`), and busy timeout.
- Schema version table (`schema_migrations`) with idempotent migrations.
- Tables:
  - `runs`: run metadata (`run_id`, `target`, `status`, `started_at`, `completed_at`, `summary_json`).
  - `findings`: findings metadata (`finding_id`, `run_id`, `title`, `description`, `severity`, `confidence`, `lifecycle_state`, `affected_files_json`, `created_at`, `updated_at`).
  - `evidence`: evidence records (`evidence_id`, `run_id`, `finding_id`, `evidence_type`, `description`, `file_path`, `start_line`, `end_line`, `content`, `details_json`, `provenance`, `created_at`).
  - `finding_transitions`: audit trail of state changes (`transition_id`, `finding_id`, `run_id`, `from_state`, `to_state`, `evidence_id`, `reason`, `created_at`).
  - `run_events`: append-only event log (`event_id`, `run_id`, `sequence`, `event_type`, `timestamp`, `payload_json`).
- Atomic transactions:
  - Attaching evidence and transitioning states occur within a single database transaction alongside the corresponding `RunEvent` and `finding_transitions` row.
  - If any validation or SQL constraint fails, the transaction rolls back cleanly.

### D. Run & Finding Store API (`app/forge/storage/store.py`)
- `create_run(run_id, target, ...)`
- `get_run(run_id)`
- `finish_run(run_id, status, ...)`
- `list_runs(limit=50)`
- `create_finding(run_id, title, ...)`
- `get_finding(finding_id)`
- `list_findings(run_id=None)`
- `attach_evidence(evidence)`
- `list_evidence_for_finding(finding_id)`
- `transition_finding(finding_id, to_state, evidence_id, reason)`
- `append_event(run_id, event_type, payload)`
- `list_run_events(run_id)`

### E. Lifecycle Replay & Reconstruction (`app/forge/analysis/reconstruction.py`)
- `reconstruct_finding_lifecycle(finding_id, store)`:
  - Replays transition events in sequential order starting from `suspected`.
  - Verifies that each state transition corresponds to valid evidence and sequential rules.
  - Compares reconstructed state with persisted current state in `findings`.
  - Raises `ReconstructionError` if history is broken, contradictory, or unevidenced.

### F. CLI Updates (`app/forge/cli.py`)
- `forge db init`: Idempotently initializes database tables.
- `forge runs`: Lists audit runs from SQLite.
- `forge findings list [--run-id <run_id>]`: Lists recorded findings.
- `forge findings show <finding_id>`: Shows finding details, current state, and attached evidence records.

## 3. Testing Strategy
- Unit tests for evidence validation (path bounds, truncation, secret redaction, type checks).
- Unit tests for finding state machine transitions (valid progression, invalid steps, wrong evidence, failed verification).
- Storage integration tests (SQLite FK enforcement, rollbacks, atomic transitions, append-only event constraints).
- Replay/reconstruction tests (verifying valid trails and detecting corrupt/counterfeit histories).
- CLI integration tests (`forge db init`, `forge runs`, `forge findings list`, `forge findings show`).
