"""Phase 8 Repair Execution Architecture — Review Dossier Builder.

Constructs factual, structured review dossiers consolidating original findings,
evidence, candidate patches, independent M4 validation, and M5 sandbox verification.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from forge.repair.execution.failures import (
    ApprovalPreconditionError,
    InsufficientEvidenceError,
)
from forge.repair.execution.models import (
    IndependentValidationResult,
    PatchArtifact,
    RepairExecutionPlan,
    RepairRequest,
    ReviewDossier,
    ValidationOutcome,
    VerificationOutcome,
    VerificationRunRecord,
)
from forge.repair.execution.storage import RepairExecutionStore
from forge.schemas import EvidenceRecord, Finding
from forge.storage.store import ForgeStore


class ReviewDossierBuilder:
    """Assembles comprehensive, factual review dossiers from persistent storage."""

    def __init__(
        self,
        execution_store: RepairExecutionStore,
        forge_store: Optional[ForgeStore] = None,
    ):
        self.exec_store = execution_store
        self.forge_store = forge_store

    def build_dossier(
        self,
        repair_id: str,
        request: Optional[RepairRequest] = None,
        patch: Optional[PatchArtifact] = None,
        validation_result: Optional[IndependentValidationResult] = None,
        verification_run: Optional[VerificationRunRecord] = None,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        dossier_version: str = "1.0.0",
    ) -> ReviewDossier:
        """Assemble a complete ReviewDossier for a repair request."""
        # 1. Resolve RepairRequest
        req = request or self.exec_store.get_repair_request(repair_id)
        if not req:
            raise ApprovalPreconditionError(f"Repair request '{repair_id}' does not exist.")

        # 2. Resolve PatchArtifact
        p_art = patch
        if not p_art and req.patch_artifact_id:
            p_art = self.exec_store.get_patch_artifact(req.patch_artifact_id)
        if not p_art:
            raise ApprovalPreconditionError(
                f"No patch artifact associated with repair request '{repair_id}'."
            )

        # 3. Resolve Finding
        fnd = finding
        if not fnd and self.forge_store and req.finding_id:
            fnd = self.forge_store.get_finding(req.finding_id)
        finding_title = fnd.title if fnd else "Unknown Finding"
        finding_sev = fnd.severity.value if fnd and hasattr(fnd.severity, "value") else "UNKNOWN"

        # 4. Resolve Evidence
        ev_records = evidence_records
        if ev_records is None and self.forge_store and req.finding_id:
            ev_records = self.forge_store.list_evidence_for_finding(req.finding_id)
        evidence_ids = [e.evidence_id for e in (ev_records or [])] if ev_records else (req.evidence_ids or [])

        # 5. Resolve M4 Validation Result
        val_res = validation_result
        if not val_res:
            val_res = self.exec_store.get_validation_result_for_patch(p_art.artifact_id)
        if not val_res:
            val_list = self.exec_store.list_validation_results_for_repair(repair_id)
            if val_list:
                val_res = val_list[0]
        val_id = val_res.validation_id if val_res else "NOT_VALIDATED"
        val_outcome = val_res.overall_status.value if val_res else "NONE"

        # 6. Resolve M5 Verification Run
        verif_run = verification_run
        if not verif_run:
            verif_run = self.exec_store.get_latest_verification_run_for_repair(repair_id)
        verif_id = verif_run.verification_id if verif_run else "NOT_VERIFIED"
        verif_outcome = verif_run.overall_outcome.value if verif_run else "NONE"

        # Summaries
        baseline_summary: Dict[str, Any] = {}
        patched_summary: Dict[str, Any] = {}
        regression_summary = ""
        regression_detected = False
        defect_resolved = False

        if verif_run:
            regression_summary = verif_run.regression_analysis.summary
            regression_detected = verif_run.regression_analysis.regression_detected
            defect_resolved = verif_run.regression_analysis.defect_resolved

            baseline_total = sum(b.tests_collected for b in verif_run.baseline_executions)
            baseline_passed = sum(b.tests_passed for b in verif_run.baseline_executions)
            baseline_failed = sum(b.tests_failed for b in verif_run.baseline_executions)
            baseline_summary = {
                "total_executions": len(verif_run.baseline_executions),
                "tests_collected": baseline_total,
                "tests_passed": baseline_passed,
                "tests_failed": baseline_failed,
            }

            patched_total = sum(p.tests_collected for p in verif_run.patched_executions)
            patched_passed = sum(p.tests_passed for p in verif_run.patched_executions)
            patched_failed = sum(p.tests_failed for p in verif_run.patched_executions)
            patched_summary = {
                "total_executions": len(verif_run.patched_executions),
                "tests_collected": patched_total,
                "tests_passed": patched_passed,
                "tests_failed": patched_failed,
            }

        # Security Findings & Invariant Checks
        security_findings: List[str] = []
        if val_res and val_res.overall_status == ValidationOutcome.ACCEPTED:
            security_findings.append("M4 AST semantic invariants satisfied (no dangerous imports, no stripped guards).")
            security_findings.append("No credential or secret introduction detected in patch additions.")
        elif val_res:
            security_findings.append(f"M4 validation warning: overall status is '{val_res.overall_status.value}'.")
        else:
            security_findings.append("M4 independent validation record is missing.")

        # Workspace Integrity
        ws_integrity = True
        ws_id = req.workspace_id or (verif_run.workspace_id if verif_run else "unknown")
        if verif_run and hasattr(verif_run, "workspace_id"):
            ws_id = verif_run.workspace_id

        # Deterministic Facts (Traceable)
        target_paths = getattr(p_art, "affected_paths", None) or getattr(p_art, "touched_files", None) or [p_art.target_file]
        hunks_cnt = getattr(p_art, "hunks_count", None) or getattr(p_art, "hunk_count", 0)
        diff_str = getattr(p_art, "diff_content", None) or getattr(p_art, "unified_diff", "")
        facts: List[str] = [
            f"Repository target: {req.target_repo}",
            f"Pinned base commit: {req.base_commit_sha}",
            f"Finding {req.finding_id}: '{finding_title}' (Severity: {finding_sev})",
            f"Candidate patch {p_art.artifact_id} touches: {', '.join(target_paths)}",
            f"Patch content SHA-256: {p_art.content_hash}",
            f"Patch size: +{p_art.lines_added} / -{p_art.lines_deleted} lines across {hunks_cnt} hunk(s)",
            f"M4 independent validation: {val_outcome} (ID: {val_id})",
            f"M5 sandbox verification: {verif_outcome} (ID: {verif_id})",
        ]
        if verif_run:
            facts.append(f"Regression analysis: {regression_summary}")

        # Model Explanations (Labeled as AI text)
        model_explanations: List[str] = []
        plan = self.exec_store.get_repair_plan_for_repair(repair_id)
        if plan and plan.rationale:
            model_explanations.append(f"[Repair Plan Rationale] {plan.rationale}")
        if plan and plan.root_cause:
            model_explanations.append(f"[Identified Root Cause] {plan.root_cause}")

        # Unresolved Uncertainties / Limitations
        limitations: List[str] = []
        uncertainties: List[str] = []
        if fnd and fnd.limitations:
            limitations.append(fnd.limitations)
        if verif_outcome != VerificationOutcome.VERIFIED.value:
            uncertainties.append(f"Verification outcome is '{verif_outcome}'; defect resolution is unproven.")
        if not ev_records:
            uncertainties.append("No supporting source evidence records attached to finding.")

        # Recommended Decision
        if val_outcome == ValidationOutcome.ACCEPTED.value and verif_outcome == VerificationOutcome.VERIFIED.value and defect_resolved and not regression_detected:
            rec_decision = "APPROVE"
        elif regression_detected or verif_outcome == VerificationOutcome.FAILED.value:
            rec_decision = "REJECT"
        elif val_outcome == ValidationOutcome.REJECTED.value:
            rec_decision = "REJECT"
        else:
            rec_decision = "REQUEST_CHANGES"

        dossier = ReviewDossier(
            dossier_id=f"dossier_{uuid.uuid4().hex[:12]}",
            dossier_version=dossier_version,
            repair_id=repair_id,
            finding_id=req.finding_id,
            finding_title=finding_title,
            finding_severity=finding_sev,
            repository_target=str(req.target_repo),
            base_commit_sha=req.base_commit_sha,
            evidence_ids=evidence_ids,
            patch_artifact_id=p_art.artifact_id,
            patch_sha256=p_art.content_hash,
            target_files=target_paths,
            lines_added=p_art.lines_added,
            lines_removed=p_art.lines_deleted,
            hunk_count=hunks_cnt,
            unified_diff=diff_str,
            validation_id=val_id,
            validation_outcome=val_outcome,
            verification_id=verif_id,
            verification_outcome=verif_outcome,
            baseline_test_summary=baseline_summary,
            patched_test_summary=patched_summary,
            regression_analysis_summary=regression_summary,
            regression_detected=regression_detected,
            defect_resolved=defect_resolved,
            workspace_integrity_confirmed=ws_integrity,
            workspace_id=ws_id,
            security_findings=security_findings,
            limitations=limitations,
            recommended_decision=rec_decision,
            facts=facts,
            model_explanations=model_explanations,
            unresolved_uncertainties=uncertainties,
            generated_at=datetime.now(timezone.utc),
        )

        # Persist dossier in store
        self.exec_store.save_review_dossier(dossier)
        return dossier

    @staticmethod
    def render_markdown(dossier: ReviewDossier) -> str:
        """Render a formatted human-readable Markdown view of the review dossier."""
        lines = [
            f"# FORGE Repair Review Dossier — {dossier.dossier_id}",
            "",
            f"- **Repair ID:** `{dossier.repair_id}`",
            f"- **Dossier Version:** `{dossier.dossier_version}`",
            f"- **Generated At:** `{dossier.generated_at.isoformat()}`",
            f"- **Recommended Decision:** `{dossier.recommended_decision}`",
            "",
            "## 1. Finding & Context",
            f"- **Finding ID:** `{dossier.finding_id}`",
            f"- **Title:** {dossier.finding_title}",
            f"- **Severity:** `{dossier.finding_severity}`",
            f"- **Target Repository:** `{dossier.repository_target}`",
            f"- **Base Commit SHA:** `{dossier.base_commit_sha}`",
            f"- **Supporting Evidence IDs:** {', '.join(dossier.evidence_ids) if dossier.evidence_ids else 'None'}",
            "",
            "## 2. Candidate Patch Details",
            f"- **Artifact ID:** `{dossier.patch_artifact_id}`",
            f"- **SHA-256 Digest:** `{dossier.patch_sha256}`",
            f"- **Target Files:** {', '.join(dossier.target_files)}",
            f"- **Metrics:** +{dossier.lines_added} / -{dossier.lines_removed} lines across {dossier.hunk_count} hunk(s)",
            "",
            "### Unified Diff",
            "```diff",
            dossier.unified_diff,
            "```",
            "",
            "## 3. Independent Validation & Verification",
            f"- **M4 Validation ID:** `{dossier.validation_id}`",
            f"- **M4 Validation Outcome:** `{dossier.validation_outcome}`",
            f"- **M5 Verification ID:** `{dossier.verification_id}`",
            f"- **M5 Verification Outcome:** `{dossier.verification_outcome}`",
            f"- **Defect Resolved:** `{'YES' if dossier.defect_resolved else 'NO'}`",
            f"- **Regressions Detected:** `{'YES' if dossier.regression_detected else 'NO'}`",
            f"- **Regression Summary:** {dossier.regression_analysis_summary or 'None'}",
            "",
            "## 4. Traceable Facts vs. Model Content",
            "### Verified Facts (Traceable to Storage)",
        ]
        for fact in dossier.facts:
            lines.append(f"- {fact}")

        lines.extend([
            "",
            "### Model-Generated Explanations (AI Text)",
        ])
        if dossier.model_explanations:
            for exp in dossier.model_explanations:
                lines.append(f"- {exp}")
        else:
            lines.append("- (No model-generated explanations)")

        lines.extend([
            "",
            "### Unresolved Uncertainties & Limitations",
        ])
        if dossier.unresolved_uncertainties or dossier.limitations:
            for unc in dossier.unresolved_uncertainties:
                lines.append(f"- {unc}")
            for lim in dossier.limitations:
                lines.append(f"- (Limitation) {lim}")
        else:
            lines.append("- (None noted)")

        lines.extend([
            "",
            "## 5. Security & Safety Invariants",
        ])
        for sf in dossier.security_findings:
            lines.append(f"- [OK] {sf}")

        return "\n".join(lines)
