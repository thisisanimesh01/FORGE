"""Phase 12 M6: Patch Quality Analyzer.

Evaluates patch proportionality, scope alignment, line additions/removals,
change concentration, unnecessary modifications, and protected path checks.
Strictly read-only; does not modify or re-apply patches.
"""

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
)


PROTECTED_PATH_PATTERNS = [
    r"^\.git/",
    r"^AGENTS\.md$",
    r"^\.env",
    r".*\.pem$",
    r".*\.key$",
    r"id_rsa",
]


class RepairPatchAnalyzer:
    """Analyzes unified diffs and patches for engineering quality and safety."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def analyze_patch(
        self,
        patch_content: str,
        approved_files: List[str],
        repair_plan_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore]:
        """Analyze patch content against approved file list and policy thresholds."""
        findings: List[str] = []
        unnecessary_indicators: List[str] = []

        files_modified: Set[str] = set()
        file_change_counts: Dict[str, int] = {}
        lines_added = 0
        lines_removed = 0

        current_file: Optional[str] = None
        current_added_lines: List[str] = []
        current_removed_lines: List[str] = []

        # Parse diff
        for line in patch_content.splitlines():
            if line.startswith("--- a/"):
                old_path = line[6:].strip()
            elif line.startswith("+++ b/"):
                current_file = line[6:].strip()
                files_modified.add(current_file)
                if current_file not in file_change_counts:
                    file_change_counts[current_file] = 0
            elif line.startswith("@@"):
                # Hunk header
                pass
            elif line.startswith("+") and not line.startswith("+++"):
                lines_added += 1
                if current_file:
                    file_change_counts[current_file] += 1
                current_added_lines.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                lines_removed += 1
                if current_file:
                    file_change_counts[current_file] += 1
                current_removed_lines.append(line[1:])

        total_lines = lines_added + lines_removed
        files_count = len(files_modified)

        # Detect unnecessary changes (whitespace-only or comment-only changes)
        for added in current_added_lines:
            stripped = added.strip()
            if not stripped:
                unnecessary_indicators.append("empty_whitespace_addition")
            elif stripped.startswith("#") and len(stripped) > 200:
                unnecessary_indicators.append("excessive_commentary")

        # Change concentration: max changes in one file / total changes
        if total_lines > 0 and file_change_counts:
            max_in_one = max(file_change_counts.values())
            change_concentration = max_in_one / total_lines
        else:
            change_concentration = 1.0

        # Scope alignment & protected path checks
        normalized_approved = {p.strip("/").replace("\\", "/") for p in approved_files}
        scope_expansion_files: List[str] = []
        protected_path_touches: List[str] = []

        for f in files_modified:
            norm_f = f.strip("/").replace("\\", "/")
            if normalized_approved and norm_f not in normalized_approved:
                scope_expansion_files.append(f)
                findings.append(f"Scope expansion: file '{f}' was modified but not in approved files")

            for pattern in PROTECTED_PATH_PATTERNS:
                if re.search(pattern, f):
                    protected_path_touches.append(f)
                    findings.append(f"Protected path violation: patch modifies protected file '{f}'")

        # Threshold checks
        if total_lines > self.policy.max_patch_lines:
            findings.append(
                f"Patch size {total_lines} lines exceeds policy limit of {self.policy.max_patch_lines}"
            )
        if files_count > self.policy.max_files_changed:
            findings.append(
                f"Modified files count {files_count} exceeds policy limit of {self.policy.max_files_changed}"
            )

        # Score calculation
        score = 1.0
        if protected_path_touches:
            score = 0.0
        else:
            if scope_expansion_files:
                score -= min(0.4, 0.2 * len(scope_expansion_files))
            if total_lines > self.policy.max_patch_lines:
                score -= 0.3
            if files_count > self.policy.max_files_changed:
                score -= 0.2
            if len(unnecessary_indicators) > 10:
                score -= 0.1

        score = max(0.0, min(1.0, score))

        status = (
            QualityStatus.PASSED
            if score >= self.policy.min_dimension_score and not protected_path_touches and not scope_expansion_files
            else QualityStatus.FAILED
        )

        metrics = {
            "patch_lines_added": lines_added,
            "patch_lines_removed": lines_removed,
            "patch_total_lines": total_lines,
            "files_changed_count": files_count,
            "change_concentration": round(change_concentration, 4),
            "unnecessary_change_indicators": unnecessary_indicators[:20],
            "scope_expansion_files": scope_expansion_files,
            "protected_path_touches": protected_path_touches,
        }

        explanation = (
            f"Patch modifies {files_count} file(s) (+{lines_added}/-{lines_removed} lines). "
            f"Concentration: {change_concentration:.2f}. "
            f"Findings: {len(findings)}."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.PATCH_QUALITY,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.PATCH_QUALITY, 0.15),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=True,
        )

        return metrics, dimension_score
