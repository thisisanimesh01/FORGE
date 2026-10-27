"""Certified Engineering Dossier generator, validator, and export pipeline.

Phase 5 Milestone 5: Exports a deterministic, integrity-verifiable engineering package
enabling human operators to audit proposed repairs, verification evidence, and audit ledgers
before manual application.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from forge import __version__
from forge.config import redact_secrets
from forge.repair.contracts import compute_patch_sha256
from forge.repair.models import ProtectedPathPolicy, RepairPlan
from forge.schemas import EvidenceType, Finding, FindingSeverity
from forge.storage.store import ForgeStore
from forge.workflow.dossier_models import (
    AuditLedgerExport,
    DossierError,
    DossierExportDirectoryError,
    DossierExportRefusedError,
    DossierFileEntry,
    DossierIntegrityError,
    DossierManifest,
    DossierSecurityError,
    DossierSignature,
    DossierSignatureError,
    DossierValidationError,
    DossierValidationResult,
    DossierWorkflowNotApprovedError,
    HumanReviewStatus,
    VerificationCertificate,
    VerificationCertificateCheckEntry,
)
from forge.workflow.models import (
    EngineeringWorkflow,
    EngineeringWorkflowEvent,
    EngineeringWorkflowState,
    MultiCheckVerificationResult,
    VerificationAggregateOutcome,
    WorkflowIntakeSpec,
)

# Standard set of canonical files required in an exported engineering dossier
REQUIRED_DOSSIER_FILES: Set[str] = {
    "patch.diff",
    "verification_certificate.json",
    "findings.sarif",
    "audit_ledger.json",
    "APPLY.md",
}

ALL_ALLOWED_DOSSIER_FILES: Set[str] = REQUIRED_DOSSIER_FILES | {
    "manifest.json",
}

SARIF_SCHEMA_URI = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
SARIF_VERSION = "2.1.0"

SEVERITY_TO_SARIF_LEVEL = {
    FindingSeverity.CRITICAL: "error",
    FindingSeverity.HIGH: "error",
    FindingSeverity.MEDIUM: "warning",
    FindingSeverity.LOW: "note",
    FindingSeverity.INFO: "note",
}


# =============================================================================
# Helper Utilities
# =============================================================================

def compute_file_sha256(path: Path) -> str:
    """Compute lowercase hexadecimal SHA-256 digest of a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def compute_bytes_sha256(data: bytes) -> str:
    """Compute lowercase hexadecimal SHA-256 digest of raw bytes."""
    return hashlib.sha256(data).hexdigest().lower()


def verify_path_containment(target_path: Path, parent_dir: Path) -> None:
    """Ensure target path is strictly contained within parent directory without symlink traversal."""
    resolved_parent = parent_dir.resolve()
    resolved_target = target_path.resolve()

    # Reject if target path or parent has symlinks
    if target_path.is_symlink() or parent_dir.is_symlink():
        raise DossierSecurityError(f"Symlinks are forbidden in dossier export: '{target_path}'")

    try:
        resolved_target.relative_to(resolved_parent)
    except ValueError:
        raise DossierSecurityError(
            f"Path traversal detected: '{target_path}' escapes parent directory '{parent_dir}'"
        )


# =============================================================================
# Pre-Export Workflow Validation
# =============================================================================

