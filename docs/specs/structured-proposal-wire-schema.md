# Structured Proposal Wire-Schema Reliability Specification

- **Milestone:** Structured Proposal Wire-Schema Reliability & Patch Representation Contract
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero target repository mutations, zero commits, zero pushes, zero PRs)
- **Traceability:** Addresses the omission of patch representation keys by local LLM grammar engines during Pilot 01 Attempt 5, satisfying `AGENTS.md` Sections 1, 2, 4, 5, 6, and 7.

---

## 1. Executive Summary & Problem Diagnosis

During Supervised Pilot 01 Attempt 5, FORGE requested a structured repair proposal from local Ollama (`qwen2.5-coder:1.5b`) using an OpenAI-compatible `response_format` JSON schema. Although the model produced syntactically valid JSON containing all required diagnostic fields (`root_cause`, `proposed_change`, `target_file`, `expected_behavior`, `verification_plan`), it omitted `search_block`, `replace_block`, and `unified_diff` entirely from the generated payload. As a result, Pydantic post-validation rejected the proposal because neither patch representation was present.

### Root Cause Analysis
1. **Pydantic Default Schema Behavior**: In Pydantic v2, fields with default values (`default=None`) are not included in `schema["required"]` by `model_json_schema()`. Consequently, only the diagnostic fields were marked as `"required"` on the wire schema sent to the provider.
2. **Grammar Engine Minimization**: Local LLM structured-output and grammar-constrained engines (such as Ollama / llama.cpp GBNF) strictly constrain tokens to the fields declared in `required`. Optional fields not present in `required` can be freely omitted by the grammar engine.
3. **Pydantic vs. Wire Contract Mismatch**: Python-level post-validation (`validate_patch_proposal_representation`) required that at least one patch proposal representation be provided, but the wire JSON schema did not mandate that the model output those keys.
4. **Ambiguity and Partial Proposal Risk**: The wire and model contracts previously did not reject ambiguous proposals (providing both structured search/replace and unified diff) or partial structured proposals (providing `search_block` without `replace_block`).

---

## 2. Implemented Architecture & Safety Invariants

### A. Explicit Wire Schema Contract via `json_schema_extra`
- **Schema Customization**: Defined `configure_repair_response_schema(schema: Dict[str, Any]) -> None` in `app/forge/repair/models.py`.
- **Enforced Key Presence**: Programmatically ensures `search_block`, `replace_block`, and `unified_diff` are included in `schema["required"]`.
- **Nullable Wire Types**: Because these fields are typed as `Optional[str] = Field(default=None, ...)`, Pydantic generates `anyOf: [{"type": "string"}, {"type": "null"}]`. Declaring them in `required` forces the grammar engine to emit the keys on the wire (with either string or `null` values), eliminating key omission while maintaining typing flexibility.
- **Strict Response Format**: `build_repair_response_format()` outputs this updated schema under `json_schema` with `strict: True`.

### B. Mutual Exclusivity and Completeness Validation
Updated `RepairModelResponseSchema.validate_patch_proposal_representation` to enforce:
1. **Rejection of Missing Representations**: If neither structured blocks (`search_block` and `replace_block`) nor `unified_diff` are provided with non-empty strings, raises `ValueError("Missing patch representation: ...")`.
2. **Rejection of Partial Proposals**: If `search_block` is non-empty while `replace_block` is omitted or empty (or vice-versa), raises `ValueError("Partial structured proposal: ...")`.
3. **Rejection of Ambiguous Representations**: If BOTH structured blocks (`search_block` + `replace_block`) AND `unified_diff` are provided, raises `ValueError("Ambiguous patch representation: ...")`.
4. **Backward Compatibility**: Python `model_validate` accepts legacy payloads that omit `search_block` and `replace_block` keys entirely, as long as `unified_diff` is present and valid.

### C. Planner Prompting & Targeted Retry Guidance
- **Prompt Specification**: `RepairPlanner._build_prompt` updates the example JSON payload to explicitly specify `"unified_diff": null` alongside `search_block` and `replace_block`, instructing the model to provide non-null structured blocks and null `unified_diff`.
- **Targeted Feedback in Retry Loop**: If `model_validate` fails with schema errors, `RepairPlanner.plan_repair` inspects the diagnostic error and injects targeted, actionable guidance:
  - *Missing patch representation*: Injects explicit instructions to output non-null `search_block` and `replace_block` and `unified_diff: null`.
  - *Partial structured proposal*: Injects guidance that both `search_block` and `replace_block` must be provided together.
  - *Ambiguous patch representation*: Injects guidance to choose exactly one representation.

---

## 3. Verification Suite & Quality Gates

### A. Dedicated Test Suite
Created `tests/unit/test_structured_proposal_wire_schema.py` with 12 deterministic unit tests:
1. `test_wire_schema_properties_and_required_fields`: Confirms `search_block`, `replace_block`, and `unified_diff` are present in `schema["required"]` and typed with `anyOf: [string, null]`.
2. `test_valid_structured_proposal_accepted`: Confirms structured proposals with `unified_diff: None` validate cleanly.
3. `test_valid_legacy_unified_diff_accepted`: Confirms legacy diffs with structured blocks as `None` validate cleanly.
4. `test_attempt_5_failure_regression_omitted_all_patches_rejected`: Replicates Pilot 01 Attempt 5 payload and confirms targeted rejection for missing patch representation.
5. `test_partial_structured_proposal_rejected`: Confirms rejection of proposals containing only `search_block`, only `replace_block`, or whitespace replacements.
6. `test_ambiguous_patch_representation_rejected`: Confirms rejection of proposals containing both structured blocks and unified diffs.
7. `test_planner_retry_loop_corrects_missing_patch_representation`: Confirms planner feeds back targeted guidance and succeeds on retry.
8. `test_planner_retry_loop_corrects_partial_structured_proposal`: Confirms planner guides model to complete partial structured proposals.
9. `test_planner_retry_loop_corrects_ambiguous_representation`: Confirms planner guides model to resolve ambiguous representation on retry.
10. `test_planner_fails_closed_when_schema_retries_exhausted`: Confirms planner fails closed with rejected plan when retries are exhausted.
11. `test_provider_failure_fails_closed_without_cloud_fallback`: Confirms provider exceptions fail closed without falling back to cloud.
12. `test_backward_compatibility_model_validate_with_missing_keys`: Confirms older payloads missing keys in dict validate cleanly.

### B. Full Test Suite & Benchmark Results
- **Full Test Suite**: 902 passed, 2 skipped across entire repository in 54.23s.
- **Repair Benchmarks**: 11/11 fixtures passed.
- **Review Benchmarks**: 10/10 fixtures passed.
- **Docker Integration Tests**: 2/2 passed with live container isolation.
- **Code Hygiene**: `python3 -m compileall` passed; `git diff --check` passed with 0 errors.
- **Fixture Integrity**: Verified `/private/tmp/forge_pilot_01` clean at commit `0ba1653f3607e1dd1d73b4bc07e0b16273e6c777` with identical file SHA-256 hashes.
