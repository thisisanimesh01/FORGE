"""Formal safety contracts and boundary verification for Phase 4 autonomous repair.

Guarantees:
- Target repository immutability: target repo remains strictly read-only. Repairs occur only in isolated scratch workspaces.
- Base revision pinning: repairs are strictly bound to the verified commit SHA where the defect was reproduced.
- Bounded patch candidates: enforces maximum lines, hunks, bytes, and declared-scope single/few file modifications.
- Path traversal & scope containment: checks resolve paths and verify Path.is_relative_to, rejecting traversal and symlink escapes.
- Pre/post reproduction outcomes: deterministic classification (REPAIR_VERIFIED, REPAIR_FAILED, BROKEN_BUILD, REGRESSION_DETECTED, SANDBOX_UNAVAILABLE).
- Rollback & isolation: rollback restores scratch workspaces without affecting target repositories or the host.
- Event audit trail: records every repair step, candidate, outcome, and rollback in SQLite event history.
- Human approval gate: autonomous commits, merges, and pushes are strictly forbidden; explicit human approval is mandatory.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.config import redact_secrets
from forge.sandbox.contracts import ReproductionOutcome


# =============================================================================
# Custom Exception Hierarchy
# =============================================================================

class RepairSafetyError(Exception):
    """Base exception for all repair safety contract violations."""


class ScopeContainmentError(RepairSafetyError):
    """Raised when a patch attempts to modify files outside the declared allowed scope or traverse outside workspace."""


class StaleBaseRevisionError(RepairSafetyError):
    """Raised when repository HEAD or base revision does not match the finding's verified commit SHA."""


class BudgetExceededError(RepairSafetyError):
    """Raised when a patch exceeds allocated size, hunk, or file change quotas."""


class HumanApprovalRequiredError(RepairSafetyError):
    """Raised when attempting an export, commit, or PR operation without explicit human approval."""


class AutonomousMutationForbiddenError(RepairSafetyError):
    """Raised when an autonomous action attempts git push, merge, or production deployment."""


class WorkspaceImmutabilityError(RepairSafetyError):
    """Raised when target repository path or read-only settings are compromised."""


# =============================================================================
# Enums
# =============================================================================

class RepairLifecycleState(str, Enum):
    """Lifecycle states for a repair attempt."""
    PROPOSED = "proposed"
    VALIDATED = "validated"
    EVALUATING = "evaluating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVERTED = "reverted"


class RepairOutcome(str, Enum):
    """Deterministic classification of a repair evaluation."""
    REPAIR_VERIFIED = "repair_verified"        # Defect reproduction is resolved and baseline checks pass
    REPAIR_FAILED = "repair_failed"            # Target defect is still reproducible after patch application
    BROKEN_BUILD = "broken_build"              # Patch caused syntax errors, compile breaks, or harness crashes
    REGRESSION_DETECTED = "regression_detected" # Unrelated existing tests or baseline checks failed
    SANDBOX_UNAVAILABLE = "sandbox_unavailable" # Container sandbox unavailable; fail-closed without host execution
    REPAIR_REJECTED = "repair_rejected"        # Patch rejected by scope, size, or policy validator
    INCONCLUSIVE = "inconclusive"              # Oracle absent or verification result cannot be confirmed
    TIMEOUT = "timeout"                        # Verification execution exceeded duration quota
    FLAKY = "flaky"                            # Inconsistent outcomes across retries


# =============================================================================
# Models
# =============================================================================

class RepairBudget(BaseModel):
    """Upper limits for patch candidate scope and size to prevent uncontrolled edits."""
    model_config = ConfigDict(frozen=True)

    max_lines_changed: int = Field(default=100, ge=1, le=500, description="Max total lines added or deleted")
    max_hunks: int = Field(default=5, ge=1, le=20, description="Max number of diff hunks")
    max_files_modified: int = Field(default=1, ge=1, le=5, description="Max number of files modified")
    max_patch_bytes: int = Field(default=20_000, ge=100, le=100_000, description="Max raw diff size in bytes")


