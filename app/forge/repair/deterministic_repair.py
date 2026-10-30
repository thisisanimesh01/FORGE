"""Deterministic repair construction layer for FORGE.

Moves source-coordinate ownership completely into deterministic FORGE code,
preventing LLMs from arbitrarily altering source spans, function headers, or
unanchored surrounding blocks.

Core guarantees:
- Typed Grounded Representation: Grounds all repairs in pinned commit SHA,
  source digest, target file, AST node identity, and exact statement span.
- Strict Coordinate Ownership: Replacements target exclusively the grounded statement(s).
  Models are never permitted to modify surrounding function headers, docstrings,
  or validation guards when a trusted anchor exists.
- Indentation & Scope Preservation: Verifies leading whitespace and structural alignment.
- Fail-Closed Safety: Rejects no-ops, empty content, ambiguous anchors, digest mismatches,
  AST syntax errors, and semantic invariant regressions (unreachable code, bypassed guards).
- Actionable Diagnostics: Returns structured, actionable rejection reasons without
  manufacturing or guessing code.
"""

import ast
import difflib
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field

from forge.config import redact_secrets
from forge.repair.anchoring import (
    AnchorResolutionStatus,
    EditAnchorContract,
    resolve_source_anchor,
    validate_anchor_replacement_semantics,
)
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    compute_patch_sha256,
    is_test_path,
    verify_workspace_containment,
)
from forge.repair.models import ProtectedPathPolicy
from forge.repair.semantic_verifier import analyze_python_ast_semantics
from forge.schemas import EvidenceRecord, Finding


# =============================================================================
# Typed Grounded Repair Representation
# =============================================================================

class GroundedRepairRepresentation(BaseModel):
    """Typed repair representation grounded in trusted repository source."""
    model_config = ConfigDict(frozen=True)

    target_file: str = Field(description="Target file relative path")
    base_commit_sha: str = Field(description="Pinned hexadecimal commit SHA")
    source_digest: str = Field(description="SHA-256 digest of trusted target file content")
    ast_node_type: str = Field(description="Python AST node class name (e.g. Return, Assign, If)")
    ast_node_id: str = Field(description="Deterministic node identifier (e.g. file:scope:type:line)")
    symbol_scope: Optional[str] = Field(default=None, description="Enclosing symbol name (function or class)")
    start_line: int = Field(ge=1, description="1-indexed starting line number of anchored statement")
    end_line: int = Field(ge=1, description="1-indexed ending line number of anchored statement")
    start_col: Optional[int] = Field(default=None, description="0-indexed starting column offset")
    end_col: Optional[int] = Field(default=None, description="0-indexed ending column offset")
    original_statement: str = Field(description="Exact verbatim statement text from trusted source")
    allowed_replacement_scope: str = Field(default="statement_level", description="Allowed replacement scope constraint")
    evidence_ids: List[str] = Field(default_factory=list, description="Linked evidence record IDs")
    finding_id: str = Field(description="Target finding identifier")
    expected_behavior: Optional[str] = Field(default=None, description="Expected software behavior")
    proposed_replacement: Optional[str] = Field(default=None, description="Model-proposed replacement text")
    rejection_reasons: List[str] = Field(default_factory=list, description="Explanations if anchor resolution failed")
    is_resolved: bool = Field(default=True, description="Whether statement was unambiguously grounded")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence in anchor grounding")


class DeterministicRepairResult(BaseModel):
    """Result of deterministic patch construction and invariant verification."""
    model_config = ConfigDict(frozen=True)

    is_valid: bool = Field(description="Whether deterministic repair was successfully constructed and validated")
    unified_diff: Optional[str] = Field(default=None, description="Synthesized standard unified diff")
    rejection_reasons: List[str] = Field(default_factory=list, description="Explanations of violations or validation errors")
    target_file: str = Field(description="Target file relative path")
    lines_added: int = Field(default=0, ge=0)
    lines_removed: int = Field(default=0, ge=0)
    ast_valid: bool = Field(default=False, description="Whether patched code parsed cleanly as Python AST")
    semantic_invariants_valid: bool = Field(default=False, description="Whether AST reachability & guard checks passed")
    patch_candidate: Optional[PatchCandidate] = Field(default=None, description="Structured PatchCandidate if valid")
    grounded_representation: Optional[GroundedRepairRepresentation] = Field(default=None)


