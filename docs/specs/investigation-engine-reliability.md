# Investigation Engine Tool-Call Reliability & Anti-Premature Completion Specification

- **Milestone:** Investigation Engine Reliability Remediation
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero target repository mutations, zero commits, zero pushes, zero PRs)
- **Traceability:** Addresses the root causes discovered during real-world audit `run_5a4b61a62a02` against `apache/hamilton`, satisfying `AGENTS.md` Sections 1, 2, 4, 5, 6, and 7.

---

## 1. Executive Summary & Root Cause Analysis

During a real-world repository investigation against `https://github.com/apache/hamilton.git` (`run_5a4b61a62a02`) using local Ollama with `qwen2.5-coder:1.5b`:
- The LLM returned a JSON request for `get_repository_map` in ordinary message content.
- `OpenAICompatibleProvider` returned an empty `tool_calls` list because it only inspected the OpenAI `message.tool_calls` wire array.
- `InvestigationLoop.run()` unconditionally treated `not response.tool_calls` as a final answer.
- The investigation terminated on Turn 1 with 0 tool calls, 0 findings, and 0 evidence records, yet was recorded in SQLite as `COMPLETED`.

This specification formalizes the two-tier architectural defense implemented to resolve this failure.

---

## 2. Implemented Architecture & Safety Invariants

### A. Safe Multi-Tier Content-Based Tool Extraction (`OpenAICompatibleProvider`)
In `app/forge/llm/openai.py`:
- When structured wire `tool_calls` are absent and text `content` is present, `extract_content_tool_calls` attempts safe extraction across:
  1. `<tool_call> ... </tool_call>` tags.
  2. Markdown code fences (````json ... ````).
  3. Direct JSON objects and outer `{ ... }` / `[ ... ]` delimiters.
- **Authorized Tool Allowlist:** Candidate tool names are strictly matched against `request.tools`. Unknown tool names (e.g. `execute_shell`) are rejected with an explicit diagnostic error.
- **Strict Parameter Schema Validation:** Arguments are validated against `ToolDefinition.parameters` via `validate_tool_arguments()`:
  - Required parameters must be present and non-null.
  - Types (`string`, `integer`, `boolean`, `array`, `object`) are strictly validated. Valid numeric strings are safely normalized to integers.
  - Enum constraints (e.g. `check_id` in `run_sandbox_check`, `symbol_type` in `search_symbols`) are strictly enforced.
- **Ordinary Prose Protection:** Arbitrary JSON objects without tool identification keys (`name`, `tool`, `function`) are ignored and not misidentified as tool calls.
- **Provider Isolation:** Tools are never executed inside the provider. The provider emits typed `ToolCallRequest` objects for execution by `InvestigationLoop`.

### B. Investigation Loop Invariant Enforcement (`InvestigationLoop`)
In `app/forge/investigation/loop.py`:
- **Diagnostic Correction Turn:** If tool extraction detects an invalid or malformed tool call (`response.extraction_error`), the loop does not terminate. It provides non-secret diagnostic feedback and authorized tool definitions back to the model, offering a bounded opportunity to re-issue a valid call within `self.max_steps`.
- **Step 1 Zero-Tool Guard:** If the model outputs plain text on Turn 1 without executing any tools or discovering findings, the loop prompts the model with available tools to commence the audit.
- **Anti-Premature Completion Invariant:** An investigation is strictly prohibited from finishing as `COMPLETED` if zero tools were executed and zero findings exist. If an investigation terminates without audit activity, it is truthfully marked `FAILED` with `error_message = "Investigation terminated without audit activity: Zero tools executed and zero findings recorded."`.
- **Bounded Budgets:** Retries are strictly bounded by `max_steps` (turns), `max_total_tool_calls` (50), `max_wall_clock_seconds` (300s), and repeated tool-call loop detection (maximum 2 identical calls).

---

## 3. Verification Suite & Quality Gates

A dedicated unit test suite was implemented in `tests/unit/test_investigation_reliability.py` covering all 15 operational requirements:
1. `test_native_structured_tool_calls_preserved`: OpenAI `tool_calls` wire format handled.
2. `test_direct_json_tool_call_extraction`: Direct JSON parsed into `ToolCallRequest`.
3. `test_markdown_fenced_json_extraction`: Markdown code fence JSON extracted.
4. `test_tool_call_tag_extraction`: `<tool_call>` tags extracted.
5. `test_unknown_tool_name_rejected`: Unauthorized tool names rejected.
6. `test_invalid_json_rejected_safely`: Malformed JSON safely reported without crashing.
7. `test_missing_required_arguments_rejected`: Missing required arguments rejected against schema.
8. `test_invalid_argument_type_rejected`: Type mismatches rejected against schema.
9. `test_invalid_enum_argument_rejected`: Disallowed enum values rejected.
10. `test_ordinary_explanatory_json_not_mistaken_for_tool_call`: Non-tool JSON treated as prose.
11. `test_extracted_tool_call_dispatched_and_recorded`: Extracted tool calls executed and logged to SQLite.
12. `test_ambiguous_first_response_cannot_produce_false_completion`: Turn 1 zero-tool responses fail honestly.
13. `test_maximum_turn_budget_enforced`: `max_steps` strictly halts loop as `max_steps_exceeded`.
14. `test_legitimate_final_assessment_completes_investigation`: Legitimate assessment after tools finishes as `completed`.
15. `test_malformed_tool_call_gets_correction_opportunity`: Diagnostic feedback enables successful correction.

Full test suite: 1,013 passed, 1 skipped (0 failures).
