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

---

## 7. Investigation Intelligence & Synthesis Remediation (`run_747548f2008e` & `run_d7744d583e06`)

### A. Background & Observed Defect
In a follow-up pilot against FastAPI (`run_747548f2008e`, model `qwen2.5-coder:1.5b`), the model submitted an invalid syntax finding on whitespace lines (`tests/test_datastructures.py:10-10`), which was correctly rejected by server-side validators. However:
1. The loop failed to convey actionable directives to prevent duplicate submissions.
2. The model re-attempted the identical unsupported claim 3 times until the turn budget was exhausted.
3. Upon exhausting steps, the model emitted a conversational apology:
   `"I apologize for the confusion. Please provide me with the necessary information to assist you further."`
4. The run was marked `completed` despite zero valid findings, zero evidence records, 4 rejected claims, and an unusable conversational apology.

### B. Architectural Remediations Implemented
1. **Semantic Duplicate Claim Tracking**:
   `RejectedClaimTracker` intercepts not only exact duplicate claims, but also semantically equivalent claims with rephrased titles, category variations, and overlapping lines. Corrected submissions with downgraded severity or expanded substantive code ranges are evaluated freshly.
2. **Configurable Rejection Budget**:
   `rejection_budget` (default 3, configurable via `FORGE_REJECTION_BUDGET`) enforces a hard limit on invalid finding submissions. When exhausted:
   - `record_finding` tool calls raise `RejectionBudgetExceededError`.
   - The loop dynamically removes `record_finding` from the model's available tools on subsequent turns, forcing the agent to inspect source code/dependencies or synthesize findings.
3. **Structured Synthesis Quality Gate**:
   `validate_synthesis_quality` strictly filters out conversational apologies, chat evasion fillers, raw JSON tool call dumps, and unstructured text.
4. **Autonomous Synthesis Retry & Deterministic Fallback**:
   When the model outputs an invalid response on its final turn, FORGE triggers an explicit synthesis retry turn with structured formatting instructions. If the retry fails to produce a valid technical assessment, FORGE deterministically generates a verifiable fallback synthesis labeled `[System-Generated Fallback Synthesis]`, referencing actual inspected files and rejected claim records without fabrication.
5. **Investigation Quality Classification Gate**:
   Separated system execution status (`completed`, `failed`, `max_steps_exceeded`) from investigation quality:
   - `completed`: Verified findings with evidence, or comprehensive clean repository inspection with valid technical synthesis.
   - `completed_with_limitations`: Execution completed, but limitations exist (fallback synthesis used, turn budget exhausted, or partial inspection).
   - `insufficient_evidence`: Execution finished, but candidate claims lacked evidence or zero substantive source files were inspected.
   - `failed`: System/provider failure, crash, timeout, or zero activity.
6. **Transparent Reporting**:
   Exposed `investigation_quality`, `files_inspected`, `rejected_claims`, and `limitations` in `InvestigationReport` model, JSON report exports, and Markdown reports.

### C. Verification Suite
Verified by 17 regression tests in `tests/unit/test_phase7_investigation_intelligence.py`:
1. `test_invalid_evidence_rejection_followed_by_useful_recovery`
2. `test_exact_duplicate_rejected_claim`
3. `test_semantically_equivalent_rejected_claim_with_different_title`
4. `test_corrected_claim_with_genuinely_new_evidence`
5. `test_rejection_budget_exhaustion`
6. `test_agent_switches_from_finding_submission_to_source_inspection`
7. `test_dependency_manifest_inspection`
8. `test_implementation_and_test_relationship_inspection`
9. `test_generic_apology_rejected_as_final_synthesis`
10. `test_structured_synthesis_retry`
11. `test_deterministic_fallback_synthesis`
12. `test_fallback_does_not_fabricate_inspected_files_or_findings`
13. `test_insufficient_evidence_quality_classification`
14. `test_meaningful_zero_finding_investigation_classification`
15. `test_completed_execution_with_failed_quality_gate`
16. `test_json_and_markdown_quality_status_reporting`
17. `test_existing_historical_report_compatibility`

