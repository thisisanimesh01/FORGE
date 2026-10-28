# Local Ollama Structured Output Reliability Specification

- **Milestone:** Local Ollama Structured Output & Bounded Correction Reliability
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero target repository mutations, zero commits, zero pushes, zero PRs)
- **Traceability:** Addresses the structured JSON output and trailing-comma syntax error exposed during Supervised Pilot 01 Attempt 2, satisfying `AGENTS.md` Sections 1, 2, 4, 5, 6, and 7.

---

## 1. Executive Summary & Problem Diagnosis

During Supervised Pilot 01 Attempt 2 (`wf_eng_7c4f277c67f7`), root-cause grounding and candidate target resolution successfully identified `calculator.py` as the implementation target and framed `tests/test_calculator.py` as a read-only test oracle. The defect was successfully reproduced inside an isolated Docker container with network disabled.

However, repair planning failed with:
```
Model response was not valid JSON or could not be parsed
```
Detailed inspection revealed:
1. **Unconstrained Generation**: In `OpenAICompatibleProvider`, requests to the OpenAI-compatible `/v1/chat/completions` endpoint did not pass `response_format`. The provider lacked structured output support on the wire.
2. **Trailing-Comma Syntax in Model Output**: The local model (`qwen2.5-coder:1.5b`) correctly diagnosed the root cause and generated a valid unified diff for `calculator.py`, but emitted JSON with a trailing comma after the `"unified_diff"` string (`"unified_diff": "...",}`). Standard Python `json.loads` adheres strictly to RFC 8259 and rejects trailing commas with `json.decoder.JSONDecodeError`.
3. **Loss of Diagnostic Context in Retries**: The previous JSON extraction function swallowed the parse exception and merely reported `Model response was not valid JSON or could not be parsed`. The model was never informed of the specific syntax violation (e.g. trailing comma at line/column), leading the model to repeat the identical syntax flaw across all attempts until retries were exhausted.

---

## 2. Implemented Architecture & Safety Invariants

### A. Structured Output / JSON Schema Integration
- **Pydantic Model Updates**: Added `response_format: Optional[Dict[str, Any]] = None` to `LLMRequest` in `app/forge/llm/models.py`.
- **Wire Payload Serialization**: `OpenAICompatibleProvider._build_payload` serializes `request.response_format` when present.
- **Strict Repair Schema**: `build_repair_response_format()` dynamically derives the `json_schema` response format directly from `RepairModelResponseSchema.model_json_schema()`, enforcing structured schema-constrained generation at the provider level for Ollama and OpenAI-compatible endpoints.
- **Graceful Single-Provider 400 Downgrade**: If an older endpoint rejects `json_schema` with HTTP 400, `OpenAICompatibleProvider` attempts a graceful downgrade to `{"type": "json_object"}` or drops `response_format` on the same provider without falling back to cloud or alternative providers.

### B. Strict Parsing with Diagnostic Feedback
- **Strict RFC 8259 Compliance**: Implemented `parse_json_payload(raw_content: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]`.
  - Prohibits unsafe string replacements, regex stripping of trailing commas, or permissive eval-based parsers.
  - Extracts JSON safely from direct strings, markdown code fences (` ```json `), or outermost matching `{ ... }` delimiters.
  - Captures exact parse failure diagnostics (line, column, error reason) and formats an explicit, non-secret diagnostic message (e.g., `Invalid JSON syntax: Model response was not valid JSON (...). Trailing commas before '}' or ']' are forbidden in JSON.`).
- **Secret Redaction**: All diagnostics and error messages are sanitized with `redact_secrets()`.

### C. Tightly Bounded Same-Provider Correction Retry
- **Bounded Retry Cap**: `RepairPlanner` clamps `max_retries` to `MAX_REPAIR_PLANNER_RETRIES = 3` (total attempts <= 4).
- **Time Budget Bound**: Enforces `max_planning_seconds = MAX_PLANNING_SECONDS` (120.0s). Evaluated before and immediately after every generation call. If the budget expires, planner fails closed with a clear violation.
- **Response Size Bound**: Enforces `MAX_RESPONSE_CHARS = 50_000`. If a model returns an oversized response, it is rejected and bounded.
- **Output Token Bound**: Sets `max_tokens = 4096` in `LLMRequest`.
- **Explicit Error Feedback**: If JSON parsing fails, the exact sanitized syntax error is provided to the local model, instructing it to avoid trailing commas.
- **Revalidation Invariant**: Every corrected response is revalidated from the beginning through all safety gates: response length, JSON syntax, Pydantic schema, scope containment, test file prohibition, protected path policy, unified diff budget, and in-memory patch applicability.

### D. Provider Isolation & Fail-Closed Policy
- **No Cloud Fallback**: Failures strictly remain on the local Ollama provider. No automatic routing to cloud, mock, or alternative providers.
- **Fail Closed**: If all retries fail or are exhausted, the workflow transitions safely to `repair_failed`.

---

## 3. Verification Suite & Quality Gates

### A. Dedicated Test Suite
Created `tests/unit/test_structured_output_reliability.py` with 17 deterministic unit tests:
1. `test_build_repair_response_format_structure`: Verifies `json_schema` structure against `RepairModelResponseSchema`.
2. `test_parse_json_payload_valid_direct`: Direct JSON parsing.
3. `test_parse_json_payload_valid_code_fence`: Markdown code fence parsing.
4. `test_parse_json_payload_trailing_comma_diagnosed_not_silently_fixed`: Ensures trailing commas are rejected and diagnosed without permissive rewriting.
5. `test_parse_json_payload_empty_and_oversized`: Tests empty and oversized rejection bounds.
6. `test_valid_json_repair_plan_sends_response_format`: Verifies `response_format` is sent in `LLMRequest`.
7. `test_trailing_comma_rejected_then_corrected_via_explicit_retry`: Simulates trailing-comma failure followed by successful correction via diagnostic feedback.
8. `test_trailing_comma_fails_closed_when_retries_exhausted`: Verifies fail-closed rejection when trailing commas persist.
9. `test_schema_invalid_syntactically_valid_json_rejected`: Rejects syntactically valid JSON missing required fields.
10. `test_corrected_output_still_strictly_validated_for_scope`: Ensures a corrected response still fails if it targets unauthorized or test files.
11. `test_corrected_output_still_strictly_validated_for_hunk_context`: Ensures a corrected response still fails if context lines do not match.
12. `test_planning_timeout_budget_fails_closed`: Verifies wall-clock time budget enforcement.
13. `test_oversized_response_rejected_bounded`: Verifies response length bounding.
14. `test_retry_budget_hard_cap`: Verifies max retries clamp.
15. `test_no_secrets_in_diagnostics_or_violations`: Verifies secret redaction in diagnostics.
16. `test_openai_compatible_provider_includes_response_format`: Tests provider payload serialization.
17. `test_openai_compatible_provider_graceful_400_downgrade`: Tests graceful single-provider HTTP 400 downgrade.

### B. Full Test Suite & Benchmark Results
- **Full Test Suite**: 866 passed, 2 skipped (safe opt-in Docker skips).
- **Repair Benchmarks**: 11/11 fixtures passed (100% outcome accuracy, 0 false fixes, 100% scope compliance).
- **Review Benchmarks**: 10/10 fixtures passed.
- **Code Quality**: `compileall` passed, `git diff --check` passed with 0 warnings/errors.
