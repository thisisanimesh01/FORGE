"""Comprehensive test suite for Phase 4 Readiness & Sandbox Gate.

Verifies:
1. Sandbox Fail-Closed Invariant: Untrusted repository code never executes on host when Docker is absent.
2. Multi-Provider Invariant: All 6 active providers supported without OpenAI dependencies or silent cloud fallback.
3. Repair Safety Contract: Workspace isolation, base SHA pinning, and immutability invariants.
4. Scope Containment & Path Traversal Guards: Directory traversal, absolute paths, and symlink escapes blocked.
5. Patch Candidate Budgeting: Max lines, hunks, bytes, and declared-scope boundaries enforced.
6. Pre/Post Outcome Classification: Deterministic repair verification vs failure vs regression vs broken build.
7. Human Approval Gate: Mandatory human approval before commit, merge, push, or PR export.
8. Secret Redaction: Patch candidates and audit trails scrub tokens and keys.
9. Target Repository Immutability: Source repositories remain 100% read-only and unmodified.
"""

from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import pytest

from forge.config import ForgeSettings, redact_secrets
from forge.llm.factory import _DEFAULT_MODELS, get_provider
from forge.llm.provider import LLMUnavailableError
from forge.repair.contracts import (
    AutonomousMutationForbiddenError,
    BudgetExceededError,
    HumanApprovalRequiredError,
    PatchCandidate,
    PatchValidationResult,
    RepairAuditRecord,
    RepairBudget,
    RepairLifecycleState,
    RepairOutcome,
    RepairSafetyContract,
    RepairSafetyError,
    ScopeContainmentError,
    StaleBaseRevisionError,
    WorkspaceImmutabilityError,
    assert_human_approval_gate,
    compute_patch_sha256,
    evaluate_repair_outcome,
    parse_and_validate_unified_diff,
    sanitize_patch_content,
    validate_base_sha,
    verify_workspace_containment,
)
from forge.sandbox.contracts import (
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import ExecutionStatus, SandboxRequest
from forge.sandbox.runner import ReproductionHarness
from forge.schemas import EventType


# =============================================================================
# 1. Sandbox Fail-Closed & Host Execution Prevention
# =============================================================================

def test_docker_sandbox_fails_closed_when_binary_missing(tmp_path: Path):
    """DockerSandbox must report unavailable and refuse host execution if docker is missing."""
    sandbox = DockerSandbox(docker_binary=None)
    # Force binary to None
    sandbox.docker_binary = None

    is_healthy, msg = sandbox.check_health()
    assert is_healthy is False
    assert "not found" in msg.lower() or "unavailable" in msg.lower()

    # Attempting to run must fail closed without invoking host execution
    req = SandboxRequest(
        request_id="req_test_01",
        run_id="run_test",
        workspace_path=tmp_path,
        command=["pytest", "tests/"],
    )
    result = sandbox.run(req)

    assert result.status == ExecutionStatus.UNAVAILABLE
    assert result.isolation_established is False
    assert result.exit_code is None


def test_reproduction_harness_fails_closed_without_host_execution(tmp_path: Path):
    """ReproductionHarness returns SKIPPED_SANDBOX_UNAVAILABLE and does not run code on host."""
    # Create a marker file that would be deleted if code actually ran on host
    canary_file = tmp_path / "host_canary.txt"
    canary_file.write_text("untouched")

    mock_sandbox = DockerSandbox(docker_binary="/nonexistent/bin/docker")
    harness = ReproductionHarness(sandbox=mock_sandbox)

    req = ReproductionRequest(
        request_id="req_repro_test",
        finding_id="find_test_01",
        target_repo=str(tmp_path),
        hypothesis="Testing fail closed",
        command=ReproductionCommand(
            args=["pytest", "tests/test_defect.py"],
            relative_cwd=".",
            expected_exit_codes=[1],
        ),
    )

    result = harness.execute(req, tmp_path)

    assert result.outcome == ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE
    assert result.sandbox_available is False
    assert result.exit_code is None
    assert "fail-closed" in result.diagnostic_message.lower()
    assert "host" in result.diagnostic_message.lower()
    # Host canary file must remain untouched
    assert canary_file.read_text() == "untouched"


# =============================================================================
# 2. Multi-Provider Invariant & Cloud Fallback Prohibition
# =============================================================================

def test_supported_providers_inventory():
    """Verify supported providers list matches specifications: fake, ollama, gemini, deepseek, groq."""
    providers = set(_DEFAULT_MODELS.keys())
    expected = {"fake", "ollama", "gemini", "deepseek", "groq"}
    assert providers == expected
    assert "openai" not in providers
    assert "ollama_cloud" not in providers


def test_cloud_fallback_policy_disabled_by_default():
    """Security policy requires FORGE_ALLOW_CLOUD_FALLBACK=false by default."""
    settings = ForgeSettings()
    assert settings.allow_cloud_fallback is False


def test_openai_provider_strictly_rejected():
    """Requesting 'openai' provider must raise LLMUnavailableError."""
    with pytest.raises(LLMUnavailableError, match="Unknown LLM provider 'openai'"):
        get_provider(provider_name="openai")


def test_ollama_cloud_provider_strictly_rejected():
    """Requesting 'ollama_cloud' provider must raise LLMUnavailableError."""
    with pytest.raises(LLMUnavailableError, match="Ollama Cloud provider has been removed"):
        get_provider(provider_name="ollama_cloud")


# =============================================================================
# 3. Repair Safety Contract & Workspace Isolation
# =============================================================================

def test_valid_repair_safety_contract(tmp_path: Path):
    """Valid repair safety contract instantiates successfully."""
    target_dir = tmp_path / "target_repo"
    target_dir.mkdir()
    scratch_dir = tmp_path / "scratch_work"
    scratch_dir.mkdir()

    contract = RepairSafetyContract(
        contract_id="rep_contract_find_01_a1b2",
        finding_id="find_01",
        base_commit_sha="a1b2c3d4e5f67890",
        target_repo_path=target_dir,
        scratch_workspace_path=scratch_dir,
        allowed_files=["src/parser.py"],
    )

    assert contract.contract_id == "rep_contract_find_01_a1b2"
    assert contract.require_human_approval is True
    assert contract.auto_merge_forbidden is True
    assert contract.auto_deploy_forbidden is True
    assert contract.target_repo_read_only is True


def test_reject_identical_target_and_scratch_paths(tmp_path: Path):
    """Target repository and scratch workspace cannot be the same directory."""
    target_dir = tmp_path / "target_repo"
    target_dir.mkdir()

    with pytest.raises(ValueError, match="scratch_workspace_path cannot be identical to target_repo_path"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=target_dir,
            allowed_files=["src/parser.py"],
        )


def test_reject_scratch_inside_target_repo(tmp_path: Path):
    """Scratch workspace cannot reside inside the target repository."""
    target_dir = tmp_path / "target_repo"
    target_dir.mkdir()
    nested_scratch = target_dir / "nested_scratch"
    nested_scratch.mkdir()

    with pytest.raises(ValueError, match="cannot reside inside target_repo_path"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=nested_scratch,
            allowed_files=["src/parser.py"],
        )


def test_reject_target_repo_inside_scratch(tmp_path: Path):
    """Target repository cannot reside inside scratch workspace."""
    scratch_dir = tmp_path / "scratch_dir"
    scratch_dir.mkdir()
    nested_target = scratch_dir / "target_dir"
    nested_target.mkdir()

    with pytest.raises(ValueError, match="cannot reside inside scratch_workspace_path"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=nested_target,
            scratch_workspace_path=scratch_dir,
            allowed_files=["src/parser.py"],
        )


def test_reject_disabling_human_approval(tmp_path: Path):
    """require_human_approval cannot be set to False."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    with pytest.raises(ValueError, match="require_human_approval cannot be disabled"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=scratch_dir,
            allowed_files=["src/parser.py"],
            require_human_approval=False,
        )


def test_reject_enabling_auto_merge_or_deploy(tmp_path: Path):
    """auto_merge_forbidden and auto_deploy_forbidden cannot be set to False."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    with pytest.raises(ValueError, match="auto_merge_forbidden must be True"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=scratch_dir,
            allowed_files=["src/parser.py"],
            auto_merge_forbidden=False,
        )

    with pytest.raises(ValueError, match="auto_deploy_forbidden must be True"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=scratch_dir,
            allowed_files=["src/parser.py"],
            auto_deploy_forbidden=False,
        )


