# Ollama Cloud Provider Integration Specification

> [!WARNING]
> **SUPERSEDED / REMOVED**: Ollama Cloud has been formally removed from FORGE. Ollama inference is standardized strictly on the local Ollama runtime (`http://localhost:11434/v1`) with zero cloud credentials. See [`docs/specs/local-ollama-standardization.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/local-ollama-standardization.md).

**Status:** Superseded & Removed
**Milestone:** Pre-Phase 4 Provider Extension (Ollama Cloud)  
**Branch:** `main`  
**Phase Status:** Phase 3 Completed; Pre-Phase 4 Completed; Phase 4 Not Started  

---

## 1. Overview & Objective

This specification details the addition of **Ollama Cloud** (`ollama_cloud` / `ollama-cloud`) to FORGE's multi-provider LLM infrastructure. 

Ollama Cloud offers managed, hosted inference for open models through an OpenAI-compatible HTTP interface located at `https://ollama.com/v1`. This integration allows FORGE users to leverage cloud-hosted Ollama models without running local model weights, while strictly maintaining FORGE's core invariants:
- **Zero-SDK overhead**: Connects through FORGE's existing `OpenAICompatibleProvider` via standard `httpx`.
- **Decoupled configuration**: Local Ollama (`ollama`) and Ollama Cloud (`ollama_cloud`) are separate, explicit providers. Local requests never silently fall back or route to the cloud.
- **Fail-secure cloud policy**: Explicit provider selection is required. `FORGE_ALLOW_CLOUD_FALLBACK=false` prevents silent routing.
- **Strict secret sanitization**: `OLLAMA_API_KEY` is fully scrubbed in logs, events, diagnostic dumps, and CLI output.

---

## 2. Architecture & Provider Matrix

### Provider Configuration

| Attribute | Local Ollama (`ollama`) | Ollama Cloud (`ollama_cloud` / `ollama-cloud`) |
|---|---|---|
| **Adapter Class** | `OpenAICompatibleProvider` | `OpenAICompatibleProvider` |
| **Default Base URL** | `http://localhost:11434/v1` | `https://ollama.com/v1` |
| **Default Model** | `llama3` (local weights) | `gemma4:cloud` (cloud catalog) |
| **Authentication** | None required | `Authorization: Bearer <OLLAMA_API_KEY>` |
| **Execution Environment** | Local host (port 11434) | Hosted remote infrastructure (`ollama.com`) |
| **Network Required** | No (loopback only) | Yes (outbound HTTPS) |
| **Credential Variable** | N/A | `OLLAMA_API_KEY` / `FORGE_OLLAMA_API_KEY` |

### Provider Factory Resolution

In `app/forge/llm/factory.py`, provider resolution normalizes identifiers by lowercasing and replacing hyphens with underscores:
- `ollama_cloud` and `ollama-cloud` map to `_build_ollama_cloud_provider`.
- `_build_ollama_cloud_provider` verifies `OLLAMA_API_KEY`. If unset or empty, it immediately raises `LLMUnavailableError` without attempting fallback.
- `describe_provider("ollama_cloud")` returns sanitized metadata:
  ```json
  {
    "provider": "ollama_cloud",
    "model": "gemma4:cloud",
    "api_base": "https://ollama.com/v1",
    "requires_network": true,
    "credentials_configured": true
  }
  ```
  The raw API key is never exposed.

---

## 3. Configuration & Credential Safety

### Environment Variables
Added to `app/forge/config.py`:
- `ollama_api_key: str = Field(default="", description="API key for Ollama Cloud", validation_alias=AliasChoices("OLLAMA_API_KEY", "FORGE_OLLAMA_API_KEY"))`

### Redaction Invariants
- `ForgeSettings.get_sanitized_dict()` masks `ollama_api_key` as `"[REDACTED]"` when set, or `"(not set)"` when empty.
- Outbound headers strip and sanitize bearer tokens before logging or recording error events.
- `.env.example` includes documentation and an empty placeholder:
  ```bash
  # Ollama Cloud API Key (required only when FORGE_LLM_PROVIDER=ollama_cloud)
  # Endpoint: https://ollama.com/v1
  OLLAMA_API_KEY=
  ```
- `.env` is verified gitignored and never committed.

---

## 4. Privacy, Security, & Quotas

### Data Privacy Boundary
- **Local Ollama**: Repository diffs, symbols, and analysis prompts remain strictly on the local machine within `http://localhost:11434`.
- **Ollama Cloud**: Analysis prompts and code excerpts are transmitted over TLS to Ollama's cloud endpoints (`https://ollama.com/v1`).
- **Audit-Only Principle**: Regardless of provider, model output is untrusted data and cannot grant filesystem write permissions or bypass container sandboxing.

### Account Limits & Error Handling
- HTTP 401/403 responses are mapped to `LLMAuthenticationError`.
- HTTP 429 rate limit errors are mapped to `LLMRateLimitError`.
- HTTP 5xx responses execute a single bounded retry (`max_retries=1`) before raising `LLMProviderError`.
- Automatic fallback to alternative providers upon error is strictly blocked.

---

## 5. Verification & Test Evidence

### Offline Unit Tests (`tests/unit/test_llm_providers.py`)
7 new tests added to verify provider behavior without network calls:
1. `test_ollama_cloud_provider_requires_api_key`: Validates `LLMUnavailableError` is raised when `OLLAMA_API_KEY` is missing.
2. `test_ollama_cloud_provider_with_api_key`: Validates successful initialization with target endpoint `https://ollama.com/v1`.
3. `test_ollama_cloud_hyphen_alias`: Validates `ollama-cloud` alias resolution.
4. `test_ollama_cloud_default_model`: Confirms default model resolves to `gemma4:cloud`.
5. `test_ollama_cloud_distinct_from_local_ollama`: Confirms local and cloud providers remain strictly decoupled and failure does not route locally.
6. `test_ollama_cloud_describe`: Confirms diagnostic reporting indicates `requires_network=True` with redacted credentials.
7. `test_forge_settings_redacts_ollama_api_key`: Confirms redaction in `ForgeSettings.get_sanitized_dict()`.

All 66 tests in `test_llm_providers.py` pass cleanly. Full regression suite passes 534 tests.
