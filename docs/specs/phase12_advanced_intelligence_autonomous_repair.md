# Phase 12 Specification: Advanced Intelligence & Autonomous Defect Repair

## Overview

Phase 12 builds upon FORGE's foundational repository understanding (Phase 11) to deliver autonomous, evidence-driven software engineering capabilities. It empowers FORGE to diagnose runtime and test failures, evaluate diagnostic hypotheses against disk facts, perform fault localization, and plan autonomous repairs while upholding core safety and security invariants:
- **Fail-Secure Investigation**: Zero arbitrary code execution on the host machine.
- **Evidence-Grounded Invariant**: A model suspicion never automatically becomes a confirmed defect. Confirmation requires verified disk facts, real line numbers, and concrete failure signals.
- **Strict Typed Contracts**: All inputs, hypotheses, candidate locations, and results are governed by strict Pydantic models with `extra="forbid"`.
- **Zero Cloud Leakage**: LLM interactions strictly target local Ollama endpoints without falling back to cloud services.
- **Boundary & Metadata Safety**: Repository protected files, sensitive secrets, and Git metadata are strictly isolated from analysis inputs.

---

## Milestone Roadmap

- **Milestone 1**: Evidence-Driven Defect Diagnosis `[COMPLETED]`
  - Structured input contracts ingesting user descriptions, test failure reports, error messages, tracebacks, changesets, and static findings with strict character budgets.
  - Deterministic 12-category defect classification taxonomy.
  - Multi-source failure localizer extracting and ranking candidate locations from traceback crash frames, error messages, test callee invocations, target symbols, and change-impact analysis.
  - Hypothesis formulation and scope-confined contradiction detection detecting existing guards and handlers.
  - Strict defect confirmation rules requiring grounded source evidence, verified disk lines, failure signals, and zero unrefuted contradictions.
  - Persistent storage integration in SQLite via `ForgeStore` and `EvidenceGroundingEngine`.
  - Dedicated milestone documentation in [`docs/milestones/phase-12-m1-evidence-driven-diagnosis.md`](../milestones/phase-12-m1-evidence-driven-diagnosis.md).
- **Milestone 2**: Root-Cause Analysis Engine `[COMPLETED]`
  - Backward causal tracing and forward failure propagation over cross-file call and module dependency graphs.
  - AST-based semantic contradiction and guard inspection for conditional guards and exception handlers.
  - Strict certainty semantics: PROVEN, PLAUSIBLE, MULTIPLE_PLAUSIBLE_ROOT_CAUSES, INSUFFICIENT_EVIDENCE, UNRESOLVED.
  - Dedicated milestone documentation in [`docs/milestones/phase-12-m2-root-cause-analysis.md`](../milestones/phase-12-m2-root-cause-analysis.md).
- **Milestone 3**: Repair Planning Engine `[PENDING]`
- **Milestone 4**: Controlled Autonomous Repair `[PENDING]`
- **Milestone 5**: Repair Verification `[PENDING]`
- **Milestone 6**: Repair Quality & Reliability `[PENDING]`

---

## Milestone 1 Architecture: Evidence-Driven Defect Diagnosis

### 1. Data Contracts & Schemas (`forge.diagnosis.schemas`)

#### Input Contracts (`DiagnosisInput`)
Ingests multi-modal diagnostic signals:
- `user_description`: Optional string bounded to 10,000 characters.
- `test_failure`: Structured `TestFailureReport` containing `test_name`, `test_file`, `failure_message` (clamped to 5,000 chars), `stack_trace` (clamped to 15,000 chars), and `line_number`. Marked with `__test__ = False` to prevent pytest collection conflicts.
- `static_finding`: Structured `StaticFindingReport` containing `rule_id`, `file_path`, `line_number`, `message`, and `severity`.
- `target_files` / `target_symbols`: Explicit focus sets.
- `diff` / `modified_files`: Unified diff (clamped to 50,000 chars) and change set for correlation.
- `error_message` / `stack_trace`: Direct failure strings.
- `expected_behavior` / `actual_behavior`: Observed vs expected behavior strings (each $\le 5,000$ chars).
- Validation: Requires at least one signal; forbids extra inputs (`extra="forbid"`).

#### Output Contracts (`DiagnosisResult` & `DiagnosisHypothesis`)
- `DiagnosisResult`: Structured outcome including `diagnosis_id`, `failing_behavior`, `defect_category`, `severity`, `status` (`CONFIRMED`, `SUSPECTED`, `UNRESOLVED`), `confidence`, `confidence_score` (in `[0.0, 1.0]`), `root_cause_locations`, `hypotheses`, `supporting_evidence_ids`, `contradicting_evidence_ids`, and actionable `verification_requirements`.
- `DiagnosisHypothesis`: Candidate explanation linking a `CandidateLocation`, `DefectCategory`, `confidence_score`, `supporting_evidence_ids`, and `contradicting_evidence_ids`.

---

### 2. Failure Localizer (`forge.diagnosis.localization`)

