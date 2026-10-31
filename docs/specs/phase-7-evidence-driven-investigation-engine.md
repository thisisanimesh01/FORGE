# Phase 7 Specification: Evidence-Driven Investigation Engine

## 1. Background & Problem Statement

In a live audit against FastAPI (`https://github.com/fastapi/fastapi.git`, commit `45cb2252c737906f37544f5ba6bee0295fb81468`, run `run_8120cd03f318`), FORGE recorded a false-positive finding (`find_42b375ce6f2a`) titled:
`"Upload File Invalid Pydantic V2"`

The finding alleged:
- `tests/test_datastructures.py` uses an outdated Pydantic version and may introduce security vulnerabilities.
- Classification: HIGH severity, MEDIUM confidence, SUSPECTED state.

Independent source code inspection established that this finding was completely contradicted by repository reality:
1. **Dependency Declarations**: `pyproject.toml` declared `pydantic>=2.9.0,<3.0.0` and other Pydantic v2 constraints.
2. **Implementation Logic**: `fastapi/datastructures.py` implements `UploadFile._validate()` which checks `StarletteUploadFile` and raises `ValueError("Expected UploadFile...")`.
3. **Test Semantics**: `tests/test_datastructures.py` was asserting expected error handling (`with pytest.raises(ValueError): UploadFile._validate("invalid")`). The test specifically proved that invalid input was rejected.
4. **False Positive Cause**: The model observed a negative test verifying error rejection, assumed that "invalid" meant a defect, ignored dependency manifests, failed to inspect the production implementation, and fabricated a speculative "security vulnerability" without an exploit vector.

Phase 7 hardens the investigation engine to ensure every finding is backed by cross-file evidence, verified against dependency manifests, tested against production implementations, and calibrated for severity and confidence.

---

## 2. Core Architectural Pillars

### A. Claim-to-Evidence Validation
- **No Test-Only Findings**: Candidate findings citing test files without demonstrable production impact are rejected.
- **Negative Assertion Protection**: Source ranges containing `pytest.raises`, `assertRaises`, or error-verification constructs are validated to ensure standard error rejection is not claimed as a defect.
- **Test-Name False Positive Prevention**: Deriving a defect claim directly from a test function name (e.g., `def test_upload_file_invalid_pydantic_v2()`) without evidence of failure is rejected.

### B. Cross-File Exploration
Two new read-only tools are exposed to the model in `ToolRegistry`:
1. `inspect_dependencies`: Parses workspace manifests (`pyproject.toml`, `requirements*.txt`, `setup.cfg`) to return exact declared dependencies and constraints.
2. `find_related_files`: Discovers relationships between test files, production implementation files, and configuration files.

The model instructions in `SYSTEM_PROMPT` require cross-file investigation: inspecting implementations, tests, and dependency declarations before recording findings.

### C. Deterministic Contradiction Detection
Candidate finding claims are subjected to multi-layered contradiction validators before acceptance:
- `validate_dependency_claim`: Rejects claims alleging outdated, unsupported, or missing dependencies when project manifests explicitly declare the dependency.
- `validate_test_file_claim`: Rejects claims asserting defects based on expected negative test assertions.
- `validate_implementation_contradiction`: Discovers related implementation files and detects explicit input validation that contradicts "missing validation" claims.
- `validate_claim_evidence_relevance`: Verifies semantic tokens for specific categories (e.g., SQL queries for SQL injection claims).

### D. Severity & Impact Calibration
- **No Speculative Security Claims**: Unsubstantiated claims containing phrases like "may introduce security vulnerabilities" or "potential security vulnerability" without reproducible proof are rejected with `SeverityJustificationError`.
- **Test File Severities**: Findings on test code cannot claim HIGH or CRITICAL severity without demonstrable production exploitability.
- **Severity Downgrade**: Findings lacking concrete failure modes are calibrated to MEDIUM severity with explicit limitations recorded.
- **Confidence Bounds**: Static inspection cannot confirm defects without isolated dynamic reproduction; confidence is capped at HIGH with preserved uncertainty annotations.

### E. Final Investigation Synthesis & Reporting
- The agent loop tracks all inspected files and rejected candidate findings.
- When budget is nearly exhausted, a dedicated synthesis turn is reserved.
- The exported JSON and Markdown reports explicitly report `rejected_claims_count`, inspected files, and full validation rejection rationales in the timeline.

---

## 3. Tool Specifications

### `inspect_dependencies`
Inspects declared dependencies, constraints, and package configuration across repository manifests.

#### Parameters
```json
{
  "type": "object",
  "properties": {
    "package_name": {
      "type": "string",
      "description": "Optional specific package name to inspect (e.g. 'pydantic')"
    }
  }
}
```

#### Return Value
```json
{
  "manifest_sources": ["pyproject.toml"],
  "python_requires": ">=3.8",
  "total_dependencies": 3,
  "dependencies": {
    "pydantic": ">=2.9.0,<3.0.0",
    "starlette": ">=0.40.0,<0.42.0",
    "typing-extensions": ">=4.8.0"
  }
}
```

---

### `find_related_files`
Locates implementations, tests, and configuration files related to a target file.

#### Parameters
```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "Relative file path within repository root (e.g. 'tests/test_datastructures.py')"
    }
  },
  "required": ["file_path"]
}
```

#### Return Value
```json
{
  "file_path": "tests/test_datastructures.py",
  "implementations": ["fastapi/datastructures.py"],
  "tests": ["tests/test_datastructures.py"],
  "dependencies": ["pyproject.toml"]
}
```

---

## 4. Validation Error Taxonomy

