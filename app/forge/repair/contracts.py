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
import ast
import difflib
from enum import Enum
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.config import redact_secrets
from forge.repair.semantic_verifier import analyze_python_ast_semantics
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


def is_test_path(file_path: Union[str, Path]) -> bool:
    """Determine whether a file path represents a test file rather than production implementation."""
    p = Path(file_path)
    parts = [part.lower() for part in p.parts]
    test_dirs = {"tests", "test", "testing", "spec", "specs"}
    if any(part in test_dirs for part in parts[:-1]):
        return True
    name = p.name.lower()
    if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.js") or name.endswith("_test.ts"):
        return True
    if name.endswith("_spec.rb") or name.endswith(".spec.ts") or name.endswith(".spec.js") or name.endswith(".test.js") or name.endswith(".test.ts"):
        return True
    return False


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

    if hunk_count > 0 and added_lines == 0 and removed_lines == 0:
        reasons.append("Patch candidate contains no additions or deletions (empty/no-op diff)")

    # Check for malformed hunk lines (must start with '+', '-', ' ', '\\', or be empty)
    hunk_body_pattern = re.compile(
        r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@[^\n]*\n([\s\S]*?)(?=^@@|\Z)",
        re.MULTILINE,
    )
    all_hunk_bodies = list(hunk_body_pattern.finditer(unified_diff))
    for hb in all_hunk_bodies:
        hunk_text = hb.group(1)
        for line in hunk_text.splitlines():
            if line.startswith(("+", "-", " ", "\\")) or line == "":
                continue
            reasons.append(
                f"Malformed diff hunk: line does not start with diff marker ('+', '-', or space): {line[:60]!r}"
            )
            break

    # Static check for unchanged replacement / no-op patch
    if hunk_count > 0 and all_hunk_bodies and added_lines > 0:
        all_hunks_unchanged = True
        for hb in all_hunk_bodies:
            hunk_text = hb.group(1)
            hunk_added = [l[1:] for l in hunk_text.splitlines() if l.startswith("+") and not l.startswith("+++")]
            hunk_removed = [l[1:] for l in hunk_text.splitlines() if l.startswith("-") and not l.startswith("---")]
            if not (hunk_added and hunk_removed and hunk_added == hunk_removed):
                all_hunks_unchanged = False
                break
        if all_hunks_unchanged:
            reasons.append("Patch candidate contains no effective modifications (unchanged replacement/no-op patch)")

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


