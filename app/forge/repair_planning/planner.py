"""Phase 12 M3: Repair Planning Engine.

Orchestrates evidence-driven, risk-aware repair planning:
1. Ingests verified M2 RootCauseReport, M1 DiagnosisResult, or localized defect signals.
2. Audits evidence freshness and repository path containment.
3. Generates and ranks minimal candidate repair strategies.
4. Performs deterministic risk assessment and dependency reach analysis.
5. Constructs targeted verification and rollback plans.
6. Enforces non-negotiable safety boundaries (approval_required=True, execution_permitted=False).
7. Validates plan invariants before returning immutable, structured RepairPlans.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from forge.analysis.evidence import normalize_relative_path
from forge.causality.engine import RootCauseAnalysisEngine
from forge.causality.schemas import (
    CausalNode,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.diagnosis.schemas import DefectCategory, DiagnosisResult
from forge.repair_planning.evidence import RepairEvidenceCollector
from forge.repair_planning.risk import RepairRiskAnalyzer
from forge.repair_planning.rollback import RollbackPlanBuilder
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairEvidence,
    RepairPlan,
    RepairPlanStatus,
    RepairRiskAssessment,
    RepairStrategy,
    RequiredChange,
    RollbackPlan,
    StrategyInvasiveness,
    VerificationPlan,
)
from forge.repair_planning.strategies import RepairStrategyGenerator
from forge.repair_planning.validation import RepairPlanValidator
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.schemas import FindingConfidence, FindingSeverity
from forge.storage.store import ForgeStore


class RepairPlanningEngine:
    """Core orchestration engine for autonomous software repair planning."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        store: Optional[ForgeStore] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.incremental_index = incremental_index
        self.store = store

        self._evidence_collector = RepairEvidenceCollector(
            workspace_path=self.workspace_path,
            store=self.store,
        )
        self._strategy_generator = RepairStrategyGenerator(
            workspace_path=self.workspace_path,
            incremental_index=self.incremental_index,
        )
        self._risk_analyzer = RepairRiskAnalyzer(
            workspace_path=self.workspace_path,
            incremental_index=self.incremental_index,
        )
        self._rollback_builder = RollbackPlanBuilder(workspace_path=self.workspace_path)
        self._plan_validator = RepairPlanValidator(workspace_path=self.workspace_path)
        self._causality_engine: Optional[RootCauseAnalysisEngine] = None

        # In-memory plan cache for tool queries
        self._plans: Dict[str, RepairPlan] = {}

    def get_causality_engine(self) -> RootCauseAnalysisEngine:
        if self._causality_engine is None:
            self._causality_engine = RootCauseAnalysisEngine(
                workspace_path=self.workspace_path,
                incremental_index=self.incremental_index,
                store=self.store,
            )
        return self._causality_engine

    def get_cached_plan(self, plan_id: str) -> Optional[RepairPlan]:
        """Retrieve a previously generated plan from cache."""
        return self._plans.get(plan_id)

    def plan_repair(
        self,
        root_cause_report: Optional[RootCauseReport] = None,
        diagnosis: Optional[DiagnosisResult] = None,
        failing_symbol: Optional[str] = None,
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> RepairPlan:
        """Construct a validated RepairPlan from inputs."""
        # 1. Signal verification
        has_signal = any([
            root_cause_report is not None,
            diagnosis is not None,
            bool(failing_symbol),
            bool(file_path and line_number),
            bool(error_message),
        ])
        if not has_signal:
            raise ValueError(
                "RepairPlanningEngine requires at least one diagnostic or causal signal to construct a repair plan."
            )

        # 2. Derive RootCauseReport if not provided
        if root_cause_report is None:
            engine = self.get_causality_engine()
            try:
                root_cause_report = engine.analyze(
                    diagnosis=diagnosis,
                    failing_symbol=failing_symbol,
                    file_path=file_path,
                    line_number=line_number,
                    error_message=error_message,
                )
            except Exception:
                pass

        # 3. Evidence Collection & Freshness Audit
        evidence_list: List[RepairEvidence] = []
        stale_issues: List[str] = []

        if root_cause_report:
            evs, issues = self._evidence_collector.collect_from_root_cause(root_cause_report)
            evidence_list.extend(evs)
            stale_issues.extend(issues)

        if diagnosis:
            evs, issues = self._evidence_collector.collect_from_diagnosis(diagnosis)
            evidence_list.extend(evs)
            stale_issues.extend(issues)

        # 4. Resolve Target Locations & Symbols
        primary_node = root_cause_report.primary_root_cause if root_cause_report else None
        target_file = (
            primary_node.file_path
            if primary_node
            else (normalize_relative_path(file_path) if file_path else "unknown")
        )
        target_symbol = (
            primary_node.symbol_id
            if primary_node
            else (failing_symbol or "unknown")
        )

        affected_files = [target_file] if target_file != "unknown" else []
        affected_symbols = [target_symbol] if target_symbol != "unknown" else []

        # Audit target file existence and freshness on disk
        if target_file != "unknown":
            is_valid_target, _, target_err = self._evidence_collector.verify_file_location(target_file, None, None)
            if not is_valid_target:
                target_msg = f"Target file '{target_file}' is invalid or missing on disk: {target_err}"
                if target_msg not in stale_issues:
                    stale_issues.append(target_msg)

        # Audit any evidence marked not fresh or with content drift
        for ev in evidence_list:
            if not ev.is_fresh:
                ev_msg = f"Evidence '{ev.evidence_id}' referencing '{ev.file_path}' is marked not fresh."
                if ev_msg not in stale_issues:
                    stale_issues.append(ev_msg)

        # 5. Determine Certainty & Plan Status
        causal_status = root_cause_report.status if root_cause_report else CausalStatus.INSUFFICIENT_EVIDENCE
        limitations: List[str] = []

        if stale_issues:
            limitations.append(f"Stale evidence detected during planning: {'; '.join(stale_issues)}")
            # Mandatory downgrade: a pre-constructed PROVEN report cannot override stale source evidence on disk
            plan_status = RepairPlanStatus.INSUFFICIENT_EVIDENCE
            confidence = FindingConfidence.LOW
            limitations.append(
                "Plan downgraded to INSUFFICIENT_EVIDENCE: supporting repository evidence is stale, "
                "drifted, or unverified against current workspace disk state."
            )
        elif causal_status == CausalStatus.PROVEN:
            plan_status = RepairPlanStatus.ACTIONABLE
            confidence = FindingConfidence.HIGH
        elif causal_status == CausalStatus.PLAUSIBLE:
            plan_status = RepairPlanStatus.CONDITIONAL
            confidence = FindingConfidence.MEDIUM
            limitations.append(
                "Plan is CONDITIONAL: causal explanation relies on inferred relationships or call paths."
            )
        elif causal_status == CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES:
            plan_status = RepairPlanStatus.MULTIPLE_CANDIDATES
            confidence = FindingConfidence.MEDIUM
            limitations.append(
                "Plan preserves MULTIPLE_CANDIDATES: competing plausible root causes exist."
            )
        elif causal_status == CausalStatus.INSUFFICIENT_EVIDENCE:
            plan_status = RepairPlanStatus.INSUFFICIENT_EVIDENCE
            confidence = FindingConfidence.LOW
            limitations.append(
                "Plan is NON-ACTIONABLE: insufficient verified evidence to safely plan code modifications."
            )
        else:
            plan_status = RepairPlanStatus.UNRESOLVED
            confidence = FindingConfidence.LOW
            limitations.append(
                "Plan is UNRESOLVED: defect diagnosis or causal explanation is inconclusive."
            )

        # 6. Candidate Strategy Generation
        strategies = self._strategy_generator.generate_strategies(
            root_cause_report=root_cause_report,
            diagnosis=diagnosis,
            evidence_list=evidence_list,
            failing_symbol=target_symbol,
            file_path=target_file,
            error_message=error_message,
        )

        # Disqualify candidate strategies if evidence is stale
        if stale_issues:
            for s in strategies:
                s.is_recommended = False
                if "Supporting evidence is stale" not in " ".join(s.rejection_reasons):
                    s.rejection_reasons.append(
                        "Supporting evidence is stale or unverified against current workspace disk state."
                    )

        # Determine selected strategy
        selected_strat_id: Optional[str] = None
        if plan_status in (RepairPlanStatus.ACTIONABLE, RepairPlanStatus.CONDITIONAL, RepairPlanStatus.MULTIPLE_CANDIDATES) and not stale_issues:
            recommended = next((s for s in strategies if s.is_recommended), None)
            if recommended and not recommended.rejection_reasons:
                selected_strat_id = recommended.strategy_id
        else:
            # No actionable strategy permitted when evidence is stale or insufficient
            selected_strat_id = None

        selected_strategy = next((s for s in strategies if s.strategy_id == selected_strat_id), None)
        selected_approach = selected_strategy.approach if selected_strategy else RepairApproach.LOCAL_LOGIC_CORRECTION

        # 7. Required Changes Construction
        required_changes: List[RequiredChange] = []
        if selected_strategy and target_file != "unknown":
            start_l = primary_node.start_line if primary_node else line_number
            end_l = primary_node.end_line if primary_node else line_number
            required_changes.append(
                RequiredChange(
                    file_path=target_file,
                    symbol_id=target_symbol if target_symbol != "unknown" else None,
                    start_line=start_l,
                    end_line=end_l,
                    change_type=selected_strategy.approach.value,
                    description=selected_strategy.description,
                    rationale=f"Addresses verified root cause in '{target_symbol}'.",
                )
            )

        # 8. Deterministic Risk Assessment
        risk = self._risk_analyzer.assess_risk(
            affected_files=affected_files,
            affected_symbols=affected_symbols,
            proposed_approach=selected_approach,
            root_cause_report=root_cause_report,
        )

        # 9. Verification Plan Construction
        existing_tests, proposed_tests = self._build_verification_tests(affected_files, target_symbol)
        verification_plan = VerificationPlan(
            existing_targeted_tests=existing_tests,
            recommended_new_tests=proposed_tests,
            regression_test_scope=[
                "Run test suite across affected module packages.",
                "Execute workspace smoke and regression test matrix.",
            ],
            security_checks=[
                "Static AST assertion inspection to confirm no security guards are bypassed.",
            ],
            static_analysis_requirements=[
                "Run flake8 and mypy type checks on modified files.",
            ],
            expected_verification_criteria=[
                f"Targeted test suite passes asserting expected behavior in '{target_symbol}'.",
                "Zero regressions observed in dependent callers.",
            ],
        )

        # 10. Rollback Plan Construction
        rollback_plan = self._rollback_builder.build_rollback_plan(
            affected_files=affected_files,
            affected_symbols=affected_symbols,
            required_changes=required_changes,
            approach=selected_approach,
        )

        # Re-verify freshness immediately before assembly to prevent race conditions
        pre_assembly_stale: List[str] = []
        if target_file != "unknown":
            t_valid, _, t_err = self._evidence_collector.verify_file_location(target_file, None, None)
            if not t_valid:
                pre_assembly_stale.append(f"Target file modified/removed during plan synthesis: {t_err}")

        for ev in evidence_list:
            if ev.file_path and ev.file_path != "unknown" and ev.is_fresh:
                ev_valid, ev_curr_hash, ev_err = self._evidence_collector.verify_file_location(
                    ev.file_path, ev.start_line, ev.end_line
                )
                if not ev_valid:
                    pre_assembly_stale.append(f"Evidence '{ev.evidence_id}' modified during planning: {ev_err}")
                elif ev.source_hash and ev_curr_hash and ev.source_hash != ev_curr_hash:
                    pre_assembly_stale.append(
                        f"Evidence '{ev.evidence_id}' on '{ev.file_path}' content hash drifted during planning "
                        f"(current: {ev_curr_hash[:8]}..., recorded: {ev.source_hash[:8]}...)"
                    )

        if pre_assembly_stale:
            stale_issues.extend(pre_assembly_stale)
            plan_status = RepairPlanStatus.INSUFFICIENT_EVIDENCE
            confidence = FindingConfidence.LOW
            selected_strat_id = None
            selected_strategy = None
            required_changes = []
            for s in strategies:
                s.is_recommended = False
                if "Supporting evidence drifted" not in " ".join(s.rejection_reasons):
                    s.rejection_reasons.append(
                        "Supporting evidence drifted during planning synthesis."
                    )
            limitations.append(
                f"Plan downgraded to INSUFFICIENT_EVIDENCE due to concurrent disk changes: {'; '.join(pre_assembly_stale)}"
            )

        # 11. Assemble Comprehensive RepairPlan
        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        problem_stmt = (
            f"Software defect in '{target_symbol}' ({target_file}): "
            f"{error_message or (diagnosis.failing_behavior if diagnosis else 'Observed runtime failure.')}"
        )
        repair_obj = (
            f"Modify '{target_symbol}' to eliminate runtime defect while preserving public API compatibility."
        )
        summary = (
            f"Repair plan ({plan_status.value.upper()}) for '{target_symbol}' in '{target_file}'. "
            f"Selected strategy: {selected_strategy.title if selected_strategy else 'None (Manual Triage Required)'}. "
            f"Risk: {risk.overall_risk.value.upper()} (score: {risk.risk_score:.2f})."
        )

        plan = RepairPlan(
            plan_id=plan_id,
            finding_id=diagnosis.associated_finding_id if diagnosis else None,
            root_cause_reference=root_cause_report.report_id if root_cause_report else None,
            status=plan_status,
            summary=summary,
            problem_statement=problem_stmt,
            repair_objective=repair_obj,
            selected_strategy_id=selected_strat_id,
            candidate_strategies=strategies,
            affected_files=affected_files,
            affected_symbols=affected_symbols,
            required_changes=required_changes,
            preconditions=[
                "Target repository working tree is clean and synchronized.",
                "Execution sandbox is configured and available for verification.",
            ],
            dependencies=[
                "Use existing project dependencies; zero unrequested third-party packages.",
            ],
            risk_assessment=risk,
            expected_side_effects=[
                f"Modifies internal behavior of '{target_symbol}' in '{target_file}'.",
            ],
            verification_plan=verification_plan,
            rollback_plan=rollback_plan,
            supporting_evidence=evidence_list,
            confidence=confidence,
            limitations="; ".join(limitations) if limitations else None,
            approval_required=True,       # Non-negotiable M3 safety invariant
            execution_permitted=False,    # Non-negotiable M3 safety invariant
        )

        # 12. Enforce Invariant Validation
        is_valid, val_errors, checked = self._plan_validator.validate_plan(
            plan=plan,
            root_cause_report=root_cause_report,
        )
        if not is_valid:
            raise ValueError(f"Repair plan invariant violation: {'; '.join(val_errors)}")

        # 13. Cache Plan
        self._plans[plan.plan_id] = plan
        return plan

    def generate_plan(
        self,
        root_cause_report: Optional[RootCauseReport] = None,
        diagnosis: Optional[DiagnosisResult] = None,
        failing_symbol: Optional[str] = None,
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> RepairPlan:
        """Alias for plan_repair."""
        return self.plan_repair(
            root_cause_report=root_cause_report,
            diagnosis=diagnosis,
            failing_symbol=failing_symbol,
            file_path=file_path,
            line_number=line_number,
            error_message=error_message,
        )

    def _build_verification_tests(
        self,
        affected_files: List[str],
        target_symbol: str,
    ) -> Tuple[List[str], List[str]]:
        """Identify verified on-disk test files and formulate recommended new tests."""
        existing_tests: List[str] = []
        proposed_tests: List[str] = []

        tests_dir = self.workspace_path / "tests"

        for f in affected_files:
            stem = Path(f).stem
            # Direct pattern matches verified on disk
            patterns = [
                f"tests/unit/test_{stem}.py",
                f"tests/test_{stem}.py",
                f"tests/unit/{stem}_test.py",
            ]
            for pat in patterns:
                cand = self.workspace_path / pat
                if cand.exists() and cand.is_file() and pat not in existing_tests:
                    existing_tests.append(pat)

            # Rglob search in tests/
            if tests_dir.exists() and tests_dir.is_dir():
                for p in tests_dir.rglob(f"*{stem}*.py"):
                    rel = str(p.relative_to(self.workspace_path)).replace("\\", "/")
                    if rel not in existing_tests and p.is_file():
                        existing_tests.append(rel)

        # Recommended new tests must be explicitly labeled as proposed
        proposed_tests.append(
            f"[PROPOSED] test_{Path(affected_files[0]).stem if affected_files else 'defect'}_reproduction: "
            f"verify fix for '{target_symbol}' and assert valid boundary behavior."
        )

        return existing_tests, proposed_tests
