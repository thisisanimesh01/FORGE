"""Pydantic data models for LLM interactions and tool calling."""

from enum import Enum
import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class MessageRole(str, Enum):
    """Role in a chat conversation."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCallRequest(BaseModel):
    """A tool invocation requested by an LLM."""
    model_config = ConfigDict(frozen=True)

    call_id: str = Field(description="Unique tool call identifier (e.g. from provider)")
    name: str = Field(description="Tool name to execute")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Parsed tool arguments")

    def to_hashable_key(self) -> str:
        """String representation used for loop and deduplication detection."""
        return f"{self.name}:{json.dumps(self.arguments, sort_keys=True)}"


class ChatMessage(BaseModel):
    """An individual message in the LLM conversation history."""
    role: MessageRole = Field(description="Role of the message sender")
    content: Optional[str] = Field(default=None, description="Textual content of the message")
    name: Optional[str] = Field(default=None, description="Name of the tool for tool response messages")
    tool_call_id: Optional[str] = Field(default=None, description="Associated tool_call_id for tool responses")
    tool_calls: Optional[List[ToolCallRequest]] = Field(
        default=None,
        description="Tool invocations requested by the model",
    )


class ToolDefinition(BaseModel):
    """JSON Schema definition for a tool callable by the model."""
    name: str = Field(description="Unique name of the tool")
    description: str = Field(description="Detailed explanation of what the tool does")
    parameters: Dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        description="JSON Schema object describing the tool parameters",
    )


class LLMRequest(BaseModel):
    """Standardized request sent to an LLM provider."""
    messages: List[ChatMessage] = Field(description="Conversation history")
    tools: List[ToolDefinition] = Field(default_factory=list, description="Available tools")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    model: Optional[str] = Field(default=None, description="Override model identifier")
    response_format: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured output format specification (e.g. json_object or json_schema)",
    )


class LLMResponse(BaseModel):
    """Standardized response received from an LLM provider."""
    content: Optional[str] = Field(default=None, description="Assistant message text")
    tool_calls: List[ToolCallRequest] = Field(
        default_factory=list,
        description="Requested tool invocations",
    )
    finish_reason: str = Field(default="stop", description="Stop condition: stop, tool_calls, length, error")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    model: str = Field(default="", description="Actual model that responded")
    extraction_error: Optional[str] = Field(
        default=None,
        description="Diagnostic explanation if tool call extraction from message content failed",
    )
