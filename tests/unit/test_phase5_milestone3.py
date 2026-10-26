"""Comprehensive test suite for Phase 5, Milestone 3: Automated Evidence Chaining from Reproduction to Repair.

Covers:
1. Intake command derivation & safe extraction (test specs, issue hints, rejection of unsafe/forbidden commands).
2. Canonical finding synchronization and deterministic identity resolution.
3. Strict reproduction evidence validation & provenance verification:
   - Missing evidence detection.
   - Evidence type mismatch.
   - Corrupted / over-redacted content.
   - Non-reproduced outcome rejection.
   - Finding identity mismatch.
   - Stale commit revision & revision drift.
   - Untrusted oracle rejection.
   - Anti-tampering (exit code 0 contradiction).
4. Guarded state machine transitions for reproduction & repair:
   - Outcome mapping (REPRODUCED, NOT_REPRODUCED, TIMEOUT, FLAKY, INCONCLUSIVE, SKIPPED_SANDBOX_UNAVAILABLE).
   - Rejection of repair planning without verified reproduction evidence.
5. Idempotent reproduction & retry handling.
6. End-to-end stage advancement (CREATED -> INTAKE_VALIDATED -> REPO_ANALYZED -> REPRODUCED -> REPAIR_PENDING).
7. Target repository immutability during all workflow operations.
8. CLI commands (`forge workflow repro`, `forge workflow plan-repair`, `forge workflow advance`).
"""

import json
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import pytest
from typer.testing import CliRunner

