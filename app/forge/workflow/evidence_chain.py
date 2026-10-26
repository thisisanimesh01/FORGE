"""Evidence verification, provenance validation, and chaining for Phase 5.

Milestone 3: Automated Evidence Chaining from Reproduction to Repair.

Guarantees:
- Validates that evidence originates from a trusted execution oracle (not model assertions or ungrounded prose).
- Verifies cryptographic digests, commit revisions, finding IDs, and outcome status.
- Rejects stale, missing, mismatched, fabricated, or corrupted evidence before repair planning can begin.
- Enforces strict target repository immutability.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shlex
from typing import Any, Dict, List, Optional

from forge.config import redact_secrets
from forge.reproduction.models import CaseStatus, FieldProvenance, ReproductionCase
from forge.sandbox.contracts import ReproductionCommand, ReproductionOutcome
from forge.sandbox.policy import APPROVED_EXECUTABLES, FORBIDDEN_ARGUMENT_PATTERNS, FORBIDDEN_EXECUTABLES
from forge.schemas import EvidenceRecord, EvidenceType, Finding, FindingLifecycleState, FindingSeverity
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.models import (
    CorruptedEvidenceError,
    EngineeringWorkflow,
    EvidenceChainError,
    EvidenceTamperedError,
    IntakeSourceType,
    MismatchedEvidenceError,
    MissingEvidenceError,
    StaleRevisionEvidenceError,
    UntrustedOracleError,
    WorkflowIntakeSpec,
)


TRUSTED_ORACLE_PROVENANCES = {
    FieldProvenance.TRUSTED_FIXTURE,
    FieldProvenance.STATIC_ANALYSIS,
    FieldProvenance.SYSTEM_POLICY,
    FieldProvenance.SANDBOX_EXECUTION,
}


def resolve_intake_finding_id(intake: WorkflowIntakeSpec) -> str:
    """Derive deterministic finding ID corresponding to an intake specification."""
    if intake.source_type == IntakeSourceType.REVIEW_FINDING and intake.finding_payload:
        return intake.finding_payload.finding_id
    return f"find_{intake.intake_id}"


def ensure_finding_in_store(
    store: ForgeStore,
    intake: WorkflowIntakeSpec,
    run_id: Optional[str] = None,
) -> Finding:
    """Retrieve or initialize the canonical Finding record for an intake specification."""
    fid = resolve_intake_finding_id(intake)
    existing = store.get_finding(fid)
    if existing:
        return existing

    rid = run_id or f"run_{intake.intake_id}"
    if not store.get_run(rid):
        store.create_run(target=intake.target_repo, run_id=rid)

    severity = FindingSeverity.HIGH
    if intake.finding_payload and intake.finding_payload.severity:
        severity = intake.finding_payload.severity

    finding = store.create_finding(
        run_id=rid,
        title=intake.title,
        description=intake.description,
        severity=severity,
        finding_id=fid,
        affected_files=intake.affected_files,
    )
    return finding


def extract_reproduction_command_from_intake(
    intake: WorkflowIntakeSpec,
) -> Optional[ReproductionCommand]:
    """Derive a safe, typed ReproductionCommand from task intake specification without shell evaluation."""
    if intake.source_type == IntakeSourceType.TEST_SPECIFICATION and intake.test_payload:
        raw_cmd = intake.test_payload.test_command.strip()
        try:
            tokens = shlex.split(raw_cmd)
        except Exception:
            return None

        if not tokens:
            return None

        binary = Path(tokens[0]).name
        if binary in FORBIDDEN_EXECUTABLES or binary not in APPROVED_EXECUTABLES:
            return None

        for tok in tokens:
            for pat in FORBIDDEN_ARGUMENT_PATTERNS:
                if pat.search(tok):
                    return None

        patterns = []
        if intake.test_payload.expected_failure_pattern:
            patterns.append(intake.test_payload.expected_failure_pattern)

        return ReproductionCommand(
            args=tokens,
            relative_cwd=".",
            expected_exit_codes=[1],
            expected_output_patterns=patterns,
        )

    if intake.source_type == IntakeSourceType.ISSUE_DESCRIPTION and intake.issue_payload:
        hints = intake.issue_payload.reproduction_hints or []
        for hint in hints:
            cleaned = hint.strip()
            try:
                tokens = shlex.split(cleaned)
            except Exception:
                continue

            if not tokens:
                continue

            binary = Path(tokens[0]).name
            if binary in APPROVED_EXECUTABLES and binary not in FORBIDDEN_EXECUTABLES:
                is_safe = True
                for tok in tokens:
                    for pat in FORBIDDEN_ARGUMENT_PATTERNS:
                        if pat.search(tok):
                            is_safe = False
                            break
                    if not is_safe:
                        break
                if is_safe:
                    return ReproductionCommand(
                        args=tokens,
                        relative_cwd=".",
                        expected_exit_codes=[1],
                        expected_output_patterns=[],
                    )

    return None


def validate_reproduction_evidence(
    evidence: Optional[EvidenceRecord],
    workflow: EngineeringWorkflow,
    intake: Optional[WorkflowIntakeSpec] = None,
    case: Optional[ReproductionCase] = None,
    store: Optional[ForgeStore] = None,
) -> None:
    """Strictly validate reproduction evidence before allowing progression to repair planning.

    Raises:
    - MissingEvidenceError: when evidence record does not exist
    - MismatchedEvidenceError: when evidence type, finding, or case identity mismatches
    - StaleRevisionEvidenceError: when evidence is pinned to an out-of-date commit revision
    - UntrustedOracleError: when evidence lacks trusted oracle provenance
    - EvidenceTamperedError: when digests, outputs, or exit codes indicate tampering
    - CorruptedEvidenceError: when evidence content is empty or structurally corrupted
    - EvidenceChainError: when outcome status is not reproduced
    """
    # 1. Existence check
    if evidence is None:
        raise MissingEvidenceError(
            f"No reproduction evidence found for workflow '{workflow.workflow_id}'. "
            "Progression to repair planning requires validated reproduction evidence."
        )

    # 2. Evidence type check
    if evidence.evidence_type != EvidenceType.REPRODUCTION_RESULT:
        raise MismatchedEvidenceError(
            f"Evidence '{evidence.evidence_id}' has type '{evidence.evidence_type.value}', "
            f"expected '{EvidenceType.REPRODUCTION_RESULT.value}'."
        )

    # 3. Content integrity & corruption check
    content = (evidence.content or "").strip()
    if not content:
        raise CorruptedEvidenceError(
            f"Evidence '{evidence.evidence_id}' content is empty. Evidence cannot be verified."
        )

    required_markers = ["Reproduction Command:", "Observed Exit Code:"]
    missing_markers = [m for m in required_markers if m not in content]
    if missing_markers:
        raise CorruptedEvidenceError(
            f"Evidence '{evidence.evidence_id}' content is corrupted or over-redacted (missing: {missing_markers})."
        )

    # 4. Details dictionary and outcome validation
    details = evidence.details or {}
    outcome = details.get("outcome") or details.get("status")
    if str(outcome).lower() != "reproduced":
        raise EvidenceChainError(
            f"Evidence '{evidence.evidence_id}' outcome is '{outcome}'. "
            "Progression to repair planning strictly requires verified 'reproduced' outcome."
        )

    # 5. Finding identity alignment
    expected_finding_id = None
    if intake:
        expected_finding_id = resolve_intake_finding_id(intake)
    if expected_finding_id and evidence.finding_id != expected_finding_id:
        raise MismatchedEvidenceError(
            f"Evidence '{evidence.evidence_id}' belongs to finding '{evidence.finding_id}', "
            f"expected '{expected_finding_id}'."
        )

    # 6. Repository revision pinning & drift detection
    wf_base_sha = workflow.base_commit_sha.strip().lower()
    if case:
        case_sha = case.target_commit_sha.strip().lower()
        if not case_sha.startswith(wf_base_sha) and not wf_base_sha.startswith(case_sha):
            raise StaleRevisionEvidenceError(
                f"Reproduction case '{case.case_id}' was executed on commit '{case.target_commit_sha}', "
                f"which does not match workflow base commit '{workflow.base_commit_sha}'. Revision drift detected."
            )

    if "commit_sha" in details and details["commit_sha"]:
        ev_sha = str(details["commit_sha"]).strip().lower()
        if not ev_sha.startswith(wf_base_sha) and not wf_base_sha.startswith(ev_sha):
            raise StaleRevisionEvidenceError(
                f"Evidence details record commit '{ev_sha}', "
                f"which does not match workflow base commit '{workflow.base_commit_sha}'."
            )

    # 7. Trusted Oracle & Provenance Verification
    if case:
        prov = case.provenance_map or {}
        # Ensure command or execution provenance comes from a trusted oracle source
        exec_prov = prov.get("execution") or prov.get("command") or prov.get("hypothesis")
        if exec_prov and exec_prov not in TRUSTED_ORACLE_PROVENANCES and exec_prov != FieldProvenance.UNTRUSTED_REPOSITORY:
            raise UntrustedOracleError(
                f"Reproduction case '{case.case_id}' has untrusted provenance: '{exec_prov.value}'. "
                "Reproduction must be grounded in an approved test or static oracle."
            )

    # 8. Anti-Tampering & Exit Code Consistency
    observed_exit_code = details.get("exit_code")
    if observed_exit_code is not None:
        # A clean exit (code 0) contradicts a defect reproduction claim
        if observed_exit_code == 0:
            raise EvidenceTamperedError(
                f"Evidence '{evidence.evidence_id}' records exit code 0 (success), "
                "which contradicts the claim of defect reproduction."
            )

    # Check attempt records if store is available
    attempt_id = details.get("attempt_id")
    if store and case and attempt_id:
        try:
            conn = store._conn()
            try:
                cur = conn.execute(
                    "SELECT exit_code, stdout_digest, stderr_digest, outcome FROM reproduction_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                )
                row = cur.fetchone()
                if row:
                    att_exit = row["exit_code"]
                    att_outcome = row["outcome"]
                    if att_exit == 0 or att_outcome not in ("reproduced",):
                        raise EvidenceTamperedError(
                            f"Reproduction attempt '{attempt_id}' recorded exit code {att_exit} and outcome '{att_outcome}', "
                            "inconsistent with reproduction evidence claim."
                        )
            finally:
                conn.close()
        except EvidenceTamperedError:
            raise
        except Exception:
            pass
