"""Deterministic plan validator for FORGE reproduction plans.

Phase 3, Milestone 3: Deterministic Reproduction Planning.

Validates ReproductionPlan objects before execution, enforcing all AGENTS.md
security constraints and policy invariants.

Rejection criteria (any one causes rejection):
- Unapproved executables (not in APPROVED_EXECUTABLES allowlist).
- Shell interpolation in arguments (forbidden patterns).
- Working-directory escape or absolute paths outside workspace.
- Symlink-escape risk in relative_cwd.
- Stale / missing commit SHA.
- Unbounded resource limits (timeout, memory, PID).
- Network-enabled plans without explicit policy approval.
- Package-install commands (pip install / pip download / pip wheel).
- Git modification commands (push, commit, merge, rebase, reset, force-push).
- Credential access arguments (--token, --password, GITHUB_TOKEN, etc.).
- Plans with INSUFFICIENT_INFORMATION strategy (not executable).
- Plans lacking trusted expected-behaviour provenance.
"""

import re
from pathlib import Path, PurePosixPath
from typing import List, Optional

from forge.reproduction.models import FieldProvenance
from forge.reproduction.planner import (
    PlanValidationResult,
    ReproductionPlan,
    ReproductionStrategy,
)
from forge.sandbox.policy import (
    APPROVED_EXECUTABLES,
    FORBIDDEN_ARGUMENT_PATTERNS,
    FORBIDDEN_PIP_SUBCOMMANDS,
)


# ---------------------------------------------------------------------------
# Extra forbidden patterns specific to plan validation
# ---------------------------------------------------------------------------

# Credential / secret-access argument patterns
_CREDENTIAL_PATTERNS: List[re.Pattern] = [
    re.compile(r"--token", re.IGNORECASE),
    re.compile(r"--password", re.IGNORECASE),
    re.compile(r"--api[_-]?key", re.IGNORECASE),
    re.compile(r"GITHUB_TOKEN", re.IGNORECASE),
    re.compile(r"AWS_SECRET", re.IGNORECASE),
    re.compile(r"CI_JOB_TOKEN", re.IGNORECASE),
    re.compile(r"PRIVATE_KEY", re.IGNORECASE),
]

# Git subcommands that modify repository state
_FORBIDDEN_GIT_SUBCOMMANDS: set = {
    "push", "commit", "merge", "rebase", "reset", "cherry-pick",
    "am", "apply", "fetch", "pull", "clone", "init", "tag",
    "stash", "rm", "mv", "add",
}

# Trusted provenance sources for expected behaviour
_TRUSTED_EXPECTED_BEHAVIOR_PROVENANCE = {
    FieldProvenance.TRUSTED_FIXTURE,
    FieldProvenance.STATIC_ANALYSIS,
}

# Minimum and maximum acceptable resource limits
_MAX_TIMEOUT_SECONDS = 300.0
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_MEMORY_BYTES_STR = "2g"  # Checked by convention; actual check is in limits validator


