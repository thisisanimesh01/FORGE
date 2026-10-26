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
