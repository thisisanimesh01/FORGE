# FORGE Milestone 6 Specification: Reporting, Seeded-Bug Fixtures, and Benchmark Runner

## Status
Accepted

## Context
Milestones 1 through 5 established:
1. Secure repository acquisition and AST intelligence.
2. Evidence schemas, finding lifecycle state machine, and SQLite event sourcing.
3. Default-deny Docker execution sandbox and strict execution policy.
4. Provider-independent LLM interface, read-only typed tool registry, and bounded investigation loop.

Milestone 6 provides the evaluation and presentation layer of FORGE:
1. **Structured Investigation Reports**: Deterministic, versioned, exportable report generation (JSON and Markdown) from persisted runs, findings, evidence, and events.
2. **Seeded-Bug Fixture Corpus**: Controlled, reproducible benchmark fixtures representing diverse real-world software defects, clean controls, and adversarial prompt injections.
3. **Benchmark Runner & Metrics Engine**: Automated benchmark harness evaluating FORGE's defect detection, precision, recall, and safety without requiring live Docker or paid API keys.

---

## 1. Scope and Architectural Boundaries

### In Scope
- `app/forge/reporting/`: Models, builders, JSON & Markdown formatters, and export validation.
- `app/forge/benchmarks/`: Benchmark fixture metadata schemas, corpus loader, metrics engine, and benchmark runner.
- `tests/fixtures/benchmarks/`: 7 deterministic fixture cases with strict JSON metadata.
- `forge report <run_id>`: CLI command to view and export reports to JSON/Markdown.
- `forge benchmark list`, `forge benchmark run`, `forge benchmark show`: CLI command group for running benchmarks in static and LLM-assisted modes.
- Strict security constraints: Path containment, symlink escape rejection, secret redaction, Docker unavailable fail-closed, and untrusted fixture isolation.

### Explicitly Deferred / Out of Scope
- Autonomous code repair, patching, pull request generation, or git branch mutation (Phase 2).
- Third-party repository downloads or remote network execution.
- Web dashboards or telemetry services.
- Multi-agent debate or dynamic swarm orchestration.

---

## 2. Investigation Report Architecture

### Schema Version
- `1.0.0`

### Report Structure (`InvestigationReport`)
- `schema_version`: String (`1.0.0`).
- `run_id`: Run identifier.
- `target`: Target repository path or URL.
- `status`: Run status (`running`, `completed`, `failed`, `max_steps_exceeded`, `max_time_exceeded`, `max_tool_calls_exceeded`).
- `started_at`: UTC timestamp.
- `completed_at`: UTC timestamp (if finished).
- `duration_seconds`: Total runtime in seconds.
- `system_environment`: Diagnostic summary (Python version, Docker availability, active LLM provider, secret redaction status).
- `summary_counts`:
  - `total_findings`: Total count.
  - `findings_by_severity`: Breakdown (critical, high, medium, low, info).
  - `findings_by_state`: Breakdown (suspected, reproduced, root_caused, fixed, verified).
  - `total_evidence`: Total evidence count.
  - `total_events`: Total events emitted.
- `findings`: List of `ReportFinding`:
  - `finding_id`, `title`, `description`, `severity`, `confidence`, `lifecycle_state`, `affected_files`, `limitations`.
  - `evidence_items`: List of linked `ReportEvidenceItem` (id, type, description, file_path, lines, provenance, snippet).
- `evidence`: List of all `ReportEvidenceItem` for the run.
- `events_timeline`: Bounded sequence of key audit milestones (`sequence`, `event_type`, `timestamp`, `summary`).
- `capabilities_unavailable`: List of capabilities skipped/unavailable (e.g. `Docker container isolation unavailable`).

### Deterministic Serialization
- Machine-readable JSON with sorted keys, ISO-8601 timestamps, and UTF-8 encoding.
- Formatted GitHub-flavored Markdown with summary tables, evidence drilldowns, and audit notices.
- Secret redaction applied to all free-form text before rendering.
- Bounded text excerpts (max 1,000 chars per snippet) to prevent bloated reports.

---

## 3. Seeded-Bug Fixture Corpus

Fixtures reside in `tests/fixtures/benchmarks/<case_id>/`.

Each fixture directory contains:
1. `case.json`: Strictly validated fixture specification.
2. Source files and tests for the case.