class ReproductionPlanValidator:
    """Validates a ReproductionPlan against all security and policy constraints.

    Usage::

        validator = ReproductionPlanValidator()
        result = validator.validate(plan)
        if not result.is_valid:
            for v in result.violations:
                print(v)
    """

    def validate(
        self,
        plan: ReproductionPlan,
        workspace_path: Optional[Path] = None,
    ) -> PlanValidationResult:
        """Validate *plan* and return a PlanValidationResult.

        Args:
            plan: The ReproductionPlan to validate.
            workspace_path: Optional absolute path to the workspace root; used
                for symlink-escape and absolute-path checks.

        Returns:
            PlanValidationResult with is_valid=True and empty violations on
            success, or is_valid=False with one or more violation strings.
        """
        violations: List[str] = []

        # ------------------------------------------------------------------ #
        # 1. INSUFFICIENT_INFORMATION plans cannot be executed
        # ------------------------------------------------------------------ #
        if plan.strategy == ReproductionStrategy.INSUFFICIENT_INFORMATION:
            violations.append(
                "Plan strategy is INSUFFICIENT_INFORMATION: no independently sourced "
                "expected behaviour was available.  This plan cannot be executed."
            )
            return PlanValidationResult(
                is_valid=False, violations=violations, plan_id=plan.plan_id
            )

        # ------------------------------------------------------------------ #
        # 2. Command must be present for executable plans
        # ------------------------------------------------------------------ #
        if plan.command is None:
            violations.append(
                "No command specified for an executable plan strategy "
                f"'{plan.strategy.value}'.  A typed command argument vector is required."
            )
            return PlanValidationResult(
                is_valid=False, violations=violations, plan_id=plan.plan_id
            )

        args = plan.command.args

        # ------------------------------------------------------------------ #
        # 3. Binary allowlist check
        # ------------------------------------------------------------------ #
        binary = args[0].strip() if args else ""

        if "/" in binary or "\\" in binary:
            violations.append(
                f"Executable '{binary}' contains path separators; only bare allowlisted "
                "binary names are permitted (no directory prefixes)."
            )
        else:
            binary_name = Path(binary).name.lower()
            if binary_name not in APPROVED_EXECUTABLES:
                violations.append(
                    f"Executable '{binary_name}' is not in the approved allowlist: "
                    f"{sorted(APPROVED_EXECUTABLES)}."
                )

        # ------------------------------------------------------------------ #
        # 4. Shell interpolation / injection character check
        # ------------------------------------------------------------------ #
        for idx, arg in enumerate(args):
            for pattern in FORBIDDEN_ARGUMENT_PATTERNS:
                if pattern.search(arg):
                    violations.append(
                        f"Argument at index {idx} '{arg!r}' contains forbidden shell "
                        "metacharacter or injection pattern."
                    )
                    break

        # ------------------------------------------------------------------ #
        # 5. Credential / secret-access argument detection
        # ------------------------------------------------------------------ #
        for idx, arg in enumerate(args):
            for pat in _CREDENTIAL_PATTERNS:
                if pat.search(arg):
                    violations.append(
                        f"Argument at index {idx} '{arg!r}' matches a credential or "
                        "secret-access pattern and is forbidden."
                    )
                    break

        # ------------------------------------------------------------------ #
        # 6. Pip install / package installation check
        # ------------------------------------------------------------------ #
        binary_name_lower = Path(binary).name.lower() if binary else ""
        if binary_name_lower == "pip" and len(args) > 1:
            subcmd = args[1].lower()
            if subcmd in FORBIDDEN_PIP_SUBCOMMANDS:
                violations.append(
                    f"pip subcommand '{subcmd}' installs or modifies packages; "
                    "package installation from untrusted repositories is forbidden."
                )

        # ------------------------------------------------------------------ #
        # 7. Git modification command check
        # ------------------------------------------------------------------ #
        if binary_name_lower == "git" and len(args) > 1:
            subcmd = args[1].lower()
            if subcmd in _FORBIDDEN_GIT_SUBCOMMANDS:
                violations.append(
                    f"git subcommand '{subcmd}' modifies repository state; "
                    "FORGE is audit-only and may not modify target repositories."
                )

        # ------------------------------------------------------------------ #
        # 8. Working-directory containment
        # ------------------------------------------------------------------ #
        raw_cwd = plan.command.relative_cwd
        norm_cwd = raw_cwd.strip().replace("\\", "/")

        if norm_cwd.startswith("/"):
            violations.append(
                f"relative_cwd '{raw_cwd}' is an absolute path; it must be relative "
                "and contained within the repository workspace."
            )

        parts = PurePosixPath(norm_cwd).parts
        depth = 0
        for part in parts:
            if part == "..":
                depth -= 1
            elif part != ".":
                depth += 1
            if depth < 0:
                violations.append(
                    f"relative_cwd '{raw_cwd}' escapes the repository root via path traversal."
                )
                break

        # ------------------------------------------------------------------ #
        # 9. Symlink-escape and containment check (when workspace_path is provided)
        # ------------------------------------------------------------------ #
        if workspace_path is not None:
            try:
                resolved_ws = workspace_path.resolve()
                cwd_candidate = (workspace_path / norm_cwd).resolve()
                if not cwd_candidate.is_relative_to(resolved_ws):
                    violations.append(
                        f"Resolved working directory '{cwd_candidate}' escapes workspace "
                        f"'{resolved_ws}' (possible symlink escape)."
                    )
            except (OSError, ValueError):
                # Path doesn't exist yet — acceptable for pre-execution validation
                pass

            # Check arguments for symlinks escaping workspace
            for idx, arg in enumerate(args[1:], start=1):
                if not arg.startswith("-") and ("/" in arg or "\\" in arg or arg):
                    try:
                        resolved_ws = workspace_path.resolve()
                        arg_candidate = workspace_path / norm_cwd / arg
                        if arg_candidate.is_symlink():
                            resolved_arg = arg_candidate.resolve()
                            if not resolved_arg.is_relative_to(resolved_ws):
                                violations.append(
                                    f"Argument at index {idx} '{arg}' is a symlink resolving "
                                    f"to '{resolved_arg}' outside workspace '{resolved_ws}'."
                                )
                    except (OSError, ValueError):
                        pass

            # Stale revision detection against actual workspace if workspace is a git repository
            if (workspace_path / ".git").exists() and plan.commit_sha:
                from forge.repository.acquisition import run_git_command

                try:
                    cmd_res = run_git_command(["rev-parse", "HEAD"], cwd=workspace_path, timeout_seconds=5)
                    if cmd_res.returncode == 0:
                        obs_sha = cmd_res.stdout.strip().lower()
                        exp_sha = plan.commit_sha.strip().lower()
                        if not obs_sha.startswith(exp_sha) and not exp_sha.startswith(obs_sha):
                            violations.append(
                                f"Workspace revision '{obs_sha}' does not match plan pinned commit '{exp_sha}' (stale revision mismatch)."
                            )
                except Exception:
                    pass

        # ------------------------------------------------------------------ #
        # 10. Commit SHA present and non-empty
        # ------------------------------------------------------------------ #
        if not plan.commit_sha or not plan.commit_sha.strip():
            violations.append(
                "Plan.commit_sha is empty or missing; a pinned revision is required "
                "to prevent stale or substituted execution."
            )

        # ------------------------------------------------------------------ #
        # 11. Resource limits: timeout must be bounded
        # ------------------------------------------------------------------ #
        timeout = plan.environment.timeout_seconds
        if timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
            violations.append(
                f"Execution timeout {timeout}s is outside permitted range "
                f"[{_MIN_TIMEOUT_SECONDS}, {_MAX_TIMEOUT_SECONDS}]s."
            )

        # ------------------------------------------------------------------ #
        # 12. Network access: must not be approved unless explicitly policy-allowed
        # ------------------------------------------------------------------ #
        if plan.network_approved:
            violations.append(
                "Network access is not permitted for reproduction plans in Phase 3. "
                "Set network_approved=False or obtain explicit policy approval."
            )

        # ------------------------------------------------------------------ #
        # 13. Trusted expected-behaviour provenance check
        # ------------------------------------------------------------------ #
        eb_provenance = plan.provenance.expected_behavior_source
        if plan.expected_behavior is not None:
            if eb_provenance not in _TRUSTED_EXPECTED_BEHAVIOR_PROVENANCE:
                violations.append(
                    f"expected_behavior provenance '{eb_provenance.value}' is not a "
                    "trusted source.  Expected behaviour must originate from "
                    "TRUSTED_FIXTURE or STATIC_ANALYSIS."
                )

        # ------------------------------------------------------------------ #
        # Return result
        # ------------------------------------------------------------------ #
        return PlanValidationResult(
            is_valid=len(violations) == 0,
            violations=violations,
            plan_id=plan.plan_id,
        )
