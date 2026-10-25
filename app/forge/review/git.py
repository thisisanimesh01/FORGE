"""Safe read-only Git diff invocation and revision validation."""

from pathlib import Path
import re
import subprocess
from typing import Optional

from forge.config import redact_secrets
from forge.repository.validation import assert_contained_path, SecurityError

# Conservative regex for valid git revision identifiers (branch name, tag, commit SHA, HEAD~N)
REVISION_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\./~^@{}]+$")

MAX_DIFF_BYTES = 2_000_000  # 2MB max diff capture
DEFAULT_DIFF_TIMEOUT_SECONDS = 30


class GitDiffError(Exception):
    """Raised when reading git diff fails or violates security boundaries."""
    pass


def validate_revision(rev: str, param_name: str = "revision") -> str:
    """Validate git revision string against command injection and option injection.

    Rejects:
    - Empty strings
    - Strings starting with '-' or '--' (disallowing git CLI flag injection)
    - Characters like ';', '&', '|', '`', '$', '>', '<', newlines, null bytes
    """
    if not rev or not rev.strip():
        raise GitDiffError(f"Git {param_name} cannot be empty.")

    cleaned = rev.strip()
    if cleaned.startswith("-"):
        raise GitDiffError(f"Git {param_name} cannot start with a dash ('{cleaned}').")

    if not REVISION_PATTERN.match(cleaned):
        raise GitDiffError(f"Git {param_name} contains invalid or dangerous characters: '{cleaned}'.")

    return cleaned


def read_git_diff(
    repo_path: Path,
    base: str,
    head: str,
    timeout_seconds: int = DEFAULT_DIFF_TIMEOUT_SECONDS,
    max_bytes: int = MAX_DIFF_BYTES,
) -> str:
    """Execute git diff safely without shell interpolation, hooks, or external programs.

    Guarantees:
    - Target directory is validated and must contain a .git directory.
    - Base and head revisions are validated against option injection.
    - External diff tools and text converters are disabled (--no-ext-diff, --no-textconv).
    - Hooks are disabled (core.hooksPath=/dev/null).
    - Output is bounded in length and wall-clock execution time.
    """
    if not repo_path.exists() or not repo_path.is_dir():
        raise GitDiffError(f"Target repository path '{repo_path}' does not exist or is not a directory.")

    git_dir = repo_path / ".git"
    if not git_dir.exists():
        raise GitDiffError(f"Target path '{repo_path}' is not a valid Git repository (.git not found).")

    valid_base = validate_revision(base, "base revision")
    valid_head = validate_revision(head, "head revision")

    # Command vector: arguments only, no shell
    cmd = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", f"safe.directory={repo_path.resolve()}",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "-U3",
        valid_base,
        valid_head,
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_path.resolve(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitDiffError(f"Git diff timed out after {timeout_seconds}s: {exc}")
    except Exception as exc:
        raise GitDiffError(f"Failed to execute git diff: {redact_secrets(str(exc))}")

    if proc.returncode != 0:
        err_msg = redact_secrets(proc.stderr.strip() or proc.stdout.strip())
        raise GitDiffError(f"Git diff failed with exit code {proc.returncode}: {err_msg}")

    diff_text = proc.stdout
    if len(diff_text.encode("utf-8", errors="replace")) > max_bytes:
        # Bounded truncation
        truncated = diff_text[:max_bytes]
        return truncated + "\n\n[DIFF TRUNCATED: Output exceeded maximum allowed size]"

    return diff_text
