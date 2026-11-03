"""Phase 9 Git Publication Architecture — GitHub Provider Implementation.

Integrates with the GitHub REST API for repository verification, branch checking,
and pull request management with strict token security and secret redaction.
"""

from datetime import datetime, timezone
import os
import re
from typing import Any, Dict, List, Optional
import uuid

import httpx

from forge.config import redact_secrets
from forge.publication.failures import (
    DuplicatePullRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    PullRequestCreationError,
    PullRequestVerificationError,
    RemoteAuthenticationError,
    RemotePublicationError,
    RemoteRepositoryVerificationError,
)
from forge.publication.models import (
    PullRequestDetails,
    PullRequestRecord,
    RemoteRepositorySpec,
)
from forge.publication.providers.base import (
    PullRequestProviderProtocol,
    retry_idempotent_operation,
)


class GitHubPullRequestProvider(PullRequestProviderProtocol):
    """GitHub implementation of PullRequestProviderProtocol."""

    def __init__(
        self,
        token: Optional[str] = None,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 30.0,
        client: Optional[httpx.Client] = None,
    ):
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds
        # Resolve token from arguments or standard environment variables
        self._token = token or os.environ.get("FORGE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self._client = client

    def __repr__(self) -> str:
        # Never expose token or secret credentials in string representations
        token_status = "present" if self._token else "missing"
        return f"<GitHubPullRequestProvider api_base_url='{self._api_base_url}' token='{token_status}'>"

    def get_provider_name(self) -> str:
        return "github"

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "FORGE-Autonomous-Publication-Engine/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.Client(headers=headers, timeout=self._timeout)

    def _sanitize_error_message(self, message: str) -> str:
        """Sanitize error messages ensuring zero credential leakage."""
        clean = redact_secrets(message)
        if self._token:
            clean = clean.replace(self._token, "[REDACTED_TOKEN]")
        return clean

    def validate_credentials(self) -> bool:
        """Verify that GitHub credentials are valid and authenticated."""
        if not self._token:
            raise RemoteAuthenticationError(
                "Missing GitHub access credentials. Please set GITHUB_TOKEN or FORGE_GITHUB_TOKEN in your environment."
            )

        client = self._get_client()
        should_close = self._client is None
        try:
            resp = client.get(f"{self._api_base_url}/user")
            if resp.status_code == 200:
                return True
            if resp.status_code in (401, 403):
                reason = resp.json().get("message", "Bad credentials") if resp.headers.get("content-type", "").startswith("application/json") else resp.text
                raise RemoteAuthenticationError(
                    f"GitHub authentication failed (HTTP {resp.status_code}): {self._sanitize_error_message(reason)}"
                )
            if resp.status_code == 429:
                raise ProviderRateLimitError("GitHub API rate limit exceeded during credential validation.")
            raise RemoteAuthenticationError(f"Unexpected response validating credentials: HTTP {resp.status_code}")
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"GitHub credential validation timed out: {e}") from e
        except (RemoteAuthenticationError, ProviderRateLimitError, ProviderTimeoutError):
            raise
        except Exception as e:
            raise RemoteAuthenticationError(
                f"Failed to communicate with GitHub API: {self._sanitize_error_message(str(e))}"
            ) from e
        finally:
            if should_close:
                client.close()

    def verify_repository(self, owner: str, repo: str) -> RemoteRepositorySpec:
        """Verify repository existence, canonical path, and permissions on GitHub."""
        self.validate_credentials()
        client = self._get_client()
        should_close = self._client is None
        try:
            resp = client.get(f"{self._api_base_url}/repos/{owner}/{repo}")
            if resp.status_code == 404:
                raise RemoteRepositoryVerificationError(
                    f"Target repository '{owner}/{repo}' was not found on GitHub or is inaccessible."
                )
            if resp.status_code in (401, 403):
                reason = resp.json().get("message", "Access forbidden") if "json" in resp.headers.get("content-type", "") else resp.text
                raise RemoteAuthenticationError(
                    f"Access forbidden to repository '{owner}/{repo}': {self._sanitize_error_message(reason)}"
                )
            if resp.status_code == 429:
                raise ProviderRateLimitError(f"GitHub rate limit exceeded querying repository '{owner}/{repo}'.")
            if resp.status_code != 200:
                raise RemoteRepositoryVerificationError(
                    f"Failed to verify repository '{owner}/{repo}': HTTP {resp.status_code}"
                )

            data = resp.json()
            permissions = data.get("permissions", {})
            can_push = permissions.get("push", True)  # default True if not explicitly reported

            # Canonical remote URL
            remote_url = data.get("clone_url", f"https://github.com/{owner}/{repo}.git")

            return RemoteRepositorySpec(
                provider="github",
                owner=data.get("owner", {}).get("login", owner),
                repository=data.get("name", repo),
                default_branch=data.get("default_branch", "main"),
                remote_url=remote_url,
                is_private=data.get("private", False),
                can_push=can_push,
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Timeout verifying repository '{owner}/{repo}': {e}") from e
        finally:
            if should_close:
                client.close()

    def check_branch_exists(self, owner: str, repo: str, branch: str) -> Optional[str]:
        """Check if branch exists on remote. Returns HEAD commit SHA if present, None otherwise."""
        self.validate_credentials()
        client = self._get_client()
        should_close = self._client is None
        try:
            # Clean branch name
            ref_branch = branch.replace("refs/heads/", "")
            resp = client.get(f"{self._api_base_url}/repos/{owner}/{repo}/branches/{ref_branch}")
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                data = resp.json()
                return data.get("commit", {}).get("sha")
            if resp.status_code == 429:
                raise ProviderRateLimitError(f"Rate limit checking branch '{branch}'.")
            raise RemotePublicationError(f"Unexpected error checking branch '{branch}': HTTP {resp.status_code}")
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Timeout checking remote branch '{branch}': {e}") from e
        finally:
            if should_close:
                client.close()

    def check_existing_pr(
        self,
        owner: str,
        repo: str,
        head_branch: str,
        base_branch: str,
    ) -> Optional[PullRequestRecord]:
        """Query if an open pull request already exists for the given head and base branches."""
        self.validate_credentials()
        client = self._get_client()
        should_close = self._client is None
        try:
            # GitHub head query format: owner:branch_name
            clean_head = head_branch.replace("refs/heads/", "")
            head_param = f"{owner}:{clean_head}"
            clean_base = base_branch.replace("refs/heads/", "")

            resp = client.get(
                f"{self._api_base_url}/repos/{owner}/{repo}/pulls",
                params={"state": "open", "head": head_param, "base": clean_base},
            )
            if resp.status_code == 200:
                prs = resp.json()
                if prs and len(prs) > 0:
                    pr_data = prs[0]
                    return PullRequestRecord(
                        pr_record_id=f"pr_{uuid.uuid4().hex[:12]}",
                        repair_id="unknown",
                        publication_id="unknown",
                        plan_id="unknown",
                        decision_id="unknown",
                        staged_commit_id="unknown",
                        remote_branch_id="unknown",
                        provider="github",
                        repository_owner=owner,
                        repository_name=repo,
                        target_branch=clean_base,
                        source_branch=clean_head,
                        pr_number=pr_data["number"],
                        pr_url=pr_data["html_url"],
                        pr_title=pr_data["title"],
                        pr_body=pr_data.get("body", "") or "",
                        pr_state=pr_data.get("state", "open"),
                        is_draft=pr_data.get("draft", False),
                        created_at=datetime.fromisoformat(pr_data["created_at"].replace("Z", "+00:00")),
                        verified_at=datetime.now(timezone.utc),
                        status="verified",
                    )
                return None
            if resp.status_code == 429:
                raise ProviderRateLimitError(f"Rate limit checking pull requests for '{owner}/{repo}'.")
            return None
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Timeout checking existing pull requests: {e}") from e
        finally:
            if should_close:
                client.close()

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
        """Create an upstream pull request on GitHub."""
        self.validate_credentials()

        # Idempotency check: verify whether PR already exists
        existing = self.check_existing_pr(
            owner=owner,
            repo=repo,
            head_branch=details.source_branch,
            base_branch=details.target_branch,
        )
        if existing:
            # Populate linked IDs
            existing.repair_id = repair_id
            existing.publication_id = publication_id
            existing.plan_id = plan_id
            existing.decision_id = decision_id
            existing.staged_commit_id = staged_commit_id
            existing.remote_branch_id = remote_branch_id
            raise DuplicatePullRequestError(
                f"An open pull request already exists for branch '{details.source_branch}' on '{owner}/{repo}': "
                f"PR #{existing.pr_number} ({existing.pr_url})"
            )

        client = self._get_client()
        should_close = self._client is None
        try:
            payload = {
                "title": details.title,
                "body": details.body,
                "head": details.source_branch,
                "base": details.target_branch,
                "draft": details.draft,
            }

            resp = client.post(
                f"{self._api_base_url}/repos/{owner}/{repo}/pulls",
                json=payload,
            )

            if resp.status_code == 201:
                pr_data = resp.json()
                return PullRequestRecord(
                    pr_record_id=f"pr_{uuid.uuid4().hex[:12]}",
                    repair_id=repair_id,
                    publication_id=publication_id,
                    plan_id=plan_id,
                    decision_id=decision_id,
                    staged_commit_id=staged_commit_id,
                    remote_branch_id=remote_branch_id,
                    provider="github",
                    repository_owner=owner,
                    repository_name=repo,
                    target_branch=details.target_branch,
                    source_branch=details.source_branch,
                    pr_number=pr_data["number"],
                    pr_url=pr_data["html_url"],
                    pr_title=pr_data["title"],
                    pr_body=pr_data.get("body", "") or "",
                    pr_state=pr_data.get("state", "open"),
                    is_draft=pr_data.get("draft", False),
                    created_at=datetime.fromisoformat(pr_data["created_at"].replace("Z", "+00:00")),
                    verified_at=datetime.now(timezone.utc),
                    status="created",
                )

            # Error handling
            if resp.status_code == 422:
                err_data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
                err_msg = err_data.get("message", "Validation Failed")
                errors = err_data.get("errors", [])
                err_details = "; ".join([str(e.get("message", e)) for e in errors]) if errors else ""
                combined = f"{err_msg} - {err_details}" if err_details else err_msg

                if "already exists" in combined.lower():
                    # Lost-response recovery: check existing PR
                    recovered = self.check_existing_pr(owner, repo, details.source_branch, details.target_branch)
                    if recovered:
                        recovered.repair_id = repair_id
                        recovered.publication_id = publication_id
                        recovered.plan_id = plan_id
                        recovered.decision_id = decision_id
                        recovered.staged_commit_id = staged_commit_id
                        recovered.remote_branch_id = remote_branch_id
                        return recovered

                raise PullRequestCreationError(
                    f"GitHub rejected pull request creation (HTTP 422): {self._sanitize_error_message(combined)}"
                )

            if resp.status_code in (401, 403):
                raise RemoteAuthenticationError(
                    f"GitHub authentication error creating PR (HTTP {resp.status_code}): {self._sanitize_error_message(resp.text)}"
                )

            if resp.status_code == 429:
                raise ProviderRateLimitError("GitHub API rate limit exceeded while creating pull request.")

            raise PullRequestCreationError(
                f"Failed to create pull request on GitHub (HTTP {resp.status_code}): {self._sanitize_error_message(resp.text)}"
            )

        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Timeout creating pull request on GitHub: {e}") from e
        finally:
            if should_close:
                client.close()

    def get_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> Optional[PullRequestRecord]:
        """Fetch pull request metadata from GitHub by number."""
        self.validate_credentials()
        client = self._get_client()
        should_close = self._client is None
        try:
            resp = client.get(f"{self._api_base_url}/repos/{owner}/{repo}/pulls/{pr_number}")
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                pr_data = resp.json()
                return PullRequestRecord(
                    pr_record_id=f"pr_{uuid.uuid4().hex[:12]}",
                    repair_id="unknown",
                    publication_id="unknown",
                    plan_id="unknown",
                    decision_id="unknown",
                    staged_commit_id="unknown",
                    remote_branch_id="unknown",
                    provider="github",
                    repository_owner=owner,
                    repository_name=repo,
                    target_branch=pr_data.get("base", {}).get("ref", "main"),
                    source_branch=pr_data.get("head", {}).get("ref", ""),
                    pr_number=pr_data["number"],
                    pr_url=pr_data["html_url"],
                    pr_title=pr_data["title"],
                    pr_body=pr_data.get("body", "") or "",
                    pr_state=pr_data.get("state", "open"),
                    is_draft=pr_data.get("draft", False),
                    created_at=datetime.fromisoformat(pr_data["created_at"].replace("Z", "+00:00")),
                    verified_at=datetime.now(timezone.utc),
                    status="verified",
                )
            if resp.status_code == 429:
                raise ProviderRateLimitError(f"Rate limit fetching PR #{pr_number}.")
            raise PullRequestVerificationError(
                f"Failed to fetch PR #{pr_number} from GitHub: HTTP {resp.status_code}"
            )
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Timeout fetching PR #{pr_number}: {e}") from e
        finally:
            if should_close:
                client.close()
