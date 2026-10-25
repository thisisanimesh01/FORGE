"""Deterministic ID generation and deduplication for review findings."""

import hashlib
from typing import List, Optional

from forge.review.models import ReviewFinding


def generate_finding_id(
    rule_id: str,
    file_path: str,
    line_number: Optional[int],
    code_snippet: Optional[str] = None,
) -> str:
    """Generate a deterministic, content-addressable finding ID.

    Incorporates rule_id, normalized file_path, line_number, and normalized snippet.
    Guarantees stable IDs across identical inputs without random UUIDs.
    """
    norm_path = (file_path or "").strip().replace("\\", "/").lower()
    norm_snippet = (code_snippet or "").strip()[:100]
    raw_key = f"{rule_id}:{norm_path}:{line_number or 0}:{norm_snippet}"
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]
    return f"rev_{rule_id.lower()}_{digest}"


def deduplicate_findings(findings: List[ReviewFinding]) -> List[ReviewFinding]:
    """Deduplicate findings deterministically while preserving distinct defects.

    Two findings are considered duplicates if they share the exact same
    (rule_id, file_path, line_number, code_snippet).
    Nearby defects on different lines or distinct rules on the same line are preserved.
    """
    seen = set()
    deduped: List[ReviewFinding] = []

    for f in findings:
        key = (f.rule_id, f.file_path, f.line_number, (f.code_snippet or "").strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)

    # Sort deterministically by file_path, line_number, rule_id
    deduped.sort(key=lambda x: (x.file_path, x.line_number or 0, x.rule_id))
    return deduped