def validate_workflow_for_export(
    workflow_id: str,
    store: ForgeStore,
    target_repo_path: Optional[Union[str, Path]] = None,
) -> Tuple[
    EngineeringWorkflow,
    WorkflowIntakeSpec,
    RepairPlan,
    MultiCheckVerificationResult,
    List[EngineeringWorkflowEvent],
    Finding,
]:
    """Strictly revalidate workflow state, ledger, patch integrity, and verification before export.

    Enforces:
    1. Workflow state MUST be APPROVED with recorded human_decision='approve'.
    2. Ledger events must contain valid approval event and strictly sequential order.
    3. Associated repair plan exists, diff is valid and matches stored SHA256.
    4. Target file is not protected under system policy.
    5. Multi-check verification exists, passed, and detected zero regressions.
    6. Intake specification exists and base commit SHA matches.
    7. Pinned commit SHA matches target repo HEAD if target repo path is accessible.
    """
    wf = store.get_engineering_workflow(workflow_id)
    if not wf:
        raise DossierExportRefusedError(f"Engineering workflow '{workflow_id}' not found in store.")

    # 1. State check: MUST be APPROVED
    if wf.state != EngineeringWorkflowState.APPROVED:
        raise DossierWorkflowNotApprovedError(
            f"Workflow '{workflow_id}' is in state '{wf.state.value}'. "
            f"Dossiers may only be exported after explicit human operator approval. "
            f"Current state is not '{EngineeringWorkflowState.APPROVED.value}'."
        )

    if wf.human_decision != "approve":
        raise DossierWorkflowNotApprovedError(
            f"Workflow '{workflow_id}' is marked as '{wf.state.value}' but has invalid "
            f"human_decision='{wf.human_decision}'. Explicit 'approve' decision is required."
        )

    # 2. Transactional Ledger Audit
    events = store.list_engineering_workflow_events(workflow_id)
    if not events:
        raise DossierExportRefusedError(
            f"Audit ledger for workflow '{workflow_id}' is empty. Transactional integrity violated."
        )

    # Verify sequential ordering (1, 2, ..., N)
    for idx, ev in enumerate(events, start=1):
        if ev.sequence != idx:
            raise DossierIntegrityError(
                f"Ledger sequence discrepancy in workflow '{workflow_id}': "
                f"expected sequence {idx}, observed {ev.sequence}."
            )

    # Verify presence of approval event in ledger
    approval_event = next(
        (
            e for e in events
            if e.to_state == EngineeringWorkflowState.APPROVED
            and e.payload.get("human_decision") == "approve"
        ),
        None,
    )
    if not approval_event:
        raise DossierWorkflowNotApprovedError(
            f"Workflow '{workflow_id}' does not have a verified approval event recorded in its ledger."
        )

    # 3. Intake Specification
    intake = store.get_workflow_intake(wf.intake_id)
    if not intake:
        raise DossierExportRefusedError(
            f"Workflow intake record '{wf.intake_id}' not found in store."
        )

    if intake.base_commit_sha != wf.base_commit_sha:
        raise DossierIntegrityError(
            f"Commit SHA mismatch: workflow base commit '{wf.base_commit_sha}' does not match "
            f"intake base commit '{intake.base_commit_sha}'."
        )

    # 4. Target Repository Drift Verification (if accessible)
    repo_to_check = target_repo_path or wf.target_repo
    if repo_to_check:
        rpath = Path(repo_to_check).resolve()
        if (rpath / ".git").exists():
            try:
                head_rev = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=rpath,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip().lower()
                if head_rev != wf.base_commit_sha.lower():
                    raise DossierIntegrityError(
                        f"Target repository revision drift detected: repository HEAD is '{head_rev}', "
                        f"but workflow was pinned to '{wf.base_commit_sha}'. Export refused."
                    )
            except subprocess.SubprocessError as e:
                # If git command failed, record error if path was explicitly provided
                if target_repo_path:
                    raise DossierExportRefusedError(f"Failed to inspect target repository at '{rpath}': {e}")

    # 5. Repair Plan & Patch Integrity
    if not wf.repair_plan_id:
        raise DossierExportRefusedError(
            f"Workflow '{workflow_id}' does not have an associated repair plan ID."
        )

    plan = store.get_repair_plan(wf.repair_plan_id)
    if not plan:
        raise DossierExportRefusedError(
            f"Repair plan '{wf.repair_plan_id}' not found in store."
        )

    if not plan.patch_candidate:
        raise DossierExportRefusedError(
            f"Repair plan '{plan.plan_id}' does not contain a patch candidate."
        )

    # Anti-tampering check: verify SHA256 digest of unified diff
    computed_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
    if computed_digest != plan.patch_candidate.sha256_digest:
        raise DossierIntegrityError(
            f"Patch candidate diff content does not match stored SHA-256 digest "
            f"(computed: {computed_digest}, stored: {plan.patch_candidate.sha256_digest}). Tampering suspected."
        )

    # Protected file check
    is_prot, prot_msg = ProtectedPathPolicy.is_protected(plan.target_file)
    if is_prot:
        raise DossierSecurityError(
            f"Target file '{plan.target_file}' in repair plan is protected by safety policy: {prot_msg}"
        )

    # 6. Multi-Check Verification Record
    verifications = store.list_multi_check_verifications(workflow_id)
    if not verifications:
        raise DossierExportRefusedError(
            f"No multi-check verification records found for workflow '{workflow_id}'."
        )

    # Select matching verification attempt
    if wf.verification_attempt_id:
        verification = next((v for v in verifications if v.attempt_id == wf.verification_attempt_id), None)
        if not verification:
            raise DossierExportRefusedError(
                f"Recorded verification attempt '{wf.verification_attempt_id}' not found in store."
            )
    else:
        verification = verifications[-1]

    if verification.aggregate_outcome != VerificationAggregateOutcome.PASSED:
        raise DossierExportRefusedError(
            f"Verification outcome for workflow '{workflow_id}' is '{verification.aggregate_outcome.value}', "
            f"not 'passed'. Export refused."
        )

    if verification.regression_detected:
        raise DossierIntegrityError(
            f"Verification for workflow '{workflow_id}' recorded a regression in pre-existing test suite."
        )

    if verification.patch_digest != plan.patch_candidate.sha256_digest:
        raise DossierIntegrityError(
            f"Verification patch digest '{verification.patch_digest}' does not match "
            f"repair plan patch digest '{plan.patch_candidate.sha256_digest}'."
        )

    # 7. Finding Record
    finding = store.get_finding(plan.finding_id)
    if not finding:
        # Create minimal fallback finding representation if finding row was not found
        finding = Finding(
            finding_id=plan.finding_id,
            run_id=wf.workflow_id,
            title=intake.title,
            description=intake.description,
            severity=FindingSeverity.HIGH,
            affected_files=intake.affected_files or [plan.target_file],
        )

    return wf, intake, plan, verification, events, finding


