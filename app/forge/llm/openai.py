"""OpenAI-compatible LLM provider implementation using standard httpx."""

import json
from typing import Any, Dict, List, Optional
import httpx

from forge.config import redact_secrets, get_settings
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
)
from forge.llm.provider import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMRateLimitError,
    LLMUnavailableError,
)


class OpenAICompatibleProvider:
    """HTTP-based provider implementing OpenAI Chat Completions API with tool calling."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout_seconds: float = 60.0,
        http_client: Optional[httpx.Client] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model
        raw_base = api_base or settings.llm_api_base or "https://api.openai.com/v1"
        self.api_base = raw_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._custom_client = http_client

    def is_available(self) -> bool:
        """Return True if API key or base is configured."""
        # For local proxies (e.g. Ollama, vLLM), API key may not be required if api_base is set
        if self.api_key and self.api_key.strip():
            return True
        if self.api_base and "api.openai.com" not in self.api_base:
            return True
        return False

    def _get_client(self) -> httpx.Client:
        if self._custom_client:
            return self._custom_client
        return httpx.Client(timeout=self.timeout_seconds, follow_redirects=False)

    def _format_message(self, msg: ChatMessage) -> Dict[str, Any]:
        """Convert ChatMessage into OpenAI wire format."""
        formatted: Dict[str, Any] = {"role": msg.role.value}

        if msg.content is not None:
            formatted["content"] = msg.content
        else:
            formatted["content"] = ""

        if msg.name:
            formatted["name"] = msg.name

        if msg.tool_call_id:
            formatted["tool_call_id"] = msg.tool_call_id

        if msg.tool_calls:
            formatted["tool_calls"] = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in msg.tool_calls
            ]

        return formatted

    def _build_payload(self, request: LLMRequest) -> Dict[str, Any]:
        """Construct JSON payload for /v1/chat/completions."""
        payload: Dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [self._format_message(m) for m in request.messages],
            "temperature": request.temperature,
        }

        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]

        return payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Send chat completion request to endpoint and parse response."""
        if not self.is_available():
            raise LLMAuthenticationError(
                "LLM provider is not configured. Please set FORGE_LLM_API_KEY or configure a local provider."
            )

        endpoint = f"{self.api_base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "FORGE-Security-Auditor/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = self._build_payload(request)
        client = self._get_client()

        try:
            resp = client.post(endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"LLM request timed out after {self.timeout_seconds}s: {exc}")
        except httpx.RequestError as exc:
            sanitized_err = redact_secrets(str(exc))
            raise LLMUnavailableError(f"Network error connecting to LLM provider at {self.api_base}: {sanitized_err}")

        # Handle HTTP error status codes
        if resp.status_code == 401 or resp.status_code == 403:
            raise LLMAuthenticationError(
                f"LLM authentication failed (HTTP {resp.status_code}). Check your FORGE_LLM_API_KEY."
            )

        if resp.status_code == 429:
            raise LLMRateLimitError(
                f"LLM rate limit or quota exceeded (HTTP 429): {redact_secrets(resp.text[:200])}"
            )

        if resp.status_code == 400:
            err_text = resp.text.lower()
            if "context_length" in err_text or "token" in err_text or "maximum context" in err_text:
                raise LLMContextLengthError(
                    f"LLM context length exceeded: {redact_secrets(resp.text[:200])}"
                )
            raise LLMProviderError(f"LLM API bad request (HTTP 400): {redact_secrets(resp.text[:300])}")

        if resp.status_code >= 500:
            raise LLMUnavailableError(
                f"LLM provider service error (HTTP {resp.status_code}): {redact_secrets(resp.text[:200])}"
            )

        if resp.status_code != 200:
            raise LLMProviderError(
                f"Unexpected HTTP status {resp.status_code} from LLM provider: {redact_secrets(resp.text[:200])}"
            )

        # Parse valid JSON response
        try:
            data = resp.json()
        except Exception as exc:
            raise LLMProviderError(f"Malformed JSON returned by LLM provider: {exc}")

        choices = data.get("choices", [])
        if not choices:
            raise LLMProviderError("LLM response contained no choices")

        first_choice = choices[0]
        msg_data = first_choice.get("message", {})
        finish_reason = first_choice.get("finish_reason", "stop")
        content = msg_data.get("content")

        # Parse tool calls
        tool_calls: List[ToolCallRequest] = []
        raw_tool_calls = msg_data.get("tool_calls", [])
        if raw_tool_calls:
            for idx, tc in enumerate(raw_tool_calls):
                fn = tc.get("function", {})
                call_id = tc.get("id") or f"call_{idx}"
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")

                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {"raw_arguments": raw_args}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}

                tool_calls.append(
                    ToolCallRequest(
                        call_id=call_id,
                        name=name,
                        arguments=args,
                    )
                )

        usage = data.get("usage", {})
        p_tokens = usage.get("prompt_tokens", 0)
        c_tokens = usage.get("completion_tokens", 0)
        t_tokens = usage.get("total_tokens", p_tokens + c_tokens)
        resp_model = data.get("model", self.model)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=t_tokens,
            model=resp_model,
        )
