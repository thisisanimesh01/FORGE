# FORGE Phase 1 Implementation Plan

## Overview
Phase 1 implements Secure Repository Intelligence for FORGE. The system will be implemented incrementally across 7 distinct milestones, accompanied by unit, integration, and security tests.

## Milestone Plan

### Milestone 1: Foundation, Configuration, Schemas & CLI
- **Deliverables**:
  - `pyproject.toml`, `.gitignore`, `.env.example`, `README.md`.
  - `forge.config`: Pydantic settings for paths, budgets, provider configs, timeouts.
  - `forge.schemas`: Pydantic base models for runs, repositories, checks, findings, and evidence.
  - `forge.cli`: Typer CLI with `doctor`, `inspect`, `report`, `runs list`, `benchmark`.
  - Tests for config, schemas, and CLI doctor command.

### Milestone 2: Safe Repository Acquisition & Python Intelligence
- **Deliverables**:
  - `forge.repository.validation`: URL and path containment validation, path traversal guard, URL scheme whitelisting.
  - `forge.repository.acquisition`: Git clone with `--depth 1`, `core.hooksPath=/dev/null`, commit SHA extraction, dedicated temp directories.
  - `forge.repository.detector`: Frameworks, project layout, entrypoints, test directories.
  - `forge.repository.indexer`: AST visitor extracting classes, functions, docstrings, imports, call references.
  - `forge.repository.instructions`: Detection and parsing of `AGENTS.md`, README, tagged as untrusted context.
  - `forge.repository.architecture`: Compact repository map generator with token/size budgets.
  - Tests for acquisition, AST indexing, path containment, and repo mapping.

### Milestone 3: Evidence Model, Finding State Machine & SQLite Event History
- **Deliverables**:
  - `forge.storage.database`: SQLite schema for runs, events, findings, and evidence.
  - `forge.storage.events`: Event-sourcing recorder with sequential IDs and sanitized JSON payloads.
  - `forge.storage.runs`: Run query interface.
  - `forge.analysis.evidence`: Typed evidence records (AST, static check, stack trace, etc.).
  - `forge.analysis.findings`: Finding lifecycle state machine enforcing evidence requirements and audit-only state bounds.
  - Tests for event append, run reconstruction, finding transitions, and evidence links.

### Milestone 4: Sandbox Interface & Execution Policy
- **Deliverables**:
  - `forge.execution.sandbox`: Base `Sandbox` protocol and typed command/result models.
  - `forge.execution.docker_sandbox`: Docker-backed execution with non-root user, read-only root, dropped capabilities, no socket mount, CPU/mem/time limits.
  - `forge.execution.results`: Execution result parsing and exit code tracking.
  - `forge.analysis.baseline`: Safe baseline checks (AST syntax validation, import verification, and isolated pytest checks).
  - Tests verifying policy limits, Docker unavailability fallback, and baseline check execution.

### Milestone 5: LLM Provider Abstraction & Typed Investigation Tools
- **Deliverables**:
  - `forge.llm.provider`: `LLMProvider` protocol (completion & tool calling).
  - `forge.llm.providers.fake`: `FakeLLMProvider` for deterministic offline testing.
  - `forge.llm.providers.generic_http`: HTTP-based provider for OpenAI/Anthropic/Gemini compatible APIs.
  - `forge.agent.tool_registry`: Strict read-only tools:
    - `list_dir(path)`
    - `search(query, path_scope)`
    - `read_file(path, start_line, end_line)`
    - `get_symbol_index(path)`
    - `run_check(check_id)`
  - `forge.agent.orchestrator`: Investigation loop with step counter, context budgeting, untrusted data labeling, and error handling.
  - Tests for tool calling, tool sandboxing, fake provider, and budget truncation.

### Milestone 6: Reporting, Buggy Fixtures & Benchmark Runner
- **Deliverables**:
  - `forge.reporting.report`: Markdown and JSON report generator with secret redaction.
  - `tests/fixtures/buggy_python_app/`: 5+ distinct, isolated bug cases with reproducible tests:
    1. Off-by-one boundary defect in binary search / pagination.
    2. Zero division / empty list handling in statistical aggregator.
    3. Unhandled exception / KeyError in payload parser.
    4. Faulty discount calculation in cart checkout.
    5. State leakage / mutable default argument bug.
  - `tests/fixtures/prompt_injection_repo/`: Repository with adversarial instructions in README/docstrings.
  - `benchmarks/runner.py`: Mini benchmark harness evaluating FORGE detection rate, security resistance, and runtime.
  - Tests for benchmark runner and report generation.

### Milestone 7: Integration, Security Validation & CLI End-to-End
- **Deliverables**:
  - End-to-end integration tests:
    - Full audit without LLM (`--no-llm`).
    - Full audit with `FakeLLMProvider`.
    - Prompt injection defense verification.
    - Path traversal attack attempts.
    - Zero modification check of target repo.
  - Architecture Decision Records (`docs/decisions/`).
  - Comprehensive `README.md` and `LICENSE`.
