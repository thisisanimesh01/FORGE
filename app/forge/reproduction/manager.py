"""Case manager and execution coordinator for reproduction cases.

Part of Phase 3, Milestone 2: Reproduction Case Management & Evidence Capture.

Provides:
- Transactional creation, retrieval, and listing of reproduction cases and attempts.
- Evidence synthesis adhering to strict redaction, bounding, and provenance rules.
- Deterministic outcome classification: REPRODUCED, NOT_REPRODUCED, INCONCLUSIVE, BLOCKED, TIMED_OUT, ERROR.
- Resumption with strict revision validation (rejecting stale or mismatched revisions).
- Safe bounded retries honoring idempotency policy.
- Complete fail-closed sandbox behavior (never executes code on the host).
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from forge.analysis.evidence import create_evidence
from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.config import redact_secrets
from forge.reproduction.models import (
    CaseStatus,
    FieldProvenance,
    ReproductionAttempt,
    ReproductionCase,
    RetryPolicy,
    generate_case_id,
)
from forge.sandbox.contracts import (
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.runner import ReproductionHarness
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    EventType,
    FindingLifecycleState,
)
from forge.storage.database import get_db_connection
from forge.storage.store import ForgeStore, StoreError


class CaseManagementError(Exception):
    """Raised when case creation, resumption, or attempt execution violates policies."""
    pass


class StaleRevisionError(CaseManagementError):
    """Raised when target repository revision does not match the pinned case revision."""
    pass


class NonIdempotentRetryError(CaseManagementError):
    """Raised when attempting to retry a non-idempotent command."""
    pass


class ReproductionCaseManager:
    """Coordinates lifecycle, persistence, execution, and evidence for reproduction cases."""

    def __init__(
        self,
        store: Optional[ForgeStore] = None,
        db_path: Optional[Path] = None,
        harness: Optional[ReproductionHarness] = None,
    ):
        self.store = store or ForgeStore(db_path=db_path)
        self.harness = harness or ReproductionHarness()

    def _conn(self):
        return get_db_connection(self.store.db_path)

    # ---------------------------------------------------------
    # Case Persistence & Retrieval
    # ---------------------------------------------------------

    def create_case(
        self,
        finding_id: str,
        target_repo: str,
        target_commit_sha: str,
        contract: ReproductionRequest,
        run_id: Optional[str] = None,
        max_retries: int = 0,
        is_idempotent: bool = True,
        provenance_map: Optional[Dict[str, FieldProvenance]] = None,
    ) -> ReproductionCase:
        """Deterministically create and persist a reproduction case."""
        # Validate finding exists in store
        finding = self.store.get_finding(finding_id)
        if not finding:
            raise StoreError(f"Finding '{finding_id}' does not exist in store")

        rid = run_id or finding.run_id or self.store.create_run(target=target_repo)
        case_id = generate_case_id(finding_id, target_repo, target_commit_sha)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        retry_policy = RetryPolicy(
            max_retries=max(0, min(3, max_retries)),
            is_idempotent=is_idempotent,
        )

        p_map = provenance_map or {
            "finding_id": FieldProvenance.STATIC_ANALYSIS,
            "target_repo": FieldProvenance.USER_INPUT,
            "target_commit_sha": FieldProvenance.USER_INPUT,
            "command": FieldProvenance.UNTRUSTED_REPOSITORY,
            "environment": FieldProvenance.SYSTEM_POLICY,
        }

        case = ReproductionCase(
            case_id=case_id,
            finding_id=finding_id,
            run_id=rid,
            target_repo=target_repo,
            target_commit_sha=target_commit_sha,
            contract=contract,
            policy_version="1.0.0",
            status=CaseStatus.PENDING,
            attempts_count=0,
            max_retries=retry_policy.max_retries,
            retry_policy=retry_policy,
            provenance_map=p_map,
            created_at=now,
            updated_at=now,
        )

        conn = self._conn()
        try:
            with conn:
                # Check for existing case
                cur = conn.execute("SELECT case_id FROM reproduction_cases WHERE case_id = ?", (case_id,))
                if cur.fetchone():
                    raise CaseManagementError(f"Reproduction case '{case_id}' already exists")

                conn.execute(
                    """
                    INSERT INTO reproduction_cases (
                        case_id, finding_id, run_id, target_repo, target_commit_sha,
                        contract_json, policy_version, status, attempts_count, max_retries,
                        retry_policy_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case.case_id,
                        case.finding_id,
                        case.run_id,
                        case.target_repo,
                        case.target_commit_sha,
                        json.dumps(case.contract.model_dump(mode="json")),
                        case.policy_version,
                        case.status.value,
                        case.attempts_count,
                        case.max_retries,
                        json.dumps(case.retry_policy.model_dump(mode="json")),
                        now_iso,
                        now_iso,
                    ),
                )

                self.store._append_event_locked(
                    conn,
                    rid,
                    EventType.REPRODUCTION_CASE_CREATED,
                    {
                        "case_id": case.case_id,
                        "finding_id": case.finding_id,
                        "target_repo": case.target_repo,
                        "target_commit_sha": case.target_commit_sha,
                    },
                )
            return case
        finally:
            conn.close()

    def get_case(self, case_id: str) -> Optional[ReproductionCase]:
        """Fetch a reproduction case by ID."""
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM reproduction_cases WHERE case_id = ?", (case_id,))
            row = cur.fetchone()
            if not row:
                return None

            contract_dict = json.loads(row["contract_json"])
            contract = ReproductionRequest.model_validate(contract_dict)

            retry_policy = RetryPolicy()
            if "retry_policy_json" in row.keys() and row["retry_policy_json"]:
                try:
                    retry_policy = RetryPolicy.model_validate(json.loads(row["retry_policy_json"]))
                except Exception:
                    pass

            return ReproductionCase(
                case_id=row["case_id"],
                finding_id=row["finding_id"],
                run_id=row["run_id"],
                target_repo=row["target_repo"],
                target_commit_sha=row["target_commit_sha"],
                contract=contract,
                policy_version=row["policy_version"],
                status=CaseStatus(row["status"]),
                attempts_count=row["attempts_count"],
                max_retries=row["max_retries"],
                retry_policy=retry_policy,
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        finally:
            conn.close()

    def list_cases(self, finding_id: Optional[str] = None) -> List[ReproductionCase]:
        """List reproduction cases, optionally filtered by finding_id."""
        conn = self._conn()
        try:
            if finding_id:
                cur = conn.execute(
                    "SELECT case_id FROM reproduction_cases WHERE finding_id = ? ORDER BY created_at ASC",
                    (finding_id,),
                )
            else:
                cur = conn.execute("SELECT case_id FROM reproduction_cases ORDER BY created_at ASC")
            return [self.get_case(r["case_id"]) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_attempts(self, case_id: str) -> List[ReproductionAttempt]:
        """List all attempts for a given case in chronological order."""
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM reproduction_attempts WHERE case_id = ? ORDER BY attempt_number ASC",
                (case_id,),
            )
            attempts = []
            for r in cur.fetchall():
                attempts.append(
                    ReproductionAttempt(
                        attempt_id=r["attempt_id"],
                        case_id=r["case_id"],
                        attempt_number=r["attempt_number"],
                        run_id=r["run_id"],
                        outcome=ReproductionOutcome(r["outcome"]),
                        exit_code=r["exit_code"],
                        duration_ms=r["duration_ms"],
                        stdout_digest=r["stdout_digest"],
                        stderr_digest=r["stderr_digest"],
                        stdout_excerpt=r["stdout_excerpt"],
                        stderr_excerpt=r["stderr_excerpt"],
                        stdout_truncated=bool(r["stdout_truncated"]),
                        stderr_truncated=bool(r["stderr_truncated"]),
                        matched_patterns=json.loads(r["matched_patterns_json"]),
                        evidence_id=r["evidence_id"],
                        isolation_details=json.loads(r["isolation_details_json"]),
                        provenance=json.loads(r["provenance_json"]),
                        diagnostic_message=r["diagnostic_message"],
                        cleanup_status=r["cleanup_status"],
                        started_at=datetime.fromisoformat(r["started_at"]),
                        completed_at=datetime.fromisoformat(r["completed_at"]),
                    )
                )
            return attempts
        finally:
            conn.close()

    # ---------------------------------------------------------
    # Attempt Execution & Evidence Lifecycle
    # ---------------------------------------------------------

    def run_attempt(
        self,
        case_id: str,
        workspace_path: Path,
        current_commit_sha: Optional[str] = None,
        is_resume: bool = False,
    ) -> ReproductionAttempt:
        """Execute a reproduction attempt, capture evidence, and update case status."""
        case = self.get_case(case_id)
        if not case:
            raise StoreError(f"Reproduction case '{case_id}' not found")

        # Revalidate commit revision (prevent stale revisions)
        if current_commit_sha:
            clean_expected = case.target_commit_sha.strip().lower()
            clean_observed = current_commit_sha.strip().lower()
            if not clean_observed.startswith(clean_expected) and not clean_expected.startswith(clean_observed):
                raise StaleRevisionError(
                    f"Repository revision mismatch for case '{case_id}': "
                    f"Case pinned to '{case.target_commit_sha}', but workspace revision is '{current_commit_sha}'"
                )

        # Check retry eligibility
        attempts = self.list_attempts(case_id)
        attempt_number = len(attempts) + 1

        if attempt_number > 1:
            if not case.retry_policy.is_idempotent:
                raise NonIdempotentRetryError(
                    f"Case '{case_id}' command is not marked idempotent; automated retry refused."
                )
            if attempt_number > (case.max_retries + 1):
                raise CaseManagementError(
                    f"Case '{case_id}' exceeded max attempts ({case.max_retries + 1}). Further retries refused."
                )

        started_at = datetime.now(timezone.utc)
        attempt_id = f"att_{uuid.uuid4().hex[:12]}"

        # Record start event
        conn = self._conn()
        try:
            with conn:
                self.store._append_event_locked(
                    conn,
                    case.run_id,
                    EventType.REPRODUCTION_ATTEMPT_STARTED if not is_resume else EventType.REPRODUCTION_CASE_RESUMED,
                    {
                        "case_id": case.case_id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                    },
                )
        finally:
            conn.close()

        # Execute using ReproductionHarness inside isolated sandbox (or fail closed)
        cleanup_status = "cleaned"
        try:
            res: ReproductionResult = self.harness.execute(case.contract, workspace_path)
        except Exception as e:
            cleanup_status = "error_in_cleanup"
            res = ReproductionResult(
                request_id=case.contract.request_id,
                finding_id=case.finding_id,
                outcome=ReproductionOutcome.ERROR,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=0.0,
                matched_patterns=[],
                sandbox_available=False,
                isolation_details={},
                diagnostic_message=f"Harness execution error: {e}",
                completed_at=datetime.now(timezone.utc),
            )

        completed_at = datetime.now(timezone.utc)

        # Compute digests of raw output before excerpting
        stdout_bytes = res.stdout.encode("utf-8")
        stderr_bytes = res.stderr.encode("utf-8")
        stdout_digest = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_digest = hashlib.sha256(stderr_bytes).hexdigest()

        # Bounded excerpting (max 5000 chars each for attempt record)
        max_excerpt = 5000
        stdout_truncated = len(res.stdout) > max_excerpt
        stderr_truncated = len(res.stderr) > max_excerpt
        stdout_clean = redact_secrets(res.stdout[:max_excerpt])
        stderr_clean = redact_secrets(res.stderr[:max_excerpt])

        # Map to CaseStatus
        if res.outcome == ReproductionOutcome.REPRODUCED:
            new_case_status = CaseStatus.REPRODUCED
        elif res.outcome == ReproductionOutcome.NOT_REPRODUCED:
            new_case_status = CaseStatus.NOT_REPRODUCED
        elif res.outcome == ReproductionOutcome.INCONCLUSIVE:
            new_case_status = CaseStatus.INCONCLUSIVE
        elif res.outcome in [ReproductionOutcome.BLOCKED, ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE]:
            new_case_status = CaseStatus.BLOCKED
        elif res.outcome == ReproductionOutcome.PASSED:
            # Clean test passed -> defect did not reproduce
            new_case_status = CaseStatus.NOT_REPRODUCED
        else:
            new_case_status = CaseStatus.FAILED

        # Create structured evidence if reproduced or verified
        evidence_id = None
        evidence_content = (
            f"Reproduction Command: {' '.join(case.contract.command.args)}\n"
            f"Observed Exit Code: {res.exit_code}\n"
            f"Matched Patterns: {res.matched_patterns}\n"
            f"Diagnostic: {res.diagnostic_message}\n\n"
            f"--- STDOUT ---\n{stdout_clean}\n\n"
            f"--- STDERR ---\n{stderr_clean}"
        )

        provenance_dict = {
            "command": FieldProvenance.UNTRUSTED_REPOSITORY.value,
            "execution": FieldProvenance.SANDBOX_EXECUTION.value if res.sandbox_available else FieldProvenance.SYSTEM_POLICY.value,
            "hypothesis": FieldProvenance.STATIC_ANALYSIS.value,
        }

        # Transactionally record attempt, evidence, update case, and advance lifecycle if verified
        conn = self._conn()
        try:
            with conn:
                # 1. Persist EvidenceRecord in SQLite
                evidence = create_evidence(
                    run_id=case.run_id,
                    evidence_type=EvidenceType.REPRODUCTION_RESULT,
                    description=f"Reproduction attempt #{attempt_number} for finding '{case.finding_id}' ({res.outcome.value})",
                    content=evidence_content,
                    finding_id=case.finding_id,
                    details={
                        "case_id": case.case_id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "outcome": res.outcome.value,
                        "status": "reproduced" if res.outcome == ReproductionOutcome.REPRODUCED else res.outcome.value,
                        "exit_code": res.exit_code,
                        "matched_patterns": res.matched_patterns,
                        "stdout_digest": stdout_digest,
                        "stderr_digest": stderr_digest,
                    },
                    provenance=f"forge:sandbox:{case.case_id}",
                )
                evidence_id = evidence.evidence_id

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

                # 2. Persist ReproductionAttempt
                conn.execute(
                    """
                    INSERT INTO reproduction_attempts (
                        attempt_id, case_id, attempt_number, run_id, outcome, exit_code,
                        duration_ms, stdout_digest, stderr_digest, stdout_excerpt, stderr_excerpt,
                        stdout_truncated, stderr_truncated, matched_patterns_json, evidence_id,
                        isolation_details_json, provenance_json, diagnostic_message,
                        cleanup_status, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        case.case_id,
                        attempt_number,
                        case.run_id,
                        res.outcome.value,
                        res.exit_code,
                        res.duration_ms,
                        stdout_digest,
                        stderr_digest,
                        stdout_clean,
                        stderr_clean,
                        1 if stdout_truncated else 0,
                        1 if stderr_truncated else 0,
                        json.dumps(res.matched_patterns),
                        evidence_id,
                        json.dumps(res.isolation_details),
                        json.dumps(provenance_dict),
                        res.diagnostic_message,
                        cleanup_status,
                        started_at.isoformat(),
                        completed_at.isoformat(),
                    ),
                )

                # 3. Update ReproductionCase aggregate state
                conn.execute(
                    """
                    UPDATE reproduction_cases
                    SET status = ?, attempts_count = ?, updated_at = ?
                    WHERE case_id = ?
                    """,
                    (new_case_status.value, attempt_number, completed_at.isoformat(), case.case_id),
                )

                # 4. Append audit event
                event_type = (
                    EventType.REPRODUCTION_ATTEMPT_COMPLETED
                    if res.outcome == ReproductionOutcome.REPRODUCED
                    else EventType.REPRODUCTION_ATTEMPT_FAILED
                )
                self.store._append_event_locked(
                    conn,
                    case.run_id,
                    event_type,
                    {
                        "case_id": case.case_id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "outcome": res.outcome.value,
                        "exit_code": res.exit_code,
                        "evidence_id": evidence_id,
                    },
                )

                # 5. Lifecycle Transition Enforcement:
                # If defect was successfully reproduced in sandbox and evidence is verified,
                # transition finding to REPRODUCED state atomically.
                finding = self.store.get_finding(case.finding_id)
                if finding and res.outcome == ReproductionOutcome.REPRODUCED:
                    if finding.lifecycle_state == FindingLifecycleState.SUSPECTED:
                        try:
                            validate_transition(finding, FindingLifecycleState.REPRODUCED, evidence)
                            from_state = finding.lifecycle_state.value
                            conn.execute(
                                "UPDATE findings SET lifecycle_state = ?, updated_at = ? WHERE finding_id = ?",
                                (FindingLifecycleState.REPRODUCED.value, completed_at.isoformat(), case.finding_id),
                            )
                            conn.execute(
                                """
                                INSERT INTO finding_transitions (
                                    finding_id, run_id, from_state, to_state, evidence_id, reason, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    case.finding_id,
                                    case.run_id,
                                    from_state,
                                    FindingLifecycleState.REPRODUCED.value,
                                    evidence_id,
                                    f"Automated reproduction attempt #{attempt_number} confirmed defect",
                                    completed_at.isoformat(),
                                ),
                            )
                            self.store._append_event_locked(
                                conn,
                                case.run_id,
                                EventType.FINDING_STATE_TRANSITIONED,
                                {
                                    "finding_id": case.finding_id,
                                    "from_state": from_state,
                                    "to_state": FindingLifecycleState.REPRODUCED.value,
                                    "evidence_id": evidence_id,
                                    "case_id": case.case_id,
                                },
                            )
                        except InvalidTransitionError:
                            pass  # Keep finding in suspected if evidence validation fails closed
        finally:
            conn.close()

        return ReproductionAttempt(
            attempt_id=attempt_id,
            case_id=case.case_id,
            attempt_number=attempt_number,
            run_id=case.run_id,
            outcome=res.outcome,
            exit_code=res.exit_code,
            duration_ms=res.duration_ms,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            stdout_excerpt=stdout_clean,
            stderr_excerpt=stderr_clean,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            matched_patterns=res.matched_patterns,
            evidence_id=evidence_id,
            isolation_details=res.isolation_details,
            provenance={k: FieldProvenance(v) for k, v in provenance_dict.items()},
            diagnostic_message=res.diagnostic_message,
            cleanup_status=cleanup_status,
            started_at=started_at,
            completed_at=completed_at,
        )
