"""Tests for Phase 3→4 Provider Transition: multi-provider factory, configuration,
credential validation, fallback prevention, and secret redaction.

All tests are fully offline — no network, no API keys, no Docker required.
Live provider integration is explicitly opt-in and not run in CI.
"""

import os
import pytest
from unittest.mock import MagicMock, patch
from typing import Optional

from forge.llm.factory import (
    get_provider,
    describe_provider,
    _DEFAULT_MODELS,
    _DEFAULT_BASE_URLS,
)
from forge.llm.fake import FakeLLMProvider
from forge.llm.openai import OpenAICompatibleProvider
from forge.llm.provider import LLMUnavailableError
from forge.config import redact_secrets, ForgeSettings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(**kwargs):
    """Context manager that temporarily sets environment variables."""
    return patch.dict(os.environ, {k: v for k, v in kwargs.items() if v is not None}, clear=False)


# ---------------------------------------------------------------------------
# 1. Provider factory — provider selection
# ---------------------------------------------------------------------------

class TestGetProvider:
    """Tests for get_provider() factory function."""

    def test_fake_provider_returned_by_default(self):
        """No env vars → fake provider is returned."""
        with patch.dict(os.environ, {"FORGE_LLM_PROVIDER": "fake"}, clear=False):
            provider = get_provider()
        assert isinstance(provider, FakeLLMProvider)

    def test_fake_provider_explicit_argument(self):
        """Explicit provider_name='fake' → FakeLLMProvider."""
        provider = get_provider(provider_name="fake")
        assert isinstance(provider, FakeLLMProvider)
        assert provider.is_available()

    def test_fake_provider_custom_model(self):
        """Custom model name is passed through to FakeLLMProvider."""
        provider = get_provider(provider_name="fake", model="my-custom-fake")
        assert isinstance(provider, FakeLLMProvider)
        assert provider.model_name == "my-custom-fake"

    def test_ollama_provider_no_api_key_required(self):
        """Ollama provider builds without any API key."""
        with _env(FORGE_LLM_PROVIDER="ollama", OLLAMA_BASE_URL="http://localhost:11434"):
            provider = get_provider()
        assert isinstance(provider, OpenAICompatibleProvider)
        # Ollama uses /v1 subpath
        assert "/v1" in provider.api_base
        # No API key header is needed — key is absent or empty
        assert not provider.api_key  # None or empty string both acceptable

    def test_ollama_default_base_url(self):
        """Ollama uses localhost:11434 when OLLAMA_BASE_URL is absent."""
        with _env(FORGE_LLM_PROVIDER="ollama"):
            env_copy = {k: v for k, v in os.environ.items() if k != "OLLAMA_BASE_URL"}
            with patch.dict(os.environ, env_copy, clear=True):
                provider = get_provider(provider_name="ollama")
        assert "11434" in provider.api_base

    def test_ollama_custom_base_url(self):
        """OLLAMA_BASE_URL is respected."""
        with _env(OLLAMA_BASE_URL="http://192.168.1.10:11434"):
            provider = get_provider(provider_name="ollama")
        assert "192.168.1.10" in provider.api_base

    def test_ollama_cloud_provider_requires_api_key(self):
        """Ollama Cloud raises LLMUnavailableError when OLLAMA_API_KEY is missing."""
        clean = dict(os.environ)
        clean["OLLAMA_API_KEY"] = ""
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError, match="OLLAMA_API_KEY"):
                get_provider(provider_name="ollama_cloud")

    def test_ollama_cloud_provider_with_api_key(self):
        """Ollama Cloud builds successfully when OLLAMA_API_KEY is set."""
        with _env(OLLAMA_API_KEY="ollama_secret_cloud_key"):
            provider = get_provider(provider_name="ollama_cloud")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.api_base == "https://ollama.com/v1"
        assert provider.api_key == "ollama_secret_cloud_key"

    def test_ollama_cloud_hyphen_alias(self):
        """Hyphenated provider name 'ollama-cloud' resolves correctly."""
        with _env(OLLAMA_API_KEY="ollama_secret_cloud_key"):
            provider = get_provider(provider_name="ollama-cloud")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.api_base == "https://ollama.com/v1"


    def test_groq_provider_requires_api_key(self):
        """Groq raises LLMUnavailableError when GROQ_API_KEY is missing."""
        clean = dict(os.environ)
        clean["GROQ_API_KEY"] = ""
        clean["GROQ_API"] = ""
        clean["FORGE_LLM_API_KEY"] = ""
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError, match="GROQ_API_KEY"):
                get_provider(provider_name="groq")

    def test_groq_provider_with_api_key(self):
        """Groq builds successfully when GROQ_API_KEY is set."""
        with _env(GROQ_API_KEY="gsk_fake_test_key_not_real"):
            provider = get_provider(provider_name="groq")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert "groq.com" in provider.api_base

    def test_gemini_provider_requires_api_key(self):
        """Gemini raises LLMUnavailableError when GEMINI_API_KEY is missing."""
        clean = dict(os.environ)
        clean["GEMINI_API_KEY"] = ""
        clean["GEMINI_API"] = ""
        clean["FORGE_LLM_API_KEY"] = ""
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError, match="GEMINI_API_KEY"):
                get_provider(provider_name="gemini")

    def test_gemini_provider_with_api_key(self):
        """Gemini builds with correct base URL when GEMINI_API_KEY is set."""
        with _env(GEMINI_API_KEY="AIza_fake_test_not_real"):
            provider = get_provider(provider_name="gemini")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert "generativelanguage.googleapis.com" in provider.api_base

    def test_deepseek_provider_requires_api_key(self):
        """DeepSeek raises LLMUnavailableError when DEEPSEEK_API_KEY is missing."""
        clean = dict(os.environ)
        clean["DEEPSEEK_API_KEY"] = ""
        clean["DEEPSEEK_API"] = ""
        clean["FORGE_LLM_API_KEY"] = ""
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError, match="DEEPSEEK_API_KEY"):
                get_provider(provider_name="deepseek")

    def test_deepseek_provider_with_api_key(self):
        """DeepSeek builds with correct base URL when DEEPSEEK_API_KEY is set."""
        with _env(DEEPSEEK_API_KEY="sk-fake_deepseek_test_not_real_key"):
            provider = get_provider(provider_name="deepseek")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert "deepseek.com" in provider.api_base

    def test_openai_provider_rejected_as_unsupported(self):
        """OpenAI is rejected as an unsupported/unknown provider."""
        with pytest.raises(LLMUnavailableError, match="Unknown LLM provider 'openai'"):
            get_provider(provider_name="openai")

    def test_no_openai_endpoint_in_factory_defaults(self):
        """OpenAI endpoints and default models are not registered in the factory."""
        assert "openai" not in _DEFAULT_MODELS
        assert "openai" not in _DEFAULT_BASE_URLS
        for url in _DEFAULT_BASE_URLS.values():
            assert "api.openai.com" not in url

    def test_openai_compatible_provider_has_no_openai_default(self):
        """OpenAICompatibleProvider has no default URL pointing to api.openai.com."""
        provider = OpenAICompatibleProvider()
        assert "api.openai.com" not in provider.api_base
        assert provider.api_base == ""

    def test_groq_does_not_accept_foreign_credentials(self):
        """Groq requires GROQ_API_KEY and does not fall back to other credentials."""
        clean = dict(os.environ)
        clean["GROQ_API_KEY"] = ""
        clean["GROQ_API"] = ""
        clean["GEMINI_API_KEY"] = "AIza_gemini_key"
        clean["DEEPSEEK_API_KEY"] = "sk-deepseek_key"
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError, match="GROQ_API_KEY"):
                get_provider(provider_name="groq")

    def test_unknown_provider_raises(self):
        """Unknown provider name raises LLMUnavailableError."""
        with pytest.raises(LLMUnavailableError, match="Unknown LLM provider"):
            get_provider(provider_name="magic_cloud_ai")

    def test_provider_name_case_insensitive(self):
        """Provider name is normalized to lowercase."""
        provider = get_provider(provider_name="FAKE")
        assert isinstance(provider, FakeLLMProvider)

    def test_provider_name_whitespace_stripped(self):
        """Leading/trailing whitespace in provider name is stripped."""
        provider = get_provider(provider_name="  fake  ")
        assert isinstance(provider, FakeLLMProvider)


