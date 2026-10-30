# FORGE Specification — Deterministic Repair Construction Architecture & Coordinate Ownership

**Status**: COMPLETED & VERIFIED  
**Phase**: Phase 5 (Supervised Engineering Workflow Hardening)  
**Baseline HEAD**: `963a37c5faf37995f1bb8a56be196e3404fb6348`  
**Reference Invariants**: `AGENTS.md` (Audit-Only, Fail-Secure, Untrusted Input, Docker Isolation, Evidence Integrity, No Fabrication, Mandatory Release Gate)

---

## 1. Problem Statement & Pilot Forensics

Forensics across Supervised Pilots 01 through 05 demonstrated that while structural JSON schemas and reproduction oracles functioned, repair candidate construction repeatedly broke down due to **coordinate ownership ambiguity**:

1. **Coordinate Hijacking via Search Block Matching**: In earlier implementations, `RepairPlanner.plan_repair` allowed model-generated `search_block` strings to dictate coordinates. When local models (such as `qwen2.5-coder:1.5b`) proposed replacing lines 1–12 (the function header or preliminary argument checks) rather than the anchored defect statement at line 14, standard verbatim search matching accepted the proposal at line 1, completely bypassing the deterministically grounded edit anchor at line 14.
2. **Whole-Function Re-declaration for Statement Defects**: Compact models frequently attempt to redeclare the entire enclosing function definition (`def ...`) even when the defect is localized to an internal statement. When models omitted intervening docstrings or comments, verbatim matching failed; when they matched, they risked stripping validation guards or docstrings.
3. **Need for Grounded Coordinate Ownership**: Source coordinates (target file, base commit SHA, source SHA-256 digest, 1-indexed line spans, enclosing AST scope) are known deterministically by FORGE from verified reproduction stack traces and repository AST analysis. The LLM's role should be restricted to providing the replacement logic for the anchored statement, while coordinate ownership remains strictly deterministic.

---

## 2. Architecture & Typed Grounded Representation

```
+---------------------------------------------------------------------------------+
|                   Verified Defect Reproduction Evidence                        |
+---------------------------------------+-----------------------------------------+
                                        |
                                        v
+---------------------------------------------------------------------------------+
|               GroundedRepairRepresentation (app/forge/repair/                   |
|                        deterministic_repair.py)                                 |
|  - target_file: str                - start_line: int (1-indexed)                |
|  - base_commit_sha: str            - end_line: int (1-indexed)                  |
|  - source_digest: str (SHA-256)    - original_statement: str                    |
|  - ast_node_type: str              - symbol_scope: Optional[str]                |
|  - ast_node_id: str                - allowed_replacement_scope: str             |
|  - is_resolved: bool               - confidence: float                          |
+---------------------------------------+-----------------------------------------+
                                        |
                                        v
+---------------------------------------------------------------------------------+
|                       Two-Tiered Proposal Validation                            |
|                                                                                 |
|  Tier 1: Standard Search/Replace Check                                         |
|    - If search_block matches target file and encompasses anchored defect,       |
|      construct diff via standard construct_unified_diff_from_search_replace.     |
|                                                                                 |
|  Tier 2: Scope Boundary Defense & Deterministic Repair Construction             |
|    - If replacement content defines a function/class ('def ...') while the     |
|      anchor is an internal statement, strictly reject as SCOPE VIOLATION.       |
|    - If search_block was not found (omitted docstrings/lines), deterministically |
|      splice replacement at exact coordinates [start_line, end_line].            |
|    - Verify leading whitespace matching anchor indentation.                     |
|    - Validate Python AST syntax and static semantic invariants.                 |
|    - Synthesize standard unified diff candidate via difflib.                    |
+---------------------------------------------------------------------------------+
```

### Typed Contract: `GroundedRepairRepresentation`

```python
class GroundedRepairRepresentation(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_file: str
    base_commit_sha: str
    source_digest: str
    ast_node_type: str
    ast_node_id: str
    symbol_scope: Optional[str] = None
    start_line: int
    end_line: int
    start_col: Optional[int] = None
    end_col: Optional[int] = None
    original_statement: str
    allowed_replacement_scope: str = "statement_level"
    evidence_ids: List[str] = Field(default_factory=list)
    finding_id: str
    expected_behavior: Optional[str] = None
    proposed_replacement: Optional[str] = None
    rejection_reasons: List[str] = Field(default_factory=list)
    is_resolved: bool = True
    confidence: float = 1.0
```

---

## 3. Invariant Guarantees & Scope Boundary Defense

