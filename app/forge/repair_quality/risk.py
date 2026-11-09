"""Phase 12 M6: Security & Risk Analyzer.

Performs static inspection of patch contents and affected files for
security-sensitive patterns, dangerous APIs, dependency additions,
and permission changes. Operates strictly read-only without executing code.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
    RiskSeverity,
)


DANGEROUS_PATTERNS = [
    (r"\b(eval|exec)\s*\(", RiskSeverity.CRITICAL, "Arbitrary code execution primitive (eval/exec)"),
    (r"\b(subprocess|os\.system|os\.popen)\b", RiskSeverity.HIGH, "Process spawning / shell invocation"),
    (r"\bpickle\.(loads?|Unpickler)\b", RiskSeverity.HIGH, "Unsafe object deserialization (pickle)"),
    (r"\bverify\s*=\s*False\b", RiskSeverity.HIGH, "TLS/SSL verification explicitly disabled"),
    (r"\b(password|secret|api_key|token)\s*=\s*['\"][A-Za-z0-9_\-]{8,}['\"]", RiskSeverity.CRITICAL, "Potential hardcoded credential/token"),
    (r"\bshutil\.rmtree\s*\(", RiskSeverity.MEDIUM, "Recursive filesystem deletion"),
    (r"\btempfile\.mktemp\b", RiskSeverity.MEDIUM, "Insecure temporary file creation (mktemp)"),
]

DEPENDENCY_MANIFEST_PATTERNS = [
    r"pyproject\.toml$",
    r"requirements.*\.txt$",
    r"setup\.(py|cfg)$",
]


class RepairRiskAnalyzer:
    """Inspects code modifications for security risks and vulnerabilities."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def analyze_risk(
        self,
        patch_content: str,
        modified_files: List[str],
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore, RiskSeverity]:
        """Analyze security risk level of the proposed repair."""
        findings: List[str] = []
        highest_severity = RiskSeverity.LOW

        # 1. Scan added lines in patch for dangerous patterns
        for line in patch_content.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_code = line[1:]
                for pattern, severity, desc in DANGEROUS_PATTERNS:
                    if re.search(pattern, added_code):
                        findings.append(f"{severity.value.upper()}: {desc} in added code: '{added_code.strip()[:60]}'")
                        highest_severity = self._escalate_severity(highest_severity, severity)

        # 2. Check for dependency additions
        dependency_changes: List[str] = []
        for f in modified_files:
            for d_pattern in DEPENDENCY_MANIFEST_PATTERNS:
                if re.search(d_pattern, f):
                    dependency_changes.append(f)
                    findings.append(f"Dependency manifest modified: '{f}'")
                    highest_severity = self._escalate_severity(highest_severity, RiskSeverity.MEDIUM)

        # 3. Consider maintainability/lint findings from metrics if passed
        if metrics:
            lint_findings = metrics.get("type_lint_findings") or []
            if lint_findings:
                findings.extend(lint_findings)
                highest_severity = self._escalate_severity(highest_severity, RiskSeverity.MEDIUM)

        # 4. Score Calculation based on highest severity
        severity_score_map = {
            RiskSeverity.NEGLIGIBLE: 1.0,
            RiskSeverity.LOW: 1.0,
            RiskSeverity.MEDIUM: 0.70,
            RiskSeverity.HIGH: 0.30,
            RiskSeverity.CRITICAL: 0.0,
        }
        score = severity_score_map[highest_severity]

        status = (
            QualityStatus.PASSED
            if highest_severity in (RiskSeverity.NEGLIGIBLE, RiskSeverity.LOW)
            else (QualityStatus.HIGH_RISK if highest_severity in (RiskSeverity.HIGH, RiskSeverity.CRITICAL) else QualityStatus.FAILED)
        )

        risk_metrics = {
            "risk_severity": highest_severity.value,
            "security_findings_count": len(findings),
            "dependency_manifests_modified": dependency_changes,
        }

        explanation = (
            f"Risk assessed as '{highest_severity.value}'. "
            f"Security findings: {len(findings)}. "
            f"Dependency changes: {len(dependency_changes)}."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.SECURITY_RISK,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.SECURITY_RISK, 0.10),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=True,
        )

        return risk_metrics, dimension_score, highest_severity

    def _escalate_severity(self, current: RiskSeverity, new_sev: RiskSeverity) -> RiskSeverity:
        """Escalate to the higher severity level."""
        severity_rank = {
            RiskSeverity.NEGLIGIBLE: 0,
            RiskSeverity.LOW: 1,
            RiskSeverity.MEDIUM: 2,
            RiskSeverity.HIGH: 3,
            RiskSeverity.CRITICAL: 4,
        }
        if severity_rank[new_sev] > severity_rank[current]:
            return new_sev
        return current
