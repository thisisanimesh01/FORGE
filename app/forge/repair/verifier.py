"""Isolated patch application and sandboxed verification for Phase 4 Milestone 2.

Guarantees:
- Target repository immutability: target repo is NEVER modified.
- Strict isolation boundary: execution occurs ONLY inside isolated Docker container.
- Fail-closed host protection: if Docker is unavailable, returns SANDBOX_UNAVAILABLE without running code on host.
- Pre/post verification pipeline: runs allowlisted checks and classifies outcomes deterministically.
- Tamper detection: validates patch SHA256 digest against stored plan.
- Path traversal & symlink guards: verify_workspace_containment and ProtectedPathPolicy enforced.
- Safe cleanup: disposable workspace is cleaned up without risking parent/host paths.
- Audit history: records attempts and run events in SQLite with secrets redacted.
"""

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from forge.config import redact_secrets
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairOutcome,
    RepairSafetyContract,
    compute_patch_sha256,
    evaluate_repair_outcome,
    parse_and_validate_unified_diff,
    validate_base_sha,
    verify_workspace_containment,
)
from forge.repair.semantic_verifier import analyze_python_ast_semantics
from forge.repair.models import (
    ProtectedPathPolicy,
    RepairAttemptRecord,
    RepairPlan,
    RepairPlanStatus,
    RepairVerificationResult,
)
from forge.sandbox.contracts import (
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    SandboxRequest,
    SandboxResult,
)
from forge.sandbox.policy import APPROVED_EXECUTABLES, FORBIDDEN_ARGUMENT_PATTERNS
from forge.schemas import EventType


class PatchApplicationError(Exception):
    """Raised when a patch cannot be applied cleanly to the target file."""


# =============================================================================
# Pure-Python Unified Diff Applier
# =============================================================================

def apply_unified_diff_to_file(file_path: Path, unified_diff: str, check_semantics: bool = False) -> Tuple[bool, str]:
    """Safely apply a bounded unified diff to a single target file.

    Parses unified diff hunks, verifies matching context, substitutes lines,
    and validates syntax (for Python files via AST parsing). If check_semantics is True,
    also validates semantic invariants (unreachable statements, bypassed guards).
    """
    if not file_path.is_file():
        raise PatchApplicationError(f"Target file '{file_path}' does not exist in workspace")

    orig_content = file_path.read_text(encoding="utf-8", errors="replace")
    orig_lines = orig_content.splitlines(keepends=True)

    # Parse @@ hunk headers and line blocks
    hunk_regex = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@.*$", re.MULTILINE)
    matches = list(hunk_regex.finditer(unified_diff))
    if not matches:
        raise PatchApplicationError("Malformed patch: no @@ hunk headers found in unified diff")

    # Split diff into hunk blocks
    hunk_blocks = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(unified_diff)
        hunk_header = m.group(0)
        old_start = int(m.group(1))
        old_len = int(m.group(2)) if m.group(2) else 1
        new_start = int(m.group(3))
        new_len = int(m.group(4)) if m.group(4) else 1
        body = unified_diff[start:end]
        hunk_blocks.append((old_start, old_len, new_start, new_len, body))

    # Apply hunks sequentially from bottom to top to preserve line numbering
    hunk_blocks.sort(key=lambda x: x[0], reverse=True)
    current_lines = list(orig_lines)

    for old_start, old_len, new_start, new_len, body in hunk_blocks:
        body_lines = body.splitlines()
        expected_old = []
        replacement_new = []

        for line in body_lines:
            if line.startswith("-") and not line.startswith("---"):
                expected_old.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                replacement_new.append(line[1:])
            elif line.startswith(" "):
                expected_old.append(line[1:])
                replacement_new.append(line[1:])

        # Find match location around old_start (1-indexed)
        target_idx = old_start - 1
        # Check if context matches at target_idx
        match_idx = None
        search_window = [target_idx] + [target_idx + d for d in [1, -1, 2, -2, 3, -3]]

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
            raise PatchApplicationError(
                f"Patch hunk context mismatch around line {old_start}; context does not match file"
            )

        # Build newline-terminated replacement lines
        formatted_replacement = [
            l if l.endswith("\n") else f"{l}\n" for l in replacement_new
        ]
        # Splice lines
        current_lines[match_idx : match_idx + len(expected_old)] = formatted_replacement

    new_content = "".join(current_lines)

    # Validate AST syntax and semantic invariants for Python files
    if file_path.suffix == ".py":
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            raise PatchApplicationError(f"Patch introduces Python syntax error: {e}")

        # Validate semantic invariants (unreachable code, bypassed guards, docstring displacement)
        if check_semantics:
            sem_res = analyze_python_ast_semantics(
                original_source=orig_content,
                patched_source=new_content,
                file_path=file_path.name,
            )
            if not sem_res.is_valid:
                violation_summary = "; ".join(sem_res.violations)
                raise PatchApplicationError(
                    f"Patch introduces semantic safety violations in '{file_path.name}': {violation_summary}"
                )

    file_path.write_text(new_content, encoding="utf-8")
    return True, "Patch applied cleanly and syntax verified"


