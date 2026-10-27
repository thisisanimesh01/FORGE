# Repair Planner Reliability Milestone Specification

- **Milestone:** Repair Planner Reliability & Grounded Patch Verification
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Zero repository mutations, zero commits, zero pushes, zero PRs)
- **Commit Message:** `feat(repair): improve root-cause grounding, diff applicability, and scope safety in repair planner`
- **Traceability:** Addresses the root causes exposed during Supervised Pilot 01, satisfying `AGENTS.md` Sections 1, 2, 3, 4, 5, and 7.

---

## 1. Executive Summary & Problem Diagnosis

During Supervised Pilot 01, a workflow targeting a synthetic Python repository with a division-by-zero defect failed during repair planning and patch verification. A comprehensive investigation revealed several compounding architectural gaps:

1. **Target Selection & Intake Scope**: When an intake was constructed from a failing test specification (`create_intake_from_test_spec`), the intake's `affected_files` defaulted to `[clean_test_file]`. Downstream, the repair planner naively selected `target_file = finding.affected_files[0]`, commanding the model to edit the test file rather than the implementation file where the defect resided.
2. **Context Framing & Hallucination**: The planner passed only the test file as context, commanding the model to modify only that file. Lacking access to the implementation code, the model hallucinated that the test method contained the defect.
3. **Context Line Mismatch**: The model produced a unified diff starting at line 1 (`import unittest`), omitting the target file's leading docstring.
4. **Premature Status Validation**: `parse_and_validate_unified_diff` only validated unified diff regex headers and budget limits; it did not verify that context lines matched the target file on disk at the base commit revision, nor did it reject empty/no-op diffs. Consequently, the invalid diff was marked `VALIDATED`. When the verifier subsequently ran in the disposable workspace, `apply_unified_diff_to_file` crashed with a context mismatch error.
5. **Single-Attempt Failure**: Any error in model formatting immediately terminated the planning phase without a feedback loop to allow the local model to correct context line numbers or syntax errors.

---

## 2. Implemented Architecture & Safety Invariants

### A. Candidate Target Resolution & Test/Impl Discrimination
- **Test Path Classifier**: Implemented `is_test_path(path)` identifying test directories (`tests/`, `test/`) and test filename patterns (`test_*.py`, `*_test.py`).
- **Target Resolution**: Implemented `resolve_candidate_targets(repo_path, finding, evidence_records)`:
  - Prioritizes explicitly declared non-test implementation files.
  - Discovers candidate implementation files from reproduction evidence stack traces (`File ".../mod.py", line N`).
  - Analyzes test file ASTs for imported project modules.
  - Correlates finding titles and descriptions against repository `SymbolIndex`.

### B. Scope Restriction & Test Modification Invariant
- **Test File Immunity**: Modifying test files to mask defects is strictly forbidden. If an intake restricts scope solely to test files and an implementation defect is discovered, FORGE refuses to silently modify either the test file or the implementation file.
- **Fail-Closed Rejection**: The planner returns a `REJECTED` plan with an explicit explanation:
  ```
  Scope restriction: Defect identified in implementation file '<impl>', but finding scope is strictly restricted to test files (<tests>). Modifying test files to mask defects is forbidden, and expanding scope to implementation files without authorization is forbidden. An authorized revised intake expanding scope to '<impl>' is required.
  ```

### C. Ambiguous Root Cause Detection
- If no implementation target was explicitly declared and multiple candidate implementation files are identified, the planner halts with an ambiguous root-cause blocker rather than guessing a target.

### D. Multi-File Context Framing
- Formats target implementation code with 1-indexed line numbers (`_format_numbered_lines`).
- If an associated failing test exists, it is presented as a clearly demarcated read-only oracle (`=== READ-ONLY TEST ORACLE (DO NOT MODIFY - REFERENCE ONLY) ===`), ensuring the LLM understands expected behavior while preserving target boundaries.

### E. In-Memory Diff Applicability Validation
- Implemented `validate_patch_applicability(repo_path, unified_diff, base_commit_sha)`:
  - Performs an in-memory dry run against target repository files without touching disk or Git state.
  - Verifies workspace containment and file existence.
  - Verifies that hunk headers and context lines match target file content around specified lines.
  - Verifies that applying the patch produces syntactically valid Python code (`ast.parse`).
  - Rejects empty/no-op diffs (`added_lines == 0 and removed_lines == 0`).

### F. Bounded Retry Feedback Loop
- Configured bounded retry loop (`RepairPlanner(max_retries=2)`).
- When a response fails JSON parsing, schema validation, line budget limits, or in-memory applicability, concise diagnostic feedback is passed back to the same provider.
- Stops early when deterministic fake providers run out of scripted responses.
- Accumulates all failure reasons across attempts and fails closed if retries are exhausted.

### G. Local Provider Isolation & Immutability
- Local Ollama provider failures fail closed without silent cloud fallback (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
- Target repository files, branches, and commit histories are strictly immutable during planning and validation.

---

## 3. Verification Suite & Quality Gates

### A. Dedicated Regression Test Suite
Created `tests/unit/test_repair_planner_reliability.py` verifying all 9 mandatory requirements:
1. `test_failing_test_identifies_impl_file_with_evidence`: Resolves implementation file from stack traces and includes read-only test oracle.
2. `test_intake_restricted_to_tests_cannot_silently_expand`: Rejects plan when scope is restricted to test files and requires authorized intake revision.
3. `test_diff_with_incorrect_hunk_context_is_rejected`: Detects context line mismatches and rejects plan.
4. `test_diff_outside_permitted_scope_is_rejected`: Rejects diffs modifying files outside authorized scope.
5. `test_valid_minimal_implementation_patch_accepted`: Confirms valid minimal patches are marked `VALIDATED`.
6. `test_malformed_provider_output_and_exhausted_retries_fail_closed`: Fails closed after retries with all violations recorded.
7. `test_ambiguous_root_cause_evidence_produces_blocker`: Blocks planning when multiple candidate files are found ambiguously.
8. `test_candidate_validation_never_modifies_target_or_git_history`: Confirms byte-for-byte target immutability.
9. `test_no_cloud_or_alternate_provider_fallback_on_local_ollama_failure`: Confirms fail-closed behavior on unreachable Ollama port.

### B. Full Test Suite & Benchmark Results
- **Full Test Suite**: 849 passed, 2 skipped (safe opt-in Docker skips).
- **Repair Benchmarks**: 11/11 fixtures passed (100% outcome accuracy, 0 false fixes, 100% scope compliance).
- **Code Quality**: `git diff --check` passed with 0 warnings/errors.
