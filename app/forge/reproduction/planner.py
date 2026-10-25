"""Deterministic reproduction planner for FORGE.

Phase 3, Milestone 3: Deterministic Reproduction Planning.

Converts calibrated findings and trusted reproduction context into bounded,
validated reproduction plans.  Plans are strictly typed, verifiable, and
fail-closed when expected behaviour cannot be independently sourced.

Design principles:
- Plans are NEVER fabricated from LLM assertions or finding descriptions alone.
- Expected behaviour MUST come from TRUSTED_FIXTURE or STATIC_ANALYSIS provenance.
- When independent expected behaviour is absent, the plan strategy is INSUFFICIENT_INFORMATION.
- Argument vectors are always validated before any plan can be executed.
- All plans record stable deterministic IDs bound to the finding + strategy + commit.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field

from forge.reproduction.models import FieldProvenance
from forge.sandbox.contracts import ExecutionEnvironmentSpec, ReproductionCommand
from forge.sandbox.policy import APPROVED_EXECUTABLES


# ---------------------------------------------------------------------------
# Strategy enum
# ---------------------------------------------------------------------------


class ReproductionStrategy(str, Enum):
    """High-level approach for a reproduction plan."""

    RUN_SPECIFIC_TEST = "run_specific_test"
    """Execute a single named pytest node that exercises the defective code path."""

    RUN_SCOPED_TEST_FILE = "run_scoped_test_file"
    """Run an entire test file scoped to the defective module."""

    VALIDATE_BOUNDARY_CONDITION = "validate_boundary_condition"
    """Verify the exact boundary value (e.g., off-by-one) through an isolated assertion."""

    CHECK_EXPECTED_OBSERVED = "check_expected_observed"
    """Observe the actual value produced by the defective code and compare to the trusted expected value."""

    INSUFFICIENT_INFORMATION = "insufficient_information"
    """No independently sourced expected behaviour is available; plan cannot be safely constructed."""


# ---------------------------------------------------------------------------
# Plan provenance
# ---------------------------------------------------------------------------


class PlanProvenance(BaseModel):
    """Immutable provenance record for every field of a reproduction plan."""

    model_config = ConfigDict(frozen=True)

    strategy_source: FieldProvenance = Field(
        description="How the strategy was selected."
    )
    expected_behavior_source: FieldProvenance = Field(
        description="Origin of expected_behavior field."
    )
    command_source: FieldProvenance = Field(
        description="Origin of the command argument vector."
    )
    resource_limits_source: FieldProvenance = Field(
        default=FieldProvenance.SYSTEM_POLICY,
        description="Origin of resource limits (always system policy).",
    )


# ---------------------------------------------------------------------------
# Plan validation result
# ---------------------------------------------------------------------------


class PlanValidationResult(BaseModel):
    """Result of validating a ReproductionPlan before execution."""

    model_config = ConfigDict(frozen=True)

    is_valid: bool = Field(description="Whether the plan passed all validation checks.")
    violations: List[str] = Field(
        default_factory=list,
        description="Human-readable descriptions of each policy violation found.",
    )
    plan_id: str = Field(description="ID of the plan that was validated.")
    validated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Reproduction plan
# ---------------------------------------------------------------------------


class ReproductionPlan(BaseModel):
    """A fully typed, validated plan for deterministic defect reproduction.

    Plans are immutable after creation and must be independently validated
    before execution.  They record complete provenance, resource limits,
    known uncertainties, and cleanup requirements so the execution harness
    can operate without inspecting the finding description or LLM output.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(description="Stable deterministic plan ID.")
    plan_version: str = Field(
        default="1.0.0",
        description="Schema version of this plan format.",
    )
    finding_id: str = Field(description="Target finding ID.")
    commit_sha: str = Field(description="Pinned repository commit revision.")
    target_repo: str = Field(description="Repository URL or local path.")

    strategy: ReproductionStrategy = Field(description="Reproduction strategy.")
    strategy_rationale: str = Field(
        description="Brief explanation of why this strategy was selected."
    )

    # Argument vector — never a shell string
    command: Optional[ReproductionCommand] = Field(
        default=None,
        description="Typed command specification (None when strategy is INSUFFICIENT_INFORMATION).",
    )

    expected_behavior: Optional[str] = Field(
        default=None,
        description=(
            "Independently sourced description of what correct behaviour looks like. "
            "Must not be derived from the finding description or LLM assertions."
        ),
    )
    expected_exit_codes: List[int] = Field(
        default_factory=list,
        description="Exit codes that confirm defect reproduction.",
    )
    expected_output_patterns: List[str] = Field(
        default_factory=list,
        description="Regex patterns in stdout/stderr confirming reproduction.",
    )

    # Resource limits
    environment: ExecutionEnvironmentSpec = Field(
        default_factory=ExecutionEnvironmentSpec,
        description="Sandbox isolation and resource quotas.",
    )

    # Preconditions and cleanup
    preconditions: List[str] = Field(
        default_factory=list,
        description="Conditions that must hold before the plan can be executed.",
    )
    cleanup_requirements: List[str] = Field(
        default_factory=list,
        description="Actions required to restore workspace state after execution.",
    )

    # Known uncertainties
    known_uncertainties: List[str] = Field(
        default_factory=list,
        description="Known limitations or sources of uncertainty in this plan.",
    )

    # Provenance
    provenance: PlanProvenance = Field(
        description="Field-level provenance for this plan."
    )

    # Network / filesystem constraints
    network_approved: bool = Field(
        default=False,
        description="Whether network access has been explicitly approved for this plan.",
    )
    filesystem_write_paths: List[str] = Field(
        default_factory=list,
        description="Paths inside the workspace where writes are permitted.",
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Deterministic plan ID generation
# ---------------------------------------------------------------------------


def generate_plan_id(finding_id: str, strategy: ReproductionStrategy, commit_sha: str) -> str:
    """Produce a stable, deterministic plan ID bound to finding, strategy, and commit.

    The ID is a 12-hex-character SHA-256 prefix prefixed with ``plan_``.
    Identical inputs always yield the same ID, enabling deduplication.
    """
    raw = f"{finding_id}::{strategy.value}::{commit_sha.strip()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    safe_fid = finding_id.replace(":", "_").replace("/", "_")
    return f"plan_{safe_fid}_{strategy.value}_{digest}"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class ReproductionPlanner:
    """Converts calibrated findings and trusted context into bounded reproduction plans.

    Key invariants:
    - Expected behaviour is NEVER read from the finding description alone.
    - If ``trusted_expected_behavior`` is absent, the strategy is forced to
      INSUFFICIENT_INFORMATION.
    - The planner selects a strategy based on the provenance of the context, not
      on free-text assertions from LLM or repository sources.
    """

    def create_plan(
        self,
        *,
        finding_id: str,
        commit_sha: str,
        target_repo: str,
        # Trusted expected behaviour (must be independently sourced)
        trusted_expected_behavior: Optional[str],
        expected_behavior_provenance: FieldProvenance,
        # Command context
        command_args: Optional[List[str]] = None,
        command_relative_cwd: str = ".",
        command_env_vars: Optional[Dict[str, str]] = None,
        expected_exit_codes: Optional[List[int]] = None,
        expected_output_patterns: Optional[List[str]] = None,
        # Optional strategy override
        preferred_strategy: Optional[ReproductionStrategy] = None,
        # Extra plan metadata
        strategy_rationale: Optional[str] = None,
        preconditions: Optional[List[str]] = None,
        cleanup_requirements: Optional[List[str]] = None,
        known_uncertainties: Optional[List[str]] = None,
        environment: Optional[ExecutionEnvironmentSpec] = None,
    ) -> ReproductionPlan:
        """Create a validated, immutable reproduction plan.

        Args:
            finding_id: ID of the finding being reproduced.
            commit_sha: Pinned repository commit SHA.
            target_repo: Repository URL or path.
            trusted_expected_behavior: Independently sourced expected behaviour
                description.  Must not originate from the finding description or
                an LLM assertion.  If ``None``, strategy becomes
                INSUFFICIENT_INFORMATION.
            expected_behavior_provenance: Provenance of ``trusted_expected_behavior``.
                Accepted values: TRUSTED_FIXTURE, STATIC_ANALYSIS.
            command_args: Typed argument vector (list of strings, not a shell string).
            command_relative_cwd: Working directory relative to repo root.
            command_env_vars: Explicit environment variables (filtered by policy).
            expected_exit_codes: Exit codes indicating defect reproduction.
            expected_output_patterns: Regex patterns that confirm reproduction.
            preferred_strategy: Optional caller-supplied strategy hint.
            strategy_rationale: Explanation of strategy selection.
            preconditions: Conditions required before execution.
            cleanup_requirements: Cleanup actions after execution.
            known_uncertainties: Known limitations of this plan.
            environment: Custom sandbox resource quotas.

        Returns:
            An immutable ReproductionPlan.

        Raises:
            ValueError: If expected_behavior_provenance is untrusted while
                trusted_expected_behavior is provided.
        """
        # ------------------------------------------------------------------ #
        # 1. Validate expected behaviour provenance
        # ------------------------------------------------------------------ #
        _TRUSTED_PROVENANCE = {
            FieldProvenance.TRUSTED_FIXTURE,
            FieldProvenance.STATIC_ANALYSIS,
        }

        if trusted_expected_behavior is not None and expected_behavior_provenance not in _TRUSTED_PROVENANCE:
            raise ValueError(
                f"expected_behavior_provenance '{expected_behavior_provenance.value}' is not a "
                f"trusted provenance source.  Accepted values: "
                f"{[p.value for p in _TRUSTED_PROVENANCE]}.  "
                "Use INSUFFICIENT_INFORMATION strategy when expected behaviour cannot be "
                "independently sourced."
            )

        # ------------------------------------------------------------------ #
        # 2. Select strategy
        # ------------------------------------------------------------------ #
        if trusted_expected_behavior is None:
            strategy = ReproductionStrategy.INSUFFICIENT_INFORMATION
            rationale = (
                strategy_rationale
                or "No independently sourced expected behaviour is available; "
                "the plan cannot be safely constructed without fabricating an oracle."
            )
        elif preferred_strategy is not None and preferred_strategy != ReproductionStrategy.INSUFFICIENT_INFORMATION:
            strategy = preferred_strategy
            rationale = strategy_rationale or f"Caller-specified strategy: {preferred_strategy.value}."
        elif command_args and len(command_args) >= 2 and command_args[0] in {"pytest", "python3", "python"}:
            # Heuristic: if a specific test node is named, use RUN_SPECIFIC_TEST
            has_node = any("::" in a for a in command_args[1:])
            if has_node:
                strategy = ReproductionStrategy.RUN_SPECIFIC_TEST
                rationale = strategy_rationale or "Command specifies a pytest node ID, selecting RUN_SPECIFIC_TEST."
            else:
                strategy = ReproductionStrategy.RUN_SCOPED_TEST_FILE
                rationale = strategy_rationale or "Command targets a test file without a specific node, selecting RUN_SCOPED_TEST_FILE."
        else:
            strategy = ReproductionStrategy.CHECK_EXPECTED_OBSERVED
            rationale = strategy_rationale or "No pytest node detected; using CHECK_EXPECTED_OBSERVED to compare observed vs. trusted expectation."

        # ------------------------------------------------------------------ #
        # 3. Build the command (absent for INSUFFICIENT_INFORMATION)
        # ------------------------------------------------------------------ #
        command: Optional[ReproductionCommand] = None

        if strategy != ReproductionStrategy.INSUFFICIENT_INFORMATION and command_args:
            command = ReproductionCommand(
                args=command_args,
                relative_cwd=command_relative_cwd,
                env_vars=command_env_vars or {},
                expected_exit_codes=expected_exit_codes or [1],
                expected_output_patterns=expected_output_patterns or [],
            )

        # ------------------------------------------------------------------ #
        # 4. Build provenance record
        # ------------------------------------------------------------------ #
        provenance = PlanProvenance(
            strategy_source=FieldProvenance.STATIC_ANALYSIS,
            expected_behavior_source=(
                expected_behavior_provenance
                if trusted_expected_behavior is not None
                else FieldProvenance.SYSTEM_POLICY
            ),
            command_source=(
                FieldProvenance.TRUSTED_FIXTURE
                if expected_behavior_provenance == FieldProvenance.TRUSTED_FIXTURE
                else FieldProvenance.STATIC_ANALYSIS
            ),
            resource_limits_source=FieldProvenance.SYSTEM_POLICY,
        )

        # ------------------------------------------------------------------ #
        # 5. Compute stable plan ID and assemble plan
        # ------------------------------------------------------------------ #
        plan_id = generate_plan_id(finding_id, strategy, commit_sha)

        return ReproductionPlan(
            plan_id=plan_id,
            finding_id=finding_id,
            commit_sha=commit_sha,
            target_repo=target_repo,
            strategy=strategy,
            strategy_rationale=rationale,
            command=command,
            expected_behavior=trusted_expected_behavior,
            expected_exit_codes=expected_exit_codes or [],
            expected_output_patterns=expected_output_patterns or [],
            environment=environment or ExecutionEnvironmentSpec(),
            preconditions=preconditions or [],
            cleanup_requirements=cleanup_requirements or [],
            known_uncertainties=known_uncertainties or [],
            provenance=provenance,
            network_approved=False,
        )