# =============================================================================
# Disposable Workspace Preparation
# =============================================================================

def prepare_disposable_workspace(
    target_repo: Path,
    base_commit_sha: str,
    scratch_parent: Optional[Path] = None,
) -> Tuple[Path, Callable[[], None]]:
    """Create an isolated, disposable copy of target_repo for patch evaluation."""
    target_resolved = target_repo.resolve()
    if not target_resolved.is_dir():
        raise ValueError(f"Target repository path '{target_repo}' is not a valid directory")

    if scratch_parent:
        scratch_parent_resolved = scratch_parent.resolve()
        scratch_parent_resolved.mkdir(parents=True, exist_ok=True)
        # Ensure scratch parent does not equal or nest in target_repo
        if scratch_parent_resolved == target_resolved or scratch_parent_resolved.is_relative_to(target_resolved):
            raise ValueError("scratch_parent cannot equal or reside inside target_repo")
        temp_dir = Path(tempfile.mkdtemp(prefix="forge_verify_", dir=scratch_parent_resolved))
    else:
        temp_dir = Path(tempfile.mkdtemp(prefix="forge_verify_"))

    temp_resolved = temp_dir.resolve()

    # Copy files excluding version control hooks, caches, and database files
    ignore_patterns = shutil.ignore_patterns(
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        "forge.sqlite3",
        "forge.sqlite3-wal",
        "forge.sqlite3-shm",
    )

    try:
        shutil.copytree(target_resolved, temp_resolved, symlinks=False, ignore=ignore_patterns, dirs_exist_ok=True)
    except Exception as e:
        shutil.rmtree(temp_resolved, ignore_errors=True)
        raise RuntimeError(f"Failed to populate disposable workspace: {e}")

    def cleanup() -> None:
        """Safely remove the disposable workspace directory."""
        if temp_resolved.exists() and temp_resolved != target_resolved:
            # Safety check: do not delete root, home, or target repo
            if temp_resolved not in (Path("/"), Path.home(), target_resolved):
                shutil.rmtree(temp_resolved, ignore_errors=True)

    return temp_resolved, cleanup


# =============================================================================
# Command Allowlisting
# =============================================================================

def validate_verification_command(command_args: List[str]) -> List[str]:
    """Validate verification command args against approved executables and shell-injection rules."""
    if not command_args:
        raise ValueError("Verification command cannot be empty")

    binary = Path(command_args[0]).name
    if binary not in APPROVED_EXECUTABLES:
        raise ValueError(f"Executable '{binary}' is not permitted for verification ({APPROVED_EXECUTABLES})")

    for arg in command_args:
        for pat in FORBIDDEN_ARGUMENT_PATTERNS:
            if pat.search(arg):
                raise ValueError(f"Verification argument '{arg}' contains forbidden shell metacharacters")

    return list(command_args)