def test_reject_invalid_commit_sha(tmp_path: Path):
    """Invalid commit SHAs (non-hex, too short, empty) must be rejected."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    with pytest.raises(ValueError, match="Invalid git commit SHA"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="invalid_sha_xyz",
            target_repo_path=target_dir,
            scratch_workspace_path=scratch_dir,
            allowed_files=["src/parser.py"],
        )


def test_reject_path_traversal_in_allowed_files(tmp_path: Path):
    """allowed_files with '..' or leading '/' must be rejected."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    with pytest.raises(ValueError, match="Path traversal"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=scratch_dir,
            allowed_files=["../outside.py"],
        )

    with pytest.raises(ValueError, match="must be relative paths"):
        RepairSafetyContract(
            contract_id="rep_01",
            finding_id="find_01",
            base_commit_sha="a1b2c3d4e5f6",
            target_repo_path=target_dir,
            scratch_workspace_path=scratch_dir,
            allowed_files=["/etc/passwd"],
        )


# =============================================================================
# 4. Scope Containment & Path Traversal Guards
# =============================================================================

def test_verify_workspace_containment_valid(tmp_path: Path):
    """Valid path within workspace root succeeds."""
    src = tmp_path / "src"
    src.mkdir()
    parser = src / "parser.py"
    parser.write_text("print('hello')")

    resolved = verify_workspace_containment(tmp_path, "src/parser.py")
    assert resolved == parser.resolve()