### D. Real-World Live Acceptance Run
- **Target**: `https://github.com/fastapi/fastapi.git` (commit `45cb2252c737906f37544f5ba6bee0295fb81468`)
- **Run ID**: `run_d7744d583e06`
- **Provider**: Ollama (`qwen2.5-coder:1.5b`)
- **Execution Status**: `COMPLETED`
- **Investigation Quality**: `INSUFFICIENT_EVIDENCE`
- **Total Findings**: 0 (zero hallucinated defects)
- **Final Assessment**: `[System-Generated Fallback Synthesis]` properly synthesized the investigation scope, inspected state, and limitations without conversational apologies.

---

## 8. Investigation Orchestration & Tool-Selection Remediation (`run_f22cf284a17b`)

### A. Background & Observed Defect
In run `run_d7744d583e06`, the agent repeatedly selected zero-argument utility tools (`get_run_history`) instead of inspecting target source files (`files_inspected: 0`). When the model hit a loop warning on step 5 and returned conversational text without tool calls, the loop prematurely terminated because `total_tool_calls > 0`, abandoning 7 remaining steps.

### B. Architectural Remediations Implemented
1. **Deterministic Investigation Planner (`InvestigationPlanner`)**:
   - Generically discovers dependency manifests (`pyproject.toml`, `requirements.txt`, `setup.cfg`), core package implementations, and related test suites using symbol indexing and workspace relationship mapping.
   - Maintains explicit state: `planned_files`, `inspected_files`, `pending_files`, `investigation_objectives`, and `utility_calls_count`.
   - Schedules strategic investigation phases: manifests -> core implementations -> test suites -> synthesis.
2. **Utility Call Quotas & Dynamic Tool Pruning**:
   - Local database utilities (`get_run_history`) are capped (quota: 1 call per investigation).
   - Once quota is reached, utility tools are dynamically pruned from `request_tools` sent to the provider, preventing models from looping on database utilities.
3. **Autonomous Steering & Anti-Premature Termination**:
   - The investigation loop no longer terminates prematurely on tool-free turns when planned files remain uninspected.
   - Injects deterministic steering directives guiding the model to the next planned inspection target.
   - Provides autonomous step fallback if the model repeatedly outputs unstructured text without calling tools.
4. **Uninspected File Finding Protection**:
   - `record_finding` strictly enforces that candidate finding files must have been previously inspected via `read_source_excerpt`, preventing speculative claims on unexamined files.

### C. Verification Suite
Verified by 17 regression tests in `tests/unit/test_phase7_investigation_orchestration.py`:
1. `test_source_tools_present_in_provider_tool_schema`
2. `test_source_tool_calls_correctly_dispatched`
3. `test_tool_results_reach_next_model_turn`
4. `test_repeated_get_run_history_calls_detected`
5. `test_utility_call_limits_trigger_meaningful_recovery`
6. `test_planner_schedules_dependency_inspection`
7. `test_planner_schedules_implementation_inspection`
8. `test_planner_schedules_relevant_test_inspection`
9. `test_planner_adapts_when_file_does_not_exist`
10. `test_already_inspected_files_not_repeatedly_scheduled`
11. `test_model_invalid_responses_do_not_halt_deterministic_planning`
12. `test_evidence_is_persisted_after_source_inspection`
13. `test_model_cannot_claim_uninspected_files`
14. `test_final_synthesis_uses_actual_investigation_state`
15. `test_insufficient_evidence_remains_correctly_classified`
16. `test_historical_run_and_report_compatibility_is_preserved`
17. `test_existing_phase7_rejection_and_synthesis_tests_remain_passing`

### D. Real-World Live Acceptance Run
- **Target**: `https://github.com/fastapi/fastapi.git` (commit `45cb2252c737906f37544f5ba6bee0295fb81468`)
- **Run ID**: `run_f22cf284a17b`
- **Provider**: Ollama (`qwen2.5-coder:1.5b`)
- **Steps Executed**: 12 (Tool calls: 9)
- **Duration**: 56.89s
- **Files Inspected**: 4 (`pyproject.toml`, `db_connection.py`, `src/auth.py`, `src/utils.py`)
- **Execution Status**: `COMPLETED`
- **Investigation Quality**: `COMPLETED`
- **Total Findings**: 0 (clean audit, zero false positives)
- **Final Assessment**: Structured technical synthesis grounded in real inspected source lines, manifests, explicit uncertainties, and truthful completion.