# ---------------------------------------------------------------------------
# 2. Provider factory — model selection
# ---------------------------------------------------------------------------

class TestProviderModelDefaults:
    """Tests that provider-specific model defaults are applied correctly."""

    def test_fake_default_model(self):
        # Env var explicitly set to empty string overrides any value in .env file
        env_with_empty_model = dict(os.environ)
        env_with_empty_model["FORGE_LLM_MODEL"] = ""
        with patch.dict(os.environ, env_with_empty_model, clear=True):
            provider = get_provider(provider_name="fake")
        assert provider.model_name == _DEFAULT_MODELS["fake"]

    def test_ollama_default_model(self):
        with _env(FORGE_LLM_MODEL=""):
            provider = get_provider(provider_name="ollama")
        assert provider.model == _DEFAULT_MODELS["ollama"]

    def test_ollama_cloud_default_model(self):
        with _env(OLLAMA_API_KEY="test_key", FORGE_LLM_MODEL=""):
            provider = get_provider(provider_name="ollama_cloud")
        assert provider.model == _DEFAULT_MODELS["ollama_cloud"]
        assert provider.model == "gemma4:cloud"

    def test_groq_default_model(self):
        with _env(GROQ_API_KEY="gsk_fake_key", FORGE_LLM_MODEL=""):
            provider = get_provider(provider_name="groq")
        assert provider.model == _DEFAULT_MODELS["groq"]

    def test_gemini_default_model(self):
        with _env(GEMINI_API_KEY="AIza_fake_key", FORGE_LLM_MODEL=""):
            provider = get_provider(provider_name="gemini")
        assert provider.model == _DEFAULT_MODELS["gemini"]

    def test_deepseek_default_model(self):
        with _env(DEEPSEEK_API_KEY="sk-fake_deepseek", FORGE_LLM_MODEL=""):
            provider = get_provider(provider_name="deepseek")
        assert provider.model == _DEFAULT_MODELS["deepseek"]

    def test_model_override_respected(self):
        """Explicit model argument overrides provider default."""
        provider = get_provider(provider_name="fake", model="custom-llm-v1")
        assert provider.model_name == "custom-llm-v1"

    def test_env_model_used_when_no_override(self):
        """FORGE_LLM_MODEL env var is used when model argument is absent."""
        with _env(FORGE_LLM_MODEL="llama3.2", OLLAMA_BASE_URL="http://localhost:11434"):
            provider = get_provider(provider_name="ollama")
        assert provider.model == "llama3.2"


