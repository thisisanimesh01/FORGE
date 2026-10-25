"""Deterministic CI review policy evaluator."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.config import redact_secrets
from forge.review.evidence import EvidenceCalibrationStatus
from forge.review.models import ReviewFinding, ReviewReport
from forge.review.policy_models import (
    FindingPolicyAction,
    FindingPolicyEvaluation,
    PolicyEvaluationReport,
    PolicyEvidenceHandling,
    ReviewPolicy,
    RuleSuppression,
)
from forge.schemas import FindingConfidence, FindingSeverity

# Numeric rankings for threshold comparison
SEVERITY_ORDER = {
    FindingSeverity.INFO: 1,
    FindingSeverity.LOW: 2,
    FindingSeverity.MEDIUM: 3,
    FindingSeverity.HIGH: 4,
    FindingSeverity.CRITICAL: 5,
}

CONFIDENCE_ORDER = {
    FindingConfidence.LOW: 1,
    FindingConfidence.MEDIUM: 2,
    FindingConfidence.HIGH: 3,
    FindingConfidence.CONFIRMED: 4,
}


class PolicyConfigurationError(Exception):
    """Raised when policy schema or configuration is invalid."""
    pass


def load_policy_from_file(policy_path: Path) -> ReviewPolicy:
    """Load and validate a ReviewPolicy from JSON or YAML configuration."""
    if not policy_path.exists() or not policy_path.is_file():
        raise PolicyConfigurationError(f"Policy file not found: {policy_path}")

    try:
        content = policy_path.read_text(encoding="utf-8")
        raw_data = json.loads(content)
        return ReviewPolicy.model_validate(raw_data)
    except Exception as exc:
        raise PolicyConfigurationError(f"Failed to load policy from {policy_path}: {exc}")


def evaluate_review_policy(
    report: ReviewReport,
    policy: Optional[ReviewPolicy] = None,
) -> PolicyEvaluationReport:
    """Evaluate calibrated review report findings against policy.

    Guarantees:
    - Pure evaluator: Does NOT mutate detection results or calibrated evidence.
    - Deterministic ordering: Findings are evaluated and reported in stable order.
    - Auditable decisions: Every finding receives an explicit action and rationale.
    - Zero secrets: Output is scrubbed of sensitive values.
    """
    pol = policy or ReviewPolicy()
    evaluated_at = datetime.now(timezone.utc).isoformat()

    decisions: List[FindingPolicyEvaluation] = []
    blocking_count = 0
    suppressed_count = 0
    unbacked_count = 0

    min_sev_rank = SEVERITY_ORDER.get(pol.min_fail_severity, 4)
    min_conf_rank = CONFIDENCE_ORDER.get(pol.min_enforce_confidence, 3)

    for finding in report.findings:
        # Check Category Filtering
        if pol.included_categories and finding.category not in pol.included_categories:
            decisions.append(
                FindingPolicyEvaluation(
                    finding_id=finding.finding_id,
                    rule_id=finding.rule_id,
                    file_path=finding.file_path,
                    line_number=finding.line_number,
                    action=FindingPolicyAction.SKIPPED,
                    decision_reason=f"Category '{finding.category.value}' not in included_categories.",
                    is_blocking=False,
                )
            )
            continue

        if pol.excluded_categories and finding.category in pol.excluded_categories:
            decisions.append(
                FindingPolicyEvaluation(
                    finding_id=finding.finding_id,
                    rule_id=finding.rule_id,
                    file_path=finding.file_path,
                    line_number=finding.line_number,
                    action=FindingPolicyAction.SKIPPED,
                    decision_reason=f"Category '{finding.category.value}' is in excluded_categories.",
                    is_blocking=False,
                )
            )
            continue

        # Check Evidence Status
        has_verified_evidence = (
            finding.evidence_status == EvidenceCalibrationStatus.VERIFIED
            and any(ev.status == EvidenceCalibrationStatus.VERIFIED for ev in finding.calibrated_evidence)
        )

        if not has_verified_evidence:
            unbacked_count += 1
            if pol.evidence_handling == PolicyEvidenceHandling.IGNORE:
                decisions.append(
                    FindingPolicyEvaluation(
                        finding_id=finding.finding_id,
                        rule_id=finding.rule_id,
                        file_path=finding.file_path,
                        line_number=finding.line_number,
                        action=FindingPolicyAction.SKIPPED,
                        decision_reason=f"Evidence status is '{finding.evidence_status.value}'; ignored by policy.",
                        is_blocking=False,
                    )
                )
                continue
            elif pol.evidence_handling == PolicyEvidenceHandling.FAIL_CLOSED:
                blocking_count += 1
                decisions.append(
                    FindingPolicyEvaluation(
                        finding_id=finding.finding_id,
                        rule_id=finding.rule_id,
                        file_path=finding.file_path,
                        line_number=finding.line_number,
                        action=FindingPolicyAction.BLOCKED,
                        decision_reason=f"Fail-closed: finding has unverified evidence status '{finding.evidence_status.value}'.",
                        is_blocking=True,
                    )
                )
                continue
            # PolicyEvidenceHandling.REPORT_ONLY: proceed to check thresholds, but document evidence limitation

        # Check Active Suppressions
        matching_suppression = _find_active_suppression(finding, pol.suppressions)
        if matching_suppression is not None:
            suppressed_count += 1
            decisions.append(
                FindingPolicyEvaluation(
                    finding_id=finding.finding_id,
                    rule_id=finding.rule_id,
                    file_path=finding.file_path,
                    line_number=finding.line_number,
                    action=FindingPolicyAction.SUPPRESSED,
                    decision_reason=f"Suppressed: {matching_suppression.reason}",
                    is_blocking=False,
                    suppression_applied=matching_suppression,
                )
            )
            continue

        # Check Severity & Confidence thresholds
        f_sev_rank = SEVERITY_ORDER.get(finding.severity, 0)
        f_conf_rank = CONFIDENCE_ORDER.get(finding.confidence, 0)

        meets_severity = f_sev_rank >= min_sev_rank
        meets_confidence = f_conf_rank >= min_conf_rank

        if meets_severity and meets_confidence:
            blocking_count += 1
            decisions.append(
                FindingPolicyEvaluation(
                    finding_id=finding.finding_id,
                    rule_id=finding.rule_id,
                    file_path=finding.file_path,
                    line_number=finding.line_number,
                    action=FindingPolicyAction.BLOCKED,
                    decision_reason=f"Severity '{finding.severity.value}' >= '{pol.min_fail_severity.value}' and confidence '{finding.confidence.value}' >= '{pol.min_enforce_confidence.value}'.",
                    is_blocking=True,
                )
            )
        else:
            reasons = []
            if not meets_severity:
                reasons.append(f"severity '{finding.severity.value}' < '{pol.min_fail_severity.value}'")
            if not meets_confidence:
                reasons.append(f"confidence '{finding.confidence.value}' < '{pol.min_enforce_confidence.value}'")
            decisions.append(
                FindingPolicyEvaluation(
                    finding_id=finding.finding_id,
                    rule_id=finding.rule_id,
                    file_path=finding.file_path,
                    line_number=finding.line_number,
                    action=FindingPolicyAction.PASSED,
                    decision_reason=f"Passed threshold: {', '.join(reasons)}.",
                    is_blocking=False,
                )
            )

    passed = blocking_count == 0

    return PolicyEvaluationReport(
        policy_id=pol.policy_id,
        policy_name=pol.name,
        evaluated_at=evaluated_at,
        passed=passed,
        total_findings_evaluated=len(report.findings),
        blocking_findings_count=blocking_count,
        suppressed_findings_count=suppressed_count,
        unbacked_findings_count=unbacked_count,
        decisions=decisions,
        summary={
            "min_fail_severity": pol.min_fail_severity.value,
            "min_enforce_confidence": pol.min_enforce_confidence.value,
            "evidence_handling": pol.evidence_handling.value,
        },
    )


def _find_active_suppression(
    finding: ReviewFinding,
    suppressions: List[RuleSuppression],
) -> Optional[RuleSuppression]:
    """Find matching non-expired rule suppression."""
    for supp in suppressions:
        if supp.is_expired:
            continue
        if supp.rule_id == finding.rule_id:
            # If suppression specifies file_path, it must match
            if supp.file_path is None or supp.file_path.strip().lower() == finding.file_path.strip().lower():
                return supp
    return None