# =============================================================================
# Cryptographic Signing (HMAC-SHA256)
# =============================================================================

def sign_manifest_files(
    files_map: Dict[str, DossierFileEntry],
    key: Union[str, bytes],
    key_id: Optional[str] = None,
) -> DossierSignature:
    """Generate an HMAC-SHA256 cryptographic signature over the canonical manifest files map.

    Note: A cryptographic signature is strictly distinct from a SHA-256 integrity checksum.
    The checksums in manifest.json provide tamper-detection; this HMAC provides authentic provenance.
    """
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    if not key_bytes:
        raise DossierSignatureError("Signing key cannot be empty.")

    # Canonical serialization of the file entries dictionary
    canonical_files_dict = {
        name: {
            "byte_size": entry.byte_size,
            "relative_path": entry.relative_path,
            "sha256": entry.sha256,
        }
        for name, entry in sorted(files_map.items())
    }
    canonical_bytes = json.dumps(
        canonical_files_dict, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    sig_hex = hmac.new(key_bytes, canonical_bytes, hashlib.sha256).hexdigest()
    kid = key_id or f"key_{hashlib.sha256(key_bytes).hexdigest()[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    return DossierSignature(
        algorithm="HMAC-SHA256",
        key_id=kid,
        signature_hex=sig_hex,
        signed_at=now_iso,
    )


