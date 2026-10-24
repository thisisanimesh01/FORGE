"""Repository target validation and path containment security checks."""

import os
from pathlib import Path
import re
from typing import Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field


class SecurityError(Exception):
    """Raised when a security policy or path containment check is violated."""
    pass


class TargetType:
    REMOTE_GIT = "remote_git"
    LOCAL_DIRECTORY = "local_directory"


class TargetValidationResult(BaseModel):
    """Validated repository target."""
    raw_target: str
    target_type: str
    sanitized_target: str
    is_valid: bool
    error: Optional[str] = None


GIT_HTTPS_REGEX = re.compile(
    r"^https://[a-zA-Z0-9][-a-zA-Z0-9]*(\.[a-zA-Z0-9][-a-zA-Z0-9]*)*(:[0-9]+)?(/[a-zA-Z0-9_.\-]+)+(\.git)?/?$"
)


def validate_target(target: str, allow_local: bool = True) -> TargetValidationResult:
    """Validate a repository URL or local directory path."""
    if not target or not isinstance(target, str):
        return TargetValidationResult(
            raw_target=str(target),
            target_type="invalid",
            sanitized_target="",
            is_valid=False,
            error="Target cannot be empty",
        )

    stripped = target.strip()

    # Check for remote git URL
    if stripped.startswith("http://"):
        return TargetValidationResult(
            raw_target=target,
            target_type=TargetType.REMOTE_GIT,
            sanitized_target=stripped,
            is_valid=False,
            error="Insecure HTTP protocol is not permitted. Use HTTPS.",
        )

    if any(stripped.startswith(proto) for proto in ["git://", "ssh://", "file://", "ftp://"]):
        return TargetValidationResult(
            raw_target=target,
            target_type=TargetType.REMOTE_GIT,
            sanitized_target=stripped,
            is_valid=False,
            error="Unsupported protocol. Only HTTPS URLs or authorized local paths are supported.",
        )

    parsed = urlparse(stripped)

    # Check for embedded credentials (e.g. https://user:pass@github.com/...)
    if parsed.username or parsed.password:
        return TargetValidationResult(
            raw_target=target,
            target_type=TargetType.REMOTE_GIT,
            sanitized_target=f"{parsed.scheme}://{parsed.hostname or 'unknown'}{parsed.path}",
            is_valid=False,
            error="Repository URLs containing embedded credentials are strictly rejected.",
        )

    if parsed.scheme == "https":
        if not GIT_HTTPS_REGEX.match(stripped):
            return TargetValidationResult(
                raw_target=target,
                target_type=TargetType.REMOTE_GIT,
                sanitized_target=stripped,
                is_valid=False,
                error="Malformed HTTPS Git URL format.",
            )
        return TargetValidationResult(
            raw_target=target,
            target_type=TargetType.REMOTE_GIT,
            sanitized_target=stripped,
            is_valid=True,
        )

    # Local directory validation
    if allow_local:
        candidate_path = Path(stripped).expanduser()
        try:
            resolved = candidate_path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as e:
            return TargetValidationResult(
                raw_target=target,
                target_type=TargetType.LOCAL_DIRECTORY,
                sanitized_target=stripped,
                is_valid=False,
                error=f"Local path does not exist: {e}",
            )

        if not resolved.is_dir():
            return TargetValidationResult(
                raw_target=target,
                target_type=TargetType.LOCAL_DIRECTORY,
                sanitized_target=str(resolved),
                is_valid=False,
                error=f"Local path is not a directory: {resolved}",
            )

        return TargetValidationResult(
            raw_target=target,
            target_type=TargetType.LOCAL_DIRECTORY,
            sanitized_target=str(resolved),
            is_valid=True,
        )

    return TargetValidationResult(
        raw_target=target,
        target_type="unsupported",
        sanitized_target=stripped,
        is_valid=False,
        error="Local paths are not permitted in this context",
    )


def assert_contained_path(child_path: Path, root_path: Path) -> Path:
    """Ensure that child_path resolves strictly within root_path, preventing path traversal."""
    resolved_root = root_path.resolve()
    resolved_child = child_path.resolve()

    try:
        resolved_child.relative_to(resolved_root)
    except ValueError:
        raise SecurityError(
            f"Path containment violation: '{resolved_child}' is outside permitted root '{resolved_root}'"
        )

    return resolved_child