1. **Strict Coordinate Ownership**: Source file coordinates `[start_line, end_line]` are owned by FORGE, derived deterministically from verified reproduction traces and AST node identification. LLMs cannot arbitrarily redefine file boundaries.
2. **Scope Boundary Defense**: If an anchor is an internal statement (e.g. `Return`, `Assign`, `If`) and the model's replacement begins with a function or class definition (`def `, `class `, `async def `), the proposal is strictly rejected as a scope violation unless the search block is an exact matching function definition encompassing the anchor.
3. **Indentation Preservation**: Leading whitespace indentation of the original statement is enforced. Proposals stripping indentation or supplying misaligned blocks are rejected without silent mutation or heuristic trimming.
4. **No-Op & Identical Replacement Rejection**: Replacements identical to the original statement (`replace.strip() == original.strip()`) or whitespace-only are rejected.
5. **AST Syntax & Semantic Invariant Verification**: Patched files are parsed via `ast.parse` and verified via `analyze_python_ast_semantics`. Replacements introducing syntax errors, unreachable dead code, or bypassing validation guards fail closed.
6. **Revision & Digest Integrity**: Target file contents must match `source_digest` (SHA-256) at `base_commit_sha`. Stale revisions fail closed.

---

## 4. Generalization Regression Fixture: `GEN07_nested_statement_anchor_scope`

Added independent generalization fixture under `tests/fixtures/generalization_fixtures/GEN07_nested_statement_anchor_scope/`:
- **Defect Module (`src/backoff.py`)**: `BackoffPolicy.compute_delay(attempt: int)` with comprehensive class/method docstrings and a maximum delay guard. The delay formula erroneously uses linear multiplication (`calculated_delay = self.base_delay * attempt`) instead of exponential progression (`self.base_delay * (2 ** attempt)`).
- **Reproduction Test (`tests/test_backoff.py`)**: Asserts exponential progression for attempts 0, 1, 2, 3 and caps at `max_delay`. Fails with `AssertionError: 2.0 != 4.0`.
- **Validation**:
  - Deterministic grounding correctly resolves `Assign` node at line 34 within `BackoffPolicy::compute_delay`.
  - Out-of-scope proposal (redeclaration of `def compute_delay`) is rejected with actionable scope violation diagnostic.
  - Correct statement-level replacement (`calculated_delay = self.base_delay * (2 ** attempt)`) passes static verification and cleanly resolves tests in an isolated scratch workspace.

---

## 5. Verification & Live Model Evaluation

### Unit & Integration Verification Suite
12 dedicated unit and integration tests in `tests/unit/test_deterministic_repair_construction.py`:
- `test_grounded_representation_creation_and_attributes`: Verified typed attributes, line numbers, and scope.
- `test_grounded_representation_unresolved_anchor`: Verified fail-closed behavior for unresolved anchors.
- `test_correct_grounded_edit_synthesizes_unified_diff`: Verified `difflib` unified diff synthesis and `PatchCandidate` construction.
- `test_preserves_surrounding_docstrings_and_guards`: Verified surrounding context, docstrings, and guards remain untouched.
- `test_rejects_function_header_redeclaration_for_statement_anchor`: Verified scope boundary defense.
- `test_rejects_improper_indentation_without_normalization`: Verified whitespace preservation without silent normalization.
- `test_rejects_empty_and_noop_replacements`: Verified rejection of empty and no-op proposals.
- `test_rejects_malformed_syntax`: Verified fail-closed AST syntax parsing.
- `test_rejects_semantic_invariant_violations`: Verified static reachability analysis.
- `test_rejects_stale_source_digest`: Verified revision drift and digest mismatch defense.
- `test_gen07_nested_statement_repair_and_scope_rejection`: Verified GEN07 fixture end-to-end.
- `test_planner_uses_grounded_rep_and_recovers_on_retry`: Verified two-stage planner retry loop.

### Live Model Evaluation (Local Ollama `qwen2.5-coder:1.5b`)
- **Configuration**: Strictly local (`http://localhost:11434/v1`), `FORGE_ALLOW_CLOUD_FALLBACK=false`, zero cloud API access.
- **Observed Behavior**:
  - `GroundedRepairRepresentation` resolved: `src/backoff.py`, scope `BackoffPolicy::compute_delay`, lines `[34, 34]`.
  - Model proposed function header redeclaration (`def compute_delay(self, attempt: int) -> ...`).
  - Safeguard triggered: Correctly rejected as `Scope violation: Replacement content defines a function/class ('def compute_delay(self, attempt: int) ->'), but anchor is an internal statement block (Assign)`.
  - Outcome: Fail-closed rejection, 0 invalid code mutated.

### Regression Test Suite
- **Full Unit Suite**: 939 passed, 1 skipped (0 failures).
- **Bytecode Compilation**: `python3 -m compileall app/ tests/` clean.
- **Git Hygiene**: `git diff --check` clean.
