# FORGE — Autonomous Software Engineering Intelligence

FORGE is an enterprise-grade autonomous software engineering intelligence platform. It investigates software repositories, reviews code diffs with evidence calibration, evaluates CI/CD security policies, and conducts isolated defect reproduction and verification without compromising host machine integrity or mutating target repositories during audit phases.

---

## Maturity & Capability Boundaries

- **Audit-Only & Immutable**: FORGE strictly investigates repositories. It never modifies target repository source code, creates git commits, pushes branches, opens pull requests, merges code, or deploys software.
- **Untrusted Input Invariant**: All repository contents, documentation, commit messages, PR diffs, and tool outputs are treated as untrusted data.
- **Fail-Secure Container Sandboxing**: Any dynamic execution of repository code occurs strictly within an isolated Docker container with dropped capabilities, read-only root filesystems, non-root user execution, and disabled network access.
- **Refusal of Host Fallback**: When Docker is unavailable, runtime execution is marked `SKIPPED_SANDBOX_UNAVAILABLE`. FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- **Zero Fabrication**: Claims must be backed by concrete, verified evidence. Confidence is automatically downgraded for unbacked assertions.

---

## Phase Roadmap & Status

```
Phase 1: Secure Repository Intelligence  ── [COMPLETED] (Milestones 1–7)
Phase 2: Diff-Aware Intelligent Review   ── [COMPLETED] (Milestones 1–6)
Phase 3: Defect Reproduction & Verification ── [COMPLETED] (Milestones 1–6)
Pre-Phase 4: Local Ollama & Multi-Provider LLM Integration ── [COMPLETED]
Phase 4 Readiness & Sandbox Gate        ── [COMPLETED]
Phase 4: Targeted Autonomous Repair     ── [COMPLETED] (Milestones 1–6)
Phase 5: Supervised End-to-End Engineering Workflow ── [COMPLETED] (Milestones 1–6)
Repair Planner Reliability & Grounded Verification ── [COMPLETED]
Local Ollama Structured Output Reliability ── [COMPLETED]
Patch Generation Reliability & Diff Syntax Enforcement ── [COMPLETED]
Structured Patch Construction & Deterministic Synthesis ── [COMPLETED]
Structured Proposal Wire-Schema Reliability & Patch Representation Contract ── [COMPLETED]
Deterministic Patch Anchoring & Minimal Replacements ── [COMPLETED]
Deterministic Edit Anchoring & Source-Grounded Repair ── [COMPLETED]
Repair Reliability & Generalization ── [COMPLETED]
Candidate Semantic Verification Hardening ── [COMPLETED]
Deterministic Repair Construction Architecture ── [COMPLETED]
Phase 6: Supervised Beta Packaging & Operational Readiness ── [IN PROGRESS] (Milestones 1–2 Complete)
```

