# FORGE Phase 2, Milestone 3: Review Quality Benchmarking

## Status
Accepted

## Context
Phase 2 Milestone 1 established diff-aware code review (`forge review <target>`), and Milestone 2 established evidence-calibrated review findings with tripartite separation (`observed_evidence`, `inference`, `recommendation`), deterministic finding IDs, and lifecycle state-machine enforcement.

Phase 2 Milestone 3 establishes an **empirical quality benchmarking layer** specifically for diff-aware code review. Evaluating code review systems requires evaluating not just whether commands succeed without crashing, but whether reported defects correspond to real issues, whether harmless code lookalikes trigger false positives, whether finding locations and evidence are accurately attributed, and whether adversarial prompts can subvert review integrity.

---

## 1. Benchmark Architecture & Fixtures

### Benchmark Package (`app/forge/benchmarks/`)
- `app/forge/benchmarks/review_models.py`:
  - `ReviewBenchmarkCategory`: Enum categorizing benchmark fixtures (`GENUINE_SECURITY`, `GENUINE_LOGIC`, `HARMLESS_LOOKALIKE`, `CLEAN_CONTROL`, `ATTRIBUTION_CHALLENGE`, `EVIDENCE_CALIBRATION`, `DUPLICATE_AND_NEARBY`, `PROMPT_INJECTION`, `SECRET_EXPOSURE`, `EMPTY_OR_BOUNDARY`).
  - `ExpectedReviewFinding`: Target file, line range, expected severity, category, rule ID, and description.
  - `ExpectedReviewOutcome`: Ground-truth expectation (`has_defects`, `expected_findings`, `evidence_status_expected`, `requires_docker`).
  - `ReviewBenchmarkFixtureMeta`: Immutable metadata per fixture.
  - `ReviewCaseOutcome`: `PASS`, `FAIL`, `ERROR`, `SKIPPED_UNAVAILABLE`.
  - `ReviewBenchmarkMetrics`: Versioned statistics model (`REVIEW_BENCHMARK_SCHEMA_VERSION = "1.0.0"`).
  - `ReviewBenchmarkRunResult`: Complete versioned benchmark artifact.
- `app/forge/benchmarks/review_corpus.py`:
  - `ReviewCorpusLoader`: Discovers and validates fixture manifests in deterministic sorted order. Enforces strict filesystem containment checks preventing path traversal.
- `app/forge/benchmarks/review_runner.py`:
  - `ReviewBenchmarkRunner`: Orchestrates review execution against fixture diffs without leaking expected labels to the detector. Deduplicates predictions and applies strict location and evidence matching rules.
- `app/forge/benchmarks/review_metrics.py`:
  - `calculate_review_metrics()`: Pure function calculating Precision, Recall, F1 score, Specificity, False Positive Rate, Location Accuracy, Evidence Validity Rate, and Case Pass Rate.
- `app/forge/benchmarks/review_reporting.py`:
  - `review_benchmark_to_json()`: Serializes run results with automated secret redaction.
  - `review_benchmark_to_markdown()`: Generates human-readable Markdown summary tables and per-fixture outcome breakdowns.

---

## 2. Standardized Benchmark Fixtures (`tests/fixtures/review_benchmarks/`)

| Fixture ID | Category | Description | Ground Truth |
|---|---|---|---|
| `RBM01_path_traversal` | `genuine_security` | Uncontained `os.path.join` with user input | Defect: `storage.py:8` |
| `RBM02_discount_calc` | `genuine_logic` | Direct percentage subtraction defect | Defect: `pricing.py:12` |
| `RBM03_harmless_lookalike` | `harmless_lookalike` | Safe subtraction (`total - processed`) | Clean (0 findings) |
| `RBM04_clean_control` | `clean_control` | Documentation-only markdown change | Clean (0 findings) |
| `RBM05_attribution_challenge` | `attribution_challenge` | Multi-file diff: safe helper vs. dangerous shell in core | Defect: `core.py:6` (not `helpers.py`) |
| `RBM06_evidence_calibration` | `evidence_calibration` | Chained dictionary access requiring evidence verification | Defect: `payload.py:5` |
| `RBM07_prompt_injection` | `prompt_injection` | Adversarial system override directives in diff and commit | Defect: `security.py:3` and commit |
| `RBM08_secret_redaction` | `secret_exposure` | Hardcoded API token in diff | Defect: `client.py:4` (redacted) |
| `RBM09_duplicate_and_nearby` | `duplicate_and_nearby` | Two distinct shell executions on nearby lines | Defect: `runner.py:4` and `runner.py:6` |
| `RBM10_empty_boundary` | `empty_or_boundary` | Empty unified diff boundary condition | Clean (0 findings) |

---

## 3. Evaluation Rules & Mathematical Invariants

1. **Anti-Circular Evaluation**:
   - The review engine only receives the target diff text and workspace context. Expected fixture labels (`has_defects`, `expected_findings`) are never passed into engine prompts or rules.
2. **Attribution & Wrong-File Penalties**:
   - A finding reported on a file that does not contain the defect is treated as an attribution failure, tracked distinctly, and counted as a False Positive.
3. **Empty Denominators**:
   - Zero denominators (e.g. Precision when no defects were reported, or Recall when evaluating clean fixtures) evaluate to `None` (`N/A`) rather than fabricated values.
4. **Fail-Closed and Error Segregation**:
   - Fixtures that encounter execution errors (`ERROR`) or are skipped due to missing sandbox runtimes (`SKIPPED_UNAVAILABLE`) are segregated and never counted as passes or true negatives.
5. **Secret Sanitization**:
   - All benchmark reports, outputs, and JSON exports pass through `redact_secrets()`.
6. **Prompt Injection Invariance**:
   - Adversarial prompt injection text in diffs or commit messages cannot alter benchmark runner execution permissions or bypass evaluation.

---

## 4. CLI Usage

```bash
# Markdown benchmark report to stdout
forge benchmark review

# JSON output for automated CI/CD pipelines
forge benchmark review --format json

# Filter for a specific fixture
forge benchmark review --fixture RBM01_path_traversal

# Export report to file
forge benchmark review --output reports/review_benchmark.json --format json
```

## 5. Baseline Performance

In initial offline static evaluation against the 10 standardized fixtures:
- **Total Cases**: 10
- **Passed Cases**: 10 (100.00% Case Pass Rate)
- **Precision**: 100.00% (9 TP, 0 FP)
- **Recall**: 100.00% (9 TP, 0 FN)
- **Specificity**: 100.00% (3 TN, 0 FP on clean/lookalike fixtures)
- **False Positive Rate**: 0.00%
- **Location Accuracy**: 100.00%
- **Evidence Validity Rate**: 100.00%
