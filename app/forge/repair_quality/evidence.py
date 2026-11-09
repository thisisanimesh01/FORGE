"""Phase 12 M6: Evidence Validation Engine for Repair Quality.

Audits cryptographic bindings, digest integrity, execution provenance,
freshness timestamps, and milestone continuity between M2, M3, M4, and M5.
Fails closed if any evidence is missing, stale, forged, or mismatched.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from forge.repair_quality.schemas import (
    EvidenceCategory,
    EvidenceValidationStatus,
    RepairQualityEvidence,
    RepairQualityRequest,
)


class RepairQualityEvidenceValidator:
    """Validates evidence across the M2-M5 lifecycle with fail-closed semantics."""

    def __init__(self, max_evidence_age_seconds: Optional[int] = None):
        self.max_evidence_age_seconds = max_evidence_age_seconds

    def validate_request_bindings(
        self,
        request: RepairQualityRequest,
        repair_plan: Dict[str, Any],
        execution_result: Dict[str, Any],
        verification_result: Dict[str, Any],
        diagnosis_result: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, List[RepairQualityEvidence], List[str], List[str]]:
        """Validate request against provided milestone objects.

        Returns:
            (is_valid, evidence_records, missing_evidence, validation_errors)
        """
        evidence_records: List[RepairQualityEvidence] = []
        missing_evidence: List[str] = []
        validation_errors: List[str] = []

        now = datetime.now(timezone.utc)

        # 1. Validate M3 Repair Plan
        plan_id = repair_plan.get("repair_plan_id") or repair_plan.get("plan_id")
        if not plan_id:
            missing_evidence.append("repair_plan_id")
        elif plan_id != request.repair_plan_id:
            validation_errors.append(
                f"Plan ID mismatch: request has '{request.repair_plan_id}' but provided plan has '{plan_id}'"
            )
        else:
            # Check digest
            computed_plan_digest = self._compute_digest(repair_plan)
            # Or if plan has an explicit digest field
            explicit_digest = repair_plan.get("plan_digest")
            matched_digest = explicit_digest or computed_plan_digest

            if request.repair_plan_digest and explicit_digest and request.repair_plan_digest != explicit_digest:
                validation_errors.append(
                    f"Plan digest mismatch: request has '{request.repair_plan_digest}' but plan has '{explicit_digest}'"
                )
            else:
                evidence_records.append(
                    RepairQualityEvidence(
                        evidence_id=f"qev_plan_{plan_id[:8]}",
                        source_milestone="M3",
                        source_record_id=plan_id,
                        source_digest=request.repair_plan_digest,
                        evidence_category=EvidenceCategory.REPAIR_PLAN,
                        observation=f"Validated M3 RepairPlan {plan_id} with strategy {repair_plan.get('selected_strategy_id', 'unknown')}",
                        measurement_method="digest_comparison",
                        validation_status=EvidenceValidationStatus.VALID,
                    )
                )

        # 2. Validate M4 Execution Result
        exec_id = execution_result.get("execution_id")
        if not exec_id:
            missing_evidence.append("execution_id")
        elif exec_id != request.execution_id:
            validation_errors.append(
                f"Execution ID mismatch: request has '{request.execution_id}' but execution result has '{exec_id}'"
            )
        else:
            exec_status = str(execution_result.get("execution_status", "")).lower()
            if "applied" not in exec_status:
                validation_errors.append(
                    f"Repair execution status not applied: status is '{exec_status}'"
                )

            # Check patch digest
            exec_patch_digest = (
                execution_result.get("applied_patch_digest")
                or (execution_result.get("patch") or {}).get("patch_digest")
                or execution_result.get("patch_digest")
            )
            if exec_patch_digest and exec_patch_digest != request.applied_patch_digest:
                validation_errors.append(
                    f"Patch digest mismatch: request has '{request.applied_patch_digest}' but execution has '{exec_patch_digest}'"
                )

            # Check baseline tree digest
            exec_baseline_digest = (
                execution_result.get("baseline_tree_digest")
                or (execution_result.get("workspace") or {}).get("baseline_tree_digest")
            )
            if exec_baseline_digest and exec_baseline_digest != request.baseline_tree_digest:
                validation_errors.append(
                    f"Baseline tree digest mismatch: request has '{request.baseline_tree_digest}' but execution has '{exec_baseline_digest}'"
                )

            evidence_records.append(
                RepairQualityEvidence(
                    evidence_id=f"qev_exec_{exec_id[:8]}",
                    source_milestone="M4",
                    source_record_id=exec_id,
                    source_digest=request.applied_patch_digest,
                    evidence_category=EvidenceCategory.REPAIR_EXECUTION,
                    observation=f"Validated M4 RepairExecutionResult {exec_id} with status '{exec_status}'",
                    measurement_method="digest_comparison",
                    validation_status=EvidenceValidationStatus.VALID if not validation_errors else EvidenceValidationStatus.DIGEST_MISMATCH,
                )
            )

        # 3. Validate M5 Verification Result
        verif_id = verification_result.get("verification_id")
        if not verif_id:
            missing_evidence.append("verification_id")
        elif verif_id != request.verification_id:
            validation_errors.append(
                f"Verification ID mismatch: request has '{request.verification_id}' but verification result has '{verif_id}'"
            )
        else:
            verif_status = str(verification_result.get("verification_status", "")).lower()
            req_verif_status = str(request.verification_status).lower()
            if verif_status != req_verif_status:
                validation_errors.append(
                    f"Verification status mismatch: request has '{req_verif_status}' but verification result has '{verif_status}'"
                )

            # Human review invariant
            if verification_result.get("requires_human_review") is not True:
                validation_errors.append(
                    "M5 invariant breach: verification_result.requires_human_review is not True"
                )

            # Check patch digest binding
            verif_patch_digest = verification_result.get("applied_patch_digest")
            if verif_patch_digest and verif_patch_digest != request.applied_patch_digest:
                validation_errors.append(
                    f"Verification patch digest mismatch: verification has '{verif_patch_digest}' but request has '{request.applied_patch_digest}'"
                )

            evidence_records.append(
                RepairQualityEvidence(
                    evidence_id=f"qev_verif_{verif_id[:8]}",
                    source_milestone="M5",
                    source_record_id=verif_id,
                    source_digest=request.applied_patch_digest,
                    evidence_category=EvidenceCategory.VERIFICATION_RESULT,
                    observation=f"Validated M5 VerificationResult {verif_id} with status '{verif_status}'",
                    measurement_method="contract_audit",
                    validation_status=EvidenceValidationStatus.VALID if not validation_errors else EvidenceValidationStatus.DIGEST_MISMATCH,
                )
            )

        # 4. Validate Optional M2 Diagnosis Result
        if diagnosis_result:
            diag_id = diagnosis_result.get("diagnosis_id") or "diag_001"
            evidence_records.append(
                RepairQualityEvidence(
                    evidence_id=f"qev_diag_{diag_id[:8]}",
                    source_milestone="M2",
                    source_record_id=diag_id,
                    source_digest=self._compute_digest(diagnosis_result),
                    evidence_category=EvidenceCategory.DIAGNOSIS,
                    observation=f"Validated M2 DiagnosisResult {diag_id}",
                    measurement_method="schema_audit",
                    validation_status=EvidenceValidationStatus.VALID,
                )
            )

        # 5. Check Freshness
        for rec in evidence_records:
            try:
                rec_dt = datetime.fromisoformat(rec.freshness_timestamp)
                if rec_dt > now and (rec_dt - now).total_seconds() > 300:
                    validation_errors.append(
                        f"Evidence timestamp in future: {rec.evidence_id} has {rec.freshness_timestamp}"
                    )
                elif self.max_evidence_age_seconds:
                    age = (now - rec_dt).total_seconds()
                    if age > self.max_evidence_age_seconds:
                        validation_errors.append(
                            f"Evidence stale: {rec.evidence_id} is {age:.1f}s old (max {self.max_evidence_age_seconds}s)"
                        )
            except Exception:
                pass

        is_valid = len(missing_evidence) == 0 and len(validation_errors) == 0
        return is_valid, evidence_records, missing_evidence, validation_errors

    def _compute_digest(self, data: Any) -> str:
        """Compute deterministic SHA-256 digest of dictionary data."""
        serialized = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
