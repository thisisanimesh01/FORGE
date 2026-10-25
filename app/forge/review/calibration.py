"""Review finding calibration and lifecycle transition enforcement."""

from typing import List, Optional, Tuple

from forge.analysis.findings import validate_transition, InvalidTransitionError
from forge.review.evidence import (
    CalibratedEvidence,
    EvidenceCalibrationStatus,
)
from forge.review.models import (
    ReviewFinding,
)
from forge.schemas import (
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
    EvidenceRecord,
)


class FindingCalibrationError(Exception):
    """Raised when finding calibration or evidence verification fails."""
    pass


def calibrate_finding_evidence(
    finding: ReviewFinding,
    target_repo_files: Optional[List[str]] = None,
) -> ReviewFinding:
    """Validate and calibrate evidence attached to a review finding.

    Rules:
    - If evidence is missing, mark finding as INSUFFICIENT and downgrade confidence to LOW.
    - If evidence file_path does not match finding file_path, mark MISMATCHED and downgrade confidence to LOW.
    - If contradictory evidence or notes exist, preserve uncertainty note and calibrate status.
    - Model assertions alone cannot elevate lifecycle state above SUSPECTED.
    """
    evidence_list = finding.calibrated_evidence

    if not evidence_list:
        # Missing evidence
        return finding.model_copy(
            update={
                "evidence_status": EvidenceCalibrationStatus.INSUFFICIENT,
                "confidence": FindingConfidence.LOW,
                "uncertainty_note": "No concrete evidence records attached to support this finding.",
            }
        )

    # Check for file path mismatch
    for ev in evidence_list:
        if ev.file_path and finding.file_path != "<commit_message>":
            if ev.file_path != finding.file_path:
                return finding.model_copy(
                    update={
                        "evidence_status": EvidenceCalibrationStatus.MISMATCHED,
                        "confidence": FindingConfidence.LOW,
                        "uncertainty_note": f"Evidence file path '{ev.file_path}' does not match finding '{finding.file_path}'.",
                    }
                )

    # Check for contradictory evidence
    contradictions = [ev for ev in evidence_list if ev.status == EvidenceCalibrationStatus.CONTRADICTORY]
    if contradictions:
        reasons = "; ".join(c.contradiction_reason or "Contradictory evidence observed" for c in contradictions)
        return finding.model_copy(
            update={
                "evidence_status": EvidenceCalibrationStatus.CONTRADICTORY,
                "confidence": FindingConfidence.LOW,
                "uncertainty_note": f"Contradictory evidence: {reasons}",
            }
        )

    return finding


def enforce_review_lifecycle_transition(
    finding: ReviewFinding,
    target_state: FindingLifecycleState,
    evidence: CalibratedEvidence,
) -> ReviewFinding:
    """Validate transition of ReviewFinding to target_state using Phase 1 lifecycle rules.

    Converts ReviewFinding and CalibratedEvidence into Phase 1 Finding and EvidenceRecord,
    then executes validate_transition(). Raises InvalidTransitionError if unauthorized.
    """
    # Build Phase 1 Finding proxy
    p1_finding = Finding(
        finding_id=finding.finding_id,
        title=finding.title,
        description=finding.explanation,
        severity=finding.severity,
        confidence=finding.confidence,
        lifecycle_state=finding.lifecycle_state,
        affected_files=[finding.file_path],
    )

    # Build Phase 1 EvidenceRecord proxy
    p1_evidence = EvidenceRecord(
        evidence_id=evidence.evidence_id,
        finding_id=finding.finding_id,
        evidence_type=evidence.evidence_type,
        description=evidence.description,
        file_path=evidence.file_path,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        content=evidence.content_excerpt,
        details=evidence.details,
        provenance=evidence.provenance,
    )

    # Authorize through core Phase 1 state machine
    validate_transition(p1_finding, target_state, p1_evidence)

    # Update state upon successful verification
    return finding.model_copy(
        update={
            "lifecycle_state": target_state,
            "evidence_status": EvidenceCalibrationStatus.VERIFIED,
        }
    )
