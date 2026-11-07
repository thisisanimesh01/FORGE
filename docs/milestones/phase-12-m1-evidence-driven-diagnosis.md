# Phase 12, Milestone 1: Evidence-Driven Defect Diagnosis

## 1. Executive Summary & Objective

Phase 12 Milestone 1 delivers the **Evidence-Driven Defect Diagnosis Engine** for FORGE, bridging deep repository intelligence (symbol indexing, cross-file call graphs, module dependencies, and change-impact analysis) with structured defect hypothesis evaluation and root-cause localization.

Prior to Milestone 1, FORGE could inspect repositories and answer model queries via read-only tools, but lacked an automated, evidence-grounded diagnosis engine capable of ingesting error messages, tracebacks, test failures, and user descriptions to reliably pinpoint defect origins with deterministic confidence and anti-fabrication guarantees.

### Core Invariant
**A model-generated suspicion must NEVER automatically become a confirmed defect.**
Confirmation strictly requires:
1. Candidate location is verified against current disk content and line bounds.
2. Direct grounded source evidence is attached.
3. Direct failure signal exists (runtime crash traceback frame, test assertion failure, or error message).
4. Top hypothesis confidence is `HIGH` (numeric score $\ge 0.75$).
5. Zero unrefuted contradicting evidence on disk (e.g. existing guards or exception handlers).
6. Defect category is classified into a known deterministic category (not `UNKNOWN`).
7. Zero competing unrefuted hypotheses with confidence score $\ge 0.70$.

---

## 2. Architecture & Module Design

The defect diagnosis subsystem resides in `app/forge/diagnosis/`:

```
app/forge/diagnosis/
├── __init__.py         # Package exports (Engine, Localizer, Evaluator, Schemas)
├── schemas.py          # Strict Pydantic contracts (DiagnosisInput, Result, Hypothesis, Location)
├── localization.py     # FailureLocalizer: multi-signal crash frame & symbol localization
├── hypothesis.py       # HypothesisEvaluator: deterministic categorization & contradiction check
└── engine.py           # EvidenceDrivenDiagnosisEngine: end-to-end diagnosis orchestrator
```

### 2.1 Data Contracts & Input Models (`schemas.py`)
All models enforce `model_config = ConfigDict(extra="forbid")` with strict character budgets:
- `user_description`: $\le 10,000$ characters
- `stack_trace`: $\le 15,000$ characters
- `diff`: $\le 50,000$ characters
- `error_message`: $\le 5,000$ characters
- `expected_behavior` / `actual_behavior`: $\le 5,000$ characters
- `reproduction_steps`: $\le 10,000$ characters
- `TestFailureReport`: structured test failure with message, traceback, file, line. Set `__test__ = False` to prevent pytest collection confusion.
- `StaticFindingReport`: structured linter or static rule diagnostic with line and severity.
- `CandidateLocation`: verified source location with 1-indexed line numbers, provenance mechanism, and confidence weight.

### 2.2 Defect Classification Taxonomy
A deterministic 12-category classification:
1. `LOGIC_ERROR`: branching mistakes, incorrect operators, arithmetic zero-division.
2. `INCORRECT_DATA_HANDLING`: KeyError, IndexError, NoneType attribute errors, unhandled nulls.
3. `INPUT_VALIDATION_FAILURE`: missing preconditions, unvalidated arguments, ValueError, TypeError.
4. `EXCEPTION_HANDLING_DEFECT`: unhandled exceptions, incorrect catch blocks, swallow errors.
5. `RESOURCE_MANAGEMENT_ISSUE`: unclosed file handles, connection leaks, FileNotFoundError.
6. `DEPENDENCY_INTEGRATION_DEFECT`: broken imports, cycle violations, incompatible external APIs.
7. `CONCURRENCY_ISSUE`: race conditions, deadlocks, asyncio event loop issues.
8. `SECURITY_VULNERABILITY`: injections, directory traversal, credential leakage.
9. `PERFORMANCE_DEFECT`: algorithmic bottlenecks, timeout errors, infinite loops.
10. `CONFIGURATION_DEFECT`: missing environment variables, invalid settings files.
11. `TEST_BUILD_FAILURE`: assertion errors in test harnesses or broken build fixtures.
12. `UNKNOWN`: inconclusive or ambiguous symptoms.

### 2.3 Failure Localization Engine (`localization.py`)
The `FailureLocalizer` extracts candidate locations across multiple evidence sources:
- **Traceback Parsing**: Extracts file and line numbers, discounting Python stdlib/site-packages, prioritizing crash frames (0.90) over intermediate frames.
- **Error Message Parsing**: Regex-based extraction of `File "...", line X` and `path/to/file.py:X`.
- **Test Callee Correlation**: Maps failing test functions to direct production code callees via `CallGraph.get_direct_callees`.
- **Target Symbols Query**: Queries `SymbolIndex` for explicit function/class references.
- **Change-Impact Analysis**: Links modified files and affected symbol dependents to candidates.
- **Security Boundaries**: Strictly enforces path containment, rejecting directory traversal (`../../etc/passwd`) and protected paths (`.git/config`, `AGENTS.md`).
- **Production Precedence**: Candidates in test files (`tests/`) are automatically discounted and sorted behind production source code.

