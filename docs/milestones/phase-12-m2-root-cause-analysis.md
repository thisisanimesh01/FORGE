# Phase 12, Milestone 2: Root-Cause Analysis Engine

**Milestone Report**
**Repository:** `FORGE`
**Milestone:** Phase 12 M2 — Root-Cause Analysis Engine
**Status:** Completed
**Branch:** `phase-12-m2-root-cause-analysis`

---

## 1. Executive Summary

Phase 12 Milestone 2 delivers a production-grade, evidence-backed Root-Cause Analysis (RCA) Engine for FORGE. Reusing the rich cross-file call graphs, module dependency DAGs, change-impact analysis, and incremental indexing established in Phase 11 alongside the multi-signal defect diagnoses from Phase 12 M1, the RCA engine traces defect manifestations backward to their origins and models forward failure propagation.

Crucially, the engine enforces strict anti-fabrication invariants:
1. **Never Label Proven Without Direct Proof**: A causal explanation is never labeled `PROVEN` unless every step in its supporting chain is independently verified against direct source evidence, real line numbers, and call/dependency graph edges.
2. **Distinguish Roles & Certainty**: Strictly differentiates between symptom, contributing factor, causal mechanism, and root cause.
3. **Preserve Uncertainty & Multi-Root Realities**: When multiple candidate roots or competing causal chains are supported by available evidence without differentiating signals, the engine reports `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` or `INSUFFICIENT_EVIDENCE` rather than arbitrarily forcing a single cause.
4. **AST-Based Semantic Guard Inspection**: Replaces brittle syntactic heuristics with AST-level inspection of conditional guards (`ast.If`, `ast.Assert`), exception handlers (`ast.Try`, `ast.ExceptHandler`), and return paths while ignoring comments and string literals.
5. **Local Ollama Inference Only**: All optional LLM reasoning strictly targets local Ollama endpoints (`localhost:11434`) without remote fallbacks.

---

## 2. Architecture & Component Design

The implementation is packaged under `app/forge/causality/`:

```
app/forge/causality/
├── __init__.py           # Package exports and public interfaces
├── schemas.py            # Strict Pydantic models for causal graphs, chains, and reports
├── contradiction.py      # AST-based semantic contradiction and guard inspector
├── tracer.py             # Backward causal tracer and forward propagation engine
├── engine.py             # RootCauseAnalysisEngine orchestrating diagnosis, tracing, and status
└── tools.py              # CausalityTools integrating with ToolRegistry for agent execution
```

### 2.1 Causal Data Contracts (`schemas.py`)
- **Semantic Relationships (`CausalRelationship`)**: `CALLS`, `DEPENDS_ON`, `PASSES_INVALID_DATA`, `VIOLATES_PRECONDITION`, `MISUSES_RESOURCE`, `PROPAGATES_EXCEPTION`, `INVALIDATES_STATE`, `TRIGGERS_FAILURE`, `CONTRIBUTES_TO`, `UNKNOWN`.
- **Causal Roles (`CausalRole`)**: `ROOT_CAUSE`, `CAUSAL_MECHANISM`, `CONTRIBUTING_FACTOR`, `PROPAGATOR`, `SYMPTOM`.
- **Node Architectures (`CausalNodeType`)**: `FUNCTION`, `METHOD`, `CLASS`, `MODULE`, `STATEMENT`, `EXPRESSION`, `TEST`, `EXTERNAL`.
- **Outcome Statuses (`CausalStatus`)**:
  - `PROVEN`: All links in the primary causal chain are directly verified against disk and call graphs with zero unrefuted contradictions.
  - `PLAUSIBLE`: Plausible chain backed by grounded evidence but containing inferred links (e.g. data flow or state invalidation).
  - `MULTIPLE_PLAUSIBLE_ROOT_CAUSES`: Multiple distinct candidate root causes supported by evidence without discriminating test signals.
  - `INSUFFICIENT_EVIDENCE`: Failure symptom or supporting evidence is missing, ungrounded, or stale.
  - `UNRESOLVED`: Direct contradictions or unresolvable ambiguities exist.
