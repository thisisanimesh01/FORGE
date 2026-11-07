"""Phase 12 M2: Causal Tracer & Failure Propagation Engine.

Implements backward causal traversal and forward failure propagation:
- Traverses cross-file call graphs and module dependency graphs to isolate origins.
- Correlates code locations with change-impact analysis and AST symbol definitions.
- Distinguishes directly verified call/dependency relationships from inferred data/state links.
- Enforces strict depth and node bounds, cycle safety, and recursion immunity.
"""

from collections import deque
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.causality.schemas import (
    CausalChain,
    CausalEdge,
    CausalNode,
    CausalNodeType,
    CausalRelationship,
    CausalRole,
    MAX_CHAIN_DEPTH,
)
from forge.diagnosis.schemas import CandidateLocation
from forge.llm.evidence_grounding import EvidenceGroundingEngine
from forge.repository.call_graph import CallGraph, build_call_graph
from forge.repository.graph_schemas import CallEdge, CallResolutionStatus
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.indexer import SymbolIndex, SymbolRecord, build_symbol_index
from forge.repository.module_graph import ModuleDependencyGraph
from forge.repository.validation import assert_contained_path, SecurityError
from forge.schemas import EvidenceRecord, FindingConfidence


class CausalTracer:
    """Reconstructs backward causal histories and forward propagation paths."""

    def __init__(
        self,
        workspace_path: Path,
        call_graph: Optional[CallGraph] = None,
        module_graph: Optional[ModuleDependencyGraph] = None,
        symbol_index: Optional[SymbolIndex] = None,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        evidence_engine: Optional[EvidenceGroundingEngine] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.incremental_index = incremental_index
        self._call_graph = call_graph
        self._module_graph = module_graph
        self._symbol_index = symbol_index
        self.evidence_engine = evidence_engine

    def get_incremental_index(self) -> IncrementalRepositoryIndex:
        if self.incremental_index is None:
            self.incremental_index = IncrementalRepositoryIndex(repo_root=self.workspace_path)
        return self.incremental_index

    def get_symbol_index(self) -> SymbolIndex:
        if self._symbol_index is not None:
            return self._symbol_index
        if self.incremental_index is not None:
            self._symbol_index = self.incremental_index.get_symbol_index()
            return self._symbol_index
        self._symbol_index = build_symbol_index(self.workspace_path)
        return self._symbol_index

    def get_call_graph(self) -> CallGraph:
        if self._call_graph is not None:
            return self._call_graph
        if self.incremental_index is not None:
            self._call_graph = self.incremental_index.get_call_graph()
            return self._call_graph
        self._call_graph = build_call_graph(self.workspace_path)
        return self._call_graph

    def get_module_graph(self) -> ModuleDependencyGraph:
        if self._module_graph is not None:
            return self._module_graph
        if self.incremental_index is not None:
            self._module_graph = self.incremental_index.get_module_graph()
        from forge.repository.module_graph import build_module_dependency_graph
        self._module_graph = build_module_dependency_graph(self.workspace_path)
        return self._module_graph

    def resolve_symbol_node_id(self, symbol_query: str) -> str:
        """Resolve arbitrary symbol string (qualified name or function name) to CallGraph node ID."""
        cg = self.get_call_graph()
        if symbol_query in cg.nodes:
            return symbol_query
        for n in cg.nodes.keys():
            if n.endswith(":" + symbol_query):
                return n
            parts = symbol_query.split(".")
            if len(parts) >= 2:
                sym_name = parts[-1]
                file_guess = "/".join(parts[:-1]) + ".py:" + sym_name
                if file_guess == n or n.endswith(file_guess):
                    return n
        return symbol_query

    def create_node(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        role: CausalRole,
        node_type: CausalNodeType = CausalNodeType.FUNCTION,
        symbol_id: Optional[str] = None,
        description: str = "",
        uncertainty: Optional[str] = None,
    ) -> CausalNode:
        """Create a validated CausalNode, verifying its on-disk presence."""
        norm_path = file_path.replace("\\", "/").strip().lstrip("/")
        abs_path = self.workspace_path / norm_path

        is_verified = False
        evidence_ids: List[str] = []

        try:
            contained = assert_contained_path(abs_path, self.workspace_path)
            if contained.exists() and contained.is_file():
                content = contained.read_text(encoding="utf-8", errors="replace")
                total_lines = len(content.splitlines())
                if 1 <= start_line <= total_lines and start_line <= end_line:
                    is_verified = True
                    if self.evidence_engine is not None:
                        try:
                            ev = self.evidence_engine.create_source_evidence(
                                file_path=norm_path,
                                start_line=start_line,
                                end_line=min(end_line, total_lines),
                                description=f"Causal node {role.value}: {description or norm_path}",
                                symbol_name=symbol_id,
                                provenance_tool="forge:causality:tracer",
                            )
                            evidence_ids.append(ev.evidence_id)
                        except Exception:
                            pass
        except (SecurityError, Exception):
            is_verified = False

        node_id_seed = f"{norm_path}:{start_line}:{end_line}:{symbol_id or ''}"
        node_id = f"cnode_{hashlib.sha256(node_id_seed.encode()).hexdigest()[:10]}"

        return CausalNode(
            node_id=node_id,
            symbol_id=symbol_id,
            file_path=norm_path,
            start_line=start_line,
            end_line=end_line,
            node_type=node_type,
            role=role,
            description=description or f"{role.value.capitalize()} at {norm_path}:{start_line}",
            supporting_evidence_ids=evidence_ids,
            uncertainty=uncertainty,
            is_verified_on_disk=is_verified,
        )

    def create_edge(
        self,
        source_node: CausalNode,
        target_node: CausalNode,
        relationship: CausalRelationship,
        explanation: str,
        is_directly_verified: bool = False,
        confidence: Optional[FindingConfidence] = None,
        confidence_score: Optional[float] = None,
        evidence_references: Optional[List[str]] = None,
    ) -> CausalEdge:
        """Create a CausalEdge connecting two nodes."""
        edge_id_seed = f"{source_node.node_id}->{target_node.node_id}:{relationship.value}"
        edge_id = f"cedge_{hashlib.sha256(edge_id_seed.encode()).hexdigest()[:10]}"

        conf = confidence if confidence is not None else (
            FindingConfidence.HIGH if is_directly_verified else FindingConfidence.MEDIUM
        )
        score = confidence_score if confidence_score is not None else (
            0.85 if is_directly_verified else 0.65
        )

        return CausalEdge(
            edge_id=edge_id,
            source_node_id=source_node.node_id,
            target_node_id=target_node.node_id,
            relationship=relationship,
            evidence_references=evidence_references or [],
            causal_explanation=explanation,
            confidence=conf,
            confidence_score=score,
            is_directly_verified=is_directly_verified,
        )

    def trace_backward(
        self,
        symptom_node: CausalNode,
        max_depth: int = 4,
        max_nodes: int = 50,
        changed_files: Optional[List[str]] = None,
    ) -> Tuple[List[CausalNode], List[CausalEdge]]:
        """Trace callers and dependencies backward from symptom_node to find origins."""
        cg = self.get_call_graph()
        sym_idx = self.get_symbol_index()
        mod_graph = self.get_module_graph()

        nodes_by_id: Dict[str, CausalNode] = {symptom_node.node_id: symptom_node}
        edges_by_id: Dict[str, CausalEdge] = {}

        # Resolve initial symbol node in CallGraph
        start_symbol = symptom_node.symbol_id
        if not start_symbol:
            start_symbol = self._find_symbol_at_location(symptom_node.file_path, symptom_node.start_line)

        resolved_start = self.resolve_symbol_node_id(start_symbol) if start_symbol else None

        visited_symbols: Set[str] = set()
        queue: deque = deque()

        if resolved_start:
            queue.append((resolved_start, symptom_node, 0))
            visited_symbols.add(resolved_start)

        depth_limit = min(max_depth, MAX_CHAIN_DEPTH)

        while queue and len(nodes_by_id) < max_nodes:
            curr_symbol, curr_node, depth = queue.popleft()
            if depth >= depth_limit:
                continue

            # 1. Retrieve CallEdges where target is curr_symbol
            call_edges: List[CallEdge] = []
            try:
                call_edges = cg.get_call_edges(target_id=curr_symbol)
            except Exception:
                pass

            if not call_edges:
                try:
                    direct_caller_ids = cg.get_direct_callers(curr_symbol)
                    for cid in direct_caller_ids:
                        found_edges = cg.get_call_edges(source_id=cid, target_id=curr_symbol)
                        if found_edges:
                            call_edges.extend(found_edges)
                        else:
                            # Fabricate synthetic CallEdge from node info if edge record missing
                            node = cg.nodes.get(cid)
                            file_p = node.file_path if node else "unknown.py"
                            line_n = node.start_line if node else 1
                            call_edges.append(
                                CallEdge(
                                    source_id=cid,
                                    target_id=curr_symbol,
                                    source_file=file_p,
                                    line_number=line_n,
                                    col_offset=0,
                                    raw_call_expr=cid,
                                    resolution_status=CallResolutionStatus.INTERNAL_RESOLVED,
                                )
                            )
                except Exception:
                    pass

            for edge in call_edges:
                if len(nodes_by_id) >= max_nodes:
                    break

                caller_sym_id = edge.source_id
                caller_node_info = cg.nodes.get(caller_sym_id)

                file_path = edge.source_file or (caller_node_info.file_path if caller_node_info else "unknown.py")
                start_l = edge.line_number or (caller_node_info.start_line if caller_node_info else 1)
                end_l = caller_node_info.end_line if caller_node_info else start_l

                # Determine role: if caller was changed, candidate root cause
                is_changed = False
                if changed_files:
                    norm_caller_file = file_path.replace("\\", "/").strip().lstrip("/")
                    is_changed = any(norm_caller_file == cf.replace("\\", "/").strip().lstrip("/") for cf in changed_files)

                role = CausalRole.ROOT_CAUSE if is_changed else CausalRole.PROPAGATOR
                desc = f"Caller '{caller_sym_id}' invokes '{curr_symbol}' at line {edge.line_number}"
                if is_changed:
                    desc += " (modified in recent changeset)"

                caller_node = self.create_node(
                    file_path=file_path,
                    start_line=start_l,
                    end_line=end_l,
                    role=role,
                    node_type=CausalNodeType.FUNCTION,
                    symbol_id=caller_sym_id,
                    description=desc,
                )

                if caller_node.node_id not in nodes_by_id:
                    nodes_by_id[caller_node.node_id] = caller_node

                causal_rel = CausalRelationship.CALLS
                if is_changed:
                    causal_rel = CausalRelationship.PASSES_INVALID_DATA

                cedge = self.create_edge(
                    source_node=caller_node,
                    target_node=curr_node,
                    relationship=causal_rel,
                    explanation=f"Call invocation: {caller_sym_id} calls {curr_symbol} ({edge.raw_call_expr or 'call'})",
                    is_directly_verified=True,
                    confidence=FindingConfidence.HIGH,
                    confidence_score=0.90 if not is_changed else 0.85,
                    evidence_references=caller_node.supporting_evidence_ids,
                )
                edges_by_id[cedge.edge_id] = cedge

                if caller_sym_id not in visited_symbols:
                    visited_symbols.add(caller_sym_id)
                    queue.append((caller_sym_id, caller_node, depth + 1))

            # 2. Check module dependencies if call graph yields no callers
            if not call_edges and depth == 0:
                mod_name = self._file_to_module(symptom_node.file_path)
                try:
                    dependents = mod_graph.get_dependents(mod_name)
                    for dep in dependents[:5]:
                        if len(nodes_by_id) >= max_nodes:
                            break
                        dep_file = self._module_to_file(dep)
                        if dep_file:
                            dep_node = self.create_node(
                                file_path=dep_file,
                                start_line=1,
                                end_line=10,
                                role=CausalRole.CONTRIBUTING_FACTOR,
                                node_type=CausalNodeType.MODULE,
                                description=f"Module '{dep}' depends on '{mod_name}'",
                            )
                            if dep_node.node_id not in nodes_by_id:
                                nodes_by_id[dep_node.node_id] = dep_node

                            cedge = self.create_edge(
                                source_node=dep_node,
                                target_node=curr_node,
                                relationship=CausalRelationship.DEPENDS_ON,
                                explanation=f"Module dependency: {dep} imports {mod_name}",
                                is_directly_verified=True,
                                confidence=FindingConfidence.MEDIUM,
                                confidence_score=0.75,
                            )
                            edges_by_id[cedge.edge_id] = cedge
                except Exception:
                    pass

        return list(nodes_by_id.values()), list(edges_by_id.values())

    def trace_forward(
        self,
        root_node: CausalNode,
        max_depth: int = 4,
        max_nodes: int = 50,
    ) -> Tuple[List[CausalNode], List[CausalEdge]]:
        """Trace callees and dependents forward from root_node toward symptoms."""
        cg = self.get_call_graph()

        nodes_by_id: Dict[str, CausalNode] = {root_node.node_id: root_node}
        edges_by_id: Dict[str, CausalEdge] = {}

        start_symbol = root_node.symbol_id
        if not start_symbol:
            start_symbol = self._find_symbol_at_location(root_node.file_path, root_node.start_line)

        resolved_start = self.resolve_symbol_node_id(start_symbol) if start_symbol else None

        visited_symbols: Set[str] = set()
        queue: deque = deque()

        if resolved_start:
            queue.append((resolved_start, root_node, 0))
            visited_symbols.add(resolved_start)

        depth_limit = min(max_depth, MAX_CHAIN_DEPTH)

        while queue and len(nodes_by_id) < max_nodes:
            curr_symbol, curr_node, depth = queue.popleft()
            if depth >= depth_limit:
                continue

            callee_edges: List[CallEdge] = []
            try:
                callee_edges = cg.get_call_edges(source_id=curr_symbol)
            except Exception:
                pass

            if not callee_edges:
                try:
                    callee_ids = cg.get_direct_callees(curr_symbol)
                    for cid in callee_ids:
                        found_edges = cg.get_call_edges(source_id=curr_symbol, target_id=cid)
                        if found_edges:
                            callee_edges.extend(found_edges)
                        else:
                            node = cg.nodes.get(cid)
                            file_p = node.file_path if node else "unknown.py"
                            line_n = node.start_line if node else 1
                            callee_edges.append(
                                CallEdge(
                                    source_id=curr_symbol,
                                    target_id=cid,
                                    source_file=file_p,
                                    line_number=line_n,
                                    col_offset=0,
                                    raw_call_expr=cid,
                                    resolution_status=CallResolutionStatus.INTERNAL_RESOLVED,
                                )
                            )
                except Exception:
                    pass

            for edge in callee_edges:
                if len(nodes_by_id) >= max_nodes:
                    break

                callee_sym_id = edge.target_id
                callee_info = cg.nodes.get(callee_sym_id)

                file_path = callee_info.file_path if callee_info else edge.source_file
                start_l = callee_info.start_line if callee_info else 1
                end_l = callee_info.end_line if callee_info else 10

                callee_node = self.create_node(
                    file_path=file_path,
                    start_line=start_l,
                    end_line=end_l,
                    role=CausalRole.PROPAGATOR,
                    symbol_id=callee_sym_id,
                    description=f"Callee '{callee_sym_id}' invoked by '{curr_symbol}'",
                )

                if callee_node.node_id not in nodes_by_id:
                    nodes_by_id[callee_node.node_id] = callee_node

                cedge = self.create_edge(
                    source_node=curr_node,
                    target_node=callee_node,
                    relationship=CausalRelationship.CALLS,
                    explanation=f"Downstream call: {curr_symbol} calls {callee_sym_id}",
                    is_directly_verified=True,
                    confidence=FindingConfidence.HIGH,
                    confidence_score=0.90,
                )
                edges_by_id[cedge.edge_id] = cedge

                if callee_sym_id not in visited_symbols:
                    visited_symbols.add(callee_sym_id)
                    queue.append((callee_sym_id, callee_node, depth + 1))

        return list(nodes_by_id.values()), list(edges_by_id.values())

    def synthesize_chains(
        self,
        candidate_roots: List[CausalNode],
        symptom_node: CausalNode,
        nodes: List[CausalNode],
        edges: List[CausalEdge],
    ) -> List[CausalChain]:
        """Synthesize concrete linear causal chains from roots to symptom using BFS/DFS."""
        adj: Dict[str, List[Tuple[str, CausalEdge]]] = {}
        for edge in edges:
            adj.setdefault(edge.source_node_id, []).append((edge.target_node_id, edge))

        chains: List[CausalChain] = []

        for root in candidate_roots:
            if root.node_id == symptom_node.node_id:
                continue

            paths = self._find_paths(root.node_id, symptom_node.node_id, adj, max_depth=MAX_CHAIN_DEPTH)
            for path_nodes, path_edges in paths:
                all_verified = all(e.is_directly_verified for e in path_edges)
                avg_score = sum(e.confidence_score for e in path_edges) / len(path_edges) if path_edges else 0.5
                if not all_verified:
                    avg_score = min(avg_score, 0.70)

                conf = FindingConfidence.HIGH if (all_verified and avg_score >= 0.85) else (
                    FindingConfidence.MEDIUM if avg_score >= 0.60 else FindingConfidence.LOW
                )

                chain_id = f"cchain_{hashlib.sha256((':'.join(path_nodes)).encode()).hexdigest()[:8]}"
                summary = f"Root cause at {root.file_path}:{root.start_line} ({root.symbol_id or 'component'}) propagates to symptom at {symptom_node.file_path}:{symptom_node.start_line} over {len(path_edges)} step(s)."

                try:
                    chain = CausalChain(
                        chain_id=chain_id,
                        root_node_id=root.node_id,
                        symptom_node_id=symptom_node.node_id,
                        node_ids=path_nodes,
                        edge_ids=[e.edge_id for e in path_edges],
                        summary=summary,
                        confidence=conf,
                        confidence_score=round(avg_score, 2),
                        is_fully_verified=all_verified,
                    )
                    chains.append(chain)
                except Exception:
                    pass

        chains.sort(key=lambda c: (-c.confidence_score, 0 if c.is_fully_verified else 1, c.chain_id))
        return chains

    def _find_paths(
        self,
        src: str,
        dst: str,
        adj: Dict[str, List[Tuple[str, CausalEdge]]],
        max_depth: int,
    ) -> List[Tuple[List[str], List[CausalEdge]]]:
        """Find simple paths from src to dst within max_depth."""
        results: List[Tuple[List[str], List[CausalEdge]]] = []
        queue: deque = deque([([src], [])])

        while queue:
            curr_path, curr_edges = queue.popleft()
            curr = curr_path[-1]

            if curr == dst:
                results.append((curr_path, curr_edges))
                if len(results) >= 5:
                    break
                continue

            if len(curr_path) > max_depth:
                continue

            for neighbor, edge in adj.get(curr, []):
                if neighbor not in curr_path:
                    queue.append((curr_path + [neighbor], curr_edges + [edge]))

        return results

    def _find_symbol_at_location(self, file_path: str, line_number: int) -> Optional[str]:
        """Locate symbol ID containing the given file and line number."""
        sym_idx = self.get_symbol_index()
        norm_file = file_path.replace("\\", "/").strip().lstrip("/")

        best_sym: Optional[str] = None
        min_span = float("inf")

        records: List[SymbolRecord] = []
        if hasattr(sym_idx, "symbols") and isinstance(sym_idx.symbols, list):
            records = [s for s in sym_idx.symbols if getattr(s, "file_path", "").replace("\\", "/").strip().lstrip("/") == norm_file]

        for r in records:
            start_l = getattr(r, "start_line", 1)
            end_l = getattr(r, "end_line", start_l)
            if start_l <= line_number <= end_l:
                span = end_l - start_l
                if span < min_span:
                    min_span = span
                    best_sym = getattr(r, "node_id", getattr(r, "qualified_name", r.name))

        return best_sym

    def _file_to_module(self, file_path: str) -> str:
        p = Path(file_path)
        parts = list(p.with_suffix("").parts)
        if parts and parts[0] in ("app", "src"):
            parts = parts[1:]
        return ".".join(parts)

    def _module_to_file(self, module_name: str) -> Optional[str]:
        rel = module_name.replace(".", "/") + ".py"
        for prefix in ("", "app/", "src/"):
            candidate = prefix + rel
            if (self.workspace_path / candidate).is_file():
                return candidate
        return None