### 2.4 Hypothesis Formulation & Contradiction Detection (`hypothesis.py`)
- Formulates hypotheses deterministically for candidate locations.
- **Contradiction Detection**: Inspects source code within the enclosing function/scope without crossing `def ` boundaries. If an input claims missing validation or guards but the disk code actually contains `if ...: raise ValueError`, a contradicting evidence record is generated and the hypothesis score is penalized (-0.40).
- **Disk Verification Rule**: Unverified locations on disk immediately drop confidence to `0.0` (`LOW`).
- **Local Ollama Integration**: Optionally queries local Ollama (`localhost:11434`) for rich diagnostic explanations with strict JSON schema parsing, failing closed safely without cloud fallback.

### 2.5 Storage Integration & Finding Persistence (`engine.py`)
- When `persist_finding=True` and `store` is configured, creates a persistent finding in `ForgeStore` and links all grounded evidence records atomically in SQLite.
- Computes severity independently of confidence (e.g. `SECURITY_VULNERABILITY` -> `CRITICAL`, `RESOURCE_MANAGEMENT_ISSUE` -> `HIGH`, `LOGIC_ERROR` -> `HIGH`/`MEDIUM`).

---

## 3. Verification & Test Suite

Comprehensive test suite in `tests/unit/test_phase12_m1_diagnosis.py` (24 unit tests):
1. `test_diagnosis_input_requires_at_least_one_signal` — Validates signal requirement.
2. `test_diagnosis_input_extra_fields_forbidden` — Enforces `extra="forbid"`.
3. `test_diagnosis_input_character_budgets_enforced` — Bounds checks on all string inputs.
4. `test_test_failure_report_truncation` — Truncation of oversized test error messages.
5. `test_candidate_location_line_ordering` — Rejects `end_line < start_line`.
6. `test_localization_from_traceback` — Traceback frame extraction & ranking.
7. `test_localization_from_error_message` — Inline error message file & line parsing.
8. `test_localization_from_target_symbols` — Symbol index lookup and resolution.
9. `test_localization_security_rejections` — Traversal and protected file rejection.
10. `test_deterministic_category_inference` — Pattern-based defect classification.
11. `test_contradiction_detection_when_guard_exists` — Detection of code contradicting claims.
12. `test_unverified_candidate_drops_confidence_score` — Score zeroing on ungrounded paths.
13. `test_model_suspicion_without_failure_signal_cannot_confirm` — Anti-fabrication gate.
14. `test_verified_location_and_crash_signal_can_confirm` — Confirmed diagnosis requirements.
15. `test_competing_hypotheses_prevent_confirmation` — Competing explanations keep status suspected.
16. `test_verify_evidence_freshness` — Sha256 mismatch detection on disk modification.
17. `test_store_diagnosis_finding_persistence` — Finding & evidence persistence in SQLite.
18. `test_ollama_failure_graceful_fallback` — Fail-closed local inference without cloud fallback.
19. `test_end_to_end_test_failure_diagnosis` — Complete diagnostic pipeline execution.
20. `test_static_finding_localization` — Static analysis report localization.
21. `test_target_files_localization` — Explicit target file candidate extraction.
22. `test_severity_assignment_mapping` — Severity assignment by category.
23. `test_llm_hypothesis_enrichment_structured` — Ollama JSON structured enrichment.
24. `test_llm_malformed_response_handled_gracefully` — Graceful handling of invalid model output.

### Regression Verification
- **Unit Test Suite**: 24/24 passed in 0.56s.
- **Full Project Regression Suite**: 1,772 passed, 7 skipped in 187.13s (0:03:07). Zero failures, zero regressions.

---

## 4. Benchmark & Performance Results

Evaluated on the full FORGE codebase:
- **Warmup Latency (Cold Start)**: 2,955.17 ms (~2.96 seconds, including full repository index, call-graph and dependency graph resolution).
- **Subsequent Latency (Cached / Incremental)**:
  - Mean: **2.05 ms**
  - Min: **1.92 ms**
  - Max: **2.27 ms**
- **Diagnosis Accuracy**: Correctly pinpointed crash location to exact file and line (`app/forge/llm/repo_tools.py:280`) with `CONFIRMED` status, `FindingConfidence.HIGH (1.0)`, and zero false contradictions.

---

## 5. Security & Hygiene Review

- **Zero Cloud Leakage**: LLM interactions are strictly restricted to local Ollama endpoints.
- **Fail-Secure Execution**: No arbitrary code from target repositories is dynamically executed.
- **Path Isolation**: Directory traversal patterns and protected files (`.git/config`, `AGENTS.md`) are rejected during localization.
- **Secret Redaction**: Secret redaction active on all error strings and evidence excerpts.
- **Git Metadata Safety**: No changes to repository git metadata.