- **Data Models**:
  - `CausalNode`: Normalized file path, 1-indexed line span, node type, role, supporting evidence IDs, uncertainty, disk verification status.
  - `CausalEdge`: Directed relation between source and target, relationship type, evidence IDs, causal explanation, deterministic heuristic confidence score, and boolean `is_directly_verified`.
  - `CausalChain`: Ordered list of nodes and edges connecting root cause to symptom, verification status, and narrative summary.
  - `AlternativeExplanation`: Competing hypothesis with rationale, confidence, and refutation status.
  - `RootCauseReport`: Complete structured diagnosis outcome including primary root cause, contributing factors, symptom locations, causal chains, evidence references, alternative explanations, unresolved questions, confidence, and limitations.

### 2.2 AST Contradiction & Semantic Guard Inspector (`contradiction.py`)
- Parses Python source files using `ast.parse` into concrete syntax trees.
- Identifies innermost enclosing function or method scopes containing candidate locations.
- Analyzes executable guards:
  - `ast.If` statements containing `ast.Raise` (extracting exception type such as `ValueError`, `TypeError`) or `ast.Return`.
  - `ast.Assert` statements.
  - `ast.Try` blocks and `ast.ExceptHandler` clauses enclosing the candidate line range.
- **Comment & String Immunity**: Comments and docstrings are ignored by the AST, preventing false-positive contradictions from comments mentioning "missing validation".
- Refutes hypotheses claiming unhandled exceptions or missing validations when explicit AST structures prove otherwise.

### 2.3 Causal Tracer & Propagation Engine (`tracer.py`)
- **Backward Tracing**: Starting from the failure symptom, follows reverse call edges via `CallGraph.get_call_edges(target_id=...)` and module dependents via `ModuleDependencyGraph.get_dependents()`. Resolves symbols across qualified names and function identifiers.
- **Forward Propagation**: Traces downstream effects from suspected root causes through callees and dependents to model failure cascade.
- **Safety Boundaries**: Enforces maximum traversal depth (`max_depth <= 10`) and node count bounds (`max_nodes <= 200`), guaranteeing cycle safety and recursion termination via visited sets.
- **Certainty Classification & Evidence Distinction**:
  - **Verified Call Invocations**: Explicit call sites verified against current disk content receive `is_directly_verified = True` (`HIGH` confidence, 0.90) and can support `PROVEN` chains.
  - **Structural Module Dependencies**: Module import dependencies (`DEPENDS_ON`) prove structural relationships between packages but do not prove causal defect transmission. They are preserved for context and exploration but marked `is_directly_verified = False` (`MEDIUM` confidence, 0.70), ensuring a chain containing module dependency edges can never receive `PROVEN` status.
  - **Synthetic Caller Fallbacks**: When call sites are missing from recorded call graph edges but caller IDs exist, fallback relationships are preserved for exploration with `is_directly_verified = False` (`MEDIUM` confidence, 0.65) without claiming exact call-site line numbers.
  - **Inferred Data/State Edges**: Data-passing or state invalidation relationships receive `is_directly_verified = False` (`MEDIUM` confidence, 0.65).

### 2.4 Root-Cause Analysis Engine (`engine.py`)
- Ingests verified M1 `DiagnosisResult`, structured `TestFailureReport`, or direct symbol/location inputs.
- Validates evidence freshness with `verify_evidence_freshness()`; flags stale evidence when source file hash has changed on disk.
- Runs backward tracing and AST contradiction inspection.
- Synthesizes candidate linear causal chains from root causes to symptoms.
- Optionally queries local Ollama for structured narrative summaries, failing closed if unavailable without cloud fallback.
- Computes deterministic heuristic confidence score based on chain verification ratio and evidence count.

### 2.5 ToolRegistry & Investigation Tools (`tools.py`)
Exposes 5 structured read-only tools to the FORGE `ToolRegistry`:
1. `analyze_root_cause`: End-to-end evidence-backed root cause analysis.
2. `trace_causal_chain`: Explores direct call paths between source and target symbols.
3. `inspect_contributing_factors`: Inspects callers and module dependencies.
4. `compare_root_cause_hypotheses`: Compares candidate hypotheses against available evidence.
5. `explain_failure_propagation`: Traces downstream failure cascade.

---

## 3. Verification & Test Suite

Dedicated test suite in `tests/unit/test_phase12_m2_causality.py` (22 unit tests):
- **Causal Tracing**:
  - `test_direct_causal_tracing`: Direct caller identification across files.
  - `test_multi_function_causal_chain`: Multi-hop backward caller chain.
  - `test_recursive_call_safety`: Cycle and recursion immunity.
  - `test_cyclic_module_dependency_safety`: Module cycle safety.
  - `test_missing_callers_graceful_handling`: Graceful handling of root functions.
