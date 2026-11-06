# Phase 11, Milestone 6: LLM Tooling & Context Integration

## 1. Executive Summary & Objective

Phase 11 Milestone 6 completes Phase 11 by seamlessly integrating all static intelligence capabilities built across Milestones 1–5 into FORGE's LLM reasoning workflows, tool execution loops, context construction pipelines, and finding verification systems.

Prior to Milestone 6, FORGE possessed deep static analysis infrastructure (M2 symbol graphs and call graphs, M3 module dependency DAGs and cycle detectors, M4 change-impact engines, and M5 incremental caching), but lacked standardized typed interfaces for models to actively query this intelligence during audits and investigations.

Milestone 6 bridges this capability gap while strictly maintaining FORGE's foundational invariants:
- **Audit-Only & Immutable**: Zero modification of target repository files or Git metadata.
- **Fail-Secure Static Analysis**: No dynamic execution or arbitrary code interpretation of untrusted repository code.
- **Strict Typed Schemas**: All tool inputs and outputs are governed by strict Pydantic models with `extra="forbid"`.
- **Zero Fabrication**: Model findings and claims must be grounded in verified disk evidence and concrete source lines.
- **Zero Cloud Leakage**: Inference is strictly restricted to local Ollama endpoints (`localhost` / `127.0.0.1`), failing closed without fallback to cloud services.
- **Single-Process Cache Operations**: Preserves single-process operational semantics ensuring safe cache lifecycle.

---

## 2. Architectural Design

The Milestone 6 architecture consists of four primary components located within `forge.llm`:

```
app/forge/llm/
├── repo_tool_schemas.py    # Strict Pydantic input/output schemas for all 6 intelligence tools
├── repo_tools.py           # RepositoryIntelligenceTools: typed dispatch and cache-backed execution
├── context.py              # RepositoryContextBuilder: task-focused budgeted prompt context assembly
└── evidence_grounding.py   # EvidenceGroundingEngine: zero-fabrication disk verification and finding grounding
```

### 2.1 Repository Intelligence Tool Schemas (`forge.llm.repo_tool_schemas`)

Strict typed contracts for 6 specialized LLM tools:

1. **`get_repository_overview`**:
   - Inputs: `include_cache_stats: bool = True`, `check_freshness: bool = False`.
   - Outputs: Total files scanned, symbols, classes, functions, methods, call nodes/edges, module nodes/edges, circular cycle count, detected frameworks, and cache freshness telemetry.
2. **`search_symbols` & `get_symbol_details`**:
   - Inputs: `query: str`, optional `symbol_type: SymbolTypeFilter`, `max_results: int = 25`.
   - Ambiguity Detection: If a symbol name appears in multiple files (e.g. `format_data` in both `common.py` and `utils.py`), returns `is_ambiguous = True` and enumerates candidates with their enclosing modules.
   - Details: `symbol_id: str` retrieval providing qualified path, docstrings, argument signatures, decorators, line bounds, and direct relationships.
3. **`explore_call_graph`**:
   - Inputs: `symbol_id: str`, `direction: CallDirection` (`callers`, `callees`, `both`), `max_depth: int = 3`, `max_nodes: int = 50`.
   - Outputs: Bounded invocation paths, resolution status (`RESOLVED_INTERNAL`, `RESOLVED_EXTERNAL`, `UNRESOLVED`, `DYNAMIC`), line provenance, and cycle detection.
4. **`explore_module_dependencies`**:
   - Inputs: `module_name: str`, `include_cycles: bool = True`, `transitive: bool = False`.
   - Outputs: Direct dependencies, direct dependents, transitive dependents, and detected elementary circular cycles.
5. **`analyze_change_impact`**:
   - Inputs: `modified_files: List[str]`, optional `diff: str`, `max_depth: int = 5`, `max_results: int = 50`.
   - Outputs: Direct and transitive impacted entities, classification (`DIRECT`, `TRANSITIVE`, `POTENTIAL`, `UNRESOLVED`), hop distances, deterministic heuristic scores (0.0 to 1.0 ranking metrics), and propagation paths.
6. **`get_source_context`**:
   - Inputs: `file_path: str`, `start_line: int`, `end_line: int` (window bounded to $\le 250$ lines), `include_symbols: bool = True`.
   - Outputs: Verified line-numbered excerpt, SHA-256 integrity hash, secret-redacted source text, and symbol definitions located within the window.

### 2.2 Tool Dispatch & Integration (`forge.llm.repo_tools.RepositoryIntelligenceTools`)

- Automatically exposes OpenAI-compatible tool specifications via `.get_tool_definitions()`.
- Wires directly into `forge.investigation.tools.ToolRegistry` and `InvestigationLoop`.
- Employs lazy cached index instantiation via `IncrementalRepositoryIndex`, avoiding redundant parsing.
- Enforces strict path containment (`assert_contained_path`) and protected-file rejection (`is_protected_path`).

