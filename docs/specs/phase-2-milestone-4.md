# FORGE Phase 2, Milestone 4: Adversarial Review Validation and Generalization

## Status
Accepted

## Context
Phase 2 Milestone 3 established the standard review benchmarking harness with 10 synthetic fixtures. In Milestone 4, we conduct a rigorous mathematical audit of benchmark correctness, resolve the counting unit discrepancy, add evaluator self-tests proving detection of injected errors, and evaluate FORGE against a genuinely held-out, independent adversarial fixture corpus.

---

## 1. Benchmark Correctness & Metric Units Audit

### Resolution of Counting Unit Discrepancy
In Phase 2 Milestone 3 baseline reporting, a potential discrepancy was observed: "10 benchmark cases evaluated, but 9 True Positives + 3 True Negatives = 12 outcomes."

**Root Cause Analysis & Specification**:
- **Case-Level vs. Finding-Level Accounting**:
  - A benchmark **case (fixture)** can contain multiple distinct expected defects or zero expected defects (clean controls).
  - Fixtures `RBM07_prompt_injection` and `RBM09_duplicate_and_nearby` each contain **two distinct expected defects** (`2 + 2 = 4` defects across 2 fixtures).
  - The remaining 5 defective fixtures contain **one expected defect** each (`5 * 1 = 5` defects).
  - Total expected defects across the 7 defective fixtures = `4 + 5 = 9` defects.
  - When all 9 defects are detected, **Finding-Level True Positives (TP)** = `9`.
  - The remaining 3 fixtures (`RBM03_harmless_lookalike`, `RBM04_clean_control`, `RBM10_empty_boundary`) are clean controls with zero expected defects.
  - When clean fixtures produce zero findings, **Case-Level True Negatives (TN)** = `3`.
- **Resolution**:
  - We explicitly separate and report both counting units in `ReviewBenchmarkMetrics`:
    - `case_true_positives` (5), `case_true_negatives` (3), `case_false_positives` (0), `case_false_negatives` (0). Sum = `10` total cases.
    - `finding_true_positives` (9), `finding_false_positives` (0), `finding_false_negatives` (0).
  - Precision, Recall, and F1 score are rigorously computed at the finding level ($\frac{TP_{findings}}{TP_{findings} + FP_{findings}}$).
  - Denominators are strictly segregated so case counts and finding counts are never mixed in division.

---

## 2. Genuinely Held-Out Adversarial Corpus (`tests/fixtures/heldout_review_benchmarks/`)

The held-out corpus is physically separated from standard fixtures and contains 8 challenging test cases:
1. `ADV01_novel_shell_subproc`: Novel variation of shell execution via `subprocess.Popen(..., shell=True)` (genuine security defect).
2. `ADV02_novel_logic_discount`: Variable-name variation of direct discount subtraction defect (genuine logic defect).
3. `ADV03_lookalike_safe_diff`: Benign subtraction arithmetic (`variance = actual - baseline`) resembling defect pattern (harmless lookalike).
4. `ADV04_attribution_trap`: Multi-file diff modifying `auth.py` (safe) and `download.py` (uncontained `os.path.join`). Evaluates whether finding is attributed to the correct file.
5. `ADV05_multi_defect_single_file`: Single file containing two distinct vulnerabilities on different lines (`os.path.join` and `subprocess.run(shell=True)`).
6. `ADV06_prompt_injection_evasion`: Source comments attempting to instruct the LLM to disregard safety guidelines.
7. `ADV07_redacted_auth_token`: Authorization token string in diff (`Bearer sk-...`). Evaluates whether pattern scanner catches auth header variants and verifies secret redaction.
8. `ADV08_renamed_file_clean`: Pure git file rename without code modification. Verifies zero false positives on file metadata changes.

---

## 3. Evaluator Self-Tests

Evaluator integrity is verified in `tests/unit/test_review_benchmark.py::TestEvaluatorSelfTests`:
- Injected false positives decrease precision ($1.0 \to 0.5$).
- Injected false negatives decrease recall ($1.0 \to 0.5$).
- Duplicate predictions are deduplicated and cannot artificially inflate True Positives.
- Wrong-file attribution penalties correctly increment `wrong_file_attributions` and count as False Positives.
- Injected unverified evidence decreases the evidence validity rate ($1.0 \to 0.5$).

---

## 4. Empirical Evaluation Results & Regression Gates

### Comparative Evaluation

| Metric | Standard Baseline (`RBM01-10`) | Held-Out Adversarial (`ADV01-08`) |
|---|---|---|
| **Total Cases** | 10 | 8 |
| **Case Pass Rate** | 100.0% (10/10) | 87.5% (7/8) |
| **Case TP / TN** | 5 TP / 3 TN | 5 TP / 2 TN |
| **Case FN / FP** | 0 FN / 0 FP | 1 FN (`ADV07`) / 0 FP |
| **Finding Precision** | 100.0% | 100.0% (0 False Positives) |
| **Finding Recall** | 100.0% | 85.7% (6 TP, 1 FN) |
| **F1 Score** | 100.0% | 92.3% |
| **Specificity** | 100.0% | 100.0% (0 False Positives on lookalikes/renames) |
| **False Positive Rate** | 0.00% | 0.00% |
| **Location Accuracy** | 100.0% | 100.0% |
| **Evidence Validity Rate** | 100.0% | 100.0% |

### Defensible Regression Thresholds
Based on empirical baseline and held-out evaluation:
1. **Precision Floor**: $\ge 95\%$ (Current: 100.0%). The system must never flood developers with false positives.
2. **Recall Floor**: $\ge 80\%$ (Current: 85.7% on held-out adversarial, 100% on standard). Static heuristics will miss syntax variations not covered by rules, but must retain core detection.
3. **Specificity Floor**: $\ge 95\%$ (Current: 100.0%). Clean code, renames, and harmless lookalikes must pass without false alarms.
4. **Attribution Accuracy**: $\ge 95\%$ (Current: 100.0%). Multi-file diffs must accurately map to the defective file.
5. **Evidence Validity Rate**: $100.0\%$. Unbacked findings must be flagged as insufficient.

---

## 5. Security & Isolation Invariants

- **Anti-Circular Isolation**: Ground-truth expectations are strictly decoupled from detector execution.
- **Secret Redaction**: Real or fake secrets (`sk-...`) are sanitized before serialization in both JSON and Markdown reports.
- **Fail-Closed Execution**: Offline execution by default. Any test requiring isolated containers gracefully skips when Docker is unavailable.