# =============================================================================
# Grounded Representation Builder
# =============================================================================

def _find_ast_node_and_scope(
    tree: ast.AST,
    start_line: int,
    end_line: int,
) -> Tuple[Optional[ast.AST], Optional[str], Optional[int], Optional[int]]:
    """Traverse AST to identify the exact statement node, its enclosing scope, and column offsets."""
    target_node: Optional[ast.AST] = None
    target_scope: Optional[str] = None
    start_col: Optional[int] = None
    end_col: Optional[int] = None

    class NodeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope_stack: List[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        def generic_visit(self, node: ast.AST) -> Any:
            nonlocal target_node, target_scope, start_col, end_col
            if isinstance(node, ast.stmt) and hasattr(node, "lineno"):
                node_start = node.lineno
                node_end = getattr(node, "end_lineno", node.lineno)
                if node_start == start_line and node_end == end_line:
                    target_node = node
                    target_scope = "::".join(self.scope_stack) if self.scope_stack else None
                    start_col = getattr(node, "col_offset", None)
                    end_col = getattr(node, "end_col_offset", None)
            super().generic_visit(node)

    visitor = NodeVisitor()
    visitor.visit(tree)
    return target_node, target_scope, start_col, end_col


def build_grounded_repair_representation(
    repo_path: Path,
    target_file: str,
    base_commit_sha: str,
    finding: Finding,
    evidence_records: Optional[List[EvidenceRecord]] = None,
    allowed_files: Optional[List[str]] = None,
    expected_source_digest: Optional[str] = None,
) -> GroundedRepairRepresentation:
    """Build a strongly typed, AST-grounded repair representation from trusted source."""
    anchor = resolve_source_anchor(
        repo_path=repo_path,
        target_file=target_file,
        base_commit_sha=base_commit_sha,
        finding=finding,
        evidence_records=evidence_records or [],
        allowed_files=allowed_files,
        expected_source_digest=expected_source_digest,
    )

    norm_target = Path(target_file).as_posix()
    evidence_ids = [e.evidence_id for e in (evidence_records or []) if e.evidence_id]

    if anchor.status != AnchorResolutionStatus.RESOLVED:
        return GroundedRepairRepresentation(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=anchor.source_digest,
            ast_node_type="Unknown",
            ast_node_id=f"{norm_target}:unresolved:{anchor.status.value}",
            symbol_scope=None,
            start_line=1,
            end_line=1,
            original_statement="",
            allowed_replacement_scope="none",
            evidence_ids=evidence_ids,
            finding_id=finding.finding_id,
            rejection_reasons=[anchor.rejection_reason or f"Anchor status: {anchor.status.value}"],
            is_resolved=False,
            confidence=0.0,
        )

    # Inspect trusted source AST
    target_disk = repo_path / norm_target
    raw_text = target_disk.read_text(encoding="utf-8", errors="replace")

    node_type = "Statement"
    symbol_scope = None
    start_col = None
    end_col = None

    try:
        tree = ast.parse(raw_text)
        node, scope, s_col, e_col = _find_ast_node_and_scope(tree, anchor.start_line, anchor.end_line)
        if node is not None:
            node_type = type(node).__name__
            symbol_scope = scope
            start_col = s_col
            end_col = e_col
    except SyntaxError:
        pass

    ast_node_id = f"{norm_target}:{symbol_scope or 'module'}:{node_type}:{anchor.start_line}"
    allowed_scope = "statement_level"
    if node_type in ("FunctionDef", "AsyncFunctionDef", "ClassDef"):
        allowed_scope = "block_level"

    return GroundedRepairRepresentation(
        target_file=norm_target,
        base_commit_sha=base_commit_sha,
        source_digest=anchor.source_digest,
        ast_node_type=node_type,
        ast_node_id=ast_node_id,
        symbol_scope=symbol_scope,
        start_line=anchor.start_line,
        end_line=anchor.end_line,
        start_col=start_col,
        end_col=end_col,
        original_statement=anchor.anchor_text,
        allowed_replacement_scope=allowed_scope,
        evidence_ids=evidence_ids,
        finding_id=finding.finding_id,
        expected_behavior=finding.description,
        is_resolved=True,
        confidence=1.0,
    )


# =============================================================================
# Narrowly Scoped Deterministic Edit Constructor
# =============================================================================

def construct_deterministic_repair(
    grounded_rep: GroundedRepairRepresentation,
    replacement_content: str,
    repo_path: Path,
    budget: Optional[RepairBudget] = None,
    finding: Optional[Finding] = None,
    evidence_records: Optional[List[EvidenceRecord]] = None,
) -> DeterministicRepairResult:
    """Construct a verified unified diff strictly replacing only the anchored statement.

    Guarantees:
    - Coordinates are strictly owned by grounded_rep (LLM cannot alter start/end lines).
    - Preserves indentation, surrounding lines, docstrings, and guards.
    - Rejects empty, no-op, ambiguous, or out-of-scope modifications.
    - Validates patched Python AST syntax and semantic invariants.
    - Never silently normalizes or fabricates source code.
    """
    norm_target = grounded_rep.target_file

    if not grounded_rep.is_resolved:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=grounded_rep.rejection_reasons or ["Grounded repair representation is not resolved"],
            target_file=norm_target,
        )

    # 1. Content validity checks
    if not replacement_content or not replacement_content.strip():
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=["Replacement content is empty or whitespace-only."],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    if replacement_content.strip() == grounded_rep.original_statement.strip():
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=["Replacement content is identical to original statement (unchanged replacement / no-op)."],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # 2. Scope & Structural Boundary Defense
    anchor_stripped = grounded_rep.original_statement.lstrip()
    repl_stripped = replacement_content.lstrip()
    is_anchor_def = anchor_stripped.startswith(("def ", "class ", "async def "))
    is_repl_def = repl_stripped.startswith(("def ", "class ", "async def "))

    if not is_anchor_def and is_repl_def:
        first_line = replacement_content.splitlines()[0].strip() if replacement_content.splitlines() else ""
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[
                f"Scope violation: Replacement content defines a function/class ('{first_line[:40]}'), "
                f"but anchor is an internal statement block ({grounded_rep.ast_node_type}). "
                f"Source coordinates are deterministically locked to line {grounded_rep.start_line}. "
                f"Replacement must target only the anchored statement level."
            ],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # Indentation preservation
    anchor_indent = len(grounded_rep.original_statement) - len(anchor_stripped)
    repl_lines = replacement_content.splitlines()
    repl_first_line = repl_lines[0] if repl_lines else ""
    repl_indent = len(repl_first_line) - len(repl_first_line.lstrip())

    if anchor_indent > 0 and repl_indent < anchor_indent and not is_anchor_def:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[
                f"Indentation mismatch: Replacement content indentation ({repl_indent} spaces) "
                f"does not match anchor indentation ({anchor_indent} spaces)."
            ],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # 3. Target File & Revision Integrity Verification
    try:
        target_disk = verify_workspace_containment(repo_path, norm_target)
    except Exception as exc:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[f"Scope containment error for '{norm_target}': {exc}"],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    if not target_disk.is_file():
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[f"Target file '{norm_target}' does not exist in repository."],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    raw_text = target_disk.read_text(encoding="utf-8", errors="replace")
    curr_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    if curr_digest != grounded_rep.source_digest:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[
                f"Stale revision: Target file '{norm_target}' digest '{curr_digest[:12]}' "
                f"does not match grounded anchor digest '{grounded_rep.source_digest[:12]}'."
            ],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # 4. Strict Coordinate Splicing (Replacing only [start_line, end_line])
    file_lines = raw_text.splitlines(keepends=True)
    if grounded_rep.start_line > len(file_lines) or grounded_rep.end_line > len(file_lines):
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[
                f"Anchor lines [{grounded_rep.start_line}, {grounded_rep.end_line}] exceed file line count ({len(file_lines)})."
            ],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # Verify that the lines at [start_line, end_line] match original_statement
    orig_chunk = "".join(file_lines[grounded_rep.start_line - 1 : grounded_rep.end_line])
    if orig_chunk.strip() != grounded_rep.original_statement.strip():
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[
                f"Anchor text mismatch at lines {grounded_rep.start_line}-{grounded_rep.end_line}."
            ],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # Format replacement lines preserving newlines
    replacement_clean = replacement_content.rstrip("\r\n") + "\n"
    new_repl_lines = replacement_clean.splitlines(keepends=True)

    # Splice strictly at [start_line - 1 : end_line]
    patched_lines = (
        file_lines[: grounded_rep.start_line - 1]
        + new_repl_lines
        + file_lines[grounded_rep.end_line :]
    )
    patched_text = "".join(patched_lines)

    # 5. Python AST Syntax Verification
    try:
        ast.parse(patched_text)
    except SyntaxError as syn_err:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[f"Patched code failed Python AST syntax verification: {syn_err}"],
            target_file=norm_target,
            grounded_representation=grounded_rep,
            ast_valid=False,
        )

    # 6. Static Semantic Invariant Verification
    sem_res = analyze_python_ast_semantics(
        original_source=raw_text,
        patched_source=patched_text,
        file_path=norm_target,
    )
    if not sem_res.is_valid:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[f"Semantic invariant violation: {v}" for v in sem_res.violations],
            target_file=norm_target,
            grounded_representation=grounded_rep,
            ast_valid=True,
            semantic_invariants_valid=False,
        )

    # 7. Semantic Safeguards against defect perpetuation
    dummy_anchor = EditAnchorContract(
        target_file=norm_target,
        base_commit_sha=grounded_rep.base_commit_sha,
        source_digest=grounded_rep.source_digest,
        anchor_text=grounded_rep.original_statement,
        start_line=grounded_rep.start_line,
        end_line=grounded_rep.end_line,
        finding_id=grounded_rep.finding_id,
        status=AnchorResolutionStatus.RESOLVED,
    )
    sem_safe, sem_reasons = validate_anchor_replacement_semantics(
        anchor=dummy_anchor,
        replacement_content=replacement_content,
        evidence_records=evidence_records,
        finding=finding,
    )
    if not sem_safe:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=sem_reasons,
            target_file=norm_target,
            grounded_representation=grounded_rep,
            ast_valid=True,
            semantic_invariants_valid=False,
        )

    # 8. Synthesize Standard Unified Diff via difflib
    diff_gen = difflib.unified_diff(
        file_lines,
        patched_lines,
        fromfile=f"a/{norm_target}",
        tofile=f"b/{norm_target}",
        n=3,
    )
    diff_text = "".join(diff_gen)
    if not diff_text.strip():
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=["Synthesized unified diff produces zero net modifications."],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # 9. Budget Verification
    added_lines = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed_lines = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
    effective_budget = budget or RepairBudget()

    if (added_lines + removed_lines) > effective_budget.max_lines_changed:
        return DeterministicRepairResult(
            is_valid=False,
            rejection_reasons=[
                f"Patch exceeds budget: changed {added_lines + removed_lines} lines "
                f"(max allowed: {effective_budget.max_lines_changed})."
            ],
            target_file=norm_target,
            grounded_representation=grounded_rep,
        )

    # 10. Construct PatchCandidate
    patch_digest = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    patch_id = f"pat_{grounded_rep.finding_id[:16]}_{patch_digest[:8]}"

    candidate = PatchCandidate(
        patch_id=patch_id,
        contract_id=f"cnt_{patch_digest[:12]}",
        finding_id=grounded_rep.finding_id,
        base_commit_sha=grounded_rep.base_commit_sha,
        target_files=[norm_target],
        unified_diff=diff_text,
        added_lines=added_lines,
        removed_lines=removed_lines,
        hunk_count=1,
        sha256_digest=patch_digest,
    )

    return DeterministicRepairResult(
        is_valid=True,
        unified_diff=diff_text,
        rejection_reasons=[],
        target_file=norm_target,
        lines_added=added_lines,
        lines_removed=removed_lines,
        ast_valid=True,
        semantic_invariants_valid=True,
        patch_candidate=candidate,
        grounded_representation=grounded_rep,
    )
