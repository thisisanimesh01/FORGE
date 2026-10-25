"""FORGE Review Package: Diff-Aware Intelligent Code Review."""

from forge.review.calibration import (
    calibrate_finding_evidence,
    enforce_review_lifecycle_transition,
    FindingCalibrationError,
)
from forge.review.dedup import (
    deduplicate_findings,
    generate_finding_id,
)
from forge.review.engine import ReviewEngine
from forge.review.evidence import (
    CalibratedEvidence,
    EvidenceCalibrationStatus,
    create_calibrated_evidence,
    compute_content_hash,
)
from forge.review.git import read_git_diff, validate_revision, GitDiffError
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
from forge.review.parser import parse_unified_diff
from forge.review.reporting import (
    export_review_report,
    review_report_to_json,
    review_report_to_markdown,
)

__all__ = [
    "CalibratedEvidence",
    "DiffBundle",
    "DiffFile",
    "DiffHunk",
    "DiffLine",
    "EvidenceCalibrationStatus",
    "FindingCalibrationError",
    "FindingCategory",
    "GitDiffError",
    "ReviewEngine",
    "ReviewFileStatus",
    "ReviewFinding",
    "ReviewReport",
    "REVIEW_SCHEMA_VERSION",
    "calibrate_finding_evidence",
    "compute_content_hash",
    "create_calibrated_evidence",
    "deduplicate_findings",
    "enforce_review_lifecycle_transition",
    "export_review_report",
    "generate_finding_id",
    "parse_unified_diff",
    "read_git_diff",
    "review_report_to_json",
    "review_report_to_markdown",
    "validate_revision",
]
