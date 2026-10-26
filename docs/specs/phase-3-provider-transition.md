# Phase 3→4 Transition: Multi-Provider LLM Configuration and Credential Reuse

**Status:** Implemented and verified  
**Phase Status:** Phase 3 Completed; Phase 4 Not Started  
**Branch:** `main`  

---

## 1. Overview

This transition specification defines the multi-provider LLM abstraction enabling FORGE to reuse local Ollama installations alongside free-tier cloud providers (Groq, Gemini, DeepSeek) and OpenAI, without requiring vendor-specific SDK dependencies or modifying existing projects.

All four cloud providers and Ollama expose OpenAI-compatible HTTP endpoints (`/v1/chat/completions`), allowing FORGE to use its existing `OpenAICompatibleProvider` via standard `httpx` with zero new external libraries.

---

## 2. Provider Architecture and Resolution

FORGE resolves providers via the centralized factory function `forge.llm.get_provider()`:

```
                          ┌──────────────────────────┐
                          │  forge.llm.get_provider  │
                          └─────────────┬────────────┘
                                        │
           ┌──────────────┬─────────────┼──────────────┬──────────────┬──────────────┐
           ▼              ▼             ▼              ▼              ▼              ▼
       ┌────────┐    ┌─────────┐   ┌─────────┐    ┌─────────┐    ┌──────────┐   ┌──────────┐
       │  fake  │    │ ollama  │   │  groq   │    │ gemini  │    │ deepseek │   │  openai  │
       └────────┘    └─────────┘   └─────────┘    └─────────┘    └──────────┘   └──────────┘
           │              │             │              │              │              │
        Offline        Local         Cloud          Cloud          Cloud          Cloud
       Mock Loop      HTTP /v1     api.groq.com   Google Shim    api.deepseek   api.openai
```

### Provider Details & Resolution Matrix

| Provider Key | Protocol / Engine | Default Model | Base URL Endpoint | Auth Variable | Local / Free |
|---|---|---|---|---|---|
| `fake` | `FakeLLMProvider` | `forge-fake-model` | In-memory mock | None | Offline (always available) |
| `ollama` | `OpenAICompatibleProvider` | `llama3` | `http://localhost:11434/v1` | None | Local machine |
| `groq` | `OpenAICompatibleProvider` | `llama-3.3-70b-versatile` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | Cloud (free tier) |
| `gemini` | `OpenAICompatibleProvider` | `gemini-2.0-flash` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | Cloud (free tier) |
| `deepseek` | `OpenAICompatibleProvider` | `deepseek-chat` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | Cloud (free tier) |
| `openai` | `OpenAICompatibleProvider` | `gpt-4o` | `https://api.openai.com/v1` | `FORGE_LLM_API_KEY` | Cloud (commercial) |

---

## 3. Environment Configuration & Friday Credential Reuse

FORGE uses a local, gitignored `.env` file for runtime configuration. Default values favor local, zero-cost execution with Ollama.

### Supported Variable Mapping

- `FORGE_LLM_PROVIDER`: Provider selector (`ollama` by default; accepts `fake`, `ollama`, `groq`, `gemini`, `deepseek`, `openai`).
- `FORGE_LLM_MODEL`: Model identifier override. When empty, provider defaults apply.
- `OLLAMA_BASE_URL`: Base URL for local Ollama server (matches Friday convention; default: `http://localhost:11434`).
- `GROQ_API_KEY`: API key for Groq Cloud (matches Friday convention).
- `GEMINI_API_KEY`: API key for Google Gemini (matches Friday convention).
- `DEEPSEEK_API_KEY`: API key for DeepSeek (matches Friday convention).
- `FORGE_LLM_API_KEY`: API key for OpenAI provider.
- `FORGE_ALLOW_CLOUD_FALLBACK`: Hard security guard; strictly defaults to `false`.

### Safe Credential Reuse Instructions

To reuse API keys from existing local configurations (such as the Friday project) without exposing secrets:
1. Open Friday's local `.env` file manually.
2. Copy only the specific key values desired (`GROQ_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`).
3. Paste each key into the corresponding placeholder in FORGE's `.env`.
4. Never paste API keys into chat, command logs, or commit histories.
5. The local `.env` file is explicitly ignored in `.gitignore` and is never committed to Git.

---

## 4. Free-Usage and Safety Safeguards

1. **Default Local Execution**: FORGE defaults to `ollama` locally (`FORGE_LLM_PROVIDER=ollama`), preventing accidental cloud transmission.
2. **Fallback Prevention**: Silent cloud fallback is prohibited. If a configured provider lacks credentials or fails, `LLMUnavailableError` is raised immediately. FORGE will never automatically reroute requests to paid or cloud services.
3. **Secret Redaction**: All API keys, authorization tokens, and credentials are automatically scrubbed from logs, reports, and CLI diagnostic output via `redact_secrets()` and `ForgeSettings.get_sanitized_dict()`.
4. **Offline Test Suite Invariant**: Unit tests and CI pipelines run 100% offline using `FakeLLMProvider` or mocked HTTP transports. Zero network requests, Docker dependencies, or paid API credits are required.

---

## 5. Verification Suite

The transition is verified by `tests/unit/test_llm_providers.py` (55 tests) and the full regression suite (523 tests total):
- Provider resolution across all 6 providers.
- Model ID precedence (CLI flag > env var > provider default).
- Mandatory API key validation on cloud providers with immediate, typed error raising.
- Cloud fallback refusal tests.
- Sanitized diagnostic inspection (`forge doctor` and `describe_provider()`).
- Secret pattern scrubbing across all token formats (`gsk_...`, `sk-...`, `Bearer ...`).

---

## 6. Phase Status

- **Phase 1**: Secure Repository Intelligence — `[COMPLETED]`
- **Phase 2**: Diff-Aware Intelligent Review — `[COMPLETED]`
- **Phase 3**: Defect Reproduction & Verification — `[COMPLETED]`
- **Phase 3→4 Transition**: Provider Configuration & Credential Reuse — `[COMPLETED]`
- **Phase 4**: Targeted Autonomous Repair — `[NOT STARTED]` (Pending human review)