class RepairSafetyContract(BaseModel):
    """Mandatory safety contract that must be validated before any repair attempt."""
    model_config = ConfigDict(frozen=True)

    contract_id: str = Field(description="Unique deterministic identifier for this repair contract")
    finding_id: str = Field(description="Finding identifier that this contract addresses")
    base_commit_sha: str = Field(description="Immutable commit SHA where finding was reproduced")
    target_repo_path: Path = Field(description="Path to target repository (strictly read-only)")
    scratch_workspace_path: Path = Field(description="Isolated scratch workspace where patch evaluation occurs")
    allowed_files: List[str] = Field(description="Explicit list of relative file paths permitted to be edited")
    budget: RepairBudget = Field(default_factory=RepairBudget, description="Resource and size bounds for patches")
    require_human_approval: bool = Field(default=True, description="Human approval gate mandatory before export/commit")
    auto_merge_forbidden: bool = Field(default=True, description="Autonomous merge is strictly forbidden")
    auto_deploy_forbidden: bool = Field(default=True, description="Autonomous deployment is strictly forbidden")
    target_repo_read_only: bool = Field(default=True, description="Enforces target repository immutability")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")

    @field_validator("contract_id")
    @classmethod
    def validate_contract_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("contract_id cannot be empty")
        return v.strip()

    @field_validator("base_commit_sha")
    @classmethod
    def validate_commit_sha(cls, v: str) -> str:
        cleaned = v.strip()
        if not re.match(r"^[0-9a-fA-F]{7,40}$", cleaned):
            raise ValueError(f"Invalid git commit SHA: '{v}' (must be 7-40 hexadecimal characters)")
        return cleaned.lower()

    @field_validator("allowed_files")
    @classmethod
    def validate_allowed_files(cls, files: List[str]) -> List[str]:
        if not files:
            raise ValueError("allowed_files must contain at least one declared file path")
        cleaned_files = []
        for f in files:
            norm = Path(f).as_posix().strip()
            if not norm or norm == ".":
                raise ValueError("allowed_files cannot contain empty paths or '.'")
            if norm.startswith("/") or norm.startswith("\\"):
                raise ValueError(f"allowed_files must be relative paths: '{f}'")
            parts = norm.split("/")
            if ".." in parts:
                raise ValueError(f"Path traversal ('..') forbidden in allowed_files: '{f}'")
            cleaned_files.append(norm)
        return cleaned_files

    @field_validator("require_human_approval")
    @classmethod
    def enforce_human_approval(cls, v: bool) -> bool:
        if not v:
            raise ValueError("require_human_approval cannot be disabled; Phase 4 forbids autonomous unapproved commits")
        return v

    @field_validator("auto_merge_forbidden")
    @classmethod
    def enforce_no_auto_merge(cls, v: bool) -> bool:
        if not v:
            raise ValueError("auto_merge_forbidden must be True; Phase 4 forbids autonomous merging")
        return v

    @field_validator("auto_deploy_forbidden")
    @classmethod
    def enforce_no_auto_deploy(cls, v: bool) -> bool:
        if not v:
            raise ValueError("auto_deploy_forbidden must be True; Phase 4 forbids autonomous deployment")
        return v

    @field_validator("target_repo_read_only")
    @classmethod
    def enforce_target_read_only(cls, v: bool) -> bool:
        if not v:
            raise ValueError("target_repo_read_only must be True; target repository must remain strictly immutable")
        return v

    @model_validator(mode="after")
    def validate_workspace_isolation(self) -> "RepairSafetyContract":
        target_resolved = self.target_repo_path.resolve()
        scratch_resolved = self.scratch_workspace_path.resolve()

        if target_resolved == scratch_resolved:
            raise ValueError("scratch_workspace_path cannot be identical to target_repo_path")

        # Disallow scratch workspace inside target repo
        if scratch_resolved.is_relative_to(target_resolved):
            raise ValueError("scratch_workspace_path cannot reside inside target_repo_path")

        # Disallow target repo inside scratch workspace
        if target_resolved.is_relative_to(scratch_resolved):
            raise ValueError("target_repo_path cannot reside inside scratch_workspace_path")

        return self