# ---------------------------------------------------------------------------
# 3. Cloud fallback prevention
# ---------------------------------------------------------------------------

class TestCloudFallbackPrevention:
    """Verify that automatic cloud fallback is strictly forbidden."""

    def test_missing_key_raises_not_falls_back(self):
        """Missing Groq key must raise, not silently fall back to fake or another provider."""
        clean = dict(os.environ)
        clean["GROQ_API_KEY"] = ""
        clean["GROQ_API"] = ""
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError):
                get_provider(provider_name="groq")
        # Verify no FakeLLMProvider was silently returned
        # (test passes by virtue of the exception being raised above)

    def test_ollama_cloud_distinct_from_local_ollama(self):
        """Local ollama and ollama_cloud are distinct; missing cloud key does not route locally."""
        clean = dict(os.environ)
        clean["OLLAMA_API_KEY"] = ""
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError, match="OLLAMA_API_KEY"):
                get_provider(provider_name="ollama_cloud")

    def test_config_allow_cloud_fallback_defaults_false(self):
        """ForgeSettings.allow_cloud_fallback must default to False."""
        settings = ForgeSettings()
        assert settings.allow_cloud_fallback is False

    def test_config_allow_cloud_fallback_cannot_silently_enable(self):
        """Setting FORGE_ALLOW_CLOUD_FALLBACK=true is accepted in config but must not change provider behavior."""
        # The field exists but the factory does not use it to trigger silent fallback.
        with _env(FORGE_ALLOW_CLOUD_FALLBACK="true"):
            settings = ForgeSettings()
        # The setting is loaded correctly
        assert settings.allow_cloud_fallback is True
        # But the factory still raises for a provider with missing credentials.
        clean = dict(os.environ)
        clean["GROQ_API_KEY"] = ""
        clean["GROQ_API"] = ""
        clean["FORGE_ALLOW_CLOUD_FALLBACK"] = "true"
        with patch.dict(os.environ, clean, clear=True):
            with pytest.raises(LLMUnavailableError):
                get_provider(provider_name="groq")


# ---------------------------------------------------------------------------
# 4. describe_provider — sanitized diagnostics
# ---------------------------------------------------------------------------

