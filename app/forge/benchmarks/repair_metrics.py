"""Metric calculation and statistical analysis for repair quality benchmarks."""

from typing import List, Optional

from forge.benchmarks.repair_models import (
    RepairBenchmarkCaseResult,
    RepairBenchmarkMetrics,
)


def calculate_repair_metrics(case_results: List[RepairBenchmarkCaseResult]) -> RepairBenchmarkMetrics:
    """Compute aggregate repair benchmark metrics with rigorous denominator handling."""
    total = len(case_results)
    if total == 0:
        return RepairBenchmarkMetrics(
            total_cases=0,
            passed_cases=0,
            failed_cases=0,
            inconclusive_cases=0,
            skipped_cases=0,
        )

    passed = 0
    failed = 0
    inconclusive = 0
    skipped = 0

    plans_generated = 0
    valid_plans = 0
    evidence_grounded = 0
    patches_generated = 0
    patches_accepted = 0
    scope_compliant = 0
    regressions = 0
    false_fixes = 0

    total_lines = 0
    total_bytes = 0

    tp = 0
    fp = 0
    tn = 0
    fn = 0

    for c in case_results:
        if c.outcome_matched:
            passed += 1
        elif c.observed_outcome.value == "inconclusive":
            inconclusive += 1
        elif c.observed_outcome.value == "sandbox_unavailable" and c.expected_outcome.value != "sandbox_unavailable":
            skipped += 1
        else:
            failed += 1

        if c.plan_generated:
            plans_generated += 1
        if c.plan_valid:
            valid_plans += 1
        if c.evidence_grounded:
            evidence_grounded += 1
        if c.patch_generated:
            patches_generated += 1
            total_lines += c.lines_changed
            total_bytes += c.diff_bytes
            if c.patch_accepted:
                patches_accepted += 1
            if c.scope_compliant:
                scope_compliant += 1
            if c.is_regression:
                regressions += 1
            if c.is_false_fix:
                false_fixes += 1

        # Defect identification confusion matrix
        if c.has_defect_expected:
            if c.patch_accepted:
                tp += 1
            else:
                fn += 1
        else:
            # Clean control or unneeded fix
            if c.patch_generated and not c.is_clean_pass:
                fp += 1
            else:
                tn += 1

    # Ratio metrics with honest None for empty denominators
    plan_gen_rate = (plans_generated / total) if total > 0 else None
    valid_plan_rate = (valid_plans / plans_generated) if plans_generated > 0 else None
    evidence_grounding_rate = (evidence_grounded / total) if total > 0 else None
    patch_acc_rate = (patches_accepted / patches_generated) if patches_generated > 0 else None
    scope_comp_rate = (scope_compliant / patches_generated) if patches_generated > 0 else None
    outcome_acc = (passed / total) if total > 0 else None
    regression_rate = (regressions / patches_generated) if patches_generated > 0 else None
    false_fix_rate = (false_fixes / patches_generated) if patches_generated > 0 else None

    avg_lines = (total_lines / patches_generated) if patches_generated > 0 else 0.0
    avg_bytes = (total_bytes / patches_generated) if patches_generated > 0 else 0.0

    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    f1: Optional[float] = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = (2.0 * precision * recall) / (precision + recall)

    return RepairBenchmarkMetrics(
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        inconclusive_cases=inconclusive,
        skipped_cases=skipped,
        plan_generation_rate=plan_gen_rate,
        valid_plan_rate=valid_plan_rate,
        evidence_grounding_rate=evidence_grounding_rate,
        patch_acceptance_rate=patch_acc_rate,
        scope_compliance_rate=scope_comp_rate,
        outcome_accuracy=outcome_acc,
        regression_rate=regression_rate,
        false_fix_rate=false_fix_rate,
        average_lines_changed=avg_lines,
        average_diff_bytes=avg_bytes,
        precision=precision,
        recall=recall,
        f1_score=f1,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
    )
