"""Sandbox interface protocol definition and base abstractions."""

from typing import Protocol, Tuple, runtime_checkable
from forge.sandbox.models import SandboxRequest, SandboxResult


@runtime_checkable
class Sandbox(Protocol):
    """Protocol defining the sandbox execution interface."""

    def is_available(self) -> bool:
        """Return True if an isolated container execution boundary is available."""
        ...

    def check_health(self) -> Tuple[bool, str]:
        """Diagnose runtime health.

        Returns:
            Tuple of (healthy_bool, diagnostic_message).
        """
        ...

    def run(self, request: SandboxRequest) -> SandboxResult:
        """Execute a validated command inside the sandbox isolation boundary.

        If isolation is unavailable or policy is violated, fails closed without
        executing code on the host.
        """
        ...