- **Failure Propagation**:
  - `test_forward_propagation_trace`: Forward callee propagation trace.
  - `test_propagation_tool_execution`: Execution of `explain_failure_propagation`.
- **Evidence & Certainty Semantics**:
  - `test_direct_vs_inferred_edge_semantics`: Strict separation of verified vs inferred edges.
  - `test_stale_evidence_detection`: Detection of stale evidence when source content changes.
- **AST Contradiction Analysis**:
  - `test_explicit_guard_refutes_missing_validation_claim`: Refutation of false missing-guard claim.
  - `test_comments_and_docstrings_do_not_fool_ast`: Verification that comments do not trigger false contradictions.
  - `test_exception_handler_refutes_unhandled_error_claim`: Enclosing try/except refutation.
- **Root Cause Status Invariants & Refinements**:
  - `test_single_verified_chain_status_proven`: `PROVEN` status on fully verified call chain.
  - `test_missing_symptom_yields_insufficient_evidence`: `INSUFFICIENT_EVIDENCE` on ungrounded path.
  - `test_stale_evidence_blocks_proven_status`: Stale evidence prevents `PROVEN` status.
  - `test_multiple_plausible_causes_not_forced_to_single`: Multi-chain preservation without forced single cause.
  - `test_module_dependency_fallback_chain_cannot_be_proven`: Structural module import dependencies cannot establish `PROVEN` chains.
  - `test_synthetic_call_edge_cannot_be_proven`: Synthetic caller edges with unrecorded call sites drop chains to `PLAUSIBLE`.
  - `test_mixed_chain_with_inferred_and_verified_edges_cannot_be_proven`: Chains containing any unverified link drop to `PLAUSIBLE`.
  - `test_fully_verified_chain_can_be_proven`: Chains with fully verified direct source evidence achieve `PROVEN` status.
  - `test_inferred_relationships_retained_with_provenance`: Inferred relationships remain in causal reports with accurate provenance.
- **Local Ollama Fail-Closed**:
  - `test_ollama_unavailable_graceful_fallback`: Safe fallback when local Ollama is offline.
  - `test_ollama_malformed_json_fallback`: Safe fallback on malformed model output.
- **ToolRegistry Integration**:
  - `test_all_m2_tools_registered`: All 5 M2 causality tools present in ToolRegistry (24 total tool definitions).
  - `test_execute_trace_causal_chain_tool`: Execution via `ToolRegistry.execute_tool()`.
  - `test_execute_inspect_contributing_factors_tool`: Contributing factors tool execution.
  - `test_execute_compare_hypotheses_tool`: Hypotheses comparison tool execution.

### Test Results
- **M2 Unit Test Suite**: 27/27 passed in 1.03s.
- **Full Project Regression Suite**: 1,799 passed, 7 skipped. Zero failures, zero regressions across all 12 phases.

---

## 4. Benchmark & Performance Results

Measured via `app/forge/benchmarks/benchmark_phase12_m2_causality.py`:
- **Cold Graph/Index Initialization**: 2,299.54 ms (~2.30 s) on full FORGE repository.
- **Warm Backward Causal Tracing (Real Repo)**: mean = **23.34 ms** (min = 21.25 ms, max = 30.53 ms).
- **Single-Chain Reconstruction & Root Cause Report**: mean = **20.19 ms** (min = 19.72 ms, max = 21.99 ms).
- **Multi-Chain Reconstruction (Controlled Fixture)**: mean = **1.57 ms** (`status = multiple_plausible_root_causes`).
- **Cyclic Dependencies Handling**: mean = **1.50 ms** (cycle-safe termination).
- **Evidence Freshness Validation Overhead**: mean = **0.228 ms** (cryptographic SHA-256 slice audit).
- **Latency Separation**: Deterministic static analysis: 1.4 – 26.4 ms; Local Ollama generation: 450 – 1,200 ms when active.

---

## 5. Security & Isolation Safeguards

- **No Arbitrary Execution**: Target repository code is analyzed purely statically via AST and graphs.
- **Zero Cloud Leakage**: Local Ollama endpoints only (`localhost:11434`), zero external requests.
- **Protected File Enforcement**: Centralized Phase 10 boundary policy strictly active.
- **Path Containment**: Directory traversal attempts and absolute path escapes are rejected fail-closed.
- **Secret Redaction**: Secret redaction active on all error messages, tool outputs, and report narratives.
