# ADR 0003: Provider-Independent LLM Interface and Typed Investigation Loop

## Status
Accepted

## Context
Milestone 5 of FORGE requires connecting the autonomous software engineering pipeline to real LLM providers while maintaining strict security, deterministic reproducibility, bounded resource consumption, and offline operation.

The investigation loop must be able to explore repository architecture, inspect source excerpts, consult evidence, query symbols, and run safe sandboxed checks. However, allowing untrusted LLM output to dictate arbitrary tool execution, mutate repository source, or change security parameters violates the core principles of FORGE Phase 1 (Audit-Only, Fail-Secure).

## Decisions

1. **Provider-Independent Protocol (`LLMProvider`)**:
   - Implemented a structural `LLMProvider` protocol in `app/forge/llm/provider.py` with `generate(request: LLMRequest) -> LLMResponse` and `is_available() -> bool`.
   - Provider-specific payloads and headers are completely isolated to provider implementations (`app/forge/llm/openai.py`).
   - Defined typed error hierarchy (`LLMAuthenticationError`, `LLMRateLimitError`, `LLMContextLengthError`, `LLMUnavailableError`, `LLMProviderError`).

2. **Deterministic Fake Provider (`FakeLLMProvider`)**:
   - Shipped a built-in fake provider supporting scripted responses, custom response generators, call history tracking, and realistic multi-turn behavior.
   - Defaults to `fake` provider in development and testing environments so no paid API keys or network connections are needed.

3. **OpenAI-Compatible Real Provider (`OpenAICompatibleProvider`)**:
   - Uses `httpx.Client` to interact with standard OpenAI chat completion endpoints (`/v1/chat/completions`), supporting vanilla OpenAI, Azure OpenAI, Ollama, and vLLM.
   - Enforces credential hygiene: API keys are read from environment, never logged, and redacted from error messages and exception stack traces.

4. **Strict Typed Tool Registry (`ToolRegistry`)**:
   - The model can only request tools defined in the trusted registry:
     `get_repository_map`, `search_symbols`, `read_source_excerpt`, `list_directory`, `search_text`, `get_evidence`, `list_findings`, `get_run_history`, `run_sandbox_check`.
   - Model outputs proposing tool execution are treated as untrusted suggestions. Execution is mediated and validated entirely by FORGE.
   - Write, patch, push, commit, or shell-executing tools are strictly prohibited.
   - File paths and scopes are validated against repository root via path containment checks (`assert_contained_path`).

5. **Bounded Investigation Loop (`InvestigationLoop`)**:
   - Enforces hard execution budgets: `max_steps` (default 10), timeout per step, and token limits.
   - Detects repeated identical tool calls and triggers loop-break mitigation after duplicate invocations.
   - Appends audit events (`LLM_REQUEST_STARTED`, `TOOL_CALL_REQUESTED`, `TOOL_CALL_COMPLETED`, `RUN_COMPLETED`, `RUN_FAILED`) into SQLite durable store.
   - Model claims do not advance finding lifecycles: finding transitions require concrete verified evidence records.

## Consequences

- Offline workflows and CI run reliably without network access or LLM credentials.
- Real provider integrations remain interchangeable without touching the core investigation loop.
- Host integrity and security invariants are preserved against prompt injections and malicious tool call proposals.