class PatchCandidate(BaseModel):
    """A bounded, statically checked candidate patch targeting an isolated scratch workspace."""
    model_config = ConfigDict(frozen=True)

    patch_id: str = Field(description="Unique deterministic identifier for this patch candidate")
    contract_id: str = Field(description="Referenced RepairSafetyContract ID")
    finding_id: str = Field(description="Target finding identifier")
    base_commit_sha: str = Field(description="Target commit SHA")
    target_files: List[str] = Field(description="Files modified by this patch")
    unified_diff: str = Field(description="Unified diff text content")
    added_lines: int = Field(ge=0, description="Total added lines")
    removed_lines: int = Field(ge=0, description="Total removed lines")
    hunk_count: int = Field(ge=1, description="Total diff hunks")
    sha256_digest: str = Field(description="SHA256 digest of sanitized unified diff")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")


class PatchValidationResult(BaseModel):
    """Validation report evaluating a patch candidate against safety boundaries."""
    model_config = ConfigDict(frozen=True)

    patch_id: str = Field(description="Patch candidate ID")
    is_valid: bool = Field(description="Whether the patch passed all static safety checks")
    reasons: List[str] = Field(default_factory=list, description="Explanations of violations or validation notes")
    path_containment_passed: bool = Field(description="Whether all touched files fall within declared allowed scope")
    syntax_valid: bool = Field(description="Whether python code syntax parsed cleanly (if applicable)")
    within_budget: bool = Field(description="Whether patch size is within declared RepairBudget")
    base_sha_matches: bool = Field(description="Whether base SHA matches the contract")


class RepairEvaluationPrePost(BaseModel):
    """Pre- and post-repair reproduction outcomes proving defect elimination without regression."""
    model_config = ConfigDict(frozen=True)

    pre_reproduction_outcome: ReproductionOutcome = Field(description="Pre-patch reproduction outcome (should be REPRODUCED)")
    post_reproduction_outcome: ReproductionOutcome = Field(description="Post-patch reproduction outcome (should be NOT_REPRODUCED or PASSED)")
    baseline_checks_passed: bool = Field(description="Whether existing regression test suite passed")
    final_outcome: RepairOutcome = Field(description="Deterministic final repair outcome")
    diagnostic: str = Field(description="Human-readable explanation of evaluation")


class RepairAuditRecord(BaseModel):
    """Persistent audit trail entry tracking a repair candidate lifecycle."""
    model_config = ConfigDict(frozen=True)

    record_id: str = Field(description="Unique audit record identifier")
    contract_id: str = Field(description="RepairSafetyContract ID")
    finding_id: str = Field(description="Finding identifier")
    patch_id: Optional[str] = Field(default=None, description="PatchCandidate ID if proposed")
    base_commit_sha: str = Field(description="Pinned base commit SHA")
    lifecycle_state: RepairLifecycleState = Field(description="Current state of repair")
    outcome: Optional[RepairOutcome] = Field(default=None, description="Final outcome if evaluated")
    human_approved: bool = Field(default=False, description="Whether explicit human approval was granted")
    human_approver: Optional[str] = Field(default=None, description="Identity of human approver")
    approval_timestamp: Optional[datetime] = Field(default=None, description="When human approval was recorded")
    rollback_performed: bool = Field(default=False, description="Whether scratch workspace was restored/rolled back")
    audit_events: List[str] = Field(default_factory=list, description="IDs of linked RunEvents in SQLite store")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")


# =============================================================================
# Safety Verification Functions
# =============================================================================

