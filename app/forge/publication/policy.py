"""Phase 9 Git Publication Architecture — Publication Policy.

Defines the default-deny Git publication policy and invariant enforcement rules.
"""

import fnmatch
import re
from typing import List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field

from forge.config import redact_secrets
from forge.publication.failures import (
    ProtectedBranchViolationError,
    PublicationPolicyViolationError,
    SecretDetectedError,
)
from forge.publication.models import PublicationAction


# Common secret patterns for pre-publication scanning
SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),                 # OpenAI / generic secret keys
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                   # GitHub personal access tokens
    re.compile(r"gho_[a-zA-Z0-9]{36}"),                   # GitHub OAuth tokens
    re.compile(r"glpat-[a-zA-Z0-9_-]{20,}"),              # GitLab personal access tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                      # AWS Access Key ID
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), # Private keys
]


class GitPublicationPolicy(BaseModel):
    """Enforceable Git publication policy enforcing default-deny behavior and fail-closed safety."""

    model_config = ConfigDict(extra="forbid")

    allowed_actions: Set[PublicationAction] = Field(
        default_factory=lambda: {
            PublicationAction.EXPORT_PATCH,
            PublicationAction.STAGE_BRANCH,
            PublicationAction.CREATE_PR,
        },
        description="Set of permitted publication actions."
    )
    permitted_branch_prefix: str = Field(
        default="forge/",
        description="Mandatory prefix for all generated or staged branches."
    )
    branch_name_pattern: str = Field(
        default=r"^forge/[a-zA-Z0-9][a-zA-Z0-9_\-\./]*$",
        description="Strict regex defining valid branch names."
    )
    max_branch_name_length: int = Field(
        default=100,
        description="Maximum permissible branch name length."
    )
    protected_branches: Set[str] = Field(
        default_factory=lambda: {"main", "master", "develop", "production", "trunk"},
        description="Set of exact protected branch names that can never be directly targeted or overwritten."
    )
    protected_branch_patterns: List[str] = Field(
        default_factory=lambda: ["release/*", "v[0-9]+*", "prod/*", "hotfix/*"],
        description="Glob patterns defining protected branches."
    )
    require_clean_working_tree: bool = Field(
        default=True,
        description="Mandates zero uncommitted or untracked changes before any publication action."
    )
    require_exact_base_sha: bool = Field(
        default=True,
        description="Mandates that repository HEAD matches the exact base commit SHA in the approval."
    )
    require_exact_patch_digest: bool = Field(
        default=True,
        description="Mandates exact SHA-256 patch content digest equality."
    )
    require_m6_approval: bool = Field(
        default=True,
        description="Mandates an active, unexpired, untampered Phase 8 Milestone 6 approval."
    )
    allow_force_push: bool = Field(
        default=False,
        description="MUST ALWAYS BE FALSE. Force pushes are unconditionally forbidden."
    )
    allow_history_rewrite: bool = Field(
        default=False,
        description="MUST ALWAYS BE FALSE. Rebase and commit amend are unconditionally forbidden."
    )
    allow_automatic_merge: bool = Field(
        default=False,
        description="MUST ALWAYS BE FALSE. Automatic merges are unconditionally forbidden."
    )
    allow_software_deployment: bool = Field(
        default=False,
        description="MUST ALWAYS BE FALSE. Deployments are unconditionally forbidden."
    )
    allowed_remote_schemes: Set[str] = Field(
        default_factory=lambda: {"https", "ssh", "file"},
        description="Permitted remote repository URL schemes."
    )
    allowed_remote_domains: Optional[Set[str]] = Field(
        default=None,
        description="Optional allowlist of remote domains (e.g. {'github.com', 'gitlab.com'}). None allows all."
    )
    scan_secrets: bool = Field(
        default=True,
        description="Mandatory outbound secret scanning on all commit messages and metadata."
    )
    policy_version: str = Field(
        default="1.0.0",
        description="Publication policy schema version."
    )

    def validate_safety_invariants(self) -> None:
        """Validate non-negotiable architectural safety invariants."""
        if self.allow_force_push:
            raise PublicationPolicyViolationError("Policy violation: allow_force_push must never be True.")
        if self.allow_history_rewrite:
            raise PublicationPolicyViolationError("Policy violation: allow_history_rewrite must never be True.")
        if self.allow_automatic_merge:
            raise PublicationPolicyViolationError("Policy violation: allow_automatic_merge must never be True.")
        if self.allow_software_deployment:
            raise PublicationPolicyViolationError("Policy violation: allow_software_deployment must never be True.")


