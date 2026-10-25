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
        "### Classification Counts (Case-level vs Finding-level)",
        f"- **Case-Level Outcomes (Total Cases: {met.total_cases})**:",
        f"  - Case True Positives (defective fixtures passed): {met.case_true_positives}",
        f"  - Case True Negatives (clean fixtures passed): {met.case_true_negatives}",
        f"  - Case False Positives (clean fixtures falsely flagged): {met.case_false_positives}",
        f"  - Case False Negatives (defective fixtures missed): {met.case_false_negatives}",
        f"  - Case Errors: {met.error_cases}",
        f"  - Case Skipped / Unavailable: {met.skipped_unavailable_cases}",
        f"- **Finding-Level Counts (Defect Detection Unit)**:",
        f"  - Finding True Positives (TP): {met.finding_true_positives}",
        f"  - Finding False Positives (FP): {met.finding_false_positives}",
        f"  - Finding False Negatives (FN): {met.finding_false_negatives}",
        f"  - Attribution Failures (Wrong File): {met.wrong_file_attributions}",
        "",
        "### Severity Distribution",
    ]

    for sev, count in sorted(met.severity_distribution.items()):
        lines.append(f"- **{sev.capitalize()}**: {count}")

    if met.category_metrics:
        lines.extend([
            "",
            "### Per-Category Performance Breakdown",
            "",
            "| Category | Cases | Passed | Pass Rate | Finding TP | Finding FP | Finding FN | Precision | Recall |",
            "|---|---|---|---|---|---|---|---|---|",
        ])
        for cat_name, cmet in sorted(met.category_metrics.items()):
            p_rate = f"{cmet['pass_rate']:.1%}" if cmet.get("pass_rate") is not None else "N/A"
            c_prec = f"{cmet['precision']:.1%}" if cmet.get("precision") is not None else "N/A"
            c_rec = f"{cmet['recall']:.1%}" if cmet.get("recall") is not None else "N/A"
            lines.append(
                f"| `{cat_name}` | {cmet['total_cases']} | {cmet['passed_cases']} | {p_rate} | {cmet['finding_true_positives']} | {cmet['finding_false_positives']} | {cmet['finding_false_negatives']} | {c_prec} | {c_rec} |"
            )

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
