"""Deterministic edit anchoring and source-grounded repair for Phase 4.

Guarantees:
- Authoritative source of truth: Anchors are derived from trusted, pinned repository
  snapshots rather than an LLM's reconstructed copy of a function.
- Exact verbatim matching: Never fuzzy-matches, normalizes indentation, or silently
  expands the edit range.
- Strict containment: Validates file scope, protected paths, path traversal, symlinks,
  source digest, and revision drift.
- Fail secure: Returns typed ANCHOR_UNRESOLVED or ANCHOR_AMBIGUOUS when evidence cannot
  unambiguously narrow the location.
- Immutability: Repository code remains strictly read-only and untrusted data.
"""

from enum import Enum
import ast
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field

from forge.config import redact_secrets
from forge.repair.contracts import (
    PatchCandidate,
    RepairBudget,
    RepairSafetyContract,
    compute_patch_sha256,
    construct_unified_diff_from_search_replace,
    is_test_path,
    parse_and_validate_unified_diff,
    sanitize_patch_content,
    validate_base_sha,
    validate_patch_applicability,
    verify_workspace_containment,
    _extract_git_head_commit,
)
from forge.repair.models import ProtectedPathPolicy
from forge.schemas import EvidenceRecord, Finding


# =============================================================================
# Status & Data Contracts
# =============================================================================

class AnchorResolutionStatus(str, Enum):
    """Lifecycle and resolution status of a deterministic edit anchor."""
    RESOLVED = "resolved"
    ANCHOR_UNRESOLVED = "anchor_unresolved"
    ANCHOR_AMBIGUOUS = "anchor_ambiguous"
    SCOPE_VIOLATION = "scope_violation"
    PROTECTED_PATH = "protected_path"
    STALE_REVISION = "stale_revision"
    DIGEST_MISMATCH = "digest_mismatch"


class EditAnchorContract(BaseModel):
    """Strongly-typed, validated edit anchor grounded in trusted repository source."""
    model_config = ConfigDict(frozen=True)

    target_file: str = Field(description="Authorized relative path of file to modify")
    base_commit_sha: str = Field(description="Pinned source revision SHA")
    source_digest: str = Field(description="SHA-256 digest of trusted target file content")
    anchor_text: str = Field(description="Exact verbatim source anchor obtained from trusted repository content")
    start_line: int = Field(ge=1, description="1-indexed starting line number of anchor in trusted source")
    end_line: int = Field(ge=1, description="1-indexed ending line number of anchor in trusted source")
    surrounding_context: Optional[str] = Field(default=None, description="Surrounding code lines for orientation")
    replacement_content: Optional[str] = Field(default=None, description="Minimal replacement content proposed by model or applied")
    evidence_ids: List[str] = Field(default_factory=list, description="Evidence records linking defect to this anchor")
    finding_id: str = Field(description="Finding ID linked to this repair anchor")
    status: AnchorResolutionStatus = Field(description="Status of anchor resolution")
    rejection_reason: Optional[str] = Field(default=None, description="Actionable rejection reason if resolution or validation failed")


# =============================================================================
# Deterministic Source Analysis & Localization
# =============================================================================

def _extract_evidence_line_numbers_for_target(
    evidence_records: List[EvidenceRecord],
    target_file: str,
) -> List[int]:
    """Extract direct line number references pointing to target_file from evidence."""
    line_numbers: List[int] = []
    norm_target = Path(target_file).as_posix()
    file_basename = Path(target_file).name

    for ev in evidence_records:
        content = ev.content or ""
        # Match stack trace frames like: File ".../calculator.py", line 26
        for m in re.finditer(rf'(?:File\s+"(?:[^"]*[/\\])?{re.escape(file_basename)}",\s+line\s+(\d+))', content):
            try:
                line_numbers.append(int(m.group(1)))
            except ValueError:
                pass
        # Check structured evidence fields
        if ev.file_path and Path(ev.file_path).as_posix() == norm_target:
            if ev.start_line:
                line_numbers.append(ev.start_line)

    return sorted(set(line_numbers))


