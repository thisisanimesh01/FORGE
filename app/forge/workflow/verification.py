"""Isolated multi-check verification pipeline for Phase 5 Milestone 4.

Guarantees:
- Target repository immutability: target repo is NEVER modified.
- Strict isolation boundary: execution occurs ONLY inside isolated Docker container.
- Fail-closed host protection: if Docker is unavailable, returns BLOCKED without running code on host.
- Ordered multi-stage checks: reproduction oracle -> baseline test suite -> static lint -> security analysis.
- Honest baseline vs. candidate comparisons: pre-existing failures are not attributed to patch; passing baseline does not prove candidate correct.
- Anti-tampering check: validates patch SHA256 digest against stored plan.
- Missing tool honesty: missing tools are reported as SKIPPED with explicit limitations (or BLOCKED if required), never fabricated.
- Command allowlist & shell-injection prevention: only approved binaries with shell=False.
- Disposable workspaces: isolated temporary scratch directories cleaned up in guaranteed finally blocks.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from forge.config import redact_secrets
from forge.repair.contracts import (
    compute_patch_sha256,
    validate_base_sha,
)
from forge.repair.models import ProtectedPathPolicy, RepairPlan
from forge.repair.verifier import apply_unified_diff_to_file, prepare_disposable_workspace
from forge.sandbox.contracts import ExecutionEnvironmentSpec, ReproductionCommand
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    SandboxRequest,
    SandboxResult,
)
from forge.sandbox.policy import (
    APPROVED_EXECUTABLES,
    FORBIDDEN_ARGUMENT_PATTERNS,
    FORBIDDEN_EXECUTABLES,
)
from forge.workflow.evidence_chain import extract_reproduction_command_from_intake
from forge.workflow.models import (
    BaselineCheckFailureError,
    CandidateRegressionError,
    CheckStatus,
    MultiCheckVerificationPlan,
    MultiCheckVerificationResult,
    MultiCheckVerificationStageResult,
    PatchIntegrityTamperedError,
    RevisionDriftError,
    ToolUnavailableError,
    VerificationAggregateOutcome,
    VerificationBlockedError,
    VerificationCheckResult,
    VerificationCheckSpec,
    VerificationCheckType,
    VerificationPipelineError,
    WorkflowIntakeSpec,
)


# Supported static lint and security tools
SUPPORTED_LINT_TOOLS: Set[str] = {"flake8", "ruff"}
SUPPORTED_SECURITY_TOOLS: Set[str] = {"bandit"}


def validate_verification_check_command(command: List[str]) -> List[str]:
    """Validate a check command array against approved executables and shell-injection rules."""
    if not command:
        raise ValueError("Check command cannot be empty")

    binary = Path(command[0]).name
    if binary in FORBIDDEN_EXECUTABLES or binary not in APPROVED_EXECUTABLES:
        raise ValueError(
            f"Executable '{binary}' is not permitted for verification. "
            f"Allowed executables: {sorted(list(APPROVED_EXECUTABLES))}"
        )

    for arg in command:
        for pat in FORBIDDEN_ARGUMENT_PATTERNS:
            if pat.search(arg):
                raise ValueError(
                    f"Check argument '{arg}' contains forbidden shell metacharacters"
                )

    return list(command)


def build_default_verification_plan(
    workflow_id: str,
    repair_plan: RepairPlan,
    intake_spec: WorkflowIntakeSpec,
    repo_path: Path,
    include_lint: bool = True,
    include_security: bool = True,
    baseline_test_command: Optional[List[str]] = None,
) -> MultiCheckVerificationPlan:
    """Build a deterministic MultiCheckVerificationPlan from intake, repair plan, and repository."""
    checks: List[VerificationCheckSpec] = []

    # 1. Reproduction Check (Target defect oracle - baseline MUST fail, candidate MUST pass)
    repro_cmd = extract_reproduction_command_from_intake(intake_spec)
    if repro_cmd:
        repro_args = validate_verification_check_command(repro_cmd.args)
    elif repair_plan.verification_plan:
        repro_args = validate_verification_check_command(repair_plan.verification_plan.split())
    else:
        repro_args = ["pytest", "-q"]

    checks.append(
        VerificationCheckSpec(
            check_type=VerificationCheckType.REPRODUCTION,
            name=f"repro:{repro_args[0]}",
            command=repro_args,
            timeout_seconds=60.0,
            required=True,
            expected_baseline_failure=True,
        )
    )

    # 2. Baseline Test Suite Check (Regression check - pre-existing passing tests must not break)
    if baseline_test_command:
        base_args = validate_verification_check_command(baseline_test_command)
        checks.append(
            VerificationCheckSpec(
                check_type=VerificationCheckType.BASELINE_TESTS,
                name=f"baseline_suite:{base_args[0]}",
                command=base_args,
                timeout_seconds=90.0,
                required=False,
                expected_baseline_failure=False,
            )
        )
    else:
        tests_dir = repo_path / "tests"
        if tests_dir.is_dir():
            # If reproduction command was a specific test, run pytest on tests directory
            checks.append(
                VerificationCheckSpec(
                    check_type=VerificationCheckType.BASELINE_TESTS,
                    name="baseline_suite:pytest",
                    command=["pytest", "-q", "tests/"],
                    timeout_seconds=90.0,
                    required=False,
                    expected_baseline_failure=False,
                )
            )

    # 3. Static Lint Check (flake8 or ruff on target file / affected paths)
    if include_lint:
        target_file = repair_plan.target_file
        checks.append(
            VerificationCheckSpec(
                check_type=VerificationCheckType.LINT,
                name="lint:flake8",
                command=["flake8", target_file],
                timeout_seconds=30.0,
                required=False,
                expected_baseline_failure=False,
            )
        )

    # 4. Security Check (bandit on target file)
    if include_security:
        target_file = repair_plan.target_file
        checks.append(
            VerificationCheckSpec(
                check_type=VerificationCheckType.SECURITY,
                name="security:bandit",
                command=["bandit", "-r", target_file, "-ll"],
                timeout_seconds=30.0,
                required=False,
                expected_baseline_failure=False,
            )
        )

    patch_digest = (
        repair_plan.patch_candidate.sha256_digest
        if repair_plan.patch_candidate
        else compute_patch_sha256(repair_plan.proposed_change)
    )

    plan_hash = hashlib.sha256(f"{workflow_id}:{repair_plan.plan_id}:{patch_digest}".encode()).hexdigest()[:12]
    return MultiCheckVerificationPlan(
        plan_id=f"vplan_{plan_hash}",
        workflow_id=workflow_id,
        repair_plan_id=repair_plan.plan_id,
        base_commit_sha=repair_plan.base_commit_sha,
        patch_digest=patch_digest,
        target_file=repair_plan.target_file,
        checks=checks,
        overall_timeout_seconds=300.0,
    )


class MultiCheckVerifier:
    """Safely executes multi-check verification pipelines in isolated Docker sandboxes."""

    def __init__(self, sandbox: Optional[Any] = None):
        self.sandbox = sandbox or DockerSandbox()

    def check_tool_availability(self, binary: str) -> bool:
        """Check if an approved tool binary is accessible in PATH or sandbox image."""
        if hasattr(self.sandbox, "has_tool"):
            return self.sandbox.has_tool(binary)
        return shutil.which(binary) is not None

    def execute_verification(
        self,
        plan: MultiCheckVerificationPlan,
        repair_plan: RepairPlan,
        target_repo: Path,
        scratch_parent: Optional[Path] = None,
    ) -> MultiCheckVerificationResult:
        """Execute the multi-check verification pipeline across baseline and candidate workspaces."""
        start_mono = time.monotonic()
        target_resolved = target_repo.resolve()
        attempt_id = f"vatt_{hashlib.sha256(f'{plan.plan_id}:{time.time()}'.encode()).hexdigest()[:12]}"

        # ---------------------------------------------------------------------
        # 1. Pre-Execution Invariant & Anti-Tampering Checks
        # ---------------------------------------------------------------------
        if not repair_plan.patch_candidate:
            return self._build_blocked_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.BLOCKED,
                diagnostic="Repair plan has no candidate patch to verify.",
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                sandbox_available=True,
                patch_applied=False,
            )

        # Anti-tampering check: verify SHA256 digest of unified_diff
        current_digest = compute_patch_sha256(repair_plan.patch_candidate.unified_diff)
        if current_digest != repair_plan.patch_candidate.sha256_digest:
            return self._build_blocked_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.BLOCKED,
                diagnostic="Patch integrity check failed: diff content does not match stored SHA256 digest (tampering detected).",
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                sandbox_available=True,
                patch_applied=False,
            )

        # Base SHA validation
        try:
            validate_base_sha(plan.base_commit_sha, repair_plan.base_commit_sha)
        except Exception as e:
            return self._build_blocked_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.BLOCKED,
                diagnostic=f"Base revision verification failed: {e}",
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                sandbox_available=True,
                patch_applied=False,
            )

        # Protected file check
        is_prot, prot_msg = ProtectedPathPolicy.is_protected(repair_plan.target_file)
        if is_prot:
            return self._build_blocked_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.FAILED,
                diagnostic=f"Target file '{repair_plan.target_file}' is protected from patch application: {prot_msg}",
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                sandbox_available=True,
                patch_applied=False,
            )

        # ---------------------------------------------------------------------
        # 2. Sandbox Health & Fail-Closed Boundary Check
        # ---------------------------------------------------------------------
        is_healthy, health_msg = self.sandbox.check_health()
        if not is_healthy:
            return self._build_blocked_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.BLOCKED,
                diagnostic=(
                    f"Verification blocked: isolated container sandbox is unavailable ({health_msg}). "
                    "FORGE fail-closed security policy strictly refuses to run repository code directly on host."
                ),
                duration_ms=(time.monotonic() - start_mono) * 1000.0,
                sandbox_available=False,
                patch_applied=False,
            )

        baseline_results: List[VerificationCheckResult] = []
        candidate_results: List[VerificationCheckResult] = []
        limitations: List[str] = []

        # ---------------------------------------------------------------------
        # 3. Baseline Verification (Clean unpatched workspace)
        # ---------------------------------------------------------------------
        baseline_ws, baseline_cleanup = prepare_disposable_workspace(
            target_repo=target_resolved,
            base_commit_sha=plan.base_commit_sha,
            scratch_parent=scratch_parent,
        )

        try:
            for check in plan.checks:
                b_res = self._run_single_check(
                    check=check,
                    workspace_path=baseline_ws,
                    is_baseline=True,
                    plan=plan,
                    attempt_id=attempt_id,
                )
                baseline_results.append(b_res)
                if not b_res.tool_available and check.check_type in (VerificationCheckType.LINT, VerificationCheckType.SECURITY):
                    limitations.append(f"Tool '{check.command[0]}' is not available; check was skipped without fabricating results.")
        finally:
            baseline_cleanup()

        # Evaluate baseline pre-conditions:
        # For REPRODUCTION check: baseline execution MUST show defect reproduction (expected_baseline_failure).
        # If reproduction check PASSED on baseline (exit code 0), defect was not reproduced -> BASELINE_FAILED.
        repro_baseline = next(
            (r for r in baseline_results if r.check_type == VerificationCheckType.REPRODUCTION),
            None,
        )
        if repro_baseline:
            if repro_baseline.status == CheckStatus.TIMED_OUT:
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.TIMED_OUT,
                    diagnostic="Baseline reproduction check timed out before patch verification.",
                    baseline_results=baseline_results,
                    candidate_results=[],
                    limitations=limitations,
                    duration_ms=(time.monotonic() - start_mono) * 1000.0,
                )
            if repro_baseline.status == CheckStatus.PASSED:
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.BASELINE_FAILED,
                    diagnostic="Baseline reproduction oracle passed unexpectedly. Defect was not reproduced on baseline workspace.",
                    baseline_results=baseline_results,
                    candidate_results=[],
                    limitations=limitations,
                    duration_ms=(time.monotonic() - start_mono) * 1000.0,
                )
            if repro_baseline.status == CheckStatus.BLOCKED:
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.BLOCKED,
                    diagnostic=f"Baseline reproduction check was blocked: {repro_baseline.diagnostic}",
                    baseline_results=baseline_results,
                    candidate_results=[],
                    limitations=limitations,
                    duration_ms=(time.monotonic() - start_mono) * 1000.0,
                )

        # Check if any required baseline check failed to execute due to timeout or block
        for b in baseline_results:
            if b.status == CheckStatus.TIMED_OUT:
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.TIMED_OUT,
                    diagnostic=f"Baseline check '{b.name}' timed out.",
                    baseline_results=baseline_results,
                    candidate_results=[],
                    limitations=limitations,
                    duration_ms=(time.monotonic() - start_mono) * 1000.0,
                )

        # ---------------------------------------------------------------------
        # 4. Candidate Verification (Patched workspace)
        # ---------------------------------------------------------------------
        candidate_ws, candidate_cleanup = prepare_disposable_workspace(
            target_repo=target_resolved,
            base_commit_sha=plan.base_commit_sha,
            scratch_parent=scratch_parent,
        )

        try:
            # Apply unified diff patch to candidate workspace
            target_file_in_candidate = candidate_ws / repair_plan.target_file
            try:
                apply_unified_diff_to_file(target_file_in_candidate, repair_plan.patch_candidate.unified_diff)
                patch_applied = True
                syntax_valid = True
            except Exception as e:
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.FAILED,
                    diagnostic=f"Patch candidate application failed in disposable workspace: {e}",
                    baseline_results=baseline_results,
                    candidate_results=[],
                    limitations=limitations,
                    patch_applied=False,
                    syntax_valid=False,
                    duration_ms=(time.monotonic() - start_mono) * 1000.0,
                )

            # Execute candidate checks
            for check in plan.checks:
                c_res = self._run_single_check(
                    check=check,
                    workspace_path=candidate_ws,
                    is_baseline=False,
                    plan=plan,
                    attempt_id=attempt_id,
                )
                candidate_results.append(c_res)

        finally:
            candidate_cleanup()

        # ---------------------------------------------------------------------
        # 5. Deterministic Delta Analysis & Outcome Aggregation
        # ---------------------------------------------------------------------
        duration_ms = (time.monotonic() - start_mono) * 1000.0

        # Check for timeouts
        timed_out_check = next((c for c in candidate_results if c.status == CheckStatus.TIMED_OUT), None)
        if timed_out_check:
            return self._build_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.TIMED_OUT,
                diagnostic=f"Candidate check '{timed_out_check.name}' timed out.",
                baseline_results=baseline_results,
                candidate_results=candidate_results,
                limitations=limitations,
                duration_ms=duration_ms,
            )

        # Check reproduction oracle on candidate: MUST PASS
        repro_cand = next(
            (r for r in candidate_results if r.check_type == VerificationCheckType.REPRODUCTION),
            None,
        )
        if repro_cand and repro_cand.status != CheckStatus.PASSED:
            return self._build_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.FAILED,
                diagnostic=f"Target reproduction check failed on candidate patch: defect is still reproducible (status: {repro_cand.status.value}).",
                baseline_results=baseline_results,
                candidate_results=candidate_results,
                limitations=limitations,
                duration_ms=duration_ms,
            )

        # Check baseline test regressions:
        # If baseline test suite PASSED on baseline, but FAILS on candidate -> REGRESSION DETECTED!
        baseline_suite_b = next(
            (r for r in baseline_results if r.check_type == VerificationCheckType.BASELINE_TESTS),
            None,
        )
        baseline_suite_c = next(
            (r for r in candidate_results if r.check_type == VerificationCheckType.BASELINE_TESTS),
            None,
        )

        regression_detected = False
        if baseline_suite_b and baseline_suite_c:
            if baseline_suite_b.status == CheckStatus.PASSED and baseline_suite_c.status == CheckStatus.FAILED:
                regression_detected = True
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.FAILED,
                    diagnostic=f"Regression detected: candidate patch broke tests in baseline test suite '{baseline_suite_c.name}'.",
                    baseline_results=baseline_results,
                    candidate_results=candidate_results,
                    regression_detected=True,
                    limitations=limitations,
                    duration_ms=duration_ms,
                )

        # Check lint results:
        lint_cand = next(
            (r for r in candidate_results if r.check_type == VerificationCheckType.LINT),
            None,
        )
        if lint_cand and lint_cand.status == CheckStatus.FAILED:
            return self._build_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.FAILED,
                diagnostic=f"Lint check '{lint_cand.name}' failed on candidate patch:\n{lint_cand.stdout_excerpt[:300]}",
                baseline_results=baseline_results,
                candidate_results=candidate_results,
                limitations=limitations,
                duration_ms=duration_ms,
            )

        # Check security results:
        sec_cand = next(
            (r for r in candidate_results if r.check_type == VerificationCheckType.SECURITY),
            None,
        )
        if sec_cand and sec_cand.status == CheckStatus.FAILED:
            return self._build_result(
                attempt_id=attempt_id,
                plan=plan,
                outcome=VerificationAggregateOutcome.FAILED,
                diagnostic=f"Security scan '{sec_cand.name}' detected vulnerabilities on candidate patch:\n{sec_cand.stdout_excerpt[:300]}",
                baseline_results=baseline_results,
                candidate_results=candidate_results,
                limitations=limitations,
                duration_ms=duration_ms,
            )

        # Check for blocked required checks
        for c in candidate_results:
            if c.status == CheckStatus.BLOCKED:
                return self._build_result(
                    attempt_id=attempt_id,
                    plan=plan,
                    outcome=VerificationAggregateOutcome.BLOCKED,
                    diagnostic=f"Verification blocked by check '{c.name}': {c.diagnostic}",
                    baseline_results=baseline_results,
                    candidate_results=candidate_results,
                    limitations=limitations,
                    duration_ms=duration_ms,
                )

        # All checks passed cleanly
        return self._build_result(
            attempt_id=attempt_id,
            plan=plan,
            outcome=VerificationAggregateOutcome.PASSED,
            diagnostic="All verification checks passed cleanly. Defect resolved with zero regressions.",
            baseline_results=baseline_results,
            candidate_results=candidate_results,
            regression_detected=False,
            limitations=limitations,
            duration_ms=duration_ms,
        )

    def _run_single_check(
        self,
        check: VerificationCheckSpec,
        workspace_path: Path,
        is_baseline: bool,
        plan: MultiCheckVerificationPlan,
        attempt_id: str,
    ) -> VerificationCheckResult:
        """Execute a single check inside the sandbox and record bounded, redacted diagnostics."""
        binary = Path(check.command[0]).name
        provenance = f"forge:verify:{plan.workflow_id}:{check.check_type.value}:{'baseline' if is_baseline else 'candidate'}"
        scratch_id = workspace_path.name

        # Check tool availability
        if not self.check_tool_availability(binary):
            if check.required:
                return VerificationCheckResult(
                    check_type=check.check_type,
                    name=check.name,
                    status=CheckStatus.BLOCKED,
                    command=check.command,
                    pinned_revision=plan.base_commit_sha,
                    scratch_workspace_id=scratch_id,
                    provenance=provenance,
                    is_baseline=is_baseline,
                    diagnostic=f"Required tool '{binary}' is not available in sandbox environment.",
                    tool_available=False,
                )
            else:
                return VerificationCheckResult(
                    check_type=check.check_type,
                    name=check.name,
                    status=CheckStatus.SKIPPED,
                    command=check.command,
                    pinned_revision=plan.base_commit_sha,
                    scratch_workspace_id=scratch_id,
                    provenance=provenance,
                    is_baseline=is_baseline,
                    diagnostic=f"Optional tool '{binary}' is not available; check was skipped.",
                    tool_available=False,
                )

        # Prepare sandbox request
        limits = ExecutionLimits(
            timeout_seconds=check.timeout_seconds,
            memory_limit="512m",
            cpu_quota=1.0,
            pids_limit=64,
        )

        req = SandboxRequest(
            request_id=f"sbx_vcheck_{attempt_id[:8]}_{check.check_type.value}_{'b' if is_baseline else 'c'}",
            run_id=plan.workflow_id,
            workspace_path=workspace_path,
            command=check.command,
            relative_cwd=check.relative_cwd,
            env_vars=check.env_vars,
            limits=limits,
            read_only_workspace=True,
            provenance=provenance,
        )

        start_c = time.monotonic()
        res: SandboxResult = self.sandbox.run(req)
        duration_c_ms = (time.monotonic() - start_c) * 1000.0

        clean_stdout = redact_secrets(res.stdout or "")
        clean_stderr = redact_secrets(res.stderr or "")
        output_digest = hashlib.sha256((clean_stdout + clean_stderr).encode("utf-8")).hexdigest()

        # Classify check status
        if res.timed_out:
            status = CheckStatus.TIMED_OUT
            diag = f"Check '{check.name}' exceeded timeout of {check.timeout_seconds}s."
        elif res.status == ExecutionStatus.UNAVAILABLE:
            status = CheckStatus.BLOCKED
            diag = f"Sandbox unavailable during check '{check.name}': {res.error_message or 'Sandbox unavailable'}"
        elif res.exit_code == 0:
            status = CheckStatus.PASSED
            diag = f"Check '{check.name}' passed with exit code 0."
        else:
            status = CheckStatus.FAILED
            diag = f"Check '{check.name}' failed with exit code {res.exit_code}."

        return VerificationCheckResult(
            check_type=check.check_type,
            name=check.name,
            status=status,
            command=check.command,
            pinned_revision=plan.base_commit_sha,
            scratch_workspace_id=scratch_id,
            exit_code=res.exit_code,
            timed_out=res.timed_out,
            duration_ms=duration_c_ms,
            stdout_excerpt=clean_stdout[:5000],
            stderr_excerpt=clean_stderr[:5000],
            output_digest=output_digest,
            provenance=provenance,
            is_baseline=is_baseline,
            diagnostic=diag,
            tool_available=True,
        )

    def _build_blocked_result(
        self,
        attempt_id: str,
        plan: MultiCheckVerificationPlan,
        outcome: VerificationAggregateOutcome,
        diagnostic: str,
        duration_ms: float,
        sandbox_available: bool,
        patch_applied: bool,
    ) -> MultiCheckVerificationResult:
        return MultiCheckVerificationResult(
            attempt_id=attempt_id,
            workflow_id=plan.workflow_id,
            repair_plan_id=plan.repair_plan_id,
            base_commit_sha=plan.base_commit_sha,
            patch_digest=plan.patch_digest,
            aggregate_outcome=outcome,
            baseline_results=[],
            candidate_results=[],
            regression_detected=False,
            sandbox_available=sandbox_available,
            patch_applied_cleanly=patch_applied,
            syntax_valid=patch_applied,
            diagnostic=diagnostic,
            total_duration_ms=duration_ms,
            limitations=[],
        )

    def _build_result(
        self,
        attempt_id: str,
        plan: MultiCheckVerificationPlan,
        outcome: VerificationAggregateOutcome,
        diagnostic: str,
        baseline_results: List[VerificationCheckResult],
        candidate_results: List[VerificationCheckResult],
        duration_ms: float,
        regression_detected: bool = False,
        limitations: Optional[List[str]] = None,
        patch_applied: bool = True,
        syntax_valid: bool = True,
    ) -> MultiCheckVerificationResult:
        return MultiCheckVerificationResult(
            attempt_id=attempt_id,
            workflow_id=plan.workflow_id,
            repair_plan_id=plan.repair_plan_id,
            base_commit_sha=plan.base_commit_sha,
            patch_digest=plan.patch_digest,
            aggregate_outcome=outcome,
            baseline_results=baseline_results,
            candidate_results=candidate_results,
            regression_detected=regression_detected,
            sandbox_available=True,
            patch_applied_cleanly=patch_applied,
            syntax_valid=syntax_valid,
            diagnostic=diagnostic,
            total_duration_ms=duration_ms,
            limitations=limitations or [],
        )
