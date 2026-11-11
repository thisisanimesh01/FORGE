"""Execution policy enforcement for sandboxed repository commands.

Guarantees:
- Conservative allowlist of permitted binaries.
- Strict rejection of shell operators, shell chaining, shell injection, and shell wrappers.
- Working-directory containment strictly inside the acquired workspace.
- Clean environment variable filtering with strict allowlist.
- Network access disabled by default.
- Untrusted repository scripts/package hooks forbidden.
"""

from pathlib import Path
import re
from typing import Dict, List, Set

from forge.sandbox.models import SandboxRequest, NetworkPolicy


class PolicyViolationError(Exception):
    """Raised when a command request violates security or execution policies."""
    pass


# Conservative allowlist of approved executable binaries
APPROVED_EXECUTABLES: Set[str] = {
    "python",
    "python3",
    "pytest",
    "flake8",
    "mypy",
    "ruff",
    "bandit",
    "pip",
}

# Strictly forbidden shell binaries and operators
FORBIDDEN_EXECUTABLES: Set[str] = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "csh",
    "tcsh",
    "ksh",
    "fish",
    "sudo",
    "su",
    "doas",
    "eval",
    "exec",
    "system",
    "curl",
    "wget",
    "nc",
    "netcat",
    "socat",
    "ncat",
}

# Dangerous characters/operators indicative of shell injection
FORBIDDEN_ARGUMENT_PATTERNS = [
    re.compile(r"[;&|`$><]"),
    re.compile(r"[\r\n\x00]"),
]

# Strict allowlist of environment variables permitted into the sandbox container
ALLOWED_ENV_VAR_NAMES: Set[str] = {
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "LANG",
    "LC_ALL",
    "TERM",
    "PATH",
    "CI",
    "PYTEST_ADDOPTS",
}

# Forbidden pip subcommands in Phase 1 (no installation from untrusted repo)
FORBIDDEN_PIP_SUBCOMMANDS: Set[str] = {
    "install",
    "download",
    "wheel",
    "uninstall",
}

APPROVED_PIP_SUBCOMMANDS: Set[str] = {
    "list",
    "show",
    "check",
    "--version",
    "-V",
}


def validate_sandbox_request(request: SandboxRequest) -> None:
    """Validate sandbox execution request against central security policy.

    Raises:
        PolicyViolationError: If any aspect of the request violates execution policies.
    """
    if not request.command:
        raise PolicyViolationError("Command array cannot be empty")

    raw_binary = request.command[0].strip()
    if "/" in raw_binary or "\\" in raw_binary:
        raise PolicyViolationError(
            f"Executable '{raw_binary}' cannot contain directory separators or path traversal. "
            "Only bare allowlisted binary names are permitted."
        )

    binary = Path(raw_binary).name.lower()

    # 1. Binary allowlist check
    if binary in FORBIDDEN_EXECUTABLES:
        raise PolicyViolationError(f"Executable '{binary}' is explicitly forbidden by security policy")

    if binary not in APPROVED_EXECUTABLES:
        raise PolicyViolationError(
            f"Executable '{binary}' is not in the approved sandbox allowlist: {sorted(APPROVED_EXECUTABLES)}"
        )

    # 2. Executable-specific policy constraints
    # python / python3: Disallow '-c' / '--command' at any position and forbidden 'pip' module invocations
    if binary in {"python", "python3"}:
        args_after = request.command[1:]
        for idx, arg in enumerate(args_after):
            if arg in {"-c", "--command"} or arg.startswith("-c"):
                raise PolicyViolationError("Inline python script execution ('-c') is forbidden by execution policy")
            if arg == "-m" and idx + 1 < len(args_after) and args_after[idx + 1] == "pip":
                pip_sub = args_after[idx + 2].lower() if idx + 2 < len(args_after) else ""
                if pip_sub in FORBIDDEN_PIP_SUBCOMMANDS:
                    raise PolicyViolationError(
                        f"python -m pip {pip_sub} is forbidden by execution policy. "
                        "Dependency installation from untrusted repositories is disabled."
                    )

    # pip: Reject package installs/downloads
    if binary == "pip":
        if len(request.command) > 1:
            subcmd = request.command[1].lower()
            if subcmd in FORBIDDEN_PIP_SUBCOMMANDS:
                raise PolicyViolationError(
                    f"pip subcommand '{subcmd}' is forbidden in audit-only Phase 1. "
                    "Dependency installation from untrusted repositories is disabled."
                )
            if subcmd not in APPROVED_PIP_SUBCOMMANDS and not subcmd.startswith("-"):
                raise PolicyViolationError(
                    f"pip subcommand '{subcmd}' is not in the approved audit allowlist: {sorted(APPROVED_PIP_SUBCOMMANDS)}"
                )

    # 3. Check for shell chaining / injection characters across all arguments
    for idx, arg in enumerate(request.command):
        for pattern in FORBIDDEN_ARGUMENT_PATTERNS:
            if pattern.search(arg):
                raise PolicyViolationError(
                    f"Command argument at index {idx} contains forbidden shell character or operator: {arg!r}"
                )

    # 4. Working directory containment
    norm_cwd = request.relative_cwd.strip().replace("\\", "/")
    if norm_cwd.startswith("/") or norm_cwd.startswith("../") or "/../" in norm_cwd or norm_cwd == "..":
        raise PolicyViolationError(
            f"Working directory '{request.relative_cwd}' attempts path traversal outside workspace"
        )

    # Verify workspace path exists and is an absolute directory
    if not request.workspace_path.is_absolute():
        raise PolicyViolationError(f"Workspace path must be an absolute path: {request.workspace_path}")

    # Check for forbidden host mounts (e.g. root, home, /etc, credential dirs)
    resolved_workspace = request.workspace_path.resolve()
    user_home = Path.home().resolve()
    sensitive_exact = {
        user_home,
        Path("/").resolve(),
    }
    sensitive_prefixes = [
        Path("/etc").resolve(),
        Path("/root").resolve(),
        Path("/System").resolve(),
        user_home / ".ssh",
        user_home / ".aws",
        user_home / ".gnupg",
        user_home / ".kube",
        user_home / ".docker",
    ]
    if resolved_workspace in sensitive_exact or any(
        resolved_workspace == p or resolved_workspace.is_relative_to(p)
        for p in sensitive_prefixes
    ):
        raise PolicyViolationError(
            f"Workspace path points to sensitive host directory: {resolved_workspace}"
        )

    # 5. Environment variable filtering
    for env_name in request.env_vars:
        if env_name not in ALLOWED_ENV_VAR_NAMES:
            raise PolicyViolationError(
                f"Environment variable '{env_name}' is not in approved allowlist: {sorted(ALLOWED_ENV_VAR_NAMES)}"
            )

    # 6. Network policy enforcement
    if request.network_policy != NetworkPolicy.DISABLED:
        raise PolicyViolationError(
            f"Network access policy '{request.network_policy.value}' is not permitted in Phase 1 (must be disabled)"
        )