def verify_manifest_signature(
    manifest: DossierManifest,
    key: Union[str, bytes],
) -> bool:
    """Verify the HMAC-SHA256 signature in a DossierManifest against a provided key."""
    if not manifest.signature:
        return False

    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    if not key_bytes:
        return False

    canonical_files_dict = {
        name: {
            "byte_size": entry.byte_size,
            "relative_path": entry.relative_path,
            "sha256": entry.sha256,
        }
        for name, entry in sorted(manifest.files.items())
    }
    canonical_bytes = json.dumps(
        canonical_files_dict, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    expected_sig = hmac.new(key_bytes, canonical_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(manifest.signature.signature_hex, expected_sig)


# =============================================================================
# Artifact Builders
# =============================================================================

def build_verification_certificate(
    workflow: EngineeringWorkflow,
    intake: WorkflowIntakeSpec,
    plan: RepairPlan,
    verification: MultiCheckVerificationResult,
    events: List[EngineeringWorkflowEvent],
) -> VerificationCertificate:
    """Build a deterministic, typed VerificationCertificate from verified workflow records."""
    approval_event = next(
        (e for e in events if e.to_state == EngineeringWorkflowState.APPROVED),
        None,
    )
    reviewed_at = (
        approval_event.created_at.isoformat()
        if approval_event
        else workflow.updated_at.isoformat()
    )

    cert_hash = hashlib.sha256(
        f"{workflow.workflow_id}:{plan.plan_id}:{plan.patch_candidate.sha256_digest}".encode()
    ).hexdigest()[:12]

    # Convert check results deterministically
    def _convert_check(r: Any) -> VerificationCertificateCheckEntry:
        return VerificationCertificateCheckEntry(
            check_type=r.check_type.value if hasattr(r.check_type, "value") else str(r.check_type),
            name=r.name,
            command=r.command,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            is_baseline=r.is_baseline,
            exit_code=r.exit_code,
            duration_ms=round(r.duration_ms, 2),
            output_digest=r.output_digest,
            diagnostic=redact_secrets(r.diagnostic or ""),
        )

    base_checks = [_convert_check(c) for c in verification.baseline_results]
    cand_checks = [_convert_check(c) for c in verification.candidate_results]

    checks_summary = {
        "total": len(verification.candidate_results),
        "passed": sum(1 for c in verification.candidate_results if c.status.value == "passed"),
        "failed": sum(1 for c in verification.candidate_results if c.status.value == "failed"),
        "skipped": sum(1 for c in verification.candidate_results if c.status.value == "skipped"),
    }

    human_status = HumanReviewStatus(
        state=workflow.state.value,
        decision=workflow.human_decision or "approve",
        reviewed_at=reviewed_at,
        notes=redact_secrets(workflow.human_notes or ""),
    )

    return VerificationCertificate(
        certificate_id=f"vcert_{cert_hash}",
        workflow_id=workflow.workflow_id,
        intake_id=intake.intake_id,
        repair_plan_id=plan.plan_id,
        target_repo=redact_secrets(workflow.target_repo),
        base_commit_sha=workflow.base_commit_sha,
        patch_digest=plan.patch_candidate.sha256_digest,
        target_file=plan.target_file,
        aggregate_outcome="passed",
        sandbox_available=verification.sandbox_available,
        regression_detected=False,
        checks_summary=checks_summary,
        baseline_results=base_checks,
        candidate_results=cand_checks,
        limitations=[redact_secrets(lim) for lim in verification.limitations],
        verified_at=verification.created_at.isoformat(),
        human_review=human_status,
    )


def build_audit_ledger_export(
    workflow: EngineeringWorkflow,
    intake: WorkflowIntakeSpec,
    events: List[EngineeringWorkflowEvent],
) -> AuditLedgerExport:
    """Build a deterministic, secret-scrubbed export of the append-only workflow ledger."""
    clean_events: List[Dict[str, Any]] = []
    for ev in sorted(events, key=lambda x: x.sequence):
        # Scrub secrets from event payload recursively
        payload_str = json.dumps(ev.payload, sort_keys=True)
        clean_payload = json.loads(redact_secrets(payload_str))

        clean_events.append(
            {
                "sequence": ev.sequence,
                "event_id": ev.event_id,
                "from_state": ev.from_state.value if ev.from_state else None,
                "to_state": ev.to_state.value if ev.to_state else str(ev.to_state),
                "stage": ev.stage,
                "payload": clean_payload,
                "created_at": ev.created_at.isoformat(),
            }
        )

    return AuditLedgerExport(
        workflow_id=workflow.workflow_id,
        intake_id=intake.intake_id,
        target_repo=redact_secrets(workflow.target_repo),
        base_commit_sha=workflow.base_commit_sha,
        version=workflow.version,
        current_state=workflow.state.value,
        human_decision=workflow.human_decision or "approve",
        human_notes=redact_secrets(workflow.human_notes or ""),
        event_count=len(clean_events),
        events=clean_events,
    )


def build_findings_sarif(
    workflow: EngineeringWorkflow,
    intake: WorkflowIntakeSpec,
    plan: RepairPlan,
    finding: Finding,
    evidence_ids: List[str],
) -> Dict[str, Any]:
    """Serialize finding and evidence context into a standards-compliant SARIF 2.1.0 document."""
    rule_id = f"FORGE_{finding.severity.value.upper()}_DEFECT"
    sarif_level = SEVERITY_TO_SARIF_LEVEL.get(finding.severity, "warning")

    rule_def = {
        "id": rule_id,
        "name": rule_id,
        "shortDescription": {
            "text": redact_secrets(finding.title[:120]),
        },
        "fullDescription": {
            "text": redact_secrets(finding.description[:500]),
        },
        "defaultConfiguration": {
            "level": sarif_level,
        },
        "properties": {
            "tags": ["defect", "correctness", finding.severity.value],
        },
    }

    result_entry = {
        "ruleId": rule_id,
        "level": sarif_level,
        "message": {
            "text": redact_secrets(finding.description),
        },
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {
                        "uri": plan.target_file,
                        "uriBaseId": "%SRCROOT%",
                    },
                    "region": {
                        "startLine": 1,
                    },
                }
            }
        ],
        "properties": {
            "workflow_id": workflow.workflow_id,
            "intake_id": intake.intake_id,
            "finding_id": finding.finding_id,
            "severity": finding.severity.value,
            "confidence": finding.confidence.value,
            "lifecycle_state": finding.lifecycle_state.value,
            "evidence_references": evidence_ids,
            "patch_sha256": plan.patch_candidate.sha256_digest if plan.patch_candidate else None,
        },
    }

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "FORGE",
                        "version": __version__,
                        "informationUri": "https://github.com/thisisanimesh01/FORGE",
                        "rules": [rule_def],
                    }
                },
                "artifacts": [
                    {
                        "location": {
                            "uri": plan.target_file,
                        }
                    }
                ],
                "results": [result_entry],
            }
        ],
    }


