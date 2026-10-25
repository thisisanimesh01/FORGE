"""Evaluation metrics and scoring rules for review quality benchmarking."""

from typing import Dict, List, Optional
from forge.benchmarks.review_models import (
    ReviewBenchmarkMetrics,
    ReviewCaseExecutionResult,
    ReviewCaseOutcome,
)


def calculate_review_metrics(results: List[ReviewCaseExecutionResult]) -> ReviewBenchmarkMetrics:
    """Compute mathematical evaluation metrics for review benchmark execution.

    Invariants:
    - SKIPPED_UNAVAILABLE cases are not evaluated and never inflate TP/FP/FN/TN.
    - ERROR cases cannot silently become True Negatives or True Positives.
    - Zero denominators result in None (N/A), never 0.0 or 1.0.
    - Wrong file attributions are counted as False Positives and tracked distinctly.
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
    wrong_file = 0

    total_matched_loc = 0
    total_loc_candidates = 0

    valid_ev = 0
    total_ev = 0

    severity_dist: Dict[str, int] = {
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "info": 0,
    }

    for r in results:
        if r.outcome == ReviewCaseOutcome.PASS:
            passed += 1
        elif r.outcome == ReviewCaseOutcome.FAIL:
            failed += 1
        elif r.outcome == ReviewCaseOutcome.ERROR:
            error_cnt += 1
        elif r.outcome == ReviewCaseOutcome.SKIPPED_UNAVAILABLE:
            skipped_cnt += 1

        # Skipped cases do not participate in classification metrics
        if r.outcome == ReviewCaseOutcome.SKIPPED_UNAVAILABLE:
            continue

        # Error cases cannot become true negatives
        if r.outcome == ReviewCaseOutcome.ERROR:
            if r.has_defects_expected:
                fn += 1
            continue

        tp += r.true_positives
        fp += r.false_positives
        fn += r.false_negatives
        tn += r.true_negatives
        wrong_file += r.wrong_file_attributions

        valid_ev += r.valid_evidence_count
        total_ev += r.total_evidence_count

        # Extract location candidate counts if stored in details
        loc_cand = r.details.get("location_candidates", 0)
        loc_mat = r.details.get("location_matches", 0)
        total_loc_candidates += loc_cand
        total_matched_loc += loc_mat

        # Accumulate severities
        case_sevs = r.details.get("severities", {})
        for s, count in case_sevs.items():
            s_low = str(s).lower()
            severity_dist[s_low] = severity_dist.get(s_low, 0) + count

    # Derived rates
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    f1_score = (
        (2 * precision * recall / (precision + recall))
        if (precision is not None and recall is not None and (precision + recall) > 0)
        else None
    )

    # Specificity = TN / (TN + FP)
    specificity = (tn / (tn + fp)) if (tn + fp) > 0 else None

    # False Positive Rate = FP / (FP + TN)
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else None

    # Case pass rate over evaluated cases (excluding skipped)
    eval_cases = passed + failed + error_cnt
    case_pass_rate = (passed / eval_cases) if eval_cases > 0 else None

    # Location accuracy = location_matches / location_candidates
    location_accuracy = (total_matched_loc / total_loc_candidates) if total_loc_candidates > 0 else None

    # Evidence validity rate = valid_evidence / total_evidence
    evidence_validity_rate = (valid_ev / total_ev) if total_ev > 0 else None

    return ReviewBenchmarkMetrics(
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        error_cases=error_cnt,
        skipped_unavailable_cases=skipped_cnt,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        wrong_file_attributions=wrong_file,
        precision=precision,
        recall=recall,
        f1_score=f1_score,
        false_positive_rate=fpr,
        specificity=specificity,
        case_pass_rate=case_pass_rate,
        location_accuracy=location_accuracy,
        evidence_validity_rate=evidence_validity_rate,
        severity_distribution=severity_dist,
    )
