# Phase 2, Milestone 6: CI Review Policies and SARIF Export

## 1. Overview & Objective

Phase 2, Milestone 6 introduces two core enterprise platform capabilities to FORGE:
1. **Deterministic CI Review Policy Engine**: A typed, declarative policy evaluation layer that inspects calibrated review findings without modifying detection logic or mutating repositories.
2. **Standards-Compliant SARIF 2.1.0 Exporter**: Comprehensive export formatting for seamless integration with GitHub Code Scanning, GitLab SAST, IDE diagnostics, and CI/CD security dashboards.

---

## 2. Policy Engine Architecture (`forge.review.policy`)

### 2.1 Separation of Detection and Enforcement
In FORGE, detection is an objective measurement of code changes against verified static evidence, while policy evaluation is an organizational risk decision. Detection rules never silently suppress or drop findings; instead, the policy engine consumes a completed `ReviewReport` and produces a structured, auditable `PolicyEvaluationReport`.

### 2.2 Core Policy Parameters (`ReviewPolicy`)
- **`min_fail_severity`**: Threshold severity (`critical`, `high`, `medium`, `low`, `info`) required to block a build.
- **`min_enforce_confidence`**: Minimum confidence required for enforcement (`confirmed`, `high`, `medium`, `low`). Findings below this threshold pass with auditable rationale.
- **`included_categories` / `excluded_categories`**: Explicit category filters (`security`, `secret_exposure`, `defect`, etc.).
- **`evidence_handling`**:
  - `report_only` (default): Reports findings regardless of evidence calibration status.
  - `fail_closed`: Automatically blocks findings with unverified, mismatched, or insufficient evidence.
  - `ignore`: Bypasses policy evaluation for unverified findings while preserving them in logs.
- **`suppressions`**: Narrowly scoped rule suppressions requiring an explicit auditable `reason`, optional `file_path` constraint, author metadata, and expiration date (`expires_at`). Expired suppressions automatically fail closed.

### 2.3 Checked-In Safe Example Policy
A production-ready example policy is maintained at [`docs/examples/ci_review_policy.json`](../examples/ci_review_policy.json):
```json
{
  "policy_id": "ci_security_gate_strict",
  "name": "Strict CI Security & Defect Gate",
  "min_fail_severity": "high",
  "min_enforce_confidence": "high",
  "evidence_handling": "report_only",
  "scope_changed_lines_only": true,
  "suppressions": [
    {
      "rule_id": "REV_DEF_CHAINED_DICT_ACCESS",
      "file_path": "tests/unit/test_review_benchmark.py",
      "reason": "Test assertions deliberately verify exact dictionary structure in unit test suite.",
      "expires_at": "2027-01-01"
    }
  ]
}
```

---

## 3. SARIF 2.1.0 Exporter (`forge.review.sarif`)

FORGE provides full compliance with the OASIS SARIF 2.1.0 standard:
- **Schema & Version**: `$schema: https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json`, `version: 2.1.0`.
- **Driver & Rule Metadata**: Stable tool metadata (`name`, `version`, `informationUri`) and full rule definitions with markdown explanations and recommendations.
- **Severity Mapping**:
  - `CRITICAL` / `HIGH` $\rightarrow$ `error`
  - `MEDIUM` $\rightarrow$ `warning`
  - `LOW` / `INFO` $\rightarrow$ `note`
- **Location Regions**: 1-indexed relative artifact URIs with `startLine`, `startColumn`, `endLine`, and verbatim code snippets. Locations are strictly omitted for non-file artifacts (e.g., `<commit_message>`) to prevent invented coordinates.
- **Suppression Tracking**: Reflects external policy suppressions inside `results[].suppressions` with justifications.
- **Dataflow & Property Bags**: Retains `dataflowPath`, `findingId`, `confidence`, `evidenceStatus`, and `lifecycleState` in `properties`.
- **Secret Redaction**: Every exported string passes through the hardened redaction boundary.

---

## 4. CLI Commands & Exit Codes

### Review with SARIF Export
```bash
# Output SARIF to stdout (machine-readable, zero banner pollution)
forge review . --base HEAD~1 --head HEAD --format sarif

# Export SARIF directly to a file
forge review . --base HEAD~1 --head HEAD --format sarif -o report.sarif
```

### Policy Checking
```bash
# Evaluate a generated review report against a policy
forge policy check report.json --policy docs/examples/ci_review_policy.json

# Machine-readable JSON output for CI pipeline automation
forge policy check report.json --policy docs/examples/ci_review_policy.json --format json
```

### Exit Codes
- `0`: Policy check passed (zero blocking findings).
- `1`: Policy check failed (one or more findings blocked build).
- `2`: Invalid input, missing file, or malformed policy syntax.

---

## 5. Benchmark Compatibility

All 3 benchmark corpora remain deterministic, fully verified, and passing at 100% precision and recall:
- **Standard Corpus (`--corpus standard`)**: 10/10 cases passed.
- **Held-Out Adversarial Corpus (`--corpus heldout`)**: 8/8 cases passed.
- **Security Dataflow Corpus (`--corpus security`)**: 8/8 cases passed.
