"""SQLite store implementation for runs, findings, evidence, transitions, and events."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.config import redact_secrets
from forge.schemas import (
    Finding,
    FindingLifecycleState,
    FindingSeverity,
    FindingConfidence,
    EvidenceRecord,
    EvidenceType,
    RunEvent,
    EventType,
    RunSummary,
)
from forge.storage.database import get_db_connection, init_database


class StoreError(Exception):
    """Raised when a storage operation fails or a resource is missing."""
    pass


class ForgeStore:
    """Manages persistent operations in SQLite."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        init_database(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    # ---------------------------------------------------------
    # Runs API
    # ---------------------------------------------------------
    def create_run(self, target: str, run_id: Optional[str] = None) -> str:
        """Create a new run record and append RUN_CREATED event."""
        rid = run_id or f"run_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO runs (run_id, target, status, started_at, summary_json) VALUES (?, ?, ?, ?, ?)",
                    (rid, target, "running", now, "{}"),
                )
                self._append_event_locked(
                    conn,
                    rid,
                    EventType.RUN_CREATED,
                    {"target": target, "started_at": now},
                )
            return rid
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Optional[RunSummary]:
        """Fetch summary of a run."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            row = cur.fetchone()
            if not row:
                return None

            findings_cnt = conn.execute("SELECT count(*) FROM findings WHERE run_id = ?", (run_id,)).fetchone()[0]
            evidence_cnt = conn.execute("SELECT count(*) FROM evidence WHERE run_id = ?", (run_id,)).fetchone()[0]

            return RunSummary(
                run_id=row["run_id"],
                target=row["target"],
                started_at=datetime.fromisoformat(row["started_at"]),
                completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
                status=row["status"],
                total_findings=findings_cnt,
                total_evidence=evidence_cnt,
            )
        finally:
            conn.close()

    def finish_run(self, run_id: str, status: str = "completed", summary: Optional[Dict[str, Any]] = None) -> None:
        """Mark a run as completed or failed and record corresponding event."""
        now = datetime.now(timezone.utc).isoformat()
        safe_summary = summary or {}
        event_type = EventType.RUN_COMPLETED if status == "completed" else EventType.RUN_FAILED

        conn = self._conn()
        try:
            with conn:
                cur = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,))
                if not cur.fetchone():
                    raise StoreError(f"Run '{run_id}' not found")

                conn.execute(
                    "UPDATE runs SET status = ?, completed_at = ?, summary_json = ? WHERE run_id = ?",
                    (status, now, json.dumps(safe_summary), run_id),
                )
                self._append_event_locked(conn, run_id, event_type, {"status": status, "summary": safe_summary})
        finally:
            conn.close()

    def list_runs(self, limit: int = 50) -> List[RunSummary]:
        """List historical audit runs."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            return [self.get_run(r["run_id"]) for r in rows if self.get_run(r["run_id"])]
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Findings API
    # ---------------------------------------------------------
    def create_finding(
        self,
        run_id: str,
        title: str,
        description: str,
        severity: FindingSeverity = FindingSeverity.MEDIUM,
        confidence: FindingConfidence = FindingConfidence.MEDIUM,
        affected_files: Optional[List[str]] = None,
        suggested_step: Optional[str] = None,
        limitations: Optional[str] = None,
        finding_id: Optional[str] = None,
    ) -> Finding:
        """Create a new finding in SUSPECTED state and persist atomically with event."""
        fid = finding_id or f"find_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        files = affected_files or []

        finding = Finding(
            finding_id=fid,
            title=title,
            description=description,
            severity=severity,
            confidence=confidence,
            lifecycle_state=FindingLifecycleState.SUSPECTED,
            affected_files=files,
            suggested_investigation_step=suggested_step,
            limitations=limitations,
            created_at=datetime.fromisoformat(now),
        )

        conn = self._conn()
        try:
            with conn:
                # Confirm run exists
                if not conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
                    raise StoreError(f"Cannot create finding: Run '{run_id}' does not exist")

                conn.execute(
                    """
                    INSERT INTO findings (
                        finding_id, run_id, title, description, severity, confidence,
                        lifecycle_state, affected_files_json, suggested_step, limitations,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fid,
                        run_id,
                        title,
                        description,
                        severity.value,
                        confidence.value,
                        FindingLifecycleState.SUSPECTED.value,
                        json.dumps(files),
                        suggested_step,
                        limitations,
                        now,
                        now,
                    ),
                )
                self._append_event_locked(
                    conn,
                    run_id,
                    EventType.FINDING_CREATED,
                    {"finding_id": fid, "title": title, "severity": severity.value},
                )
            return finding
        finally:
            conn.close()

    def get_finding(self, finding_id: str) -> Optional[Finding]:
        """Retrieve finding and all associated evidence IDs."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM findings WHERE finding_id = ?", (finding_id,))
            row = cur.fetchone()
            if not row:
                return None

            ev_rows = conn.execute("SELECT evidence_id FROM evidence WHERE finding_id = ?", (finding_id,)).fetchall()
            ev_ids = [r["evidence_id"] for r in ev_rows]

            return Finding(
                finding_id=row["finding_id"],
                run_id=row["run_id"],
                title=row["title"],
                description=row["description"],
                severity=FindingSeverity(row["severity"]),
                confidence=FindingConfidence(row["confidence"]),
                lifecycle_state=FindingLifecycleState(row["lifecycle_state"]),
                affected_files=json.loads(row["affected_files_json"]),
                supporting_evidence_ids=ev_ids,
                suggested_investigation_step=row["suggested_step"],
                limitations=row["limitations"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )

        finally:
            conn.close()

    def list_findings(self, run_id: Optional[str] = None) -> List[Finding]:
        """List findings optionally filtered by run_id."""
        conn = self._conn()
        try:
            if run_id:
                cur = conn.execute("SELECT finding_id FROM findings WHERE run_id = ? ORDER BY created_at ASC", (run_id,))
            else:
                cur = conn.execute("SELECT finding_id FROM findings ORDER BY created_at ASC")
            return [self.get_finding(r["finding_id"]) for r in cur.fetchall()]
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Evidence API
    # ---------------------------------------------------------
    def attach_evidence(self, evidence: EvidenceRecord) -> None:
        """Persist evidence record and attach to finding and run atomically with event."""
        conn = self._conn()
        try:
            with conn:
                # Verify run exists
                if not conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (evidence.run_id,)).fetchone():
                    raise StoreError(f"Run '{evidence.run_id}' does not exist")

                # Verify finding exists if provided
                if evidence.finding_id:
                    if not conn.execute("SELECT 1 FROM findings WHERE finding_id = ?", (evidence.finding_id,)).fetchone():
                        raise StoreError(f"Finding '{evidence.finding_id}' does not exist")

                conn.execute(
                    """
                    INSERT INTO evidence (
                        evidence_id, run_id, finding_id, evidence_type, description,
                        file_path, start_line, end_line, content, details_json, provenance, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.evidence_id,
                        evidence.run_id,
                        evidence.finding_id,
                        evidence.evidence_type.value,
                        evidence.description,
                        evidence.file_path,
                        evidence.start_line,
                        evidence.end_line,
                        evidence.content,
                        json.dumps(evidence.details),
                        evidence.provenance,
                        evidence.created_at.isoformat(),
                    ),
                )
                self._append_event_locked(
                    conn,
                    evidence.run_id,
                    EventType.EVIDENCE_RECORDED,
                    {
                        "evidence_id": evidence.evidence_id,
                        "finding_id": evidence.finding_id,
                        "evidence_type": evidence.evidence_type.value,
                    },
                )
        finally:
            conn.close()

    def get_evidence(self, evidence_id: str) -> Optional[EvidenceRecord]:
        """Retrieve evidence by ID."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,))
            row = cur.fetchone()
            if not row:
                return None
            return EvidenceRecord(
                evidence_id=row["evidence_id"],
                run_id=row["run_id"],
                finding_id=row["finding_id"],
                evidence_type=EvidenceType(row["evidence_type"]),
                description=row["description"],
                file_path=row["file_path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                content=row["content"],
                details=json.loads(row["details_json"]),
                provenance=row["provenance"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        finally:
            conn.close()

    def list_evidence_for_finding(self, finding_id: str) -> List[EvidenceRecord]:
        """List all evidence attached to a specific finding."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT evidence_id FROM evidence WHERE finding_id = ? ORDER BY created_at ASC", (finding_id,))
            return [self.get_evidence(r["evidence_id"]) for r in cur.fetchall()]
        finally:
            conn.close()

    # ---------------------------------------------------------
    # State Machine & Transitions API
    # ---------------------------------------------------------
    def transition_finding(
        self,
        finding_id: str,
        to_state: FindingLifecycleState | str,
        evidence_id: str,
        reason: Optional[str] = None,
    ) -> Finding:
        """Validate evidence, transition finding state, record transition, and append event atomically."""
        conn = self._conn()
        try:
            with conn:
                finding = self.get_finding(finding_id)
                if not finding:
                    raise StoreError(f"Finding '{finding_id}' not found")

                evidence = self.get_evidence(evidence_id)
                if not evidence:
                    raise StoreError(f"Evidence '{evidence_id}' not found")

                target_enum = FindingLifecycleState(to_state) if isinstance(to_state, str) else to_state

                # Validate transition legality and evidence match
                validate_transition(finding, target_enum, evidence)

                now = datetime.now(timezone.utc).isoformat()
                from_state = finding.lifecycle_state.value
                # Update finding state atomically checking current state
                cur = conn.execute(
                    "UPDATE findings SET lifecycle_state = ?, updated_at = ? WHERE finding_id = ? AND lifecycle_state = ?",
                    (target_enum.value, now, finding_id, from_state),
                )

                if cur.rowcount == 0:
                    raise StoreError(
                        f"Concurrent modification or state change detected for finding '{finding_id}'. Transition aborted."
                    )


                # Record transition row
                conn.execute(
                    """
                    INSERT INTO finding_transitions (
                        finding_id, run_id, from_state, to_state, evidence_id, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (finding_id, evidence.run_id, from_state, target_enum.value, evidence_id, reason, now),
                )

                # Append transition event
                self._append_event_locked(
                    conn,
                    evidence.run_id,
                    EventType.FINDING_STATE_TRANSITIONED,
                    {
                        "finding_id": finding_id,
                        "from_state": from_state,
                        "to_state": target_enum.value,
                        "evidence_id": evidence_id,
                        "reason": reason,
                    },
                )

                if target_enum == FindingLifecycleState.VERIFIED:
                    self._append_event_locked(
                        conn,
                        evidence.run_id,
                        EventType.VERIFICATION_PASSED,
                        {"finding_id": finding_id, "evidence_id": evidence_id},
                    )

            return self.get_finding(finding_id)
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Events API
    # ---------------------------------------------------------
    def _append_event_locked(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_type: EventType | str,
        payload: Dict[str, Any],
    ) -> RunEvent:
        """Append an event using the open transaction connection to guarantee ordering."""
        cur = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,))
        seq = cur.fetchone()[0]
        now = datetime.now(timezone.utc).isoformat()
        eid = f"evt_{uuid.uuid4().hex[:12]}"
        ev_type = EventType(event_type) if isinstance(event_type, str) else event_type

        # Redact secrets from payload before saving
        sanitized_payload = json.loads(redact_secrets(json.dumps(payload)))

        conn.execute(
            """
            INSERT INTO run_events (event_id, run_id, sequence, event_type, timestamp, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (eid, run_id, seq, ev_type.value, now, json.dumps(sanitized_payload)),
        )

        return RunEvent(
            event_id=eid,
            run_id=run_id,
            sequence=seq,
            event_type=ev_type,
            timestamp=datetime.fromisoformat(now),
            payload=sanitized_payload,
        )

    def append_event(self, run_id: str, event_type: EventType | str, payload: Dict[str, Any]) -> RunEvent:
        """Public API to append an event to the run log."""
        conn = self._conn()
        try:
            with conn:
                return self._append_event_locked(conn, run_id, event_type, payload)
        finally:
            conn.close()

    def list_run_events(self, run_id: str) -> List[RunEvent]:
        """Fetch all events for a run in deterministic sequence order."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence ASC",
                (run_id,),
            )
            rows = cur.fetchall()
            return [
                RunEvent(
                    event_id=r["event_id"],
                    run_id=r["run_id"],
                    sequence=r["sequence"],
                    event_type=EventType(r["event_type"]),
                    timestamp=datetime.fromisoformat(r["timestamp"]),
                    payload=json.loads(r["payload_json"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Repair Plans API
    # ---------------------------------------------------------
    def save_repair_plan(self, plan: Any) -> None:
        """Persist or update a repair plan in SQLite."""
        from forge.repair.models import RepairPlan

        plan_model = plan if isinstance(plan, RepairPlan) else RepairPlan.model_validate(plan)
        now = datetime.now(timezone.utc).isoformat()
        plan_json = plan_model.model_dump_json()
        val_json = plan_model.validation.model_dump_json() if plan_model.validation else "{}"
        diff_text = plan_model.patch_candidate.unified_diff if plan_model.patch_candidate else None

        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO repair_plans (
                        plan_id, finding_id, run_id, base_commit_sha, target_repo,
                        status, target_file, plan_json, patch_diff, validation_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id) DO UPDATE SET
                        status = excluded.status,
                        plan_json = excluded.plan_json,
                        patch_diff = excluded.patch_diff,
                        validation_json = excluded.validation_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        plan_model.plan_id,
                        plan_model.finding_id,
                        plan_model.run_id,
                        plan_model.base_commit_sha,
                        plan_model.target_repo,
                        plan_model.status.value,
                        plan_model.target_file,
                        plan_json,
                        diff_text,
                        val_json,
                        plan_model.created_at.isoformat(),
                        now,
                    ),
                )
        finally:
            conn.close()

    def get_repair_plan(self, plan_id: str) -> Optional[Any]:
        """Retrieve a repair plan by ID."""
        from forge.repair.models import RepairPlan

        conn = self._conn()
        try:
            cur = conn.execute("SELECT plan_json FROM repair_plans WHERE plan_id = ?", (plan_id,))
            row = cur.fetchone()
            if not row:
                return None
            return RepairPlan.model_validate_json(row["plan_json"])
        finally:
            conn.close()

    def list_repair_plans(self, finding_id: Optional[str] = None) -> List[Any]:
        """List repair plans, optionally filtered by finding_id."""
        from forge.repair.models import RepairPlan

        conn = self._conn()
        try:
            if finding_id:
                cur = conn.execute(
                    "SELECT plan_json FROM repair_plans WHERE finding_id = ? ORDER BY created_at DESC",
                    (finding_id,),
                )
            else:
                cur = conn.execute("SELECT plan_json FROM repair_plans ORDER BY created_at DESC")
            rows = cur.fetchall()
            return [RepairPlan.model_validate_json(r["plan_json"]) for r in rows]
        finally:
            conn.close()

    def get_next_repair_attempt_number(self, plan_id: str) -> int:
        """Get the next sequential attempt number for a repair plan."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM repair_attempts WHERE plan_id = ?",
                (plan_id,),
            )
            return cur.fetchone()[0]
        finally:
            conn.close()

    def save_repair_attempt(self, attempt: Any) -> None:
        """Persist a repair attempt record in SQLite."""
        from forge.repair.models import RepairAttemptRecord

        att = attempt if isinstance(attempt, RepairAttemptRecord) else RepairAttemptRecord.model_validate(attempt)
        iso_details = json.dumps(att.isolation_details)

        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO repair_attempts (
                        attempt_id, plan_id, attempt_number, finding_id, run_id,
                        base_commit_sha, patch_digest, outcome, pre_repro_outcome,
                        post_repro_outcome, exit_code, duration_ms, sandbox_available,
                        oracle_verified, diagnostic_message, isolation_details_json,
                        stdout_excerpt, stderr_excerpt, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        att.attempt_id,
                        att.plan_id,
                        att.attempt_number,
                        att.finding_id,
                        att.run_id,
                        att.base_commit_sha,
                        att.patch_digest,
                        att.outcome.value,
                        att.pre_repro_outcome.value,
                        att.post_repro_outcome.value,
                        att.exit_code,
                        att.duration_ms,
                        1 if att.sandbox_available else 0,
                        1 if att.oracle_verified else 0,
                        att.diagnostic_message,
                        iso_details,
                        att.stdout_excerpt,
                        att.stderr_excerpt,
                        att.created_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def list_repair_attempts(self, plan_id: str) -> List[Any]:
        """List all repair attempts for a plan in chronological order."""
        from forge.repair.models import RepairAttemptRecord
        from forge.repair.contracts import RepairOutcome
        from forge.sandbox.contracts import ReproductionOutcome

        conn = self._conn()
        try:
            cur = conn.execute(
                """
                SELECT * FROM repair_attempts WHERE plan_id = ? ORDER BY attempt_number ASC
                """,
                (plan_id,),
            )
            rows = cur.fetchall()
            return [
                RepairAttemptRecord(
                    attempt_id=r["attempt_id"],
                    plan_id=r["plan_id"],
                    attempt_number=r["attempt_number"],
                    finding_id=r["finding_id"],
                    run_id=r["run_id"],
                    base_commit_sha=r["base_commit_sha"],
                    patch_digest=r["patch_digest"],
                    outcome=RepairOutcome(r["outcome"]),
                    pre_repro_outcome=ReproductionOutcome(r["pre_repro_outcome"]),
                    post_repro_outcome=ReproductionOutcome(r["post_repro_outcome"]),
                    exit_code=r["exit_code"],
                    duration_ms=r["duration_ms"],
                    sandbox_available=bool(r["sandbox_available"]),
                    oracle_verified=bool(r["oracle_verified"]),
                    diagnostic_message=r["diagnostic_message"],
                    isolation_details=json.loads(r["isolation_details_json"] or "{}"),
                    stdout_excerpt=r["stdout_excerpt"],
                    stderr_excerpt=r["stderr_excerpt"],
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Repair Workflows API (Phase 4 M4)
    # ---------------------------------------------------------
    def save_repair_workflow(self, record: Any) -> None:
        """Persist or insert an initial repair workflow record."""
        from forge.repair.orchestrator_models import RepairWorkflowRecord

        rec = record if isinstance(record, RepairWorkflowRecord) else RepairWorkflowRecord.model_validate(record)
        rec_json = rec.model_dump_json()

        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO repair_workflows (
                        workflow_id, finding_id, run_id, target_repo, base_commit_sha,
                        policy_version, state, plan_id, patch_digest, dry_run,
                        sandbox_available, outcome, verification_attempt_id,
                        human_decision, human_notes, error_message, version,
                        workflow_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id) DO UPDATE SET
                        state = excluded.state,
                        plan_id = excluded.plan_id,
                        patch_digest = excluded.patch_digest,
                        dry_run = excluded.dry_run,
                        sandbox_available = excluded.sandbox_available,
                        outcome = excluded.outcome,
                        verification_attempt_id = excluded.verification_attempt_id,
                        human_decision = excluded.human_decision,
                        human_notes = excluded.human_notes,
                        error_message = excluded.error_message,
                        version = excluded.version,
                        workflow_json = excluded.workflow_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        rec.workflow_id,
                        rec.finding_id,
                        rec.run_id,
                        rec.target_repo,
                        rec.base_commit_sha,
                        rec.policy_version,
                        rec.state.value,
                        rec.plan_id,
                        rec.patch_digest,
                        1 if rec.dry_run else 0,
                        1 if rec.sandbox_available else 0,
                        rec.outcome.value if rec.outcome else None,
                        rec.verification_attempt_id,
                        rec.human_decision,
                        rec.human_notes,
                        rec.error_message,
                        rec.version,
                        rec_json,
                        rec.created_at.isoformat(),
                        rec.updated_at.isoformat(),
                    ),
                )

                # Record initial CREATED event if no event exists for this workflow
                cur_ev = conn.execute(
                    "SELECT 1 FROM repair_workflow_events WHERE workflow_id = ?",
                    (rec.workflow_id,),
                )
                if not cur_ev.fetchone():
                    eid = f"wfe_{uuid.uuid4().hex[:12]}"
                    initial_payload = {
                        "finding_id": rec.finding_id,
                        "target_repo": rec.target_repo,
                        "base_commit_sha": rec.base_commit_sha,
                        "dry_run": rec.dry_run,
                        "policy_version": rec.policy_version,
                    }
                    clean_payload = json.loads(redact_secrets(json.dumps(initial_payload)))
                    conn.execute(
                        """
                        INSERT INTO repair_workflow_events (
                            event_id, workflow_id, sequence, from_state, to_state, stage, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            eid,
                            rec.workflow_id,
                            1,
                            None,
                            rec.state.value,
                            "init",
                            json.dumps(clean_payload),
                            rec.created_at.isoformat(),
                        ),
                    )
        finally:
            conn.close()

    def get_repair_workflow(self, workflow_id: str) -> Optional[Any]:
        """Retrieve a repair workflow by ID."""
        from forge.repair.orchestrator_models import RepairWorkflowRecord

        conn = self._conn()
        try:
            cur = conn.execute("SELECT workflow_json FROM repair_workflows WHERE workflow_id = ?", (workflow_id,))
            row = cur.fetchone()
            if not row:
                return None
            return RepairWorkflowRecord.model_validate_json(row["workflow_json"])
        finally:
            conn.close()

    def list_repair_workflows(
        self,
        finding_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Any]:
        """List repair workflows, optionally filtered by finding_id and/or state."""
        from forge.repair.orchestrator_models import RepairWorkflowRecord

        conn = self._conn()
        try:
            query = "SELECT workflow_json FROM repair_workflows WHERE 1=1"
            params: List[Any] = []
            if finding_id:
                query += " AND finding_id = ?"
                params.append(finding_id)
            if state:
                query += " AND state = ?"
                params.append(state)
            query += " ORDER BY created_at DESC"

            cur = conn.execute(query, tuple(params))
            rows = cur.fetchall()
            return [RepairWorkflowRecord.model_validate_json(r["workflow_json"]) for r in rows]
        finally:
            conn.close()

    def transition_repair_workflow(
        self,
        workflow_id: str,
        expected_from_state: Optional[Any],
        target_state: Any,
        stage: str,
        payload: Optional[Dict[str, Any]] = None,
        plan_id: Optional[str] = None,
        patch_digest: Optional[str] = None,
        outcome: Optional[Any] = None,
        verification_attempt_id: Optional[str] = None,
        human_decision: Optional[str] = None,
        human_notes: Optional[str] = None,
        error_message: Optional[str] = None,
        sandbox_available: Optional[bool] = None,
    ) -> Any:
        """Atomically validate and execute a repair workflow state transition."""
        from forge.repair.orchestrator_models import (
            InvalidWorkflowTransitionError,
            RepairWorkflowEvent,
            RepairWorkflowRecord,
            RepairWorkflowState,
            WorkflowConcurrencyError,
            validate_workflow_transition,
        )
        from forge.repair.contracts import RepairOutcome

        target_enum = RepairWorkflowState(target_state) if isinstance(target_state, str) else target_state
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        conn = self._conn()
        try:
            with conn:
                cur = conn.execute("SELECT * FROM repair_workflows WHERE workflow_id = ?", (workflow_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Repair workflow '{workflow_id}' not found")

                current_state = RepairWorkflowState(row["state"])
                current_version = row["version"]

                # 1. State machine validation
                validate_workflow_transition(current_state, target_enum)

                # 2. Concurrency check if expected_from_state was given
                if expected_from_state is not None:
                    exp_enum = RepairWorkflowState(expected_from_state) if isinstance(expected_from_state, str) else expected_from_state
                    if current_state != exp_enum:
                        raise WorkflowConcurrencyError(
                            f"Workflow '{workflow_id}' concurrency conflict: expected state '{exp_enum.value}', "
                            f"but current state is '{current_state.value}'"
                        )

                # 3. Determine new values
                new_plan_id = plan_id if plan_id is not None else row["plan_id"]
                new_patch_digest = patch_digest if patch_digest is not None else row["patch_digest"]
                new_outcome = outcome.value if isinstance(outcome, RepairOutcome) else (outcome if outcome is not None else row["outcome"])
                new_att_id = verification_attempt_id if verification_attempt_id is not None else row["verification_attempt_id"]
                new_decision = human_decision if human_decision is not None else row["human_decision"]
                new_notes = human_notes if human_notes is not None else row["human_notes"]
                new_err = error_message if error_message is not None else row["error_message"]
                new_sandbox = (1 if sandbox_available else 0) if sandbox_available is not None else row["sandbox_available"]
                new_version = current_version + 1

                # 4. Record append-only event in sequence
                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM repair_workflow_events WHERE workflow_id = ?",
                    (workflow_id,),
                )
                seq = cur_seq.fetchone()[0]
                eid = f"wfe_{uuid.uuid4().hex[:12]}"
                clean_payload = json.loads(redact_secrets(json.dumps(payload or {})))

                conn.execute(
                    """
                    INSERT INTO repair_workflow_events (
                        event_id, workflow_id, sequence, from_state, to_state, stage, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        workflow_id,
                        seq,
                        current_state.value,
                        target_enum.value,
                        stage,
                        json.dumps(clean_payload),
                        now_str,
                    ),
                )

                # 5. Build updated record
                updated_rec = RepairWorkflowRecord(
                    workflow_id=workflow_id,
                    finding_id=row["finding_id"],
                    run_id=row["run_id"],
                    target_repo=row["target_repo"],
                    base_commit_sha=row["base_commit_sha"],
                    policy_version=row["policy_version"],
                    state=target_enum,
                    plan_id=new_plan_id,
                    patch_digest=new_patch_digest,
                    dry_run=bool(row["dry_run"]),
                    sandbox_available=bool(new_sandbox),
                    outcome=RepairOutcome(new_outcome) if new_outcome else None,
                    verification_attempt_id=new_att_id,
                    human_decision=new_decision,
                    human_notes=new_notes,
                    error_message=new_err,
                    version=new_version,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=now_dt,
                )

                # 6. Update database row
                conn.execute(
                    """
                    UPDATE repair_workflows SET
                        state = ?,
                        plan_id = ?,
                        patch_digest = ?,
                        sandbox_available = ?,
                        outcome = ?,
                        verification_attempt_id = ?,
                        human_decision = ?,
                        human_notes = ?,
                        error_message = ?,
                        version = ?,
                        workflow_json = ?,
                        updated_at = ?
                    WHERE workflow_id = ?
                    """,
                    (
                        target_enum.value,
                        new_plan_id,
                        new_patch_digest,
                        new_sandbox,
                        new_outcome,
                        new_att_id,
                        new_decision,
                        new_notes,
                        new_err,
                        new_version,
                        updated_rec.model_dump_json(),
                        now_str,
                        workflow_id,
                    ),
                )

                # 7. Mirror run event if run_id is linked
                if row["run_id"]:
                    self._append_event_locked(
                        conn,
                        row["run_id"],
                        EventType.REPAIR_WORKFLOW_STATE_TRANSITIONED,
                        {
                            "workflow_id": workflow_id,
                            "from_state": current_state.value,
                            "to_state": target_enum.value,
                            "stage": stage,
                        },
                    )

                return updated_rec
        finally:
            conn.close()

    def list_repair_workflow_events(self, workflow_id: str) -> List[Any]:
        """Fetch all events for a repair workflow in strict sequence order."""
        from forge.repair.orchestrator_models import RepairWorkflowEvent, RepairWorkflowState

        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM repair_workflow_events WHERE workflow_id = ? ORDER BY sequence ASC",
                (workflow_id,),
            )
            rows = cur.fetchall()
            return [
                RepairWorkflowEvent(
                    event_id=r["event_id"],
                    workflow_id=r["workflow_id"],
                    sequence=r["sequence"],
                    from_state=RepairWorkflowState(r["from_state"]) if r["from_state"] else None,
                    to_state=RepairWorkflowState(r["to_state"]),
                    stage=r["stage"],
                    payload=json.loads(r["payload_json"] or "{}"),
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Engineering Workflows API (Phase 5)
    # ---------------------------------------------------------
    def save_workflow_intake(self, spec: Any) -> None:
        """Persist or update a WorkflowIntakeSpec."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO workflow_intakes (
                        intake_id, source_type, target_repo, base_commit_sha,
                        title, description, affected_files_json, intake_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(intake_id) DO UPDATE SET
                        source_type=excluded.source_type,
                        target_repo=excluded.target_repo,
                        base_commit_sha=excluded.base_commit_sha,
                        title=excluded.title,
                        description=excluded.description,
                        affected_files_json=excluded.affected_files_json,
                        intake_json=excluded.intake_json
                    """,
                    (
                        spec.intake_id,
                        spec.source_type.value,
                        spec.target_repo,
                        spec.base_commit_sha,
                        spec.title,
                        spec.description,
                        json.dumps(spec.affected_files),
                        spec.model_dump_json(),
                        spec.created_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def get_workflow_intake(self, intake_id: str) -> Optional[Any]:
        """Fetch a WorkflowIntakeSpec by ID."""
        from forge.workflow.models import WorkflowIntakeSpec

        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT intake_json FROM workflow_intakes WHERE intake_id = ?",
                (intake_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return WorkflowIntakeSpec.model_validate_json(row["intake_json"])
        finally:
            conn.close()

    def list_workflow_intakes(self, target_repo: Optional[str] = None) -> List[Any]:
        """List persisted WorkflowIntakeSpecs, optionally filtered by target_repo."""
        from forge.workflow.models import WorkflowIntakeSpec

        conn = self._conn()
        try:
            query = "SELECT intake_json FROM workflow_intakes WHERE 1=1"
            params: List[Any] = []
            if target_repo:
                query += " AND target_repo = ?"
                params.append(target_repo)
            query += " ORDER BY created_at DESC"

            cur = conn.execute(query, tuple(params))
            rows = cur.fetchall()
            return [WorkflowIntakeSpec.model_validate_json(r["intake_json"]) for r in rows]
        finally:
            conn.close()

    def save_engineering_workflow(self, workflow: Any) -> None:
        """Persist a new or updated EngineeringWorkflow and record initial event if needed."""
        now_dt = datetime.now(timezone.utc)
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO engineering_workflows (
                        workflow_id, intake_id, target_repo, base_commit_sha, state,
                        context_id, repro_case_id, repair_plan_id, verification_attempt_id,
                        regression_summary_json, human_decision, human_notes, error_message,
                        version, workflow_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id) DO UPDATE SET
                        intake_id = excluded.intake_id,
                        target_repo = excluded.target_repo,
                        base_commit_sha = excluded.base_commit_sha,
                        state = excluded.state,
                        context_id = excluded.context_id,
                        repro_case_id = excluded.repro_case_id,
                        repair_plan_id = excluded.repair_plan_id,
                        verification_attempt_id = excluded.verification_attempt_id,
                        regression_summary_json = excluded.regression_summary_json,
                        human_decision = excluded.human_decision,
                        human_notes = excluded.human_notes,
                        error_message = excluded.error_message,
                        version = excluded.version,
                        workflow_json = excluded.workflow_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        workflow.workflow_id,
                        workflow.intake_id,
                        workflow.target_repo,
                        workflow.base_commit_sha,
                        workflow.state.value,
                        workflow.context_id,
                        workflow.repro_case_id,
                        workflow.repair_plan_id,
                        workflow.verification_attempt_id,
                        json.dumps(workflow.regression_summary),
                        workflow.human_decision,
                        workflow.human_notes,
                        workflow.error_message,
                        workflow.version,
                        workflow.model_dump_json(),
                        workflow.created_at.isoformat(),
                        workflow.updated_at.isoformat(),
                    ),
                )

                # Record initial event if not present
                cur_ev = conn.execute(
                    "SELECT 1 FROM engineering_workflow_events WHERE workflow_id = ?",
                    (workflow.workflow_id,),
                )
                if not cur_ev.fetchone():
                    eid = f"wfe_{uuid.uuid4().hex[:12]}"
                    initial_payload = {
                        "intake_id": workflow.intake_id,
                        "target_repo": workflow.target_repo,
                        "base_commit_sha": workflow.base_commit_sha,
                    }
                    clean_payload = json.loads(redact_secrets(json.dumps(initial_payload)))
                    conn.execute(
                        """
                        INSERT INTO engineering_workflow_events (
                            event_id, workflow_id, sequence, from_state, to_state, stage, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            eid,
                            workflow.workflow_id,
                            1,
                            None,
                            workflow.state.value,
                            "init",
                            json.dumps(clean_payload),
                            workflow.created_at.isoformat(),
                        ),
                    )
        finally:
            conn.close()

    def get_engineering_workflow(self, workflow_id: str) -> Optional[Any]:
        """Fetch an EngineeringWorkflow by ID."""
        from forge.workflow.models import EngineeringWorkflow

        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT workflow_json FROM engineering_workflows WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return EngineeringWorkflow.model_validate_json(row["workflow_json"])
        finally:
            conn.close()

    def list_engineering_workflows(
        self,
        intake_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Any]:
        """List EngineeringWorkflows, optionally filtered by intake_id and/or state."""
        from forge.workflow.models import EngineeringWorkflow

        conn = self._conn()
        try:
            query = "SELECT workflow_json FROM engineering_workflows WHERE 1=1"
            params: List[Any] = []
            if intake_id:
                query += " AND intake_id = ?"
                params.append(intake_id)
            if state:
                query += " AND state = ?"
                params.append(state)
            query += " ORDER BY created_at DESC"

            cur = conn.execute(query, tuple(params))
            rows = cur.fetchall()
            return [EngineeringWorkflow.model_validate_json(r["workflow_json"]) for r in rows]
        finally:
            conn.close()

    def transition_engineering_workflow(
        self,
        workflow_id: str,
        expected_version: Optional[int],
        target_state: Any,
        stage: str,
        payload: Optional[Dict[str, Any]] = None,
        context_id: Optional[str] = None,
        repro_case_id: Optional[str] = None,
        repair_plan_id: Optional[str] = None,
        verification_attempt_id: Optional[str] = None,
        regression_summary: Optional[Dict[str, Any]] = None,
        human_decision: Optional[str] = None,
        human_notes: Optional[str] = None,
        error_message: Optional[str] = None,
        transition_context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Atomically validate and execute an engineering workflow state transition with optimistic concurrency."""
        from forge.workflow.models import (
            EngineeringWorkflow,
            EngineeringWorkflowState,
            WorkflowConcurrencyError,
        )
        from forge.workflow.state import validate_workflow_transition

        target_enum = (
            target_state
            if isinstance(target_state, EngineeringWorkflowState)
            else EngineeringWorkflowState(str(target_state))
        )

        conn = self._conn()
        try:
            with conn:
                # 1. Fetch current row
                cur = conn.execute(
                    "SELECT * FROM engineering_workflows WHERE workflow_id = ?",
                    (workflow_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

                current_version = row["version"]
                current_state = EngineeringWorkflowState(row["state"])

                # 2. Check optimistic concurrency version
                if expected_version is not None and current_version != expected_version:
                    raise WorkflowConcurrencyError(
                        f"Concurrency conflict on workflow '{workflow_id}': "
                        f"expected version {expected_version}, but found {current_version}."
                    )

                # 3. Validate transition guards
                ctx = dict(transition_context or {})
                if human_decision is not None:
                    ctx["human_decision"] = human_decision
                validate_workflow_transition(current_state, target_enum, context=ctx)

                # 4. Determine next event sequence number and record event
                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM engineering_workflow_events WHERE workflow_id = ?",
                    (workflow_id,),
                )
                seq = cur_seq.fetchone()[0]
                eid = f"wfe_{uuid.uuid4().hex[:12]}"
                clean_payload = json.loads(redact_secrets(json.dumps(payload or {})))
                now_dt = datetime.now(timezone.utc)
                now_str = now_dt.isoformat()

                conn.execute(
                    """
                    INSERT INTO engineering_workflow_events (
                        event_id, workflow_id, sequence, from_state, to_state, stage, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        workflow_id,
                        seq,
                        current_state.value,
                        target_enum.value,
                        stage,
                        json.dumps(clean_payload),
                        now_str,
                    ),
                )

                # 5. Build updated record
                new_version = current_version + 1
                new_context_id = context_id if context_id is not None else row["context_id"]
                new_repro_case_id = repro_case_id if repro_case_id is not None else row["repro_case_id"]
                new_repair_plan_id = repair_plan_id if repair_plan_id is not None else row["repair_plan_id"]
                new_verification_attempt_id = (
                    verification_attempt_id if verification_attempt_id is not None else row["verification_attempt_id"]
                )
                new_regression_summary = (
                    regression_summary
                    if regression_summary is not None
                    else json.loads(row["regression_summary_json"] or "{}")
                )
                new_human_decision = human_decision if human_decision is not None else row["human_decision"]
                new_human_notes = human_notes if human_notes is not None else row["human_notes"]
                new_error_message = error_message if error_message is not None else row["error_message"]

                updated_workflow = EngineeringWorkflow(
                    workflow_id=workflow_id,
                    intake_id=row["intake_id"],
                    target_repo=row["target_repo"],
                    base_commit_sha=row["base_commit_sha"],
                    state=target_enum,
                    context_id=new_context_id,
                    repro_case_id=new_repro_case_id,
                    repair_plan_id=new_repair_plan_id,
                    verification_attempt_id=new_verification_attempt_id,
                    regression_summary=new_regression_summary,
                    human_decision=new_human_decision,
                    human_notes=new_human_notes,
                    error_message=new_error_message,
                    version=new_version,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=now_dt,
                )

                # 6. Update database row with optimistic concurrency check
                cur_upd = conn.execute(
                    """
                    UPDATE engineering_workflows SET
                        state = ?,
                        context_id = ?,
                        repro_case_id = ?,
                        repair_plan_id = ?,
                        verification_attempt_id = ?,
                        regression_summary_json = ?,
                        human_decision = ?,
                        human_notes = ?,
                        error_message = ?,
                        version = ?,
                        workflow_json = ?,
                        updated_at = ?
                    WHERE workflow_id = ? AND version = ?
                    """,
                    (
                        target_enum.value,
                        new_context_id,
                        new_repro_case_id,
                        new_repair_plan_id,
                        new_verification_attempt_id,
                        json.dumps(new_regression_summary),
                        new_human_decision,
                        new_human_notes,
                        new_error_message,
                        new_version,
                        updated_workflow.model_dump_json(),
                        now_str,
                        workflow_id,
                        current_version,
                    ),
                )

                if cur_upd.rowcount == 0:
                    raise WorkflowConcurrencyError(
                        f"Concurrency conflict on workflow '{workflow_id}': "
                        f"expected version {current_version} update failed."
                    )

                return updated_workflow
        finally:
            conn.close()

    def attach_repair_plan_to_workflow(
        self,
        workflow_id: str,
        expected_version: int,
        repair_plan_id: str,
        stage: str = "repair_plan_attached",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Atomically record a generated repair plan ID on a workflow with optimistic concurrency."""
        from forge.workflow.models import (
            EngineeringWorkflow,
            EngineeringWorkflowState,
            WorkflowConcurrencyError,
        )

        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "SELECT * FROM engineering_workflows WHERE workflow_id = ?",
                    (workflow_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise StoreError(f"Engineering workflow '{workflow_id}' not found.")

                current_version = row["version"]
                if expected_version is not None and current_version != expected_version:
                    raise WorkflowConcurrencyError(
                        f"Concurrency conflict on workflow '{workflow_id}': "
                        f"expected version {expected_version}, but found {current_version}."
                    )

                current_state = EngineeringWorkflowState(row["state"])
                new_version = current_version + 1
                now_dt = datetime.now(timezone.utc)
                now_str = now_dt.isoformat()

                # Event recording
                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM engineering_workflow_events WHERE workflow_id = ?",
                    (workflow_id,),
                )
                seq = cur_seq.fetchone()[0]
                eid = f"wfe_{uuid.uuid4().hex[:12]}"
                clean_payload = json.loads(redact_secrets(json.dumps(payload or {})))

                conn.execute(
                    """
                    INSERT INTO engineering_workflow_events (
                        event_id, workflow_id, sequence, from_state, to_state, stage, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        workflow_id,
                        seq,
                        current_state.value,
                        current_state.value,
                        stage,
                        json.dumps(clean_payload),
                        now_str,
                    ),
                )

                updated_workflow = EngineeringWorkflow(
                    workflow_id=workflow_id,
                    intake_id=row["intake_id"],
                    target_repo=row["target_repo"],
                    base_commit_sha=row["base_commit_sha"],
                    state=current_state,
                    context_id=row["context_id"],
                    repro_case_id=row["repro_case_id"],
                    repair_plan_id=repair_plan_id,
                    verification_attempt_id=row["verification_attempt_id"],
                    regression_summary=json.loads(row["regression_summary_json"] or "{}"),
                    human_decision=row["human_decision"],
                    human_notes=row["human_notes"],
                    error_message=row["error_message"],
                    version=new_version,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=now_dt,
                )

                cur_upd = conn.execute(
                    """
                    UPDATE engineering_workflows SET
                        repair_plan_id = ?,
                        version = ?,
                        workflow_json = ?,
                        updated_at = ?
                    WHERE workflow_id = ? AND version = ?
                    """,
                    (
                        repair_plan_id,
                        new_version,
                        updated_workflow.model_dump_json(),
                        now_str,
                        workflow_id,
                        current_version,
                    ),
                )

                if cur_upd.rowcount == 0:
                    raise WorkflowConcurrencyError(
                        f"Concurrency conflict on workflow '{workflow_id}': "
                        f"expected version {current_version} update failed."
                    )

                return updated_workflow
        finally:
            conn.close()

    def list_engineering_workflow_events(self, workflow_id: str) -> List[Any]:
        """Fetch all events for an engineering workflow in strict sequence order."""
        from forge.workflow.models import EngineeringWorkflowEvent, EngineeringWorkflowState

        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM engineering_workflow_events WHERE workflow_id = ? ORDER BY sequence ASC",
                (workflow_id,),
            )
            rows = cur.fetchall()
            return [
                EngineeringWorkflowEvent(
                    event_id=r["event_id"],
                    workflow_id=r["workflow_id"],
                    sequence=r["sequence"],
                    from_state=EngineeringWorkflowState(r["from_state"]) if r["from_state"] else None,
                    to_state=EngineeringWorkflowState(r["to_state"]),
                    stage=r["stage"],
                    payload=json.loads(r["payload_json"] or "{}"),
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    def append_engineering_workflow_event(
        self,
        workflow_id: str,
        to_state: Any,
        from_state: Optional[Any] = None,
        stage: str = "custom",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Append an event to the engineering workflow ledger."""
        from forge.workflow.models import EngineeringWorkflowEvent, EngineeringWorkflowState

        conn = self._conn()
        try:
            with conn:
                cur_seq = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM engineering_workflow_events WHERE workflow_id = ?",
                    (workflow_id,),
                )
                seq = cur_seq.fetchone()[0]
                eid = f"wfe_{uuid.uuid4().hex[:12]}"
                clean_payload = json.loads(redact_secrets(json.dumps(payload or {})))
                to_val = to_state.value if hasattr(to_state, "value") else str(to_state)
                from_val = (
                    from_state.value
                    if (from_state and hasattr(from_state, "value"))
                    else (str(from_state) if from_state else None)
                )
                now_dt = datetime.now(timezone.utc)
                now_str = now_dt.isoformat()

                conn.execute(
                    """
                    INSERT INTO engineering_workflow_events (
                        event_id, workflow_id, sequence, from_state, to_state, stage, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        workflow_id,
                        seq,
                        from_val,
                        to_val,
                        stage,
                        json.dumps(clean_payload),
                        now_str,
                    ),
                )
                return EngineeringWorkflowEvent(
                    event_id=eid,
                    workflow_id=workflow_id,
                    sequence=seq,
                    from_state=EngineeringWorkflowState(from_val) if from_val else None,
                    to_state=EngineeringWorkflowState(to_val),
                    stage=stage,
                    payload=clean_payload,
                    created_at=now_dt,
                )
        finally:
            conn.close()


    def save_multi_check_verification(self, result: Any) -> None:
        """Persist a MultiCheckVerificationResult to SQLite."""
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO multi_check_verifications (
                        attempt_id, workflow_id, repair_plan_id, base_commit_sha,
                        patch_digest, aggregate_outcome, regression_detected,
                        sandbox_available, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.attempt_id,
                        result.workflow_id,
                        result.repair_plan_id,
                        result.base_commit_sha,
                        result.patch_digest,
                        result.aggregate_outcome.value if hasattr(result.aggregate_outcome, "value") else str(result.aggregate_outcome),
                        1 if result.regression_detected else 0,
                        1 if result.sandbox_available else 0,
                        result.model_dump_json(),
                        result.created_at.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def get_multi_check_verification(self, attempt_id: str) -> Optional[Any]:
        """Retrieve a MultiCheckVerificationResult by attempt_id."""
        from forge.workflow.models import MultiCheckVerificationResult

        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT result_json FROM multi_check_verifications WHERE attempt_id = ?",
                (attempt_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return MultiCheckVerificationResult.model_validate_json(row["result_json"])
        finally:
            conn.close()

    def list_multi_check_verifications(self, workflow_id: Optional[str] = None) -> List[Any]:
        """List MultiCheckVerificationResults, optionally filtered by workflow_id."""
        from forge.workflow.models import MultiCheckVerificationResult

        conn = self._conn()
        try:
            if workflow_id:
                cur = conn.execute(
                    "SELECT result_json FROM multi_check_verifications WHERE workflow_id = ? ORDER BY created_at ASC",
                    (workflow_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT result_json FROM multi_check_verifications ORDER BY created_at ASC"
                )
            rows = cur.fetchall()
            return [MultiCheckVerificationResult.model_validate_json(r["result_json"]) for r in rows]
        finally:
            conn.close()
