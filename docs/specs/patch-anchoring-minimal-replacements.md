# Deterministic Patch Anchoring and Minimal Replacements Specification

- **Milestone:** Deterministic Patch Anchoring, Statement-Level Search-and-Replace & Verbatim Context Delivery
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero target repository mutations, zero commits, zero pushes, zero PRs)
- **Traceability:** Addresses the root cause identified in Supervised Pilot 01 Attempt 6 where local LLM (`qwen2.5-coder:1.5b`) attempted whole-function replacement while omitting docstrings, failing exact search block matching. Satisfies `AGENTS.md` Sections 1, 2, 3, 4, 5, 6, and 7.

---

## 1. Executive Summary & Problem Diagnosis

During Supervised Pilot 01 Attempt 6, FORGE evaluated structured proposal synthesis using local Ollama (`qwen2.5-coder:1.5b`). While the wire-schema fix ensured that `search_block` and `replace_block` were emitted correctly on the wire without key omission, the local model attempted to replace an entire function (`def divide(...) -> float:` through `return a / b`), but omitted the function's multi-line docstring and unchanged comments from the `search_block`.

Because FORGE strictly mandates exact verbatim matching without fuzzy trimming or whitespace heuristics, `orig_text.count(search_block)` returned `0`, rejecting the patch candidate. Furthermore, the generic retry diagnosis (`"search_block not found in target file"`) did not identify that the model had omitted intervening lines or that it should scope its replacement down to the minimal statement-level defect.

### Root Cause Analysis
1. **Whole-Function Replacement Temptation**: Without explicit prompt constraints directing the model to replace only 1–3 faulty lines, small models naturally attempt to reproduce the entire function block.
2. **Docstring and Comment Omission**: When regenerating multi-line functions, compact models frequently omit docstrings, license headers, or comments, producing a `search_block` that does not match the file contents verbatim.
3. **Context Presentation vs. Anchoring**: While line-numbered excerpts help human and model comprehension, models sometimes attempt to copy line number prefixes or struggle to identify the exact unique anchor lines.
4. **Generic Rejection Diagnostics**: Rejection messages previously reported `"search_block not found"` without detecting whether the boundary lines existed but intervening lines were omitted, missing an opportunity to guide the model toward minimal statement replacement on retry.

---

## 2. Implemented Architecture & Safety Invariants

### A. Minimal Replacement Contract
- **Statement-Level Scoping**: Updated prompt contracts and repair instructions to mandate the smallest sufficient search/replace block (preferably 1–3 lines, e.g. the faulty `if` check or return statement) rather than whole functions or classes.
- **Explicit Prohibition on Omitting Intervening Lines**: Instructs models never to summarize, truncate, or omit docstrings or comments if a larger block is selected.
- **Reference-Only Line Numbers**: Prompt context explicitly establishes that line numbers are informational coordinates only, forbidding models from including line-number prefixes in search or replace blocks.

### B. Defect Extraction & Focused Anchor Framing
- **Targeted Line Number Extraction**: Added `_extract_defect_line_numbers` to `app/forge/repair/planner.py`, inspecting stack traces and reproduction evidence for frame numbers matching the target file.
- **Focused Anchor Delivery**: Added `_build_focused_anchor_section` to `app/forge/repair/planner.py`. Injects an explicit reference section `=== RELEVANT FAULTY SOURCE ANCHOR (DATA ONLY - REFERENCE) ===` into the prompt containing:
  - Exact verbatim code lines surrounding the defect (un-numbered, ready for verbatim copying).
  - Explicit line range notice.
- **Verbatim Context Delivery**: The model receives the exact characters present in the target repository file at the pinned base revision.

### C. Advanced Search Block Validation & Diagnosis
In `app/forge/repair/contracts.py`:
- **Line-Number Prefix Detection**: When `search_block` is not found, checks `re.search(r"^\s*\d+[\s:|]", search_block)` to detect line-number prefixes and provides targeted feedback: `"search_block appears to contain line-number prefixes ... Provide only the exact source code lines"`.
- **Omitted Intervening Lines Detection**: Splits `search_block` into non-empty stripped lines. If the first line and last line both match in `orig_text` with non-negative indices in correct sequence, diagnoses:
  > *"Beginning and ending lines of the search block were found in the file, but intervening lines (e.g. docstrings, comments, or unchanged statements) appear to have been omitted. Do NOT omit intervening source lines. Target a smaller, statement-level block (1-3 lines) containing only the exact lines to modify."*
- **Verbatim Matching Invariant**: Verbatim matching (`orig_text.count(search_block) == 1`) is strictly preserved. No fuzzy matching, whitespace normalization, or comment stripping is performed on the source code.

### D. Targeted Planner Retry Guidance
In `app/forge/repair/planner.py`:
- If patch synthesis fails due to a missing or ambiguous search block, the retry prompt automatically includes a focused excerpt of the target file around the defect location.
- Explicitly guides the model to copy 1–3 exact lines from the provided excerpt.

---

## 3. Verification Suite & Quality Gates

### A. Dedicated Test Suite
Created `tests/unit/test_patch_anchoring_and_minimal_replacements.py` with 11 focused tests:
1. `test_minimal_unique_statement_replacement_inside_function_with_docstring`: Verifies that a 2-line statement replacement inside a function with docstring matches uniquely and applies cleanly.
2. `test_larger_search_block_omitting_intervening_lines_rejected`: Verifies that attempting to span function signature and body while omitting the docstring is rejected with explicit intervening-lines diagnostic.
3. `test_missing_and_ambiguous_search_blocks`: Rejects non-existent search blocks (`count == 0`) and ambiguous duplicate blocks (`count > 1`).
4. `test_exact_indentation_and_newline_preservation`: Verifies exact indentation (4-space vs 8-space) is strictly preserved without fuzzy normalization.
5. `test_stale_source_revisions_and_unauthorized_paths`: Confirms stale base commit revisions and path traversal attempts are rejected.
6. `test_no_op_replacements_and_malformed_proposals`: Rejects empty search blocks, unchanged replacements, and proposals with line numbers.
7. `test_successful_deterministic_diff_generation_and_in_memory_application`: Verifies end-to-end diff construction, parsing, and in-memory AST validation.
8. `test_calculator_defect_fixed_in_isolated_scratch_workspace`: Verifies full end-to-end patch application in isolated scratch workspace and confirms reproduction test passes.
9. `test_original_fixture_and_historical_audit_records_unchanged`: Verifies the immutable test fixture `/private/tmp/forge_pilot_01` and SQLite audit records are completely untouched.
10. `test_provider_failures_without_cloud_or_mock_fallback`: Confirms provider connection failures fail closed without cloud fallback.
11. `test_planner_retry_loop_provides_focused_anchor_excerpt`: Confirms retry loop injects focused anchor excerpts when search block matching fails.

### B. Full Test Suite & Benchmark Results
- **Full Test Suite**: 913 passed, 2 skipped across entire repository in 75.14s.
- **Repair Benchmarks**: 11/11 fixtures passed.
- **Review Benchmarks**: 10/10 fixtures passed.
- **Docker Integration Tests**: 2/2 passed with live container isolation.
- **Code Hygiene**: `python3 -m compileall` passed; `git diff --check` passed with 0 errors.
- **Fixture Integrity**: Verified `/private/tmp/forge_pilot_01` clean at commit `0ba1653f3607e1dd1d73b4bc07e0b16273e6c777` with identical SHA-256 hashes (`calculator.py`: `aca03f0e...`, `test_calculator.py`: `ecff9cc9...`).
