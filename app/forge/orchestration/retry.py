"""Phase 13 M2: Bounded Retry and Deterministic Failure Classification.

Classifies errors and stage outcomes into deterministic failure categories,
enforcing strict safety boundaries: authorization, integrity, policy, and
mutating repair failures are never retried automatically.
"""

from enum import Enum
import math
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 3
HARD_MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 0.5
MAX_BACKOFF_SECONDS = 30.0


class FailureCategory(str, Enum):
    """Deterministic failure taxonomy for autonomous engineering orchestration."""

    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    TIMEOUT = "timeout"
    INVALID_INPUT = "invalid_input"
    EVIDENCE_FAILURE = "evidence_failure"
    AUTHORIZATION_FAILURE = "authorization_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    PERMANENT_EXECUTION_FAILURE = "permanent_execution_failure"
    UNCERTAIN_MUTATION = "uncertain_mutation"


# Categories that are strictly non-retryable by automated systems
NON_RETRYABLE_CATEGORIES: Set[FailureCategory] = {
    FailureCategory.AUTHORIZATION_FAILURE,
    FailureCategory.INTEGRITY_FAILURE,
    FailureCategory.SANDBOX_UNAVAILABLE,
    FailureCategory.INVALID_INPUT,
    FailureCategory.EVIDENCE_FAILURE,
    FailureCategory.PERMANENT_EXECUTION_FAILURE,
    FailureCategory.UNCERTAIN_MUTATION,
}

RETRYABLE_CATEGORIES: Set[FailureCategory] = {
    FailureCategory.TRANSIENT_INFRASTRUCTURE,
    FailureCategory.RESOURCE_EXHAUSTION,
    FailureCategory.TIMEOUT,
}


class RetryDecision(BaseModel):
    """Outcome of evaluating a failure against the retry policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    should_retry: bool
    category: FailureCategory
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: str
    reconciliation_required: bool = False


class RetryAttemptRecord(BaseModel):
    """Audit record capturing a retry attempt and its outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int
    category: FailureCategory
    error_message: str
    delay_seconds: float
    timestamp: str


class FailureClassifier:
    """Classifies exceptions, error strings, and statuses into failure categories."""

    @staticmethod
    def classify_exception(exc: Exception, is_mutating: bool = False) -> FailureCategory:
        """Classify a Python exception deterministically."""
        exc_name = type(exc).__name__
        exc_str = str(exc).lower()

        # 1. Authorization / Approval Boundary Failures (Never Retry)
        if (
            isinstance(exc, PermissionError)
            or "approval" in exc_name.lower()
            or "permission" in exc_str
            or "access denied" in exc_str
            or "unauthorized" in exc_str
            or "forbidden" in exc_str
            or "approver" in exc_str
            or "authorization" in exc_str
            or "replay" in exc_str
            or "digest mismatch" in exc_str
        ):
            return FailureCategory.AUTHORIZATION_FAILURE

        # 2. Cryptographic / Audit Integrity Violations (Never Retry)
        if (
            "tamper" in exc_str
            or "integrity" in exc_str
            or "hash chain" in exc_str
            or "audit" in exc_name.lower()
        ):
            return FailureCategory.INTEGRITY_FAILURE

        # 3. Sandbox / Isolation Failures (Fail Secure, Never Host Fallback)
        if "sandbox" in exc_str or "docker" in exc_str:
            return FailureCategory.SANDBOX_UNAVAILABLE

        # 4. Uncertain Mutation States during repair
        if is_mutating:
            return FailureCategory.UNCERTAIN_MUTATION

        # 5. Transient Database / Infrastructure Errors (Retryable)
        if isinstance(exc, sqlite3.OperationalError):
            if "locked" in exc_str or "busy" in exc_str or "cannot start a transaction" in exc_str:
                return FailureCategory.TRANSIENT_INFRASTRUCTURE

        if isinstance(exc, (ConnectionError, BrokenPipeError, ConnectionResetError)):
            return FailureCategory.TRANSIENT_INFRASTRUCTURE

        # 6. Timeouts
        if isinstance(exc, TimeoutError) or "timeout" in exc_str or "timed out" in exc_str:
            return FailureCategory.TIMEOUT

        # 7. Resource Exhaustion
        if isinstance(exc, MemoryError) or "out of memory" in exc_str or "resource exhausted" in exc_str:
            return FailureCategory.RESOURCE_EXHAUSTION

        # 8. Schema / Input Validation
        if isinstance(exc, (ValueError, TypeError, KeyError)) or "validation error" in exc_str:
            return FailureCategory.INVALID_INPUT

        # Default to permanent execution failure
        return FailureCategory.PERMANENT_EXECUTION_FAILURE


class RetryPolicy:
    """Configurable, safety-bounded retry policy with exponential backoff."""

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_RETRIES,
        base_delay_seconds: float = DEFAULT_BASE_DELAY,
        max_delay_seconds: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        # Enforce safe hard bounds
        self.max_attempts = min(max(1, max_attempts), HARD_MAX_RETRIES)
        self.base_delay_seconds = max(0.01, base_delay_seconds)
        self.max_delay_seconds = min(max(1.0, max_delay_seconds), MAX_BACKOFF_SECONDS)

    def compute_backoff(self, attempt: int) -> float:
        """Compute exponential backoff with a hard maximum cap."""
        if attempt <= 1:
            return self.base_delay_seconds
        delay = self.base_delay_seconds * (2 ** (attempt - 1))
        return min(delay, self.max_delay_seconds)

    def evaluate(
        self,
        category: FailureCategory,
        attempt: int,
        is_mutating: bool = False,
        operation_name: str = "stage",
    ) -> RetryDecision:
        """Evaluate whether an operation failure can safely be retried."""
        delay = self.compute_backoff(attempt)

        # Mutating operations (e.g. repair patch execution) can NEVER be retried blindly
        if is_mutating:
            return RetryDecision(
                should_retry=False,
                category=FailureCategory.UNCERTAIN_MUTATION if category == FailureCategory.UNCERTAIN_MUTATION else category,
                attempt=attempt,
                max_attempts=self.max_attempts,
                delay_seconds=0.0,
                reason=(
                    f"Mutating operation '{operation_name}' cannot be retried automatically. "
                    "Workspace mutation status requires explicit reconciliation before any further action."
                ),
                reconciliation_required=True,
            )

        # Non-retryable categories fail closed immediately
        if category in NON_RETRYABLE_CATEGORIES:
            return RetryDecision(
                should_retry=False,
                category=category,
                attempt=attempt,
                max_attempts=self.max_attempts,
                delay_seconds=0.0,
                reason=f"Failure category '{category.value}' is non-retryable by policy.",
                reconciliation_required=(category == FailureCategory.UNCERTAIN_MUTATION),
            )

        # Enforce maximum attempt bound
        if attempt >= self.max_attempts:
            return RetryDecision(
                should_retry=False,
                category=category,
                attempt=attempt,
                max_attempts=self.max_attempts,
                delay_seconds=0.0,
                reason=f"Exhausted maximum retry attempts ({attempt}/{self.max_attempts}).",
                reconciliation_required=False,
            )

        # Retryable failure within bounds
        return RetryDecision(
            should_retry=True,
            category=category,
            attempt=attempt,
            max_attempts=self.max_attempts,
            delay_seconds=delay,
            reason=f"Transient failure '{category.value}' is retryable (attempt {attempt}/{self.max_attempts}).",
            reconciliation_required=False,
        )
