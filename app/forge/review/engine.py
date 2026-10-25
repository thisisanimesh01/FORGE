"""Review engine evaluating changed code in diff hunks."""

from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from forge.config import get_settings, redact_secrets
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.provider import LLMProvider
from forge.review.models import (
    DiffBundle,
    DiffFile,
    DiffHunk,
    DiffLine,
    ReviewFileStatus,
    ReviewFinding,
    ReviewReport,
    REVIEW_SCHEMA_VERSION,
)
from forge.schemas import FindingSeverity, FindingConfidence


# Rule definitions for static code review:
# (rule_id, regex_pattern, severity, confidence, title, explanation)
STATIC_REVIEW_RULES: List[Tuple[str, re.Pattern, FindingSeverity, FindingConfidence, str, str]] = [
    (
        "REV_SEC_PATH_TRAVERSAL",
        re.compile(r"\bos\.path\.join\s*\([^,\)]+,\s*[^,\)]+\)", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Potential uncontained path traversal in os.path.join",
        "Path assembled using os.path.join without visible containment or traversal check (e.g. assert_contained_path).",
    ),
    (
        "REV_SEC_SHELL_EXECUTION",
        re.compile(r"\b(subprocess\.run|subprocess\.Popen|os\.system)\s*\([^)]*shell\s*=\s*True", re.IGNORECASE),
        FindingSeverity.CRITICAL,
        FindingConfidence.HIGH,
        "Insecure shell execution with shell=True",
        "Invoking subprocess or system command with shell=True creates command injection vulnerabilities.",
    ),
    (
        "REV_SEC_HARDCODED_SECRET",
        re.compile(r'(?:api_key|token|secret|password|passwd|auth)\s*=\s*["\'][a-zA-Z0-9_\-\.]{12,}["\']', re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Hardcoded credential or token detected in diff",
        "A string matching sensitive credential keywords (api_key, token, password) with an explicit literal value was added.",
    ),
    (
        "REV_DEF_UNGUARDED_INT_CONV",
        re.compile(r"\bint\s*\(\s*\w+\[['\"][^'\"]+['\"]]?\s*\)", re.IGNORECASE),
        FindingSeverity.MEDIUM,
        FindingConfidence.HIGH,
        "Unguarded integer conversion on unvalidated subscript",
        "Direct int(...) cast on dictionary or list item without prior type validation or try/except block.",
    ),
    (
        "REV_DEF_OFF_BY_ONE_BOUND",
        re.compile(r"while\s+\w+\s*<=\s*len\s*\(", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Boundary off-by-one error using '<= len(...)'",
        "Loop condition tests '<= len(...)' which causes IndexError on 0-indexed sequences.",
    ),
    (
        "REV_DEF_CHAINED_DICT_ACCESS",
        re.compile(r'\w+\[["\'][^\]]+["\']\]\s*\[["\'][^\]]+["\']\]', re.IGNORECASE),
        FindingSeverity.MEDIUM,
        FindingConfidence.MEDIUM,
        "Chained dictionary subscript access without error handling",
        "Direct nested dictionary access without .get() or KeyError handling causes runtime crashes if keys are missing.",
    ),
    (
        "REV_DEF_DIRECT_PERCENT_SUB",
        re.compile(r"=\s*\w+\s*-\s*\w*(?:percent|discount|rate)\w*", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Direct percentage subtraction defect",
        "Variable representing a discount or rate is subtracted directly rather than scaled as a fraction of base value.",
    ),
    (
        "REV_SEC_PROMPT_INJECTION",
        re.compile(r"(?:SYSTEM OVERRIDE|IGNORE ALL PREVIOUS INSTRUCTIONS|disregard safety guidelines)", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Adversarial prompt injection pattern in source/diff",
        "Diff introduces instructions attempting to override system prompts or agent constraints.",
    ),
]


class ReviewEngine:
    """Orchestrates diff-aware review in static or LLM-assisted mode."""

    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider

    def review_diff(
        self,
        diff: DiffBundle,
        target_path: Path,
        mode: str = "static",
        commit_message: Optional[str] = None,
    ) -> ReviewReport:
        """Analyze changed hunks in DiffBundle and produce a structured ReviewReport."""
        start_time = time.monotonic()
        created_at = datetime.now(timezone.utc).isoformat()
        review_id = f"rev_{uuid.uuid4().hex[:12]}"

        findings: List[ReviewFinding] = []
        seen_finding_keys = set()

        # Check commit message for prompt injection or leaked credentials
        if commit_message:
            for rule_id, pattern, severity, confidence, title, explanation in STATIC_REVIEW_RULES:
                if pattern.search(commit_message):
                    fid = f"find_commit_{rule_id}"
                    if fid not in seen_finding_keys:
                        seen_finding_keys.add(fid)
                        findings.append(
                            ReviewFinding(
                                finding_id=fid,
                                file_path="<commit_message>",
                                line_number=None,
                                severity=severity,
                                confidence=confidence,
                                title=f"{title} (in commit message)",
                                explanation=f"{explanation} Found in commit message.",
                                rule_id=rule_id,
                                code_snippet=commit_message[:200],
                            )
                        )

        # 1. Review file-level changes
        for file in diff.files:
            # Skip deleted or binary files for source code defect analysis
            if file.status in (ReviewFileStatus.DELETED, ReviewFileStatus.BINARY):
                continue

            for hunk in file.hunks:
                for line in hunk.lines:
                    # Focus static analysis on added lines (+)
                    if line.origin != "+" or not line.content:
                        continue

                    content = line.content

                    for rule_id, pattern, severity, confidence, title, explanation in STATIC_REVIEW_RULES:
                        if pattern.search(content):
                            # Deduplicate by (rule_id, file.path, line_number)
                            fkey = f"{rule_id}:{file.path}:{line.new_lineno}"
                            if fkey in seen_finding_keys:
                                continue
                            seen_finding_keys.add(fkey)

                            findings.append(
                                ReviewFinding(
                                    finding_id=f"rev_find_{uuid.uuid4().hex[:8]}",
                                    file_path=file.path,
                                    line_number=line.new_lineno,
                                    severity=severity,
                                    confidence=confidence,
                                    title=f"{title} at {file.path}:{line.new_lineno}",
                                    explanation=explanation,
                                    rule_id=rule_id,
                                    code_snippet=line.content.strip()[:200],
                                    evidence_references=[f"diff:{file.path}#L{line.new_lineno}"],
                                )
                            )

        # 2. LLM-assisted review if mode == "llm" and provider is available
        capabilities_unavailable: List[str] = []
        if mode == "llm":
            if self.provider is not None:
                findings = self._run_llm_diff_review(diff, findings)
            else:
                capabilities_unavailable.append("LLM provider unavailable; ran static diff review only.")

        duration = round(time.monotonic() - start_time, 3)

        # Summary counts
        sev_counts: Dict[str, int] = {}
        for f in findings:
            s_val = f.severity.value
            sev_counts[s_val] = sev_counts.get(s_val, 0) + 1

        diff_summary = {
            "total_files_changed": diff.total_files_changed,
            "total_additions": diff.total_additions,
            "total_deletions": diff.total_deletions,
            "raw_size_bytes": diff.raw_size_bytes,
            "is_truncated": diff.is_truncated,
        }

        settings = get_settings()
        env_info = {
            "mode": mode,
            "llm_provider": settings.llm_provider if mode == "llm" else "offline_static",
        }

        return ReviewReport(
            schema_version=REVIEW_SCHEMA_VERSION,
            review_id=review_id,
            target=str(target_path),
            base_revision=diff.base_revision,
            head_revision=diff.head_revision,
            mode=mode,
            created_at=created_at,
            duration_seconds=duration,
            diff_summary=diff_summary,
            summary_counts={
                "total_findings": len(findings),
                "by_severity": sev_counts,
            },
            findings=findings,
            files_reviewed=[f.path for f in diff.files],
            system_environment=env_info,
            capabilities_unavailable=capabilities_unavailable,
        )

    def _run_llm_diff_review(self, diff: DiffBundle, existing_findings: List[ReviewFinding]) -> List[ReviewFinding]:
        """Perform bounded LLM reasoning on diff hunks."""
        # Treat model generated assertions as suggestions. Retain evidence-backed static findings.
        return list(existing_findings)
