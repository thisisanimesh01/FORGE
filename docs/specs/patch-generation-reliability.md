# Patch Generation Reliability & Unified Diff Contract Specification

- **Milestone:** Patch Generation Reliability, Unified Diff Syntax Enforcement & No-Op Prevention
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero target repository mutations, zero commits, zero pushes, zero PRs)
- **Traceability:** Addresses the empty/no-op diff generation failure exposed during Supervised Pilot 01 Attempt 3, satisfying `AGENTS.md` Sections 1, 2, 4, 5, 6, and 7.

---

## 1. Executive Summary & Problem Diagnosis

During Supervised Pilot 01 Attempt 3 (`wf_eng_67006843331e`), structured JSON output generation succeeded and the schema validation passed cleanly. The local model (`qwen2.5-coder:1.5b`) produced valid JSON matching `RepairModelResponseSchema`.

However, repair planning failed with:
```
Patch candidate contains no additions or deletions (empty/no-op diff)
```

### Root Cause Analysis
1. **Ambiguous Column 0 Diff Markers**: In standard unified diff syntax, hunk lines indicate additions with `+` in column 0, removals with `-` in column 0, and unchanged context lines with `' '` (a single leading space). When repairing Python code, lines are indented with whitespace (e.g. 4 or 8 spaces). The model emitted Python code indented with spaces without prefixing additions with `+` (e.g. `    if b == 0:` instead of `+        if b == 0:`). Because every hunk body line began with spaces, the parser counted 0 added lines and 0 removed lines, rejecting the proposal as an empty/no-op diff.
2. **Schema & Prompt Specification Gaps**: In `RepairModelResponseSchema`, the schema description for `unified_diff` was merely `"Unified diff candidate patch"`. In `RepairPlanner._build_prompt`, instructions lacked an explicit specification of column 0 diff markers (`+`, `-`, and `' '` for context lines), leading compact models to treat Python indentation as code rather than diff syntax.
3. **Missing Guidance in Retry Loop**: When the diff validation rejected the patch candidate for containing zero additions and zero deletions, the retry feedback sent to the model merely repeated the error string without explaining how unified diff lines must be marked. The model repeatedly outputted the same indentation pattern across all retries until retries were exhausted.
4. **Unchanged Replacement / No-Op Diff Gap**: Static validation previously allowed hunks where the added lines matched the removed lines exactly, producing zero net change to the target file.

---

## 2. Implemented Architecture & Safety Invariants

### A. Explicit Unified Diff Contract in Schema and Prompt
- **Schema Instruction**: In `app/forge/repair/models.py`, `RepairModelResponseSchema.unified_diff` description was enhanced to explicitly mandate unified diff markers:
  ```python
  unified_diff: str = Field(
      description=(
          "Standard unified diff candidate patch starting with '--- a/<file>\\n+++ b/<file>\\n@@ -start,count +start,count @@'. "
          "Lines to add MUST start with '+', lines to remove MUST start with '-', and unchanged context lines MUST start with ' ' (single space). "
          "Empty diffs or diffs without '+' or '-' markers are forbidden."
      )
  )
  ```
- **Prompt Framing**: In `RepairPlanner._build_prompt`, explicit unified diff syntax requirements are codified:
  - Header must start with `--- a/{target_file}` and `+++ b/{target_file}`.
  - Hunk headers must use `@@ -<start>,<count> +<start>,<count> @@` matching the numbered file context.
  - Every line in the hunk body must begin with `+` for additions, `-` for removals, or `' '` for unchanged context.
  - Plain Python code without `+` or `-` markers is explicitly warned against as an empty/no-op diff.

