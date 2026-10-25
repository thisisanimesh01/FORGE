"""Calculation of precision, recall, F1, and pass-rate metrics for benchmarks."""

from typing import List, Optional
from forge.benchmarks.models import BenchmarkMetrics, CaseExecutionResult, CaseOutcome


def calculate_metrics(results: List[CaseExecutionResult]) -> BenchmarkMetrics:
    """Compute precision, recall, F1 score, and rates from case execution results.

    Definitions:
    - True Positive (TP): Fixture has expected defect, and FORGE produced a
      matching finding on the target file.
    - False Positive (FP): Clean fixture reported as having defects, OR defective
      fixture where finding was reported on the wrong (unaffected) file.
    - False Negative (FN): Fixture has expected defect, but FORGE produced no
      finding on the target file.
    - True Negative (TN): Clean fixture correctly identified with 0 defect findings.

    Integrity guarantees:
    - SKIPPED_UNAVAILABLE cases are not evaluated and do not count toward TP/FP/FN/TN.
    - ERROR cases cannot silently become True Negatives or True Positives.
    - If a finding does not match the target file, it is NOT counted as a True Positive.
    """
    total = len(results)
    passed = 0
    failed = 0
    error_cnt = 0
    skipped_cnt = 0

    tp = 0
    fp = 0
    fn = 0
    tn = 0

    for r in results:
        if r.outcome == CaseOutcome.PASS:
            passed += 1
        elif r.outcome == CaseOutcome.FAIL:
            failed += 1
        elif r.outcome == CaseOutcome.ERROR:
            error_cnt += 1
        elif r.outcome == CaseOutcome.SKIPPED_UNAVAILABLE:
            skipped_cnt += 1

        # Unavailable / skipped cases are not evaluated and must not inflate
        # classification counts (cannot become TP, FP, FN, or TN).
        if r.outcome == CaseOutcome.SKIPPED_UNAVAILABLE:
            continue

        # Execution error cases cannot silently become True Negatives or True Positives.
        if r.outcome == CaseOutcome.ERROR:
            if r.has_defect_expected:
                # Defective case failed to execute/detect due to error -> False Negative
                fn += 1
            # Clean case that errored is an ERROR, NOT a True Negative (tn is not incremented)
            continue

        # Validated execution outcomes (PASS or FAIL):
        if r.has_defect_expected:
            if r.defect_detected and r.matched_target_file:
                tp += 1
            elif r.defect_detected and not r.matched_target_file:
                # Reported finding on wrong file: missed the target defect (FN)
                # and falsely flagged an unaffected file (FP)
                fn += 1
                fp += 1
            else:
                # No defect detected on defective case
                fn += 1
        else:
            # Clean control case
            if r.defect_detected:
                fp += 1
            else:
                tn += 1

    # Precision = TP / (TP + FP)
    precision: Optional[float] = None
    if (tp + fp) > 0:
        precision = round(tp / (tp + fp), 4)

    # Recall = TP / (TP + FN)
    recall: Optional[float] = None
    if (tp + fn) > 0:
        recall = round(tp / (tp + fn), 4)

    # F1 Score = 2 * (Precision * Recall) / (Precision + Recall)
    f1_score: Optional[float] = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1_score = round(2 * (precision * recall) / (precision + recall), 4)

    # Pass rate = passed / evaluated_cases (where evaluated excludes skipped)
    pass_rate: Optional[float] = None
    evaluated_cases = total - skipped_cnt
    if evaluated_cases > 0:
        pass_rate = round(passed / evaluated_cases, 4)

    return BenchmarkMetrics(
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        error_cases=error_cnt,
        skipped_unavailable_cases=skipped_cnt,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        precision=precision,
        recall=recall,
        f1_score=f1_score,
        case_pass_rate=pass_rate,
    )