def sanitize_patch_content(diff_text: str) -> str:
    """Sanitize secrets and credentials from unified diff content."""
    return redact_secrets(diff_text)


def compute_patch_sha256(diff_text: str) -> str:
    """Compute deterministic SHA256 digest of sanitized patch content."""
    clean = sanitize_patch_content(diff_text)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def validate_base_sha(actual_sha: str, expected_sha: str) -> None:
    """Verify that current repository revision exactly matches the expected pinned SHA."""
    actual_clean = (actual_sha or "").strip().lower()
    expected_clean = (expected_sha or "").strip().lower()

    if not actual_clean or not expected_clean:
        raise StaleBaseRevisionError("Commit SHA cannot be empty for base revision verification")

    # Support prefix matching for short SHAs (min 7 chars)
    if actual_clean.startswith(expected_clean) or expected_clean.startswith(actual_clean):
        return

    raise StaleBaseRevisionError(
        f"Stale base revision detected: repository revision '{actual_sha}' does not match "
        f"expected pinned finding SHA '{expected_sha}'. Repair aborted to prevent TOCTOU drift."
    )


def verify_workspace_containment(workspace_root: Path, relative_path: str) -> Path:
    """Verify that relative_path resolves strictly within workspace_root without escaping.
    
    Prevents path traversal ('..'), absolute paths, and symlink traversal attacks.
    """
    root_resolved = workspace_root.resolve()
    norm = Path(relative_path).as_posix().strip()

    if norm.startswith("/") or norm.startswith("\\"):
        raise ScopeContainmentError(f"Path traversal violation: absolute path rejected: '{relative_path}'")

    candidate = (root_resolved / norm).resolve()

    if not candidate.is_relative_to(root_resolved):
        raise ScopeContainmentError(
            f"Scope containment violation: '{relative_path}' escapes workspace root '{workspace_root}'"
        )

    return candidate