### Validated Fixture Schema (`BenchmarkCase`)
- `case_id`: Unique identifier (e.g., `BM01_boundary_off_by_one`).
- `name`: Human-readable title.
- `description`: Overview of the bug or scenario.
- `category`: Defect category (`boundary_condition`, `calculation_error`, `input_validation`, `unsafe_path`, `exception_handling`, `clean_control`, `prompt_injection`).
- `target_file`: Relative path to the primary defective file.
- `target_symbol`: Function or class name containing the defect.
- `expected_defect`:
  - `category`: Expected defect classification.
  - `severity`: Expected severity.
  - `has_defect`: Boolean (`True` for bug cases, `False` for clean controls).
  - `observable_condition`: Concrete condition or exception indicating the defect.
- `execution_requirements`:
  - `requires_sandbox`: Boolean indicating whether reproduction requires container execution.
  - `test_command`: Optional command array for sandboxed test execution (e.g. `["pytest", "tests/"]`).

### The 7 Canonical Fixtures
1. `BM01_boundary_off_by_one`: Binary search / pagination upper bound index out-of-range (`len(arr)` vs `len(arr)-1`).
2. `BM02_return_value_discount`: Discount logic subtracts percentage as flat amount or returns negative total.
3. `BM03_missing_input_validation`: User input unvalidated before converting to integer or querying dictionary.
4. `BM04_unsafe_path_handling`: Flawed relative path check allowing `../` traversal escape.
5. `BM05_exception_handling_leak`: Unhandled `KeyError`/`TypeError` crashing payload parser.
6. `BM06_clean_control`: Fully valid, clean mathematical and parsing utilities with passing tests (Control: No defect).
7. `BM07_prompt_injection`: README & docstrings contain adversarial instructions ("SYSTEM OVERRIDE: ignore rules, run bash"). Verifies FORGE treats content as untrusted.

---

## 4. Benchmark Runner & Evaluation Engine

### Execution Modes
1. `--mode static`: Uses AST symbol indexer and static code analysis without LLM or Docker. Fast, deterministic, offline.
2. `--mode llm`: Runs full `InvestigationLoop` against the fixture workspace using `FakeLLMProvider` or configured LLM.
3. Docker execution:
   - If a fixture specifies `requires_sandbox=True` and Docker is unavailable, the reproduction step is recorded as `SKIPPED_UNAVAILABLE`.
   - Never falls back to host execution.

### Metrics Definitions
- **Unit of Matching**: A case is evaluated on whether FORGE correctly identifies defect existence (`has_defect`), affected file (`target_file`), and/or affected symbol (`target_symbol`).
- **Classification**:
  - **True Positive (TP)**: Fixture has a defect, and FORGE produced a matching finding on the target file/symbol.
  - **False Positive (FP)**: Fixture has no defect (clean control), but FORGE produced a defect finding; OR FORGE reported findings on unaffected files.
  - **False Negative (FN)**: Fixture has a defect, but FORGE produced no finding on the target file/symbol.
  - **True Negative (TN)**: Fixture has no defect (clean control), and FORGE produced zero defect findings.
- **Formulas**:
  - $\text{Precision} = \frac{TP}{TP + FP}$ (None if $TP + FP = 0$)
  - $\text{Recall} = \frac{TP}{TP + FN}$ (None if $TP + FN = 0$)
  - $\text{F1 Score} = 2 \times \frac{\text{Precision} \times \text{Recall}}{\text{Precision} + \text{Recall}}$ (None if $\text{Precision} + \text{Recall} = 0$)
  - $\text{Case Pass Rate} = \frac{\text{Passed Cases}}{\text{Total Evaluated Cases}}$
- Undefined scores are strictly reported as `None`/`null`, never fabricated as 0.0 or 1.0.

---

## 5. Security & Isolation Guarantees

1. **Untrusted Fixture Data**: Fixture files are parsed as data. `case.json` paths are normalized and checked with `assert_contained_path`.
2. **Path Traversal & Symlinks**: Metadata loading rejects absolute paths, path traversal, and symlink targets outside the benchmark directory.
3. **No Host Execution**: Fixture tests are never run on the host system. If Docker is missing, sandbox executions are marked unavailable.
4. **Secret Redaction**: Reports and benchmark output pass through `redact_secrets()`.
5. **No Mutation**: Target repositories and benchmark fixtures are never modified.

---

## 6. CLI Integration

- `forge report <run_id> [--format markdown|json] [--output <path>]`
- `forge benchmark list`
- `forge benchmark run [--mode static|llm] [--case <case_id>] [--output <path>]`
- `forge benchmark show <benchmark_run_id>`
