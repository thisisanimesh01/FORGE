"""OpenAI-compatible LLM provider implementation using standard httpx."""

import json
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx

from forge.config import redact_secrets, get_settings
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
    ToolDefinition,
)
from forge.llm.provider import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMRateLimitError,
    LLMUnavailableError,
)


def validate_tool_arguments(tool_def: ToolDefinition, args: Any) -> Tuple[bool, Optional[str]]:
    """Validate parsed tool arguments against the tool's JSON schema parameters.

    Returns:
        (is_valid, error_message_if_invalid)
    """
    if not isinstance(args, dict):
        return False, f"Tool arguments for '{tool_def.name}' must be a JSON object, got {type(args).__name__}"

    params = tool_def.parameters or {}
    required = params.get("required", [])
    for req in required:
        if req not in args or args[req] is None:
            return False, f"Missing required parameter '{req}' for tool '{tool_def.name}'"

    props = params.get("properties", {})
    for key, val in list(args.items()):
        if key in props and isinstance(props[key], dict):
            prop_spec = props[key]
            expected_type = prop_spec.get("type")
            if expected_type == "string":
                if not isinstance(val, str):
                    return False, f"Parameter '{key}' for tool '{tool_def.name}' must be a string, got {type(val).__name__}"
            elif expected_type == "integer":
                if isinstance(val, bool):
                    return False, f"Parameter '{key}' for tool '{tool_def.name}' must be an integer, got bool"
                if isinstance(val, str) and val.strip().lstrip("-").isdigit():
                    args[key] = int(val.strip())
                elif not isinstance(val, int):
                    return False, f"Parameter '{key}' for tool '{tool_def.name}' must be an integer, got {type(val).__name__}"
            elif expected_type == "boolean":
                if not isinstance(val, bool):
                    return False, f"Parameter '{key}' for tool '{tool_def.name}' must be a boolean, got {type(val).__name__}"
            elif expected_type == "array":
                if not isinstance(val, list):
                    return False, f"Parameter '{key}' for tool '{tool_def.name}' must be an array, got {type(val).__name__}"
            elif expected_type == "object":
                if not isinstance(val, dict):
                    return False, f"Parameter '{key}' for tool '{tool_def.name}' must be an object, got {type(val).__name__}"

            if "enum" in prop_spec and args[key] not in prop_spec["enum"]:
                return False, f"Parameter '{key}' value {args[key]!r} is not an allowed enum value for '{tool_def.name}' (allowed: {prop_spec['enum']})"

    return True, None


def extract_content_tool_calls(
    content: Optional[str],
    authorized_tools: List[ToolDefinition],
) -> Tuple[List[ToolCallRequest], Optional[str]]:
    """Attempt safe extraction of tool calls from message text content when structured tool calls are absent.

    Supports:
    - JSON inside <tool_call>...</tool_call> tags
    - JSON inside Markdown code fences (```json ... ```)
    - Direct JSON objects or outer matching { ... }

    Only accepts tool names present in authorized_tools.
    Validates arguments against the tool's parameter schema.
    Rejects malformed JSON, unknown tool names, and malformed arguments.
    Does not interpret arbitrary JSON or ordinary explanatory prose as tool calls.

    Returns:
        (tool_calls, extraction_error)
    """
    if not content or not content.strip() or not authorized_tools:
        return [], None

    tool_map = {t.name: t for t in authorized_tools}
    cleaned = content.strip()
    extracted_calls: List[ToolCallRequest] = []
    extraction_errors: List[str] = []

    def _try_parse_candidate(raw_candidate: str, source_label: str) -> bool:
        """Attempt to parse and validate a raw JSON string as a tool call candidate."""
        nonlocal extracted_calls, extraction_errors
        try:
            parsed = json.loads(raw_candidate)
        except json.JSONDecodeError as exc:
            if source_label == "tag":
                extraction_errors.append(f"Malformed JSON in <tool_call> tag: {exc}")
            return False

        candidates = parsed if isinstance(parsed, list) else [parsed]
        found_any = False

        for item in candidates:
            if not isinstance(item, dict):
                continue

            tool_name = None
            tool_args = None

            if "name" in item and isinstance(item["name"], str):
                tool_name = item["name"]
                tool_args = item.get("arguments", item.get("parameters", {}))
            elif "tool" in item and isinstance(item["tool"], str):
                tool_name = item["tool"]
                tool_args = item.get("arguments", item.get("parameters", {}))
            elif "function" in item and isinstance(item["function"], dict):
                fn = item["function"]
                if "name" in fn and isinstance(fn["name"], str):
                    tool_name = fn["name"]
                    tool_args = fn.get("arguments", fn.get("parameters", {}))
            elif "type" in item and item.get("type") == "function" and isinstance(item.get("function"), dict):
                fn = item["function"]
                if "name" in fn and isinstance(fn["name"], str):
                    tool_name = fn["name"]
                    tool_args = fn.get("arguments", fn.get("parameters", {}))

            if not tool_name:
                continue

            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except json.JSONDecodeError as exc:
                    extraction_errors.append(f"Malformed arguments JSON for tool '{tool_name}': {exc}")
                    continue
            elif tool_args is None:
                tool_args = {}

            if tool_name not in tool_map:
                extraction_errors.append(
                    f"Tool '{tool_name}' is not in authorized tools: {sorted(list(tool_map.keys()))}"
                )
                continue

            tool_def = tool_map[tool_name]
            is_valid, arg_err = validate_tool_arguments(tool_def, tool_args)
            if not is_valid:
                extraction_errors.append(arg_err or f"Invalid arguments for tool '{tool_name}'")
                continue

            extracted_calls.append(
                ToolCallRequest(
                    call_id=f"call_text_{len(extracted_calls) + 1}",
                    name=tool_name,
                    arguments=tool_args,
                )
            )
            found_any = True

        return found_any

    # 1. Check for <tool_call> tags
    tag_pattern = re.compile(r"<tool_call>([\s\S]*?)</tool_call>", re.IGNORECASE)
    tag_matches = tag_pattern.findall(cleaned)
    if tag_matches:
        for block in tag_matches:
            _try_parse_candidate(block.strip(), source_label="tag")
        if extracted_calls:
            return extracted_calls, None
        if extraction_errors:
            return [], extraction_errors[0]

    # 2. Check for markdown code fences
    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    fence_matches = fence_pattern.findall(cleaned)
    if fence_matches:
        for block in fence_matches:
            _try_parse_candidate(block.strip(), source_label="fence")
        if extracted_calls:
            return extracted_calls, None

    # 3. Check for direct JSON or outermost { ... } / [ ... ]
    if cleaned.startswith("{") or cleaned.startswith("["):
        _try_parse_candidate(cleaned, source_label="direct")
        if extracted_calls:
            return extracted_calls, None

    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate_slice = cleaned[first_brace : last_brace + 1]
        _try_parse_candidate(candidate_slice, source_label="braces")
        if extracted_calls:
            return extracted_calls, None

    first_bracket = cleaned.find("[")
    last_bracket = cleaned.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        candidate_slice = cleaned[first_bracket : last_bracket + 1]
        _try_parse_candidate(candidate_slice, source_label="brackets")
        if extracted_calls:
            return extracted_calls, None

    err = extraction_errors[0] if extraction_errors else None
    return [], err