def build_apply_markdown(
    workflow: EngineeringWorkflow,
    intake: WorkflowIntakeSpec,
    plan: RepairPlan,
    verification: MultiCheckVerificationResult,
    cert: VerificationCertificate,
) -> str:
    """Generate human-readable, auditable APPLY.md instructions for manual patch inspection."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format check results table
    check_rows = []
    for c in cert.candidate_results:
        st_emoji = "[PASS]" if c.status == "passed" else f"[{c.status.upper()}]"
        check_rows.append(
            f"| `{c.name}` | {st_emoji} | `{' '.join(c.command)}` | `{c.exit_code}` | {c.duration_ms:.1f}ms |"
        )
    check_table = "\n".join(check_rows) if check_rows else "| None | - | - | - | - |"

    limitations_text = ""
    if cert.limitations:
        limitations_text = "\n### Verification Limitations\n" + "\n".join(
            f"- {lim}" for lim in cert.limitations
        )

    # Format instructions
    md_content = f"""# FORGE Engineering Dossier: Manual Application & Review Guide

- **Dossier Package**: `forge_engineering_dossier`
- **Workflow ID**: `{workflow.workflow_id}`
- **Intake Task**: `{intake.intake_id}` (`{intake.source_type.value}`)
- **Target Repository**: `{workflow.target_repo}`
- **Pinned Base Commit**: `{workflow.base_commit_sha}`
- **Patch SHA-256 Digest**: `{plan.patch_candidate.sha256_digest}`
- **Modified Target File**: `{plan.target_file}`
- **Human Approval Status**: `APPROVED` (Reviewer: Operator, Decided: `{cert.human_review.reviewed_at}`)
- **Export Generated**: `{now_utc}`

---

> [!IMPORTANT]
> **Audit-Only & Human Supervision Notice**  
> In accordance with FORGE security rules (AGENTS.md Section 1), FORGE operates strictly as an intelligence and proposal system. FORGE **never modifies target repositories, creates git commits, pushes branches, opens PRs, or deploys code**.  
> The candidate patch in `patch.diff` has passed isolated multi-check verification and explicit operator approval. Applying this change is strictly the responsibility of an authorized software engineer.

---

## 1. Verification Summary

Candidate patch verified cleanly across isolated baseline and candidate scratch workspaces with zero observed regressions:

| Check Name | Status | Command Executed | Exit Code | Duration |
|---|---|---|---|---|
{check_table}
{limitations_text}

---

## 2. Step-by-Step Manual Application Procedure

Execute the following commands from the root of the target repository:

### Step 1: Pre-Application Integrity Check
Ensure your working tree is clean and checked out to the exact pinned revision:
```bash
# 1. Verify working tree is clean
git status

# 2. Verify HEAD matches the pinned base revision
git rev-parse HEAD
# Expected output must match:
# {workflow.base_commit_sha}
```

### Step 2: Dry-Run Inspection
Verify that the patch applies cleanly without conflicts:
```bash
git apply --check /path/to/dossier/patch.diff
```

### Step 3: Apply the Patch
Apply the certified candidate diff:
```bash
git apply /path/to/dossier/patch.diff
```

### Step 4: Post-Application Verification
Re-run the verified target defect check to confirm resolution:
```bash
# Target defect verification command:
{plan.verification_plan or "pytest -q tests/"}
```

### Step 5: Rollback Procedure (If Needed)
If post-application manual tests fail or unexpected behavior is observed:
```bash
# Revert the applied patch cleanly:
git apply --reverse /path/to/dossier/patch.diff

# Or discard changes to the target file:
git checkout -- {plan.target_file}
```

---