class TestDescribeProvider:
    """Tests for describe_provider() diagnostics helper."""

    def test_fake_describe(self):
        info = describe_provider("fake")
        assert info["provider"] == "fake"
        assert info["requires_network"] is False
        assert info["credentials_configured"] is True
        assert "REDACTED" not in str(info)

    def test_ollama_describe_no_key_field(self):
        with _env(OLLAMA_BASE_URL="http://localhost:11434"):
            info = describe_provider("ollama")
        assert info["provider"] == "ollama"
        assert info["requires_network"] is False
        assert info["credentials_configured"] is True  # No key needed

    def test_ollama_cloud_describe(self):
        with _env(OLLAMA_API_KEY="cloud_test_secret"):
            info = describe_provider("ollama_cloud")
        assert info["provider"] == "ollama_cloud"
        assert info["requires_network"] is True
        assert info["credentials_configured"] is True
        assert "https://ollama.com/v1" in info["api_base"]
        assert "cloud_test_secret" not in str(info)

    def test_groq_describe_no_key_configured(self):
        clean = dict(os.environ)
        clean["GROQ_API_KEY"] = ""
        clean["GROQ_API"] = ""
        with patch.dict(os.environ, clean, clear=True):
            info = describe_provider("groq")
        assert info["provider"] == "groq"
        assert info["credentials_configured"] is False
        assert info["requires_network"] is True
        assert "api.groq.com" in info["api_base"]

    def test_describe_provider_openai_rejected_as_unknown(self):
        """describe_provider for 'openai' does not claim credentials or return openai endpoint."""
        info = describe_provider("openai")
        assert info["provider"] == "openai"
        assert info["credentials_configured"] is False
        assert info["api_base"] == "unknown"
        assert "api.openai.com" not in info["api_base"]

    def test_groq_describe_key_configured(self):
        with _env(GROQ_API_KEY="gsk_test_only"):
            info = describe_provider("groq")
        assert info["credentials_configured"] is True
        # API key value must not be in the output
        assert "gsk_test_only" not in str(info)

    def test_gemini_describe_base_url(self):
        with _env(GEMINI_API_KEY="AIza_fake"):
            info = describe_provider("gemini")
        assert "generativelanguage.googleapis.com" in info["api_base"]

    def test_deepseek_describe_base_url(self):
        with _env(DEEPSEEK_API_KEY="sk-fake"):
            info = describe_provider("deepseek")
        assert "deepseek.com" in info["api_base"]

    def test_no_secrets_leak_in_describe(self):
        """describe_provider never includes actual key values."""
        fake_key = "gsk_this_is_a_fake_secret_key_do_not_use"
        with _env(GROQ_API_KEY=fake_key):
            info = describe_provider("groq")
        assert fake_key not in str(info)


# ---------------------------------------------------------------------------
# 5. Secret redaction
# ---------------------------------------------------------------------------

