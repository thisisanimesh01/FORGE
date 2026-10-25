"""Deterministic Fake LLM provider for tests and offline mock auditing."""

from typing import Callable, List, Optional, Union
from forge.llm.models import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCallRequest,
)
from forge.llm.provider import LLMProvider


class FakeLLMProvider:
    """Simulates LLM provider behavior deterministically without network calls.

    Can accept:
    - A sequence of pre-scripted `LLMResponse` objects.
    - A custom generator function `(LLMRequest) -> LLMResponse`.
    - Default behavior: inspects messages and returns a synthesized audit summary.
    """

    def __init__(
        self,
        scripted_responses: Optional[List[LLMResponse]] = None,
        response_generator: Optional[Callable[[LLMRequest], LLMResponse]] = None,
        model_name: str = "forge-fake-model",
    ):
        self.scripted_responses = list(scripted_responses) if scripted_responses else []
        self.response_generator = response_generator
        self.model_name = model_name
        self.call_history: List[LLMRequest] = []

    def is_available(self) -> bool:
        """Fake provider is always available."""
        return True

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate the next pre-scripted or rule-based response."""
        self.call_history.append(request)

        if self.scripted_responses:
            return self.scripted_responses.pop(0)

        if self.response_generator:
            return self.response_generator(request)

        # Default fallback response
        # If the last message was a tool result, produce a conclusion
        last_message = request.messages[-1] if request.messages else None
        if last_message and last_message.role == MessageRole.TOOL:
            return LLMResponse(
                content=f"Investigation completed based on observed tool results for {last_message.name}.",
                finish_reason="stop",
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                model=self.model_name,
            )

        # By default, request repository map if tools are available
        has_repo_map_tool = any(t.name == "get_repository_map" for t in request.tools)
        if has_repo_map_tool:
            return LLMResponse(
                content="I will begin by inspecting the repository architecture map.",
                tool_calls=[
                    ToolCallRequest(
                        call_id="call_fake_001",
                        name="get_repository_map",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
                prompt_tokens=50,
                completion_tokens=15,
                total_tokens=65,
                model=self.model_name,
            )

        return LLMResponse(
            content="Static code inspection completed. No defects identified in provided context.",
            finish_reason="stop",
            prompt_tokens=50,
            completion_tokens=15,
            total_tokens=65,
            model=self.model_name,
        )
