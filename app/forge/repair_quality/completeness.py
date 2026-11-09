"""Phase 12 M6: Repair Completeness Analyzer.

Verifies that all M3 RequiredChanges, target files, and required verification
checks were comprehensively addressed in the repair.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
)


class RepairCompletenessAnalyzer:
    """Evaluates whether an applied repair completely addresses the repair plan."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def analyze_completeness(
        self,
        repair_plan: Dict[str, Any],
        modified_files: List[str],
        verification_result: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore]:
        """Analyze completeness of repair against repair plan and verification outcomes."""
        findings: List[str] = []
        normalized_modified = {f.strip("/").replace("\\", "/") for f in modified_files}

        # 1. Check Required Changes from Plan
        required_changes = repair_plan.get("required_changes") or []
        total_required_changes = len(required_changes)
        addressed_changes = 0
        unaddressed_changes: List[str] = []

        for idx, rc in enumerate(required_changes):
            rc_file = rc.get("file_path", "") if isinstance(rc, dict) else getattr(rc, "file_path", "")
            norm_rc_file = rc_file.strip("/").replace("\\", "/")
            if norm_rc_file and norm_rc_file in normalized_modified:
                addressed_changes += 1
            else:
                desc = rc.get("description", f"change_{idx}") if isinstance(rc, dict) else getattr(rc, "description", f"change_{idx}")
                unaddressed_changes.append(f"{norm_rc_file}: {desc}")
                findings.append(f"Unaddressed required change: '{norm_rc_file}' was not modified ({desc})")

        # 2. Check Affected Files
        plan_affected_files = repair_plan.get("affected_files") or []
        missing_affected_files: List[str] = []
        for af in plan_affected_files:
            norm_af = af.strip("/").replace("\\", "/")
            if norm_af not in normalized_modified:
                missing_affected_files.append(norm_af)
                findings.append(f"Plan affected file '{norm_af}' was not modified in the repair")

        # 3. Check Verification Completeness
        verification_completeness_ratio = 1.0
        if verification_result:
            req_completed = verification_result.get("required_checks_completed") or []
            req_failed = verification_result.get("required_checks_failed") or []
            skipped = verification_result.get("skipped_checks") or {}

            total_req_checks = len(req_completed) + len(req_failed)
            if total_req_checks > 0:
                verification_completeness_ratio = len(req_completed) / total_req_checks
            else:
                # If no checks were completed
                verification_completeness_ratio = 0.0
                findings.append("No required verification checks were completed")

            if req_failed:
                findings.append(f"Verification checks failed: {req_failed}")

        # 4. Score Calculation
        score = 1.0
        if total_required_changes > 0:
            change_ratio = addressed_changes / total_required_changes
            score *= change_ratio
        elif not plan_affected_files and not modified_files:
            score = 0.0
            findings.append("No changes specified in plan and no files modified")

        if missing_affected_files:
            score -= min(0.3, 0.1 * len(missing_affected_files))

        if verification_completeness_ratio < self.policy.min_verification_completeness:
            score -= 0.4
            findings.append(
                f"Verification completeness ({verification_completeness_ratio:.2f}) is below policy threshold ({self.policy.min_verification_completeness:.2f})"
            )

        score = max(0.0, min(1.0, score))

        status = (
            QualityStatus.PASSED
            if score >= self.policy.min_dimension_score and not unaddressed_changes
            else QualityStatus.FAILED
        )

        metrics = {
            "total_required_changes": total_required_changes,
            "addressed_changes_count": addressed_changes,
            "unaddressed_changes": unaddressed_changes,
            "missing_affected_files": missing_affected_files,
            "verification_completeness_ratio": verification_completeness_ratio,
        }

        explanation = (
            f"Addressed {addressed_changes}/{total_required_changes} required changes. "
            f"Missing affected files: {len(missing_affected_files)}. "
            f"Verification completeness: {verification_completeness_ratio:.2f}."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.COMPLETENESS,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.COMPLETENESS, 0.20),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=True,
        )

        return metrics, dimension_score
