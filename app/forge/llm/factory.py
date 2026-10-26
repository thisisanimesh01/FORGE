"""LLM provider factory for FORGE.

Resolves ``FORGE_LLM_PROVIDER`` to a configured provider instance without
requiring provider-specific SDKs.  All supported providers expose an
OpenAI-compatible ``/v1/chat/completions`` endpoint, so they are served by the
existing ``OpenAICompatibleProvider`` with provider-specific base-URLs and auth.

Supported providers
-------------------
* ``fake``     — deterministic offline mock (no credentials, no network).
* ``ollama``   — local Ollama inference server (no API key required).
* ``groq``     — Groq Cloud API (OpenAI-compatible; requires ``GROQ_API_KEY``).
* ``gemini``   — Google Gemini via the OpenAI-compatibility shim
                 (requires ``GEMINI_API_KEY``).
* ``deepseek`` — DeepSeek API (OpenAI-compatible; requires ``DEEPSEEK_API_KEY``).
* ``openai``   — OpenAI API (requires ``FORGE_LLM_API_KEY``).

Environment variable reference
-------------------------------
``FORGE_LLM_PROVIDER``  — provider selector (default: ``fake``).
``FORGE_LLM_MODEL``     — model ID override (provider-specific default used when absent).
``FORGE_LLM_API_BASE``  — base-URL override (overrides per-provider default).
``FORGE_LLM_API_KEY``   — API key (used by openai provider and as last-resort fallback).
``OLLAMA_BASE_URL``     — Ollama base URL (default: http://localhost:11434).
``GROQ_API_KEY``        — Groq API key.
``GEMINI_API_KEY``      — Gemini API key.
``DEEPSEEK_API_KEY``    — DeepSeek API key.

Security invariants
-------------------
* Automatic cloud fallback is forbidden.  If the configured provider is
  unavailable, an ``LLMUnavailableError`` is raised immediately.
* API keys are never logged or included in exception messages.
* Tests always use the ``fake`` provider; no network or credential access is
  required for the normal test suite.
"""

import os
from typing import Optional

from forge.llm.fake import FakeLLMProvider
from forge.llm.openai import OpenAICompatibleProvider
from forge.llm.provider import LLMProvider, LLMUnavailableError


# ---------------------------------------------------------------------------
# Per-provider defaults
# ---------------------------------------------------------------------------

#: Default model IDs used when ``FORGE_LLM_MODEL`` is not set.
_DEFAULT_MODELS: dict[str, str] = {
    "fake": "forge-fake-model",
    "ollama": "llama3",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o",
}

#: Base URL for each cloud provider's OpenAI-compatible endpoint.
_DEFAULT_BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
}


def get_provider(
    provider_name: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMProvider:
    """Return a configured LLM provider instance.

    Provider selection order:
    1. ``provider_name`` argument (from CLI ``--provider`` flag).
    2. ``FORGE_LLM_PROVIDER`` environment variable.
    3. Default: ``fake`` (offline, no credentials needed).

    Model selection order:
    1. ``model`` argument (from CLI ``--model`` flag).
    2. ``FORGE_LLM_MODEL`` environment variable.
    3. Provider-specific default from ``_DEFAULT_MODELS``.

    Args:
        provider_name: Override provider key (case-insensitive).
        model: Override model identifier.

    Returns:
        A configured ``LLMProvider`` instance.

    Raises:
        LLMUnavailableError: If provider is unknown or required credentials are
            missing.  **No automatic fallback to other providers.**
    """
    from forge.config import get_settings

    settings = get_settings()
    resolved_provider = (provider_name or settings.llm_provider or "fake").lower().strip()

    if resolved_provider == "fake":
        resolved_model = model or settings.llm_model or _DEFAULT_MODELS["fake"]
        return FakeLLMProvider(model_name=resolved_model)

    if resolved_provider == "ollama":
        return _build_ollama_provider(model=model, settings=settings)

    if resolved_provider == "groq":
        return _build_groq_provider(model=model, settings=settings)

    if resolved_provider == "gemini":
        return _build_gemini_provider(model=model, settings=settings)

    if resolved_provider == "deepseek":
        return _build_deepseek_provider(model=model, settings=settings)

    if resolved_provider == "openai":
        return _build_openai_provider(model=model, settings=settings)

    raise LLMUnavailableError(
        f"Unknown LLM provider '{resolved_provider}'. "
        "Supported providers: fake, ollama, groq, gemini, deepseek, openai. "
        "Set FORGE_LLM_PROVIDER to one of these values."
    )


# ---------------------------------------------------------------------------
# Per-provider builder helpers
# ---------------------------------------------------------------------------


def _build_ollama_provider(model, settings) -> OpenAICompatibleProvider:
    """Build an Ollama provider (local, no API key required)."""
    # Ollama uses its own env var name to match Friday's convention.
    ollama_base = (
        os.environ.get("OLLAMA_BASE_URL")
        or settings.llm_api_base
        or "http://localhost:11434"
    ).rstrip("/")
    # Ollama's OpenAI-compat path is /v1
    if not ollama_base.endswith("/v1"):
        api_base = f"{ollama_base}/v1"
    else:
        api_base = ollama_base

    resolved_model = model or settings.llm_model or _DEFAULT_MODELS["ollama"]
    # Ollama requires no API key; pass None so the Authorization header is omitted.
    provider = OpenAICompatibleProvider(
        api_key=None,
        model=resolved_model,
        api_base=api_base,
    )
    return provider


def _build_groq_provider(model, settings) -> OpenAICompatibleProvider:
    """Build a Groq Cloud provider."""
    api_key = (
        os.environ.get("GROQ_API_KEY")
        or settings.llm_api_key
        or ""
    ).strip()
    if not api_key:
        raise LLMUnavailableError(
            "Groq provider requires GROQ_API_KEY to be set. "
            "Add it to your .env file or environment. "
            "No automatic fallback is performed."
        )
    api_base = settings.llm_api_base or _DEFAULT_BASE_URLS["groq"]
    resolved_model = model or settings.llm_model or _DEFAULT_MODELS["groq"]
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=resolved_model,
        api_base=api_base,
    )


