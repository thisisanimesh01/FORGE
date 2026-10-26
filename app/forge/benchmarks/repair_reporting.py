"""Reporting and serialization for repair quality benchmark results."""

import json
from typing import Any, Dict

from forge.benchmarks.repair_models import RepairBenchmarkRunResult
from forge.config import redact_secrets


def repair_benchmark_to_dict(result: RepairBenchmarkRunResult) -> Dict[str, Any]:
    """Serialize repair benchmark run result into a sanitized dictionary."""
    data = result.model_dump(mode="json")
    return json.loads(redact_secrets(json.dumps(data)))


def repair_benchmark_to_json(result: RepairBenchmarkRunResult) -> str:
    """Serialize repair benchmark run result into formatted JSON."""
    return json.dumps(repair_benchmark_to_dict(result), indent=2, sort_keys=True)


def repair_benchmark_to_markdown(result: RepairBenchmarkRunResult) -> str:
    """Render a comprehensive, human-readable Markdown report for repair benchmarks."""
    met = result.metrics

    def fmt_pct(val: Any) -> str:
        if val is None:
            return "N/A"
        return f"{val:.1%}"

    lines = [
        f"# FORGE Repair Quality Benchmark Report",
        f"- **Benchmark Run ID**: `{result.benchmark_id}`",
        f"- **Schema Version**: `{result.schema_version}`",
        f"- **Evaluation Mode**: `{result.mode.upper()}`",
        f"- **Provider / Model**: `{result.provider}` (`{result.model}`)",
        f"- **Duration**: `{result.duration_seconds:.2f}s`",
        f"- **Sandbox Available**: `{'YES (Isolated)' if result.sandbox_available else 'NO (Simulated/Fail-Closed)'}`",
        "",
        "## 1. Aggregate Quality Metrics",
        "",
        "| Metric | Value | Description / Denominator |",
        "|---|---|---|",
        f"| **Total Cases** | `{met.total_cases}` | Total fixtures evaluated |",
        f"| **Passed Cases** | `{met.passed_cases}` | Observed outcome matched expected ground truth |",
        f"| **Failed Cases** | `{met.failed_cases}` | Outcome mismatch or unhandled defect |",
        f"| **Inconclusive Cases** | `{met.inconclusive_cases}` | Missing oracle or ambiguous defect |",
        f"| **Skipped Cases** | `{met.skipped_cases}` | Sandbox unavailable (fail-closed skip) |",
        f"| **Outcome Accuracy** | `{fmt_pct(met.outcome_accuracy)}` | `passed_cases / total_cases` |",
        f"| **Plan Generation Rate** | `{fmt_pct(met.plan_generation_rate)}` | `plans_generated / total_cases` |",
        f"| **Valid Plan Rate** | `{fmt_pct(met.valid_plan_rate)}` | `valid_plans / plans_generated` |",
        f"| **Evidence Grounding Rate**| `{fmt_pct(met.evidence_grounding_rate)}` | `grounded_cases / total_cases` |",
        f"| **Patch Acceptance Rate** | `{fmt_pct(met.patch_acceptance_rate)}` | `patches_verified / patches_generated` |",
        f"| **Scope Compliance Rate** | `{fmt_pct(met.scope_compliance_rate)}` | `compliant_patches / patches_generated` |",
        f"| **Regression Rate** | `{fmt_pct(met.regression_rate)}` | `regressions_detected / patches_generated` |",
        f"| **False-Fix Rate** | `{fmt_pct(met.false_fix_rate)}` | `failed_or_broken / patches_generated` |",
        f"| **Average Lines Changed** | `{met.average_lines_changed:.1f}` | Mean lines added/removed per candidate |",
        f"| **Average Diff Bytes** | `{met.average_diff_bytes:.0f} B` | Mean raw patch size in bytes |",
        "",
        "### Defect Identification Performance (Binary Ground Truth)",
        f"- **Precision**: `{fmt_pct(met.precision)}` (TP: {met.true_positives}, FP: {met.false_positives})",
        f"- **Recall**: `{fmt_pct(met.recall)}` (TP: {met.true_positives}, FN: {met.false_negatives})",
        f"- **F1 Score**: `{fmt_pct(met.f1_score)}`",
        f"- **True Negatives (Clean Controls)**: `{met.true_negatives}`",
        "",
        "## 2. Fixture-by-Fixture Results",
        "",
        "| Fixture ID | Category | Expected Outcome | Observed Outcome | Status | Lines | Duration |",
        "|---|---|---|---|---|---|---|",
    ]

    for cr in result.case_results:
        status_icon = "MATCH" if cr.outcome_matched else "MISMATCH"
        lines.append(
            f"| `{cr.fixture_id}` | `{cr.category.value}` | `{cr.expected_outcome.value}` | "
            f"`{cr.observed_outcome.value}` | **{status_icon}** | `{cr.lines_changed}` | `{cr.duration_ms:.1f}ms` |"
        )

    lines.extend([
        "",
        "## 3. Scope & Validity Notice",
        f"> [!NOTE]",
        f"> {result.disclaimer}",
        "",
    ])

    return redact_secrets("\n".join(lines))
