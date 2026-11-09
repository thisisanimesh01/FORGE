"""Phase 12 M6: Security & Risk Analyzer.

Performs static inspection of patch contents and affected files for
security-sensitive patterns, dangerous APIs, dependency additions,
and permission changes.
Distinguishes actual executable code invocations from comments, documentation,
and harmless string literal references. Operates strictly read-only.
"""

import ast
import re
from typing import Any, Dict, List, Optional, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
    RiskSeverity,
)


DANGEROUS_CALL_PATTERNS = [
    (r"\b(eval|exec)\s*\(", RiskSeverity.CRITICAL, "Arbitrary code execution primitive (eval/exec)"),
    (r"\b(subprocess\.(call|check_call|check_output|run|Popen)|os\.system|os\.popen)\s*\(", RiskSeverity.HIGH, "Process spawning / shell invocation"),
    (r"\bpickle\.(loads?|Unpickler)\s*\(", RiskSeverity.HIGH, "Unsafe object deserialization (pickle)"),
    (r"\bverify\s*=\s*False\b", RiskSeverity.HIGH, "TLS/SSL verification explicitly disabled"),
    (r"\bshutil\.rmtree\s*\(", RiskSeverity.MEDIUM, "Recursive filesystem deletion"),
    (r"\btempfile\.mktemp\s*\(", RiskSeverity.MEDIUM, "Insecure temporary file creation (mktemp)"),
]

CREDENTIAL_PATTERN = re.compile(
    r"\b(password|secret|api_key|token)\s*=\s*['\"]([A-Za-z0-9_\-]{8,})['\"]",
    re.IGNORECASE,
)

SAFE_CREDENTIAL_SUBSTRINGS = [
    "test", "dummy", "fake", "example", "redacted", "placeholder", "mock", "sample", "default"
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
                raw_added = line[1:]
                stripped_added = raw_added.strip()

                # Skip comment-only lines and empty lines
                if not stripped_added or stripped_added.startswith("#"):
                    continue

                # Strip comments and string literals to get executable code context
                executable_code = self._strip_comments_and_strings(raw_added)

                # Check executable call patterns against cleaned code
                for pattern, severity, desc in DANGEROUS_CALL_PATTERNS:
                    if re.search(pattern, executable_code):
                        findings.append(
                            f"{severity.value.upper()}: {desc} in added code: '{stripped_added[:60]}'"
                        )
                        highest_severity = self._escalate_severity(highest_severity, severity)

                # Check credentials in raw line (excluding safe/test placeholders)
                cred_match = CREDENTIAL_PATTERN.search(raw_added)
                if cred_match:
                    var_name, val = cred_match.group(1), cred_match.group(2)
                    is_safe_placeholder = any(s in val.lower() for s in SAFE_CREDENTIAL_SUBSTRINGS)
                    if not is_safe_placeholder and not stripped_added.startswith("#"):
                        findings.append(
                            f"CRITICAL: Potential hardcoded credential/token in variable '{var_name}'"
                        )
                        highest_severity = self._escalate_severity(highest_severity, RiskSeverity.CRITICAL)

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

    def _strip_comments_and_strings(self, line: str) -> str:
        """Strip comments and string literal contents from a Python code line.

        This prevents false positives where dangerous keywords (e.g. 'eval',
        'subprocess') appear in comments, docstrings, or logging strings.
        """
        # First remove string contents: replace "..." and '...' with ""
        # Regex matches double-quoted strings and single-quoted strings
        no_strings = re.sub(r'("[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')', '""', line)

        # Then strip inline comments (from unquoted # to end of line)
        if "#" in no_strings:
            no_strings = no_strings.split("#", 1)[0]

        return no_strings

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
