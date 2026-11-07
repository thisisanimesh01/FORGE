"""Phase 12 M2: Root-Cause Analysis Engine.

Orchestrates causal reconstruction from verified defect evidence, failure symptoms,
cross-file call graphs, module dependencies, and AST contradiction inspection.

Enforces Core Invariants:
- A causal explanation must NEVER be labeled PROVEN unless its supporting relationships
  are independently verified against direct source evidence and call/dependency graphs.
- Distinguishes symptom, contributing factor, causal mechanism, and root cause.
- A stack-trace frame is not automatically the root cause; a dependency is not proof of causation;
  an LLM explanation is not evidence.
- Preserves unresolved uncertainty and avoids forcing a single root cause when multiple are plausible.
- Local Ollama only: zero cloud inference, zero cloud fallback.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from forge.causality.contradiction import ASTContradictionInspector
from forge.causality.schemas import (
    AlternativeExplanation,
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    CausalStatus,
    RootCauseReport,
)
from forge.causality.tracer import CausalTracer
from forge.diagnosis.schemas import (
    CandidateLocation,
    DefectCategory,
    DiagnosisInput,
    DiagnosisResult,
    DiagnosisStatus,
    TestFailureReport,
)
from forge.llm.context import RepositoryContextBuilder
from forge.llm.evidence_grounding import EvidenceGroundingEngine, verify_evidence_freshness
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.provider import LLMProvider, LLMUnavailableError
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.schemas import EvidenceRecord, FindingConfidence, FindingSeverity

if TYPE_CHECKING:
    from forge.diagnosis.engine import EvidenceDrivenDiagnosisEngine
    from forge.storage.store import ForgeStore


class RootCauseAnalysisEngine:
    """Core production-grade Root-Cause Analysis Engine for FORGE Phase 12 M2."""

    def __init__(
        self,
        workspace_path: Path,
        run_id: str = "run_default",
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        store: Optional["ForgeStore"] = None,
        provider: Optional[LLMProvider] = None,
        llm_provider: Optional[LLMProvider] = None,
        context_builder: Optional[RepositoryContextBuilder] = None,
        diagnosis_engine: Optional["EvidenceDrivenDiagnosisEngine"] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.run_id = run_id
        self._incremental_index = incremental_index
        self.store = store
        self.provider = provider or llm_provider
        self._context_builder = context_builder
        self._diagnosis_engine = diagnosis_engine

        # Lazily initialized sub-components
        self._tracer: Optional[CausalTracer] = None
        self._contradiction_inspector: Optional[ASTContradictionInspector] = None
        self._evidence_engine: Optional[EvidenceGroundingEngine] = None

    def get_incremental_index(self) -> IncrementalRepositoryIndex:
        if self._incremental_index is None:
            self._incremental_index = IncrementalRepositoryIndex(repo_root=self.workspace_path)
        return self._incremental_index

    def get_evidence_engine(self) -> EvidenceGroundingEngine:
        if self._evidence_engine is None:
            idx = self.get_incremental_index()
            self._evidence_engine = EvidenceGroundingEngine(
                workspace_path=self.workspace_path,
                run_id=self.run_id,
                incremental_index=idx,
                store=self.store,
            )
        return self._evidence_engine

    def get_tracer(self) -> CausalTracer:
        if self._tracer is None:
            idx = self.get_incremental_index()
            self._tracer = CausalTracer(
                workspace_path=self.workspace_path,
                call_graph=idx.get_call_graph(),
                module_graph=idx.get_module_graph(),
                symbol_index=idx.get_symbol_index(),
                incremental_index=idx,
                evidence_engine=self.get_evidence_engine(),
            )
        return self._tracer

    def get_contradiction_inspector(self) -> ASTContradictionInspector:
        if self._contradiction_inspector is None:
            self._contradiction_inspector = ASTContradictionInspector(workspace_path=self.workspace_path)
        return self._contradiction_inspector

    def analyze(
        self,
        diagnosis: Optional[DiagnosisResult] = None,
        diagnosis_input: Optional[DiagnosisInput] = None,
        test_failure: Optional[TestFailureReport] = None,
        failing_symbol: Optional[str] = None,
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
        error_message: Optional[str] = None,
        changed_files: Optional[List[str]] = None,
        max_depth: int = 4,
        persist_finding: bool = True,
    ) -> RootCauseReport:
        """Reconstruct causal chain and determine root-cause status."""
        tracer = self.get_tracer()
        inspector = self.get_contradiction_inspector()
        evidence_engine = self.get_evidence_engine()

        # 1. Validate input signal
        has_signal = any([
            diagnosis is not None,
            diagnosis_input is not None,
            test_failure is not None,
            bool(failing_symbol),
            bool(file_path and line_number),
            bool(error_message),
        ])
        if not has_signal:
            raise ValueError("RootCauseAnalysisEngine requires at least one diagnostic or failure signal.")

        limitations: List[str] = []
        unresolved_questions: List[str] = []
        evidence_refs: List[str] = []

        # 2. Extract or infer diagnosis if needed
        diag_id = diagnosis.diagnosis_id if diagnosis else None
        defect_cat = diagnosis.defect_category if diagnosis else DefectCategory.UNKNOWN

        if diagnosis is None and diagnosis_input is not None and self._diagnosis_engine is not None:
            try:
                diagnosis = self._diagnosis_engine.diagnose(diagnosis_input, persist_finding=False)
                diag_id = diagnosis.diagnosis_id
                defect_cat = diagnosis.defect_category
            except Exception:
                pass

        # 3. Audit freshness of existing evidence
        stale_evidence_found = False
        if diagnosis:
            evidence_refs.extend(diagnosis.supporting_evidence_ids)
            if self.store:
                for eid in diagnosis.supporting_evidence_ids:
                    try:
                        ev_rec = self.store.get_evidence(eid)
                        if ev_rec:
                            is_fresh, stale_reason = evidence_engine.verify_evidence_freshness(ev_rec)
                            if not is_fresh:
                                stale_evidence_found = True
                                limitations.append(f"Stale evidence detected for {eid}: {stale_reason}")
                                unresolved_questions.append(f"Evidence {eid} does not reflect current disk state.")
                    except Exception:
                        pass

        # 4. Determine Symptom Location
        symptom_node = self._locate_symptom(
            diagnosis=diagnosis,
            test_failure=test_failure,
            failing_symbol=failing_symbol,
            file_path=file_path,
            line_number=line_number,
            error_message=error_message,
        )

        if symptom_node is None or not symptom_node.is_verified_on_disk:
            # Cannot locate verified failure symptom
            rep_key = f"{self.run_id}:{diag_id or file_path}".encode()
            report_id = f"rc_{hashlib.sha256(rep_key).hexdigest()[:8]}"
            limitations.append("Failure symptom could not be verified against current repository source on disk.")
            unresolved_questions.append("Where is the precise execution point of failure?")
            return RootCauseReport(
                report_id=report_id,
                diagnosis_id=diag_id,
                run_id=self.run_id,
                status=CausalStatus.INSUFFICIENT_EVIDENCE,
                primary_root_cause=None,
                contributing_factors=[],
                symptom_locations=[symptom_node] if symptom_node else [],
                nodes=[symptom_node] if symptom_node else [],
                edges=[],
                causal_chains=[],
                evidence_references=evidence_refs,
                alternative_explanations=[],
                unresolved_questions=unresolved_questions,
                confidence=FindingConfidence.LOW,
                confidence_score=0.20,
                limitations="; ".join(limitations),
            )

        evidence_refs.extend(symptom_node.supporting_evidence_ids)

        # 5. Backward Causal Tracing
        backward_nodes, backward_edges = tracer.trace_backward(
            symptom_node=symptom_node,
            max_depth=max_depth,
            changed_files=changed_files,
        )

        nodes_by_id: Dict[str, CausalNode] = {n.node_id: n for n in backward_nodes}
        edges_by_id: Dict[str, CausalEdge] = {e.edge_id: e for e in backward_edges}

        # 6. AST Contradiction Analysis
        alternative_explanations: List[AlternativeExplanation] = []
        claimed_error = error_message or (test_failure.failure_message if test_failure else "")
        if diagnosis:
            claimed_error = f"{claimed_error} {diagnosis.failing_behavior} {diagnosis.input_summary}"

        # Inspect symptom and candidate nodes for contradictions
        refuted_nodes: Set[str] = set()
        for node in list(nodes_by_id.values()):
            if node.is_verified_on_disk:
                contradiction = inspector.detect_contradiction(
                    file_path=node.file_path,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    claimed_defect=claimed_error,
                    category=defect_cat,
                )
                if contradiction:
                    # AST directly refutes a defect claim in this location
                    refuted_nodes.add(node.node_id)
                    contra_line = contradiction.get("line")
                    alt_hash_key = f"{node.node_id}:{contra_line}".encode()
                    alt_id = f"alt_{hashlib.sha256(alt_hash_key).hexdigest()[:8]}"
                    alt = AlternativeExplanation(
                        explanation_id=alt_id,
                        suspected_root_cause=f"Defect at {node.file_path}:{node.start_line}",
                        rationale=contradiction.get("detail", "Refuted by AST inspection."),
                        confidence=FindingConfidence.LOW,
                        confidence_score=0.10,
                        contradicted_by=[contradiction.get("detail", "")],
                        is_refuted=True,
                    )
                    alternative_explanations.append(alt)
                    limitations.append(f"Contradiction identified at {node.file_path}:{contradiction.get('line')}: {contradiction.get('detail')}")

        # 7. Identify Candidate Root Causes
        candidate_roots: List[CausalNode] = []

        # Find nodes marked as ROOT_CAUSE or origin callers (no incoming production callers), excluding test harnesses
        incoming_prod_targets = {
            e.target_node_id
            for e in edges_by_id.values()
            if e.source_node_id in nodes_by_id and not self._is_test_file(nodes_by_id[e.source_node_id].file_path)
        }
        explicit_roots = [
            n for n in nodes_by_id.values()
            if n.node_id not in refuted_nodes
            and n.node_id != symptom_node.node_id
            and n.role == CausalRole.ROOT_CAUSE
            and not self._is_test_file(n.file_path)
        ]

        if explicit_roots:
            candidate_roots.extend(explicit_roots)
        else:
            for node in nodes_by_id.values():
                if node.node_id in refuted_nodes or node.node_id == symptom_node.node_id:
                    continue
                if self._is_test_file(node.file_path):
                    continue
                if node.node_id not in incoming_prod_targets:
                    candidate_roots.append(node)

        # If diagnosis provided candidate locations, integrate them if not yet present
        if diagnosis and not candidate_roots:
            for loc in diagnosis.root_cause_locations:
                # Skip test files from being marked as root cause
                if self._is_test_file(loc.file_path):
                    continue
                loc_node = tracer.create_node(
                    file_path=loc.file_path,
                    start_line=loc.start_line,
                    end_line=loc.end_line,
                    role=CausalRole.ROOT_CAUSE,
                    symbol_id=loc.symbol_id,
                    description=loc.reason,
                )
                if loc_node.node_id not in nodes_by_id and loc_node.node_id not in refuted_nodes:
                    nodes_by_id[loc_node.node_id] = loc_node
                    candidate_roots.append(loc_node)

                    # Connect with inferred or calls edge to symptom if not already connected
                    if loc_node.node_id != symptom_node.node_id:
                        inferred_edge = tracer.create_edge(
                            source_node=loc_node,
                            target_node=symptom_node,
                            relationship=CausalRelationship.TRIGGERS_FAILURE,
                            explanation=f"Diagnosis candidate {loc.file_path}:{loc.start_line} triggers symptom",
                            is_directly_verified=False,
                            confidence=FindingConfidence.MEDIUM,
                            confidence_score=0.65,
                        )
                        edges_by_id[inferred_edge.edge_id] = inferred_edge

        # 8. Synthesize Causal Chains
        causal_chains = tracer.synthesize_chains(
            candidate_roots=candidate_roots,
            symptom_node=symptom_node,
            nodes=list(nodes_by_id.values()),
            edges=list(edges_by_id.values()),
        )

        # 9. Local Ollama LLM Enrichment (Optional, Fail-Closed)
        if self.provider:
            self._enrich_with_ollama(
                symptom_node=symptom_node,
                candidate_roots=candidate_roots,
                chains=causal_chains,
                alternative_explanations=alternative_explanations,
            )

        # 10. Status and Root-Cause Selection
        status = CausalStatus.UNRESOLVED
        primary_root: Optional[CausalNode] = None
        contributing_factors: List[CausalNode] = []
        overall_confidence = FindingConfidence.LOW
        conf_score = 0.40

        if stale_evidence_found:
            status = CausalStatus.INSUFFICIENT_EVIDENCE
            conf_score = 0.35
            overall_confidence = FindingConfidence.LOW
        elif not causal_chains and not candidate_roots:
            # Symptom exists alone with no identifiable upstream chain or cause
            status = CausalStatus.INSUFFICIENT_EVIDENCE
            primary_root = symptom_node
            conf_score = 0.45
            overall_confidence = FindingConfidence.LOW
            unresolved_questions.append("No upstream callers or dependency triggers identified for the observed symptom.")
        elif len(causal_chains) == 1 or (len(candidate_roots) == 1 and len(causal_chains) >= 1):
            best_chain = causal_chains[0]
            primary_root = nodes_by_id.get(best_chain.root_node_id)
            for cid in best_chain.node_ids[1:-1]:
                if cid in nodes_by_id:
                    contributing_factors.append(nodes_by_id[cid])

            # Validate that every node and edge in the chain is directly verified
            chain_edges = [edges_by_id[eid] for eid in best_chain.edge_ids if eid in edges_by_id]
            has_unverified_edge = any(not e.is_directly_verified for e in chain_edges)
            has_structural_dep = any(e.relationship == CausalRelationship.DEPENDS_ON for e in chain_edges)
            all_nodes_verified = all(
                nodes_by_id[nid].is_verified_on_disk
                for nid in best_chain.node_ids
                if nid in nodes_by_id
            ) and len(best_chain.node_ids) == sum(1 for nid in best_chain.node_ids if nid in nodes_by_id)

            if (
                best_chain.is_fully_verified
                and not has_unverified_edge
                and not has_structural_dep
                and primary_root
                and primary_root.is_verified_on_disk
                and all_nodes_verified
            ):
                status = CausalStatus.PROVEN
                conf_score = round(max(0.85, best_chain.confidence_score), 2)
                overall_confidence = FindingConfidence.HIGH
            else:
                status = CausalStatus.PLAUSIBLE
                conf_score = round(min(0.80, best_chain.confidence_score), 2)
                overall_confidence = FindingConfidence.MEDIUM
        elif len(causal_chains) > 1:
            # Multiple competing chains
            root_ids = {c.root_node_id for c in causal_chains}
            if len(root_ids) > 1:
                status = CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES
                conf_score = 0.60
                overall_confidence = FindingConfidence.MEDIUM
                unresolved_questions.append(f"Identified {len(root_ids)} distinct candidate root causes without sufficient differentiating test evidence.")
                # Mark candidates as contributing factors
                for rid in root_ids:
                    if rid in nodes_by_id:
                        contributing_factors.append(nodes_by_id[rid])
            else:
                # Same root, multiple paths
                best_chain = causal_chains[0]
                primary_root = nodes_by_id.get(best_chain.root_node_id)
                status = CausalStatus.PLAUSIBLE
                conf_score = round(best_chain.confidence_score, 2)
                overall_confidence = FindingConfidence.MEDIUM
        else:
            status = CausalStatus.UNRESOLVED
            conf_score = 0.30
            overall_confidence = FindingConfidence.LOW

        for cnode in nodes_by_id.values():
            evidence_refs.extend(cnode.supporting_evidence_ids)
        for cedge in edges_by_id.values():
            evidence_refs.extend(cedge.evidence_references)

        evidence_refs = sorted(list(set(evidence_refs)))
        report_key = f"{self.run_id}:{symptom_node.node_id}:{status.value}".encode()
        report_id = f"rc_{hashlib.sha256(report_key).hexdigest()[:8]}"

        report = RootCauseReport(
            report_id=report_id,
            diagnosis_id=diag_id,
            run_id=self.run_id,
            status=status,
            primary_root_cause=primary_root,
            contributing_factors=contributing_factors,
            symptom_locations=[symptom_node],
            nodes=list(nodes_by_id.values()),
            edges=list(edges_by_id.values()),
            causal_chains=causal_chains,
            evidence_references=evidence_refs,
            alternative_explanations=alternative_explanations,
            unresolved_questions=unresolved_questions,
            confidence=overall_confidence,
            confidence_score=conf_score,
            limitations="; ".join(limitations) if limitations else None,
            metadata={
                "nodes_count": len(nodes_by_id),
                "edges_count": len(edges_by_id),
                "chains_count": len(causal_chains),
                "stale_evidence": stale_evidence_found,
            },
        )

        return report

    def _locate_symptom(
        self,
        diagnosis: Optional[DiagnosisResult],
        test_failure: Optional[TestFailureReport],
        failing_symbol: Optional[str],
        file_path: Optional[str],
        line_number: Optional[int],
        error_message: Optional[str],
    ) -> Optional[CausalNode]:
        """Locate and instantiate the failure symptom CausalNode."""
        tracer = self.get_tracer()

        # 1. Direct explicit file and line
        if file_path and line_number:
            return tracer.create_node(
                file_path=file_path,
                start_line=line_number,
                end_line=line_number,
                role=CausalRole.SYMPTOM,
                symbol_id=failing_symbol,
                description=f"Failure symptom at {file_path}:{line_number} ({error_message or 'runtime failure'})",
            )

        # 2. Extract from test failure report
        if test_failure:
            t_file = test_failure.test_file or "tests/test_failure.py"
            t_line = test_failure.line_number or 1
            return tracer.create_node(
                file_path=t_file,
                start_line=t_line,
                end_line=t_line,
                role=CausalRole.SYMPTOM,
                node_type=CausalNodeType.TEST,
                description=f"Failing test: {test_failure.test_name} - {test_failure.failure_message[:100]}",
            )

        # 3. Extract from diagnosis
        if diagnosis:
            if diagnosis.root_cause_locations:
                loc = diagnosis.root_cause_locations[0]
                return tracer.create_node(
                    file_path=loc.file_path,
                    start_line=loc.start_line,
                    end_line=loc.end_line,
                    role=CausalRole.SYMPTOM,
                    symbol_id=loc.symbol_id,
                    description=f"Diagnosed symptom: {diagnosis.failing_behavior[:100]}",
                )

        return None

    def _enrich_with_ollama(
        self,
        symptom_node: CausalNode,
        candidate_roots: List[CausalNode],
        chains: List[CausalChain],
        alternative_explanations: List[AlternativeExplanation],
    ) -> None:
        """Query local Ollama to explain retrieved evidence and rank alternatives. Fail-closed."""
        if not self.provider:
            return

        prompt = (
            f"You are a strict causal analysis engine. Given failure symptom at {symptom_node.file_path}:{symptom_node.start_line} "
            f"and {len(candidate_roots)} candidate causes, summarize the failure narrative.\n"
            f"Output strictly valid JSON with keys 'summary' (str) and 'alternative' (str or null)."
        )

        try:
            req = LLMRequest(
                messages=[
                    ChatMessage(role=MessageRole.SYSTEM, content="Analyze software root causes with zero fabrication."),
                    ChatMessage(role=MessageRole.USER, content=prompt),
                ],
                temperature=0.0,
                max_tokens=400,
            )
            resp = self.provider.complete(req)
            data = json.loads(resp.content)
            if isinstance(data, dict):
                alt_text = data.get("alternative")
                if alt_text and isinstance(alt_text, str) and len(alt_text) > 10:
                    alt_id = f"alt_llm_{hashlib.sha256(alt_text.encode()).hexdigest()[:8]}"
                    alternative_explanations.append(
                        AlternativeExplanation(
                            explanation_id=alt_id,
                            suspected_root_cause="Alternative causal hypothesis",
                            rationale=alt_text[:200],
                            confidence=FindingConfidence.LOW,
                            confidence_score=0.30,
                            is_refuted=False,
                        )
                    )
        except Exception:
            # Local Ollama unavailable or invalid JSON; fail closed gracefully
            pass

    def _is_test_file(self, file_path: str) -> bool:
        p = file_path.replace("\\", "/").lower()
        return p.startswith("tests/") or "/tests/" in p or Path(p).name.startswith("test_")