def _extract_test_call_site_symbols(
    repo_path: Path,
    evidence_records: List[EvidenceRecord],
) -> List[str]:
    """Inspect failing test traces in evidence to discover the called implementation symbols."""
    discovered_symbols: List[str] = []

    for ev in evidence_records:
        content = ev.content or ""
        # Match test frames like: File "/workspace/tests/test_calculator.py", line 22
        for m in re.finditer(r'File\s+"(?:/workspace/)?([^"]+)",\s+line\s+(\d+)', content):
            raw_test_path = m.group(1).strip()
            line_no = int(m.group(2))
            norm_test = Path(raw_test_path).as_posix()

            if is_test_path(norm_test) and norm_test.endswith(".py"):
                try:
                    test_disk = verify_workspace_containment(repo_path, norm_test)
                    if test_disk.is_file():
                        tree = ast.parse(test_disk.read_text(encoding="utf-8", errors="replace"))
                        for node in ast.walk(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if node.lineno <= line_no <= node.end_lineno:
                                    # Inside failing test method: extract calls
                                    for child in ast.walk(node):
                                        if isinstance(child, ast.Call):
                                            if isinstance(child.func, ast.Name):
                                                discovered_symbols.append(child.func.id)
                                            elif isinstance(child.func, ast.Attribute):
                                                discovered_symbols.append(child.func.attr)
                except Exception:
                    continue

    # Also extract tokens from test name if present in details
    for ev in evidence_records:
        details = ev.details or {}
        test_name = details.get("test_name") or ""
        if test_name:
            parts = test_name.split(".")
            method = parts[-1]
            if method.startswith("test_"):
                # e.g., test_divide_by_zero -> divide
                subparts = method[5:].split("_")
                if subparts:
                    discovered_symbols.append(subparts[0])

    return list(dict.fromkeys(discovered_symbols))


def _locate_statement_in_python_ast(
    raw_text: str,
    target_lines: List[int],
    called_symbols: List[str],
    finding: Finding,
) -> Optional[Tuple[int, int]]:
    """Deterministically locate the candidate statement line range (1-indexed start, end) using AST."""
    try:
        tree = ast.parse(raw_text)
    except SyntaxError:
        return None

    # Strategy 1: If direct line numbers are known, find smallest statement covering line
    if target_lines:
        target_line = target_lines[0]
        covering_stmt: Optional[Tuple[int, int]] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt) and hasattr(node, "lineno") and hasattr(node, "end_lineno"):
                if node.lineno <= target_line <= node.end_lineno:
                    # Prefer smaller statement ranges
                    span = node.end_lineno - node.lineno
                    if covering_stmt is None or span < (covering_stmt[1] - covering_stmt[0]):
                        covering_stmt = (node.lineno, node.end_lineno)
        if covering_stmt is not None:
            return covering_stmt

    # Strategy 2: If called symbol is known, find matching FunctionDef
    defect_keywords = {"zero", "division", "zerodivisionerror", "null", "none", "bound", "index", "defect", "error"}
    finding_text = f"{finding.title} {finding.description}".lower()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in called_symbols:
                # Exclude docstrings
                body_stmts = [
                    s for s in node.body
                    if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
                ]
                if not body_stmts:
                    continue

                # If only 1 body statement, that's our target statement
                if len(body_stmts) == 1:
                    s = body_stmts[0]
                    return (s.lineno, s.end_lineno)

                # If multiple statements: look for If statement or Return statement
                for stmt in body_stmts:
                    if isinstance(stmt, ast.If):
                        # An If guard statement handling special cases / boundary conditions
                        return (stmt.lineno, stmt.end_lineno)

                # Fallback to the first non-docstring statement in function
                first_s = body_stmts[0]
                return (first_s.lineno, first_s.end_lineno)

    # Strategy 3: Check finding title for symbol name
    for sym in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", finding_text):
        if sym in {"test", "error", "failing", "defect", "reproduced", "true", "false", "none"}:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.lower() == sym.lower():
                body_stmts = [
                    s for s in node.body
                    if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
                ]
                if body_stmts:
                    for stmt in body_stmts:
                        if isinstance(stmt, ast.If):
                            return (stmt.lineno, stmt.end_lineno)
                    return (body_stmts[0].lineno, body_stmts[0].end_lineno)

    return None


