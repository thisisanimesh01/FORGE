"""FORGE Repair Verification Package (Phase 12 Milestone 5).

Provides evidence-driven, reproducible, fail-closed verification of repaired workspaces:
- Discovery: Bounded test discovery mapping modified source files to targeted and regression tests.
- Selection: Intelligent test selector selecting targeted, regression, and static checks.
- Plan: Deterministic VerificationPlan binding digests, scope, and policy.
- Sandbox: Container sandbox adapter enforcing network disabling and fail-closed security.
- Parser: Structured test result parser with collection error and inconsistency detection.
- Baseline: Immutable baseline snapshot verification and execution.
- Regression: Deterministic regression engine categorizing NO_REGRESSION, REGRESSION, PRE_EXISTING, IMPROVED.
- Integrity: Multi-stage workspace, repository, and snapshot protection.
- Gates: Fail-closed gatekeeper controlling state transitions.
- Events: Append-only cryptographic audit event chain.
- Executor: End-to-end orchestration updating verification_pending only upon PASSED gates.
- Tools: 9 typed tools registered in ToolRegistry.
"""

from forge.repair_verification.baseline import BaselineVerificationRunner
from forge.repair_verification.discovery import DiscoveryResult, RepairTestDiscoverer
from forge.repair_verification.events import VerificationEventRecorder
from forge.repair_verification.executor import RepairVerificationExecutor
from forge.repair_verification.gates import VerificationGateKeeper
from forge.repair_verification.integrity import VerificationIntegrityChecker
from forge.repair_verification.regression import DeterministicRegressionEngine
from forge.repair_verification.result_parser import PytestResultParser
from forge.repair_verification.sandbox import VerificationSandbox
from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    CompareRepairVerificationInput,
    CompareRepairVerificationOutput,
    CreateRepairVerificationPlanInput,
    CreateRepairVerificationPlanOutput,
    DiscoverRepairTestsInput,
    DiscoverRepairTestsOutput,
    GetRepairVerificationSummaryInput,
    GetRepairVerificationSummaryOutput,
    InspectVerificationEvidenceInput,
    InspectVerificationEvidenceOutput,
    InspectVerificationEnvironmentInput,
    InspectVerificationEnvironmentOutput,
    RegressionClassification,
    RegressionFinding,
    RunBaselineVerificationInput,
    RunBaselineVerificationOutput,
    RunRepairVerificationInput,
    RunRepairVerificationOutput,
    SelectedCheck,
    SuiteExecutionSummary,
    TestExecutionResult,
    TestIdentifier,
    TestScopeCategory,
    ValidateVerificationIntegrityInput,
    ValidateVerificationIntegrityOutput,
    VerificationEvidenceRecord,
    VerificationEvent,
    VerificationEventType,
    VerificationPlan,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from forge.repair_verification.test_selection import IntelligentTestSelector
from forge.repair_verification.tools import RepairVerificationTools
from forge.repair_verification.verification_plan import VerificationPlanGenerator

__all__ = [
    "BaselineVerificationRunner",
    "CheckExecutionResult",
    "CheckOutcome",
    "CompareRepairVerificationInput",
    "CompareRepairVerificationOutput",
    "CreateRepairVerificationPlanInput",
    "CreateRepairVerificationPlanOutput",
    "DeterministicRegressionEngine",
    "DiscoverRepairTestsInput",
    "DiscoverRepairTestsOutput",
    "DiscoveryResult",
    "GetRepairVerificationSummaryInput",
    "GetRepairVerificationSummaryOutput",
    "InspectVerificationEvidenceInput",
    "InspectVerificationEvidenceOutput",
    "InspectVerificationEnvironmentInput",
    "InspectVerificationEnvironmentOutput",
    "IntelligentTestSelector",
    "PytestResultParser",
    "RegressionClassification",
    "RegressionFinding",
    "RepairTestDiscoverer",
    "RepairVerificationExecutor",
    "RepairVerificationTools",
    "RunBaselineVerificationInput",
    "RunBaselineVerificationOutput",
    "RunRepairVerificationInput",
    "RunRepairVerificationOutput",
    "SelectedCheck",
    "SuiteExecutionSummary",
    "TestExecutionResult",
    "TestIdentifier",
    "TestScopeCategory",
    "ValidateVerificationIntegrityInput",
    "ValidateVerificationIntegrityOutput",
    "VerificationEvidenceRecord",
    "VerificationEvent",
    "VerificationEventType",
    "VerificationGateKeeper",
    "VerificationIntegrityChecker",
    "VerificationPlan",
    "VerificationPlanGenerator",
    "VerificationPolicy",
    "VerificationResult",
    "VerificationSandbox",
    "VerificationStatus",
]