### Phase 6, Milestone 2: Packaging, CLI Usability & Distribution (Completed)
- **Objective & Scope**: Package FORGE into standard Python distribution artifacts (wheel `.whl` and source distribution `.tar.gz`) enabling clean standalone installation and CLI execution outside the source repository without dependency bloat or security regressions.
- **Architectural Deliverables**:
  - *Distribution Artifact Generation & Verification*: Built pure-Python wheel (`dist/forge_core-0.1.0b1-py3-none-any.whl`, 351 KB) and source distribution (`dist/forge_core-0.1.0b1.tar.gz`, 488 KB).
  - *Strict Distribution Exclusion Policy ([`MANIFEST.in`](file:///Users/animeshyadav/Desktop/project/forge/MANIFEST.in))*: Explicitly includes core code and documentation while barring local `.env` files, SQLite databases (`*.sqlite`, `*.db`), test fixtures, pilot directories, caches, and bytecode from distribution archives.
  - *Clean-Install Verification in Isolated Environments*: Verified installation of both wheel and sdist in temporary virtual environments (`/tmp/forge_clean_*`) outside the repository with zero `PYTHONPATH` bleed. Confirmed `forge version`, `forge doctor`, `forge db init`, and Python module imports execute cleanly without source checkout dependencies.
  - *CLI Usability & Shell Auto-Completion*: Integrated and verified Typer native shell completion for `bash`, `zsh`, `fish`, and `powershell` via `forge --install-completion` and `forge --show-completion`.
  - *Milestone Specification ([`docs/specs/phase-6-m2-packaging-distribution.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-6-m2-packaging-distribution.md))*: Codified distribution architecture, artifact forensics, clean-install verification records, and security invariants.

### Phase 6, Milestone 1: Beta Readiness Audit & Release Foundation (Completed)
- **Objective & Scope**: Establish the operational foundation for the limited, human-supervised developer beta of FORGE following the conclusion of Supervised Pilots 01–08.
- **Architectural Deliverables**:
  - *Supervised Beta Contract ([`docs/BETA_CONTRACT.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/BETA_CONTRACT.md))*: Codifies non-negotiable invariants: strict target repository immutability, mandatory human review gate (`awaiting_human_review`), fail-closed container sandboxing (`--network none`, read-only, non-root user), local-only model inference by default (`FORGE_ALLOW_CLOUD_FALLBACK=false`), and zero automated target code mutations, commits, PRs, merges, or deployments.
  - *Empirical Known Issues & Model Limitations ([`docs/BETA_KNOWN_ISSUES.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/BETA_KNOWN_ISSUES.md))*: Discloses observed model capabilities from Pilots 01–08. Single-statement arithmetic and logic replacements operate with high fidelity; multi-statement control-flow synthesis and cross-type statement replacements on compact local models (1.5B) require close operator oversight. Details how FORGE traps these failure modes before disk mutation.
  - *Operator Quickstart & Administration Guide ([`docs/OPERATOR_GUIDE.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/OPERATOR_GUIDE.md))*: Step-by-step instructions for installation, configuration, system diagnostics (`forge doctor`), task intake, Docker reproduction, local model repair planning, multi-check verification, human approval, and dossier export.
  - *Package Metadata & Version Alignment*: Aligned package version to PEP 440 beta version `0.1.0b1` across `pyproject.toml` and `app/forge/__init__.py`. Aligned CLI root help and `forge doctor` ready messages.
  - *Offline Test Baseline & Live Docker Validation*: Full test suite of 958 tests executes completely offline without external network or API keys. Opt-in Docker integration tests verified live container isolation properties.

### Deterministic Repair Construction Architecture & Coordinate Ownership (Completed)
- **Problem & Root Cause**: Analysis of Pilot 01–05 forensics revealed that repair candidate generation broke down when local models (`qwen2.5-coder:1.5b`) attempted to override coordinates or redeclare whole function headers (`def ...`) when defects were localized to internal statements. Because earlier proposal constructors allowed model-supplied search blocks to dictate coordinates, models could hijack coordinates to line 1 rather than the anchored defect statement at line 14.
- **Architectural Enhancements**:
  - *Typed Grounded Repair Representation (`GroundedRepairRepresentation`)*: Implemented in `app/forge/repair/deterministic_repair.py`, capturing `target_file`, `base_commit_sha`, SHA-256 `source_digest`, AST node type/identity, symbol scope, exact 1-indexed `[start_line, end_line]`, and verbatim statement text.
  - *Deterministic Coordinate Ownership & Scope Boundary Defense*: Locks coordinates to the grounded statement. If a model proposal re-declares a function/class header (`def ...`) for an internal statement anchor, it is strictly rejected as a `Scope violation` unless the search block is an exact matching function definition encompassing the anchor.
  - *Indentation & Formatting Verification*: Enforces leading whitespace matching the original statement's indentation, rejecting unindented or misaligned lines without silent normalization.
  - *AST Syntax & Static Semantic Verification*: Verifies that candidate code parses cleanly with `ast.parse` and satisfies semantic invariants (no unreachable code, no bypassed guards) before generating a diff.
  - *difflib Standard Diff Synthesis*: Synthesizes standard unified diffs via Python `difflib.unified_diff`, enforcing line and hunk budgets.
  - *Two-Tiered Proposal Validation in Planner*: `RepairPlanner.plan_repair` evaluates structured search/replace proposals through coordinate containment checks and deterministic repair grounding, propagating actionable diagnostics in retry feedback.
  - *Generalization Regression Fixture (`GEN07_nested_statement_anchor_scope`)*: Added fixture under `tests/fixtures/generalization_fixtures/` covering nested statement anchoring inside a class method with docstrings and guards (`BackoffPolicy.compute_delay`), verifying statement grounding, out-of-scope rejection, and verified execution.
  - *Fail-Secure Invariants*: Retained zero target repository mutation, Docker container sandboxing, and strict enforcement of `FORGE_ALLOW_CLOUD_FALLBACK=false`.
- **Verification**: Verified with 12 dedicated unit tests (`tests/unit/test_deterministic_repair_construction.py`), 939 full regression tests, and live local Ollama evaluation.

### Candidate Semantic Verification Hardening & AST Invariants (Completed)
- **Problem & Root Cause**: In Supervised Pilot 03, the local model proposed replacing a function definition header with an immediate `return` statement calculating the discount. While this satisfied the target positive-price test in the reproduction check, it introduced dead unreachable code, displaced the function docstring, and completely bypassed pre-existing negative-price validation guards (`if price < 0: raise ValueError(...)`). Because `ast.parse` only checks syntactic grammar and the reproduction check lacked negative-price assertions, the regression was missed and the candidate erroneously passed verification to reach `awaiting_human_review`.
- **Architectural Enhancements**:
  - *AST Reachability Analysis*: Implemented `collect_unreachable_statements` in `app/forge/repair/semantic_verifier.py` to traverse function and block statement sequences and flag any statements following unconditional terminators (`Return`, `Raise`, `Break`, `Continue`, terminating `If/Try`).
  - *Validation Guard Preservation*: Added `_check_validation_guards` to compare baseline AST functions against candidate functions, preventing guards (`If` statements raising exceptions or assertions) from being bypassed by preceding terminators or stripped from function bodies.
  - *Docstring Integrity Protection*: Added `_check_docstring_integrity` to ensure function docstrings are not displaced below executable statements.
  - *Verification Pipeline Integration*: Added `VerificationCheckType.SEMANTIC = "semantic"` to `app/forge/workflow/models.py` and integrated `semantic:ast_invariants` into `build_default_verification_plan` and `execute_verification` in `app/forge/workflow/verification.py` as check #5, strictly failing candidate verification if static semantic violations are found. Added explicit diagnostics when reproduction passes without broader regression test coverage.
  - *Generalization Regression Fixture (`GEN06_bypassed_validation_early_return`)*: Added fixture under `tests/fixtures/generalization_fixtures/` replicating the Pilot 03 bypass failure mode and verifying that early returns bypassing validation are rejected while correct minimal replacements pass.
  - *Fail-Closed Invariants*: Zero target repository mutations, Docker sandbox isolation strictly required for dynamic execution, and `FORGE_ALLOW_CLOUD_FALLBACK=false` preserved.
- **Verification**: Verified with 15 dedicated unit tests (`tests/unit/test_semantic_verifier.py`), 927 full regression tests, and bytecode compilation.

### Repair Reliability & Generalization (Completed)
- **Problem & Root Cause**: Analysis of all eight attempts of Supervised Pilot 01 revealed that while structural schema and edit-anchoring mechanisms functioned, repair generation broke down due to two interrelated issues: (1) model proposals inverted defect logic (e.g., proposing `return 0.0` or stripping guards when reproduction evidence confirmed an unhandled exception), and (2) deterministic AST statement localization contained calculator-specific biases (unconditionally selecting `ast.If` blocks) while suppressing specific anchor-grounding rejection reasons (`anch_reasons`) during planner retries.
- **Architectural Enhancements**:
  - *Multi-Signal Semantic Statement Scoring*: Updated `_locate_statement_in_python_ast` in `app/forge/repair/anchoring.py` to deterministically score candidate statements inside target files across three defect categories: unraised exceptions (`... not raised`), runtime errors (`IndexError`, `KeyError`), and value assertions (`AssertionError: X != Y`). Scores statements based on keyword and token overlap, numeric literal matching against reproduction traces (e.g., `0.5`, `-273.15`, `65535`), math operator matching, and statement compactness. Fails closed with `ANCHOR_UNRESOLVED` when positive evidence is absent (`best_score <= 0.0`).
  - *Semantic Safeguard Validation*: Added `validate_anchor_replacement_semantics` in `app/forge/repair/anchoring.py` to inspect proposed replacements against reproduction evidence. Explicitly rejects proposals that fail to raise cited unhandled exceptions as `Semantic safety violations`, and blocks guard-stripping proposals that replace conditional checks with unconditional returns.
  - *Diagnostic Reason Propagation in Planner Retries*: Updated `RepairPlanner.plan_repair` to preserve anchor-grounding rejection reasons (`anch_reasons`) when synthesizing diffs, feeding concrete diagnostic feedback into the retry prompt (such as indentation mismatches or unraised exception violations) rather than misleading generic errors.
  - *Five Independent Generalization Fixtures*: Built and verified five distinct defect fixtures under `tests/fixtures/generalization_fixtures/` covering incorrect logic calculation (`GEN01_incorrect_logic`), empty collection edge cases (`GEN02_edge_case_failure`), plausible incorrect repairs rejected by semantic safeguards (`GEN03_plausible_incorrect_rejected`), 16-space whitespace preservation (`GEN04_whitespace_sensitive`), and malformed/out-of-scope proposal rejection (`GEN05_malformed_semantically_wrong`).
  - *Fail-Secure Invariants*: Retained zero modifications to target repositories, `FORGE_ALLOW_CLOUD_FALLBACK=false`, and mandatory human authorization gate.
- **Verification**: Verified with 7 dedicated unit and live Ollama tests (`tests/unit/test_repair_generalization.py`), 913 full regression tests, 56/56 repair and review benchmarks, and 2/2 live Docker integration tests.

### Deterministic Edit Anchoring & Source-Grounded Repair (Completed)
- **Problem & Root Cause**: Forensics across Pilot 01 Attempts 1–7 demonstrated that requiring compact local coding models (`qwen2.5-coder:1.5b`) to author both the exact verbatim search coordinates and replacement blocks leads to recurring failures when files contain docstrings, comments, or multi-line structures. Models omit intervening docstrings or comments, failing verbatim matching. However, AST parsing and reproduction evidence can localize the exact statement deterministically without relying on model-generated coordinates.
- **Architectural Enhancements**:
  - *Separation of Localization from Replacement*: Separates deterministic source localization (executed against trusted repository files via Python AST analysis and test reproduction traces) from replacement proposal generation (proposed by the model).
  - *Typed Edit Anchor Contract (`EditAnchorContract`)*: Formalizes target path, base commit SHA, source SHA-256 digest, exact anchor text, 1-indexed line coordinates, surrounding context, evidence linkage, and typed resolution status (`AnchorResolutionStatus`).
  - *Safe Anchor Resolution (`resolve_source_anchor`)*: Enforces path traversal and protected path defense, scope containment, revision drift interception, and deterministic AST statement localization from failing test assertion traces to the target function body. Disambiguates or fails closed (`ANCHOR_AMBIGUOUS`, `ANCHOR_UNRESOLVED`).
  - *Deterministic Patch Construction (`construct_patch_from_edit_anchor`)*: Synthesizes standard unified diffs from verified anchors and replacement blocks via `difflib.unified_diff`. Rejects no-ops, empty content, and structural/indentation mismatches (such as whole-function definitions replacing internal statement anchors). Verifies in-memory patch applicability and Python AST syntax without modifying target files.
  - *Repair Planner Integration*: Injects deterministically resolved source anchors (`=== DETERMINISTICALLY RESOLVED SOURCE ANCHOR ===`) into prompt context and provides graceful anchor-grounded recovery when model search blocks omit intervening lines but replacement content is valid.
  - *Fail-Closed & Sandbox Invariants*: Retains strict target repository immutability, `FORGE_ALLOW_CLOUD_FALLBACK=false`, and mandatory human authorization gate.
- **Verification**: Verified with 12 dedicated unit tests (`tests/unit/test_deterministic_edit_anchoring.py`), 906 full regression tests, 56/56 repair and review benchmarks, and 2/2 live Docker integration tests.

### Deterministic Patch Anchoring & Minimal Replacements (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 6 revealed that while local Ollama (`qwen2.5-coder:1.5b`) emitted structured search/replace keys reliably, it attempted to replace entire multi-line functions while omitting docstrings or comments inside `search_block`. Because FORGE enforces strict verbatim matching without fuzzy trimming (`count == 1`), the search block failed to match. Furthermore, error diagnostics did not detect omitted intervening lines or guide the model toward minimal statement-level replacement.
- **Architectural Enhancements**:
  - *Minimal Statement Replacement Strategy*: Updated prompt contracts to mandate minimal, statement-level search/replace blocks (1–3 lines) targeting the exact faulty logic rather than whole functions, and strictly forbidding omission of intervening source lines.
  - *Defect Line Extraction & Focused Anchor Framing*: Added `_extract_defect_line_numbers` and `_build_focused_anchor_section` in `RepairPlanner` to extract defect lines from reproduction evidence and inject an un-numbered, verbatim source code reference block (`=== RELEVANT FAULTY SOURCE ANCHOR (DATA ONLY - REFERENCE) ===`) directly into the prompt.
  - *Omitted Intervening Lines Diagnostic*: Added detection in `construct_unified_diff_from_search_replace` that identifies when the start and end lines of a search block exist in the file but intervening lines (such as docstrings or comments) were omitted, returning clear, actionable guidance.
  - *Line Number Prefix Detection*: Detects when search blocks accidentally include leading line numbers (e.g., `108: `) and instructs the model to supply exact code lines.
  - *Focused Retry Feedback*: Automatically injects focused verbatim source excerpts into the retry prompt when search block matching fails, grounding model corrections in ground-truth repository text.
  - *Verbatim Precision Preserved*: Verbatim matching (`orig_text.count(search_block) == 1`) is strictly preserved without fuzzy heuristics, whitespace normalization, or comment stripping.
- **Verification**: Verified with 11 dedicated unit tests (`tests/unit/test_patch_anchoring_and_minimal_replacements.py`), 913 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Structured Proposal Wire-Schema Reliability & Patch Representation Contract (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 5 exposed that while local Ollama (`qwen2.5-coder:1.5b`) returned syntactically valid JSON with required diagnostic fields, it completely omitted `search_block`, `replace_block`, and `unified_diff` on the wire. Because those fields defaulted to `None` in Pydantic, `model_json_schema()` omitted them from `required`, allowing Ollama's grammar engine to minimize output by omitting all patch fields. Pydantic post-validation then failed closed because no patch representation was present.
- **Architectural Enhancements**:
  - *Explicit Wire Schema Contract*: Configured `RepairModelResponseSchema` via `json_schema_extra` to programmatically include `search_block`, `replace_block`, and `unified_diff` in `schema["required"]`. Because the fields are typed with `Optional[str]`, they generate nullable `anyOf: [string, null]`, forcing local LLM grammar engines to emit the keys without key omission on the wire.
  - *Mutual Exclusivity & Single Representation Enforcement*: `validate_patch_proposal_representation` strictly accepts either structured search/replace blocks or a valid `unified_diff`. It rejects missing representations, partial structured proposals (e.g., `search_block` without `replace_block`), and ambiguous proposals (providing both structured blocks and `unified_diff`).
  - *Prompt Framing & Schema Examples*: `RepairPlanner._build_prompt` includes `"unified_diff": null` in the schema template, explicitly instructing models to provide non-null structured blocks with null unified diffs.
  - *Targeted Retry Feedback*: If validation fails due to missing patch representations, partial proposals, or ambiguous representations, `RepairPlanner.plan_repair` feeds back targeted, actionable correction guidance.
  - *Full Backward Compatibility*: Older JSON payloads omitting `search_block` and `replace_block` keys validate cleanly as long as a valid `unified_diff` is provided.
- **Verification**: Verified with 12 dedicated unit tests (`tests/unit/test_structured_proposal_wire_schema.py`), 902 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Structured Patch Construction & Deterministic Synthesis (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 4 forensics confirmed that compact local coding models (`qwen2.5-coder:1.5b`) repeatedly emit Python code indented with whitespace instead of prefixing additions with `+` in column 0 inside unified diff hunks, despite explicit prompt instructions. Requiring compact models to calculate hunk line numbers, diff headers, and column-0 markers places unnecessary syntactic strain on the model.
- **Architectural Enhancements**:
  - *Strict Structured Proposal Schema*: Defined `StructuredPatchProposalSchema` requiring `target_file`, `search_block` (exact verbatim lines), `replace_block` (substitution lines), and `change_rationale`. `RepairModelResponseSchema` supports both structured search/replace and legacy unified diffs.
  - *Deterministic Diff Constructor*: `construct_unified_diff_from_search_replace` enforces exact verbatim matching (`orig_text.count(search_block) == 1`), path traversal and protected path rejection, test file rejection, base revision pinning, and size budgets. It uses Python standard `difflib.unified_diff` to synthesize 100% syntactically valid unified diffs with exact line headers and markers.
  - *In-Memory Applicability & Syntax Verification*: Passes constructed diffs through `parse_and_validate_unified_diff` and in-memory dry-run `validate_patch_applicability` (including Python AST parsing).
  - *Prompt Refinement & Targeted Retries*: Instructs models to provide exact verbatim code lines without line number prefixes, and supplies targeted diagnostics for missing or ambiguous search blocks, unchanged replacements, or empty blocks.
- **Verification**: Verified with 13 dedicated unit tests (`tests/unit/test_structured_patch_construction.py`), 890 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Patch Generation Reliability & Diff Syntax Enforcement (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 3 exposed that compact coding models emit Python code indented with spaces inside diff hunks instead of prefixing additions with `+` in column 0. The diff validator correctly detected zero additions and deletions, rejecting the patch as an empty diff. However, retry loops lacked explicit column 0 diff marker feedback, and static validation lacked checks for unchanged replacement / no-op diffs.
- **Architectural Enhancements**:
  - *Unified Diff Schema Contract*: `RepairModelResponseSchema.unified_diff` description explicitly mandates standard unified diff formatting starting with `--- a/<file>` and `+++ b/<file>`, with lines to add starting with `+`, lines to remove starting with `-`, and context lines starting with `' '`.
  - *Prompt Syntax Requirements*: `RepairPlanner._build_prompt` codifies column 0 diff marker requirements, warning explicitly that lines starting with indentation spaces are treated as unchanged context lines.
  - *Malformed Hunk Detection*: `parse_and_validate_unified_diff` scans all hunk body lines to ensure every line begins with a valid diff marker (`+`, `-`, `' '`, `\`, or empty string), rejecting un-prefixed code snippets immediately.
  - *Static Unchanged Replacement Detection*: `parse_and_validate_unified_diff` checks whether all added lines in each hunk match the removed lines, rejecting no-op replacements.
  - *In-Memory Net Change Verification*: `validate_patch_applicability` verifies that applying the patch candidate produces a non-zero net modification to the target file (`"".join(current_lines) != orig_text`).
  - *Targeted Retry Diagnostics*: `RepairPlanner.plan_repair` provides specific diagnostic guidance on empty diffs, context mismatches, missing headers, and no-op replacements, guiding the local model to correct syntax without weakening any validation boundary.
  - *Safe Candidate Construction*: Guards `PatchCandidate` instantiation with `max(1, hunk_count)` and `try/except` to ensure fail-closed plan rejection when diffs are malformed.
- **Verification**: Verified with 11 dedicated regression tests (`tests/unit/test_patch_generation_reliability.py`), 877 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Local Ollama Structured Output Reliability (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 2 exposed that local coding models emitted trailing commas before closing braces/brackets, failing RFC 8259 JSON parsing. Requests did not pass OpenAI-compatible `response_format` schemas on the wire, and retry loops lacked syntax diagnostic feedback.
- **Architectural Enhancements**:
  - *Structured Output / JSON Schema Wire Formatting*: `LLMRequest` supports `response_format` containing OpenAI-compatible `json_schema` derived from `RepairModelResponseSchema`. `OpenAICompatibleProvider` serializes `response_format` on the wire and supports graceful single-provider 400 downgrade.
  - *Strict RFC 8259 Parsing*: `parse_json_payload` safely parses JSON from direct content, markdown code fences, or `{ ... }` bounds, strictly rejecting trailing commas without silent mutation, and returning non-secret syntax diagnostics.
  - *Bounded Same-Provider Retries*: Enforces a hard cap of 3 retries (total attempts $\le 4$), 120s wall-clock time limit, 50,000 char response bound, and 4096 output token limit.
- **Verification**: Verified with 17 dedicated unit tests (`tests/unit/test_structured_output_reliability.py`), 866 full regression tests, and 11/11 passing repair benchmarks.

### Repair Planner Reliability & Grounded Verification (Completed)
- **Problem & Root Cause**: Pilot 01 exposed that intakes created from failing test specifications defaulted `affected_files` solely to test files. Downstream repair planning targeted tests instead of implementation code, causing context mismatches and verification crashes.
- **Architectural Enhancements**:
  - *Candidate Target Resolution*: `resolve_candidate_targets` discovers candidate implementation targets from reproduction stack traces, AST test imports, and symbol indexing.
  - *Strict Test Immunity & Scope Restriction*: `is_test_path` identifies test files. Modifying test files to mask defects is strictly forbidden. Intakes restricted solely to tests reject repairs when implementation files require modification, requiring an authorized revised intake.
  - *Ambiguous Root-Cause Protection*: Halts with an ambiguous root-cause blocker if multiple candidate implementation files are found without explicit designation.
  - *Multi-File Context Framing*: Target implementation code is presented with 1-indexed line numbers; failing test oracles are explicitly demarcated as read-only reference data.
  - *In-Memory Patch Applicability Validation*: `validate_patch_applicability` verifies hunk headers, context line matching against base commit files, Python AST syntax, and rejects empty/no-op diffs before marking plans `VALIDATED`.
  - *Bounded Retry Feedback Loop*: Allows up to `1 + max_retries` attempts with structured diagnostic feedback, failing closed if retries are exhausted without silent cloud fallback.
  - *Target Repository Immutability*: All planning and applicability checks run strictly in-memory, ensuring zero modification to target files or Git history.
- **Verification**: Verified with 9 dedicated regression tests (`tests/unit/test_repair_planner_reliability.py`), 849 full regression tests, and 11/11 passing repair benchmarks.

### Phase 5: Supervised End-to-End Engineering Workflow (Completed)
- **Kickoff & Architectural Foundation (Completed)**: Unifies repository acquisition, diff review, defect reproduction, and bounded repair verification into a supervised defect-to-verified-fix workflow. Codifies a 19-state typed state machine (`CREATED` through `AWAITING_HUMAN_REVIEW` to terminal `APPROVED` / `REJECTED`), transactional event ledger, cryptographic revision pinning, fail-closed container boundaries (`SANDBOX_UNAVAILABLE`), honest outcome taxonomy (`NOT_REPRODUCED`, `FLAKY`, `BROKEN_BUILD`, `REGRESSION_DETECTED`), and an exportable certified Engineering Dossier (`patch.diff`, verification proof, SARIF).
- **M1 — Unified Task Intake & Repository Context Pipeline (Implemented & Verified)**: Standardizes defect intake across review findings (`review_finding`), unified git diffs (`unified_diff`), issue problem statements (`issue_description`), and test specifications (`test_specification`) into a validated `WorkflowIntakeSpec`. Cryptographically pins exact 40-character commit SHA (`resolve_and_pin_revision`), intercepts revision drift (`RevisionDriftError`), detects dirty working trees (`DirtyRepositoryError`), and sanitizes affected file paths (rejecting path traversal `..` and absolute paths). Assembles scoped, deterministic repository context (`RepositoryContext`) under explicit character and token budgets (`ContextBudget`, `ContextBudgetSummary`), filtering relevant AST symbols, extracting line-numbered source excerpts, wrapping discovered repository instructions in passive untrusted boundary banners, and providing CLI commands (`forge workflow intake create/validate/show` and `forge workflow context build/show`). Verified with 26 focused unit tests and 713 regression tests.
- **M2 — Supervised Workflow State Machine & Transactional Ledger (Implemented & Verified)**: Delivers a typed, guarded 19-state lifecycle machine with code-enforced transition validation, transactional SQLite storage (`workflow_intakes`, `engineering_workflows`, `engineering_workflow_events`), optimistic concurrency control via monotonic integer versioning (`WorkflowConcurrencyError` on conflicting updates), append-only sequential event auditing with automatic secret redaction, safe crash recovery and unblocking (`REPRO_BLOCKED` and `VERIFICATION_BLOCKED` safely resumed; terminal states strictly reject resumption), and a mandatory human authorization gate (`AWAITING_HUMAN_REVIEW` strictly required before `APPROVED`). Features CLI commands (`forge workflow start`, `forge workflow status`, `forge workflow list`, `forge workflow resume`, `forge workflow review`). Verified with 21 unit and integration tests and 734 full-suite tests.
- **M3 — Automated Evidence Chaining from Reproduction to Repair (Implemented & Verified)**: Connects verified defect reproduction directly into bounded repair planning. Safely derives non-shell reproduction commands from test specifications or issue reproduction hints (`extract_reproduction_command_from_intake`). Validates reproduction evidence integrity against commit revision pinning, finding identity, non-empty/untruncated content, exit code contradictions, and trusted oracle provenance (`validate_reproduction_evidence`). Manages guarded transitions through `reproduce_defect`, `plan_repair`, and sequential `advance_workflow` in `EngineeringWorkflowOrchestrator`, halting on inconclusive or failed outcomes, blocking safely without host fallback on Docker absence (`REPRO_BLOCKED`), and atomically attaching valid candidate patches with optimistic concurrency in SQLite (`attach_repair_plan_to_workflow`). Features CLI commands (`forge workflow repro`, `forge workflow plan-repair`, `forge workflow advance`). Verified with 31 unit tests and 765 full-suite tests.
- **M4 — Multi-Check Regression & Safety Verification (Implemented & Verified)**: Delivers a bounded, isolated multi-check verification pipeline executing across baseline and candidate disposable scratch workspaces inside Docker containers (`MultiCheckVerifier`). Chains reproduction check (defect oracle), baseline test suite (`pytest -q tests/`), static linting (`flake8`/`ruff`), and security analysis (`bandit`), with command allowlisting and shell injection defense (`validate_verification_check_command`). Categorizes outcomes honestly (`PASSED`, `FAILED`, `BLOCKED`, `TIMED_OUT`, `INCONCLUSIVE`, `BASELINE_FAILED`). Distinguishes candidate regressions (`REGRESSION_DETECTED`) from pre-existing baseline failures (`BASELINE_FAILED`). Transitions clean repairs safely through `VERIFIED -> REGRESSION_CHECKING -> AWAITING_HUMAN_REVIEW` while strictly halting for human review before any approval. Persists verification attempts in SQLite `multi_check_verifications`. Features CLI commands (`forge workflow verify` and `forge workflow advance --verify`). Verified with 24 unit and integration tests and 789 full-suite tests.
- **M5 — Certified Engineering Dossier & Operator Export (Implemented & Verified)**: Delivers certified engineering dossier generation and offline operator export for approved workflows. Strictly conditions export on explicit human operator approval (`APPROVED` state with `human_decision == "approve"`), revalidating workflow version, pinned commit SHA, patch digest, evidence provenance, and sequential ledger integrity. Exports a deterministic package: `patch.diff` (unmodified bounded diff $\le 100$ lines, $\le 20$ KB), `verification_certificate.json` (machine-readable proof and delta comparison), `findings.sarif` (standards-compliant SARIF 2.1.0 document), `audit_ledger.json` (complete sequential secret-scrubbed audit trail), `APPLY.md` (dry-run, apply, verification, and rollback runbook), and `manifest.json` (canonical SHA-256 integrity checksums + optional HMAC-SHA256 signature). Enforces path containment, directory safety policy, and offline validation (`validate_engineering_dossier`). Features CLI commands (`forge workflow export` and `forge workflow validate-dossier`). Verified with 40 unit and integration tests and 829 full-suite tests.
- **M6 — End-to-End Workflow Acceptance, Reliability & Release (Implemented & Verified)**: Final acceptance and release audit establishing a 30-scenario deterministic acceptance corpus across 10 reliability domains (intake & revision pinning, context budgeting, adversarial instructions, prompt injection, path traversal, state machine transitions, illegal jump rejection, optimistic concurrency, transactional ledger & secret scrubbing, defect reproduction, non-reproduction fail-closed, flaky oracle handling, reproduction timeouts, protected path rejection, Docker absence fail-closed, sandbox resource limits, network isolation, evidence tamper detection, revision drift interception, duplicate start idempotency, blocked workflow resumption, clean repair verification, baseline regression interception, pre-existing defect honest attribution, graceful degradation for optional linters, mandatory human review gate enforcement, operator rejection, unapproved export refusal, and certified dossier export with SHA-256 manifest and HMAC signing). Features quantitative metrics with honest denominators, automated secret scrubbing, and CLI commands (`forge workflow test` and `forge workflow acceptance`). Verified with 100.0% pass rate (30/30) and 838 full regression tests.
- **Milestone Sequence (Roadmap)**:
  - *M1 — Unified Task Intake & Repository Context Pipeline*: Completed and verified.
  - *M2 — Supervised Workflow State Machine & Transactional Ledger*: Completed and verified.
  - *M3 — Automated Evidence Chaining from Reproduction to Repair*: Completed and verified.
  - *M4 — Multi-Check Regression & Safety Verification*: Completed and verified.
  - *M5 — Certified Engineering Dossier & Operator Export*: Completed and verified.
  - *M6 — End-to-End Workflow Acceptance, Reliability & Release*: Completed and verified.
- **Strict Boundary Notice**: Target repository immutability is strictly enforced. Autonomous git commits, branch pushes, pull requests, merges, and deployments remain strictly forbidden. Automated workflow strictly halts at `AWAITING_HUMAN_REVIEW`. Only explicit operator approval unlocks dossier export.

### Phase 4: Targeted Autonomous Repair (Completed)
- **M1 — Evidence-Grounded Repair Planning & Patch Generation (Implemented)**: Converts calibrated findings and verified reproduction evidence into structured, bounded repair plans and candidate unified diffs. Enforces strict evidence grounding (ungrounded findings rejected), prompt-injection mitigation via untrusted context framing, protected file immunity (`AGENTS.md`, `README.md`, `.gitignore`, `pyproject.toml`, `.github/*`), strict size budgeting ($\le 100$ lines, $\le 5$ hunks, $\le 1$ file, $\le 20$ KB), target repository immutability, mandatory human approval gates, and SQLite persistence.
- **M2 — Isolated Patch Application & Verification (Implemented)**: Safely applies validated candidate patches in isolated disposable scratch workspaces and verifies them against independent test reproduction oracles inside the Docker container sandbox boundary. Features a pure-Python AST-verifying diff applier, tamper detection (SHA256 mismatch rejection), command allowlisting and shell injection defense, fail-closed host protection (`SANDBOX_UNAVAILABLE` when Docker is absent without executing code on the host), deterministic outcome taxonomy (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `TIMEOUT`, `INCONCLUSIVE`), SQLite attempt persistence (`repair_attempts`), and CLI commands (`forge repair verify`, `forge repair attempts`).
- **M3 — Repair Quality & Regression Benchmarking (Implemented)**: Deterministic, offline benchmark suite evaluating repair-plan quality, patch validation, and verification classification across 11 representative defect categories (syntax errors, incorrect logic, boundary conditions, missing validation, exception handling, security-sensitive mistakes, clean controls, ambiguous findings, insufficient evidence, adversarial instructions, and regression-inducing patches). Features independent ground truth, honest denominators (`None` / `N/A` for empty classes), secret redaction, and CLI integration (`forge benchmark repair` and `forge repair benchmark`).
- **M4 — Controlled Repair Orchestration & Lifecycle (Implemented)**: Resumable, auditable end-to-end orchestration workflow connecting verified evidence, bounded repair planning, static patch validation, containerized sandbox verification, and a mandatory human review gate. Features a typed state machine with strict transition enforcement (`CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED`), fail-closed container boundaries (`SANDBOX_UNAVAILABLE` when Docker is missing), anti-tampering SHA256 digest validation, stale base revision detection, plan-only dry-run bypass, optimistic concurrency control with monotonic versioning, append-only sequential event auditing with automated secret scrubbing, strict target repository immutability (zero file modifications, no commits/pushes/PRs), and full CLI management (`forge repair workflow start/status/list/resume/review`).
- **M5 — End-to-End Repair Reliability & Adversarial Testing (Implemented)**: Hardened end-to-end acceptance suite executing 26 deterministic scenarios across 10 failure domains: happy-path repair, evidence integrity gating, budget and protected-file safety, revision drift interception, all 6 verification outcomes, fail-closed container boundaries, provider timeout/malformed/auth resilience, resumability, optimistic concurrency conflict detection, adversarial prompt injection untrusted framing, path traversal rejection, and mandatory human review enforcement. Features quantitative metrics with honest denominators, automated secret scrubbing, and CLI commands (`forge repair test`, `forge repair acceptance`).
- **M6 — Final Acceptance & Release Readiness (Implemented)**: End-to-end audit across M1–M5, validation of real vs. simulated container isolation contracts, 100% pass across all 26 acceptance scenarios and 11 repair quality benchmarks, 100% pass on 686-test regression suite, usability and error-signaling audit, and formal release decision (`CONDITIONALLY READY FOR SUPERVISED USE` due to host Docker dependency for live container execution).
- **Strict Boundary Notice**: **Automated modification of target repositories, autonomous git commits, branch pushes, pull requests, merges, and deployments remain strictly FORBIDDEN.**

### Phase 4 Readiness & Sandbox Gate (Completed)
- **Container Sandbox & Host Isolation Audit**: Complete audit of container sandbox boundaries (`DockerSandbox`, `ReproductionHarness`). Verified that when Docker is absent (`shutil.which("docker") is None`), FORGE fails closed with `SKIPPED_SANDBOX_UNAVAILABLE` and strictly refuses to execute target repository code directly on the host.
- **Multi-Provider & Cloud Guard Invariants**: Retained 5 supported providers (`fake`, `ollama`, `gemini`, `deepseek`, `groq`) with complete elimination of OpenAI API and Ollama Cloud dependencies, standardizing Ollama strictly on local execution (`http://localhost:11434/v1`) and enforcing `FORGE_ALLOW_CLOUD_FALLBACK=false`.
- **Formal Repair Safety Contract (`forge.repair.contracts`)**: Codified formal safety boundaries required before Phase 4 patch generation can begin:
  - *Target Repository Immutability*: Target repository remains strictly read-only; repairs are strictly isolated to independent scratch workspaces.
  - *Base Revision Pinning*: Repairs are cryptographically pinned to the finding's verified commit SHA (`validate_base_sha`).
  - *Patch Candidate Constraints*: Enforces strict bounds on lines changed (max 100), hunks (max 5), files modified (max 1), and diff bytes (max 20KB).
  - *Scope Containment*: Path traversal, absolute paths, and symlink escapes outside the scratch workspace are strictly rejected via `verify_workspace_containment`.
  - *Pre/Post Outcome Taxonomy*: Deterministic classification (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `SANDBOX_UNAVAILABLE`).
  - *Mandatory Human Approval Gate*: Autonomous git commits, pushes, branch merges, and pull request generation are strictly forbidden (`assert_human_approval_gate`).
- **Phase 4 Autonomous Repair Notice**: **Phase 4 autonomous repair is NOT STARTED**. No automated patch generation, target repository mutation, git commit, push, or deployment has been implemented or enabled.

### Pre-Phase 4: Local Ollama & Multi-Provider LLM Integration (Completed)
- **Local Ollama Standardization**: Standardized Ollama on strictly local inference (`http://localhost:11434/v1`) with zero cloud credential requirements. `ollama_cloud` is completely removed to prevent unauthorized or silent cloud routing. Hardware resource analysis (8 GB M1, 13 GiB available disk) establishing suitability of compact coding models (`qwen2.5-coder:1.5b` or `llama3.2:3b`).
- **Multi-Cloud Integration**: Standard `httpx` OpenAI-compatible support for Groq (`GROQ_API_KEY` / `GROQ_API`), Google Gemini (`GEMINI_API_KEY` / `GEMINI_API`), and DeepSeek (`DEEPSEEK_API_KEY` / `DEEPSEEK_API`) with zero extra SDK dependencies.
- **Fail-Secure Architecture**: Strict cloud fallback prohibition (`FORGE_ALLOW_CLOUD_FALLBACK=false`), fail-closed behavior on local endpoint connection failures, bounded transient 5xx retries, typed rate-limit and authentication error mapping, and automated token scrubbing.


### Phase 1: Secure Repository Intelligence (Completed)
- **M1 — Architecture Baseline & Workspace Isolation**: Safe HTTPS/local repository acquisition, path traversal guards, and git hook suppression.
- **M2 — Static Repository Intelligence**: AST symbol indexing, language/framework detection, instruction file extraction, and token-bounded context budgeting.
- **M3 — Evidence Records, State Machine, & Storage**: Structured evidence, finding lifecycle transitions (`suspected -> reproduced -> root_caused -> fixed -> verified`), and SQLite event store.
- **M4 — Secure Container Sandbox Boundary**: Docker adapter with non-root user (`1000:1000`), `--network none`, `--read-only`, dropped capabilities, memory/CPU quotas, and secret redaction.
- **M5 — Bounded Investigation Orchestration**: Multi-turn typed LLM agent with tool calling (`list_dir`, `search`, `read_file`, `get_symbol_index`, `run_check`).
- **M6 — Evidence-Linked Reports & Benchmarking**: Seeded-bug test fixtures, precision/recall evaluation, and JSON/Markdown report generation.
- **M7 — Checkpoint & Comprehensive Verification**: End-to-end security audits, secret scanning, and host immutability verification.

### Phase 2: Diff-Aware Intelligent Review (Completed)
- **M1 — Diff-Aware Code Review Engine**: Read-only Git diff analysis comparing two revisions, accurate hunk and changed-line mapping, security vulnerability and defect detection on modified lines, and automated secret redaction.
- **M2 — Evidence-Calibrated Review Findings**: Tripartite finding structure separating observed facts from inferences and recommendations. Deterministic finding IDs, evidence verification against diff hunks, automatic confidence downgrading for unbacked claims, and strict lifecycle state machine enforcement.
- **M3 — Review Quality Benchmarking**: Versioned benchmark suite evaluating review quality covering genuine security/logic defects, harmless lookalikes, clean controls, attribution challenges, prompt injection, and secret redaction.
- **M4 — Adversarial Review Validation & Generalization**: Genuinely held-out adversarial fixture corpus, resolution of case-level vs finding-level counting units, evaluator self-tests, and defensible regression thresholds.
- **M5 — Security-Sensitive Dataflow & Secret-Exposure Detection**: AST-based security analysis and bounded dataflow tracking. Detects hardcoded credentials in nested dictionaries and Authorization headers, formatted token assembly via string interpolation, and sensitive values leaking into logging, print, or HTTP sinks (`--corpus security`).
- **M6 — CI Review Policies & SARIF Export**: Declarative CI policy enforcement with configurable severity/confidence thresholds, category filtering, auditable scoped rule suppressions, fail-closed evidence handling, and full OASIS SARIF 2.1.0 compliance.

### Phase 3: Defect Reproduction & Verification (Completed)
- **M1 — Sandbox Execution Contracts (Implemented)**: Typed execution contracts (`ReproductionRequest`, `ReproductionCommand`, `ExecutionEnvironmentSpec`, `ReproductionResult`) with strict schema validation, shell injection prevention, approved executable allowlists, resource limits, and fail-closed degradation.
- **M2 — Reproduction Case Management & Evidence Capture (Implemented)**: Deterministic reproduction case tracking, revision pinning, multi-attempt history, bounded/redacted evidence capture, SHA256 digests, field-level provenance, safe retry policy (forbidding automatic non-idempotent retries), resume validation against stale revisions, and lifecycle transition integration.
- **M3 — Deterministic Reproduction Planning (Implemented)**: Typed planning layer converting calibrated findings and trusted context into bounded, validated reproduction plans. Five strategies (`RUN_SPECIFIC_TEST`, `RUN_SCOPED_TEST_FILE`, `VALIDATE_BOUNDARY_CONDITION`, `CHECK_EXPECTED_OBSERVED`, `INSUFFICIENT_INFORMATION`). Deterministic plan IDs, full policy validator (14+ rejection criteria), CLI `forge plan` command group.
- **M4 — Outcome Classification & Evidence Validation (Implemented)**: Strongly typed 8-outcome taxonomy (`REPRODUCED`, `NOT_REPRODUCED`, `UNRELATED_FAILURE`, `ENVIRONMENT_BLOCKED`, `TIMEOUT`, `FLAKY`, `INVALID_PLAN`, `INCONCLUSIVE`) with deterministic reason codes, strict oracle presence and provenance checks (never infer reproduction from generic exit code or model assertion), precedence enforcement, and flaky retry detection.
- **M5 — Reproduction Reliability and Regression Hardening (Implemented)**: Full pipeline hardening against concurrency races, partial write rollback, SQLite unique attempt number constraints, immediate git revision TOCTOU detection, symlink escape checks using Path.is_relative_to, head/tail output bounding preservation with explicit markers, interruption/cleanup failure classification as INCONCLUSIVE, and standardized CLI exit codes (0: reproduced, 1: not reproduced/failed, 2: invalid plan/error, 3: environment blocked, 4: timeout/inconclusive/flaky).
- **M6 — End-to-End Reproduction Acceptance (Implemented)**: Comprehensive 12-scenario acceptance pass verifying end-to-end integration across case management, plan generation, offline validation, oracle provenance, fail-closed host protection, complete 8-outcome classification, SQLite persistence across process restarts, retry caps, finding lifecycle transition gating, standardized CLI exit codes (0–4), Phase 1/2 backward compatibility, and target repository immutability.

---

## Installation

Ensure Python 3.11+ is installed.

### Option A: Install via Standalone Distribution Wheel (.whl)
For standard operator workstations without requiring a git checkout:
```bash
python3 -m venv ~/forge-venv
source ~/forge-venv/bin/activate
pip install forge_core-0.1.0b1-py3-none-any.whl
```

### Option B: Developer / Editable Installation
For development, inspection, and benchmark execution:
```bash
git clone https://github.com/thisisanimesh01/FORGE.git
cd FORGE
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Verified CLI Usage

### System Diagnostics & Shell Completion
```bash
# Verify environment, Python, Git, SQLite, and Docker status
forge doctor

# Inspect sandbox runtime health, container configuration, and isolation limits
forge sandbox doctor

# Install shell auto-completion (bash, zsh, fish, or powershell)
forge --install-completion zsh
```

### Supervised End-to-End Engineering Workflow: Intake & Context (Phase 5 Milestone 1)
```bash
# 1. Create a task intake specification from an issue description
forge workflow intake create --type issue --repo /path/to/repo --issue-title "Division by zero" --issue-text "divide(10, 0) crash" --affected-file pkg/calculator.py --output intake.json

# 2. Create intake from a specific failing test command (optionally specifying affected implementation files)
forge workflow intake create --type test --repo /path/to/repo --test-file tests/test_calc.py --test-command "pytest tests/test_calc.py::test_zero" --affected-file pkg/calculator.py --output intake_test.json

# 3. Create intake from raw unified diff or patch file
forge workflow intake create --type diff --repo /path/to/repo --diff-file changes.diff --output intake_diff.json

# 4. Create intake from an existing review finding in SQLite
forge workflow intake create --type finding --repo /path/to/repo --finding-id find_01 --output intake_finding.json

# 5. Validate an intake specification offline and against repository
forge workflow intake validate intake.json --repo /path/to/repo

# 6. Display intake specification
forge workflow intake show intake.json

# 7. Assemble scoped repository context for a validated intake specification
forge workflow context build intake.json --output context.json

# 8. Assemble context with custom character budget
forge workflow context build intake.json --budget 15000 --output context.json

# 9. Inspect assembled context metadata
forge workflow context show context.json

# 10. Display the rendered markdown prompt ready for model consumption
forge workflow context show context.json --prompt-only
```

### Supervised End-to-End Engineering Workflow: Lifecycle & State Machine (Phase 5 Milestone 2)
```bash
# 1. Start an engineering workflow from a validated task intake specification
forge workflow start --intake intake.json

# 2. Start and auto-advance to INTAKE_VALIDATED and REPO_ANALYZED
forge workflow start --intake intake.json --repo /path/to/repo --auto-advance

# 3. Inspect workflow status and append-only event ledger timeline
forge workflow status wf_eng_38b1f8ac9210
forge workflow status wf_eng_38b1f8ac9210 --format json

# 4. List engineering workflows stored in SQLite
forge workflow list
forge workflow list --state awaiting_human_review
forge workflow list --intake-id intake_001

# 5. Safely resume a blocked or interrupted workflow
forge workflow resume wf_eng_38b1f8ac9210

# 6. Submit mandatory human review decision (approve or reject)
forge workflow review wf_eng_38b1f8ac9210 --decision approve --notes "Verified fix offline"
forge workflow review wf_eng_38b1f8ac9210 --decision reject --notes "Fix violates policy"
```

### Automated Evidence Chaining & Defect Reproduction (Phase 5 Milestone 3)
```bash
# 1. Execute defect reproduction and chain validated evidence into workflow
forge workflow repro wf_eng_38b1f8ac9210

# 2. Execute reproduction with explicit repository path and test command override
forge workflow repro wf_eng_38b1f8ac9210 --repo /path/to/repo --test-command "pytest tests/test_calc.py"

# 3. Plan evidence-grounded repair candidate for a reproduced workflow
forge workflow plan-repair wf_eng_38b1f8ac9210 --repo /path/to/repo

# 4. Plan repair with specific provider, model, and scratch directory
forge workflow plan-repair wf_eng_38b1f8ac9210 --provider ollama --model qwen2.5-coder:7b --scratch-dir /tmp/scratch

# 5. Sequentially advance workflow through intake, context, reproduction, and repair planning
forge workflow advance wf_eng_38b1f8ac9210 --repo /path/to/repo
```

### Multi-Check Regression & Safety Verification (Phase 5 Milestone 4)
```bash
# 1. Execute isolated multi-check verification against repair candidate
forge workflow verify wf_eng_38b1f8ac9210

# 2. Verify with repository override and dedicated scratch directory
forge workflow verify wf_eng_38b1f8ac9210 --repo /path/to/repo --scratch-dir /tmp/scratch

# 3. Verify skipping optional lint or security checks
forge workflow verify wf_eng_38b1f8ac9210 --no-lint --no-security

# 4. Verify with explicit baseline test command override
forge workflow verify wf_eng_38b1f8ac9210 --baseline-test "pytest -q tests/test_core.py"

# 5. Verify and output structured JSON report
forge workflow verify wf_eng_38b1f8ac9210 --format json

# 6. Sequentially advance workflow all the way through multi-check verification to human review gate
forge workflow advance wf_eng_38b1f8ac9210 --repo /path/to/repo --verify
```

### Certified Engineering Dossier & Operator Export (Phase 5 Milestone 5)
```bash
# 1. Export an approved workflow into a certified engineering dossier package
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output

# 2. Export with repository path for HEAD revision drift validation
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --repo /path/to/repo

# 3. Export with cryptographic HMAC-SHA256 signature using an operator key
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --key "operator-secret-token" --key-id "sec-ops-1"

# 4. Export with key loaded securely from file and force overwrite
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --force --key-file /path/to/key.txt

# 5. Export and output machine-readable JSON summary
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --format json

# 6. Validate an exported engineering dossier offline (checks SHA-256 digests and file completeness)
forge workflow validate-dossier ./dossier_output

# 7. Validate an exported engineering dossier with HMAC-SHA256 signature verification
forge workflow validate-dossier ./dossier_output --key "operator-secret-token"
forge workflow validate-dossier ./dossier_output --key-file /path/to/key.txt --format json
```

### End-to-End Workflow Acceptance & Reliability Suite (Phase 5 Milestone 6)
```bash
# 1. Execute the full Phase 5 end-to-end acceptance suite (30 scenarios across 10 domains)
forge workflow test

# 2. Execute acceptance suite and output machine-readable JSON report
forge workflow test --format json

# 3. Export acceptance test report to a markdown or json file
forge workflow test --output report.md
forge workflow test --format json --output report.json

# 4. Filter acceptance suite by specific scenario ID
forge workflow test --scenario P5_ACC01

# 5. Filter acceptance suite by domain category
forge workflow test --category human_review_gate
forge workflow test --category adversarial_intake

# 6. Opt-in to live Docker sandbox execution if available
forge workflow test --docker-live

# 7. Alias command for acceptance suite
forge workflow acceptance
```

### Evidence-Grounded Repair Planning & Isolated Verification (Phase 4 Milestones 1–2)
```bash
# Generate a bounded repair plan and candidate patch from an audited finding
forge repair plan find_01 --repo /path/to/repo --commit a1b2c3d4e5f6 --provider ollama

# Generate plan and export full structured JSON artifact
forge repair plan find_01 --output plans/plan_find_01.json --format json

# Inspect an existing repair plan and unified diff patch candidate
forge repair show plans/plan_find_01.json
forge repair show rpl_find_01_a1b2c3d4_7e3b9a1d

# Validate an existing repair plan offline against safety, scope, and budget rules
forge repair validate plans/plan_find_01.json

# List historical repair plans stored in SQLite
forge repair list
forge repair list --finding-id find_01

# Safely apply a candidate patch in an isolated scratch workspace and verify inside sandbox
forge repair verify rpl_find_01_a1b2c3d4_7e3b9a1d --repo /path/to/repo

# Verify a plan JSON file with custom verification command and JSON output
forge repair verify plans/plan_find_01.json --repo /path/to/repo --command "pytest tests/test_parser.py" --format json

# List historical verification attempts for a repair plan
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d --format json
```

### Repair Quality & Regression Benchmarking (Phase 4 Milestone 3)
```bash
# Run full offline repair quality benchmark suite (Markdown report)
forge benchmark repair

# Run using the repair command alias
forge repair benchmark

# Export benchmark results as formatted JSON
forge benchmark repair --format json

# Filter for a specific fixture category
forge benchmark repair --fixture REP01_syntax_error

# Export benchmark report to file
forge benchmark repair --output reports/repair_benchmark_report.md

# Opt-in live provider evaluation (requires explicit provider flag)
forge benchmark repair --mode provider --provider ollama --model qwen2.5-coder:7b
```

### Controlled Repair Orchestration & Human Review Gate (Phase 4 Milestone 4)
```bash
# Start an end-to-end repair workflow for a finding (plan-only dry-run mode)
forge repair workflow start find_001 --repo . --commit a1b2c3d4e5f6 --dry-run

# Start full workflow with sandboxed verification execution
forge repair workflow start find_001 --repo . --commit a1b2c3d4e5f6

# View workflow lifecycle state, candidate diff, and sequential audit trail
forge repair workflow status rwf_find_001_a1b2c3d4_abc123

# Export status report as formatted JSON
forge repair workflow status rwf_find_001_a1b2c3d4_abc123 --format json

# List repair workflows, optionally filtered by state or finding
forge repair workflow list
forge repair workflow list --state awaiting_human_review
forge repair workflow list --finding-id find_001 --format json

# Resume a blocked or paused workflow
forge repair workflow resume rwf_find_001_a1b2c3d4_abc123

# Submit mandatory human review decision (approve or reject)
forge repair workflow review rwf_find_001_a1b2c3d4_abc123 --decision approve --notes "Verified fix against regression suite"
forge repair workflow review rwf_find_001_a1b2c3d4_abc123 --decision reject --notes "Incomplete edge case handling"
```

### End-to-End Repair Reliability & Adversarial Acceptance Suite (Phase 4 Milestone 5)
```bash
# Execute the full 26-scenario end-to-end acceptance suite (Markdown report)
forge repair test

# Run via acceptance alias
forge repair acceptance

# Run a specific acceptance scenario by ID (e.g., ACC01, ACC15, ACC26)
forge repair test --scenario ACC01

# Export machine-readable JSON acceptance report with automated secret scrubbing
forge repair test --format json --output reports/acceptance_report.json
```

### Isolated Defect Reproduction Contracts (Phase 3 Milestone 1)
```bash
# Validate reproduction contract schema and security bounds offline
forge sandbox check contracts/reproduce_example.json

# Execute defect reproduction inside an isolated sandbox or report fail-closed skip
forge sandbox reproduce contracts/reproduce_example.json -o reports/repro_result.json
```

### Reproduction Case Management & Evidence Capture (Phase 3 Milestone 2)
```bash
# Create a tracked reproduction case pinned to target commit SHA
forge repro create contracts/auth_defect.json --commit a1b2c3d4e5f6 --max-retries 1

# List reproduction cases (optionally filter by finding ID)
forge repro list
forge repro list --finding-id find_01
forge repro list --format json

# Inspect a reproduction case and its full attempt history
forge repro show case_find_01_7e3b9a1d4c20
forge repro show case_find_01_7e3b9a1d4c20 --format json

# Classify a reproduction case and its attempt history
forge repro classify case_find_01_7e3b9a1d4c20
forge repro classify case_find_01_7e3b9a1d4c20 --format json

# Execute or resume a reproduction case attempt inside the sandbox
forge repro run case_find_01_7e3b9a1d4c20
forge repro run case_find_01_7e3b9a1d4c20 --resume
forge repro run case_find_01_7e3b9a1d4c20 --format json
```

### Deterministic Reproduction Planning (Phase 3 Milestone 3)
```bash
# Generate a bounded reproduction plan for a finding
forge plan generate find_01 \
  --commit a1b2c3d4e5f6 \
  --repo https://github.com/example/repo.git \
  --command '["pytest", "tests/test_parser.py::test_off_by_one"]' \
  --expected "Loop index must remain < len(arr)" \
  --provenance trusted_fixture \
  --output plans/plan_find_01.json

# Validate a plan offline without execution (fail-secure policy check)
forge plan validate plans/plan_find_01.json

# Classify a plan's execution viability and oracle provenance
forge plan classify plans/plan_find_01.json

# Inspect plan metadata, strategy rationale, and provenance
forge plan show plans/plan_find_01.json

# Validate and execute a reproduction plan inside the sandbox
forge plan run plans/plan_find_01.json --workspace /path/to/workspace
```

### Diff-Aware Code Review (Phase 2 Milestones 1, 2, 5, & 6)
```bash
# Review latest commit changes against previous revision
forge review <repo-path> --base HEAD~1 --head HEAD

# Review branch differences
forge review <repo-path> --base main --head feature-branch

# Offline static review without LLM assistance
forge review <repo-path> --base main --head HEAD --no-llm

# Output review as JSON or standards-compliant SARIF 2.1.0
forge review <repo-path> --format json
forge review <repo-path> --format sarif -o reports/review.sarif
```

### CI Review Policy Enforcement (Phase 2 Milestone 6)
```bash
# Evaluate review findings against default CI policy
forge policy check reports/review.json

# Gate CI pipeline using custom declarative policy
forge policy check reports/review.json --policy docs/examples/ci_review_policy.json

# Output machine-readable policy decision for CI/CD pipelines
forge policy check reports/review.json --format json
```

### Review Quality & Security Benchmarks (Phase 2 Milestones 3, 4, & 5)
```bash
# Run standard review quality benchmarks
forge benchmark review

# Run held-out adversarial benchmark corpus
forge benchmark review --corpus heldout

# Run security dataflow benchmark corpus
forge benchmark review --corpus security

# Output benchmark metrics to JSON
forge benchmark review --format json -o reports/review_benchmark.json
```

### Repository Auditing & Investigation (Phase 1)
```bash
# Safely acquire and inspect repository structure (read-only)
forge inspect <repo-url-or-path> --no-llm

# Multi-turn LLM investigation
forge investigate <repo-url-or-path>

# Export audit report
forge report <run-id> --format markdown -o reports/run.md

# Audit history & findings
forge runs
forge findings list
forge findings show <finding-id>

# Phase 1 repository benchmark suite
forge benchmark run --mode static
forge benchmark run --mode llm
```

---

## Benchmark Metrics & Evaluation

FORGE measures detection accuracy against curated ground-truth fixtures:
- **True Positive (TP)**: Target defect correctly detected in affected file and symbol.
- **False Positive (FP)**: Defect falsely claimed on clean control fixtures or unaffected files.
- **False Negative (FN)**: Target defect missed by the analysis.
- **True Negative (TN)**: Clean control fixture correctly identified with 0 defect findings.
- **Precision**: $\frac{TP}{TP + FP}$ (N/A if no defects reported).
- **Recall**: $\frac{TP}{TP + FN}$ (N/A if no seeded defects present).
- **F1 Score**: Harmonic mean of Precision and Recall.
- **Case Pass Rate**: Percentage of benchmark cases where outcome matched ground truth.

---

## Sandboxing & Fail-Closed Security Policy

FORGE enforces strict container isolation when running code from target repositories:
- **Network Isolation**: Containers are launched with `--network none`; dynamic calls have zero external network access.
- **Read-Only Filesystem**: The target workspace is mounted read-only (`--mount type=bind,...,readonly`); repository code cannot mutate host files.
- **Non-Root Execution**: Runs strictly as UID:GID `1000:1000`.
- **Privilege Dropping**: Configured with `--security-opt no-new-privileges` and `--cap-drop ALL`.
- **Fail-Closed Host Protection**: When Docker is unavailable, dynamic execution is marked `SKIPPED_SANDBOX_UNAVAILABLE` (or `BLOCKED`). FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- **Real Integration Validation**: Opt-in integration tests verify all isolation boundaries against a live Docker daemon:
  ```bash
  FORGE_DOCKER_TESTS=1 pytest tests/integration/test_docker_sandbox_integration.py
  ```

---

## Configuration

Copy `.env.example` to `.env` and configure as needed:
```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `FORGE_LLM_PROVIDER` | `ollama` | Provider: `ollama` (default local), `groq`, `gemini`, `deepseek`, or `fake` (offline) |
| `FORGE_LLM_MODEL` | *(empty)* | Model identifier override (e.g. `qwen2.5-coder:1.5b`; provider defaults used when empty) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Base URL for local Ollama server (compatible with Friday convention) |
| `GROQ_API_KEY` | *(empty)* | API key for Groq Cloud (compatible with Friday convention) |
| `GEMINI_API_KEY` | *(empty)* | API key for Google Gemini (compatible with Friday convention) |
| `DEEPSEEK_API_KEY` | *(empty)* | API key for DeepSeek (compatible with Friday convention) |
| `FORGE_ALLOW_CLOUD_FALLBACK` | `false` | Security guard: silent cloud fallback is strictly forbidden |
| `FORGE_LLM_API_BASE` | *(empty)* | Custom endpoint base URL override (vLLM, custom proxies) |
| `FORGE_MAX_STEPS` | `10` | Max investigation iterations |
| `FORGE_DOCKER_IMAGE` | `python:3.11-slim` | Base container image for sandboxed execution |

### Local Ollama Setup & Usage

FORGE standardizes on local Ollama execution (`http://localhost:11434/v1`). Hosted/cloud Ollama inference is explicitly rejected.

1. **Install and Run Ollama**: Ensure Ollama is running locally:
   ```bash
   ollama serve
   ```
2. **Download Model**:
   ```bash
   ollama pull qwen2.5-coder:1.5b
   ```
3. **Configure Environment**:
   ```bash
   export FORGE_LLM_PROVIDER=ollama
   export FORGE_LLM_MODEL=qwen2.5-coder:1.5b
   export OLLAMA_BASE_URL=http://localhost:11434  # default
   ```
4. **Guarantees & Troubleshooting**:
   - **No Silent Cloud Routing**: Requesting `ollama_cloud` fails immediately with `LLMUnavailableError`.
   - **Local Network Failure**: If local Ollama is down or connection is refused, FORGE fails closed with `LLMUnavailableError` without falling back to cloud providers or fake mocks (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
   - **Model Availability**: If the requested model is not installed locally, Ollama returns an error and FORGE surfaces the failure honestly.
   - **Structured Output Support**: FORGE sends an explicit OpenAI-compatible `response_format` with `json_schema` derived from `RepairModelResponseSchema` to ensure strict JSON output.
   - **Syntax Error Recovery & Bounded Retries**: If a model generates malformed JSON (such as a trailing comma before a closing brace), FORGE rejects permissive rewriting and instead performs a bounded retry (max 3 retries, 120s timeout, 50,000 char limit) with concise diagnostic feedback so the local model can correct its output. If retries are exhausted, it fails closed safely.

---

## Documentation & Architecture References

- Engineering Rules & Mandatory Release Gates: [`AGENTS.md`](file:///Users/animeshyadav/Desktop/project/forge/AGENTS.md)
- Phase 1 Specification: [`docs/specs/phase-1.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-1.md)
- Phase 2 Specifications:
  - Diff Review: [`docs/specs/phase-2-milestone-1.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-1.md)
  - Evidence Calibration: [`docs/specs/phase-2-milestone-2.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-2.md)
  - Review Benchmarks: [`docs/specs/phase-2-milestone-3.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-3.md)
  - Adversarial Generalization: [`docs/specs/phase-2-milestone-4.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-4.md)
  - Security Dataflow Analysis: [`docs/specs/phase-2-milestone-5.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-5.md)
  - CI Review Policy & SARIF: [`docs/specs/phase-2-milestone-6.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-6.md)
- Phase 3 Specifications:
  - Sandbox Execution Contracts: [`docs/specs/phase-3-milestone-1.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-1.md)
  - Case Management & Evidence Capture: [`docs/specs/phase-3-milestone-2.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-2.md)
  - Deterministic Reproduction Planning: [`docs/specs/phase-3-milestone-3.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-3.md)
  - Outcome Classification & Evidence Validation: [`docs/specs/phase-3-milestone-4.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-4.md)
  - Reproduction Reliability & Regression Hardening: [`docs/specs/phase-3-milestone-5.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-5.md)
  - End-to-End Reproduction Acceptance: [`docs/specs/phase-3-milestone-6.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-6.md)
- Phase 3→4 Transition & Pre-Phase 4:
  - Multi-Provider Configuration & Credential Reuse: [`docs/specs/phase-3-provider-transition.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-provider-transition.md)
  - Local Ollama & Multi-Provider LLM Integration: [`docs/specs/pre-phase-4-llm-providers.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/pre-phase-4-llm-providers.md)
  - Local Ollama Standardization & Cloud Removal: [`docs/specs/local-ollama-standardization.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/local-ollama-standardization.md)
  - Ollama Cloud Provider Integration (Superseded): [`docs/specs/ollama-cloud-provider.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/ollama-cloud-provider.md)
  - Removal of OpenAI API Provider: [`docs/specs/remove-openai-provider.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/remove-openai-provider.md)
  - Phase 4 Readiness & Sandbox Gate: [`docs/specs/phase-4-readiness.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-readiness.md)
- Phase 4 Specifications:
  - Evidence-Grounded Repair Planning & Patch Generation: [`docs/specs/phase-4-m1-repair-planning.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m1-repair-planning.md)
  - Isolated Patch Application & Verification: [`docs/specs/phase-4-m2-patch-verification.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m2-patch-verification.md)
  - Repair Quality & Regression Benchmarking: [`docs/specs/phase-4-m3-repair-benchmarks.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m3-repair-benchmarks.md)
  - Controlled Repair Orchestration & Lifecycle: [`docs/specs/phase-4-m4-repair-orchestration.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m4-repair-orchestration.md)
  - End-to-End Repair Reliability & Adversarial Testing: [`docs/specs/phase-4-m5-reliability.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m5-reliability.md)
  - Final Acceptance & Release Readiness: [`docs/specs/phase-4-m6-acceptance.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m6-acceptance.md)
  - Docker Integration Validation & Deterministic Test Repair: [`docs/specs/docker-integration-validation.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/docker-integration-validation.md)
- Phase 5 Specifications:
  - Supervised End-to-End Engineering Workflow Architecture & Roadmap: [`docs/specs/phase-5-roadmap.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-roadmap.md)
  - Unified Task Intake & Repository Context Pipeline: [`docs/specs/phase-5-m1-intake-context.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m1-intake-context.md)
  - Supervised Workflow State Machine & Transactional Ledger: [`docs/specs/phase-5-m2-workflow-ledger.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m2-workflow-ledger.md)
  - Automated Evidence Chaining from Reproduction to Repair: [`docs/specs/phase-5-m3-evidence-chaining.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m3-evidence-chaining.md)
  - Multi-Check Regression & Safety Verification: [`docs/specs/phase-5-m4-multi-check-verification.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m4-multi-check-verification.md)
  - Certified Engineering Dossier & Operator Export: [`docs/specs/phase-5-m5-dossier-export.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m5-dossier-export.md)
  - End-to-End Workflow Acceptance, Reliability & Release: [`docs/specs/phase-5-m6-acceptance.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m6-acceptance.md)
  - Repair Planner Grounding, Applicability & Scope Safety: [`docs/specs/repair-planner-reliability.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/repair-planner-reliability.md)
  - Local Ollama Structured Output & Syntax Error Recovery: [`docs/specs/local-ollama-structured-output.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/local-ollama-structured-output.md)
  - Patch Generation Reliability & Diff Syntax Enforcement: [`docs/specs/patch-generation-reliability.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/patch-generation-reliability.md)
  - Structured Patch Construction: [`docs/specs/structured-patch-construction.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/structured-patch-construction.md)
  - Structured Proposal Wire-Schema Reliability: [`docs/specs/structured-proposal-wire-schema.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/structured-proposal-wire-schema.md)
  - Deterministic Patch Anchoring & Minimal Replacements: [`docs/specs/patch-anchoring-minimal-replacements.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/patch-anchoring-minimal-replacements.md)
  - Deterministic Edit Anchoring & Source-Grounded Repair: [`docs/specs/deterministic-edit-anchoring.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/deterministic-edit-anchoring.md)
  - Repair Reliability & Generalization: [`docs/specs/repair-reliability-generalization.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/repair-reliability-generalization.md)
- Example CI Policy: [`docs/examples/ci_review_policy.json`](file:///Users/animeshyadav/Desktop/project/forge/docs/examples/ci_review_policy.json)
