# Specification: Removal of OpenAI API Provider Support

**Status:** Completed & Verified  
**Milestone:** LLM Provider Refactor (OpenAI Deprecation)  
**Branch:** `main`  
**Phase Status:** Phase 3 Completed; Pre-Phase 4 Completed; Phase 4 Not Started  

---

## 1. Objective & Scope

This specification records the complete removal of the **OpenAI API as a supported FORGE LLM provider**.

FORGE must not require an OpenAI API key, make calls to OpenAI cloud endpoints, or silently route user requests to OpenAI. At the same time, the generic OpenAI-compatible HTTP protocol adapter (`OpenAICompatibleProvider`) is strictly preserved because remaining providers (Ollama Cloud, Google Gemini, Groq, DeepSeek, and local Ollama) communicate using this standard HTTP Chat Completions format.

---

## 2. Rationale & Architecture Decisions

### 2.1 Complete Provider Decoupling
The `openai` provider entry is removed from:
- `_DEFAULT_MODELS` and `_DEFAULT_BASE_URLS` in `app/forge/llm/factory.py`.
- `get_provider()` factory routing logic. Any attempt to request provider `openai` raises `LLMUnavailableError("Unknown LLM provider 'openai'...")`.
- CLI `--provider` options and help strings.
- Configuration schemas and examples (`.env.example`, `README.md`).

### 2.2 Preservation of the Generic HTTP Adapter
The adapter class `OpenAICompatibleProvider` (`app/forge/llm/openai.py`) remains intact to serve:
- Local Ollama (`http://localhost:11434/v1`)
- Ollama Cloud (`https://ollama.com/v1`)
- Groq Cloud (`https://api.groq.com/openai/v1`)
- Google Gemini via OpenAI shim (`https://generativelanguage.googleapis.com/v1beta/openai`)
- DeepSeek API (`https://api.deepseek.com/v1`)

However, all OpenAI-specific bindings inside `OpenAICompatibleProvider` were eliminated:
1. **No default endpoint**: `raw_base` no longer defaults to `https://api.openai.com/v1`. Instantiating `OpenAICompatibleProvider` without an explicit `api_base` leaves `self.api_base = ""`, and invoking `generate()` without an endpoint raises an immediate `LLMUnavailableError`.
2. **Endpoint validation**: `is_available()` returns `True` only if explicit credentials are set or if pointing to a loopback/local address (`localhost` / `127.0.0.1`).
3. **No error message leaks**: HTTP 401/403 exceptions no longer instruct users to "Check your FORGE_LLM_API_KEY", but reference provider credentials generically.

### 2.3 Dependency & Migration Impact of Configuration Keys

#### `FORGE_LLM_API_KEY` (Removed)
- **Dependency Analysis**: Was used only by the `openai` provider and as an undocumented implicit fallback across Groq, Gemini, and DeepSeek.
- **Migration Impact**: Removing `FORGE_LLM_API_KEY` enforces FORGE's invariant that cloud providers require explicit selection and their own dedicated credentials (`GROQ_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OLLAMA_API_KEY`).
- **Action**: Removed from `ForgeSettings`, `.env.example`, and `get_sanitized_dict()`. Cross-provider fallback was eliminated from all builder helpers.

#### `FORGE_LLM_API_BASE` (Preserved as Generic Override)
- **Dependency Analysis**: `FORGE_LLM_API_BASE` is actively utilized to allow enterprise users to route inference through internal forwarding proxies, local vLLM instances, or staging gateways across any provider.
- **Migration Impact**: Blindly removing `FORGE_LLM_API_BASE` would destroy generic custom endpoint configuration capabilities.
- **Action**: Preserved `llm_api_base` in `ForgeSettings` and `factory.py`, but scrubbed all Azure OpenAI and `api.openai.com` examples from `.env.example` and documentation in favor of generic proxy examples.

---

## 3. Retained Provider Matrix

| Provider Key | Protocol / Adapter | Default Model | Base URL Endpoint | Auth Variable | Execution Mode |
|---|---|---|---|---|---|
| `fake` | `FakeLLMProvider` | `forge-fake-model` | In-memory mock | None | Offline (CI / test suite) |
| `ollama` | `OpenAICompatibleProvider` | `llama3` | `http://localhost:11434/v1` | None required | Local host (Default) |
| `ollama_cloud` | `OpenAICompatibleProvider` | `gemma4:cloud` | `https://ollama.com/v1` | `OLLAMA_API_KEY` | Hosted Remote Cloud |
| `groq` | `OpenAICompatibleProvider` | `llama-3.3-70b-versatile` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | Cloud (free tier) |
| `gemini` | `OpenAICompatibleProvider` | `gemini-2.0-flash` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | Cloud (free tier) |
| `deepseek` | `OpenAICompatibleProvider` | `deepseek-chat` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | Cloud (commercial) |

---

## 4. Verification Evidence

1. **Rejection of OpenAI Provider**:
   - `test_openai_provider_rejected_as_unsupported`: Requesting `get_provider(provider_name="openai")` raises `LLMUnavailableError` with message `Unknown LLM provider 'openai'`.
   - `test_cli_investigate_openai_provider_rejected`: CLI command `forge investigate . --provider openai` terminates with exit code 1.
2. **Absence of OpenAI Endpoints**:
   - `test_no_openai_endpoint_in_factory_defaults`: Validates that no endpoint containing `api.openai.com` exists in `_DEFAULT_BASE_URLS` or `_DEFAULT_MODELS`.
   - `test_openai_compatible_provider_has_no_openai_default`: Validates that `OpenAICompatibleProvider()` defaults to empty `api_base`, never `api.openai.com`.
3. **No Implicit Fallback**:
   - `test_groq_does_not_accept_foreign_credentials`: Validates that missing provider credentials for one provider cannot borrow credentials from another provider.
4. **All Remaining Providers Operational**:
   - Offline mocked test suites verify `fake`, `ollama`, `ollama_cloud`, `groq`, `gemini`, and `deepseek` resolve and operate deterministically.
