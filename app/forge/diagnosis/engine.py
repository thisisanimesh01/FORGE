"""Phase 12 M1: Evidence-Driven Defect Diagnosis Engine.

Orchestrates input validation, failure localization, evidence grounding,
hypothesis generation and evaluation, and structured diagnosis reporting.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from forge.config import redact_secrets
from forge.diagnosis.hypothesis import HypothesisEvaluator
from forge.diagnosis.localization import FailureLocalizer
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    LocationSource,
    TestFailureReport,
)
from forge.llm.context import RepositoryContextBuilder
from forge.llm.evidence_grounding import EvidenceGroundingEngine
from forge.llm.provider import LLMProvider
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.schemas import EvidenceRecord, FindingConfidence, FindingSeverity

if TYPE_CHECKING:
    from forge.storage.store import ForgeStore


class EvidenceDrivenDiagnosisEngine:
    """Core diagnosis engine for FORGE Phase 12 M1."""

    def __init__(
        self,
        workspace_path: Path,
        run_id: str = "run_default",
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        store: Optional["ForgeStore"] = None,
        provider: Optional[LLMProvider] = None,
        llm_provider: Optional[LLMProvider] = None,
        context_builder: Optional[RepositoryContextBuilder] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.run_id = run_id
        self._incremental_index = incremental_index
        self.store = store
        self.provider = provider or llm_provider
        self._context_builder = context_builder

        # Initialize sub-components lazily
        self._localizer: Optional[FailureLocalizer] = None
        self._evaluator: Optional[HypothesisEvaluator] = None
        self._evidence_engine: Optional[EvidenceGroundingEngine] = None

    @property
    def localizer(self) -> FailureLocalizer:
        return self.get_localizer()

    @property
    def evaluator(self) -> HypothesisEvaluator:
        return self.get_evaluator()

    @property
    def evidence_engine(self) -> EvidenceGroundingEngine:
        return self.get_evidence_engine()

    def get_incremental_index(self) -> IncrementalRepositoryIndex:
        """Lazily initialize or return the IncrementalRepositoryIndex."""
        if self._incremental_index is None:
            self._incremental_index = IncrementalRepositoryIndex(repo_root=self.workspace_path)
        return self._incremental_index

    def get_evidence_engine(self) -> EvidenceGroundingEngine:
        """Lazily initialize or return EvidenceGroundingEngine."""
        if self._evidence_engine is None:
            idx = self.get_incremental_index()
            self._evidence_engine = EvidenceGroundingEngine(
                workspace_path=self.workspace_path,
                run_id=self.run_id,
                incremental_index=idx,
                store=self.store,
            )
        return self._evidence_engine

    def get_context_builder(self) -> RepositoryContextBuilder:
        """Lazily initialize or return RepositoryContextBuilder."""
        if self._context_builder is None:
            idx = self.get_incremental_index()
            self._context_builder = RepositoryContextBuilder(
                workspace_path=self.workspace_path,
                incremental_index=idx,
            )
        return self._context_builder

    def get_localizer(self) -> FailureLocalizer:
        """Lazily initialize or return FailureLocalizer with full graph capabilities."""
        if self._localizer is None:
            idx = self.get_incremental_index()
            symbols, call_graph, module_graph, _ = idx.build()

            impact_analyzer = None
            try:
                from forge.analysis.impact.analyzer import ChangeImpactAnalyzer
                impact_analyzer = ChangeImpactAnalyzer(
                    repo_root=self.workspace_path,
                    incremental_index=idx,
                )
            except Exception:
                pass

            self._localizer = FailureLocalizer(
                workspace_path=self.workspace_path,
                symbol_index=symbols,
                call_graph=call_graph,
                module_graph=module_graph,
                impact_analyzer=impact_analyzer,
            )
        return self._localizer

    def get_evaluator(self) -> HypothesisEvaluator:
        """Lazily initialize or return HypothesisEvaluator."""
        if self._evaluator is None:
            self._evaluator = HypothesisEvaluator(
                workspace_path=self.workspace_path,
                evidence_engine=self.get_evidence_engine(),
                provider=self.provider,
                context_builder=self.get_context_builder(),
            )
        return self._evaluator

    # -----------------------------------------------------------------------
    # Main Diagnostic Pipeline
    # -----------------------------------------------------------------------

    def diagnose(
        self,
        input_data: DiagnosisInput,
        run_id: Optional[str] = None,
        persist_finding: bool = True,
    ) -> DiagnosisResult:
        """Execute complete evidence-driven defect diagnosis pipeline."""
        if run_id:
            self.run_id = run_id
            if self._evidence_engine:
                self._evidence_engine.run_id = run_id

        diagnosis_id = f"diag_{hashlib.sha256(f'{self.run_id}:{datetime.now(timezone.utc).isoformat()}:{input_data.user_description}'.encode()).hexdigest()[:12]}"

        # Step 1: Failure Localization
        localizer = self.get_localizer()
        candidate_locations = localizer.localize(input_data)

        # Step 2: Hypothesis Generation, Grounding & Evaluation
        evaluator = self.get_evaluator()
        hypotheses, supporting_records, contradicting_records = evaluator.generate_and_evaluate_hypotheses(
            input_data,
            candidate_locations,
        )

        # Step 3: Summarize failing behavior
        failing_behavior = self._extract_failing_behavior(input_data)
        input_summary = self._summarize_input(input_data)

        # Step 4: Determine Overall Status, Category, and Confidence
        status, category, confidence, conf_score, rationale = self._determine_overall_diagnosis(
            hypotheses,
            candidate_locations,
            input_data,
        )

        # Step 5: Assign Severity independently of Confidence
        severity = self._determine_severity(category, input_data, candidate_locations)

        # Step 6: Formulate Verification Requirements and Next Step
        verification_reqs = self._formulate_verification_requirements(status, hypotheses, candidate_locations)
        next_step = self._formulate_recommended_next_step(status, hypotheses, candidate_locations)

        # Step 7: Optional Finding Storage Link
        associated_finding_id = None
        if self.store and persist_finding and hypotheses:
            top_h = hypotheses[0]
            finding_title = (
                input_data.error_message
                or (input_data.test_failure.failure_message if input_data.test_failure else None)
                or f"Diagnosed Defect: {category.value} in {top_h.suspected_location.file_path}"
            )
            try:
                finding = self.get_evidence_engine().validate_candidate_finding(
                    title=finding_title,
                    description=top_h.description,
                    affected_file=top_h.suspected_location.file_path,
                    start_line=top_h.suspected_location.start_line,
                    end_line=top_h.suspected_location.end_line,
                    severity=severity,
                    confidence=confidence,
                )
                associated_finding_id = finding.finding_id
            except Exception:
                pass

        supporting_ids = [r.evidence_id for r in supporting_records]
        contradicting_ids = [r.evidence_id for r in contradicting_records]

        limitations = None
        if status != DiagnosisStatus.CONFIRMED:
            limitations = (
                "Diagnosis remains suspected or unresolved because direct disk reproduction or test verification "
                "is incomplete. Model suspicion alone does not confirm defect status."
            )

        return DiagnosisResult(
            diagnosis_id=diagnosis_id,
            run_id=self.run_id,
            input_summary=input_summary,
            failing_behavior=failing_behavior,
            defect_category=category,
            severity=severity,
            status=status,
            confidence=confidence,
            confidence_score=conf_score,
            root_cause_locations=candidate_locations[:5],
            hypotheses=hypotheses,
            supporting_evidence_ids=supporting_ids,
            contradicting_evidence_ids=contradicting_ids,
            confidence_explanation=rationale,
            verification_requirements=verification_reqs,
            recommended_next_step=next_step,
            limitations=limitations,
            associated_finding_id=associated_finding_id,
        )

    # -----------------------------------------------------------------------
    # Helper: Failure Summary & Symptom Extraction
    # -----------------------------------------------------------------------

    def _extract_failing_behavior(self, input_data: DiagnosisInput) -> str:
        """Formulate a concise description of what behavior is failing."""
        if input_data.test_failure:
            tf = input_data.test_failure
            return f"Test failure in '{tf.test_name}': {tf.failure_message[:120]}"
        if input_data.error_message:
            return f"Runtime error: {input_data.error_message[:150]}"
        if input_data.actual_behavior and input_data.expected_behavior:
            return f"Behavior divergence: Expected '{input_data.expected_behavior[:80]}' but observed '{input_data.actual_behavior[:80]}'"
        if input_data.user_description:
            return f"Reported issue: {input_data.user_description[:150]}"
        if input_data.static_finding:
            return f"Static analysis rule violation '{input_data.static_finding.rule_id}': {input_data.static_finding.message[:120]}"
        return "Unknown failure behavior from provided inputs."

    def _summarize_input(self, input_data: DiagnosisInput) -> str:
        """Create a human-readable summary of provided input signals."""
        signals = []
        if input_data.user_description:
            signals.append("User Description")
        if input_data.test_failure:
            signals.append(f"Test Failure ({input_data.test_failure.test_name})")
        if input_data.finding_id:
            signals.append(f"FORGE Finding ({input_data.finding_id})")
        if input_data.static_finding:
            signals.append(f"Static Finding ({input_data.static_finding.rule_id})")
        if input_data.diff:
            signals.append("Git Diff")
        if input_data.stack_trace:
            signals.append("Stack Trace")
        if input_data.target_files:
            signals.append(f"Target Files ({len(input_data.target_files)})")
        if input_data.target_symbols:
            signals.append(f"Target Symbols ({len(input_data.target_symbols)})")
        return f"Diagnostic input signals: {', '.join(signals) if signals else 'Minimal'}"

    # -----------------------------------------------------------------------
    # Helper: Status, Category, and Confidence Determination
    # -----------------------------------------------------------------------

    def _determine_overall_diagnosis(
        self,
        hypotheses: List[DiagnosisHypothesis],
        candidate_locations: List[CandidateLocation],
        input_data: DiagnosisInput,
    ) -> Tuple[DiagnosisStatus, DefectCategory, FindingConfidence, float, str]:
        """Synthesize overall diagnosis status, category, confidence, and explanatory rationale.

        CORE INVARIANT: Model suspicion alone CANNOT produce a CONFIRMED defect.
        """
        if not hypotheses or not candidate_locations:
            return (
                DiagnosisStatus.UNRESOLVED,
                DefectCategory.UNKNOWN,
                FindingConfidence.LOW,
                0.0,
                "Insufficient evidence: No candidate locations or hypotheses could be formulated from inputs.",
            )

        top_h = hypotheses[0]

        # Check for unrefuted competing hypotheses (excluding test files)
        competing = [
            h for h in hypotheses[1:]
            if h.confidence_score >= 0.70
            and h.suspected_location.file_path != top_h.suspected_location.file_path
            and not h.suspected_location.file_path.startswith("tests/")
            and not h.suspected_location.file_path.startswith("test_")
        ]

        # Has direct failure evidence (stack trace crash frame, test assertion failure, or error message)
        has_failure_signal = bool(
            input_data.test_failure is not None
            or input_data.stack_trace is not None
            or (input_data.error_message and any(loc.source == LocationSource.ERROR_MESSAGE for loc in candidate_locations))
        )

        # Has direct grounded supporting evidence
        has_grounded_evidence = bool(top_h.supporting_evidence_ids)

        # Has zero unrefuted contradicting evidence
        no_contradictions = (len(top_h.contradicting_evidence_ids) == 0)

        # Criteria for CONFIRMED diagnosis:
        is_confirmed = (
            top_h.confidence == FindingConfidence.HIGH
            and top_h.confidence_score >= 0.75
            and top_h.suspected_location.is_verified_on_disk
            and has_grounded_evidence
            and has_failure_signal
            and no_contradictions
            and top_h.defect_category != DefectCategory.UNKNOWN
            and len(competing) == 0
        )

        if is_confirmed:
            status = DiagnosisStatus.CONFIRMED
            rationale = (
                f"Diagnosis CONFIRMED: Defect location '{top_h.suspected_location.file_path}:{top_h.suspected_location.start_line}' "
                f"is verified on disk with grounded source evidence. Direct failure signals align with category "
                f"'{top_h.defect_category.value}' (Confidence score: {top_h.confidence_score:.2f}). No contradicting evidence exists."
            )
        elif top_h.confidence_score >= 0.40 and top_h.suspected_location.is_verified_on_disk:
            status = DiagnosisStatus.SUSPECTED
            if competing:
                rationale = (
                    f"Diagnosis SUSPECTED: Competing hypotheses exist ({len(competing)} alternative candidate locations). "
                    f"Top hypothesis points to '{top_h.suspected_location.file_path}' (Confidence: {top_h.confidence.value}, score: {top_h.confidence_score:.2f}), "
                    "but requires additional verification to rule out competing explanations."
                )
            elif not has_failure_signal:
                rationale = (
                    f"Diagnosis SUSPECTED: Plausible code pattern identified in '{top_h.suspected_location.file_path}', "
                    "but no runtime failure trace or failing test assertion is available to confirm active defect status."
                )
            elif not no_contradictions:
                rationale = (
                    f"Diagnosis SUSPECTED: Candidate location in '{top_h.suspected_location.file_path}' has {len(top_h.contradicting_evidence_ids)} "
                    "contradicting evidence records that weaken confirmation."
                )
            else:
                rationale = (
                    f"Diagnosis SUSPECTED: Promising hypothesis in '{top_h.suspected_location.file_path}' (Confidence: {top_h.confidence.value}, "
                    f"score: {top_h.confidence_score:.2f}). Verification pending."
                )
        else:
            status = DiagnosisStatus.UNRESOLVED
            rationale = (
                f"Diagnosis UNRESOLVED: Insufficient or low-confidence evidence (Confidence score: {top_h.confidence_score:.2f}). "
                "Unable to confirm defect origin without further test or execution traces."
            )

        return (
            status,
            top_h.defect_category,
            top_h.confidence,
            top_h.confidence_score,
            rationale,
        )

    # -----------------------------------------------------------------------
    # Helper: Severity Determination
    # -----------------------------------------------------------------------

    def _determine_severity(
        self,
        category: DefectCategory,
        input_data: DiagnosisInput,
        candidate_locations: List[CandidateLocation],
    ) -> FindingSeverity:
        """Determine impact severity independently of confidence."""
        if category == DefectCategory.SECURITY_VULNERABILITY:
            return FindingSeverity.CRITICAL

        if category == DefectCategory.RESOURCE_MANAGEMENT_ISSUE:
            return FindingSeverity.HIGH

        if input_data.stack_trace or (input_data.test_failure and "Error" in input_data.test_failure.failure_message):
            return FindingSeverity.HIGH

        if category in (DefectCategory.INCORRECT_DATA_HANDLING, DefectCategory.LOGIC_ERROR, DefectCategory.INPUT_VALIDATION_FAILURE):
            return FindingSeverity.MEDIUM

        if category == DefectCategory.PERFORMANCE_DEFECT:
            return FindingSeverity.LOW

        if input_data.static_finding and input_data.static_finding.severity:
            return input_data.static_finding.severity

        return FindingSeverity.MEDIUM

    # -----------------------------------------------------------------------
    # Helper: Verification Requirements & Recommended Next Step
    # -----------------------------------------------------------------------

    def _formulate_verification_requirements(
        self,
        status: DiagnosisStatus,
        hypotheses: List[DiagnosisHypothesis],
        candidate_locations: List[CandidateLocation],
    ) -> List[str]:
        """Enumerate specific requirements to confirm or refute the diagnosis."""
        reqs: List[str] = []
        if not hypotheses:
            reqs.append("Provide a stack trace or failing test assertion to localize candidate defect points.")
            return reqs

        top_h = hypotheses[0]
        loc = top_h.suspected_location
        reqs.append(f"Execute targeted unit test covering '{loc.file_path}' lines {loc.start_line}–{loc.end_line}.")

        if top_h.missing_information:
            for mi in top_h.missing_information:
                reqs.append(f"Resolve uncertainty: {mi}")

        if status != DiagnosisStatus.CONFIRMED:
            reqs.append("Verify reproduction in isolated execution sandbox to promote from suspected to confirmed.")

        return reqs

    def _formulate_recommended_next_step(
        self,
        status: DiagnosisStatus,
        hypotheses: List[DiagnosisHypothesis],
        candidate_locations: List[CandidateLocation],
    ) -> str:
        """Formulate an actionable, concrete next step."""
        if not hypotheses or status == DiagnosisStatus.UNRESOLVED:
            return "Gather additional error logs or reproduction scripts before proceeding to repair planning."

        top_h = hypotheses[0]
        if status == DiagnosisStatus.CONFIRMED:
            return f"Proceed to Phase 12 M2 (Root-Cause Analysis) and M3 (Repair Planning) for '{top_h.suspected_location.file_path}'."
        else:
            return f"Inspect candidate locations in '{top_h.suspected_location.file_path}' and execute reproduction verification."
