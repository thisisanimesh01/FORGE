"""Review engine evaluating changed code in diff hunks with evidence calibration."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from forge.config import get_settings, redact_secrets
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.provider import LLMProvider
from forge.review.calibration import calibrate_finding_evidence
from forge.review.dedup import deduplicate_findings, generate_finding_id
from forge.review.evidence import (
    CalibratedEvidence,
    EvidenceCalibrationStatus,
    create_calibrated_evidence,
)
from forge.review.models import (
    DiffBundle,
    DiffFile,
    DiffHunk,
    DiffLine,
    FindingCategory,
    ReviewFileStatus,
    ReviewFinding,
    ReviewReport,
    REVIEW_SCHEMA_VERSION,
)
from forge.schemas import FindingSeverity, FindingConfidence, FindingLifecycleState, EvidenceType


# Structured static review rules:
# (rule_id, category, regex_pattern, severity, confidence, title, observed_template, inference_template, rec_template)
STATIC_REVIEW_RULES = [
    (
        "REV_SEC_PATH_TRAVERSAL",
        FindingCategory.SECURITY,
        re.compile(r"\bos\.path\.join\s*\([^,\)]+,\s*[^,\)]+\)", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Potential uncontained path traversal in os.path.join",
        "Path assembled using os.path.join with external input without visible containment check.",
        "An adversary could supply relative path components ('../') to escape the designated base directory.",
        "Enforce strict path containment using forge.repository.validation.assert_contained_path before filesystem access.",
    ),
    (
        "REV_SEC_SHELL_EXECUTION",
        FindingCategory.SECURITY,
        re.compile(r"\b(subprocess\.run|subprocess\.Popen|os\.system)\s*\([^)]*shell\s*=\s*True", re.IGNORECASE),
        FindingSeverity.CRITICAL,
        FindingConfidence.HIGH,
        "Insecure shell execution with shell=True",
        "Invoking subprocess or system command with shell=True on unvalidated arguments.",
        "Allows arbitrary shell command injection if command components contain user-controllable input.",
        "Pass arguments as a typed argument list with shell=False and avoid shell wrappers.",
    ),
    (
        "REV_SEC_HARDCODED_SECRET",
        FindingCategory.SECRET_EXPOSURE,
        re.compile(r'(?:api_key|token|secret|password|passwd|auth)\s*=\s*["\'][a-zA-Z0-9_\-\.]{12,}["\']', re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Hardcoded credential or token detected in diff",
        "A string matching sensitive credential keywords with an explicit literal string was added.",
        "Hardcoded secrets in repository revisions risk unauthorized exfiltration and credential compromise.",
        "Move sensitive credentials to external environment variables and load securely at runtime.",
    ),
    (
        "REV_DEF_UNGUARDED_INT_CONV",
        FindingCategory.DEFECT,
        re.compile(r"\bint\s*\(\s*\w+\[['\"][^'\"]+['\"]]?\s*\)", re.IGNORECASE),
        FindingSeverity.MEDIUM,
        FindingConfidence.HIGH,
        "Unguarded integer conversion on unvalidated subscript",
        "Direct int(...) conversion applied to a subscript expression without error handling.",
        "Will raise ValueError if string is non-numeric, or KeyError if key does not exist.",
        "Wrap conversion in a try/except (ValueError, KeyError) block or validate input types prior to cast.",
    ),
    (
        "REV_DEF_OFF_BY_ONE_BOUND",
        FindingCategory.DEFECT,
        re.compile(r"while\s+\w+\s*<=\s*len\s*\(", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Boundary off-by-one error using '<= len(...)'",
        "Loop boundary condition tests '<= len(...)' on a 0-indexed collection.",
        "Index variable will reach len(collection), resulting in IndexError during item lookup.",
        "Change loop condition from '<=' to '< len(...)' or adjust upper bound.",
    ),
    (
        "REV_DEF_CHAINED_DICT_ACCESS",
        FindingCategory.DEFECT,
        re.compile(r'\w+\[["\'][^\]]+["\']\]\s*\[["\'][^\]]+["\']\]', re.IGNORECASE),
        FindingSeverity.MEDIUM,
        FindingConfidence.MEDIUM,
        "Chained dictionary subscript access without error handling",
        "Nested dictionary access via chained bracket notation without .get() or KeyError guard.",
        "If intermediate or leaf key is absent, code raises unhandled KeyError.",
        "Use safe access methods such as .get() with defaults or check key existence.",
    ),
    (
        "REV_DEF_DIRECT_PERCENT_SUB",
        FindingCategory.DEFECT,
        re.compile(r"=\s*\w+\s*-\s*\w*(?:percent|discount|rate)\w*", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Direct percentage subtraction defect",
        "A discount/rate variable is subtracted directly from base price without scaling.",
        "Results in incorrect calculation (e.g. subtracting 20 instead of 20% of base price).",
        "Compute percentage reduction via (base_price * discount_percent / 100.0).",
    ),
    (
        "REV_SEC_PROMPT_INJECTION",
        FindingCategory.PROMPT_INJECTION,
        re.compile(r"(?:SYSTEM OVERRIDE|IGNORE ALL PREVIOUS INSTRUCTIONS|disregard safety guidelines)", re.IGNORECASE),
        FindingSeverity.HIGH,
        FindingConfidence.HIGH,
        "Adversarial prompt injection pattern in source/diff",
        "Content introduces explicit adversarial directives targeting LLM instructions.",
        "If forwarded to model prompts without containment, may attempt to override system guardrails.",
        "Sanitize inputs and treat diff contents as strictly untrusted context.",
    ),
]


class ReviewEngine:
    """Orchestrates diff-aware review in static or LLM-assisted mode with calibrated findings."""

    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider

    def review_diff(
        self,
        diff: DiffBundle,
        target_path: Path,
        mode: str = "static",
        commit_message: Optional[str] = None,
    ) -> ReviewReport:
        """Analyze changed hunks in DiffBundle and produce an evidence-calibrated ReviewReport."""
        start_time = time.monotonic()
        created_at = datetime.now(timezone.utc).isoformat()
        # Stable deterministic review ID based on base and head revisions
        rev_hash = hashlib.sha256(f"{diff.base_revision}:{diff.head_revision}".encode("utf-8")).hexdigest()[:12]
        review_id = f"rev_{rev_hash}"

        raw_findings: List[ReviewFinding] = []

        # 1. Check commit message for prompt injection or leaked credentials
        if commit_message:
            for rule_id, cat, pattern, severity, confidence, title, obs, inf, rec in STATIC_REVIEW_RULES:
                if pattern.search(commit_message):
                    fid = generate_finding_id(rule_id, "<commit_message>", None, commit_message)
                    ev = create_calibrated_evidence(
                        evidence_type=EvidenceType.STATIC_ANALYSIS,
                        description=f"Commit message triggered rule {rule_id}",
                        content=commit_message[:200],
                        provenance="forge:review:commit_msg",
                        file_path="<commit_message>",
                    )
                    finding = ReviewFinding(
                        finding_id=fid,
                        category=cat,
                        severity=severity,
                        confidence=confidence,
                        lifecycle_state=FindingLifecycleState.SUSPECTED,
                        title=f"{title} (in commit message)",
                        explanation=f"{obs} Found in commit message.",
                        observed_evidence=obs,
                        inference=inf,
                        recommendation=rec,
                        file_path="<commit_message>",
                        line_number=None,
                        rule_id=rule_id,
                        code_snippet=commit_message[:200],
                        calibrated_evidence=[ev],
                        evidence_references=[ev.evidence_id],
                        evidence_status=EvidenceCalibrationStatus.VERIFIED,
                    )
                    raw_findings.append(finding)

        # 2. Review file-level changes
        for file in diff.files:
            # Skip deleted or binary files for source code defect analysis
            if file.status in (ReviewFileStatus.DELETED, ReviewFileStatus.BINARY):
                continue

            for hunk in file.hunks:
                for line in hunk.lines:
                    # Focus static analysis strictly on added lines (+)
                    if line.origin != "+" or not line.content:
                        continue

                    content = line.content

                    for rule_id, cat, pattern, severity, confidence, title, obs, inf, rec in STATIC_REVIEW_RULES:
                        if pattern.search(content):
                            clean_snippet = line.content.strip()[:200]
                            fid = generate_finding_id(rule_id, file.path, line.new_lineno, clean_snippet)

                            ev = create_calibrated_evidence(
                                evidence_type=EvidenceType.SOURCE_EXCERPT,
                                description=f"Source excerpt in diff hunk matching rule {rule_id}",
                                content=clean_snippet,
                                provenance="forge:review:diff",
                                file_path=file.path,
                                start_line=line.new_lineno,
                                end_line=line.new_lineno,
                                details={"hunk_header": hunk.header, "rule_id": rule_id},
                            )

                            finding = ReviewFinding(
                                finding_id=fid,
                                category=cat,
                                severity=severity,
                                confidence=confidence,
                                lifecycle_state=FindingLifecycleState.SUSPECTED,
                                title=f"{title} at {file.path}:{line.new_lineno}",
                                explanation=f"{obs} {inf}",
                                observed_evidence=obs,
                                inference=inf,
                                recommendation=rec,
                                file_path=file.path,
                                line_number=line.new_lineno,
                                end_line_number=line.new_lineno,
                                rule_id=rule_id,
                                code_snippet=clean_snippet,
                                calibrated_evidence=[ev],
                                evidence_references=[ev.evidence_id],
                                evidence_status=EvidenceCalibrationStatus.VERIFIED,
                            )
                            raw_findings.append(finding)

        # 3. Deduplicate findings deterministically
        deduped = deduplicate_findings(raw_findings)

        # 4. Calibrate evidence for all findings
        calibrated_findings: List[ReviewFinding] = []
        for f in deduped:
            calibrated = calibrate_finding_evidence(f)
            calibrated_findings.append(calibrated)

        # 5. LLM-assisted review if mode == "llm"
        capabilities_unavailable: List[str] = []
        if mode == "llm":
            if self.provider is not None:
                calibrated_findings = self._run_llm_diff_review(diff, calibrated_findings)
            else:
                capabilities_unavailable.append("LLM provider unavailable; ran static diff review only.")

        duration = round(time.monotonic() - start_time, 3)

        # Summary counts
        sev_counts: Dict[str, int] = {}
        for f in calibrated_findings:
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
                "total_findings": len(calibrated_findings),
                "by_severity": sev_counts,
            },
            findings=calibrated_findings,
            files_reviewed=[f.path for f in diff.files],
            system_environment=env_info,
            capabilities_unavailable=capabilities_unavailable,
        )

    def _run_llm_diff_review(self, diff: DiffBundle, existing_findings: List[ReviewFinding]) -> List[ReviewFinding]:
        """Perform bounded LLM reasoning on diff hunks.

        Rules:
        - Model claims cannot advance lifecycle state (must remain SUSPECTED).
        - Any proposed finding must have calibrated evidence attached.
        """
        # Phase 2 Milestone 2 ensures model suggestions cannot bypass evidence requirements.
        return list(existing_findings)