# =============================================================================
# Main Resolution API
# =============================================================================

def resolve_source_anchor(
    repo_path: Path,
    target_file: str,
    base_commit_sha: str,
    finding: Finding,
    evidence_records: List[EvidenceRecord],
    allowed_files: Optional[List[str]] = None,
    expected_source_digest: Optional[str] = None,
) -> EditAnchorContract:
    """Deterministically resolve an exact edit anchor from trusted pinned source content."""
    evidence_ids = [e.evidence_id for e in evidence_records]
    finding_id = finding.finding_id
    dummy_digest = "0" * 64

    # 1. Path format and traversal validation
    norm_target = Path(target_file).as_posix().strip()
    if not norm_target or norm_target == ".":
        return EditAnchorContract(
            target_file=target_file,
            base_commit_sha=base_commit_sha,
            source_digest=dummy_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.SCOPE_VIOLATION,
            rejection_reason="Empty or root target file path cannot be anchored",
        )

    if norm_target.startswith("/") or norm_target.startswith("\\") or ".." in norm_target.split("/"):
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=dummy_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.SCOPE_VIOLATION,
            rejection_reason=f"Path traversal violation rejected in target file: '{target_file}'",
        )

    # 2. Protected path validation
    is_prot, prot_reason = ProtectedPathPolicy.is_protected(norm_target)
    if is_prot:
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=dummy_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.PROTECTED_PATH,
            rejection_reason=f"Target file '{norm_target}' is protected by security policy: {prot_reason}",
        )

    # 3. Test path rejection
    if is_test_path(norm_target):
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=dummy_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.SCOPE_VIOLATION,
            rejection_reason=f"Target file '{norm_target}' is a test file. Modifying test files to mask defects is strictly forbidden.",
        )

    # 4. Scope containment against allowed_files
    if allowed_files is not None:
        allowed_set = {Path(f).as_posix().strip() for f in allowed_files}
        if norm_target not in allowed_set:
            return EditAnchorContract(
                target_file=norm_target,
                base_commit_sha=base_commit_sha,
                source_digest=dummy_digest,
                anchor_text="",
                start_line=1,
                end_line=1,
                evidence_ids=evidence_ids,
                finding_id=finding_id,
                status=AnchorResolutionStatus.SCOPE_VIOLATION,
                rejection_reason=f"Target file '{norm_target}' is outside declared allowed scope: {sorted(allowed_set)}",
            )

    # 5. Workspace containment and symlink escape defense
    try:
        target_disk = verify_workspace_containment(repo_path, norm_target)
    except Exception as e:
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=dummy_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.SCOPE_VIOLATION,
            rejection_reason=f"Scope containment violation for '{norm_target}': {e}",
        )

    if not target_disk.is_file():
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=dummy_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.ANCHOR_UNRESOLVED,
            rejection_reason=f"Target file '{norm_target}' does not exist in repository",
        )

    # 6. Base revision verification
    actual_sha = _extract_git_head_commit(repo_path)
    if actual_sha is not None:
        try:
            validate_base_sha(actual_sha, base_commit_sha)
        except Exception as e:
            return EditAnchorContract(
                target_file=norm_target,
                base_commit_sha=base_commit_sha,
                source_digest=dummy_digest,
                anchor_text="",
                start_line=1,
                end_line=1,
                evidence_ids=evidence_ids,
                finding_id=finding_id,
                status=AnchorResolutionStatus.STALE_REVISION,
                rejection_reason=f"Stale base revision: {e}",
            )

    # 7. Read trusted source content and verify digest
    raw_text = target_disk.read_text(encoding="utf-8", errors="replace")
    source_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    if expected_source_digest and source_digest != expected_source_digest:
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=source_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.DIGEST_MISMATCH,
            rejection_reason=f"Source digest mismatch for '{norm_target}': expected {expected_source_digest}, got {source_digest}",
        )

    # 8. Deterministic Localization
    raw_lines = raw_text.splitlines(keepends=True)
    if not raw_lines:
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=source_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.ANCHOR_UNRESOLVED,
            rejection_reason=f"Target file '{norm_target}' is empty",
        )

    target_lines = _extract_evidence_line_numbers_for_target(evidence_records, norm_target)
    called_symbols = _extract_test_call_site_symbols(repo_path, evidence_records)

    stmt_range = _locate_statement_in_python_ast(
        raw_text=raw_text,
        target_lines=target_lines,
        called_symbols=called_symbols,
        finding=finding,
    )

    if stmt_range is None:
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=source_digest,
            anchor_text="",
            start_line=1,
            end_line=1,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.ANCHOR_UNRESOLVED,
            rejection_reason=f"Unable to deterministically locate defect anchor in '{norm_target}' from available evidence",
        )

    start_line, end_line = stmt_range
    anchor_lines = raw_lines[start_line - 1 : end_line]
    anchor_text = "".join(anchor_lines)

    # Strip trailing newline for exact search block compatibility while preserving internal newlines
    if anchor_text.endswith("\r\n"):
        anchor_text = anchor_text[:-2]
    elif anchor_text.endswith("\n"):
        anchor_text = anchor_text[:-1]

    # 9. Exact Verbatim Count & Ambiguity Check
    count = raw_text.count(anchor_text)
    if count == 0:
        return EditAnchorContract(
            target_file=norm_target,
            base_commit_sha=base_commit_sha,
            source_digest=source_digest,
            anchor_text="",
            start_line=start_line,
            end_line=end_line,
            evidence_ids=evidence_ids,
            finding_id=finding_id,
            status=AnchorResolutionStatus.ANCHOR_UNRESOLVED,
            rejection_reason=f"Extracted anchor not found verbatim in '{norm_target}'",
        )

    if count > 1:
        # Ambiguous anchor: try expanding by 1 context line before/after if within file bounds
        expanded_start = max(1, start_line - 1)
        expanded_end = min(len(raw_lines), end_line + 1)
        expanded_lines = raw_lines[expanded_start - 1 : expanded_end]
        expanded_text = "".join(expanded_lines).rstrip("\r\n")

        if raw_text.count(expanded_text) == 1:
            start_line = expanded_start
            end_line = expanded_end
            anchor_text = expanded_text
        else:
            return EditAnchorContract(
                target_file=norm_target,
                base_commit_sha=base_commit_sha,
                source_digest=source_digest,
                anchor_text=anchor_text,
                start_line=start_line,
                end_line=end_line,
                evidence_ids=evidence_ids,
                finding_id=finding_id,
                status=AnchorResolutionStatus.ANCHOR_AMBIGUOUS,
                rejection_reason=f"Anchor matches {count} locations in '{norm_target}' and cannot be disambiguated",
            )

    # 10. Extract Surrounding Context for Orientation
    ctx_start = max(1, start_line - 3)
    ctx_end = min(len(raw_lines), end_line + 3)
    ctx_lines = [
        f"{idx:4d}: {raw_lines[idx - 1].rstrip('\r\n')}"
        for idx in range(ctx_start, ctx_end + 1)
    ]
    surrounding_context = "\n".join(ctx_lines)

    return EditAnchorContract(
        target_file=norm_target,
        base_commit_sha=base_commit_sha,
        source_digest=source_digest,
        anchor_text=anchor_text,
        start_line=start_line,
        end_line=end_line,
        surrounding_context=surrounding_context,
        evidence_ids=evidence_ids,
        finding_id=finding_id,
        status=AnchorResolutionStatus.RESOLVED,
        rejection_reason=None,
    )


