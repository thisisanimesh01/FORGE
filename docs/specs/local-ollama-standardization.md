# Local Ollama Standardization & Cloud Removal Specification

**Status:** Completed & Verified<br>
**Milestone:** Local Ollama Standardization & Hardened Privacy Boundary<br>
**Branch:** `main`<br>
**Phase Status:** Phase 5 Completed (Released); Supervised Pilot Prerequisite<br>

---

## 1. Overview & Objective

This specification details the formal removal of **Ollama Cloud** (`ollama_cloud` / `ollama-cloud`) from FORGE and the standardization of Ollama inference exclusively on the **local Ollama runtime** (`http://localhost:11434/v1`).

### Rationale & Security Objectives
1. **Local-Only Privacy Invariant**: Guarantee that repository audit, defect investigation, reproduction planning, and repair operations using Ollama never transmit source code, prompts, tool outputs, or context across the public internet to third-party cloud infrastructure.
2. **Elimination of Ollama Cloud Credentials**: Remove `OLLAMA_API_KEY` and any Ollama Cloud credential loading from the runtime settings and configuration schema.
3. **Strict Fail-Closed Rejection**: Any attempt to configure or invoke `ollama_cloud` or `ollama-cloud` fails immediately with an explicit `LLMUnavailableError`.
4. **No Silent Fallback**: Ensure that failure to connect to the local Ollama daemon (e.g., daemon stopped, port blocked, connection refused) raises `LLMUnavailableError` immediately without falling back to cloud providers or mock providers (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
5. **Flexible Local Model Selection**: Support arbitrary locally installed Ollama models (e.g., `qwen2.5-coder:1.5b`, `llama3`, `llama3.2:3b`) via command-line arguments or `FORGE_LLM_MODEL`.

---

## 2. Supported Provider Inventory

With Ollama Cloud removed, FORGE supports exactly 5 LLM providers:

| Provider Key | Type | Default Endpoint | Default Model | Authentication | Network |
|---|---|---|---|---|---|
| `fake` | Offline mock | N/A | `mock-default` | None | Offline |
| `ollama` | Local runner | `http://localhost:11434/v1` | `llama3` (or configured) | None | Loopback only |
| `groq` | Cloud API | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` | Outbound HTTPS |
| `gemini` | Cloud API | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.0-flash` | `GEMINI_API_KEY` | Outbound HTTPS |
| `deepseek` | Cloud API | `https://api.deepseek.com/v1` | `deepseek-chat` | `DEEPSEEK_API_KEY` | Outbound HTTPS |

*Note: OpenAI API remains permanently removed and rejected.*

---

## 3. Architecture & Implementation Changes

### 3.1. Provider Factory (`app/forge/llm/factory.py`)
- Removed `ollama_cloud` from `_DEFAULT_MODELS` and `_DEFAULT_BASE_URLS`.
- Removed `_build_ollama_cloud_provider()` builder.
- Added explicit rejection guard:
  ```python
  if resolved_provider in ("ollama_cloud", "ollama-cloud"):
      raise LLMUnavailableError(
          "Ollama Cloud provider has been removed; Ollama is local-only at http://localhost:11434/v1. "
          "Use 'ollama' with a locally hosted model."
      )
  ```
- Local Ollama builder (`_build_ollama_provider`) binds strictly to `http://localhost:11434/v1` (unless explicitly overridden via `OLLAMA_BASE_URL` or `FORGE_LLM_API_BASE`).
- Provider description (`describe_provider`) returns `unknown` endpoint and `credentials_configured = False` if `ollama_cloud` is queried.

### 3.2. Settings & Secret Redaction (`app/forge/config.py`)
- Removed `ollama_api_key` field from `ForgeSettings`.
- Removed `ollama_api_key` from `get_sanitized_dict()`.
- Allowed provider choices updated: `fake, ollama, groq, gemini, or deepseek`.

### 3.3. CLI Interface (`app/forge/cli.py`)
- Updated `--provider` option help strings in `investigate` and `benchmark repair` commands to reflect `fake, ollama, groq, gemini, deepseek`.

### 3.4. Template & Configuration (`.env.example`)
- Removed `ollama_cloud` and `OLLAMA_API_KEY`.
- Added documentation and examples for local Ollama setup using `qwen2.5-coder:1.5b`.

---

## 4. Verification & Testing Matrix

The following test suites verify the enforcement of this specification:
1. `tests/unit/test_llm_providers.py`:
   - `test_ollama_cloud_provider_is_removed`: Confirms `get_provider("ollama_cloud")` raises `LLMUnavailableError`.
   - `test_ollama_cloud_hyphen_alias_is_removed`: Confirms `get_provider("ollama-cloud")` raises `LLMUnavailableError`.
   - `test_ollama_defaults_to_local_v1_endpoint`: Confirms default URL is `http://localhost:11434/v1` with no API key.
   - `test_ollama_qwen_model_configuration`: Confirms `qwen2.5-coder:1.5b` model configuration.
   - `test_local_ollama_connection_failure_fails_closed`: Confirms network connection errors fail closed and do not route to cloud or fake mocks.
   - `test_ollama_cloud_describe_not_supported`: Confirms `describe_provider` does not claim credentials or cloud URL.
   - `test_forge_settings_does_not_have_llm_api_key`: Confirms `ollama_api_key` is not present in sanitized settings.
2. `tests/unit/test_phase4_readiness.py`:
   - `test_supported_providers_inventory`: Confirms exact 5-provider set.
   - `test_ollama_cloud_provider_strictly_rejected`: Confirms strict rejection of Ollama Cloud.