class TestSecretRedaction:
    """Tests for the redact_secrets() utility."""

    def test_groq_key_pattern_redacted(self):
        text = "Error: using gsk_abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuv12345678 for auth"
        result = redact_secrets(text)
        assert "gsk_" not in result

    def test_sk_key_pattern_redacted(self):
        text = "api_key: sk-abcdef123456789012345678901234567890abcdef"
        result = redact_secrets(text)
        assert "sk-" not in result or "[REDACTED" in result

    def test_empty_string_handled_gracefully(self):
        assert redact_secrets("") == ""

    def test_none_safe(self):
        # redact_secrets should handle falsy input without crashing
        assert redact_secrets(None) is None  # type: ignore[arg-type]

    def test_no_credentials_unchanged(self):
        safe_text = "Investigation completed. Found 3 findings."
        result = redact_secrets(safe_text)
        # Text without secrets passes through (allow for minor whitespace changes)
        assert "Investigation completed" in result
        assert "3 findings" in result

    def test_forge_settings_does_not_have_llm_api_key(self):
        """ForgeSettings no longer defines llm_api_key."""
        settings = ForgeSettings()
        assert not hasattr(settings, "llm_api_key")
        sanitized = settings.get_sanitized_dict()
        assert "llm_api_key" not in sanitized

    def test_forge_settings_redacts_provider_specific_keys(self):
        """get_sanitized_dict() redacts GROQ_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY, OLLAMA_API_KEY."""
        with _env(
            GROQ_API_KEY="gsk_fakefakefakefakefakefakefakefakefakefakefakefakefakefake",
            GEMINI_API_KEY="AIza_fakefake",
            DEEPSEEK_API_KEY="sk-fakedeepseekkey",
            OLLAMA_API_KEY="ollama_secret_cloud_key_value",
        ):
            settings = ForgeSettings()
            sanitized = settings.get_sanitized_dict()
        assert sanitized.get("groq_api_key") == "[REDACTED]"
        assert sanitized.get("gemini_api_key") == "[REDACTED]"
        assert sanitized.get("deepseek_api_key") == "[REDACTED]"
        assert sanitized.get("ollama_api_key") == "[REDACTED]"

    def test_forge_settings_unset_key_shown_as_not_set(self):
        """Keys that are not set appear as '(not set)' rather than empty."""
        clean = dict(os.environ)
        clean["GROQ_API_KEY"] = ""
        clean["GROQ_API"] = ""
        clean["GEMINI_API_KEY"] = ""
        clean["GEMINI_API"] = ""
        clean["DEEPSEEK_API_KEY"] = ""
        clean["DEEPSEEK_API"] = ""
        clean["OLLAMA_API_KEY"] = ""
        with patch.dict(os.environ, clean, clear=True):
            settings = ForgeSettings()
            sanitized = settings.get_sanitized_dict()
        assert sanitized.get("groq_api_key") == "(not set)"
        assert sanitized.get("ollama_api_key") == "(not set)"




# ---------------------------------------------------------------------------
# 6. Configuration loading
# ---------------------------------------------------------------------------

class TestConfigurationLoading:
    """Tests for ForgeSettings loading and defaults."""

    def test_default_provider_is_ollama_after_env_set(self):
        """FORGE_LLM_PROVIDER env var is picked up."""
        with _env(FORGE_LLM_PROVIDER="ollama"):
            settings = ForgeSettings()
        assert settings.llm_provider == "ollama"

    def test_default_model_is_empty_string(self):
        """FORGE_LLM_MODEL can be set to empty string, allowing provider defaults."""
        # Explicitly clear FORGE_LLM_MODEL to test the empty-string-default behavior
        # (the .env file may set a model; this test verifies the config field accepts empty)
        env_with_empty_model = dict(os.environ)
        env_with_empty_model["FORGE_LLM_MODEL"] = ""
        with patch.dict(os.environ, env_with_empty_model, clear=True):
            settings = ForgeSettings()
        assert settings.llm_model == ""

    def test_allow_cloud_fallback_defaults_to_false(self):
        settings = ForgeSettings()
        assert settings.allow_cloud_fallback is False

    def test_sanitized_dict_omits_all_secrets(self):
        with _env(GROQ_API_KEY="gsk_fake123456789012345678901234567890ab"):
            settings = ForgeSettings()
        sanitized = settings.get_sanitized_dict()
        full_str = str(sanitized)
        # Must not contain the actual key value
        assert "gsk_fake" not in full_str

    def test_ollama_base_url_in_sanitized_dict(self):
        """OLLAMA_BASE_URL appears verbatim in sanitized dict (not a secret)."""
        with _env(OLLAMA_BASE_URL="http://custom-host:11434"):
            settings = ForgeSettings()
            sanitized = settings.get_sanitized_dict()
        assert "custom-host" in sanitized.get("ollama_base_url", "")


# ---------------------------------------------------------------------------
# 7. FakeLLMProvider still works for offline tests
# ---------------------------------------------------------------------------

class TestFakeLLMProviderPreservation:
    """Verify FakeLLMProvider is fully preserved and works as before."""

    def test_fake_is_always_available(self):
        provider = FakeLLMProvider()
        assert provider.is_available()

    def test_fake_returns_scripted_response(self):
        from forge.llm.models import LLMResponse, LLMRequest, ChatMessage, MessageRole
        scripted = LLMResponse(content="scripted result", finish_reason="stop", model="fake")
        provider = FakeLLMProvider(scripted_responses=[scripted])
        req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="hello")])
        resp = provider.generate(req)
        assert resp.content == "scripted result"

    def test_fake_records_call_history(self):
        from forge.llm.models import LLMRequest, ChatMessage, MessageRole
        provider = FakeLLMProvider()
        req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="audit this")])
        provider.generate(req)
        assert len(provider.call_history) == 1


