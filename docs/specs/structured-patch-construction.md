# Structured Patch Construction Specification

- **Milestone:** Structured Patch Construction & Deterministic Search-and-Replace Synthesis
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero target repository mutations, zero commits, zero pushes, zero PRs)
- **Traceability:** Addresses the diff marker omission pattern in compact local LLMs (`qwen2.5-coder:1.5b`) exposed during Pilot 01 Attempt 4, satisfying `AGENTS.md` Sections 1, 2, 4, 5, 6, and 7.

---

## 1. Executive Summary & Problem Diagnosis

During Supervised Pilot 01 Attempt 4 (`wf_eng_5f6c97122545`), FORGE evaluated local repair proposals with strict unified diff validation. Forensics confirmed that `qwen2.5-coder:1.5b` systematically emitted Python code lines indented with whitespace instead of prefixing additions with `+` in column 0, despite explicit system prompts and schema definitions.

### Root Cause Analysis
1. **Model Capability Limitation on Column 0 Diff Syntax**: Compact models (e.g. 1.5B parameters) are trained extensively on raw Python source code where indentation represents block structure. When asked to generate unified diffs, the model naturally writes Python code with standard indentation, treating column 0 as leading whitespace rather than diff syntax.
2. **Fragility of Model-Authored Unified Diffs**: Requiring models to calculate diff headers (`--- a/...`, `+++ b/...`), compute line counts (`@@ -start,count +start,count @@`), and maintain diff markers (`+`, `-`, ` `) places unnecessary syntactic overhead on the model.
3. **Structured Search-and-Replace Superiority**: Local models are exceptionally reliable at quoting verbatim code blocks to replace (`search_block`) and writing clean substitution code (`replace_block`).
4. **Deterministic Host Construction**: Python's standard `difflib.unified_diff` deterministically synthesizes 100% syntactically valid unified diffs with exact headers, hunk line counts, and diff markers from validated search-and-replace blocks.

---

## 2. Implemented Architecture & Safety Invariants

### A. Strict Structured Patch Proposal Schema
- **`StructuredPatchProposalSchema`**: Defined in `app/forge/repair/models.py`:
  - `target_file: str`: Relative path of file to modify within authorized scope.
  - `search_block: str`: Exact verbatim lines from the original target file to be replaced.
  - `replace_block: str`: Replacement lines to substitute in place of the search block.
  - `change_rationale: str`: Concise explanation of the code modification rationale.
- **Dual Representation in `RepairModelResponseSchema`**: Updated `RepairModelResponseSchema` to support both structured search/replace fields (`search_block`, `replace_block`, `change_rationale`) and legacy `unified_diff`, ensuring 100% backward compatibility with existing tests and benchmarks.

### B. Deterministic Unified Diff Constructor
- **`construct_unified_diff_from_search_replace`**: Implemented in `app/forge/repair/contracts.py`:
  - **Path & Scope Containment**: Validates relative path, rejects traversal (`..`, `/`), rejects test files (`is_test_path`), checks protected files (`ProtectedPathPolicy.is_protected`), and enforces declared allowed scope (`allowed_files`).
  - **Base Revision Pinning**: Verifies that the target repository's current revision matches `base_commit_sha`.
  - **Exact Verbatim Matching (`count == 1`)**: Rejects missing search blocks (`count == 0`) and ambiguous search blocks (`count > 1`) with targeted actionable diagnostics. No guessing or fuzzy indentation shifts are permitted.
  - **No-Op Prevention**: Rejects empty/whitespace blocks and unchanged replacements (`search_block == replace_block`).
  - **Trusted Diff Synthesis**: Uses Python `difflib.unified_diff` with `keepends=True` and `fromfile=f"a/{norm_target}"`, `tofile=f"b/{norm_target}"` to generate standard unified diffs.
  - **Downstream Validation**: Passes constructed diffs through `parse_and_validate_unified_diff` and in-memory dry-run `validate_patch_applicability` (including AST syntax validation).

### C. Repair Planner & Prompt Updates
- **Prompt Framing**: `RepairPlanner._build_prompt` instructs models to formulate structured search-and-replace proposals with verbatim lines from the target file without line number prefixes.
- **Planner Integration**: In `RepairPlanner.plan_repair`, when `search_block` and `replace_block` are provided, `construct_unified_diff_from_search_replace` deterministically builds the unified diff, feeds it into downstream verification, and builds `PatchCandidate`.
- **Targeted Retry Diagnostics**: Injects actionable guidance into retry loops for search block not found, ambiguous matches, unchanged replacements, or empty blocks.

---

## 3. Verification Suite & Quality Gates

### A. Dedicated Test Suite
Created `tests/unit/test_structured_patch_construction.py` with 13 deterministic unit tests:
1. `test_valid_minimal_replacement_produces_genuine_diff_and_validated`: Minimal search-and-replace proposal produces genuine unified diff and `RepairPlanStatus.VALIDATED`.
2. `test_exact_indentation_preservation`: Verifies 4-space, 8-space, and nested indentations are preserved verbatim.
3. `test_missing_search_block_rejected`: Fails closed when search block is absent (`count == 0`).
4. `test_ambiguous_multiply_matching_search_block_rejected`: Fails closed when search block matches multiple locations (`count > 1`).
5. `test_empty_or_whitespace_blocks_rejected`: Rejects empty or whitespace-only search or replace blocks.
6. `test_unchanged_replacement_rejected`: Rejects identical search and replace blocks (no-op).
7. `test_unauthorized_paths_and_path_traversal_rejected`: Rejects `..`, absolute paths, protected files, and test files.
8. `test_stale_base_revision_rejected`: Rejects base commit revision mismatches.
9. `test_malformed_structured_output_and_bounded_response_size`: Enforces 50KB size cap and valid schema structure.
10. `test_generated_patch_parses_and_applies_cleanly_in_memory`: Confirms constructed diffs parse and apply cleanly with valid Python AST.
11. `test_replacement_resolves_calculator_reproduction_in_scratch`: Verifies candidate patch resolves defect reproduction in an isolated scratch workspace.
12. `test_provider_failure_fails_closed_without_cloud_fallback`: Confirms provider errors fail closed without falling back to cloud.
13. `test_target_repository_and_git_history_remain_immutable`: Proves target repo, worktree, and Git history remain 100% untouched.

### B. Full Test Suite & Benchmark Results
- **Full Test Suite**: 890 passed, 2 skipped across entire repository.
- **Repair Benchmarks**: 11/11 fixtures passed.
- **Review Benchmarks**: 10/10 fixtures passed.
- **Docker Integration Tests**: 2/2 passed with live container isolation.
- **Code Hygiene**: `python3 -m compileall` passed; `git diff --check` passed with 0 errors.
- **Fixture Integrity**: Verified `/private/tmp/forge_pilot_01` clean at commit `0ba1653f3607e1dd1d73b4bc07e0b16273e6c777` with identical file SHA-256 hashes.
