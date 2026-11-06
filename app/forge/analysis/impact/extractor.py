"""Changeset extraction from unified diffs, file lists, and symbol AST mappings."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from forge.analysis.impact.schemas import (
    ChangeSet,
    ChangeType,
    EntityType,
    FileChange,
    SymbolChange,
)
from forge.repository.call_graph import CallGraph
from forge.repository.graph_schemas import NodeKind
from forge.repository.indexer import parse_python_file_details
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path
from forge.review.models import ReviewFileStatus
from forge.review.parser import parse_unified_diff


def _map_review_status_to_change_type(status: ReviewFileStatus) -> ChangeType:
    """Map review parser status to impact ChangeType."""
    if status == ReviewFileStatus.ADDED:
        return ChangeType.ADDED
    if status == ReviewFileStatus.DELETED:
        return ChangeType.DELETED
    if status == ReviewFileStatus.RENAMED:
        return ChangeType.RENAMED
    return ChangeType.MODIFIED


def extract_changeset_from_diff(
    diff_text: str,
    repo_root: Path,
    call_graph: Optional[CallGraph] = None,
) -> ChangeSet:
    """Extract structured FileChanges and SymbolChanges from raw unified diff text.

    Safely identifies touched line numbers, excludes protected paths, and correlates
    modified line numbers against static AST symbols without executing code.
    """
    root = repo_root.resolve()

    # Preprocess if standard patch without "diff --git " header
    processed_diff = diff_text
    if "diff --git " not in diff_text:
        lines = diff_text.splitlines()
        new_lines: List[str] = []
        for line in lines:
            if line.startswith("--- a/"):
                old_p = line[6:].strip()
                new_lines.append(f"diff --git a/{old_p} b/{old_p}")
            new_lines.append(line)
        processed_diff = "\n".join(new_lines)

    diff_bundle = parse_unified_diff(processed_diff)

    file_changes: List[FileChange] = []
    symbol_changes: List[SymbolChange] = []

    for diff_file in diff_bundle.files:
        raw_path = diff_file.path.strip()
        if raw_path.startswith("/") or raw_path.startswith("\\") or Path(raw_path).is_absolute():
            continue

        rel_path = raw_path.replace("\\", "/").lstrip("/")
        parts = Path(rel_path).parts
        if any(part in ("..", ".git") or part.startswith(".git") for part in parts):
            continue

        if is_protected_path(rel_path, base_dir=root):
            continue

        target_file = root / rel_path
        if target_file.is_symlink():
            try:
                resolved_target = target_file.resolve()
                assert_contained_path(resolved_target, root)
                if is_protected_path(resolved_target, base_dir=root):
                    continue
            except Exception:
                continue

        try:
            assert_contained_path(target_file, root)
        except Exception:
            continue

        change_type = _map_review_status_to_change_type(diff_file.status)
        old_path = diff_file.old_path.replace("\\", "/").lstrip("/") if diff_file.old_path else None

        changed_lines: List[int] = []
        deleted_lines: List[int] = []

        for hunk in diff_file.hunks:
            for line in hunk.lines:
                if line.origin == "+" and line.new_lineno is not None:
                    changed_lines.append(line.new_lineno)
                elif line.origin == "-" and line.old_lineno is not None:
                    deleted_lines.append(line.old_lineno)

        file_change = FileChange(
            path=rel_path,
            change_type=change_type,
            old_path=old_path,
            changed_lines=sorted(list(set(changed_lines))),
            deleted_lines=sorted(list(set(deleted_lines))),
        )
        file_changes.append(file_change)

        # Correlate changed lines with symbols
        syms = _correlate_file_symbols(file_change, root, call_graph)
        symbol_changes.extend(syms)

    return ChangeSet(
        files=file_changes,
        symbols=symbol_changes,
        base_revision=diff_bundle.base_revision,
        head_revision=diff_bundle.head_revision,
    )


def extract_changeset_from_files(
    changed_files: List[Any],
    repo_root: Path,
    call_graph: Optional[CallGraph] = None,
) -> ChangeSet:
    """Extract structured ChangeSet from a list of file dictionaries or FileChange records."""
    root = repo_root.resolve()
    file_changes: List[FileChange] = []
    symbol_changes: List[SymbolChange] = []

    for item in changed_files:
        if isinstance(item, str):
            item = {"path": item, "change_type": "modified"}

        if isinstance(item, FileChange):
            raw_path = item.path
            ctype = item.change_type
            old_path = item.old_path
            changed_lines = item.changed_lines
            deleted_lines = item.deleted_lines
        else:
            raw_path = str(item.get("path", ""))
            raw_type = item.get("change_type", "modified")
            if isinstance(raw_type, str):
                ctype = ChangeType(raw_type.lower())
            else:
                ctype = raw_type
            old_path = item.get("old_path")
            changed_lines = sorted(list(set(item.get("changed_lines", []))))
            deleted_lines = sorted(list(set(item.get("deleted_lines", []))))

        raw_clean = raw_path.strip()
        # Reject absolute paths or traversal
        if raw_clean.startswith("/") or raw_clean.startswith("\\") or Path(raw_clean).is_absolute():
            continue

        norm_path = raw_clean.replace("\\", "/").lstrip("/")
        parts = Path(norm_path).parts
        if any(part in ("..", ".git") or part.startswith(".git") for part in parts):
            continue

        if is_protected_path(norm_path, base_dir=root):
            continue

        target_file = root / norm_path
        if target_file.is_symlink():
            try:
                resolved_target = target_file.resolve()
                assert_contained_path(resolved_target, root)
                if is_protected_path(resolved_target, base_dir=root):
                    continue
            except Exception:
                continue

        try:
            assert_contained_path(target_file, root)
        except Exception:
            continue

        fc = FileChange(
            path=norm_path,
            change_type=ctype,
            old_path=old_path,
            changed_lines=changed_lines,
            deleted_lines=deleted_lines,
        )

        file_changes.append(fc)
        syms = _correlate_file_symbols(fc, root, call_graph)
        symbol_changes.extend(syms)

    return ChangeSet(files=file_changes, symbols=symbol_changes)


def _correlate_file_symbols(
    file_change: FileChange,
    repo_root: Path,
    call_graph: Optional[CallGraph] = None,
) -> List[SymbolChange]:
    """Map changed lines within a file to specific AST symbols."""
    rel_path = file_change.path
    abs_path = repo_root / rel_path

    # Handle deleted file
    if file_change.change_type == ChangeType.DELETED:
        if call_graph:
            deleted_syms: List[SymbolChange] = []
            for node in call_graph.nodes.values():
                if node.file_path == rel_path and node.kind != NodeKind.MODULE:
                    deleted_syms.append(
                        SymbolChange(
                            symbol_id=node.node_id,
                            name=node.name,
                            qualified_name=node.qualified_name,
                            file_path=node.file_path,
                            entity_type=_node_kind_to_entity_type(node.kind),
                            change_type=ChangeType.DELETED,
                            start_line=node.start_line,
                            end_line=node.end_line,
                        )
                    )
            return deleted_syms
        return []

    if not abs_path.exists() or not abs_path.is_file():
        return []

    # Parse symbols from file AST
    visitor = parse_python_file_details(abs_path, repo_root)
    if not visitor:
        return []

    detected_symbols: List[SymbolChange] = []
    changed_line_set = set(file_change.changed_lines)

    for node in visitor.nodes:
        if node.kind == NodeKind.MODULE:
            continue

        # If changed_lines is empty (e.g. whole file added/modified without hunk lines),
        # or if the symbol boundaries intersect changed lines:
        intersects = not changed_line_set or any(
            node.start_line <= ln <= node.end_line for ln in changed_line_set
        )

        if intersects:
            detected_symbols.append(
                SymbolChange(
                    symbol_id=node.node_id,
                    name=node.name,
                    qualified_name=node.qualified_name,
                    file_path=rel_path,
                    entity_type=_node_kind_to_entity_type(node.kind),
                    change_type=file_change.change_type,
                    start_line=node.start_line,
                    end_line=node.end_line,
                )
            )

    return detected_symbols


def _node_kind_to_entity_type(kind: NodeKind) -> EntityType:
    """Map NodeKind to EntityType."""
    if kind == NodeKind.FUNCTION:
        return EntityType.FUNCTION
    if kind == NodeKind.METHOD:
        return EntityType.METHOD
    if kind == NodeKind.CLASS:
        return EntityType.CLASS
    if kind == NodeKind.MODULE:
        return EntityType.MODULE
    if kind == NodeKind.VARIABLE:
        return EntityType.VARIABLE
    return EntityType.UNKNOWN