# ---------------------------------------------------------------------------
# 8. OpenAICompatibleProvider endpoint correctness
# ---------------------------------------------------------------------------

class TestOpenAICompatibleProviderEndpoints:
    """Verify provider-specific endpoints are set correctly by the factory."""

    def test_groq_endpoint(self):
        with _env(GROQ_API_KEY="gsk_fake"):
            provider = get_provider(provider_name="groq")
        assert provider.api_base == _DEFAULT_BASE_URLS["groq"].rstrip("/")

    def test_gemini_endpoint(self):
        with _env(GEMINI_API_KEY="AIza_fake"):
            provider = get_provider(provider_name="gemini")
        assert provider.api_base == _DEFAULT_BASE_URLS["gemini"].rstrip("/")

    def test_deepseek_endpoint(self):
        with _env(DEEPSEEK_API_KEY="sk-fake_deepseek"):
            provider = get_provider(provider_name="deepseek")
        assert provider.api_base == _DEFAULT_BASE_URLS["deepseek"].rstrip("/")

    def test_api_base_override_respected(self):
        """FORGE_LLM_API_BASE overrides the default endpoint for any cloud provider."""
        with _env(
            GROQ_API_KEY="gsk_fake",
            FORGE_LLM_API_BASE="https://my-proxy.example.com/v1",
        ):
            provider = get_provider(provider_name="groq")
        assert "my-proxy.example.com" in provider.api_base

    def test_ollama_appends_v1_path(self):
        """Ollama API base always ends in /v1 for OpenAI compat."""
        with _env(OLLAMA_BASE_URL="http://localhost:11434"):
            provider = get_provider(provider_name="ollama")
        assert provider.api_base.endswith("/v1")

    def test_ollama_no_double_v1(self):
        """If OLLAMA_BASE_URL already ends in /v1, it is not doubled."""
        with _env(OLLAMA_BASE_URL="http://localhost:11434/v1"):
            provider = get_provider(provider_name="ollama")
        assert not provider.api_base.endswith("/v1/v1")

    def test_groq_api_alias_accepted(self):
        """GROQ_API alias is accepted when GROQ_API_KEY is not set."""
        with patch.dict(os.environ, {"GROQ_API": "gsk_alias_key"}, clear=False):
            # Remove GROQ_API_KEY if present
            clean = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
            clean["GROQ_API"] = "gsk_alias_key"
            with patch.dict(os.environ, clean, clear=True):
                provider = get_provider(provider_name="groq")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.api_key == "gsk_alias_key"

    def test_gemini_api_alias_accepted(self):
        """GEMINI_API alias is accepted when GEMINI_API_KEY is not set."""
        clean = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        clean["GEMINI_API"] = "AIza_alias_key"
        with patch.dict(os.environ, clean, clear=True):
            provider = get_provider(provider_name="gemini")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.api_key == "AIza_alias_key"

    def test_deepseek_api_alias_accepted(self):
        """DEEPSEEK_API alias is accepted when DEEPSEEK_API_KEY is not set."""
        clean = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        clean["DEEPSEEK_API"] = "sk-deepseek-alias"
        with patch.dict(os.environ, clean, clear=True):
            provider = get_provider(provider_name="deepseek")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.api_key == "sk-deepseek-alias"


# ---------------------------------------------------------------------------
# 9. Bounded retries
# ---------------------------------------------------------------------------

class TestBoundedRetries:
    """Verify bounded retry behavior for transient errors."""

    def test_transient_500_retried_and_succeeds(self):
        import httpx
        from forge.llm.models import LLMRequest, ChatMessage, MessageRole

        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "recovered", "role": "assistant"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        provider = OpenAICompatibleProvider(
            api_key="sk-test",
            api_base="https://api.mock.example/v1",
            http_client=client,
            max_retries=1,
        )

        req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        resp = provider.generate(req)
        assert resp.content == "recovered"
        assert calls == 2

    def test_persistent_500_raises_after_max_retries(self):
        import httpx
        from forge.llm.models import LLMRequest, ChatMessage, MessageRole

        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": "service unavailable"})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        provider = OpenAICompatibleProvider(
            api_key="sk-test",
            api_base="https://api.mock.example/v1",
            http_client=client,
            max_retries=1,
        )

        req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        with pytest.raises(LLMUnavailableError, match="service error"):
            provider.generate(req)
        assert calls == 2
