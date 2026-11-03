"""Phase 9 Git Publication Architecture — Provider Base & Protocols.

Defines the abstract interface, capability detection, and retry policies for
upstream Git hosting providers (e.g. GitHub, GitLab).
"""

from abc import ABC, abstractmethod
import logging
import random
import time
from typing import Any, Callable, Dict, Optional, Protocol, TypeVar, runtime_checkable

from forge.config import redact_secrets
from forge.publication.failures import (
    ProviderRateLimitError,
    ProviderTimeoutError,
    RemoteAuthenticationError,
    RemotePublicationError,
)
from forge.publication.models import (
    PullRequestDetails,
    PullRequestRecord,
    RemoteRepositorySpec,
)

logger = logging.getLogger("forge.publication.providers")

T = TypeVar("T")


@runtime_checkable
class PullRequestProviderProtocol(Protocol):
    """Runtime-checkable protocol for Git hosting provider integrations."""

    def get_provider_name(self) -> str:
        """Return canonical provider name (e.g. 'github', 'gitlab')."""
        ...

    def validate_credentials(self) -> bool:
        """Verify that provider credentials are present, valid, and authenticated."""
        ...

    def verify_repository(self, owner: str, repo: str) -> RemoteRepositorySpec:
        """Verify repository existence, canonical path, and permissions on remote."""
        ...

    def check_branch_exists(self, owner: str, repo: str, branch: str) -> Optional[str]:
        """Check if branch exists on remote. Returns commit SHA if present, None otherwise."""
        ...

    def check_existing_pr(
        self,
        owner: str,
        repo: str,
        head_branch: str,
        base_branch: str,
    ) -> Optional[PullRequestRecord]:
        """Query if an open pull request already exists for the given head and base branches."""
        ...

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        details: PullRequestDetails,
        repair_id: str,
        publication_id: str,
        plan_id: str,
        decision_id: str,
        staged_commit_id: str,
        remote_branch_id: str,
    ) -> PullRequestRecord:
        """Create an upstream pull request on the remote provider."""
        ...

    def get_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> Optional[PullRequestRecord]:
        """Fetch pull request metadata from the remote provider by number."""
        ...


def retry_idempotent_operation(
    operation: Callable[[], T],
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    operation_name: str = "provider_operation",
) -> T:
    """Execute an idempotent provider operation with exponential backoff retry.

    Retries only on transient network failures, timeouts, and rate limits.
    Fails closed immediately on authentication or permission errors.
    """
    delay = initial_delay
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except RemoteAuthenticationError:
            # Never retry authentication failures
            raise
        except ProviderRateLimitError as e:
            last_error = e
            if attempt == max_retries:
                raise
            sleep_time = delay * (1.0 + (random.random() * 0.2 if jitter else 0.0))
            time.sleep(sleep_time)
            delay *= backoff_factor
        except (ProviderTimeoutError, TimeoutError) as e:
            last_error = e
            if attempt == max_retries:
                raise ProviderTimeoutError(f"Operation '{operation_name}' timed out after {max_retries} attempts.") from e
            sleep_time = delay * (1.0 + (random.random() * 0.2 if jitter else 0.0))
            time.sleep(sleep_time)
            delay *= backoff_factor
        except Exception as e:
            # If not a known transient error, raise immediately
            sanitized_msg = redact_secrets(str(e))
            raise RemotePublicationError(f"Operation '{operation_name}' failed: {sanitized_msg}") from e

    raise last_error or RemotePublicationError(f"Operation '{operation_name}' exhausted {max_retries} retries.")