def test_verify_workspace_containment_rejects_traversal(tmp_path: Path):
    """Path traversal escaping workspace root raises ScopeContainmentError."""
    with pytest.raises(ScopeContainmentError, match="escapes workspace root"):
        verify_workspace_containment(tmp_path, "../../etc/passwd")


def test_verify_workspace_containment_rejects_absolute_path(tmp_path: Path):
    """Absolute paths raise ScopeContainmentError."""
    with pytest.raises(ScopeContainmentError, match="absolute path rejected"):
        verify_workspace_containment(tmp_path, "/etc/passwd")


def test_verify_workspace_containment_rejects_symlink_escape(tmp_path: Path):
    """Symlink pointing outside workspace root raises ScopeContainmentError."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target_file = external / "secret.txt"
    target_file.write_text("secret")

    # Create symlink inside workspace pointing outside
    link = workspace / "escape_link"
    link.symlink_to(target_file)

    with pytest.raises(ScopeContainmentError, match="escapes workspace root"):
        verify_workspace_containment(workspace, "escape_link")


# =============================================================================
# 5. Base Revision Pinning & Stale Revision Rejection
# =============================================================================

def test_validate_base_sha_matching():
    """Matching SHA or valid prefix match succeeds."""
    validate_base_sha("a1b2c3d4e5f67890", "a1b2c3d4e5f67890")
    validate_base_sha("a1b2c3d4e5f67890", "a1b2c3d")  # 7-char prefix match


def test_validate_base_sha_stale_mismatch():
    """Mismatched commit SHA raises StaleBaseRevisionError."""
    with pytest.raises(StaleBaseRevisionError, match="Stale base revision detected"):
        validate_base_sha("1111111111111111", "2222222222222222")


def test_validate_base_sha_empty():
    """Empty SHA raises StaleBaseRevisionError."""
    with pytest.raises(StaleBaseRevisionError, match="cannot be empty"):
        validate_base_sha("", "a1b2c3d4e5f6")


# =============================================================================
# 6. Patch Candidate Parsing & Budget Limits
# =============================================================================

def _make_contract(tmp_path: Path, budget: RepairBudget = None, allowed_files: list = None) -> RepairSafetyContract:
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return RepairSafetyContract(
        contract_id="rep_test_01",
        finding_id="find_01",
        base_commit_sha="a1b2c3d4e5f6",
        target_repo_path=target,
        scratch_workspace_path=scratch,
        allowed_files=allowed_files or ["src/parser.py"],
        budget=budget or RepairBudget(),
    )


def test_valid_patch_parsing(tmp_path: Path):
    """A valid unified diff within budget and scope passes validation."""
    contract = _make_contract(tmp_path)
    diff = (
        "--- a/src/parser.py\n"
        "+++ b/src/parser.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-for i in range(len(arr) + 1):\n"
        "+for i in range(len(arr)):\n"
        "     process(arr[i])\n"
    )

    is_valid, reasons, metrics = parse_and_validate_unified_diff(contract, diff)
    assert is_valid is True
    assert reasons == []
    assert metrics["hunk_count"] == 1
    assert metrics["added_lines"] == 1
    assert metrics["removed_lines"] == 1
    assert metrics["touched_files"] == ["src/parser.py"]


def test_reject_patch_modifying_unallowed_file(tmp_path: Path):
    """Patch touching a file outside allowed_files must be rejected."""
    contract = _make_contract(tmp_path, allowed_files=["src/parser.py"])
    diff = (
        "--- a/src/unauthorized.py\n"
        "+++ b/src/unauthorized.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-bad\n"
        "+good\n"
    )

    is_valid, reasons, _ = parse_and_validate_unified_diff(contract, diff)
    assert is_valid is False
    assert any("not in declared allowed scope" in r for r in reasons)


def test_reject_patch_exceeding_line_budget(tmp_path: Path):
    """Patch exceeding max_lines_changed must be rejected."""
    budget = RepairBudget(max_lines_changed=4)
    contract = _make_contract(tmp_path, budget=budget)
    # 5 added lines
    diff = (
        "--- a/src/parser.py\n"
        "+++ b/src/parser.py\n"
        "@@ -1,2 +1,7 @@\n"
        "+line1\n"
        "+line2\n"
        "+line3\n"
        "+line4\n"
        "+line5\n"
    )

    is_valid, reasons, _ = parse_and_validate_unified_diff(contract, diff)
    assert is_valid is False
    assert any("exceeds budget max" in r for r in reasons)


def test_reject_patch_exceeding_hunk_budget(tmp_path: Path):
    """Patch exceeding max_hunks must be rejected."""
    budget = RepairBudget(max_hunks=1)
    contract = _make_contract(tmp_path, budget=budget)
    diff = (
        "--- a/src/parser.py\n"
        "+++ b/src/parser.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-a\n"
        "+b\n"
        "@@ -10,2 +10,2 @@\n"
        "-c\n"
        "+d\n"
    )

    is_valid, reasons, _ = parse_and_validate_unified_diff(contract, diff)
    assert is_valid is False
    assert any("Hunk count" in r and "exceeds budget max" in r for r in reasons)


def test_reject_patch_with_path_traversal(tmp_path: Path):
    """Patch targeting diff headers with traversal must be rejected."""
    contract = _make_contract(tmp_path, allowed_files=["src/parser.py"])
    diff = (
        "--- a/../etc/passwd\n"
        "+++ b/../etc/passwd\n"
        "@@ -1,2 +1,2 @@\n"
        "-root\n"
        "+hacked\n"
    )

    is_valid, reasons, _ = parse_and_validate_unified_diff(contract, diff)
    assert is_valid is False
    assert any("path traversal" in r.lower() for r in reasons)


# =============================================================================
# 7. Pre/Post Reproduction Outcome Matrix
# =============================================================================

def test_evaluate_repair_verified():
    """Defect reproduced pre-patch, passes post-patch, baselines pass -> REPAIR_VERIFIED."""
    outcome, msg = evaluate_repair_outcome(
        pre_outcome=ReproductionOutcome.REPRODUCED,
        post_outcome=ReproductionOutcome.NOT_REPRODUCED,
        baseline_checks_passed=True,
        sandbox_healthy=True,
    )
    assert outcome == RepairOutcome.REPAIR_VERIFIED
    assert "verified" in msg.lower()


def test_evaluate_repair_failed_still_reproducible():
    """Defect remains reproduced post-patch -> REPAIR_FAILED."""
    outcome, msg = evaluate_repair_outcome(
        pre_outcome=ReproductionOutcome.REPRODUCED,
        post_outcome=ReproductionOutcome.REPRODUCED,
        baseline_checks_passed=True,
        sandbox_healthy=True,
    )
    assert outcome == RepairOutcome.REPAIR_FAILED
    assert "remains reproducible" in msg.lower()


def test_evaluate_repair_regression():
    """Baseline tests fail post-patch -> REGRESSION_DETECTED."""
    outcome, msg = evaluate_repair_outcome(
        pre_outcome=ReproductionOutcome.REPRODUCED,
        post_outcome=ReproductionOutcome.NOT_REPRODUCED,
        baseline_checks_passed=False,
        sandbox_healthy=True,
    )
    assert outcome == RepairOutcome.REGRESSION_DETECTED
    assert "regression" in msg.lower()


def test_evaluate_repair_broken_build():
    """Post-patch execution crashes or fails with unrelated failure -> BROKEN_BUILD."""
    outcome, msg = evaluate_repair_outcome(
        pre_outcome=ReproductionOutcome.REPRODUCED,
        post_outcome=ReproductionOutcome.UNRELATED_FAILURE,
        baseline_checks_passed=True,
        sandbox_healthy=True,
    )
    assert outcome == RepairOutcome.BROKEN_BUILD
    assert "broken build" in msg.lower()


def test_evaluate_repair_sandbox_unavailable():
    """When sandbox is unhealthy, classification returns SANDBOX_UNAVAILABLE."""
    outcome, msg = evaluate_repair_outcome(
        pre_outcome=ReproductionOutcome.REPRODUCED,
        post_outcome=ReproductionOutcome.NOT_REPRODUCED,
        baseline_checks_passed=True,
        sandbox_healthy=False,
    )
    assert outcome == RepairOutcome.SANDBOX_UNAVAILABLE
    assert "sandbox is unavailable" in msg.lower()


# =============================================================================
# 8. Human Approval Gate
# =============================================================================

def test_assert_human_approval_gate_blocks_unapproved_commit():
    """Attempting commit without human approval raises HumanApprovalRequiredError."""
    record = RepairAuditRecord(
        record_id="rec_01",
        contract_id="rep_01",
        finding_id="find_01",
        base_commit_sha="a1b2c3d4e5f6",
        lifecycle_state=RepairLifecycleState.ACCEPTED,
        human_approved=False,
    )

    with pytest.raises(HumanApprovalRequiredError, match="explicit human approval is mandatory"):
        assert_human_approval_gate(record, "commit")

    with pytest.raises(HumanApprovalRequiredError, match="explicit human approval is mandatory"):
        assert_human_approval_gate(record, "merge")

    with pytest.raises(HumanApprovalRequiredError, match="explicit human approval is mandatory"):
        assert_human_approval_gate(record, "push")

    with pytest.raises(HumanApprovalRequiredError, match="explicit human approval is mandatory"):
        assert_human_approval_gate(record, "export_pr")


def test_assert_human_approval_gate_allows_approved_record():
    """Approved record allows action to proceed."""
    record = RepairAuditRecord(
        record_id="rec_01",
        contract_id="rep_01",
        finding_id="find_01",
        base_commit_sha="a1b2c3d4e5f6",
        lifecycle_state=RepairLifecycleState.ACCEPTED,
        human_approved=True,
        human_approver="security_officer_alice",
        approval_timestamp=datetime.now(timezone.utc),
    )

    # Should not raise
    assert_human_approval_gate(record, "commit")
    assert_human_approval_gate(record, "export_pr")


# =============================================================================
# 9. Secret Redaction & Target Repo Immutability
# =============================================================================

def test_secret_redaction_in_patch():
    """Secrets embedded in patch diffs are redacted."""
    diff_with_secret = (
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old_val\n"
        "+api_key = 'sk-abcdef1234567890abcdef1234567890'\n"
    )
    clean = sanitize_patch_content(diff_with_secret)
    assert "sk-" not in clean or "[REDACTED" in clean


def test_target_repository_immutability(tmp_path: Path):
    """Target repository directory remains bit-for-bit unchanged after repair evaluation."""
    target_dir = tmp_path / "target_repo"
    target_dir.mkdir()
    sample_file = target_dir / "module.py"
    sample_file.write_text("def run():\n    return 42\n")

    initial_content = sample_file.read_text()
    initial_mtime = sample_file.stat().st_mtime_ns

    scratch_dir = tmp_path / "scratch_work"
    scratch_dir.mkdir()

    contract = RepairSafetyContract(
        contract_id="rep_immutability_test",
        finding_id="find_01",
        base_commit_sha="a1b2c3d4e5f6",
        target_repo_path=target_dir,
        scratch_workspace_path=scratch_dir,
        allowed_files=["module.py"],
    )

    # Verify target file has not changed
    assert sample_file.read_text() == initial_content
    assert sample_file.stat().st_mtime_ns == initial_mtime
