"""Data models for CI review policy evaluation."""

from datetime import datetime, date, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge.schemas import FindingSeverity, FindingConfidence
from forge.review.models import FindingCategory
from forge.review.evidence import EvidenceCalibrationStatus


class PolicyEvidenceHandling(str, Enum):
    """Behavior when evaluating findings with non-verified evidence."""
    FAIL_CLOSED = "fail_closed"  # Treat as policy violation
    IGNORE = "ignore"            # Skip policy evaluation for unverified evidence
    REPORT_ONLY = "report_only"  # Include in report without causing policy failure


class FindingPolicyAction(str, Enum):
    """Outcome action for an individual finding under policy evaluation."""
    PASSED = "passed"
    BLOCKED = "blocked"
    SUPPRESSED = "suppressed"
    SKIPPED = "skipped"


class RuleSuppression(BaseModel):
    """Specification of a narrow, documented rule suppression."""
    model_config = ConfigDict(frozen=True)

    rule_id: str
    file_path: Optional[str] = None
    reason: str = Field(min_length=5, description="Auditable rationale for suppression")
    author: Optional[str] = None
    expires_at: Optional[str] = Field(default=None, description="ISO-8601 expiration timestamp (YYYY-MM-DD)")

    @property
    def is_expired(self) -> bool:
        """Check if suppression has passed its expiration date."""
        if not self.expires_at:
            return False
        try:
            # Parse YYYY-MM-DD or full ISO datetime
            exp_date_str = self.expires_at.split("T")[0]
            exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
            now_date = datetime.now(timezone.utc).date()
            return now_date > exp_date
        except Exception:
            return False


class ReviewPolicy(BaseModel):
    """Declarative CI review enforcement policy."""
    model_config = ConfigDict(frozen=True)

    policy_id: str = "default_security_policy"
    name: str = "Default Security & Defect Policy"
    description: Optional[str] = None

    # Severity & Confidence thresholds
    min_fail_severity: FindingSeverity = FindingSeverity.HIGH
    min_enforce_confidence: FindingConfidence = FindingConfidence.HIGH

    # Category filters
    included_categories: Optional[List[FindingCategory]] = None
    excluded_categories: List[FindingCategory] = Field(default_factory=list)

    # Evidence handling
    evidence_handling: PolicyEvidenceHandling = PolicyEvidenceHandling.REPORT_ONLY

    # Scope
    scope_changed_lines_only: bool = True

    # Suppressions
    suppressions: List[RuleSuppression] = Field(default_factory=list)


class FindingPolicyEvaluation(BaseModel):
    """Detailed policy decision for a single finding."""
    model_config = ConfigDict(frozen=True)

    finding_id: str
    rule_id: str
    file_path: str
    line_number: Optional[int] = None
    action: FindingPolicyAction
    decision_reason: str
    is_blocking: bool
    suppression_applied: Optional[RuleSuppression] = None


class PolicyEvaluationReport(BaseModel):
    """Overall outcome of policy evaluation across all review findings."""
    model_config = ConfigDict(frozen=True)

    policy_id: str
    policy_name: str
    evaluated_at: str
    passed: bool
    total_findings_evaluated: int
    blocking_findings_count: int
    suppressed_findings_count: int
    unbacked_findings_count: int
    decisions: List[FindingPolicyEvaluation] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)