def _build_gemini_provider(model, settings) -> OpenAICompatibleProvider:
    """Build a Gemini provider using Google's OpenAI-compatible shim."""
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or settings.llm_api_key
        or ""
    ).strip()
    if not api_key:
        raise LLMUnavailableError(
            "Gemini provider requires GEMINI_API_KEY to be set. "
            "Add it to your .env file or environment. "
            "No automatic fallback is performed."
        )
    api_base = settings.llm_api_base or _DEFAULT_BASE_URLS["gemini"]
    resolved_model = model or settings.llm_model or _DEFAULT_MODELS["gemini"]
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=resolved_model,
        api_base=api_base,
    )


def _build_deepseek_provider(model, settings) -> OpenAICompatibleProvider:
    """Build a DeepSeek provider."""
    api_key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or settings.llm_api_key
        or ""
    ).strip()
    if not api_key:
        raise LLMUnavailableError(
            "DeepSeek provider requires DEEPSEEK_API_KEY to be set. "
            "Add it to your .env file or environment. "
            "No automatic fallback is performed."
        )
    api_base = settings.llm_api_base or _DEFAULT_BASE_URLS["deepseek"]
    resolved_model = model or settings.llm_model or _DEFAULT_MODELS["deepseek"]
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=resolved_model,
        api_base=api_base,
    )


def _build_openai_provider(model, settings) -> OpenAICompatibleProvider:
    """Build an OpenAI provider."""
    api_key = (settings.llm_api_key or "").strip()
    if not api_key:
        raise LLMUnavailableError(
            "Provider 'openai' is not configured with credentials. Set FORGE_LLM_API_KEY. "
            "No automatic fallback is performed."
        )
    api_base = settings.llm_api_base or _DEFAULT_BASE_URLS["openai"]
    resolved_model = model or settings.llm_model or _DEFAULT_MODELS["openai"]
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=resolved_model,
        api_base=api_base,
    )


def describe_provider(provider_name: Optional[str] = None) -> dict:
    """Return a sanitized description of the current provider configuration.

    Credentials are never included in the output.

    Args:
        provider_name: Override provider key (defaults to ``FORGE_LLM_PROVIDER``).

    Returns:
        Dictionary with keys ``provider``, ``model``, ``api_base``,
        ``credentials_configured``, and ``requires_network``.
    """
    from forge.config import get_settings

    settings = get_settings()
    resolved = (provider_name or settings.llm_provider or "fake").lower().strip()

    if resolved == "fake":
        return {
            "provider": "fake",
            "model": settings.llm_model or _DEFAULT_MODELS["fake"],
            "api_base": "N/A (offline mock)",
            "credentials_configured": True,
            "requires_network": False,
        }

    if resolved == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL") or settings.llm_api_base or "http://localhost:11434"
        return {
            "provider": "ollama",
            "model": settings.llm_model or _DEFAULT_MODELS["ollama"],
            "api_base": base,
            "credentials_configured": True,  # No key required for local Ollama
            "requires_network": False,        # Local by default
        }

    key_vars = {
        "groq": "GROQ_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "FORGE_LLM_API_KEY",
    }
    key_env = key_vars.get(resolved, "FORGE_LLM_API_KEY")
    key_set = bool((os.environ.get(key_env) or settings.llm_api_key or "").strip())
    base = settings.llm_api_base or _DEFAULT_BASE_URLS.get(resolved, "unknown")

    return {
        "provider": resolved,
        "model": settings.llm_model or _DEFAULT_MODELS.get(resolved, ""),
        "api_base": base,
        "credentials_configured": key_set,
        "requires_network": True,
    }