### B. Strict Static & In-Memory Patch Validation
- **Malformed Diff Line Detection**: In `parse_and_validate_unified_diff`, all hunk body lines are validated to ensure they begin with `+`, `-`, `' '`, `\`, or empty string. Lines beginning with arbitrary characters without diff markers are rejected as malformed hunks.
- **Static Unchanged Replacement Detection**: In `parse_and_validate_unified_diff`, hunks where added lines are identical to removed lines are detected and rejected as unchanged replacement/no-op patches.
- **In-Memory Net Change Verification**: In `validate_patch_applicability`, the in-memory dry-run check verifies that applying the diff to the target file at the pinned base revision produces a genuine net modification (`"".join(current_lines) != orig_text`). Unchanged replacements are strictly rejected.
- **Safe PatchCandidate Construction**: `RepairPlanner.plan_repair` ensures `hunk_count = max(1, last_metrics.get("hunk_count", 1))` and guards `PatchCandidate` instantiation with `try/except`, ensuring invalid candidates fail closed without uncaught model validation exceptions.

### C. Targeted Retry Guidance
- **Contextual Diff Guidance**: In `RepairPlanner.plan_repair`, when diff validation violations occur, targeted guidance is injected into the retry prompt:
  - *No Additions/Deletions*: Informs the model that lines to add must start with `+` in column 0, and that lines starting with spaces are treated as unchanged context lines.
  - *Context Mismatch*: Informs the model to verify line numbers and surrounding context lines against the numbered target file content.
  - *Missing Headers / Malformed Lines*: Details the required `--- a/...`, `+++ b/...`, and `@@ ... @@` header structure.
  - *Unchanged Replacement*: Informs the model that no net change occurred and effective code changes are required.

### D. Provider Isolation & Immutability
- **Target Repository Immutability**: All applicability dry-runs occur strictly in-memory. Target files and Git history remain 100% read-only and untouched.
- **Zero Fallback**: Local Ollama failures fail closed without falling back to cloud providers, mock providers, or unauthorized endpoints.

---

## 3. Verification Suite & Quality Gates

### A. Dedicated Test Suite
Created `tests/unit/test_patch_generation_reliability.py` with 11 deterministic unit tests covering all required scenarios:
1. `test_valid_minimal_unified_diff_accepted`: Valid minimal unified diff against pinned base is accepted and marked `VALIDATED`.
2. `test_context_only_hunk_rejected`: Context-only hunk (0 additions, 0 deletions) is rejected.
3. `test_unchanged_replacement_patch_rejected`: Unchanged replacement / no-op patch is rejected.
4. `test_code_snippet_without_diff_markers_rejected`: Raw code snippet without diff markers is rejected.
5. `test_valid_patch_with_incorrect_hunk_context_rejected`: Valid patch syntax with mismatched context lines is rejected.
6. `test_patch_targeting_unauthorized_file_rejected`: Patch targeting files outside allowed scope is rejected.
7. `test_bounded_retry_succeeds_when_context_only_diff_corrected`: Bounded retry succeeds when provider corrects a context-only diff on subsequent attempt.
8. `test_exhausted_retries_fail_closed`: Persistent invalid diffs fail closed when retries are exhausted.
9. `test_candidate_actually_changes_pinned_source_and_resolves_defect`: Candidate patch genuinely alters pinned source and resolves the reproduction test in an isolated scratch workspace.
10. `test_original_fixture_and_git_history_remain_unchanged`: Confirms target repository files, git status, and HEAD SHA are strictly unchanged.
11. `test_local_provider_failure_cannot_trigger_cloud_or_mock_fallback`: Provider connection failures fail closed without triggering cloud fallback.

### B. Full Test Suite & Benchmark Results
- **Full Test Suite**: 877 passed, 2 skipped across entire repository.
- **Repair Benchmarks**: 11/11 fixtures passed (100% accuracy, 0 false fixes, 100% scope compliance).
- **Review Benchmarks**: 10/10 fixtures passed.
- **Docker Integration Tests**: 2/2 passed with live container isolation.
- **Code Hygiene**: `python3 -m compileall` passed; `git diff --check` passed with 0 errors.
- **Fixture Integrity**: Verified `/private/tmp/forge_pilot_01` clean at commit `0ba1653f3607e1dd1d73b4bc07e0b16273e6c777` with identical file SHA-256 hashes.
- **Audit Store**: All 6 runs and 4 repair plans in SQLite preserved without modification.
