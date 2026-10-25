"""Formatting and export utilities for evidence-calibrated code review reports."""

import json
from pathlib import Path

from forge.config import redact_secrets
from forge.review.models import ReviewReport


def review_report_to_json(report: ReviewReport, indent: int = 2) -> str:
    """Serialize ReviewReport to JSON with secrets redacted."""
    raw_dict = report.model_dump(mode="json")
    json_str = json.dumps(raw_dict, indent=indent, sort_keys=True)
    return redact_secrets(json_str)


def review_report_to_markdown(report: ReviewReport) -> str:
    """Format ReviewReport as GitHub-flavored Markdown with secrets redacted."""
    lines = [
        f"# FORGE Code Review Report: {report.review_id}",
        "",
        f"- **Schema Version**: `{report.schema_version}`",
        f"- **Target Repository**: `{report.target}`",
        f"- **Base Revision**: `{report.base_revision}`",
        f"- **Head Revision**: `{report.head_revision}`",
        f"- **Review Mode**: `{report.mode}`",
        f"- **Created At**: `{report.created_at}`",
        f"- **Duration**: `{report.duration_seconds}s`",
        "",
        "## Diff Summary",
        f"- Files Changed: {report.diff_summary.get('total_files_changed', 0)}",
        f"- Additions: `+{report.diff_summary.get('total_additions', 0)}`",
        f"- Deletions: `-{report.diff_summary.get('total_deletions', 0)}`",
        f"- Raw Diff Size: {report.diff_summary.get('raw_size_bytes', 0)} bytes",
        "",
    ]

    if report.capabilities_unavailable:
        lines.append("## Capabilities Unavailable / Skipped")
        for cap in report.capabilities_unavailable:
            lines.append(f"- [WARN] {cap}")
        lines.append("")

    lines.extend([
        "## Review Findings Summary",
        f"- **Total Findings**: {report.summary_counts.get('total_findings', 0)}",
    ])
    by_sev = report.summary_counts.get("by_severity", {})
    for sev, count in sorted(by_sev.items()):
        lines.append(f"  - `{sev.upper()}`: {count}")
    lines.append("")

    lines.append("## Detailed Findings")
    if not report.findings:
        lines.append("No defects, security issues, or policy violations detected in changed lines.")
    else:
        for idx, f in enumerate(report.findings, start=1):
            line_str = f"Line {f.line_number}" if f.line_number else "Commit / File-level"
            lines.extend([
                f"### {idx}. [{f.severity.value.upper()}] {f.title}",
                f"- **Finding ID**: `{f.finding_id}`",
                f"- **Category**: `{f.category.value}`",
                f"- **Location**: `{f.file_path}` ({line_str})",
                f"- **Rule ID**: `{f.rule_id}`",
                f"- **Severity**: `{f.severity.value.upper()}`",
                f"- **Confidence**: `{f.confidence.value.upper()}`",
                f"- **Lifecycle State**: `{f.lifecycle_state.value}`",
                f"- **Evidence Status**: `{f.evidence_status.value}`",
                "",
                f"**Observed Evidence:** {f.observed_evidence}",
                "",
                f"**Inference:** {f.inference}",
                "",
                f"**Recommendation:** {f.recommendation}",
            ])

            if f.uncertainty_note:
                lines.extend([
                    "",
                    f"> [!WARNING] **Uncertainty / Evidence Limitation**: {f.uncertainty_note}",
                ])

            if f.code_snippet:
                lines.extend([
                    "",
                    "```python",
                    f.code_snippet,
                    "```",
                ])

            if f.dataflow_path:
                lines.extend([
                    "",
                    f"> **Dataflow Path:** `{f.dataflow_path.get('source_kind', 'source')}:{f.dataflow_path.get('source_name', '')}` (line {f.dataflow_path.get('source_line')}) ➔ `{f.dataflow_path.get('sink_kind', 'sink')}:{f.dataflow_path.get('sink_name', '')}` (line {f.dataflow_path.get('sink_line')})",
                    f"> {f.dataflow_path.get('description', '')}",
                ])

            if f.calibrated_evidence:
                lines.extend([
                    "",
                    "**Attached Evidence:**",
                ])
                for ev in f.calibrated_evidence:
                    lines.append(
                        f"- `{ev.evidence_id}` [{ev.status.value}] ({ev.provenance}): {ev.description} (Hash: `{ev.content_hash}`)"
                    )
            lines.append("")

    md_output = "\n".join(lines)
    return redact_secrets(md_output)


def export_review_report(report: ReviewReport, output_path: Path, output_format: str = "markdown") -> Path:
    """Save review report to disk safely."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        content = review_report_to_json(report)
    else:
        content = review_report_to_markdown(report)

    output_path.write_text(content, encoding="utf-8")
    return output_path