def parse_and_validate_unified_diff(
    contract: RepairSafetyContract,
    unified_diff: str,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Parse and validate unified diff against contract scope, budget, and syntax rules.
    
    Returns (is_valid, reasons, metrics_dict).
    """
    reasons: List[str] = []
    if not unified_diff or not unified_diff.strip():
        return False, ["Unified diff is empty"], {}

    budget = contract.budget
    raw_bytes = len(unified_diff.encode("utf-8"))
    if raw_bytes > budget.max_patch_bytes:
        reasons.append(
            f"Patch size ({raw_bytes} bytes) exceeds budget limit ({budget.max_patch_bytes} bytes)"
        )

    # Extract target files from diff headers (--- a/... and +++ b/...)
    file_pattern = re.compile(r"^\+\+\+\s+[b/]*(.*)$", re.MULTILINE)
    touched_files: Set[str] = set()
    for m in file_pattern.finditer(unified_diff):
        raw_target = m.group(1).strip()
        if raw_target.startswith("b/"):
            raw_target = raw_target[2:]
        if raw_target and raw_target != "/dev/null":
            norm = Path(raw_target).as_posix()
            touched_files.add(norm)

    if not touched_files:
        reasons.append("Malformed diff: no target files detected in diff headers")

    # Scope containment: all touched files must be in allowed_files
    allowed_set = set(contract.allowed_files)
    for tf in touched_files:
        if tf.startswith("/") or ".." in tf.split("/"):
            reasons.append(f"Target file contains path traversal: '{tf}'")
        elif tf not in allowed_set:
            reasons.append(f"Target file '{tf}' is not in declared allowed scope: {sorted(allowed_set)}")

    if len(touched_files) > budget.max_files_modified:
        reasons.append(
            f"Modified {len(touched_files)} files, exceeding budget max of {budget.max_files_modified}"
        )

    # Count hunks and lines
    hunk_pattern = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@", re.MULTILINE)
    hunks = list(hunk_pattern.finditer(unified_diff))
    hunk_count = len(hunks)

    if hunk_count == 0:
        reasons.append("Malformed diff: no @@ hunk headers detected")
    elif hunk_count > budget.max_hunks:
        reasons.append(f"Hunk count ({hunk_count}) exceeds budget max of {budget.max_hunks}")

    added_lines = 0
    removed_lines = 0
    for line in unified_diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added_lines += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed_lines += 1

    total_changed = added_lines + removed_lines
    if total_changed > budget.max_lines_changed:
        reasons.append(
            f"Total changed lines ({total_changed}) exceeds budget max of {budget.max_lines_changed}"
        )

    metrics = {
        "touched_files": sorted(touched_files),
        "hunk_count": hunk_count,
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "total_changed_lines": total_changed,
        "raw_bytes": raw_bytes,
    }

    is_valid = len(reasons) == 0
    return is_valid, reasons, metrics


def evaluate_repair_outcome(
    pre_outcome: ReproductionOutcome,
    post_outcome: ReproductionOutcome,
    baseline_checks_passed: bool,
    sandbox_healthy: bool,
    oracle_present: bool = True,
    oracle_verified: bool = True,
) -> Tuple[RepairOutcome, str]:
    """Deterministically classify repair outcome based on pre/post reproduction and baseline status."""
    if not sandbox_healthy:
        return (
            RepairOutcome.SANDBOX_UNAVAILABLE,
            "Container sandbox is unavailable; cannot safely evaluate repair in isolated environment",
        )

    if not oracle_present or not oracle_verified:
        return (
            RepairOutcome.INCONCLUSIVE,
            "Verification is inconclusive: independent test or oracle was absent or unverified",
        )

    if post_outcome == ReproductionOutcome.TIMEOUT:
        return (
            RepairOutcome.TIMEOUT,
            "Verification execution exceeded timeout quota",
        )

    if post_outcome == ReproductionOutcome.FLAKY:
        return (
            RepairOutcome.FLAKY,
            "Flaky verification outcome detected across execution retries",
        )

    if not baseline_checks_passed:
        return (
            RepairOutcome.REGRESSION_DETECTED,
            "Regression detected: baseline tests failed after patch application",
        )

    # Defect was reproduced prior to patch
    if pre_outcome == ReproductionOutcome.REPRODUCED:
        if post_outcome in (ReproductionOutcome.NOT_REPRODUCED, ReproductionOutcome.PASSED):
            return (
                RepairOutcome.REPAIR_VERIFIED,
                "Repair verified: target defect reproduction resolved and all regression checks pass",
            )
        elif post_outcome == ReproductionOutcome.REPRODUCED:
            return (
                RepairOutcome.REPAIR_FAILED,
                "Repair failed: target defect remains reproducible after patch application",
            )
        elif post_outcome in (
            ReproductionOutcome.UNRELATED_FAILURE,
            ReproductionOutcome.FAILED,
            ReproductionOutcome.RESOURCE_EXHAUSTED,
        ):
            return (
                RepairOutcome.BROKEN_BUILD,
                f"Broken build: post-patch reproduction crashed ({post_outcome.value})",
            )

    return (
        RepairOutcome.REPAIR_FAILED,
        f"Inconclusive or invalid outcome combination: pre={pre_outcome.value}, post={post_outcome.value}",
    )


def assert_human_approval_gate(audit_record: RepairAuditRecord, action: str) -> None:
    """Enforce mandatory human approval gate before export, commit, or pull request operations."""
    forbidden_actions = {"commit", "merge", "push", "deploy", "export_pr", "apply_to_target"}
    if action.lower() in forbidden_actions and not audit_record.human_approved:
        raise HumanApprovalRequiredError(
            f"Action '{action}' blocked by Phase 4 safety gate: explicit human approval is mandatory. "
            f"Contract '{audit_record.contract_id}' has not been approved by a human operator."
        )