### 2.3 Repository Context Builder (`forge.llm.context.RepositoryContextBuilder`)

Constructs structured, prompt-ready markdown context for LLM agents within strict character budgets (default 16,000 characters):
1. **Task-Relevance Ranking**: Matches symbols and files relevant to the user query or task.
2. **Graph Expansion**: Augments selected symbols with caller/callee relationships, module import DAGs, and circular dependency warnings.
3. **Source Excerpt Consolidation**: Merges overlapping line windows across inspected files into deduplicated, non-overlapping slices.
4. **Line Numbering & Secret Redaction**: Applies line numbers and masks credentials (`[REDACTED_SECRET]`).
5. **Deterministic Budget Capping**: Enforces character budget; if exceeded, safely truncates while appending explicit warning markers (`[TRUNCATED: Context budget exceeded...]`).

### 2.4 Evidence-Grounded Reasoning Engine (`forge.llm.evidence_grounding.EvidenceGroundingEngine`)

Prevents LLM hallucinations and fabricated findings:
- Validates candidate findings against actual file existence and line boundaries on disk before creation.
- Rejects path traversals, out-of-bounds line numbers, and protected files fail-closed with `GroundingValidationError`.
- Automatically generates cryptographically grounded `SOURCE_EXCERPT` evidence records with SHA-256 hashes and attaches them to findings in SQLite `ForgeStore`.

### 2.5 Local Ollama Isolation

- FORGE enforces strictly local inference (`localhost` / `127.0.0.1` endpoints).
- When Ollama is unreachable, execution fails closed with `LLMUnavailableError` without attempting network calls to third-party cloud APIs.

---

## 3. Empirical Verification & Performance

### 3.1 Tool and Context Construction Benchmarks

Measurements conducted across test fixture repositories:

| Component / Tool | Operation | Latency |
| :--- | :--- | :--- |
| `get_repository_overview` | Read-only scan & cache freshness check | **2.15 ms** |
| `search_symbols` | Symbol search & ambiguity detection | **<0.01 ms** |
| `get_symbol_details` | Symbol record lookup & relationship resolution | **<0.01 ms** |
| `explore_call_graph` | Caller/callee bounded traversal | **<0.01 ms** |
| `explore_module_dependencies` | Dependency DAG & cycle extraction | **~0.01 ms** |
| `analyze_change_impact` | Reverse caller/dependent impact propagation | **0.44 ms** |
| `get_source_context` | Bounded slice extraction, redaction & hashing | **0.15 ms** |
| `RepositoryContextBuilder` | Full budgeted context assembly (16k chars) | **1.04 ms** |

### 3.2 Unit Test Verification Suite

A dedicated test suite in `tests/unit/test_phase11_m6_llm_tooling.py` validates all aspects:
- Tool contract enforcement & schema validation (`extra="forbid"`).
- Execution correctness across all 6 intelligence tools.
- Ambiguous symbol detection and disambiguation.
- Bounded call graph and module dependency graph traversal.
- Context builder budget capping, deduplication, and cycle reporting.
- M5 cache integration and recovery in context assembly.
- Evidence grounding disk validation and zero-fabrication enforcement.
- Local Ollama restriction and fail-closed error handling.
- Turn-budgeted investigation loop execution.
- Path containment and secret redaction.

**Results**:
- `tests/unit/test_phase11_m6_llm_tooling.py`: **35 passed in 1.27s (100% pass rate)**.
- **Phase 11 Total Tests**: **145 passed (100% pass rate)** across M2, M3, M4, M5, and M6.
- **Full FORGE Regression Suite**: **1,748 passed, 7 skipped in 174s (100% pass rate)**. Zero regressions.

---

## 4. Phase 11 Completion Status

Phase 11 (Intelligent Repository Understanding) is now **100% COMPLETE**:
- [x] **Milestone 1**: Architecture Audit & Capability Mapping (`docs/milestones/phase-11-m1-architecture-audit.md`)
- [x] **Milestone 2**: Symbol Graph & Cross-File Call-Graph Engine (`docs/milestones/phase-11-m2-symbol-graph-and-call-graph.md`)
- [x] **Milestone 3**: Module Dependency Graph & Cycle Detection (`docs/milestones/phase-11-m3-module-dependency-graph.md`)
- [x] **Milestone 4**: Change-Impact Analysis Engine (`docs/milestones/phase-11-m4-change-impact-analysis.md`)
- [x] **Milestone 5**: Incremental Indexing & Caching (`docs/milestones/phase-11-m5-incremental-indexing-and-caching.md`)
- [x] **Milestone 6**: LLM Tooling & Context Integration (`docs/milestones/phase-11-m6-llm-tooling-context-integration.md`)