The `FailureLocalizer` resolves candidate defect locations using:
1. **Traceback Analysis**: Extracts frames matching repository files; scores top crash frames at 0.90 confidence weight.
2. **Error Message Parsing**: Regex-based extraction of `File "...", line X` and `path.py:X`.
3. **Test Callee Correlation**: Inquires `CallGraph` for direct production callees of failing test functions.
4. **Target Symbols Query**: Queries `SymbolIndex` for matching symbol declarations.
5. **Change-Impact Analysis**: Integrates `ChangeImpactAnalyzer` to map modified files and impacted symbols to candidate locations.
6. **Path Containment & Security**: Drops non-repository paths, traversal attempts, and protected paths (`.git/config`, `AGENTS.md`).
7. **Production Precedence**: Test harness files are discounted and sorted behind production source code.

---

### 3. Hypothesis Generation & Contradiction Detection (`forge.diagnosis.hypothesis`)

- **Deterministic Taxonomy Inference**: Maps error patterns to 12 standard defect categories (`LOGIC_ERROR`, `INCORRECT_DATA_HANDLING`, `INPUT_VALIDATION_FAILURE`, etc.).
- **Enclosing Function Contradiction Scope**: Inspects code within the enclosing `def ` boundary. If a candidate claims a missing check/guard, but an explicit check raising an exception exists on disk, a contradicting evidence record is registered and the hypothesis is penalized (-0.40).
- **Disk Grounding Rule**: If candidate lines do not exist on disk, confidence score drops to `0.0`.
- **Local Ollama Integration**: Optionally enriches explanations via local Ollama inference (`localhost:11434`) using structured JSON formats with fail-closed error handling.

---

### 4. Diagnosis Engine (`forge.diagnosis.engine`)

Coordinates the entire pipeline:
1. Dispatches inputs to `FailureLocalizer`.
2. Formulates and grounds hypotheses via `HypothesisEvaluator` and `EvidenceGroundingEngine`.
3. Synthesizes overall status:
   - `CONFIRMED`: Verified location + direct failure signal + direct grounded evidence + zero unrefuted contradictions + no competing unrefuted hypotheses ($\ge 0.70$) + known category + high confidence ($\ge 0.75$).
   - `SUSPECTED`: Plausible hypothesis backed by partial evidence or presence of competing hypotheses.
   - `UNRESOLVED`: Inconclusive, unverified, or missing failure signals.
4. Persists findings and attaches evidence records in `ForgeStore` when enabled.

---

## Milestone 2 Architecture: Root-Cause Analysis Engine

### 1. Causal Data Contracts (`forge.causality.schemas`)
- **Causal Roles**: `ROOT_CAUSE`, `CAUSAL_MECHANISM`, `CONTRIBUTING_FACTOR`, `PROPAGATOR`, `SYMPTOM`.
- **Semantic Relationships**: `CALLS`, `DEPENDS_ON`, `PASSES_INVALID_DATA`, `VIOLATES_PRECONDITION`, `MISUSES_RESOURCE`, `PROPAGATES_EXCEPTION`, `INVALIDATES_STATE`, `TRIGGERS_FAILURE`, `CONTRIBUTES_TO`, `UNKNOWN`.
- **Causal Nodes & Edges**: `CausalNode` and `CausalEdge` tracking disk verification, line bounds, and supporting evidence references.
- **Linear Causal Chains**: `CausalChain` models ordered paths from origin cause to symptom with deterministic confidence scores.
- **Status Classification**:
  - `PROVEN`: All steps in primary chain directly verified with exact source evidence.
  - `PLAUSIBLE`: Plausible chain backed by grounded evidence but containing inferred links.
  - `MULTIPLE_PLAUSIBLE_ROOT_CAUSES`: Multiple distinct candidate causes supported by evidence without differentiating signals.
  - `INSUFFICIENT_EVIDENCE`: Missing, ungrounded, or stale evidence.
  - `UNRESOLVED`: Conflicting or contradictory evidence.

---

### 2. AST Contradiction & Semantic Guard Inspector (`forge.causality.contradiction`)
- Inspects executable Python AST structures within the innermost enclosing function scope.
- Identifies `ast.If` validation guards, `ast.Assert` statements, and `ast.Try`/`ast.ExceptHandler` blocks.
- Disregards comments and string literals, preventing false-positive contradictions from text mentioning validation.

---

### 3. Backward Causal Tracer & Forward Propagation (`forge.causality.tracer`)
- Traverses backward callers via `CallGraph.get_call_edges(target_id=...)` and module dependents via `ModuleDependencyGraph.get_dependents()`.
- Traces downstream effect propagation from suspected root causes to symptoms.
- Enforces maximum depth and node count bounds, guaranteeing cycle safety and recursion termination.
- Distinguishes directly verified call edges (`is_directly_verified = True`, 0.90 confidence) from inferred data/state edges (0.65 confidence).

---

### 4. Root-Cause Analysis Engine (`forge.causality.engine`)
- Ingests verified M1 `DiagnosisResult`, `TestFailureReport`, or direct symbol/location inputs.
- Audits evidence freshness with `verify_evidence_freshness()`.
- Synthesizes linear causal chains and determines root-cause status without forcing a single cause when multiple are plausible.
- Optionally enriches narratives via local Ollama inference, failing closed if unavailable.

---

### 5. Causality Tools & ToolRegistry Integration (`forge.causality.tools`)
- Exposes 5 structured read-only tools to `ToolRegistry`: `analyze_root_cause`, `trace_causal_chain`, `inspect_contributing_factors`, `compare_root_cause_hypotheses`, `explain_failure_propagation`.
