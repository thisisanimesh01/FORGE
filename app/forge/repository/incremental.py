"""Incremental repository indexing and caching engine for FORGE Phase 11 Milestone 5.

Maintains deterministic, corrupt-resistant, and semantically equivalent indexing state
across source file modifications, additions, deletions, and renames.
"""

import ast
import builtins
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repository.cache import IndexCacheManager
from forge.repository.cache_schemas import (
    INDEX_SCHEMA_VERSION,
    INDEXER_PARSER_VERSION,
    FileIndexRecord,
    IncrementalUpdateSummary,
    RepositoryIndexState,
)
from forge.repository.call_graph import CallGraph, resolve_call_graph_from_records
from forge.repository.graph_schemas import (
    CallEdge,
    CallGraphSummary,
    CallResolutionStatus,
    CallSiteRecord,
    DependencyCycle,
    ExternalImportRecord,
    GraphNode,
    ImportEvidence,
    ImportRecord,
    ModuleDependencyGraphSummary,
    ModuleEdge,
    ModuleNode,
    NodeKind,
    StronglyConnectedComponent,
    UnresolvedImportRecord,
)
from forge.repository.indexer import (
    ASTSymbolVisitor,
    SymbolIndex,
    SymbolRecord,
    discover_workspace_python_files,
    parse_python_file_details,
    rel_path_to_module_name,
)
from forge.repository.module_graph import ModuleDependencyGraph
from forge.repository.protection import is_protected_path
from forge.repository.resolver import WorkspaceImportResolver
from forge.repository.validation import assert_contained_path


_BUILTIN_FUNCTIONS: Set[str] = {
    name for name, obj in builtins.__dict__.items()
    if callable(obj) or isinstance(obj, type)
}


def visitor_to_file_record(
    visitor: ASTSymbolVisitor,
    file_path: str,
    content_hash: str,
    size_bytes: int,
    mtime_ns: int = 0,
    docstring: Optional[str] = None,
    total_lines: int = 0,
) -> FileIndexRecord:
    """Convert an ASTSymbolVisitor instance into a serializable FileIndexRecord."""
    callable_locals = {
        caller_id: sorted(list(vars_set))
        for caller_id, vars_set in visitor.callable_local_vars.items()
    }
    return FileIndexRecord(
        file_path=file_path,
        content_hash=content_hash,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        module_name=visitor.module_name,
        docstring=docstring,
        total_lines=total_lines,
        symbols=visitor.symbols,
        nodes=visitor.nodes,
        call_sites=visitor.call_sites,
        imports=visitor.imports,
        class_bases=visitor.class_bases,
        callable_local_vars=callable_locals,
    )


def parse_file_to_record(
    file_path: Path,
    repo_root: Path,
    content_hash: Optional[str] = None,
    preverified: bool = False,
) -> Optional[FileIndexRecord]:
    """Parse a single Python file into a FileIndexRecord with full containment and security validation."""
    if not preverified:
        try:
            resolved = assert_contained_path(file_path, repo_root)
        except Exception:
            return None

        if is_protected_path(resolved, base_dir=repo_root):
            return None
    else:
        resolved = file_path

    rel_path = str(resolved.relative_to(repo_root))

    try:
        raw_bytes = resolved.read_bytes()
        size_bytes = len(raw_bytes)
        stat = resolved.stat()
        mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))
        content = raw_bytes.decode("utf-8", errors="ignore")
        total_lines = len(content.splitlines())
    except Exception:
        return None

    if content_hash is None:
        content_hash = hashlib.sha256(raw_bytes).hexdigest()

    visitor = parse_python_file_details(
        resolved,
        repo_root,
        preloaded_content=content,
        preverified=True,
    )
    if not visitor:
        # File has syntax error or is unparseable: return clean empty record for graph consistency
        return FileIndexRecord(
            file_path=rel_path,
            content_hash=content_hash,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            module_name=rel_path_to_module_name(rel_path),
            docstring=None,
            total_lines=total_lines,
            symbols=[],
            nodes=[],
            call_sites=[],
            imports=[],
            class_bases={},
            callable_local_vars={},
        )

    return visitor_to_file_record(
        visitor=visitor,
        file_path=rel_path,
        content_hash=content_hash,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        docstring=visitor.module_docstring,
        total_lines=total_lines,
    )


