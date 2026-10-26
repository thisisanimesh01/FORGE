# Pre-Phase 4 Milestone: Local Ollama + Multi-Provider LLM Integration

**Status:** Completed & Verified  
**Milestone:** Pre-Phase 4 LLM Infrastructure  
**Branch:** `main`  
**Phase Status:** Phase 3 Completed; Pre-Phase 4 Completed; Phase 4 Not Started  

---

## 1. Overview & Objectives

Before beginning Phase 4 (Targeted Autonomous Repair), this milestone establishes and verifies FORGE's multi-provider LLM infrastructure. It enables local, zero-cost execution via Ollama as the default provider, alongside cloud provider support for Groq, Google Gemini, and DeepSeek (with OpenAI API provider support removed per deprecation specification).

Key architectural requirements enforced:
- **Zero-SDK overhead**: All cloud providers and Ollama expose standard HTTP chat completion endpoints (`/v1/chat/completions`), allowing FORGE to use the generic `OpenAICompatibleProvider` protocol adapter via standard `httpx`.
- **Fail-secure cloud fallback**: Automatic or silent provider switching is strictly forbidden (`allow_cloud_fallback=False`). Missing credentials or endpoint errors raise immediate, typed exceptions.
- **Strict secret redaction**: All API keys, tokens, and authorization headers are scrubbed before logging, reports, or CLI display.
- **Untrusted model boundary**: Model outputs are untrusted data and cannot alter execution permissions, sandbox policies, or network configurations.

---

## 2. System Hardware & Ollama Analysis

A system assessment of this Mac was conducted:
- **Hardware Profile**: Apple M1 (8 cores), 8 GB Unified Memory, 13 GiB free disk space.
- **Local Ollama State**:
  - Binary status: Not installed on host (`which ollama` returned not found; not installed in `/opt/homebrew` or `/Applications`).
  - Service status: Port 11434 connection refused (`http://localhost:11434`).
  - Automated installation blocker: Homebrew network bottle retrieval timed out.
- **Recommended Local Coding Model**:
  - Model: `qwen2.5-coder:1.5b` (or `llama3.2:3b` / `qwen2.5-coder:3b`).
  - Footprint: ~1.0 GB disk space, ~1.5 GB memory footprint.
  - Hardware suitability: Fits comfortably within 8 GB Unified Memory without risking memory pressure, swapping, or disk exhaustion on the 13 GiB available disk.
  - Note: Models larger than 7B (such as `llama3:8b` at ~4.7 GB) are discouraged on this 8 GB machine as they would consume over 60% of total system RAM.

---

## 3. Provider Architecture & Configuration Matrix

Provider resolution is centralized in `forge.llm.get_provider()`:

| Provider Key | Protocol / Adapter | Default Model | Base URL Endpoint | Auth Variable(s) | Execution Mode |
|---|---|---|---|---|---|
| `fake` | `FakeLLMProvider` | `forge-fake-model` | In-memory deterministic mock | None | Offline (CI / test suite) |
| `ollama` | `OpenAICompatibleProvider` | `llama3` | `http://localhost:11434/v1` | None required | Local machine (Default) |
| `ollama_cloud` | `OpenAICompatibleProvider` | `gemma4:cloud` | `https://ollama.com/v1` | `OLLAMA_API_KEY` | Hosted Remote Cloud |
| `groq` | `OpenAICompatibleProvider` | `llama-3.3-70b-versatile` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY`, `GROQ_API` | Cloud (free tier available) |
| `gemini` | `OpenAICompatibleProvider` | `gemini-2.0-flash` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY`, `GEMINI_API` | Cloud (free tier available) |
| `deepseek` | `OpenAICompatibleProvider` | `deepseek-chat` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY`, `DEEPSEEK_API` | Cloud (commercial available) |

### Key Precedence & Aliases
`ForgeSettings` supports both standard `*_API_KEY` and short `*_API` naming via `pydantic.AliasChoices`:
- Groq: `GROQ_API_KEY` or `GROQ_API` or `FORGE_GROQ_API_KEY`
- Gemini: `GEMINI_API_KEY` or `GEMINI_API` or `FORGE_GEMINI_API_KEY`
- DeepSeek: `DEEPSEEK_API_KEY` or `DEEPSEEK_API` or `FORGE_DEEPSEEK_API_KEY`
- Ollama: `OLLAMA_BASE_URL` or `FORGE_OLLAMA_BASE_URL` (default: `http://localhost:11434`)

---

## 4. Bounded Retries & Error Classification

`OpenAICompatibleProvider` implements bounded transient retries:
- **Retryable Errors**: HTTP 5xx server errors and network `TimeoutException` (up to `max_retries=1`).
- **Non-Retryable Errors (Immediate Fail)**:
  - HTTP 401 / 403: `LLMAuthenticationError`
  - HTTP 429: `LLMRateLimitError`
  - HTTP 400 with context length: `LLMContextLengthError`
  - HTTP 400 generic: `LLMProviderError`
- **Fallback Guard**: When a provider request fails, FORGE never falls back to alternative cloud providers. `FORGE_ALLOW_CLOUD_FALLBACK=false` is enforced.

---

## 5. Verification & Test Evidence

### Offline Tests
60 dedicated unit tests in `tests/unit/test_llm_providers.py` passed in 0.36s:
- Multi-provider factory resolution across all 6 providers.
- Model default inheritance and explicit CLI/env override hierarchy.
- Missing credentials verification for all cloud providers (raising `LLMUnavailableError`).
- Key alias acceptance (`GROQ_API`, `GEMINI_API`, `DEEPSEEK_API`).
- Bounded retries: recovery from transient 500 error vs. exhaustion after max retries.
- Sanitized diagnostic inspection (`describe_provider()` and `forge doctor`).
- Secret pattern scrubbing across token types (`gsk_...`, `sk-...`, `Bearer ...`).
- Offline deterministic preservation (`FakeLLMProvider` is always available).

### Regression Suite
- Full test suite: 528 passing tests (`528 passed in 7.34s`).
- Review quality benchmarks: 10/10 fixtures passing with 100% precision and 100% recall.
- `forge doctor`: Passes with active secret redaction and Ollama local diagnostics.

### Live Cloud Smoke Tests
In accordance with user safety rules, live cloud API smoke tests were intentionally **skipped** during automated verification to prevent unauthorized billing, token consumption, or unintended transmission of repository data.

---

## 6. Phase Status & Next Steps

- **Phase 1**: Secure Repository Intelligence — `[COMPLETED]`
- **Phase 2**: Diff-Aware Intelligent Review — `[COMPLETED]`
- **Phase 3**: Defect Reproduction & Verification — `[COMPLETED]`
- **Pre-Phase 4**: Local Ollama & Multi-Provider LLM Integration — `[COMPLETED]`
- **Phase 4**: Targeted Autonomous Repair — `[NOT STARTED]` (Awaiting user review)