def validate_patch_applicability(
    repo_path: Path,
    unified_diff: str,
    base_commit_sha: Optional[str] = None,
    check_semantics: bool = False,
) -> Tuple[bool, List[str]]:
    """Validate in-memory whether a unified diff applies cleanly to target repository files at pinned base revision.

    Guarantees:
    - Never mutates target repository files or git state (in-memory dry run only).
    - Verifies file existence within repository workspace.
    - Verifies that hunk context lines match actual file content around declared line numbers.
    - Verifies that applying the patch yields syntactically valid Python code (for .py files).
    - If check_semantics is True, also validates semantic safety invariants via AST analysis.

    Returns:
        (is_applicable, reasons)
    """
    reasons: List[str] = []
    if not unified_diff or not unified_diff.strip():
        return False, ["Unified diff is empty"]

    # Parse target files and their hunks
    file_diff_pattern = re.compile(r"^---\s+[a/]*(.*?)\n\+\+\+\s+[b/]*(.*?)$", re.MULTILINE)
    hunk_pattern = re.compile(
        r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@[^\n]*\n([\s\S]*?)(?=^@@|\Z)",
        re.MULTILINE,
    )

    file_headers = list(file_diff_pattern.finditer(unified_diff))
    if not file_headers:
        return False, ["Malformed diff: no file headers ('--- a/...' and '+++ b/...') detected"]

    file_blocks: List[Tuple[str, str, str]] = []
    for i, fh in enumerate(file_headers):
        old_file = fh.group(1).strip()
        new_file = fh.group(2).strip()
        start_idx = fh.end()
        end_idx = file_headers[i + 1].start() if i + 1 < len(file_headers) else len(unified_diff)
        body = unified_diff[start_idx:end_idx]
        file_blocks.append((old_file, new_file, body))

    for old_file, new_file, body in file_blocks:
        target_file = new_file
        if target_file.startswith("b/"):
            target_file = target_file[2:]
        if target_file == "/dev/null":
            continue

        try:
            target_disk = verify_workspace_containment(repo_path, target_file)
        except Exception as e:
            reasons.append(f"Scope containment error for '{target_file}': {e}")
            continue

        is_new_file = (old_file == "/dev/null" or old_file == "a//dev/null")
        if not is_new_file:
            if not target_disk.is_file():
                reasons.append(f"Target file '{target_file}' does not exist in repository at base revision")
                continue
            orig_text = target_disk.read_text(encoding="utf-8", errors="replace")
            orig_lines = orig_text.splitlines(keepends=True)
        else:
            orig_lines = []

        hunk_matches = list(hunk_pattern.finditer(body))
        if not hunk_matches:
            reasons.append(f"Target file '{target_file}' has headers but no valid @@ hunk blocks")
            continue

        hunk_blocks = []
        for m in hunk_matches:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            hunk_blocks.append((old_start, old_len, new_start, new_len, m.group(5)))

        # Sort hunks in descending order of old_start for in-memory splicing
        hunk_blocks.sort(key=lambda x: x[0], reverse=True)
        current_lines = list(orig_lines)

        file_applicable = True
        for old_start, old_len, new_start, new_len, hunk_body in hunk_blocks:
            body_lines = hunk_body.splitlines()
            expected_old: List[str] = []
            replacement_new: List[str] = []

            for line in body_lines:
                if line.startswith("-") and not line.startswith("---"):
                    expected_old.append(line[1:])
                elif line.startswith("+") and not line.startswith("+++"):
                    replacement_new.append(line[1:])
                elif line.startswith(" "):
                    expected_old.append(line[1:])
                    replacement_new.append(line[1:])
                elif line == "":
                    expected_old.append("")
                    replacement_new.append("")

            # Find matching context around old_start (1-indexed)
            target_idx = old_start - 1
            match_idx = None
            search_window = [target_idx] + [target_idx + d for d in [1, -1, 2, -2, 3, -3, 4, -4, 5, -5]]

            for candidate_idx in search_window:
                if candidate_idx < 0 or candidate_idx + len(expected_old) > len(current_lines):
                    continue
                matches_candidate = True
                for k, exp in enumerate(expected_old):
                    actual = current_lines[candidate_idx + k].rstrip("\r\n")
                    if actual != exp.rstrip("\r\n"):
                        matches_candidate = False
                        break
                if matches_candidate:
                    match_idx = candidate_idx
                    break

            if match_idx is None:
                file_applicable = False
                reasons.append(
                    f"Patch hunk context mismatch in '{target_file}' around line {old_start}: "
                    f"context lines do not match file content at pinned revision"
                )
                break

            # In-memory splice
            formatted_replacement = [
                l if l.endswith("\n") else f"{l}\n" for l in replacement_new
            ]
            current_lines[match_idx : match_idx + len(expected_old)] = formatted_replacement

        if not file_applicable:
            continue

        # Check Python syntax and semantic safety for .py files
        if target_file.endswith(".py"):
            new_content = "".join(current_lines)
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                reasons.append(f"Patch introduces Python syntax error in '{target_file}': {e}")
            else:
                if check_semantics and not is_new_file:
                    sem_res = analyze_python_ast_semantics(
                        original_source=orig_text,
                        patched_source=new_content,
                        file_path=target_file,
                    )
                    if not sem_res.is_valid:
                        reasons.extend(sem_res.violations)

        # Check that patch actually modified file content (prevent unchanged replacement)
        if not is_new_file and "".join(current_lines) == orig_text:
            reasons.append(
                f"Patch candidate produces no net changes to '{target_file}' (unchanged replacement/no-op patch)"
            )

    return len(reasons) == 0, reasons


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


