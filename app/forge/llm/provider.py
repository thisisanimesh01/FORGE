"""Provider-independent LLM protocol and exception definitions."""

from typing import Protocol, runtime_checkable
from forge.llm.models import LLMRequest, LLMResponse


class LLMProviderError(Exception):
    """Base error for LLM provider failures."""
    pass


class LLMAuthenticationError(LLMProviderError):
    """Raised when provider API key or credentials are invalid."""
    pass


class LLMRateLimitError(LLMProviderError):
    """Raised when rate limits or quotas are exceeded."""
    pass


class LLMContextLengthError(LLMProviderError):
    """Raised when request exceeds provider context window."""
    pass


class LLMUnavailableError(LLMProviderError):
    """Raised when provider endpoint is unreachable or down."""
    pass


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol defining the interface for an LLM provider."""

    def is_available(self) -> bool:
        """Return True if provider is configured and reachable."""
        ...

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate a response given an LLMRequest.

        Raises:
            LLMAuthenticationError
            LLMRateLimitError
            LLMContextLengthError
            LLMUnavailableError
            LLMProviderError
        """
        ...
