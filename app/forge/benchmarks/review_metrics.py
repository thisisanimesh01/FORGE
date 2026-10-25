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

    # Case-level counts (each case is exactly one of TP, FP, FN, TN, Error, or Skipped)
    case_tp = 0
    case_fp = 0
    case_fn = 0
    case_tn = 0

    # Per-category accumulation: category -> list of results
    cat_groups: Dict[str, List[ReviewCaseExecutionResult]] = {}

    for r in results:
        cat_key = r.category.value if hasattr(r.category, "value") else str(r.category)
        cat_groups.setdefault(cat_key, []).append(r)

        if r.outcome == ReviewCaseOutcome.PASS:
            passed += 1
            if r.has_defects_expected:
                case_tp += 1
            else:
                case_tn += 1
        elif r.outcome == ReviewCaseOutcome.FAIL:
            failed += 1
            if r.has_defects_expected:
                case_fn += 1
            else:
                case_fp += 1
        elif r.outcome == ReviewCaseOutcome.ERROR:
            error_cnt += 1
            if r.has_defects_expected:
                case_fn += 1
        elif r.outcome == ReviewCaseOutcome.SKIPPED_UNAVAILABLE:
            skipped_cnt += 1

        # Skipped cases do not participate in finding-level classification metrics
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

    # Finding-level derived rates
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    f1_score = (
        (2 * precision * recall / (precision + recall))
        if (precision is not None and recall is not None and (precision + recall) > 0)
        else None
    )

    # Specificity = TN / (TN + FP) on finding/clean fixtures
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

    # Compute per-category metrics
    category_metrics: Dict[str, Dict[str, Any]] = {}
    for cat_name, cat_res in cat_groups.items():
        cat_total = len(cat_res)
        cat_pass = sum(1 for x in cat_res if x.outcome == ReviewCaseOutcome.PASS)
        cat_tp = sum(x.true_positives for x in cat_res if x.outcome != ReviewCaseOutcome.SKIPPED_UNAVAILABLE)
        cat_fp = sum(x.false_positives for x in cat_res if x.outcome != ReviewCaseOutcome.SKIPPED_UNAVAILABLE)
        cat_fn = sum(x.false_negatives for x in cat_res if x.outcome != ReviewCaseOutcome.SKIPPED_UNAVAILABLE)
        cat_prec = (cat_tp / (cat_tp + cat_fp)) if (cat_tp + cat_fp) > 0 else None
        cat_rec = (cat_tp / (cat_tp + cat_fn)) if (cat_tp + cat_fn) > 0 else None
        category_metrics[cat_name] = {
            "total_cases": cat_total,
            "passed_cases": cat_pass,
            "pass_rate": (cat_pass / cat_total) if cat_total > 0 else None,
            "finding_true_positives": cat_tp,
            "finding_false_positives": cat_fp,
            "finding_false_negatives": cat_fn,
            "precision": cat_prec,
            "recall": cat_rec,
        }

    return ReviewBenchmarkMetrics(
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        error_cases=error_cnt,
        skipped_unavailable_cases=skipped_cnt,
        case_true_positives=case_tp,
        case_false_positives=case_fp,
        case_false_negatives=case_fn,
        case_true_negatives=case_tn,
        finding_true_positives=tp,
        finding_false_positives=fp,
        finding_false_negatives=fn,
        wrong_file_attributions=wrong_file,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        precision=precision,
        recall=recall,
        f1_score=f1_score,
        false_positive_rate=fpr,
        specificity=specificity,
        case_pass_rate=case_pass_rate,
        location_accuracy=location_accuracy,
        evidence_validity_rate=evidence_validity_rate,
        severity_distribution=severity_dist,
        category_metrics=category_metrics,
    )