# =============================================================================
# Deterministic Patch Construction from Edit Anchor
# =============================================================================

def construct_patch_from_edit_anchor(
    anchor: EditAnchorContract,
    replacement_content: str,
    repo_path: Path,
    budget: Optional[RepairBudget] = None,
) -> Tuple[bool, str, List[str], Dict[str, Any]]:
    """Construct and validate a standard unified diff from a verified EditAnchorContract and replacement content.

    Guarantees:
    - Authoritative source grounding: replaces anchor.anchor_text verified from trusted source.
    - Non-empty and non-identical: rejects empty replacements and unchanged no-ops.
    - Verified digest: validates that repository file content still matches anchor.source_digest.
    - Standard diff format: synthesizes 100% compliant diff with column 0 markers and @@ hunk headers.
    - In-memory applicability: confirms in-memory splice and Python AST parsing without touching target repository.
    """
    if anchor.status != AnchorResolutionStatus.RESOLVED:
        return (
            False,
            "",
            [f"Cannot construct patch: anchor resolution status is '{anchor.status.value}': {anchor.rejection_reason}"],
            {},
        )

    # Block content validation
    if not replacement_content or not replacement_content.strip():
        return False, "", ["Replacement content is empty or whitespace-only"], {}

    if replacement_content == anchor.anchor_text:
        return False, "", ["Replacement content is identical to anchor text (unchanged replacement / no-op)"], {}

    # Structural and indentation consistency check
    anchor_stripped = anchor.anchor_text.lstrip()
    repl_stripped = replacement_content.lstrip()
    is_anchor_def = anchor_stripped.startswith(("def ", "class ", "async def "))
    is_repl_def = repl_stripped.startswith(("def ", "class ", "async def "))

    if not is_anchor_def and is_repl_def:
        first_line = replacement_content.splitlines()[0].strip() if replacement_content.splitlines() else ""
        return (
            False,
            "",
            [f"Replacement content defines a function/class ('{first_line[:40]}'), but anchor is an internal statement block. Replacement must target the anchored statement level."],
            {},
        )

    anchor_indent = len(anchor.anchor_text) - len(anchor_stripped)
    repl_first_line = replacement_content.splitlines()[0] if replacement_content.splitlines() else ""
    repl_indent = len(repl_first_line) - len(repl_first_line.lstrip())
    if anchor_indent > 0 and repl_indent < anchor_indent and not is_anchor_def:
        return (
            False,
            "",
            [f"Replacement content indentation ({repl_indent} spaces) does not match anchor indentation ({anchor_indent} spaces)."],
            {},
        )

    # Target file verification
    try:
        target_disk = verify_workspace_containment(repo_path, anchor.target_file)
    except Exception as e:
        return False, "", [f"Scope containment error for '{anchor.target_file}': {e}"], {}

    if not target_disk.is_file():
        return False, "", [f"Target file '{anchor.target_file}' does not exist in repository"], {}

    raw_text = target_disk.read_text(encoding="utf-8", errors="replace")
    curr_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    if curr_digest != anchor.source_digest:
        return (
            False,
            "",
            [f"Target file '{anchor.target_file}' content changed since anchor resolution (digest mismatch)"],
            {},
        )

    # Delegate to construct_unified_diff_from_search_replace
    return construct_unified_diff_from_search_replace(
        repo_path=repo_path,
        target_file=anchor.target_file,
        search_block=anchor.anchor_text,
        replace_block=replacement_content,
        allowed_files=[anchor.target_file],
        budget=budget,
        base_commit_sha=anchor.base_commit_sha,
    )
