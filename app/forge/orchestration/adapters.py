"""Phase 13 M1: Stage Adapters for Autonomous Engineering Orchestration.

Provides modular adapters decoupling stage engines from workflow orchestration.
Includes production adapters wrapping Phase 12 engines as well as deterministic
fake adapters for adversarial testing and simulation.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
)
from forge.causality.schemas import (
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairPlan,
    RepairPlanStatus,
    RepairRiskAssessment,
    RepairStrategy,
    RequiredChange,
    RiskLikelihood,
    RiskSeverity,
    RollbackPlan,
    StrategyComplexity,
    StrategyInvasiveness,
    StrategyReversibility,
    VerificationPlan,
)
from forge.repair_execution.approval import compute_plan_digest
from forge.repair_execution.executor import ControlledRepairExecutor
from forge.repair_execution.ledger import ApprovalLedger
from forge.repair_execution.schemas import (
    ExecutionLimits,
    ExecutionStatus,
    FailureClassification,
    RepairApproval,
    RepairExecutionResult,
    RepairWorkspace,
    RollbackStatus,
    WorkspaceStatus,
)
from forge.repair_execution.workspace import RepairWorkspaceManager, compute_tree_digest
from forge.repair_verification.executor import RepairVerificationExecutor
from forge.repair_verification.schemas import (
    RegressionClassification,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.repair_quality.executor import RepairQualityExecutor
from forge.repair_quality.schemas import (
    ConfidenceClassification,
    QualityGateDecision,
    QualityStatus,
    RepairQualityMetrics,
    RepairQualityPolicy,
    RepairQualityRequest,
    RepairQualityResult,
)
from forge.orchestration.schemas import WorkflowContext
from forge.schemas import FindingConfidence, FindingSeverity


# ---------------------------------------------------------------------------
# Abstract Stage Adapters
# ---------------------------------------------------------------------------


class DiagnosisAdapter(ABC):
    """Abstract adapter for defect diagnosis stage."""

    @abstractmethod
    def run_diagnosis(
        self,
        context: WorkflowContext,
        input_data: Optional[DiagnosisInput] = None,
    ) -> DiagnosisResult:
        """Execute defect diagnosis."""


class RootCauseAdapter(ABC):
    """Abstract adapter for root-cause analysis stage."""

    @abstractmethod
    def run_root_cause(
        self,
        context: WorkflowContext,
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> RootCauseReport:
        """Execute root-cause analysis."""


class PlanningAdapter(ABC):
    """Abstract adapter for repair planning stage."""

    @abstractmethod
    def run_planning(
        self,
        context: WorkflowContext,
        root_cause: Optional[RootCauseReport] = None,
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> RepairPlan:
        """Generate structured repair plan."""


class ExecutionAdapter(ABC):
    """Abstract adapter for controlled repair execution stage."""

    @abstractmethod
    def run_execution(
        self,
        context: WorkflowContext,
        plan: RepairPlan,
        approval: RepairApproval,
        workspace: Optional[RepairWorkspace] = None,
    ) -> Tuple[RepairExecutionResult, RepairWorkspace]:
        """Execute repair inside isolated workspace."""


class VerificationAdapter(ABC):
    """Abstract adapter for repair verification stage."""

    @abstractmethod
    def run_verification(
        self,
        context: WorkflowContext,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        policy: Optional[VerificationPolicy] = None,
    ) -> VerificationResult:
        """Verify applied repair."""


class QualityAdapter(ABC):
    """Abstract adapter for repair quality evaluation stage."""

    @abstractmethod
    def run_quality(
        self,
        context: WorkflowContext,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        plan: RepairPlan,
        verification_result: VerificationResult,
        diagnosis: Optional[DiagnosisResult] = None,
        root_cause: Optional[RootCauseReport] = None,
        policy: Optional[RepairQualityPolicy] = None,
    ) -> RepairQualityResult:
        """Evaluate multi-dimensional repair quality."""


# ---------------------------------------------------------------------------
# Production Adapters (Wrapping Phase 12 Engines)
# ---------------------------------------------------------------------------


class DefaultDiagnosisAdapter(DiagnosisAdapter):
    """Production diagnosis adapter wrapping EvidenceDrivenDiagnosisEngine."""

    def __init__(self, workspace_path: Optional[Path] = None) -> None:
        self.workspace_path = workspace_path

    def run_diagnosis(
        self,
        context: WorkflowContext,
        input_data: Optional[DiagnosisInput] = None,
    ) -> DiagnosisResult:
        from forge.diagnosis.engine import EvidenceDrivenDiagnosisEngine

        repo_root = self.workspace_path or Path(context.repository_path)
        engine = EvidenceDrivenDiagnosisEngine(
            workspace_path=repo_root,
            run_id=context.run_id,
        )
        diag_input = input_data or context.defect_input or DiagnosisInput()
        return engine.diagnose(diag_input)


class DefaultRootCauseAdapter(RootCauseAdapter):
    """Production root-cause adapter wrapping RootCauseAnalysisEngine."""

    def __init__(self, workspace_path: Optional[Path] = None) -> None:
        self.workspace_path = workspace_path

    def run_root_cause(
        self,
        context: WorkflowContext,
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> RootCauseReport:
        from forge.causality.engine import RootCauseAnalysisEngine

        repo_root = self.workspace_path or Path(context.repository_path)
        engine = RootCauseAnalysisEngine(workspace_path=repo_root)
        return engine.analyze_root_cause(diagnosis=diagnosis)


class DefaultPlanningAdapter(PlanningAdapter):
    """Production planning adapter wrapping RepairPlanningEngine."""

    def __init__(self, workspace_path: Optional[Path] = None) -> None:
        self.workspace_path = workspace_path

    def run_planning(
        self,
        context: WorkflowContext,
        root_cause: Optional[RootCauseReport] = None,
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> RepairPlan:
        from forge.repair_planning.planner import RepairPlanningEngine

        repo_root = self.workspace_path or Path(context.repository_path)
        engine = RepairPlanningEngine(workspace_path=repo_root)
        return engine.plan_repair(
            root_cause_report=root_cause,
            diagnosis=diagnosis,
        )


class DefaultExecutionAdapter(ExecutionAdapter):
    """Production execution adapter wrapping ControlledRepairExecutor."""

    def __init__(
        self,
        workspace_manager: Optional[RepairWorkspaceManager] = None,
        executor: Optional[ControlledRepairExecutor] = None,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.executor = executor

    def run_execution(
        self,
        context: WorkflowContext,
        plan: RepairPlan,
        approval: RepairApproval,
        workspace: Optional[RepairWorkspace] = None,
    ) -> Tuple[RepairExecutionResult, RepairWorkspace]:
        repo_root = Path(context.repository_path)
        wm = self.workspace_manager or RepairWorkspaceManager()
        active_ws = workspace or wm.create_workspace(repo_root)

        exec_engine = self.executor or ControlledRepairExecutor(
            repo_path=repo_root,
            workspace_manager=wm,
            trusted_approver_identities=context.trusted_approvers,
        )
        result = exec_engine.execute_repair(
            plan=plan,
            approval=approval,
            limits=context.execution_limits,
            workspace=active_ws,
        )
        return result, active_ws


class DefaultVerificationAdapter(VerificationAdapter):
    """Production verification adapter wrapping RepairVerificationExecutor."""

    def __init__(self, executor: Optional[RepairVerificationExecutor] = None) -> None:
        self.executor = executor or RepairVerificationExecutor()

    def run_verification(
        self,
        context: WorkflowContext,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        policy: Optional[VerificationPolicy] = None,
    ) -> VerificationResult:
        active_policy = policy or context.verification_policy or VerificationPolicy()
        result, _ = self.executor.verify_repair(
            execution_result=execution_result,
            workspace=workspace,
            policy=active_policy,
        )
        return result


class DefaultQualityAdapter(QualityAdapter):
    """Production quality adapter wrapping RepairQualityExecutor."""

    def __init__(self, executor: Optional[RepairQualityExecutor] = None) -> None:
        self.executor = executor

    def run_quality(
        self,
        context: WorkflowContext,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        plan: RepairPlan,
        verification_result: VerificationResult,
        diagnosis: Optional[DiagnosisResult] = None,
        root_cause: Optional[RootCauseReport] = None,
        policy: Optional[RepairQualityPolicy] = None,
    ) -> RepairQualityResult:
        active_policy = policy or context.quality_policy or RepairQualityPolicy()
        exec_engine = self.executor or RepairQualityExecutor(policy=active_policy)
        plan_digest = compute_plan_digest(plan)

        request = RepairQualityRequest(
            request_id=f"rq_{uuid.uuid4().hex[:12]}",
            execution_id=execution_result.execution_id,
            workspace_id=workspace.workspace_id,
            repair_plan_id=plan.plan_id,
            repair_plan_digest=plan_digest,
            applied_patch_digest=execution_result.applied_patch_digest or "",
            baseline_tree_digest=workspace.baseline_tree_digest,
            verification_id=verification_result.verification_id,
            diagnosis_id=diagnosis.diagnosis_id if diagnosis else None,
            root_cause_report_id=root_cause.report_id if root_cause else None,
            repository_path=str(context.repository_path),
            policy=active_policy,
        )
        return exec_engine.evaluate_quality(request)


# ---------------------------------------------------------------------------
# Fake / Test Adapters (Deterministic Simulation without External Processes)
# ---------------------------------------------------------------------------


class FakeDiagnosisAdapter(DiagnosisAdapter):
    """Configurable fake diagnosis adapter for deterministic tests."""

    def __init__(
        self,
        status: DiagnosisStatus = DiagnosisStatus.CONFIRMED,
        defect_category: DefectCategory = DefectCategory.LOGIC_ERROR,
        confidence: FindingConfidence = FindingConfidence.HIGH,
        should_raise: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.defect_category = defect_category
        self.confidence = confidence
        self.should_raise = should_raise

    def run_diagnosis(
        self,
        context: WorkflowContext,
        input_data: Optional[DiagnosisInput] = None,
    ) -> DiagnosisResult:
        if self.should_raise:
            raise self.should_raise

        loc = CandidateLocation(
            file_path="src/logic.py",
            start_line=10,
            end_line=20,
            source=LocationSource.TEST_FAILURE,
            confidence_weight=0.90,
            reason="Traceback frame pointed to logic.py line 15",
            is_verified_on_disk=True,
        )
        hyp = DiagnosisHypothesis(
            hypothesis_id="hyp_001",
            description="Simulated defect in logic",
            suspected_location=loc,
            defect_category=self.defect_category,
            confidence=self.confidence,
            confidence_score=0.90 if self.confidence == FindingConfidence.HIGH else 0.50,
            suggested_verification_method="Run pytest tests/test_logic.py",
        )
        return DiagnosisResult(
            diagnosis_id=f"diag_{uuid.uuid4().hex[:8]}",
            run_id=context.run_id,
            input_summary="Simulated defect input",
            failing_behavior="Off-by-one boundary failure",
            status=self.status,
            defect_category=self.defect_category,
            severity=FindingSeverity.HIGH,
            confidence=self.confidence,
            confidence_score=0.90 if self.confidence == FindingConfidence.HIGH else 0.50,
            root_cause_locations=[loc],
            hypotheses=[hyp],
            confidence_explanation="High confidence from simulated test failure",
            verification_requirements=["Run pytest"],
            recommended_next_step="Plan repair",
        )


class FakeRootCauseAdapter(RootCauseAdapter):
    """Configurable fake root-cause adapter for deterministic tests."""

    def __init__(
        self,
        status: CausalStatus = CausalStatus.PROVEN,
        should_raise: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.should_raise = should_raise

    def run_root_cause(
        self,
        context: WorkflowContext,
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> RootCauseReport:
        if self.should_raise:
            raise self.should_raise

        root_node = CausalNode(
            node_id="node_001",
            file_path="src/logic.py",
            start_line=10,
            end_line=20,
            node_type=CausalNodeType.FUNCTION,
            role=CausalRole.ROOT_CAUSE,
            description="Root cause condition in logic.py:10",
            is_verified_on_disk=True,
        )
        symptom_node = CausalNode(
            node_id="node_002",
            file_path="src/logic.py",
            start_line=15,
            end_line=16,
            node_type=CausalNodeType.STATEMENT,
            role=CausalRole.SYMPTOM,
            description="Index error observed here",
            is_verified_on_disk=True,
        )
        edge = CausalEdge(
            edge_id="edge_001",
            source_node_id="node_001",
            target_node_id="node_002",
            relationship=CausalRelationship.TRIGGERS_FAILURE,
            causal_explanation="Condition triggers index failure",
            confidence=FindingConfidence.HIGH,
            confidence_score=0.95,
            is_directly_verified=True,
        )
        chain = CausalChain(
            chain_id="chain_001",
            root_node_id="node_001",
            symptom_node_id="node_002",
            node_ids=["node_001", "node_002"],
            edge_ids=["edge_001"],
            summary="Simulated causal failure chain",
            confidence=FindingConfidence.HIGH,
            confidence_score=0.95,
            is_fully_verified=True,
        )
        return RootCauseReport(
            report_id=f"rc_{uuid.uuid4().hex[:8]}",
            run_id=context.run_id,
            diagnosis_id=diagnosis.diagnosis_id if diagnosis else None,
            status=self.status,
            primary_root_cause=root_node,
            nodes=[root_node, symptom_node],
            edges=[edge],
            causal_chains=[chain],
            confidence=FindingConfidence.HIGH,
            confidence_score=0.95,
        )


class FakePlanningAdapter(PlanningAdapter):
    """Configurable fake repair planning adapter for deterministic tests."""

    def __init__(
        self,
        plan_status: RepairPlanStatus = RepairPlanStatus.ACTIONABLE,
        affected_files: Optional[List[str]] = None,
        should_raise: Optional[Exception] = None,
    ) -> None:
        self.plan_status = plan_status
        self.affected_files = affected_files or ["src/logic.py"]
        self.should_raise = should_raise

    def run_planning(
        self,
        context: WorkflowContext,
        root_cause: Optional[RootCauseReport] = None,
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> RepairPlan:
        if self.should_raise:
            raise self.should_raise

        strategy = RepairStrategy(
            strategy_id="strat_001",
            title="Fix off-by-one error",
            description="Adjust loop boundary",
            approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
            affected_files=self.affected_files,
            complexity=StrategyComplexity.LOW,
            invasiveness=StrategyInvasiveness.MINIMAL,
            reversibility=StrategyReversibility.HIGH,
            is_recommended=True,
            score=0.90,
        )
        return RepairPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            status=self.plan_status,
            summary="Simulated repair plan",
            problem_statement="Off-by-one loop index",
            repair_objective="Prevent index out of bounds",
            selected_strategy_id="strat_001",
            candidate_strategies=[strategy],
            affected_files=self.affected_files,
            required_changes=[
                RequiredChange(
                    file_path=self.affected_files[0],
                    change_type="modify_condition",
                    description="Fix boundary condition",
                    rationale="Prevent IndexOutOfBounds",
                )
            ],
            risk_assessment=RepairRiskAssessment(
                overall_risk=RiskSeverity.LOW,
                likelihood=RiskLikelihood.LOW,
                risk_score=0.15,
                explanation="Low risk local fix",
            ),
            verification_plan=VerificationPlan(
                existing_targeted_tests=["tests/test_logic.py"],
            ),
            rollback_plan=RollbackPlan(
                changes_to_reverse=["Revert loop condition change"],
                rollback_triggers=["Verification failure"],
            ),
            confidence=FindingConfidence.HIGH,
            approval_required=True,
            execution_permitted=False,
        )


class FakeExecutionAdapter(ExecutionAdapter):
    """Configurable fake execution adapter for deterministic tests."""

    def __init__(
        self,
        status: ExecutionStatus = ExecutionStatus.APPLIED,
        diff_summary: str = "Applied patch to src/logic.py",
        should_raise: Optional[Exception] = None,
        ledger: Optional[ApprovalLedger] = None,
    ) -> None:
        self.status = status
        self.diff_summary = diff_summary
        self.should_raise = should_raise
        self.ledger = ledger

    def run_execution(
        self,
        context: WorkflowContext,
        plan: RepairPlan,
        approval: RepairApproval,
        workspace: Optional[RepairWorkspace] = None,
    ) -> Tuple[RepairExecutionResult, RepairWorkspace]:
        if self.should_raise:
            raise self.should_raise

        repo_root = Path(context.repository_path)
        plan_digest = compute_plan_digest(plan)
        ws = workspace or RepairWorkspace(
            workspace_id=f"ws_{uuid.uuid4().hex[:8]}",
            original_repository_reference=str(repo_root),
            isolated_workspace_path=str(repo_root),
            baseline_commit="0" * 40,
            baseline_tree_digest="0" * 64,
            allowed_paths=plan.affected_files,
            workspace_status=WorkspaceStatus.READY,
        )

        exec_id = f"exec_{uuid.uuid4().hex[:8]}"

        # Atomically record consumption in ledger if present
        if self.ledger:
            recorded = self.ledger.record_consumed(approval.approval_id, exec_id)
            if not recorded:
                res = RepairExecutionResult(
                    execution_id=exec_id,
                    repair_plan_id=plan.plan_id,
                    execution_status=ExecutionStatus.BLOCKED,
                    workspace_id=ws.workspace_id,
                    diff_summary=f"Execution blocked: Approval '{approval.approval_id}' was already consumed concurrently or revoked.",
                    applied_patch_digest="0" * 64,
                    verification_pending=True,
                    rollback_status=RollbackStatus.NOT_TRIGGERED,
                    failure_classification=FailureClassification.UNAUTHORIZED_EXECUTION,
                    approval_reference=approval.approval_id,
                )
                return res, ws

        res = RepairExecutionResult(
            execution_id=exec_id,
            repair_plan_id=plan.plan_id,
            execution_status=self.status,
            workspace_id=ws.workspace_id,
            diff_summary=self.diff_summary,
            applied_patch_digest=plan_digest,
            verification_pending=True if self.status == ExecutionStatus.APPLIED else False,
            rollback_status=RollbackStatus.NOT_TRIGGERED,
            failure_classification=FailureClassification.NONE if self.status == ExecutionStatus.APPLIED else FailureClassification.UNAUTHORIZED_EXECUTION,
            approval_reference=approval.approval_id,
        )
        return res, ws


class FakeVerificationAdapter(VerificationAdapter):
    """Configurable fake verification adapter for deterministic tests."""

    def __init__(
        self,
        status: VerificationStatus = VerificationStatus.PASSED,
        regression: RegressionClassification = RegressionClassification.NO_REGRESSION_OBSERVED,
        gate_decision: str = "PASSED",
        should_raise: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.regression = regression
        self.gate_decision = gate_decision
        self.should_raise = should_raise

    def run_verification(
        self,
        context: WorkflowContext,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        policy: Optional[VerificationPolicy] = None,
    ) -> VerificationResult:
        if self.should_raise:
            raise self.should_raise

        patch_digest = execution_result.applied_patch_digest or ("0" * 64)
        return VerificationResult(
            verification_id=f"vrf_{uuid.uuid4().hex[:8]}",
            execution_id=execution_result.execution_id,
            workspace_id=workspace.workspace_id,
            repair_plan_id=execution_result.repair_plan_id,
            repair_plan_digest=patch_digest,
            applied_patch_digest=patch_digest,
            baseline_tree_digest=workspace.baseline_tree_digest,
            verification_status=self.status,
            regression_classification=self.regression,
            required_checks_completed=["chk_targeted_001"] if self.status == VerificationStatus.PASSED else [],
            required_checks_failed=["chk_targeted_001"] if self.status != VerificationStatus.PASSED else [],
            gate_decision=self.gate_decision,
            requires_human_review=True,
        )


class FakeQualityAdapter(QualityAdapter):
    """Configurable fake quality adapter for deterministic tests."""

    def __init__(
        self,
        status: QualityStatus = QualityStatus.PASSED,
        gate_decision: QualityGateDecision = QualityGateDecision.PASSED,
        overall_score: float = 0.92,
        policy_violations: Optional[List[str]] = None,
        should_raise: Optional[Exception] = None,
    ) -> None:
        self.status = status
        self.gate_decision = gate_decision
        self.overall_score = overall_score
        self.policy_violations = policy_violations or []
        self.should_raise = should_raise

    def run_quality(
        self,
        context: WorkflowContext,
        execution_result: RepairExecutionResult,
        workspace: RepairWorkspace,
        plan: RepairPlan,
        verification_result: VerificationResult,
        diagnosis: Optional[DiagnosisResult] = None,
        root_cause: Optional[RootCauseReport] = None,
        policy: Optional[RepairQualityPolicy] = None,
    ) -> RepairQualityResult:
        if self.should_raise:
            raise self.should_raise

        active_policy = policy or RepairQualityPolicy()
        policy_digest = active_policy.compute_digest()
        patch_digest = execution_result.applied_patch_digest or ("0" * 64)

        metrics = RepairQualityMetrics(
            patch_lines_added=5,
            patch_lines_removed=2,
            patch_total_lines=7,
            files_changed_count=len(plan.affected_files),
            change_concentration=1.0,
            verification_completeness_ratio=1.0,
            regressions_detected_count=0 if self.status == QualityStatus.PASSED else 1,
            historical_data_available=True,
            historical_status="available",
        )

        return RepairQualityResult(
            result_id=f"qev_{uuid.uuid4().hex[:8]}",
            request_id=f"rqr_{uuid.uuid4().hex[:8]}",
            quality_status=self.status,
            overall_score=self.overall_score if self.status == QualityStatus.PASSED else 0.40,
            metrics=metrics,
            policy_violations=self.policy_violations or ([] if self.status == QualityStatus.PASSED else ["Score below threshold"]),
            risk_level=RiskSeverity.LOW if self.status == QualityStatus.PASSED else RiskSeverity.HIGH,
            confidence=ConfidenceClassification.HIGH,
            gate_decision=self.gate_decision,
            policy_version=active_policy.policy_version,
            policy_digest=policy_digest,
            requires_human_review=True,
        )