def _extract_git_head_commit(repo_path: Path) -> Optional[str]:
    """Extract current HEAD commit SHA from a local git repository without modifying state."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def construct_unified_diff_from_search_replace(
    repo_path: Path,
    target_file: str,
    search_block: str,
    replace_block: str,
    allowed_files: Optional[List[str]] = None,
    budget: Optional[RepairBudget] = None,
    base_commit_sha: Optional[str] = None,
) -> Tuple[bool, str, List[str], Dict[str, Any]]:
    """Deterministically construct and validate a standard unified diff from structured search-and-replace proposals.

    Guarantees:
    - Verbatim matching: search_block must match orig_text exactly once (count == 1).
    - No guessing: fails closed if search_block is missing (count == 0) or ambiguous (count > 1).
    - Scope containment: rejects traversal, symlink escapes, absolute paths, test files, and protected paths.
    - Base revision pinning: verifies base commit revision if provided.
    - Budget bounds: enforces patch size, hunks, and line budgets.
    - Syntactic validity: constructs standard unified diff and validates applicability and Python syntax.
    """
    reasons: List[str] = []
    norm_target = Path(target_file).as_posix().strip()

    # 1. Path format and traversal validation
    if not norm_target or norm_target == ".":
        return False, "", ["Empty or root target file path cannot be modified"], {}

    if norm_target.startswith("/") or norm_target.startswith("\\"):
        return False, "", [f"Path traversal violation: absolute path rejected: '{target_file}'"], {}

    if ".." in norm_target.split("/"):
        return False, "", [f"Path traversal violation: '..' forbidden: '{target_file}'"], {}

    # 2. Protected path validation
    from forge.repair.models import ProtectedPathPolicy
    is_prot, prot_reason = ProtectedPathPolicy.is_protected(norm_target)
    if is_prot:
        return False, "", [f"Target file '{norm_target}' is protected by security policy: {prot_reason}"], {}

    # 3. Test path rejection
    if is_test_path(norm_target):
        return (
            False,
            "",
            [f"Target file '{norm_target}' is a test file. Modifying test files to mask defects is strictly forbidden."],
            {},
        )

    # 4. Scope containment against allowed_files
    if allowed_files is not None:
        allowed_set = {Path(f).as_posix().strip() for f in allowed_files}
        if norm_target not in allowed_set:
            return (
                False,
                "",
                [f"Target file '{norm_target}' is not in declared allowed scope: {sorted(allowed_set)}"],
                {},
            )

    # 5. Workspace containment and file existence
    try:
        target_disk = verify_workspace_containment(repo_path, norm_target)
    except Exception as e:
        return False, "", [f"Scope containment error for '{norm_target}': {e}"], {}

    if not target_disk.is_file():
        return False, "", [f"Target file '{norm_target}' does not exist in repository"], {}

    # 6. Base revision verification
    actual_sha = _extract_git_head_commit(repo_path)
    if base_commit_sha is not None and actual_sha is not None:
        try:
            validate_base_sha(actual_sha, base_commit_sha)
        except StaleBaseRevisionError as e:
            return False, "", [str(e)], {}

    # 7. Block content validation
    if not search_block or not search_block.strip():
        return False, "", ["Search block is empty or whitespace-only"], {}

    if not replace_block or not replace_block.strip():
        return False, "", ["Replace block is empty or whitespace-only"], {}

    if search_block == replace_block:
        return False, "", ["Replace block is identical to search block (unchanged replacement / no-op)"], {}

    # 8. Read original text and check exact verbatim count
    orig_text = target_disk.read_text(encoding="utf-8", errors="replace")

    # Match newline convention if needed
    if "\r\n" in orig_text and "\r\n" not in search_block:
        search_block_alt = search_block.replace("\n", "\r\n")
        if search_block_alt in orig_text:
            search_block = search_block_alt
            if "\r\n" not in replace_block:
                replace_block = replace_block.replace("\n", "\r\n")
    elif "\r\n" not in orig_text and "\r\n" in search_block:
        search_block_alt = search_block.replace("\r\n", "\n")
        if search_block_alt in orig_text:
            search_block = search_block_alt
            if "\r\n" in replace_block:
                replace_block = replace_block.replace("\r\n", "\n")

    count = orig_text.count(search_block)
    if count == 0:
        # Check if model emitted line number prefixes (e.g. "  26: " or "26 | ")
        if re.search(r"^\s*\d+[\s:|]", search_block, re.MULTILINE):
            return (
                False,
                "",
                [
                    f"Search block not found in '{norm_target}': Search block appears to contain line number prefixes "
                    f"(e.g. '  26: ' or '26 | '). Do not include line numbers in search_block; copy only verbatim source code."
                ],
                {},
            )

        # Check if the search block omits intervening lines (e.g., spans from function header to body skipping docstring)
        sb_lines = [l.strip() for l in search_block.splitlines() if l.strip()]
        if len(sb_lines) >= 2:
            first_line = sb_lines[0]
            last_line = sb_lines[-1]
            if first_line in orig_text and last_line in orig_text:
                pos1 = orig_text.find(first_line)
                pos2 = orig_text.rfind(last_line)
                if 0 <= pos1 < pos2:
                    return (
                        False,
                        "",
                        [
                            f"Search block not found in '{norm_target}': Beginning and ending lines were found, but "
                            f"intervening lines (e.g. docstrings, comments, or unchanged statements) appear to have "
                            f"been omitted. Do NOT omit intervening source lines. Target a smaller, statement-level block "
                            f"(1-3 lines) containing only the exact lines to modify."
                        ],
                        {},
                    )

        return (
            False,
            "",
            [
                f"Search block not found in '{norm_target}'. The search block must match exact verbatim lines "
                f"in the target file including indentation and line breaks."
            ],
            {},
        )
    if count > 1:
        return (
            False,
            "",
            [
                f"Search block matches {count} locations in '{norm_target}'. The search block must be unique "
                f"and match exactly 1 location. Include additional surrounding context lines to disambiguate."
            ],
            {},
        )

    # 9. Deterministic substitution and unified diff construction
    new_text = orig_text.replace(search_block, replace_block, 1)

    orig_lines = orig_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile=f"a/{norm_target}",
            tofile=f"b/{norm_target}",
            n=3,
        )
    )
    unified_diff = "".join(diff_lines)

    if not unified_diff.strip():
        return False, "", ["Generated unified diff is empty (no changes detected)"], {}

    # 10. Budget and static diff validation
    budget = budget or RepairBudget()
    contract = RepairSafetyContract(
        contract_id="contract_structured_patch",
        finding_id="finding_structured_patch",
        base_commit_sha=base_commit_sha or actual_sha or "0000000000000000000000000000000000000000",
        target_repo_path=repo_path.resolve(),
        scratch_workspace_path=(repo_path.parent / f".scratch_val_{os.getpid()}").resolve(),
        allowed_files=allowed_files or [norm_target],
        budget=budget,
    )
    diff_valid, diff_reasons, metrics = parse_and_validate_unified_diff(contract, unified_diff)
    if not diff_valid:
        return False, unified_diff, diff_reasons, metrics

    # 11. In-memory patch applicability and syntax check
    appl_valid, appl_reasons = validate_patch_applicability(repo_path, unified_diff, base_commit_sha=base_commit_sha)
    if not appl_valid:
        return False, unified_diff, appl_reasons, metrics

    return True, unified_diff, [], metrics
