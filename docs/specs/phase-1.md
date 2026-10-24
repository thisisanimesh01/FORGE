# Phase 1 Specification: Secure Repository Intelligence

## 1. Goal
Provide an audit-only CLI application (`forge`) that can safely acquire a target repository, perform static intelligence and isolated baseline checks, run an LLM investigation loop using typed read-only tools, track evidence and findings in an SQLite event store, and produce structured engineering reports.

## 2. Functional Requirements
1. **Acquisition & Path Containment**:
   - Accept HTTPS URLs or explicitly authorized local directories.
   - Reject arbitrary schemes (`file://`, `ssh://`), embedded credentials, and path traversals (`../`).
   - Clone into dedicated isolated workspaces outside the target repository with git hooks disabled (`core.hooksPath=/dev/null`).
   - Canonical path resolution to prevent symlink traversal outside the repo root.
2. **Repository Intelligence (Python-first)**:
   - Identify Python projects, configuration files (`pyproject.toml`, `requirements.txt`, etc.).
   - AST-based symbol extraction (functions, classes, methods, imports).
   - Compact repository map generation respecting file size/token budgets.
   - Identification of `AGENTS.md` and repository instructions (tagged untrusted).
3. **Sandbox & Baseline Checks**:
   - Provide `Sandbox` abstraction with Docker backend and NoOp/DryRun backend for host safety.
   - Docker container execution: non-root, read-only root, no host credential mounts, timeout and resource limits.
   - Predefined checks: static lint/syntax checks, and runtime checks (pytest) when container is available.
   - Graceful skip with explicit reasons if Docker is unavailable.
4. **LLM Orchestration & Read-Only Tools**:
   - Provider abstraction with support for configured real providers (e.g., Anthropic / OpenAI / Gemini HTTP-compatible or fake test providers).
   - Deterministic `FakeLLMProvider` for offline testing.
   - Tools: `list_dir`, `search`, `read_file`, `get_symbol_index`, `run_check`.
   - Budget constraints (max turns, token/character limits) and output truncation.
5. **Evidence & Findings State Machine**:
   - Typed `EvidenceRecord` (source location, command output, stack traces).
   - Finding model with lifecycle: `suspected -> reproduced -> root_caused -> fixed -> verified`. Phase 1 enforces audit-only (`suspected`, cannot fix or verify fixes).
6. **Persistence & Event Sourcing**:
   - SQLite append-only event log (`run_created`, `repository_acquired`, `tool_call_requested`, etc.).
   - Replay/inspection of run history via `forge runs list` and `forge report <run-id>`.
7. **Reports & Benchmarks**:
   - Markdown and JSON output with secret redaction.
   - Mini benchmark suite with >= 5 buggy Python fixture cases + 1 prompt injection fixture.

## 3. Acceptance Criteria
- Unit test suite covers validation, AST parsing, budgeting, tool schemas, finding state machine, and event storage.
- Integration test suite verifies end-to-end audit run without LLM key (`--no-llm`), with fake LLM, and prompt injection defense.
- Benchmark runner tests all fixtures and outputs summary without fabrication.
- Target repository files and git state remain 100% unaltered.