def build_symbol_index_from_records(records: Dict[str, FileIndexRecord]) -> SymbolIndex:
    """Build a SymbolIndex from cached or freshly parsed FileIndexRecords."""
    all_symbols: List[SymbolRecord] = []
    file_symbols: Dict[str, List[SymbolRecord]] = {}

    classes = 0
    funcs = 0
    methods = 0
    imports = 0

    for rel_str, record in sorted(records.items()):
        if record.symbols:
            file_symbols[rel_str] = record.symbols
            all_symbols.extend(record.symbols)
            for sym in record.symbols:
                if sym.symbol_type == "class":
                    classes += 1
                elif sym.symbol_type == "function":
                    funcs += 1
                elif sym.symbol_type == "method":
                    methods += 1
                elif sym.symbol_type == "import":
                    imports += 1

    return SymbolIndex(
        symbols=all_symbols,
        file_symbols=file_symbols,
        total_classes=classes,
        total_functions=funcs,
        total_methods=methods,
        total_imports=imports,
    )


def build_call_graph_from_records(
    records: Dict[str, FileIndexRecord],
    repo_root: Path,
    resolver: Optional[WorkspaceImportResolver] = None,
) -> CallGraph:
    """Assemble a CallGraph across FileIndexRecords using deterministic cross-file resolution."""
    return resolve_call_graph_from_records(records, repo_root, resolver=resolver)


