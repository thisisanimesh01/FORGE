"""Phase 12 M3: Candidate Repair Strategy Generation.

Generates structured, minimal, and risk-aware candidate repair strategies
grounded in verified root-cause analysis, defect taxonomy, and causal relations.

Upholds:
- Strategy minimality (preferring local corrections over architectural refactoring).
- Multi-candidate preservation (preserving distinct approaches and trade-offs).
- Certainty gating (uncertain or insufficient evidence produces non-actionable strategies).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.causality.schemas import (
    CausalNode,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.diagnosis.schemas import DefectCategory, DiagnosisResult
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairEvidence,
    RepairStrategy,
    StrategyComplexity,
    StrategyInvasiveness,
    StrategyReversibility,
)
from forge.repository.incremental import IncrementalRepositoryIndex


class RepairStrategyGenerator:
    """Generates and ranks candidate repair strategies from causal reports."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.incremental_index = incremental_index

    def generate_strategies(
        self,
        root_cause_report: Optional[RootCauseReport] = None,
        diagnosis: Optional[DiagnosisResult] = None,
        evidence_list: Optional[List[RepairEvidence]] = None,
        failing_symbol: Optional[str] = None,
        file_path: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> List[RepairStrategy]:
        """Generate ranked candidate repair strategies."""
        status = root_cause_report.status if root_cause_report else CausalStatus.INSUFFICIENT_EVIDENCE
        category = (
            diagnosis.defect_category
            if diagnosis
            else DefectCategory.UNKNOWN
        )

        ev_ids = [e.evidence_id for e in (evidence_list or []) if e.is_fresh]

        # -------------------------------------------------------------------
        # 1. Non-actionable or Unresolved Root Causes
        # -------------------------------------------------------------------
        if status in (CausalStatus.INSUFFICIENT_EVIDENCE, CausalStatus.UNRESOLVED):
            return [
                RepairStrategy(
                    strategy_id="strat_manual_investigation_01",
                    title="Manual Investigation Required (Insufficient/Unresolved Evidence)",
                    description=(
                        "Autonomous repair is prohibited because root-cause analysis yielded "
                        f"status '{status.value}'. Essential causal links or verified code facts "
                        "are missing, contradictory, or stale. Manual developer triage is required."
                    ),
                    approach=RepairApproach.UNKNOWN_OR_MANUAL_INVESTIGATION,
                    affected_files=[file_path] if file_path else [],
                    affected_symbols=[failing_symbol] if failing_symbol else [],
                    supporting_evidence=ev_ids,
                    expected_benefits=["Avoids fabricating unverified or destructive code modifications."],
                    risks=["Defect remains present until developer investigation is completed."],
                    complexity=StrategyComplexity.HIGH,
                    invasiveness=StrategyInvasiveness.MINIMAL,
                    reversibility=StrategyReversibility.HIGH,
                    test_requirements=["Gather reproduction trace and verify failing assertions before planning repair."],
                    rejection_reasons=[f"Autonomous planning blocked by root-cause status '{status.value}'."],
                    is_recommended=False,
                    score=0.10,
                )
            ]

        # -------------------------------------------------------------------
        # 2. Extract Primary Target & Causal Details
        # -------------------------------------------------------------------
        primary_node = root_cause_report.primary_root_cause if root_cause_report else None
        target_file = (primary_node.file_path if primary_node else file_path) or "unknown"
        target_symbol = (primary_node.symbol_id if primary_node else failing_symbol) or "unknown"

        # Check for specific causal relationships in chains
        has_invalid_data = False
        has_precondition_violation = False
        has_exception_propagation = False
        has_resource_misuse = False

        if root_cause_report:
            for edge in root_cause_report.edges:
                if edge.relationship == CausalRelationship.PASSES_INVALID_DATA:
                    has_invalid_data = True
                elif edge.relationship == CausalRelationship.VIOLATES_PRECONDITION:
                    has_precondition_violation = True
                elif edge.relationship == CausalRelationship.PROPAGATES_EXCEPTION:
                    has_exception_propagation = True
                elif edge.relationship == CausalRelationship.MISUSES_RESOURCE:
                    has_resource_misuse = True

        candidates: List[RepairStrategy] = []

        # -------------------------------------------------------------------
        # 3. Strategy Category Mapping
        # -------------------------------------------------------------------

        # Primary Minimal Strategy: Local Logic Correction or Input Guard
        if category == DefectCategory.INPUT_VALIDATION_FAILURE or has_invalid_data or has_precondition_violation:
            strat_val = RepairStrategy(
                strategy_id="strat_input_validation_01",
                title=f"Add input validation guard in '{target_symbol}'",
                description=(
                    f"Insert explicit defensive precondition checks in '{target_symbol}' to validate incoming "
                    "parameters or state prior to execution, preventing invalid data from propagating downstream."
                ),
                approach=RepairApproach.INPUT_VALIDATION,
                affected_files=[target_file],
                affected_symbols=[target_symbol],
                supporting_evidence=ev_ids,
                expected_benefits=[
                    "Prevents invalid argument values from reaching sensitive internal operations.",
                    "Fails early with clear error messaging or handles edge cases safely.",
                ],
                risks=["May reject unexpected boundary inputs if validation schema is too strict."],
                complexity=StrategyComplexity.LOW,
                invasiveness=StrategyInvasiveness.MINIMAL,
                reversibility=StrategyReversibility.HIGH,
                test_requirements=[
                    f"Unit tests verifying rejection of malformed inputs to '{target_symbol}'.",
                    "Regression tests confirming valid inputs continue to execute successfully.",
                ],
                rejection_reasons=[],
                score=0.90,
            )
            candidates.append(strat_val)

        # Local logic correction (most common minimal repair)
        strat_logic = RepairStrategy(
            strategy_id="strat_local_logic_01",
            title=f"Correct local statement logic in '{target_symbol}'",
            description=(
                f"Adjust internal calculations, conditional branches, or bounds checks within '{target_symbol}' "
                "to align with expected specification and eliminate the verified defect."
            ),
            approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
            affected_files=[target_file],
            affected_symbols=[target_symbol],
            supporting_evidence=ev_ids,
            expected_benefits=[
                "Minimally invasive fix directly addressing the isolated root cause.",
                "Zero public API signature or interface alterations.",
            ],
            risks=["Potential subtle edge-case behavior change in dependent callers."],
            complexity=StrategyComplexity.LOW,
            invasiveness=StrategyInvasiveness.MINIMAL,
            reversibility=StrategyReversibility.HIGH,
            test_requirements=[
                f"Targeted unit test reproducing and asserting the corrected behavior in '{target_symbol}'.",
                "Existing regression suite to verify no breakage in dependent components.",
            ],
            rejection_reasons=[],
            score=0.88 if candidates else 0.92,
        )
        candidates.append(strat_logic)

        # Error handling fallback strategy
        if category in (DefectCategory.EXCEPTION_HANDLING_DEFECT, DefectCategory.LOGIC_ERROR) or has_exception_propagation:
            strat_err = RepairStrategy(
                strategy_id="strat_error_handling_02",
                title=f"Introduce structured exception handling in '{target_symbol}'",
                description=(
                    f"Wrap vulnerable operations in '{target_symbol}' with structured exception handling "
                    "or fallback returns to prevent unhandled runtime crashes."
                ),
                approach=RepairApproach.ERROR_HANDLING,
                affected_files=[target_file],
                affected_symbols=[target_symbol],
                supporting_evidence=ev_ids,
                expected_benefits=[
                    "Graceful degradation when unexpected runtime states or external errors occur.",
                    "Prevents cascading failures in calling code.",
                ],
                risks=["Catching overly broad exceptions could mask underlying logical defects."],
                complexity=StrategyComplexity.MEDIUM,
                invasiveness=StrategyInvasiveness.MODERATE,
                reversibility=StrategyReversibility.HIGH,
                test_requirements=[
                    f"Tests simulating exceptional conditions in '{target_symbol}' and checking fallback handling.",
                ],
                rejection_reasons=[],
                score=0.75,
            )
            candidates.append(strat_err)

        # Resource management strategy
        if category == DefectCategory.RESOURCE_MANAGEMENT_ISSUE or has_resource_misuse:
            strat_res = RepairStrategy(
                strategy_id="strat_resource_mgmt_01",
                title=f"Enforce context-managed resource lifecycle in '{target_symbol}'",
                description=(
                    f"Convert manual resource allocation/cleanup in '{target_symbol}' to deterministic context managers "
                    "or explicit finally blocks to guarantee resource release."
                ),
                approach=RepairApproach.RESOURCE_MANAGEMENT,
                affected_files=[target_file],
                affected_symbols=[target_symbol],
                supporting_evidence=ev_ids,
                expected_benefits=["Eliminates resource leaks and unclosed handles."],
                risks=["Subtle timing or lifecycle shifts if resources are closed earlier than before."],
                complexity=StrategyComplexity.MEDIUM,
                invasiveness=StrategyInvasiveness.MINIMAL,
                reversibility=StrategyReversibility.HIGH,
                test_requirements=["Lifecycle tests confirming cleanup on both success and exception paths."],
                rejection_reasons=[],
                score=0.85,
            )
            candidates.append(strat_res)

        # Security hardening strategy
        if category == DefectCategory.SECURITY_VULNERABILITY:
            strat_sec = RepairStrategy(
                strategy_id="strat_security_hardening_01",
                title=f"Sanitize and harden input handling in '{target_symbol}'",
                description=(
                    f"Enforce strict sanitization, bounds checking, and capability restrictions in '{target_symbol}' "
                    "to neutralize the security vulnerability."
                ),
                approach=RepairApproach.SECURITY_HARDENING,
                affected_files=[target_file],
                affected_symbols=[target_symbol],
                supporting_evidence=ev_ids,
                expected_benefits=["Closes verified security vulnerability and protects data boundaries."],
                risks=["May reject boundary inputs previously accepted if deemed untrusted."],
                complexity=StrategyComplexity.MEDIUM,
                invasiveness=StrategyInvasiveness.MINIMAL,
                reversibility=StrategyReversibility.HIGH,
                test_requirements=["Security regression tests asserting rejection of exploit payloads."],
                rejection_reasons=[],
                score=0.89,
            )
            candidates.append(strat_sec)

        # Alternative Overly Broad Refactoring (always disfavored/rejected to enforce minimality)
        strat_broad = RepairStrategy(
            strategy_id="strat_broad_refactor_rejected",
            title=f"Architectural refactoring of module containing '{target_symbol}'",
            description=(
                f"Redesign and refactor the entire module surrounding '{target_symbol}', rewriting interfaces and "
                "redistributing state management across new classes."
            ),
            approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
            affected_files=[target_file],
            affected_symbols=[target_symbol, f"{target_symbol}_helper"],
            supporting_evidence=ev_ids,
            expected_benefits=["Modernized module structure."],
            risks=[
                "High probability of regression.",
                "Violates minimality principle by altering unaffected code.",
                "Breaks existing callers and public interfaces.",
            ],
            complexity=StrategyComplexity.HIGH,
            invasiveness=StrategyInvasiveness.EXTENSIVE,
            reversibility=StrategyReversibility.LOW,
            test_requirements=["Full integration suite rewrite."],
            rejection_reasons=[
                "Rejected: Violates repair minimality principle. An unrequested architectural rewrite introduces high regression risk."
            ],
            is_recommended=False,
            score=0.20,
        )
        candidates.append(strat_broad)

        # -------------------------------------------------------------------
        # 4. Multi-Plausible Roots Handling
        # -------------------------------------------------------------------
        if status == CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES:
            # Preserve separate strategies for each alternative root hypothesis
            if root_cause_report and root_cause_report.alternative_explanations:
                for idx, alt in enumerate(root_cause_report.alternative_explanations, start=1):
                    if not alt.is_refuted:
                        strat_alt = RepairStrategy(
                            strategy_id=f"strat_competing_root_{idx:02d}",
                            title=f"Conditional repair for plausible root '{alt.suspected_root_cause}'",
                            description=(
                                f"Repair addressing competing root-cause explanation: '{alt.rationale}'. "
                                "Should be evaluated conditionally alongside the primary root cause."
                            ),
                            approach=RepairApproach.LOCAL_LOGIC_CORRECTION,
                            affected_files=[target_file],
                            affected_symbols=[alt.suspected_root_cause],
                            supporting_evidence=alt.contradicted_by or ev_ids,
                            expected_benefits=[f"Addresses competing causal hypothesis {alt.explanation_id}."],
                            risks=["May be redundant or incorrect if competing root cause is refuted by further evidence."],
                            complexity=StrategyComplexity.MEDIUM,
                            invasiveness=StrategyInvasiveness.MODERATE,
                            reversibility=StrategyReversibility.HIGH,
                            test_requirements=["Differential test verifying whether this alternative resolves the failure."],
                            rejection_reasons=[],
                            score=0.70,
                        )
                        candidates.append(strat_alt)

        # -------------------------------------------------------------------
        # 5. Rank and Select Recommended Strategy
        # -------------------------------------------------------------------
        # Sort candidates by score descending
        candidates.sort(key=lambda s: s.score, reverse=True)

        # Mark top non-rejected strategy as recommended
        for c in candidates:
            if not c.rejection_reasons and c.approach != RepairApproach.UNKNOWN_OR_MANUAL_INVESTIGATION:
                c.is_recommended = True
                break

        return candidates
