# FORGE Specification — Candidate Semantic Verification Hardening

**Status**: COMPLETED & VERIFIED  
**Phase**: Phase 5 (Supervised Engineering Workflow Hardening)  
**Baseline HEAD**: `90d33986d602bd2c5372308d4967bc91e8d32ed2`  
**Reference Invariants**: `AGENTS.md` (Fail Secure, Untrusted Input, Sandbox Isolation, Evidence Integrity, No Fabrication, Mandatory Release Gate)

---

## 1. Problem Statement & Pilot 03 Forensics

In Supervised Pilot 03, FORGE executed an end-to-end defect repair against a pricing discount module where `calculate_discount` had an incorrect discount computation formula. While the repair planner successfully generated a patch and the Docker sandbox reproduction check passed, forensics revealed a critical semantic validation gap:

1. **Premature Return & Bypassed Guard**: The model proposed replacing the `def calculate_discount` function signature line and inserting `return price - (price * discount_percent / 100)` immediately at the top of the function body.
2. **Dead / Unreachable Code**: By returning immediately at line 2, the remaining function body—including validation guards (`if price < 0: raise ValueError(...)` and `if not (0 <= discount_percent <= 100): raise ValueError(...)`)—became unreachable dead code.
3. **Displaced Docstring**: The function docstring was displaced below the early return statement, destroying module documentation and function signature contracts.
4. **False Positive Verification**: The test suite provided in Pilot 03 only tested valid positive prices (e.g. 100.0 and 50.0) and lacked negative-price or out-of-range discount assertions. Because the reproduction test passed and `ast.parse` only checks grammar (not reachability), `MultiCheckVerifier` marked the patch as `passed: True`, allowing a flawed patch to advance to `awaiting_human_review`.

Relying solely on external test suites or pure syntax validation is insufficient when test suites have incomplete assertion coverage. FORGE candidate verification must enforce static semantic invariants before any candidate patch is presented for human review.

---

## 2. Architecture & Static Semantic Invariant Verification

```
                      +---------------------------------------+
                      |         Candidate Unified Diff        |
                      +-------------------+-------------------+
                                          |
                                          v
+-----------------------+     +-------------------------------+
| Baseline Source Code  | --> |     MultiCheckVerifier        |
| (Trusted Repository)  |     |   Candidate Workspace Diff    |
+-----------------------+     +---------------+---------------+
                                              |
                                              v
                              +-------------------------------+
                              |    AST Semantic Analyzer      |
                              | (app/forge/repair/semantic_   |
                              |  verifier.py)                 |
                              +---------------+---------------+
                                              |
               +------------------------------+------------------------------+
               |                              |                              |
               v                              v                              v
+-------------------------------+ +-------------------------------+ +-------------------------------+
|    Reachability Detector      | |   Validation Guard Detector   | | Docstring Integrity Checker   |
| Statements following un-      | | Guard conditions bypassed by  | | Displaced docstrings following|
| conditional Return, Raise,    | | preceding terminators or      | | executable statements         |
| Break, Continue, or Blocks    | | stripped without equivalents  | |                               |
+---------------+---------------+ +---------------+---------------+ +---------------+---------------+
               |                              |                              |
               +------------------------------+------------------------------+
                                              |
                                              v
                              +-------------------------------+
                              |     SemanticAnalysisResult    |
                              |  (violations, bypassed_guards,|
                              |   unreachable_statements)     |
                              +---------------+---------------+
                                              |
                          [Pass: Clean]       |       [Fail: Violations]
                                              v
                              +-------------------------------+
                              |  VerificationCheckResult      |
                              |  CheckType: SEMANTIC          |
                              |  passed: False -> REJECT      |
                              +-------------------------------+
```

---

## 3. Implemented Components & Safeguards

### 3.1 AST Reachability Detector (`collect_unreachable_statements`)
- Traverses Python AST statement bodies (`Module`, `FunctionDef`, `AsyncFunctionDef`, `For`, `While`, `If`, `Try`, `With`).
- Tracks unconditional terminators: `ast.Return`, `ast.Raise`, `ast.Break`, `ast.Continue`, and compound blocks (`ast.If` with both branches terminating, `ast.Try` with body and handlers terminating).
- Flags any subsequent statements within the same block as unreachable dead code with exact 1-indexed line numbers and statement types.

### 3.2 Validation Guard Preservation (`_check_validation_guards`)
- Compares baseline function ASTs against candidate patched function ASTs.
- Identifies guard patterns: `ast.If` statements containing `ast.Raise` or `ast.Assert`.
- Detects two failure modes:
  1. **Bypassed Guard**: A guard statement exists in the function body, but is preceded by an unconditional terminator (such as an early `return`), rendering the guard dead.
  2. **Stripped Guard**: A baseline function contained validation guards that were completely removed or omitted in the candidate function without equivalent safeguards.

### 3.3 Docstring Integrity Checker (`_check_docstring_integrity`)
- Enforces Python docstring conventions: if a function body contains an `ast.Expr` string literal, it must be the very first statement (ignoring comments/empty lines).
- Flags candidate patches where non-docstring executable statements are inserted before the docstring.

### 3.4 Verification Pipeline Integration
- Added `VerificationCheckType.SEMANTIC = "semantic"` to `app/forge/workflow/models.py`.
- Integrated `semantic:ast_invariants` into `build_default_verification_plan` in `app/forge/workflow/verification.py` as check #5, maintaining backward-compatible ordering (`REPRODUCTION`, `BASELINE_TESTS`, `LINT`, `SECURITY`, `SEMANTIC`).
- Evaluated in `execute_verification`: candidate verification strictly fails if semantic invariant analysis reports violations.
- Added explicit diagnostic warnings when reproduction tests pass but broader regression test coverage is absent.

### 3.5 Generalization Fixture `GEN06_bypassed_validation_early_return`
- Created under `tests/fixtures/generalization_fixtures/GEN06_bypassed_validation_early_return/`:
  - `src/pricing.py`: Original discount logic with `if price < 0: raise ValueError(...)`.
  - `tests/test_pricing.py`: Both positive and negative price tests.
  - `fixture.json`: Validates that the flawed Pilot 03 early-return patch is rejected with `unreachable_statements` and `bypassed_guards`, while the minimal correct replacement passes.

---

## 4. Verification & Testing

- **Dedicated Unit Tests**: `tests/unit/test_semantic_verifier.py` (15/15 tests passing in 0.46s).
- **Full Unit Test Suite**: 927 passed, 1 skipped across all test modules in 59.35s.
- **Bytecode Compilation**: `python -m compileall app/ tests/unit/` completed with 0 errors.
- **Git Hygiene**: `git diff --check` clean, zero secret leaks.

---

## 5. Limitations & Safe Operation

1. **Static Analysis Limitations**: Static AST analysis operates on syntactic and structural patterns. It does not perform symbolic execution or full abstract interpretation. Dynamic regression tests remain necessary for full semantic correctness.
2. **Language Scope**: AST invariant analysis is currently implemented for Python files (`.py`). Non-Python files or files that do not parse as Python are treated as cleanly pass-through for AST invariants and rely on multi-check test execution and linters.
3. **Fail-Closed & Immutable Invariants**: Zero target repository mutations, zero git commits on target repositories, Docker isolation strictly enforced, and cloud fallback remains disabled (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