from forge.analysis.evidence import create_evidence
from forge.cli import app
from forge.config import redact_secrets
from forge.repair.contracts import PatchCandidate, RepairBudget
from forge.repair.models import PlanValidationResult, RepairPlan, RepairPlanStatus
from forge.repair.planner import RepairPlanner
from forge.reproduction.classifier import ClassificationOutcome, ClassificationReasonCode, ReproductionClassification
from forge.reproduction.manager import ReproductionCaseManager
from forge.reproduction.models import CaseStatus, FieldProvenance, ReproductionAttempt, ReproductionCase
from forge.sandbox.contracts import (
    ReproductionCommand,
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.database import init_database
from forge.storage.store import ForgeStore, StoreError
from forge.workflow.evidence_chain import (
    ensure_finding_in_store,
    extract_reproduction_command_from_intake,
    resolve_intake_finding_id,
    validate_reproduction_evidence,
)
from forge.workflow.intake import (
    create_intake_from_finding,
    create_intake_from_issue,
    create_intake_from_test_spec,
)
from forge.workflow.models import (
    CorruptedEvidenceError,
    EngineeringWorkflow,
    EngineeringWorkflowState,
    EvidenceChainError,
    EvidenceTamperedError,
    FindingIntakePayload,
    IntakeSourceType,
    InvalidWorkflowTransitionError,
    IssueIntakePayload,
    MismatchedEvidenceError,
    MissingEvidenceError,
    RepairPlanningStageResult,
    ReproductionStageResult,
    RevisionDriftError,
    StaleRevisionEvidenceError,
    TestSpecIntakePayload,
    UntrustedOracleError,
    WorkflowIntakeSpec,
)
from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

# Prevent pytest from collecting TestSpecIntakePayload as a test class
TestSpecIntakePayload.__test__ = False

runner = CliRunner()


# =============================================================================
# Fixtures & Test Doubles
# =============================================================================

@pytest.fixture
def mock_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_workflow_m3.db"
    init_database(db_path)
    return db_path


@pytest.fixture
def store(mock_db: Path) -> ForgeStore:
    return ForgeStore(db_path=mock_db)


@pytest.fixture
def orchestrator(store: ForgeStore) -> EngineeringWorkflowOrchestrator:
    return EngineeringWorkflowOrchestrator(store=store)


@pytest.fixture
def test_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Create a minimal clean local git repository for testing and return (repo_path, commit_sha)."""
    repo = tmp_path / "git_target_repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True, capture_output=True)

    src_dir = repo / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "calculator.py").write_text("def add(a, b):\n    return a - b  # Bug: subtraction instead of addition\n", encoding="utf-8")
    (repo / "README.md").write_text("# Target Calculator Repo\n", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_calc.py").write_text("def test_calc(): assert False\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    sha = res.stdout.strip()
    return repo, sha


class MockReproductionHarness:
    """Configurable mock test harness simulating sandboxed execution outcomes."""

    def __init__(self, outcome: ReproductionOutcome = ReproductionOutcome.REPRODUCED, exit_code: int = 1, stdout: str = "", stderr: str = "AssertionError: 3 != 1"):
        self.outcome = outcome
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.executed_commands: List[ReproductionCommand] = []

    def execute(self, request: ReproductionRequest, workspace_path: Path) -> ReproductionResult:
        self.executed_commands.append(request.command)
        return ReproductionResult(
            request_id=request.request_id,
            finding_id=request.finding_id,
            outcome=self.outcome,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_ms=42.0,
            matched_patterns=["AssertionError"] if "AssertionError" in self.stderr else [],
            sandbox_available=(self.outcome != ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE),
            isolation_details={"sandbox": "mock"},
            diagnostic_message="Simulated mock execution" if self.outcome != ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE else "Docker unavailable",
        )


class MockRepairPlanner:
    """Configurable mock repair planner simulating valid and rejected patch candidates."""

    def __init__(self, status: RepairPlanStatus = RepairPlanStatus.VALIDATED, violations: Optional[List[str]] = None):
        self.status = status
        self.violations = violations or []

    def plan_repair(
        self,
        finding_id: str,
        target_repo: Path,
        base_commit_sha: Optional[str] = None,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        scratch_workspace_path: Optional[Path] = None,
        store: Any = None,
        run_id: Optional[str] = None,
    ) -> RepairPlan:
        patch = None
        sha = base_commit_sha or "0000000000000000000000000000000000000000"
        if self.status == RepairPlanStatus.VALIDATED:
            patch = PatchCandidate(
                patch_id="pat_mock_001",
                contract_id="contract_mock_001",
                finding_id=finding_id,
                base_commit_sha=sha,
                target_files=["src/calculator.py"],
                unified_diff="--- a/src/calculator.py\n+++ b/src/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
                added_lines=1,
                removed_lines=1,
                hunk_count=1,
                sha256_digest="abc123sha256abc123sha256abc123sha256abc123sha256",
            )
        validation = PlanValidationResult(
            plan_id="plan_mock_001",
            is_valid=(self.status == RepairPlanStatus.VALIDATED),
            violations=self.violations,
            evidence_valid=True,
            patch_valid=True,
            scope_valid=True,
            budget_valid=True,
            base_sha_valid=True,
        )
        return RepairPlan(
            plan_id="plan_mock_001",
            finding_id=finding_id,
            target_repo=str(target_repo),
            base_commit_sha=sha,
            cited_evidence_ids=["evi_001"],
            root_cause="Arithmetic operator error",
            proposed_change="Replace subtraction with addition",
            target_file="src/calculator.py",
            expected_behavior="add(a, b) returns a + b",
            verification_plan="pytest tests/test_calc.py",
            status=self.status,
            patch_candidate=patch,
            validation=validation,
            run_id=run_id,
        )


# =============================================================================
# Intake Command Derivation Tests
# =============================================================================

def test_extract_command_from_test_spec(test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
        expected_failure_pattern="AssertionError",
    )
    cmd = extract_reproduction_command_from_intake(spec)
    assert cmd is not None
    assert cmd.args == ["pytest", "tests/test_calc.py"]
    assert cmd.expected_output_patterns == ["AssertionError"]


def test_extract_command_from_issue_hints(test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_issue(
        repo_path=repo,
        title="Issue with parser",
        issue_text="Parser crashes on empty input",
        issue_id="ISSUE-42",
        reproduction_hints=["pytest tests/test_parser.py -k test_empty"],
        affected_files=["src/calculator.py"],
    )
    cmd = extract_reproduction_command_from_intake(spec)
    assert cmd is not None
    assert cmd.args == ["pytest", "tests/test_parser.py", "-k", "test_empty"]


def test_extract_command_rejects_unsafe_executable(test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    # Construct intake directly to test extraction filtering of forbidden executable
    spec = WorkflowIntakeSpec(
        intake_id="intake_unsafe_bin",
        target_repo=str(repo),
        base_commit_sha=sha,
        source_type=IntakeSourceType.TEST_SPECIFICATION,
        title="Unsafe bin",
        description="curl is not approved",
        test_payload=TestSpecIntakePayload(test_file="tests/test_calc.py", test_command="curl -X POST http://malicious.com"),
    )
    cmd = extract_reproduction_command_from_intake(spec)
    assert cmd is None


def test_extract_command_rejects_forbidden_argument_patterns(test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    # Construct intake directly to test argument pattern rejection
    spec = WorkflowIntakeSpec(
        intake_id="intake_unsafe_pat",
        target_repo=str(repo),
        base_commit_sha=sha,
        source_type=IntakeSourceType.TEST_SPECIFICATION,
        title="Unsafe arg",
        description="Argument injection",
        test_payload=TestSpecIntakePayload(test_file="tests/test_calc.py", test_command="pytest $(whoami)"),
    )
    cmd = extract_reproduction_command_from_intake(spec)
    assert cmd is None


# =============================================================================
# Canonical Finding Synchronization Tests
# =============================================================================

def test_ensure_finding_in_store(store: ForgeStore, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_issue(
        repo_path=repo,
        title="Test defect",
        issue_text="Defect description",
        issue_id="ISSUE-99",
        affected_files=["src/calculator.py"],
    )
    finding = ensure_finding_in_store(store, spec, run_id="run_001")
    assert finding is not None
    assert finding.finding_id == f"find_{spec.intake_id}"
    assert finding.title == "Test defect"

    # Second call retrieves identical record idempotently
    finding2 = ensure_finding_in_store(store, spec, run_id="run_001")
    assert finding2.finding_id == finding.finding_id


# =============================================================================
# Evidence Validation & Provenance Verification Tests
# =============================================================================

def _build_valid_evidence(
    run_id: str,
    finding_id: str,
    commit_sha: str,
    outcome: str = "reproduced",
    exit_code: int = 1,
) -> EvidenceRecord:
    content = (
        "Reproduction Command: pytest tests/test_calc.py\n"
        f"Observed Exit Code: {exit_code}\n"
        f"Classified Outcome: {outcome}\n"
        "--- STDOUT ---\n\n--- STDERR ---\nAssertionError: failed"
    )
    return create_evidence(
        run_id=run_id,
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Reproduction attempt",
        content=content,
        finding_id=finding_id,
        details={
            "outcome": outcome,
            "status": outcome,
            "exit_code": exit_code,
            "commit_sha": commit_sha,
        },
    )


def test_validate_evidence_missing_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    with pytest.raises(MissingEvidenceError, match="No reproduction evidence found"):
        validate_reproduction_evidence(None, wf)


def test_validate_evidence_type_mismatch_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = create_evidence(
        run_id="run_001",
        evidence_type=EvidenceType.SOURCE_EXCERPT,
        description="Source excerpt",
        content="some code",
    )
    with pytest.raises(MismatchedEvidenceError, match="expected 'reproduction_result'"):
        validate_reproduction_evidence(ev, wf)


def test_validate_evidence_corrupted_empty_content_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = EvidenceRecord(
        evidence_id="evi_001",
        run_id="run_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="   ",
        details={"outcome": "reproduced"},
    )
    with pytest.raises(CorruptedEvidenceError, match="content is empty"):
        validate_reproduction_evidence(ev, wf)


def test_validate_evidence_missing_structural_markers_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = EvidenceRecord(
        evidence_id="evi_001",
        run_id="run_001",
        evidence_type=EvidenceType.REPRODUCTION_RESULT,
        description="Repro",
        content="I believe this is a bug but no markers here",
        details={"outcome": "reproduced"},
    )
    with pytest.raises(CorruptedEvidenceError, match="corrupted or over-redacted"):
        validate_reproduction_evidence(ev, wf)


def test_validate_evidence_non_reproduced_outcome_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = _build_valid_evidence("run_001", "find_001", wf.base_commit_sha, outcome="failed")
    with pytest.raises(EvidenceChainError, match="strictly requires verified 'reproduced' outcome"):
        validate_reproduction_evidence(ev, wf)


def test_validate_evidence_finding_mismatch_raises(test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id=spec.intake_id,
        target_repo=str(repo),
        base_commit_sha=sha,
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = _build_valid_evidence("run_001", "find_DIFFERENT_ID", sha)
    with pytest.raises(MismatchedEvidenceError, match="expected 'find_"):
        validate_reproduction_evidence(ev, wf, intake=spec)


def test_validate_evidence_stale_commit_revision_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="1111111111111111111111111111111111111111",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = _build_valid_evidence("run_001", "find_001", "2222222222222222222222222222222222222222")
    with pytest.raises(StaleRevisionEvidenceError, match="does not match workflow base commit"):
        validate_reproduction_evidence(ev, wf)


def test_validate_evidence_untrusted_oracle_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = _build_valid_evidence("run_001", "find_001", wf.base_commit_sha)

    # Construct case with untrusted provenance
    req = ReproductionRequest(
        request_id="req_001",
        finding_id="find_001",
        target_repo="/tmp/repo",
        commit_sha=wf.base_commit_sha,
        hypothesis="Defect in calculator",
        command=ReproductionCommand(args=["pytest"]),
    )
    case = ReproductionCase(
        case_id="case_001",
        finding_id="find_001",
        run_id="run_001",
        target_repo="/tmp/repo",
        target_commit_sha=wf.base_commit_sha,
        contract=req,
        policy_version="1.0.0",
        status=CaseStatus.REPRODUCED,
        provenance_map={"execution": FieldProvenance.UNTRUSTED_LLM},
    )
    with pytest.raises(UntrustedOracleError, match="untrusted provenance: 'untrusted_llm'"):
        validate_reproduction_evidence(ev, wf, case=case)


def test_validate_evidence_exit_code_0_contradiction_raises():
    wf = EngineeringWorkflow(
        workflow_id="wf_001",
        intake_id="intake_001",
        target_repo="/tmp/repo",
        base_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        state=EngineeringWorkflowState.REPRODUCED,
    )
    ev = _build_valid_evidence("run_001", "find_001", wf.base_commit_sha, exit_code=0)
    with pytest.raises(EvidenceTamperedError, match="records exit code 0 .* contradicts"):
        validate_reproduction_evidence(ev, wf)


# =============================================================================
# Reproduction Execution & Outcome Mapping Tests
# =============================================================================

def test_reproduce_defect_outcome_reproduced(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)
    assert wf.state == EngineeringWorkflowState.REPO_ANALYZED

    harness = MockReproductionHarness(outcome=ReproductionOutcome.REPRODUCED, exit_code=1, stderr="AssertionError: 3 != 1")
    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    assert wf_repro.state == EngineeringWorkflowState.REPRODUCED
    assert wf_repro.repro_case_id is not None

    events = orchestrator.list_events(wf.workflow_id)
    stages = [e.stage for e in events]
    assert "start_reproduction" in stages
    assert "defect_reproduction" in stages


def test_reproduce_defect_outcome_not_reproduced(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.NOT_REPRODUCED, exit_code=0, stderr="")
    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    assert wf_repro.state == EngineeringWorkflowState.REPRO_FAILED
    assert "no defect violation" in (wf_repro.error_message or "").lower() or "not_reproduced" in (wf_repro.error_message or "").lower()


def test_reproduce_defect_outcome_timeout(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.TIMEOUT, exit_code=None, stderr="Timeout reached")
    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    assert wf_repro.state == EngineeringWorkflowState.REPRO_FAILED


def test_reproduce_defect_outcome_flaky(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.FLAKY, exit_code=1, stderr="Flaky behavior")
    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    assert wf_repro.state == EngineeringWorkflowState.REPRO_INCONCLUSIVE


def test_reproduce_defect_outcome_sandbox_unavailable_fails_closed(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE, exit_code=None, stderr="")
    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    # Never fall back to host execution: must land in REPRO_BLOCKED
    assert wf_repro.state == EngineeringWorkflowState.REPRO_BLOCKED
    assert "docker unavailable" in (wf_repro.error_message or "").lower() or "sandbox unavailable" in (wf_repro.error_message or "").lower() or "blocked" in (wf_repro.error_message or "").lower()


def test_reproduce_defect_no_command_inconclusive(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    # Intake with no commands or hints
    spec = WorkflowIntakeSpec(
        intake_id="intake_no_cmd",
        target_repo=str(repo),
        base_commit_sha=sha,
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        title="Ambiguous issue",
        description="Something is broken without repro commands",
        issue_payload=IssueIntakePayload(
            issue_id="ISSUE-000",
            issue_text="Something is broken without repro commands",
            reproduction_hints=[],
        ),
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo)
    assert wf_repro.state == EngineeringWorkflowState.REPRO_INCONCLUSIVE


def test_reproduce_defect_revision_drift_detection(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    # Introduce a new commit in the workspace
    (repo / "new_file.txt").write_text("drift", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Drift commit"], cwd=repo, check=True, capture_output=True)

    with pytest.raises(RevisionDriftError, match="Revision drift detected"):
        orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo)


def test_reproduce_defect_idempotency_reuse(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.REPRODUCED, exit_code=1, stderr="AssertionError: 3 != 1")
    wf_first = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)
    assert wf_first.state == EngineeringWorkflowState.REPRODUCED
    assert len(harness.executed_commands) == 1

    # Calling reproduce_defect again on the same reproduced workflow is idempotent
    wf_second = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)
    assert wf_second.state == EngineeringWorkflowState.REPRODUCED
    assert len(harness.executed_commands) == 1  # No second execution


# =============================================================================
# Repair Planning Chaining Tests
# =============================================================================

def test_plan_repair_fails_if_not_reproduced(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)
    assert wf.state == EngineeringWorkflowState.REPO_ANALYZED

    with pytest.raises(InvalidWorkflowTransitionError, match="must be in 'reproduced' state"):
        orchestrator.plan_repair(wf.workflow_id, workspace_path=repo)


def test_plan_repair_success(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.REPRODUCED, exit_code=1, stderr="AssertionError: 3 != 1")
    wf_repro = orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)
    assert wf_repro.state == EngineeringWorkflowState.REPRODUCED

    planner = MockRepairPlanner(status=RepairPlanStatus.VALIDATED)
    wf_plan = orchestrator.plan_repair(wf.workflow_id, workspace_path=repo, planner=planner)

    assert wf_plan.state == EngineeringWorkflowState.REPAIR_PENDING
    assert wf_plan.repair_plan_id == "plan_mock_001"

    events = orchestrator.list_events(wf.workflow_id)
    stages = [e.stage for e in events]
    assert "start_repair_planning" in stages
    assert "repair_planning_completed" in stages


def test_plan_repair_rejected_plan_maps_to_repair_failed(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.REPRODUCED, exit_code=1, stderr="AssertionError: 3 != 1")
    orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    planner = MockRepairPlanner(status=RepairPlanStatus.REJECTED, violations=["Budget exceeded: >100 lines changed"])
    wf_plan = orchestrator.plan_repair(wf.workflow_id, workspace_path=repo, planner=planner)

    assert wf_plan.state == EngineeringWorkflowState.REPAIR_FAILED
    assert "budget exceeded" in (wf_plan.error_message or "").lower()


# =============================================================================
# End-to-End Workflow Advancement Tests
# =============================================================================

def test_advance_workflow_happy_path(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    # Start without auto_advance -> CREATED state
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=False)
    assert wf.state == EngineeringWorkflowState.CREATED

    harness = MockReproductionHarness(outcome=ReproductionOutcome.REPRODUCED, exit_code=1, stderr="AssertionError: 3 != 1")
    planner = MockRepairPlanner(status=RepairPlanStatus.VALIDATED)

    wf_final = orchestrator.advance_workflow(
        wf.workflow_id,
        workspace_path=repo,
        harness=harness,
        planner=planner,
    )

    assert wf_final.state == EngineeringWorkflowState.REPAIR_PENDING
    assert wf_final.repro_case_id is not None
    assert wf_final.repair_plan_id is not None


def test_advance_workflow_halts_on_repro_blocked(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=False)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE)
    wf_advanced = orchestrator.advance_workflow(wf.workflow_id, workspace_path=repo, harness=harness)

    assert wf_advanced.state == EngineeringWorkflowState.REPRO_BLOCKED
    assert wf_advanced.repair_plan_id is None


# =============================================================================
# Target Repository Immutability Tests
# =============================================================================

def test_target_repository_immutability(orchestrator: EngineeringWorkflowOrchestrator, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orchestrator.start_workflow(spec, target_repo=repo, auto_advance=True)

    harness = MockReproductionHarness(outcome=ReproductionOutcome.REPRODUCED, exit_code=1, stderr="AssertionError: 3 != 1")
    orchestrator.reproduce_defect(wf.workflow_id, workspace_path=repo, harness=harness)

    planner = MockRepairPlanner(status=RepairPlanStatus.VALIDATED)
    orchestrator.plan_repair(wf.workflow_id, workspace_path=repo, planner=planner)

    # Verify target repo git status remains completely clean
    res = subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True)
    assert res.stdout.strip() == ""

    # Verify HEAD commit SHA unchanged
    res_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    assert res_sha.stdout.strip() == sha


# =============================================================================
# CLI Integration Tests
# =============================================================================

def test_cli_workflow_repro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    db_path = tmp_path / "cli_test.db"
    init_database(db_path)
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

    store = ForgeStore(db_path=db_path)
    orch = EngineeringWorkflowOrchestrator(store=store)
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orch.start_workflow(spec, target_repo=repo, auto_advance=True)

    # Run CLI repro command (will execute Mock via harness or sandbox check)
    result = runner.invoke(app, ["workflow", "repro", wf.workflow_id, "--repo", str(repo)])
    assert result.exit_code == 0
    assert "Engineering Workflow" in result.output


def test_cli_workflow_plan_repair_not_reproduced_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    db_path = tmp_path / "cli_test.db"
    init_database(db_path)
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

    store = ForgeStore(db_path=db_path)
    orch = EngineeringWorkflowOrchestrator(store=store)
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orch.start_workflow(spec, target_repo=repo, auto_advance=True)

    result = runner.invoke(app, ["workflow", "plan-repair", wf.workflow_id, "--repo", str(repo)])
    assert result.exit_code == 1
    assert "must be in 'reproduced' state" in result.output


def test_cli_workflow_advance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_git_repo: Tuple[Path, str]):
    repo, sha = test_git_repo
    db_path = tmp_path / "cli_test.db"
    init_database(db_path)
    monkeypatch.setenv("FORGE_DB_PATH", str(db_path))

    store = ForgeStore(db_path=db_path)
    orch = EngineeringWorkflowOrchestrator(store=store)
    spec = create_intake_from_test_spec(
        repo_path=repo,
        test_file="tests/test_calc.py",
        test_command="pytest tests/test_calc.py",
    )
    wf = orch.start_workflow(spec, target_repo=repo, auto_advance=False)

    result = runner.invoke(app, ["workflow", "advance", wf.workflow_id, "--repo", str(repo)])
    assert result.exit_code == 0
    assert "Engineering Workflow" in result.output
