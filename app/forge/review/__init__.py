"""FORGE Review Package: Diff-Aware Intelligent Code Review."""

from forge.review.engine import ReviewEngine
from forge.review.git import read_git_diff, validate_revision, GitDiffError
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
from forge.review.parser import parse_unified_diff
from forge.review.reporting import (
    export_review_report,
    review_report_to_json,
    review_report_to_markdown,
)

__all__ = [
    "DiffBundle",
    "DiffFile",
    "DiffHunk",
    "DiffLine",
    "GitDiffError",
    "ReviewEngine",
    "ReviewFileStatus",
    "ReviewFinding",
    "ReviewReport",
    "REVIEW_SCHEMA_VERSION",
    "export_review_report",
    "parse_unified_diff",
    "read_git_diff",
    "review_report_to_json",
    "review_report_to_markdown",
    "validate_revision",
]