## 3. Package Integrity & Manifest
The accompanying `manifest.json` provides cryptographic SHA-256 checksums for all files in this directory. To verify integrity offline:
```bash
forge workflow validate-dossier /path/to/dossier
```
"""
    return md_content.strip() + "\n"


# =============================================================================
# Dossier Export Engine
# =============================================================================

def export_engineering_dossier(
    workflow_id: str,
    output_dir: Union[str, Path],
    store: ForgeStore,
    target_repo_path: Optional[Union[str, Path]] = None,
    signing_key: Optional[Union[str, bytes]] = None,
    key_id: Optional[str] = None,
    force: bool = False,
) -> DossierManifest:
    """Export an approved engineering workflow into a certified, integrity-verified dossier package.

    Parameters:
    - workflow_id: Unique identifier of the approved engineering workflow.
    - output_dir: Directory path where the dossier package will be created.
    - store: ForgeStore database connection.
    - target_repo_path: Optional path to local target repository for HEAD drift validation.
    - signing_key: Optional secret key to generate HMAC-SHA256 signature over manifest.
    - key_id: Optional identifier for signing key.
    - force: Whether to allow overwriting an existing dossier in the output directory.

    Returns:
    - DossierManifest: Canonical integrity manifest for the exported dossier.
    """
    raw_path = Path(output_dir)
    if raw_path.is_symlink() or os.path.islink(str(raw_path)):
        raise DossierSecurityError(f"Output directory cannot be a symlink: '{raw_path}'")
    out_path = raw_path.resolve()
    if out_path.is_symlink() or os.path.islink(str(out_path)):
        raise DossierSecurityError(f"Output directory cannot be a symlink: '{out_path}'")

    # Output directory policy
    if out_path.exists():
        if not out_path.is_dir():
            raise DossierExportDirectoryError(f"Target path '{out_path}' exists and is not a directory.")

        existing_entries = os.listdir(out_path)
        if existing_entries:
            if not force:
                raise DossierExportDirectoryError(
                    f"Export directory '{out_path}' already exists and is not empty ({len(existing_entries)} items). "
                    f"Use --force to overwrite an existing dossier."
                )

            # Security check on existing entries before overwrite: reject symlinks or unexpected foreign files
            for entry_name in existing_entries:
                entry_path = out_path / entry_name
                if entry_path.is_symlink():
                    raise DossierSecurityError(
                        f"Refusing to overwrite directory containing symlink: '{entry_path}'"
                    )
    else:
        out_path.mkdir(parents=True, exist_ok=True)

    # 1. Strictly validate workflow prerequisites
    wf, intake, plan, verification, events, finding = validate_workflow_for_export(
        workflow_id=workflow_id,
        store=store,
        target_repo_path=target_repo_path,
    )

    # 2. Build artifacts in memory
    patch_diff_content = plan.patch_candidate.unified_diff
    if not patch_diff_content.endswith("\n"):
        patch_diff_content += "\n"

    cert_obj = build_verification_certificate(
        workflow=wf,
        intake=intake,
        plan=plan,
        verification=verification,
        events=events,
    )
    cert_json = json.dumps(cert_obj.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    ledger_obj = build_audit_ledger_export(
        workflow=wf,
        intake=intake,
        events=events,
    )
    ledger_json = json.dumps(ledger_obj.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    evidence_ids = plan.cited_evidence_ids or []
    if wf.repro_case_id:
        evidence_ids = list(set(evidence_ids + [wf.repro_case_id]))
    sarif_dict = build_findings_sarif(
        workflow=wf,
        intake=intake,
        plan=plan,
        finding=finding,
        evidence_ids=evidence_ids,
    )
    sarif_json = json.dumps(sarif_dict, indent=2, sort_keys=True) + "\n"

    apply_md_content = build_apply_markdown(
        workflow=wf,
        intake=intake,
        plan=plan,
        verification=verification,
        cert=cert_obj,
    )

    artifacts_to_write: Dict[str, bytes] = {
        "patch.diff": patch_diff_content.encode("utf-8"),
        "verification_certificate.json": cert_json.encode("utf-8"),
        "findings.sarif": sarif_json.encode("utf-8"),
        "audit_ledger.json": ledger_json.encode("utf-8"),
        "APPLY.md": apply_md_content.encode("utf-8"),
    }

    # 3. Write each artifact and compute integrity metadata
    files_manifest: Dict[str, DossierFileEntry] = {}
    for filename, data in artifacts_to_write.items():
        target_file = out_path / filename
        verify_path_containment(target_file, out_path)

        with open(target_file, "wb") as f:
            f.write(data)

        files_manifest[filename] = DossierFileEntry(
            relative_path=filename,
            sha256=compute_bytes_sha256(data),
            byte_size=len(data),
        )

    # 4. Optional Cryptographic Signing
    signature: Optional[DossierSignature] = None
    if signing_key is not None:
        signature = sign_manifest_files(
            files_map=files_manifest,
            key=signing_key,
            key_id=key_id,
        )

    # 5. Build and write manifest.json
    dos_id_hash = hashlib.sha256(
        f"{wf.workflow_id}:{plan.patch_candidate.sha256_digest}:{wf.base_commit_sha}".encode()
    ).hexdigest()[:12]
    now_iso = datetime.now(timezone.utc).isoformat()

    manifest = DossierManifest(
        dossier_id=f"dos_{dos_id_hash}",
        workflow_id=wf.workflow_id,
        intake_id=intake.intake_id,
        target_repo=redact_secrets(wf.target_repo),
        base_commit_sha=wf.base_commit_sha,
        patch_digest=plan.patch_candidate.sha256_digest,
        exported_at=now_iso,
        files=files_manifest,
        signature=signature,
    )

    manifest_file = out_path / "manifest.json"
    verify_path_containment(manifest_file, out_path)
    manifest_bytes = json.dumps(
        manifest.model_dump(mode="json"), indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"

    with open(manifest_file, "wb") as f:
        f.write(manifest_bytes)

    return manifest


# =============================================================================
# Offline Dossier Package Validation
# =============================================================================

def validate_engineering_dossier(
    dossier_dir: Union[str, Path],
    signing_key: Optional[Union[str, bytes]] = None,
) -> DossierValidationResult:
    """Perform deterministic offline verification of an exported engineering dossier package.

    Validates:
    1. Directory existence, permissions, and absence of symlinks.
    2. Manifest existence and schema validity.
    3. Presence of all required files and absence of foreign or untracked files.
    4. Path containment: zero directory traversal escapes.
    5. Exact SHA-256 match for every file in the package against manifest.json.
    6. Exact byte size match for every file in the package.
    7. Consistency between patch.diff SHA256 and manifest patch_digest.
    8. Consistency of verification_certificate.json and audit_ledger.json.
    9. Optional HMAC-SHA256 signature verification if signature is present.
    """
    dpath = Path(dossier_dir).resolve()
    errors: List[str] = []
    warnings: List[str] = []
    files_verified = 0

    if not dpath.exists():
        return DossierValidationResult(
            is_valid=False,
            dossier_dir=str(dpath),
            errors=[f"Dossier directory does not exist: '{dpath}'"],
        )

    if not dpath.is_dir():
        return DossierValidationResult(
            is_valid=False,
            dossier_dir=str(dpath),
            errors=[f"Target path is not a directory: '{dpath}'"],
        )

    if dpath.is_symlink():
        return DossierValidationResult(
            is_valid=False,
            dossier_dir=str(dpath),
            errors=[f"Dossier directory cannot be a symlink: '{dpath}'"],
        )

    # 1. Manifest file existence
    manifest_path = dpath / "manifest.json"
    if not manifest_path.is_file():
        return DossierValidationResult(
            is_valid=False,
            dossier_dir=str(dpath),
            errors=[f"Mandatory file 'manifest.json' is missing from '{dpath}'."],
        )

    if manifest_path.is_symlink():
        return DossierValidationResult(
            is_valid=False,
            dossier_dir=str(dpath),
            errors=["'manifest.json' cannot be a symlink."],
        )

    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = DossierManifest.model_validate_json(manifest_text)
    except Exception as e:
        return DossierValidationResult(
            is_valid=False,
            dossier_dir=str(dpath),
            errors=[f"Malformed 'manifest.json': {e}"],
        )

    # 2. Check for missing required files
    for req_file in REQUIRED_DOSSIER_FILES:
        if req_file not in manifest.files:
            errors.append(f"Required artifact '{req_file}' is missing from manifest.json.")
        target_f = dpath / req_file
        if not target_f.exists():
            errors.append(f"Required artifact '{req_file}' was not found on disk in dossier directory.")
        elif target_f.is_symlink():
            errors.append(f"Security violation: artifact '{req_file}' is a symlink.")

    # 3. Check for foreign, untracked, or unexpected files on disk
    try:
        disk_files = set(os.listdir(dpath))
        unexpected = disk_files - ALL_ALLOWED_DOSSIER_FILES
        if unexpected:
            errors.append(
                f"Unexpected foreign files found in dossier directory: {sorted(unexpected)}"
            )
    except Exception as e:
        errors.append(f"Failed to scan directory files: {e}")

    # 4. Verify integrity (SHA-256 and byte size) of every file in manifest
    for fname, entry in manifest.files.items():
        fpath = dpath / fname
        try:
            verify_path_containment(fpath, dpath)
        except DossierSecurityError as e:
            errors.append(str(e))
            continue

        if not fpath.exists():
            errors.append(f"Manifest lists file '{fname}' but it does not exist on disk.")
            continue

        if fpath.is_symlink():
            errors.append(f"Artifact '{fname}' is an illegal symlink.")
            continue

        # Verify byte size
        actual_size = fpath.stat().st_size
        if actual_size != entry.byte_size:
            errors.append(
                f"Byte size mismatch on '{fname}': expected {entry.byte_size}, observed {actual_size}."
            )

        # Verify SHA-256 checksum
        actual_sha = compute_file_sha256(fpath)
        if actual_sha != entry.sha256.lower():
            errors.append(
                f"Integrity check failed on '{fname}': expected SHA-256 {entry.sha256}, observed {actual_sha} (tampered)."
            )
        else:
            files_verified += 1

    # 5. Semantic Validation of Artifacts (if checksums passed)
    # Check patch.diff matches manifest patch_digest
    if "patch.diff" in manifest.files:
        patch_entry = manifest.files["patch.diff"]
        if patch_entry.sha256.lower() != manifest.patch_digest.lower():
            errors.append(
                f"Patch candidate digest discrepancy: 'patch.diff' SHA-256 '{patch_entry.sha256}' "
                f"does not match manifest patch_digest '{manifest.patch_digest}'."
            )

    # Check verification_certificate.json
    cert_path = dpath / "verification_certificate.json"
    if cert_path.is_file():
        try:
            cert_data = json.loads(cert_path.read_text(encoding="utf-8"))
            if cert_data.get("aggregate_outcome") != "passed":
                errors.append(
                    f"Verification certificate aggregate_outcome is '{cert_data.get('aggregate_outcome')}', expected 'passed'."
                )
            if cert_data.get("patch_digest") != manifest.patch_digest:
                errors.append(
                    f"Verification certificate patch_digest '{cert_data.get('patch_digest')}' "
                    f"does not match manifest patch_digest '{manifest.patch_digest}'."
                )
            if cert_data.get("base_commit_sha") != manifest.base_commit_sha:
                errors.append(
                    f"Verification certificate base_commit_sha '{cert_data.get('base_commit_sha')}' "
                    f"does not match manifest base_commit_sha '{manifest.base_commit_sha}'."
                )
            hr = cert_data.get("human_review", {})
            if hr.get("decision") != "approve":
                errors.append(
                    f"Verification certificate human_review decision is '{hr.get('decision')}', expected 'approve'."
                )
        except Exception as e:
            errors.append(f"Invalid verification_certificate.json structure: {e}")

    # Check audit_ledger.json
    ledger_path = dpath / "audit_ledger.json"
    if ledger_path.is_file():
        try:
            ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
            if ledger_data.get("current_state") != "approved":
                errors.append(
                    f"Audit ledger current_state is '{ledger_data.get('current_state')}', expected 'approved'."
                )
            if ledger_data.get("human_decision") != "approve":
                errors.append(
                    f"Audit ledger human_decision is '{ledger_data.get('human_decision')}', expected 'approve'."
                )
        except Exception as e:
            errors.append(f"Invalid audit_ledger.json structure: {e}")

    # 6. Cryptographic Signature Verification
    signature_present = manifest.signature is not None
    signature_valid: Optional[bool] = None

    if signature_present:
        if signing_key is not None:
            is_sig_ok = verify_manifest_signature(manifest, signing_key)
            signature_valid = is_sig_ok
            if not is_sig_ok:
                errors.append(
                    f"HMAC-SHA256 signature verification failed (algorithm: {manifest.signature.algorithm}, "
                    f"key_id: '{manifest.signature.key_id}'). Signature is invalid or key mismatch."
                )
        else:
            warnings.append(
                f"Manifest contains an HMAC-SHA256 signature (key_id: '{manifest.signature.key_id}'), "
                f"but no signing key was provided for cryptographic verification."
            )
    elif signing_key is not None:
        warnings.append(
            "A signing key was provided, but the dossier manifest is unsigned."
        )

    is_valid = len(errors) == 0

    return DossierValidationResult(
        is_valid=is_valid,
        dossier_dir=str(dpath),
        workflow_id=manifest.workflow_id,
        base_commit_sha=manifest.base_commit_sha,
        patch_digest=manifest.patch_digest,
        files_verified=files_verified,
        signature_present=signature_present,
        signature_valid=signature_valid,
        errors=errors,
        warnings=warnings,
        manifest=manifest if is_valid else None,
    )
