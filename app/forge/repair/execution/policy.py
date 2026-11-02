"""Phase 8 Repair Execution Architecture — Safety Policies & Boundaries.

Defines strict safety policies, execution boundaries, command whitelists,
environment isolation, and patch budget constraints for repair execution.
"""

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge.config import redact_secrets


# =============================================================================
# Policy Exceptions
# =============================================================================

class RepairPolicyViolationError(Exception):
    """Base exception for repair execution policy violations."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class TargetRepoImmutabilityViolationError(RepairPolicyViolationError):
    """Raised when an operation attempts to mutate or use the target repository directly."""


class ScopeContainmentViolationError(RepairPolicyViolationError):
    """Raised when an operation attempts path traversal or touches unauthorized files."""


class ArbitraryExecutionForbiddenError(RepairPolicyViolationError):
    """Raised when an unapproved shell command or injection token is detected."""


class NetworkAccessForbiddenError(RepairPolicyViolationError):
    """Raised when network access is attempted under restricted policy."""


class BudgetExceededViolationError(RepairPolicyViolationError):
    """Raised when patch lines, hunks, or file modification limits are exceeded."""


class ProtectedPathViolationError(RepairPolicyViolationError):
    """Raised when an edit touches protected configuration, license, or security files."""


# =============================================================================
# Policy Constants
# =============================================================================

DEFAULT_ALLOWED_COMMAND_PREFIXES: Set[str] = {
    "pytest",
    "python",
    "python3",
    "unittest",
    "flake8",
    "ruff",
    "mypy",
    "git",
}

DEFAULT_ALLOWED_ENV_VARS: Set[str] = {
    "PATH",
    "PYTHONPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "HOMEDRIVE",
    "HOMEPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
    "FORGE_SANDBOX",
}

SHELL_INJECTION_TOKENS: Tuple[str, ...] = (
    ";",
    "|",
    "&",
    "`",
    "$",
    "(",
    ")",
    "{",
    "}",
    "<",
    ">",
    "\n",
    "\r",
)

PROTECTED_EXACT_FILENAMES: Set[str] = {
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "LICENSE.txt",
    "NOTICE",
    ".gitignore",
    ".gitattributes",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "forge.sqlite3",
    "forge.db",
}

PROTECTED_PATH_PREFIXES: Tuple[str, ...] = (
    ".git/",
    ".github/",
    ".venv/",
    "venv/",
    ".env",
    ".forge/",
)


# =============================================================================
# Policy Specification Model
# =============================================================================

class RepairExecutionPolicy(BaseModel):
    """Comprehensive safety contract and execution policy for repair workflows."""
    model_config = ConfigDict(frozen=True)

    # Resource and Patch Bounds
    max_lines_changed: int = Field(default=100, ge=1, le=500, description="Max total lines added or deleted")
    max_hunks: int = Field(default=5, ge=1, le=20, description="Max number of diff hunks")
    max_files_modified: int = Field(default=1, ge=1, le=5, description="Max number of files modified")
    max_patch_bytes: int = Field(default=20_000, ge=100, le=100_000, description="Max raw diff size in bytes")

    # Safety Invariants
    target_repo_read_only: bool = Field(default=True, description="Strictly enforces target repository immutability")
    allow_network: bool = Field(default=False, description="Strictly forbids network access during repair/verification")
    allow_arbitrary_shell: bool = Field(default=False, description="Strictly forbids unvetted shell string execution")
    require_human_approval: bool = Field(default=True, description="Human approval gate mandatory before export/commit")
    auto_commit_forbidden: bool = Field(default=True, description="Autonomous git commit is strictly forbidden")
    auto_push_forbidden: bool = Field(default=True, description="Autonomous git push is strictly forbidden")
    auto_pr_forbidden: bool = Field(default=True, description="Autonomous PR creation is strictly forbidden")
    auto_deploy_forbidden: bool = Field(default=True, description="Autonomous deployment is strictly forbidden")

    # Whitelists
    allowed_commands: Set[str] = Field(default_factory=lambda: set(DEFAULT_ALLOWED_COMMAND_PREFIXES))
    allowed_env_vars: Set[str] = Field(default_factory=lambda: set(DEFAULT_ALLOWED_ENV_VARS))
    protected_filenames: Set[str] = Field(default_factory=lambda: set(PROTECTED_EXACT_FILENAMES))
    protected_prefixes: Tuple[str, ...] = Field(default=PROTECTED_PATH_PREFIXES)

    @field_validator("target_repo_read_only", "require_human_approval", "auto_commit_forbidden", "auto_push_forbidden", "auto_pr_forbidden")
    @classmethod
    def enforce_mandatory_safety_invariants(cls, v: bool, info) -> bool:
        if not v:
            raise ValueError(f"Safety invariant '{info.field_name}' cannot be disabled; must remain True")
        return v

    def validate_target_file(self, file_path: str, allowed_files: List[str]) -> str:
        """Verify that a target file is within declared scope and not protected.

        Raises:
            ScopeContainmentViolationError: If file is outside allowed_files or attempts traversal.
            ProtectedPathViolationError: If file is protected by policy.
        """
        norm = Path(file_path).as_posix().strip()
        if not norm or norm == ".":
            raise ScopeContainmentViolationError("Empty or root path cannot be modified")
        if norm.startswith("/") or norm.startswith("\\"):
            raise ScopeContainmentViolationError(f"Absolute path '{file_path}' is forbidden")
        if ".." in norm.split("/"):
            raise ScopeContainmentViolationError(f"Path traversal in '{file_path}' is forbidden")

        # Check protected filenames
        name = Path(norm).name
        if name in self.protected_filenames:
            raise ProtectedPathViolationError(
                f"File '{name}' is explicitly protected by security policy"
            )

        # Check protected prefixes
        for prefix in self.protected_prefixes:
            if norm.startswith(prefix) or f"/{prefix}" in f"/{norm}":
                raise ProtectedPathViolationError(
                    f"Path '{file_path}' matches protected prefix '{prefix}'"
                )

        # Check declared allowed files
        norm_allowed = {Path(f).as_posix().strip() for f in allowed_files}
        if norm not in norm_allowed:
            raise ScopeContainmentViolationError(
                f"File '{file_path}' is not within declared allowed files: {sorted(norm_allowed)}"
            )

        return norm

    def validate_command(self, cmd_args: List[str]) -> List[str]:
        """Verify that an execution command uses an approved binary and no injection tokens.

        Raises:
            ArbitraryExecutionForbiddenError: If command or arguments violate policy.
        """
        if not cmd_args:
            raise ArbitraryExecutionForbiddenError("Command argument list cannot be empty")

        binary = Path(cmd_args[0]).name.lower()
        if binary not in self.allowed_commands:
            raise ArbitraryExecutionForbiddenError(
                f"Command '{binary}' is not in pre-approved command whitelist: {sorted(self.allowed_commands)}"
            )

        # Check injection tokens in arguments
        for arg in cmd_args:
            for token in SHELL_INJECTION_TOKENS:
                if token in arg:
                    raise ArbitraryExecutionForbiddenError(
                        f"Command argument '{arg}' contains forbidden shell injection token '{token}'"
                    )

        return cmd_args

    def sanitize_environment(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Filter environment variables strictly against allowed whitelist, stripping secrets."""
        incoming = env or {}
        sanitized = {}
        for k, v in incoming.items():
            if k in self.allowed_env_vars:
                sanitized[k] = redact_secrets(v)
        return sanitized

    def check_patch_budget(
        self,
        lines_added: int,
        lines_deleted: int,
        hunks_count: int,
        files_modified_count: int,
        patch_bytes: int,
    ) -> None:
        """Verify that candidate patch metrics comply with budget limits.

        Raises:
            BudgetExceededViolationError: If any metric exceeds budget limits.
        """
        total_lines = lines_added + lines_deleted
        if total_lines > self.max_lines_changed:
            raise BudgetExceededViolationError(
                f"Patch total lines changed ({total_lines}) exceeds maximum allowed ({self.max_lines_changed})"
            )
        if hunks_count > self.max_hunks:
            raise BudgetExceededViolationError(
                f"Patch hunks count ({hunks_count}) exceeds maximum allowed ({self.max_hunks})"
            )
        if files_modified_count > self.max_files_modified:
            raise BudgetExceededViolationError(
                f"Patch files modified ({files_modified_count}) exceeds maximum allowed ({self.max_files_modified})"
            )
        if patch_bytes > self.max_patch_bytes:
            raise BudgetExceededViolationError(
                f"Patch byte size ({patch_bytes}B) exceeds maximum allowed ({self.max_patch_bytes}B)"
            )