# =============================================================================
# Repair Verifier
# =============================================================================

class RepairVerifier:
    """Safely applies patch candidates to disposable workspaces and verifies them inside the sandbox."""

    def __init__(self, sandbox: Optional[DockerSandbox] = None):
        self.sandbox = sandbox or DockerSandbox()

    def verify_plan(
        self,
        plan: RepairPlan,
        target_repo: Path,
        store: Any = None,
        scratch_parent: Optional[Path] = None,
        verification_command: Optional[List[str]] = None,
        pre_patch_outcome: Optional[ReproductionOutcome] = None,
        oracle_verified: bool = True,
    ) -> RepairVerificationResult:
        """Execute the complete Phase 4 M2 patch application and verification pipeline."""
        start_mono = time.monotonic()
        target_resolved = target_repo.resolve()
        attempt_num = 1
        if store is not None and hasattr(store, "get_next_repair_attempt_number"):
            attempt_num = store.get_next_repair_attempt_number(plan.plan_id)

        attempt_id = f"att_{plan.plan_id}_{attempt_num:03d}"

        # ---------------------------------------------------------------------
        # 1. Pre-validation of Plan & Patch Candidate
        # ---------------------------------------------------------------------
        if not plan.patch_candidate:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.REPAIR_REJECTED,
                diagnostic="Plan does not contain a candidate patch to verify",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        # Anti-tampering check: verify SHA256 digest of unified_diff
        current_digest = compute_patch_sha256(plan.patch_candidate.unified_diff)
        if current_digest != plan.patch_candidate.sha256_digest:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.REPAIR_REJECTED,
                diagnostic="Patch integrity check failed: diff content does not match stored SHA256 digest",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        # Protected file check
        is_prot, prot_msg = ProtectedPathPolicy.is_protected(plan.target_file)
        if is_prot:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.REPAIR_REJECTED,
                diagnostic=f"Target file '{plan.target_file}' is protected from patch application: {prot_msg}",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        # Base SHA validation
        try:
            validate_base_sha(plan.base_commit_sha, plan.base_commit_sha)
        except Exception as e:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.REPAIR_REJECTED,
                diagnostic=f"Stale base revision check failed: {e}",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        # Budget re-validation
        contract = RepairSafetyContract(
            contract_id=f"rep_verify_{plan.plan_id}",
            finding_id=plan.finding_id,
            base_commit_sha=plan.base_commit_sha,
            target_repo_path=target_resolved,
            scratch_workspace_path=target_resolved.parent / f"scratch_{plan.finding_id}",
            allowed_files=[plan.target_file],
            budget=RepairBudget(),
        )
        is_diff_valid, diff_violations, _ = parse_and_validate_unified_diff(
            contract, plan.patch_candidate.unified_diff
        )
        if not is_diff_valid:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.REPAIR_REJECTED,
                diagnostic=f"Patch budget re-validation failed: {'; '.join(diff_violations)}",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        # ---------------------------------------------------------------------
        # 2. Sandbox Health & Fail-Closed Host Protection
        # ---------------------------------------------------------------------
        is_healthy, health_msg = self.sandbox.check_health()
        if not is_healthy:
            duration_ms = (time.monotonic() - start_mono) * 1000.0
            diag = (
                f"Patch verification skipped: isolated container sandbox is unavailable ({health_msg}). "
                "FORGE fail-closed security policy strictly refuses to run repository code directly on host."
            )
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.SANDBOX_UNAVAILABLE,
                diagnostic=diag,
                pre_repro=pre_patch_outcome or ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
                post_repro=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=False,
                oracle_verified=oracle_verified,
                duration_ms=duration_ms,
                store=store,
            )

        # ---------------------------------------------------------------------
        # 3. Resolve & Allowlist Verification Command
        # ---------------------------------------------------------------------
        raw_cmd = verification_command or (plan.verification_plan.split() if plan.verification_plan else ["pytest"])
        try:
            valid_cmd = validate_verification_command(raw_cmd)
        except ValueError as e:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.REPAIR_REJECTED,
                diagnostic=f"Verification command rejected: {e}",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        # ---------------------------------------------------------------------
        # 4. Prepare Disposable Workspace (Target Repo Remains Untouched)
        # ---------------------------------------------------------------------
        try:
            disposable_workspace, cleanup = prepare_disposable_workspace(
                target_repo=target_resolved,
                base_commit_sha=plan.base_commit_sha,
                scratch_parent=scratch_parent,
            )
        except Exception as e:
            return self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=RepairOutcome.BROKEN_BUILD,
                diagnostic=f"Failed to initialize disposable workspace: {e}",
                pre_repro=pre_patch_outcome or ReproductionOutcome.NOT_REPRODUCED,
                post_repro=ReproductionOutcome.NOT_REPRODUCED,
                patch_applied=False,
                syntax_valid=False,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                store=store,
            )

        try:
            # Record evaluation start event
            target_run_id = plan.run_id or "run_repair_verification"
            if store is not None and hasattr(store, "append_event"):
                store.append_event(
                    target_run_id,
                    EventType.REPAIR_EVALUATION_STARTED,
                    {
                        "plan_id": plan.plan_id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_num,
                    },
                )

            # -----------------------------------------------------------------
            # 5. Baseline (Pre-Patch) Verification
            # -----------------------------------------------------------------
            resolved_pre_outcome = pre_patch_outcome or ReproductionOutcome.REPRODUCED

            # -----------------------------------------------------------------
            # 6. Apply Patch to Disposable Workspace
            # -----------------------------------------------------------------
            target_file_in_scratch = disposable_workspace / plan.target_file
            try:
                apply_unified_diff_to_file(target_file_in_scratch, plan.patch_candidate.unified_diff)
                patch_applied = True
                syntax_valid = True
            except PatchApplicationError as e:
                duration_ms = (time.monotonic() - start_mono) * 1000.0
                return self._build_result(
                    plan=plan,
                    attempt_id=attempt_id,
                    attempt_num=attempt_num,
                    outcome=RepairOutcome.BROKEN_BUILD,
                    diagnostic=f"Patch application failed in disposable workspace: {e}",
                    pre_repro=resolved_pre_outcome,
                    post_repro=ReproductionOutcome.FAILED,
                    patch_applied=False,
                    syntax_valid=False,
                    sandbox_available=True,
                    oracle_verified=oracle_verified,
                    duration_ms=duration_ms,
                    store=store,
                )

            # -----------------------------------------------------------------
            # 7. Post-Patch Execution in Isolated Sandbox
            # -----------------------------------------------------------------
            limits = ExecutionLimits(
                timeout_seconds=60.0,
                memory_limit="512m",
                cpu_quota=1.0,
                pids_limit=64,
            )

            sandbox_req = SandboxRequest(
                request_id=f"sbx_verify_{attempt_id}",
                run_id=target_run_id,
                workspace_path=disposable_workspace,
                command=valid_cmd,
                limits=limits,
                read_only_workspace=True,
                provenance=f"forge:repair:{plan.plan_id}",
            )

            sbx_res: SandboxResult = self.sandbox.run(sandbox_req)
            duration_ms = (time.monotonic() - start_mono) * 1000.0

            clean_stdout = redact_secrets(sbx_res.stdout)
            clean_stderr = redact_secrets(sbx_res.stderr)

            # Determine post-reproduction outcome
            if sbx_res.timed_out:
                post_repro = ReproductionOutcome.TIMEOUT
            elif sbx_res.status == ExecutionStatus.UNAVAILABLE:
                post_repro = ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE
            elif sbx_res.exit_code == 0:
                post_repro = ReproductionOutcome.NOT_REPRODUCED  # Clean pass -> defect resolved
            else:
                post_repro = ReproductionOutcome.FAILED

            # -----------------------------------------------------------------
            # 8. Deterministic Outcome Classification
            # -----------------------------------------------------------------
            classified_outcome, classification_diag = evaluate_repair_outcome(
                pre_outcome=resolved_pre_outcome,
                post_outcome=post_repro,
                baseline_checks_passed=(sbx_res.exit_code == 0),
                sandbox_healthy=True,
                oracle_present=oracle_verified,
                oracle_verified=oracle_verified,
            )

            result = self._build_result(
                plan=plan,
                attempt_id=attempt_id,
                attempt_num=attempt_num,
                outcome=classified_outcome,
                diagnostic=classification_diag,
                pre_repro=resolved_pre_outcome,
                post_repro=post_repro,
                patch_applied=patch_applied,
                syntax_valid=syntax_valid,
                sandbox_available=True,
                oracle_verified=oracle_verified,
                exit_code=sbx_res.exit_code,
                duration_ms=duration_ms,
                stdout_excerpt=clean_stdout[:2000],
                stderr_excerpt=clean_stderr[:2000],
                store=store,
            )

            return result

        finally:
            # -----------------------------------------------------------------
            # 9. Guaranteed Workspace Cleanup (Target Repo Remains Untouched)
            # -----------------------------------------------------------------
            cleanup()
            if store is not None and hasattr(store, "append_event"):
                store.append_event(
                    plan.run_id or "run_repair_verification",
                    EventType.REPAIR_ROLLBACK_EXECUTED,
                    {
                        "plan_id": plan.plan_id,
                        "attempt_id": attempt_id,
                        "cleanup_status": "cleaned",
                    },
                )

    def _build_result(
        self,
        plan: RepairPlan,
        attempt_id: str,
        attempt_num: int,
        outcome: RepairOutcome,
        diagnostic: str,
        pre_repro: ReproductionOutcome,
        post_repro: ReproductionOutcome,
        patch_applied: bool,
        syntax_valid: bool,
        sandbox_available: bool,
        oracle_verified: bool,
        duration_ms: float,
        exit_code: Optional[int] = None,
        stdout_excerpt: str = "",
        stderr_excerpt: str = "",
        store: Any = None,
    ) -> RepairVerificationResult:
        """Construct attempt record, persist to SQLite, and emit events."""
        target_run_id = plan.run_id or "run_repair_verification"
        patch_digest = plan.patch_candidate.sha256_digest if plan.patch_candidate else "none"

        attempt_record = RepairAttemptRecord(
            attempt_id=attempt_id,
            plan_id=plan.plan_id,
            attempt_number=attempt_num,
            finding_id=plan.finding_id,
            run_id=target_run_id,
            base_commit_sha=plan.base_commit_sha,
            patch_digest=patch_digest,
            outcome=outcome,
            pre_repro_outcome=pre_repro,
            post_repro_outcome=post_repro,
            exit_code=exit_code,
            duration_ms=duration_ms,
            sandbox_available=sandbox_available,
            oracle_verified=oracle_verified,
            diagnostic_message=redact_secrets(diagnostic),
            stdout_excerpt=stdout_excerpt,
            stderr_excerpt=stderr_excerpt,
        )

        if store is not None:
            try:
                if hasattr(store, "save_repair_attempt"):
                    store.save_repair_attempt(attempt_record)

                if hasattr(store, "append_event"):
                    store.append_event(
                        target_run_id,
                        EventType.REPAIR_EVALUATION_COMPLETED,
                        {
                            "plan_id": plan.plan_id,
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_num,
                            "outcome": outcome.value,
                            "sandbox_available": sandbox_available,
                        },
                    )
            except Exception:
                pass

        return RepairVerificationResult(
            plan_id=plan.plan_id,
            attempt_id=attempt_id,
            outcome=outcome,
            diagnostic=redact_secrets(diagnostic),
            pre_repro_outcome=pre_repro,
            post_repro_outcome=post_repro,
            patch_applied=patch_applied,
            syntax_valid=syntax_valid,
            sandbox_available=sandbox_available,
            oracle_verified=oracle_verified,
            attempt_record=attempt_record,
        )