def build_module_graph_from_records(
    records: Dict[str, FileIndexRecord],
    repo_root: Path,
    resolver: Optional[WorkspaceImportResolver] = None,
) -> ModuleDependencyGraph:
    """Assemble a ModuleDependencyGraph across FileIndexRecords with cycle detection."""
    root = repo_root.resolve()
    if resolver is None:
        resolver = WorkspaceImportResolver(root)

    nodes: Dict[str, ModuleNode] = {}
    file_to_module: Dict[str, str] = {}
    alias_to_module: Dict[str, str] = {}

    for rel_str, record in sorted(records.items()):
        mod_id = resolver.file_to_module.get(rel_str) or resolver.get_module_name_for_file(rel_str)
        if not mod_id:
            mod_id = Path(rel_str).stem

        file_to_module[rel_str] = mod_id
        is_pkg = Path(rel_str).name == "__init__.py"

        node = ModuleNode(
            module_id=mod_id,
            file_path=rel_str,
            is_package=is_pkg,
            docstring=record.docstring,
            total_lines=record.total_lines,
        )
        nodes[mod_id] = node

    for alias_mod, rel_file in resolver.module_to_file.items():
        if rel_file in file_to_module:
            canonical_mod = file_to_module[rel_file]
            if alias_mod != canonical_mod:
                alias_to_module[alias_mod] = canonical_mod

    edge_map: Dict[str, ModuleEdge] = {}
    external_imports: List[ExternalImportRecord] = []
    unresolved_imports: List[UnresolvedImportRecord] = []
    ambiguous_count = 0

    for rel_str, record in sorted(records.items()):
        source_mod_id = file_to_module.get(rel_str)
        if not source_mod_id or source_mod_id not in nodes:
            continue

        for imp in record.imports:
            if imp.name == "*":
                import_type = "star"
            elif imp.level > 0:
                import_type = "relative"
            elif imp.from_module:
                import_type = "from"
            else:
                import_type = "direct"

            resolved = resolver.resolve_import(rel_str, imp)

            if resolved.resolution_status == CallResolutionStatus.RESOLVED_INTERNAL and resolved.target_file:
                target_mod_id = file_to_module.get(resolved.target_file)
                if not target_mod_id:
                    target_mod_id = resolver.get_module_name_for_file(resolved.target_file)

                if target_mod_id and target_mod_id in nodes:
                    edge_key = f"{source_mod_id}->{target_mod_id}"
                    evidence = ImportEvidence(
                        importing_file=rel_str,
                        line_number=imp.line,
                        imported_name=imp.name,
                        alias=imp.alias,
                        from_module=imp.from_module,
                        level=imp.level,
                        import_type=import_type,
                        resolution_status=resolved.resolution_status,
                        resolution_reason=resolved.reason,
                    )

                    if edge_key in edge_map:
                        edge = edge_map[edge_key]
                        edge.evidence.append(evidence)
                        edge.weight += 1
                    else:
                        is_self = source_mod_id == target_mod_id
                        edge_map[edge_key] = ModuleEdge(
                            source_module=source_mod_id,
                            target_module=target_mod_id,
                            source_file=rel_str,
                            target_file=resolved.target_file,
                            evidence=[evidence],
                            weight=1,
                            is_self_dependency=is_self,
                        )

            elif resolved.resolution_status == CallResolutionStatus.RESOLVED_EXTERNAL:
                pkg_name = imp.from_module.split(".")[0] if imp.from_module else imp.name.split(".")[0]
                is_std = resolver.is_stdlib_module(pkg_name)
                ext_rec = ExternalImportRecord(
                    importing_module=source_mod_id,
                    importing_file=rel_str,
                    package_name=pkg_name,
                    imported_name=imp.name,
                    is_stdlib=is_std,
                    line_number=imp.line,
                )
                external_imports.append(ext_rec)
                if pkg_name not in nodes[source_mod_id].external_dependencies:
                    nodes[source_mod_id].external_dependencies.append(pkg_name)

            elif resolved.resolution_status == CallResolutionStatus.AMBIGUOUS:
                ambiguous_count += 1

            else:
                unres_rec = UnresolvedImportRecord(
                    importing_module=source_mod_id,
                    importing_file=rel_str,
                    imported_name=imp.name,
                    from_module=imp.from_module,
                    level=imp.level,
                    line_number=imp.line,
                    reason=resolved.reason,
                )
                unresolved_imports.append(unres_rec)

    forward_adjacency: Dict[str, List[str]] = {m: [] for m in nodes}
    reverse_adjacency: Dict[str, List[str]] = {m: [] for m in nodes}

    for edge in edge_map.values():
        forward_adjacency[edge.source_module].append(edge.target_module)
        reverse_adjacency[edge.target_module].append(edge.source_module)

    for mod_id, node in nodes.items():
        node.internal_dependencies = sorted(list(set(forward_adjacency.get(mod_id, []))))
        node.internal_dependents = sorted(list(set(reverse_adjacency.get(mod_id, []))))
        node.external_dependencies = sorted(node.external_dependencies)
        node.out_degree = len(node.internal_dependencies)
        node.in_degree = len(node.internal_dependents)
        forward_adjacency[mod_id] = sorted(list(set(forward_adjacency.get(mod_id, []))))
        reverse_adjacency[mod_id] = sorted(list(set(reverse_adjacency.get(mod_id, []))))

    graph = ModuleDependencyGraph(
        nodes=nodes,
        edges=edge_map,
        forward_adjacency=forward_adjacency,
        reverse_adjacency=reverse_adjacency,
        external_imports=external_imports,
        unresolved_imports=unresolved_imports,
        alias_to_module=alias_to_module,
        file_to_module=file_to_module,
    )

    sccs = graph.get_strongly_connected_components()
    cyclic_sccs = [s for s in sccs if s.is_cyclic]
    cycles = graph.detect_cycles()

    cyclic_module_set: Set[str] = set()
    for s in cyclic_sccs:
        cyclic_module_set.update(s.modules)

    isolated_count = sum(
        1 for n in nodes.values() if not n.internal_dependencies and not n.internal_dependents
    )

    stdlib_count = sum(1 for e in external_imports if e.is_stdlib)
    third_party_count = sum(1 for e in external_imports if not e.is_stdlib)

    summary = ModuleDependencyGraphSummary(
        total_modules=len(nodes),
        total_internal_edges=len(edge_map),
        total_external_imports=len(external_imports),
        total_stdlib_imports=stdlib_count,
        total_third_party_imports=third_party_count,
        total_unresolved_imports=len(unresolved_imports),
        total_ambiguous_imports=ambiguous_count,
        total_cycles=len(cycles),
        total_sccs=len(sccs),
        cyclic_sccs=len(cyclic_sccs),
        modules_in_cycles=len(cyclic_module_set),
        isolated_modules=isolated_count,
    )
    graph.summary = summary
    return graph