class OpenAICompatibleProvider:
    """HTTP-based provider implementing OpenAI Chat Completions API with tool calling."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 1,
        http_client: Optional[httpx.Client] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else ""
        self.model = model or settings.llm_model
        raw_base = api_base or settings.llm_api_base or ""
        self.api_base = raw_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self._custom_client = http_client

    def is_available(self) -> bool:
        """Return True if API key or local/loopback endpoint is configured."""
        if self.api_key and self.api_key.strip():
            return True
        if self.api_base and ("localhost" in self.api_base or "127.0.0.1" in self.api_base):
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

        if request.response_format:
            payload["response_format"] = request.response_format

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
                "LLM provider is not configured. Please set provider credentials or configure a local provider."
            )

        if not self.api_base:
            raise LLMUnavailableError(
                "LLM endpoint base URL is not configured. Set api_base or configure a valid provider."
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

        total_attempts = 1 + self.max_retries
        resp: Optional[httpx.Response] = None

        for attempt in range(total_attempts):
            try:
                resp = client.post(endpoint, json=payload, headers=headers)
                if resp.status_code >= 500 and attempt < total_attempts - 1:
                    continue
                break
            except httpx.TimeoutException as exc:
                if attempt == total_attempts - 1:
                    raise LLMUnavailableError(f"LLM request timed out after {self.timeout_seconds}s: {exc}")
            except httpx.RequestError as exc:
                if attempt == total_attempts - 1:
                    sanitized_err = redact_secrets(str(exc))
                    raise LLMUnavailableError(f"Network error connecting to LLM provider at {self.api_base}: {sanitized_err}")

        assert resp is not None  # Guard for type-checker

        # Handle HTTP error status codes
        if resp.status_code == 401 or resp.status_code == 403:
            raise LLMAuthenticationError(
                f"LLM authentication failed (HTTP {resp.status_code}). Check provider credentials."
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
            if "response_format" in err_text or "json_schema" in err_text:
                # Provider rejected structured output specification; attempt graceful downgrade on the same provider
                rf = payload.get("response_format")
                if isinstance(rf, dict) and rf.get("type") == "json_schema":
                    payload["response_format"] = {"type": "json_object"}
                    try:
                        fallback_resp = client.post(endpoint, json=payload, headers=headers)
                        if fallback_resp.status_code == 200:
                            resp = fallback_resp
                        elif fallback_resp.status_code == 400 and "response_format" in fallback_resp.text.lower():
                            del payload["response_format"]
                            no_rf_resp = client.post(endpoint, json=payload, headers=headers)
                            if no_rf_resp.status_code == 200:
                                resp = no_rf_resp
                    except Exception:
                        pass
                elif "response_format" in payload:
                    del payload["response_format"]
                    try:
                        no_rf_resp = client.post(endpoint, json=payload, headers=headers)
                        if no_rf_resp.status_code == 200:
                            resp = no_rf_resp
                    except Exception:
                        pass

            if resp.status_code == 400:
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
        extraction_error: Optional[str] = None

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
        elif content and request.tools:
            # Fallback: attempt safe content-based tool extraction
            extracted, extraction_error = extract_content_tool_calls(content, request.tools)
            if extracted:
                tool_calls = extracted
                finish_reason = "tool_calls"

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
            extraction_error=extraction_error,
        )
