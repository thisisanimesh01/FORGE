"""Opt-in live Ollama local inference integration tests.

Verifies:
1. Live endpoint connectivity at http://localhost:11434.
2. Model inventory inspection and presence of local coding model (e.g. qwen2.5-coder:1.5b).
3. Local completion generation through OpenAI-compatible interface without cloud fallback.
4. Fail-closed behavior on unreachable endpoints or absent models.
"""

import json
import os
import urllib.request
import pytest

from forge.config import ForgeSettings
from forge.diagnostics import DiagnosticCategory, check_llm_provider
from forge.llm.factory import get_provider
from forge.llm.models import ChatMessage, LLMRequest, MessageRole

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("FORGE_LIVE_OLLAMA_TESTS"),
        reason="Opt-in live Ollama integration tests. Run with FORGE_LIVE_OLLAMA_TESTS=1 to execute.",
    ),
]


def test_live_ollama_endpoint_and_inventory():
    """Verify live Ollama HTTP server is responsive and inventory includes models."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    tags_url = f"{base_url}/api/tags"

    req = urllib.request.Request(tags_url, headers={"User-Agent": "FORGE-Test/0.1.0b1"})
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))

    models = data.get("models", [])
    assert len(models) > 0, f"No models found in Ollama at {base_url}"
    model_names = [m.get("name", "") for m in models]
    assert any("qwen" in name.lower() or "llama" in name.lower() or "coder" in name.lower() for name in model_names), (
        f"Expected coding model in inventory, found: {model_names}"
    )


def test_live_ollama_diagnostic_probe():
    """Verify check_llm_provider detects live Ollama model accurately."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    test_model = os.environ.get("FORGE_LLM_MODEL", "qwen2.5-coder:1.5b")

    settings = ForgeSettings(
        llm_provider="ollama",
        llm_model=test_model,
        ollama_base_url=base_url,
        allow_cloud_fallback=False,
    )

    check = check_llm_provider(settings, timeout_seconds=5.0)
    assert check.category == DiagnosticCategory.AVAILABLE_HEALTHY
    assert check.is_healthy is True
    assert test_model in check.message


def test_live_ollama_local_inference_completion():
    """Verify live Ollama model produces valid completion through OpenAICompatibleProvider."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    test_model = os.environ.get("FORGE_LLM_MODEL", "qwen2.5-coder:1.5b")

    provider = get_provider("ollama", model=test_model)
    request = LLMRequest(
        messages=[
            ChatMessage(role=MessageRole.SYSTEM, content="You are a precise software engineering assistant."),
            ChatMessage(role=MessageRole.USER, content="Reply with exact phrase: HELLO_FORGE_DIAGNOSTICS"),
        ],
        temperature=0.0,
        max_tokens=32,
    )

    response = provider.generate(request)
    assert response is not None
    assert response.content is not None
    assert len(response.content.strip()) > 0
    assert "HELLO_FORGE_DIAGNOSTICS" in response.content
