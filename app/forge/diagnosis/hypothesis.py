"""Phase 12 M1: Hypothesis Generation and Evaluation Engine.

Generates candidate defect explanations and evaluates them against verifiable repository facts.
Enforces the core invariant: A model-generated suspicion never automatically becomes a confirmed defect.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.config import redact_secrets
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisHypothesis,
    DiagnosisInput,
    DiagnosisStatus,
    LocationSource,
)
from forge.llm.context import RepositoryContextBuilder
from forge.llm.evidence_grounding import EvidenceGroundingEngine
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.provider import LLMProvider, LLMUnavailableError
from forge.schemas import EvidenceRecord, FindingConfidence, FindingSeverity


# Pattern matchers for deterministic defect category inference
CATEGORY_PATTERNS: List[Tuple[re.Pattern, DefectCategory]] = [
    (re.compile(r"\b(IndexError|KeyError|bounds|out of range|slice|NoneType|subscript|attribute)\b", re.I), DefectCategory.INCORRECT_DATA_HANDLING),
    (re.compile(r"\b(TypeError|ValueError|invalid literal|schema|unsupported operand|parse error|validation|missing check|guard)\b", re.I), DefectCategory.INPUT_VALIDATION_FAILURE),
    (re.compile(r"\b(AssertionError|assert|expected|mismatch|off-by-one|incorrect result|wrong return)\b", re.I), DefectCategory.LOGIC_ERROR),
    (re.compile(r"\b(ZeroDivisionError|OverflowError|FloatingPointError)\b", re.I), DefectCategory.LOGIC_ERROR),
    (re.compile(r"\b(FileNotFoundError|PermissionError|leak|file descriptor|socket|handle|unclosed)\b", re.I), DefectCategory.RESOURCE_MANAGEMENT_ISSUE),
    (re.compile(r"\b(ImportError|ModuleNotFoundError|circular import|dependency|version conflict)\b", re.I), DefectCategory.DEPENDENCY_INTEGRATION_DEFECT),
    (re.compile(r"\b(except|try|unhandled exception|re-raise|catch|finally)\b", re.I), DefectCategory.EXCEPTION_HANDLING_DEFECT),
    (re.compile(r"\b(race condition|deadlock|thread|mutex|lock|concurrency|asyncio)\b", re.I), DefectCategory.CONCURRENCY_ISSUE),
    (re.compile(r"\b(injection|traversal|secret|credential|token|vulnerability|cve|eval|exec)\b", re.I), DefectCategory.SECURITY_VULNERABILITY),
    (re.compile(r"\b(slow|performance|quadratic|infinite loop|memory leak|bottleneck|timeout|timed out|TimeoutError)\b", re.I), DefectCategory.PERFORMANCE_DEFECT),
    (re.compile(r"\b(fixture|pytest|test setup|teardown|build failed)\b", re.I), DefectCategory.TEST_BUILD_FAILURE),
]


class HypothesisEvaluator:
    """Generates, grounds, and evaluates candidate diagnostic hypotheses."""

    def __init__(
        self,
        workspace_path: Path,
        evidence_engine: Optional[EvidenceGroundingEngine] = None,
        provider: Optional[LLMProvider] = None,
        context_builder: Optional[RepositoryContextBuilder] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.evidence_engine = evidence_engine or EvidenceGroundingEngine(workspace_path=self.workspace_path)
        self.provider = provider
        self.context_builder = context_builder

    def infer_category(
        self,
        error_message: Optional[str] = None,
        user_description: Optional[str] = None,
    ) -> DefectCategory:
        """Deterministically infer defect category from message and description strings."""
        combined = " ".join(filter(None, [error_message, user_description]))
        for pattern, cat in CATEGORY_PATTERNS:
            if pattern.search(combined):
                return cat
        return DefectCategory.UNKNOWN

    def evaluate(
        self,
        input_data: DiagnosisInput,
        candidate_locations: List[CandidateLocation],
        evidence_engine: Optional[EvidenceGroundingEngine] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[List[DiagnosisHypothesis], List[EvidenceRecord]]:
        """Evaluate candidates against evidence and return hypotheses and contradicting records."""
        if evidence_engine is not None:
            self.evidence_engine = evidence_engine
        hyps, supporting, contradicting = self.generate_and_evaluate_hypotheses(input_data, candidate_locations)
        return hyps, contradicting


    def generate_and_evaluate_hypotheses(
        self,
        input_data: DiagnosisInput,
        candidate_locations: List[CandidateLocation],
    ) -> Tuple[List[DiagnosisHypothesis], List[EvidenceRecord], List[EvidenceRecord]]:
        """Generate hypotheses, ground evidence, check contradictions, and evaluate confidence."""
        if not candidate_locations:
            # When no candidate locations can be determined from input
            return [], [], []

        supporting_evidence_records: List[EvidenceRecord] = []
        contradicting_evidence_records: List[EvidenceRecord] = []

        # 1. Generate candidate hypotheses
        raw_hypotheses = self._generate_hypotheses(input_data, candidate_locations)

        # 2. Ground direct source evidence for each candidate hypothesis
        grounded_hypotheses: List[DiagnosisHypothesis] = []
        for hyp in raw_hypotheses:
            loc = hyp.suspected_location
            try:
                evidence = self.evidence_engine.create_source_evidence(
                    file_path=loc.file_path,
                    start_line=loc.start_line,
                    end_line=loc.end_line,
                    description=f"Direct source excerpt for hypothesis '{hyp.hypothesis_id}': {hyp.description[:80]}",
                    symbol_name=loc.symbol_id,
                    provenance_tool="forge:diagnosis:hypothesis_evaluation",
                )
                supporting_evidence_records.append(evidence)
                hyp.supporting_evidence_ids.append(evidence.evidence_id)
            except Exception:
                # Evidence could not be grounded on disk (e.g. fabricated or out-of-bounds line)
                pass

            # 3. Check for contradicting evidence in actual source text
            contradiction = self._detect_contradiction(hyp, input_data)
            if contradiction:
                try:
                    c_evidence = self.evidence_engine.create_source_evidence(
                        file_path=contradiction["file_path"],
                        start_line=contradiction["start_line"],
                        end_line=contradiction["end_line"],
                        description=f"Contradiction detected (guard or validation exists) refuting hypothesis '{hyp.hypothesis_id}': {contradiction['reason']}",
                        provenance_tool="forge:diagnosis:contradiction_detection",
                    )
                    contradicting_evidence_records.append(c_evidence)
                    hyp.contradicting_evidence_ids.append(c_evidence.evidence_id)
                except Exception:
                    pass

            # 4. Rigorously score hypothesis confidence
            evaluated_hyp = self._evaluate_hypothesis_confidence(hyp, input_data)
            grounded_hypotheses.append(evaluated_hyp)

        def _hyp_sort_key(h: DiagnosisHypothesis):
            is_test = (
                h.suspected_location.file_path.startswith("tests/")
                or "/tests/" in h.suspected_location.file_path
                or Path(h.suspected_location.file_path).name.startswith("test_")
            )
            return (1 if is_test else 0, -h.confidence_score, h.hypothesis_id)

        grounded_hypotheses.sort(key=_hyp_sort_key)
        return grounded_hypotheses, supporting_evidence_records, contradicting_evidence_records

    # -----------------------------------------------------------------------
    # Hypothesis Generation
    # -----------------------------------------------------------------------

    def _generate_hypotheses(
        self,
        input_data: DiagnosisInput,
        candidate_locations: List[CandidateLocation],
    ) -> List[DiagnosisHypothesis]:
        """Generate candidate hypotheses deterministically, augmented by LLM if available."""
        hypotheses: List[DiagnosisHypothesis] = []

        # Deterministic hypothesis for top candidate locations
        for idx, loc in enumerate(candidate_locations[:5], start=1):
            category = self._infer_defect_category(input_data, loc)
            desc = self._formulate_hypothesis_description(input_data, loc, category)
            hyp_id = f"hyp_{hashlib.sha256(f'{loc.file_path}:{loc.start_line}:{category.value}'.encode()).hexdigest()[:8]}"

            missing_info: List[str] = []
            if not loc.is_verified_on_disk:
                missing_info.append("Candidate location is not verified against current disk content.")
            if not input_data.test_failure and not input_data.stack_trace:
                missing_info.append("No runtime stack trace or failing test assertion available to confirm crash behavior.")

            verification_method = (
                f"Inspect '{loc.file_path}' lines {loc.start_line}–{loc.end_line} and execute regression test covering {loc.symbol_id or loc.file_path}."
            )

            hypotheses.append(
                DiagnosisHypothesis(
                    hypothesis_id=hyp_id,
                    description=desc,
                    suspected_location=loc,
                    defect_category=category,
                    confidence=FindingConfidence.LOW,
                    confidence_score=0.40,
                    missing_information=missing_info,
                    suggested_verification_method=verification_method,
                )
            )

        # Attempt LLM hypothesis enrichment if provider and context builder are available
        if self.provider and self.context_builder:
            llm_hyp = self._prompt_llm_hypotheses(input_data, candidate_locations)
            if llm_hyp:
                # Merge enriched explanations without overriding disk locations
                for h in hypotheses:
                    if h.suspected_location.file_path in llm_hyp:
                        h.description = f"{h.description} (LLM Analysis: {llm_hyp[h.suspected_location.file_path]})"

        return hypotheses

    def _infer_defect_category(self, input_data: DiagnosisInput, loc: CandidateLocation) -> DefectCategory:
        """Deterministically infer most appropriate defect category from error signals."""
        combined_text = " ".join(filter(None, [
            input_data.error_message,
            input_data.user_description,
            input_data.test_failure.failure_message if input_data.test_failure else None,
            input_data.stack_trace,
            loc.reason,
        ]))

        for pattern, cat in CATEGORY_PATTERNS:
            if pattern.search(combined_text):
                return cat

        return DefectCategory.UNKNOWN

    def _formulate_hypothesis_description(
        self,
        input_data: DiagnosisInput,
        loc: CandidateLocation,
        category: DefectCategory,
    ) -> str:
        """Formulate a specific, descriptive explanation of the suspected failure mechanism."""
        err = input_data.error_message or (input_data.test_failure.failure_message if input_data.test_failure else "")
        sym = f"'{loc.symbol_id}'" if loc.symbol_id else f"code in '{loc.file_path}'"

        if category == DefectCategory.INCORRECT_DATA_HANDLING:
            return f"Data handling defect in {sym} (lines {loc.start_line}–{loc.end_line}): Unexpected data type or index/key out of bounds caused failure: {err[:80]}"
        elif category == DefectCategory.INPUT_VALIDATION_FAILURE:
            return f"Input validation defect in {sym} (lines {loc.start_line}–{loc.end_line}): Missing or defective precondition validation failed to reject invalid input: {err[:80]}"
        elif category == DefectCategory.LOGIC_ERROR:
            return f"Logic defect in {sym} (lines {loc.start_line}–{loc.end_line}): Incorrect conditional branching or invariant violation produced failing state: {err[:80]}"
        elif category == DefectCategory.RESOURCE_MANAGEMENT_ISSUE:
            return f"Resource management defect in {sym} (lines {loc.start_line}–{loc.end_line}): Unhandled file, handle, or connection lifecycle error: {err[:80]}"
        elif category == DefectCategory.DEPENDENCY_INTEGRATION_DEFECT:
            return f"Dependency or import integration defect in {sym} (lines {loc.start_line}–{loc.end_line}): Broken import, cycle, or incompatible external interface: {err[:80]}"
        else:
            return f"Defect suspected in {sym} at {loc.file_path}:{loc.start_line}–{loc.end_line} associated with: {err[:80] or loc.reason}"

    # -----------------------------------------------------------------------
    # Contradiction Detection
    # -----------------------------------------------------------------------

    def _detect_contradiction(
        self,
        hyp: DiagnosisHypothesis,
        input_data: DiagnosisInput,
    ) -> Optional[Dict[str, Any]]:
        """Identify if actual source code on disk directly contradicts the hypothesis."""
        loc = hyp.suspected_location
        full_path = self.workspace_path / loc.file_path
        if not full_path.exists() or not full_path.is_file():
            return None

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            # Start with candidate location's own lines
            func_start = loc.start_line
            func_end = loc.end_line
            candidate_slice = lines[loc.start_line - 1 : loc.end_line]

            # Expand backwards up to the enclosing 'def' or at most 10 lines
            prefix = []
            if not any(l.strip().startswith(("def ", "async def ")) for l in candidate_slice):
                for i in range(loc.start_line - 2, max(-1, loc.start_line - 12), -1):
                    prefix.insert(0, lines[i])
                    func_start = i + 1
                    if lines[i].strip().startswith(("def ", "async def ")):
                        break

            # Expand forwards up to the next 'def' or at most 10 lines
            suffix = []
            for i in range(loc.end_line, min(len(lines), loc.end_line + 10)):
                if lines[i].strip().startswith(("def ", "async def ")):
                    break
                suffix.append(lines[i])
                func_end = i + 1

            surrounding_text = "\n".join(prefix + candidate_slice + suffix)

            # Contradiction Scenario A: Hypothesis claims missing validation, but explicit guard exists
            if hyp.defect_category in (DefectCategory.INPUT_VALIDATION_FAILURE, DefectCategory.LOGIC_ERROR, DefectCategory.UNKNOWN):
                if ("raise ValueError" in surrounding_text or "raise TypeError" in surrounding_text) and ("if " in surrounding_text or "assert " in surrounding_text):
                    combined_claim = f"{hyp.description} {input_data.error_message or ''} {input_data.user_description or ''} {loc.reason}".lower()
                    if any(kw in combined_claim for kw in ("missing", "check", "validation", "guard", "unvalidated", "unverified")):
                        return {
                            "file_path": loc.file_path,
                            "start_line": func_start,
                            "end_line": func_end,
                            "reason": "Source code contains explicit input validation raising ValueError/TypeError in candidate span.",
                        }

            # Contradiction Scenario B: Hypothesis claims unhandled exception, but explicit try/except handles it
            if hyp.defect_category == DefectCategory.EXCEPTION_HANDLING_DEFECT:
                if "try:" in surrounding_text and "except " in surrounding_text:
                    return {
                        "file_path": loc.file_path,
                        "start_line": clamped_start,
                        "end_line": clamped_end,
                        "reason": "Candidate location contains explicit try...except block handling exceptions.",
                    }

        except Exception:
            pass

        return None

    # -----------------------------------------------------------------------
    # Hypothesis Evaluation & Strict Confidence Scoring
    # -----------------------------------------------------------------------

    def _evaluate_hypothesis_confidence(
        self,
        hyp: DiagnosisHypothesis,
        input_data: DiagnosisInput,
    ) -> DiagnosisHypothesis:
        """Calibrate numeric confidence score and assign FindingConfidence level.

        CRITICAL INVARIANT: Model suspicion alone CANNOT promote a hypothesis to high confidence.
        """
        score = hyp.suspected_location.confidence_weight

        # Disk verification requirement
        if not hyp.suspected_location.is_verified_on_disk:
            # Fabricated or missing file on disk completely invalidates confidence
            hyp.confidence_score = 0.0
            hyp.confidence = FindingConfidence.LOW
            hyp.missing_information.append("File does not exist on disk or lines are out of bounds.")
            return hyp

        # Evidence grounding bonus
        if hyp.supporting_evidence_ids:
            score += 0.10

        # Runtime crash / reproduction confirmation bonus
        if hyp.suspected_location.source == LocationSource.STACK_TRACE:
            score += 0.10
        elif hyp.suspected_location.source == LocationSource.TEST_FAILURE:
            score += 0.05

        # Contradiction penalty
        if hyp.contradicting_evidence_ids:
            score -= 0.40
            hyp.missing_information.append(f"Contradicting evidence exists ({len(hyp.contradicting_evidence_ids)} records).")

        # Unknown defect category penalty
        if hyp.defect_category == DefectCategory.UNKNOWN:
            score -= 0.15

        # Clamp between 0.0 and 1.0
        final_score = max(0.0, min(1.0, round(score, 2)))
        hyp.confidence_score = final_score

        if final_score >= 0.75:
            hyp.confidence = FindingConfidence.HIGH
        elif final_score >= 0.45:
            hyp.confidence = FindingConfidence.MEDIUM
        else:
            hyp.confidence = FindingConfidence.LOW

        return hyp

    # -----------------------------------------------------------------------
    # LLM Interaction (Local-Only)
    # -----------------------------------------------------------------------

    def _prompt_llm_hypotheses(
        self,
        input_data: DiagnosisInput,
        candidate_locations: List[CandidateLocation],
    ) -> Dict[str, str]:
        """Query local Ollama provider for targeted hypothesis reasoning."""
        if not self.provider or not self.context_builder:
            return {}

        target_files = [c.file_path for c in candidate_locations[:3]]
        query = input_data.error_message or input_data.user_description or "Diagnose failure"

        try:
            built_ctx = self.context_builder.build_context(
                query=query[:200],
                target_files=target_files,
                char_budget=8000,
            )

            prompt = (
                f"You are the FORGE Defect Diagnosis Engine. Analyze the following failure context:\n"
                f"Failure Description: {input_data.user_description or 'None'}\n"
                f"Error Message: {input_data.error_message or 'None'}\n\n"
                f"{built_ctx.rendered_context}\n\n"
                "Return a compact JSON object mapping file paths to a 1-sentence explanation of suspected root cause, "
                'e.g. {"path/to/file.py": "Description of defect"}. Do not fabricate non-existent files.'
            )

            request = LLMRequest(
                messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
                temperature=0.0,
                max_tokens=256,
            )

            response = self.provider.generate(request)
            content = (response.content or "").strip()
            # Attempt to parse json from content
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception:
            # Fails safe on provider errors, timeouts, or malformed JSON
            pass

        return {}