class IncrementalRepositoryIndex:
    """Deterministic, versioned incremental index coordinator for repository understanding.

    Maintains content-hash based cache records and guarantees exact semantic equivalence
    with full rebuilds while accelerating repeat indexing operations.
    """

    def __init__(
        self,
        repo_root: Path,
        cache_manager: Optional[IndexCacheManager] = None,
        max_python_files: int = 500,
        max_file_size_bytes: int = 500_000,
        state_file: Optional[Path] = None,
        auto_persist_state: bool = True,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.max_python_files = max_python_files
        self.max_file_size_bytes = max_file_size_bytes
        self.auto_persist_state = auto_persist_state

        if cache_manager is not None:
            self.cache_manager = cache_manager
        else:
            default_cache_dir = self.repo_root / ".forge" / "cache" / "indexing"
            self.cache_manager = IndexCacheManager(default_cache_dir)

        if state_file is not None:
            self.state_file = Path(state_file)
        else:
            self.state_file = self.cache_manager.cache_dir / "repo_state.json"

        # Cached in-memory graphs from most recent build
        self._symbol_index: Optional[SymbolIndex] = None
        self._call_graph: Optional[CallGraph] = None
        self._module_graph: Optional[ModuleDependencyGraph] = None
        self._last_summary: Optional[IncrementalUpdateSummary] = None
        self._cached_records: Dict[str, FileIndexRecord] = {}

    def _discover_python_files(self) -> List[Path]:
        """Discover valid workspace Python files respecting boundaries and exclusions."""
        return discover_workspace_python_files(
            self.repo_root,
            max_python_files=self.max_python_files,
            max_file_size_bytes=self.max_file_size_bytes,
            exclude_forge=True,
        )

    def _load_persisted_state(self) -> Optional[RepositoryIndexState]:
        """Load persisted repository index state if present and schema-compatible."""
        if not self.state_file.exists():
            return None
        try:
            raw = self.state_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if (
                data.get("schema_version") != self.cache_manager.schema_version
                or data.get("parser_version") != self.cache_manager.parser_version
            ):
                return None
            return RepositoryIndexState.model_validate(data)
        except Exception:
            return None

    def _save_persisted_state(self, file_hashes: Dict[str, str]) -> None:
        """Persist repository index state snapshot atomically."""
        if not self.auto_persist_state:
            return
        sorted_hashes = sorted(file_hashes.items())
        combined_str = "|".join(f"{k}:{v}" for k, v in sorted_hashes)
        fingerprint = hashlib.sha256(combined_str.encode("utf-8")).hexdigest()

        state = RepositoryIndexState(
            schema_version=self.cache_manager.schema_version,
            parser_version=self.cache_manager.parser_version,
            repo_fingerprint=fingerprint,
            file_hashes=file_hashes,
            last_updated_at=time.time(),
        )

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_file.parent / f".tmp.state.{os.getpid()}"
            temp_path.write_text(state.model_dump_json(), encoding="utf-8")
            os.replace(temp_path, self.state_file)
        except Exception:
            pass

    def build(
        self,
        force_rebuild: bool = False,
    ) -> Tuple[SymbolIndex, CallGraph, ModuleDependencyGraph, IncrementalUpdateSummary]:
        """Execute incremental indexing and graph assembly with automatic fallback on corruption."""
        start_time = time.time()
        try:
            return self._build_internal(force_rebuild=force_rebuild, start_time=start_time)
        except Exception as e:
            # Safe recovery fallback: trigger clean rebuild from scratch
            fallback_reason = f"Incremental build failed ({type(e).__name__}: {e}); recovered via full rebuild"
            self.cache_manager.clear()
            symbol_index, call_graph, module_graph, summary = self._build_internal(
                force_rebuild=True,
                start_time=start_time,
            )
            summary.rebuild_fallback_triggered = True
            summary.rebuild_fallback_reason = fallback_reason
            return symbol_index, call_graph, module_graph, summary

    def _build_internal(
        self,
        force_rebuild: bool,
        start_time: float,
    ) -> Tuple[SymbolIndex, CallGraph, ModuleDependencyGraph, IncrementalUpdateSummary]:
        """Internal worker for incremental build and delta processing."""
        discovered_files = self._discover_python_files()

        # Step 1: Compute content hashes for already-verified files cleanly
        current_hashes: Dict[str, str] = {}
        for f in discovered_files:
            rel_str = str(f.relative_to(self.repo_root))
            try:
                raw_bytes = f.read_bytes()
                current_hashes[rel_str] = hashlib.sha256(raw_bytes).hexdigest()
            except (OSError, IOError):
                continue

        # Step 2: Compare against previous state
        prev_state = None if force_rebuild else self._load_persisted_state()
        prev_hashes = prev_state.file_hashes if prev_state else {}

        curr_set = set(current_hashes.keys())
        prev_set = set(prev_hashes.keys())

        raw_added = curr_set - prev_set
        raw_deleted = prev_set - curr_set
        common = curr_set & prev_set

        modified_set = {f for f in common if current_hashes[f] != prev_hashes[f]}
        unchanged_set = {f for f in common if current_hashes[f] == prev_hashes[f]}

        # Detect renames: deleted file with identical content hash as added file
        renamed_count = 0
        added_set = set(raw_added)
        deleted_set = set(raw_deleted)

        hash_to_deleted: Dict[str, str] = {prev_hashes[d]: d for d in raw_deleted if d in prev_hashes}
        for a in list(raw_added):
            a_hash = current_hashes.get(a)
            if a_hash and a_hash in hash_to_deleted:
                old_f = hash_to_deleted[a_hash]
                renamed_count += 1
                added_set.discard(a)
                deleted_set.discard(old_f)

        # Invalidate deleted and modified files from cache
        for d in raw_deleted:
            self.cache_manager.invalidate(d)
        for m in modified_set:
            self.cache_manager.invalidate(m)

        self.cache_manager.prune_obsolete(curr_set)

        # Step 3: Load records (cache hits vs AST parsing)
        records: Dict[str, FileIndexRecord] = {}
        cache_hits_before = self.cache_manager.stats.cache_hits
        cache_misses_before = self.cache_manager.stats.cache_misses

        # Unchanged files: try cache first (always checks disk checksum Integrity)
        for rel_str in unchanged_set:
            rec = None
            if not force_rebuild:
                rec = self.cache_manager.get(rel_str, current_hashes[rel_str])

            if rec is None:
                abs_file = self.repo_root / rel_str
                rec = parse_file_to_record(abs_file, self.repo_root, content_hash=current_hashes[rel_str], preverified=True)
                if rec:
                    self.cache_manager.put(rel_str, rec)

            if rec:
                records[rel_str] = rec

        # Added and modified files: parse and cache
        to_parse = added_set | modified_set
        self.cache_manager.stats.cache_misses += len(to_parse) + len(raw_added - added_set)
        for rel_str in to_parse:
            abs_file = self.repo_root / rel_str
            rec = parse_file_to_record(abs_file, self.repo_root, content_hash=current_hashes[rel_str], preverified=True)
            if rec:
                self.cache_manager.put(rel_str, rec)
                records[rel_str] = rec

        # For detected renames: load or parse under new path
        for rel_str in (raw_added - added_set):
            abs_file = self.repo_root / rel_str
            rec = parse_file_to_record(abs_file, self.repo_root, content_hash=current_hashes[rel_str], preverified=True)
            if rec:
                self.cache_manager.put(rel_str, rec)
                records[rel_str] = rec

        # Step 4: Assemble graphs from records sharing a single resolver instance
        resolver = WorkspaceImportResolver(self.repo_root, known_python_files=list(current_hashes.keys()))
        symbol_index = build_symbol_index_from_records(records)
        call_graph = build_call_graph_from_records(records, self.repo_root, resolver=resolver)
        module_graph = build_module_graph_from_records(records, self.repo_root, resolver=resolver)

        # Step 5: Identify invalidated dependents
        changed_files = modified_set | raw_deleted | raw_added
        invalidated_dependents = 0
        if changed_files:
            changed_modules: Set[str] = set()
            for f in changed_files:
                mod = resolver.file_to_module.get(f) or resolver.get_module_name_for_file(f)
                if mod:
                    changed_modules.add(mod)

            affected_modules: Set[str] = set()
            for mod in changed_modules:
                transitive = module_graph.get_transitive_dependents(mod, max_depth=10)
                affected_modules.update(transitive)

            affected_modules.difference_update(changed_modules)
            invalidated_dependents = len(affected_modules)

        # Persist updated state snapshot only when needed
        if force_rebuild or prev_state is None or current_hashes != prev_hashes or not self.state_file.exists():
            self._save_persisted_state(current_hashes)

        duration = time.time() - start_time
        hits_delta = self.cache_manager.stats.cache_hits - cache_hits_before
        misses_delta = self.cache_manager.stats.cache_misses - cache_misses_before

        summary = IncrementalUpdateSummary(
            total_files_scanned=len(discovered_files),
            unchanged_files=len(unchanged_set),
            added_files=len(raw_added),
            modified_files=len(modified_set),
            deleted_files=len(raw_deleted),
            renamed_files=renamed_count,
            invalidated_dependents=invalidated_dependents,
            cache_hits=hits_delta,
            cache_misses=misses_delta,
            duration_seconds=round(duration, 4),
            rebuild_fallback_triggered=False,
            rebuild_fallback_reason=None,
        )

        self._symbol_index = symbol_index
        self._call_graph = call_graph
        self._module_graph = module_graph
        self._last_summary = summary
        self._cached_records = records

        return symbol_index, call_graph, module_graph, summary

    def get_symbol_index(self) -> SymbolIndex:
        """Return the most recently computed SymbolIndex, building if necessary."""
        if self._symbol_index is None:
            self.build()
        assert self._symbol_index is not None
        return self._symbol_index

    def get_call_graph(self) -> CallGraph:
        """Return the most recently computed CallGraph, building if necessary."""
        if self._call_graph is None:
            self.build()
        assert self._call_graph is not None
        return self._call_graph

    def get_module_graph(self) -> ModuleDependencyGraph:
        """Return the most recently computed ModuleDependencyGraph, building if necessary."""
        if self._module_graph is None:
            self.build()
        assert self._module_graph is not None
        return self._module_graph

    def get_summary(self) -> Optional[IncrementalUpdateSummary]:
        """Return the summary of the last build operation."""
        return self._last_summary

    @staticmethod
    def verify_equivalence(
        full_symbols: SymbolIndex,
        full_call: CallGraph,
        full_module: ModuleDependencyGraph,
        inc_symbols: SymbolIndex,
        inc_call: CallGraph,
        inc_module: ModuleDependencyGraph,
    ) -> bool:
        """Strictly assert semantic equivalence between full clean rebuild and incremental indexing."""
        # 1. Symbol Index Equivalence
        if full_symbols.total_classes != inc_symbols.total_classes:
            return False
        if full_symbols.total_functions != inc_symbols.total_functions:
            return False
        if full_symbols.total_methods != inc_symbols.total_methods:
            return False
        if full_symbols.total_imports != inc_symbols.total_imports:
            return False
        if set(full_symbols.file_symbols.keys()) != set(inc_symbols.file_symbols.keys()):
            return False

        # 2. Call Graph Equivalence
        if set(full_call.nodes.keys()) != set(inc_call.nodes.keys()):
            return False
        if len(full_call.edges) != len(inc_call.edges):
            return False
        if full_call.summary.resolved_internal_calls != inc_call.summary.resolved_internal_calls:
            return False
        if full_call.forward_adjacency != inc_call.forward_adjacency:
            return False
        if full_call.reverse_adjacency != inc_call.reverse_adjacency:
            return False

        # 3. Module Graph Equivalence
        if set(full_module.nodes.keys()) != set(inc_module.nodes.keys()):
            return False
        if set(full_module.edges.keys()) != set(inc_module.edges.keys()):
            return False
        if full_module.forward_adjacency != inc_module.forward_adjacency:
            return False
        if full_module.reverse_adjacency != inc_module.reverse_adjacency:
            return False

        # 4. Cycle Detection Equivalence
        full_cycles = sorted(c.cycle_id for c in full_module.detect_cycles())
        inc_cycles = sorted(c.cycle_id for c in inc_module.detect_cycles())
        if full_cycles != inc_cycles:
            return False

        return True


def get_incremental_index(repo_root: Path, cache_dir: Optional[Path] = None) -> IncrementalRepositoryIndex:
    """Convenience factory returning an IncrementalRepositoryIndex."""
    cm = IndexCacheManager(cache_dir) if cache_dir else None
    return IncrementalRepositoryIndex(repo_root=repo_root, cache_manager=cm)


def build_incremental_call_graph(repo_root: Path, cache_dir: Optional[Path] = None) -> CallGraph:
    """Convenience helper building and returning a CallGraph incrementally."""
    index = get_incremental_index(repo_root=repo_root, cache_dir=cache_dir)
    return index.get_call_graph()


def build_incremental_module_graph(repo_root: Path, cache_dir: Optional[Path] = None) -> ModuleDependencyGraph:
    """Convenience helper building and returning a ModuleDependencyGraph incrementally."""
    index = get_incremental_index(repo_root=repo_root, cache_dir=cache_dir)
    return index.get_module_graph()
