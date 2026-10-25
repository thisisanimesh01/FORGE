"""Markdown and JSON report formatting for review benchmark results."""

import json
from pathlib import Path
from typing import Any, Dict

from forge.benchmarks.review_models import ReviewBenchmarkRunResult
from forge.config import redact_secrets


def review_benchmark_to_json(result: ReviewBenchmarkRunResult, indent: int = 2) -> str:
    """Format ReviewBenchmarkRunResult as clean JSON with secrets redacted."""
    raw = result.model_dump(mode="json")
    json_str = json.dumps(raw, indent=indent, sort_keys=True)
    return redact_secrets(json_str)


def review_benchmark_to_markdown(result: ReviewBenchmarkRunResult) -> str:
    """Format ReviewBenchmarkRunResult as human-readable GitHub-flavored Markdown."""
    met = result.metrics

    prec_str = f"{met.precision:.2%}" if met.precision is not None else "N/A"
    rec_str = f"{met.recall:.2%}" if met.recall is not None else "N/A"
    f1_str = f"{met.f1_score:.2%}" if met.f1_score is not None else "N/A"
    fpr_str = f"{met.false_positive_rate:.2%}" if met.false_positive_rate is not None else "N/A"
    spec_str = f"{met.specificity:.2%}" if met.specificity is not None else "N/A"
    pass_str = f"{met.case_pass_rate:.2%}" if met.case_pass_rate is not None else "N/A"
    loc_str = f"{met.location_accuracy:.2%}" if met.location_accuracy is not None else "N/A"
    ev_str = f"{met.evidence_validity_rate:.2%}" if met.evidence_validity_rate is not None else "N/A"

    lines = [
        f"# FORGE Review Quality Benchmark Report: `{result.benchmark_run_id}`",
        "",
        f"- **Schema Version**: `{result.schema_version}`",
        f"- **Evaluation Mode**: `{result.mode}`",
        f"- **Corpus Version**: `{result.corpus_version}`",
        f"- **Started At**: `{result.started_at}`",
        f"- **Completed At**: `{result.completed_at}`",
        f"- **Duration**: `{result.total_duration_seconds}s`",
        "",
        "## Summary Metrics",
        "",
        "| Metric | Value | Interpretation |",
        "|---|---|---|",
        f"| **Total Cases** | {met.total_cases} | Total benchmark fixtures evaluated |",
        f"| **Case Pass Rate** | {pass_str} | Percentage of fixtures where outcome matched expectation |",
        f"| **Precision** | {prec_str} | Fraction of reported defects that are genuine (TP / (TP + FP)) |",
        f"| **Recall** | {rec_str} | Fraction of seeded defects detected (TP / (TP + FN)) |",
        f"| **F1 Score** | {f1_str} | Harmonic mean of Precision and Recall |",
        f"| **Specificity** | {spec_str} | True negative rate on clean code (TN / (TN + FP)) |",
        f"| **False Positive Rate** | {fpr_str} | Rate of harmless code misclassified as defective |",
        f"| **Location Accuracy** | {loc_str} | Accuracy of finding line & file attribution |",
        f"| **Evidence Validity Rate** | {ev_str} | Rate of findings backed by verified evidence |",
        "",
        "### Classification Counts",
        f"- **True Positives (TP)**: {met.true_positives}",
        f"- **False Positives (FP)**: {met.false_positives}",
        f"- **False Negatives (FN)**: {met.false_negatives}",
        f"- **True Negatives (TN)**: {met.true_negatives}",
        f"- **Attribution Failures (Wrong File)**: {met.wrong_file_attributions}",
        f"- **Execution Errors**: {met.error_cases}",
        f"- **Skipped / Unavailable**: {met.skipped_unavailable_cases}",
        "",
        "### Severity Distribution",
    ]

    for sev, count in sorted(met.severity_distribution.items()):
        lines.append(f"- **{sev.capitalize()}**: {count}")

    lines.extend([
        "",
        "## Fixture Evaluation Breakdown",
        "",
        "| Fixture ID | Category | Outcome | Expected | Detected | TP | FP | FN | Duration |",
        "|---|---|---|---|---|---|---|---|---|",
    ])

    for cr in result.case_results:
        exp_str = "Defect" if cr.has_defects_expected else "Clean"
        outcome_badge = f"`{cr.outcome.value.upper()}`"
        lines.append(
            f"| `{cr.fixture_id}` | `{cr.category.value}` | {outcome_badge} | {exp_str} | {cr.findings_count} | {cr.true_positives} | {cr.false_positives} | {cr.false_negatives} | {cr.duration_seconds}s |"
        )

    lines.extend([
        "",
        "## Limitations & Boundaries",
        "- **Synthetic Controlled Fixtures**: Results represent performance against standardized diff fixtures and do not guarantee complete defect detection in arbitrary codebases.",
        "- **Fail-Closed Execution**: Any fixture requiring containerized sandbox execution safely skips if Docker is absent, rather than executing unsandboxed on the host.",
        "- **Read-Only Invariant**: Benchmark runner guarantees zero modifications to fixture repositories.",
    ])

    rendered = "\n".join(lines)
    return redact_secrets(rendered)