def is_protected_branch(branch_name: str, policy: GitPublicationPolicy) -> bool:
    """Return True if branch_name matches any protected branch or pattern."""
    clean_name = branch_name.strip()
    if clean_name in policy.protected_branches:
        return True
    for pattern in policy.protected_branch_patterns:
        if fnmatch.fnmatch(clean_name, pattern):
            return True
    return False


def validate_branch_name(branch_name: str, policy: GitPublicationPolicy) -> None:
    """Validate a proposed branch name against syntax, length, and protection policies."""
    if not branch_name or not branch_name.strip():
        raise PublicationPolicyViolationError("Branch name cannot be empty or whitespace.")

    clean_name = branch_name.strip()

    if len(clean_name) > policy.max_branch_name_length:
        raise PublicationPolicyViolationError(
            f"Branch name '{clean_name}' exceeds maximum allowed length of {policy.max_branch_name_length}."
        )

    # Check protected branch collision first
    if is_protected_branch(clean_name, policy):
        raise ProtectedBranchViolationError(
            f"Branch name '{clean_name}' collides with protected branch policy."
        )

    # Reject dangerous Git ref constructs
    if (
        ".." in clean_name
        or clean_name.endswith("/")
        or clean_name.endswith(".lock")
        or clean_name.startswith("/")
        or "@{" in clean_name
        or "\\" in clean_name
        or " " in clean_name
    ):
        raise PublicationPolicyViolationError(f"Branch name '{clean_name}' contains illegal Git ref characters.")

    if not clean_name.startswith(policy.permitted_branch_prefix):
        raise PublicationPolicyViolationError(
            f"Branch name '{clean_name}' must begin with permitted prefix '{policy.permitted_branch_prefix}'."
        )

    if not re.match(policy.branch_name_pattern, clean_name):
        raise PublicationPolicyViolationError(
            f"Branch name '{clean_name}' does not match permitted pattern '{policy.branch_name_pattern}'."
        )


def validate_remote_url(remote_url: Optional[str], policy: GitPublicationPolicy) -> None:
    """Verify that a remote repository URL complies with scheme and domain allowlists."""
    if not remote_url:
        return

    # Check scheme
    url_lower = remote_url.lower()
    matched_scheme = False
    for scheme in policy.allowed_remote_schemes:
        if url_lower.startswith(f"{scheme}://") or (scheme == "ssh" and ("@" in url_lower and ":" in url_lower)):
            matched_scheme = True
            break

    if not matched_scheme:
        raise PublicationPolicyViolationError(
            f"Remote URL '{redact_secrets(remote_url)}' uses unauthorized scheme; "
            f"must be one of {policy.allowed_remote_schemes}."
        )

    # Check domain allowlist if configured
    if policy.allowed_remote_domains:
        domain_matched = False
        for domain in policy.allowed_remote_domains:
            if domain.lower() in url_lower:
                domain_matched = True
                break
        if not domain_matched:
            raise PublicationPolicyViolationError(
                f"Remote URL '{redact_secrets(remote_url)}' domain is not in allowed domains: "
                f"{policy.allowed_remote_domains}."
            )


def scan_for_secrets(text: str) -> None:
    """Scan commit message or branch metadata for secrets; raises SecretDetectedError if found."""
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            redacted = redact_secrets(match.group(0))
            raise SecretDetectedError(
                f"Secret or credential detected in metadata: pattern matched '{redacted}'."
            )