| Exception | Base Class | Trigger Condition | Directive |
|---|---|---|---|
| `ContradictionValidationError` | `FindingValidationError` | Finding claim contradicts dependency manifests or implementation validation | Cross-reference manifests and implementations; do not claim contradicted defects |
| `TestAssertionMisinterpretationError` | `FindingValidationError` | Test assertion checking negative case or error handling claimed as defect | Negative tests verifying rejection are expected behavior; inspect production source |
| `SeverityJustificationError` | `FindingValidationError` | Speculative security claim or unjustified HIGH/CRITICAL severity | Provide concrete impact demonstration or downgrade severity |
| `UnrelatedEvidenceError` | `InsufficientEvidenceError` | Source range lacks relevant syntax tokens for claimed defect class | Cite exact source lines containing the relevant operation |
| `RepeatedRejectedClaimError` | `FindingValidationError` | Finding resubmitted after prior rejection in same run | Do not re-attempt rejected claims; synthesize observations truthfully |

---

## 5. Verification Suite

The implementation is verified by a 10-point regression suite in `tests/unit/test_phase7_investigation_evidence_validation.py`:
1. `test_unsupported_pydantic_vulnerability_claim`: Reproduces FastAPI failure case; verifies rejection of speculative Pydantic vulnerability claim.
2. `test_contradictory_dependency_evidence`: Verifies rejection of claims alleging missing dependencies declared in manifests.
3. `test_implementation_contradicting_alleged_defect`: Verifies cross-file validation checking implementation code.
4. `test_test_name_based_false_positive`: Verifies rejection of defect claims deriving purely from test function names.
5. `test_high_severity_without_demonstrated_impact`: Verifies rejection of speculative security claims and proper severity calibration.
6. `test_evidence_unrelated_to_finding_description`: Verifies rejection of unrelated source code excerpts.
7. `test_duplicate_rejected_claims`: Verifies that per-run rejected claim tracker intercepts rephrased retries.
8. `test_correctly_supported_findings_remain_accepted`: Verifies that genuine verified bugs are accepted with preserved uncertainty.
9. `test_insufficient_evidence_results_in_unverified_outcome`: Verifies that insufficient evidence leaves the store unpolluted.
10. `test_final_synthesis_accurately_reporting_accepted_and_rejected_claims`: Verifies truthful loop completion and report synthesis.

---

## 6. Pilot Run Failure Forensic Analysis & Engine Remediation (`run_ce01ff96700b`)

### A. Failure Case
- **Run ID**: `run_ce01ff96700b`
- **Target**: `https://github.com/fastapi/fastapi.git` (commit `45cb2252c737906f37544f5ba6bee0295fb81468`)
- **Provider**: Ollama (`qwen2.5-coder:1.5b`)
- **Status**: `FAILED` (duration: 144.91s)
- **Timeline Observation**: Turn 5 started at `19:11:28`, run failed at `19:13:28` (an exact 120.0-second gap).

### B. Forensic Root Cause
1. **Primary**: During Step 5, the model request to Ollama's `/chat/completions` endpoint exceeded the HTTP read timeout.
2. **120-Second Gap Explained**: `OpenAICompatibleProvider` defaulted to `timeout_seconds = 60.0` and `max_retries = 1`. When the first request attempt timed out after 60s, `httpx.TimeoutException` was caught by the retry loop, which executed a second attempt that timed out after another 60s. Total elapsed time before raising `LLMUnavailableError`: exactly `2 * 60.0s = 120.0s`.
3. **Audit Visibility Gap**: The loop caught `LLMUnavailableError` and logged a generic error string, but no structured failure event was persisted, and the report builder did not surface failure details or execution stage in exported JSON or Markdown reports.
4. **Synthesis Invariant Gap**: Runs failing during or before final synthesis did not distinguish between partial progress and clean completion in report models.

### C. Remediation Implemented
1. **Configurable LLM Timeout**: Added `llm_timeout_seconds` to `ForgeSettings` (default `120.0s`), wired through `OpenAICompatibleProvider` and all provider factories.
2. **Explicit `LLMTimeoutError`**: Introduced a dedicated subclass of `LLMUnavailableError` to classify timeout exceptions separately from network connection failures.
3. **Structured Failure Event**: Standardized `EventType.RUN_FAILED` payloads containing:
   - `failure_category` (`timeout`, `connection_error`, `authentication_error`, `rate_limit`, `context_length`, `provider_error`, `malformed_response`, `empty_response`, `tool_dispatch_error`, `synthesis_error`, `unexpected_exception`)
   - `error_message` (sanitized with zero secrets)
   - `stage` (`initialization`, `llm_generation`, `tool_dispatch`, `synthesis`)
   - `step` (turn number)
   - `retryable` (boolean flag)
   - `timestamp` (ISO-8601 UTC)
4. **Report Failure Details Exposure**: `InvestigationReport` model now incorporates `failure_details` and `final_synthesis`. Both JSON and Markdown reports prominently render failure diagnosis while preserving all partial investigation findings, evidence, and event history.
5. **Synthesis Integrity**: Enforced that a run cannot be marked `completed` if final synthesis did not succeed. If turn budget is reached without synthesis, status is marked `max_steps_exceeded`; if synthesis fails with an exception, status is marked `failed` with stage `synthesis`.

### D. Regression Suite
Verified by 12 regression tests in `tests/unit/test_phase7_failed_run_root_cause.py`:
1. `test_ollama_request_timeout`
2. `test_ollama_connection_failure`
3. `test_empty_model_response`
4. `test_malformed_model_response`
5. `test_invalid_tool_call_response`
6. `test_tool_dispatch_exception`
7. `test_investigation_loop_unexpected_exception`
8. `test_final_synthesis_failure`
9. `test_failure_event_persistence`
10. `test_report_rendering_of_failure_details`
11. `test_secret_redaction_in_failure_messages`
12. `test_preservation_of_partial_investigation_history`
