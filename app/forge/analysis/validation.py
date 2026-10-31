"""Deterministic finding claim, evidence, and deduplication validation."""

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.analysis.evidence import normalize_relative_path
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ToolExecutionError(Exception):
    """Raised when a tool execution fails or violates constraints."""
    pass


class FindingValidationError(ToolExecutionError):
    """Base exception for finding claim validation failures."""
    pass


class SyntaxClaimValidationError(FindingValidationError):
    """Raised when a syntax claim is deterministically disproven or unrelated."""
    pass


class ConsistencyValidationError(FindingValidationError):
    """Raised when defect category, title, description, and source are internally contradictory."""
    pass


class InsufficientEvidenceError(FindingValidationError):
    """Raised when evidence context is insufficient or unrelated to the alleged defect."""
    pass


class DuplicateFindingError(FindingValidationError):
    """Raised when a candidate finding is rejected as an unhandled duplicate."""
    pass


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ValidationOutcome:
    """Outcome of validating a candidate finding claim."""
    is_valid: bool
    is_duplicate: bool = False
    duplicate_of_id: Optional[str] = None
    action: str = "accepted"  # "accepted", "duplicate_prevented", "duplicate_linked"
    adjusted_confidence: Optional[FindingConfidence] = None
    limitations: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Normalization and Similarity Utilities
# ---------------------------------------------------------------------------

STOP_WORDS = {
    "a", "an", "the", "in", "on", "at", "for", "to", "of", "and", "or",
    "is", "it", "this", "that", "with", "by", "from", "as", "into", "through",
}

CATEGORY_MAP = {
    "syntax": "syntax_error",
    "syntax_error": "syntax_error",
    "parse_error": "syntax_error",
    "grammar": "syntax_error",
    "ast_syntax": "syntax_error",
    "loop": "loop",
    "loop_defect": "loop",
    "infinite_loop": "loop",
    "iteration_defect": "loop",
    "logic": "logic_bug",
    "logic_bug": "logic_bug",
    "logic_error": "logic_bug",
    "incorrect_logic": "logic_bug",
    "boundary": "boundary_condition",
    "boundary_condition": "boundary_condition",
    "off_by_one": "boundary_condition",
    "index_error": "boundary_condition",
    "validation": "missing_validation",
    "missing_validation": "missing_validation",
    "null_check": "missing_validation",
    "security": "security",
    "vulnerability": "security",
    "leak": "resource_leak",
    "resource_leak": "resource_leak",
    "memory_leak": "resource_leak",
}


def canonicalize_category(raw_category: Optional[str]) -> str:
    """Map a raw defect category string to its canonical taxonomy group."""
    if not raw_category:
        return "general"
    norm = re.sub(r"[\s\-_]+", "_", raw_category.strip().lower())
    return CATEGORY_MAP.get(norm, norm)


def normalize_tokens(text: str) -> Set[str]:
    """Tokenize text into lowercased alphanumeric words, removing stop words."""
    words = re.findall(r"\b[a-zA-Z0-9_]+\b", text.lower())
    return {w for w in words if w not in STOP_WORDS and len(w) > 1}


def compute_token_jaccard_similarity(tokens1: Set[str], tokens2: Set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not tokens1 or not tokens2:
        return 0.0
    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)
    return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Static Syntax Validation (Python AST)
# ---------------------------------------------------------------------------

def is_syntax_defect_claim(
    defect_category: Optional[str],
    title: str,
    description: str,
) -> bool:
    """Determine whether a candidate finding claims a syntax or parsing defect."""
    cat = canonicalize_category(defect_category)
    if cat == "syntax_error":
        return True

    text = f"{title} {description}".lower()
    syntax_indicators = [
        "syntax error",
        "syntaxerror",
        "missing colon",
        "expected ':'",
        "invalid syntax",
        "unexpected token",
        "unmatched parenthesis",
        "unmatched bracket",
        "unexpected eof",
        "indentation error",
        "parsing error",
    ]
    return any(ind in text for ind in syntax_indicators)


def validate_python_syntax_claim(
    file_path: str,
    full_content: str,
    start_line: int,
    end_line: int,
    slice_lines: List[str],
    title: str,
    description: str,
) -> None:
    """Validate a syntax defect claim against deterministic Python AST parsing.

    Raises:
        SyntaxClaimValidationError: If the file is provably syntactically valid.
        InsufficientEvidenceError: If a real syntax error exists but cited range is unrelated.
    """
    try:
        ast.parse(full_content, filename=file_path)
        # AST parse succeeded: The entire file has valid Python syntax!
        # Any claim of a syntax error or missing colon is deterministically FALSE.
        raise SyntaxClaimValidationError(
            f"Deterministic static syntax analysis confirmed that '{file_path}' has valid Python syntax. "
            f"Rejected false positive syntax error claim ('{title}')."
        )
    except SyntaxError as syn_err:
        # A real syntax error exists in the file. Verify whether the cited range localizes it.
        err_line = syn_err.lineno or 1
        margin = 2
        covers_error = (start_line - margin) <= err_line <= (end_line + margin)
        if not covers_error:
            raise InsufficientEvidenceError(
                f"File '{file_path}' contains a syntax error at line {err_line} ('{syn_err.msg}'), "
                f"but cited line range {start_line}-{end_line} does not cover or localize the defect."
            )
        # If the claim specifically alleges a missing colon, check if the syntax error matches
        claim_text = f"{title} {description}".lower()
        if "missing colon" in claim_text:
            # Check if error message or line supports missing colon
            is_colon_error = "colon" in (syn_err.msg or "").lower() or syn_err.msg == "expected ':'"
            if not is_colon_error and "expected" in (syn_err.msg or "").lower() and ":" in (syn_err.msg or ""):
                is_colon_error = True
            if not is_colon_error:
                # Syntax error exists but is not a missing colon
                pass


def validate_missing_colon_claim(
    slice_lines: List[str],
    title: str,
    description: str,
) -> None:
    """Validate specific 'missing colon' claims against the cited excerpt."""
    claim_text = f"{title} {description}".lower()
    if "missing colon" not in claim_text and "expected ':'" not in claim_text:
        return

    # Check if the cited line already contains a colon
    # For example: `"output_type": (get_type_as_string(self.type) or ""),` or `"name": self.name,`
    for line in slice_lines:
        stripped = line.strip()
        # If line has a colon and looks like a dictionary key-value or slice
        if ":" in stripped:
            parts = stripped.split(":", 1)
            key_part = parts[0].strip()
            # If it's a valid string key with colon: `"output_type":`
            if (key_part.startswith('"') and key_part.endswith('"')) or (key_part.startswith("'") and key_part.endswith("'")):
                raise InsufficientEvidenceError(
                    f"Evidence line '{stripped}' already contains a colon for key '{key_part}'. "
                    f"Allegation of 'missing colon' is unsupported by the cited source."
                )


# ---------------------------------------------------------------------------
# Category & Evidence Consistency Validation
# ---------------------------------------------------------------------------

def validate_category_consistency(
    file_path: str,
    full_content: str,
    start_line: int,
    end_line: int,
    slice_lines: List[str],
    defect_category: Optional[str],
    title: str,
    description: str,
) -> None:
    """Ensure that the declared defect category, title, description, and source are consistent.

    Raises:
        ConsistencyValidationError: If internal contradictions are identified.
        InsufficientEvidenceError: If the source excerpt lacks required context for the category.
    """
    cat = canonicalize_category(defect_category)
    title_lower = title.lower()
    desc_lower = description.lower()

    # Rule 1: Loop defect consistency
    is_loop_claimed = cat == "loop" or "loop" in title_lower
    if is_loop_claimed:
        # Check if description actually talks about a syntax error
        syntax_words = ["missing colon", "syntax error", "unexpected token", "expected ':'"]
        has_syntax_desc = any(w in desc_lower for w in syntax_words)
        has_loop_desc = any(w in desc_lower for w in ["iteration", "infinite", "loop", "termination", "cycle", "while", "for"])

        if has_syntax_desc and not has_loop_desc:
            raise ConsistencyValidationError(
                f"Inconsistent classification: defect category '{defect_category or 'loop'}' contradicts "
                f"description alleging a syntax error ('{description[:60]}...')."
            )

        # Check if evidence contains loop constructs
        raw_excerpt = "\n".join(slice_lines)
        has_loop_keyword = any(kw in raw_excerpt for kw in ["for ", "while ", "for\t", "while\t", "__iter__", "iter("])

        # If Python file, inspect AST nodes enclosing the range
        has_loop_in_ast = False
        if file_path.endswith((".py", ".pyi")):
            try:
                tree = ast.parse(full_content, filename=file_path)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                        node_start = getattr(node, "lineno", 0)
                        node_end = getattr(node, "end_lineno", node_start)
                        # Check overlap with excerpt
                        if max(start_line, node_start) <= min(end_line, node_end):
                            has_loop_in_ast = True
                            break
            except Exception:
                pass

        if not has_loop_keyword and not has_loop_in_ast:
            raise InsufficientEvidenceError(
                f"Evidence context for category '{defect_category or 'loop'}' cited lines {start_line}-{end_line} "
                f"which contain no loop construct (for, while, or recursion)."
            )

    # Rule 2: Syntax defect consistency
    if cat == "syntax_error":
        # Check if description talks purely about non-syntax issues
        if "sql injection" in desc_lower or "cross-site" in desc_lower or "privilege escalation" in desc_lower:
            raise ConsistencyValidationError(
                f"Inconsistent classification: defect category 'syntax_error' contradicts security description."
            )


# ---------------------------------------------------------------------------
# Duplicate Finding Detection
# ---------------------------------------------------------------------------

def check_duplicate_finding(
    store: Optional[ForgeStore],
    run_id: str,
    norm_path: str,
    start_line: int,
    end_line: int,
    defect_category: Optional[str],
    title: str,
    description: str,
    content_digest: str,
) -> Optional[Tuple[Finding, bool]]:
    """Detect whether a candidate finding is a duplicate of an existing finding in this run.

    Returns:
        (existing_finding, is_identical_evidence) or None if not a duplicate.
    """
    if not store:
        return None

    try:
        existing_findings = store.list_findings(run_id=run_id)
    except Exception:
        return None

    if not existing_findings:
        return None

    cand_tokens = normalize_tokens(description)
    cand_title_tokens = normalize_tokens(title)
    cand_cat = canonicalize_category(defect_category)

    for ex in existing_findings:
        if norm_path not in ex.affected_files:
            continue

        ex_evidence = store.list_evidence_for_finding(ex.finding_id)
        # Check evidence line overlap for this file
        matching_ev = [
            ev for ev in ex_evidence
            if ev.file_path == norm_path and ev.start_line is not None and ev.end_line is not None
        ]

        has_range_overlap = any(
            max(start_line, ev.start_line) <= min(end_line, ev.end_line)
            for ev in matching_ev
        )

        exact_range_match = any(
            start_line == ev.start_line and end_line == ev.end_line
            for ev in matching_ev
        )

        has_same_digest = any(
            content_digest == ev.details.get("content_digest")
            for ev in matching_ev
        )

        # Retrieve existing category
        ex_cat_raw = ex_evidence[0].details.get("defect_category") if ex_evidence else None
        ex_cat = canonicalize_category(ex_cat_raw) if ex_cat_raw else canonicalize_category(ex.title)

        ex_tokens = normalize_tokens(ex.description)
        ex_title_tokens = normalize_tokens(ex.title)

        desc_sim = compute_token_jaccard_similarity(cand_tokens, ex_tokens)
        title_sim = compute_token_jaccard_similarity(cand_title_tokens, ex_title_tokens)

        # Duplicate Condition 1: Same category, overlapping range, and textual similarity
        is_same_category = (cand_cat == ex_cat)
        if has_range_overlap and is_same_category and (desc_sim >= 0.4 or title_sim >= 0.5):
            return (ex, has_same_digest and exact_range_match)

        # Duplicate Condition 2: Identical or near-identical description on overlapping range
        # (Catches cases like Hamilton Failure B & C where description is repeated despite category changes)
        if has_range_overlap and (desc_sim >= 0.65 or description.strip().lower() == ex.description.strip().lower()):
            return (ex, has_same_digest and exact_range_match)

        # Duplicate Condition 3: Exact range match with same digest and moderate similarity
        if exact_range_match and has_same_digest and (desc_sim >= 0.3 or title_sim >= 0.4):
            return (ex, True)

    return None


# ---------------------------------------------------------------------------
# Master Finding Claim Validator
# ---------------------------------------------------------------------------

def validate_finding_claim(
    workspace_path: Path,
    norm_path: str,
    resolved_file: Path,
    start_line: int,
    end_line: int,
    total_lines: int,
    slice_lines: List[str],
    content_digest: str,
    defect_category: Optional[str],
    title: str,
    description: str,
    confidence: FindingConfidence,
    store: Optional[ForgeStore] = None,
    run_id: Optional[str] = None,
) -> ValidationOutcome:
    """Perform deterministic claim validation, static syntax checks, and deduplication.

    Raises:
        SyntaxClaimValidationError: If a claimed syntax error is disproven by static analysis.
        ConsistencyValidationError: If category, title, description, and code are contradictory.
        InsufficientEvidenceError: If evidence context does not support the defect claim.
        DuplicateFindingError: If finding is an exact duplicate of an existing finding.
    """
    # Step 1: Content non-emptiness
    non_empty_lines = [l for l in slice_lines if l.strip()]
    if not non_empty_lines:
        raise InsufficientEvidenceError(
            f"Cited source range {start_line}-{end_line} in '{norm_path}' contains only empty lines or whitespace."
        )

    # Read full file content safely
    try:
        full_content = resolved_file.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ToolExecutionError(f"Failed to read file '{norm_path}' for validation: {exc}")

    # Step 2: Category and evidence consistency
    validate_category_consistency(
        file_path=norm_path,
        full_content=full_content,
        start_line=start_line,
        end_line=end_line,
        slice_lines=slice_lines,
        defect_category=defect_category,
        title=title,
        description=description,
    )

    # Step 3: Python static syntax analysis
    if norm_path.endswith((".py", ".pyi")):
        if is_syntax_defect_claim(defect_category, title, description):
            validate_python_syntax_claim(
                file_path=norm_path,
                full_content=full_content,
                start_line=start_line,
                end_line=end_line,
                slice_lines=slice_lines,
                title=title,
                description=description,
            )

    # Step 4: Specific missing-colon validation against excerpt
    validate_missing_colon_claim(
        slice_lines=slice_lines,
        title=title,
        description=description,
    )

    # Step 5: Duplicate finding detection
    if store and run_id:
        dup_match = check_duplicate_finding(
            store=store,
            run_id=run_id,
            norm_path=norm_path,
            start_line=start_line,
            end_line=end_line,
            defect_category=defect_category,
            title=title,
            description=description,
            content_digest=content_digest,
        )
        if dup_match:
            ex_finding, is_identical_evidence = dup_match
            if is_identical_evidence:
                raise DuplicateFindingError(
                    f"Duplicate finding detected: Defect is already tracked under finding "
                    f"'{ex_finding.finding_id}' ('{ex_finding.title}') at {norm_path}:{start_line}-{end_line}."
                )
            else:
                # Evidence is non-identical (e.g. additional lines): can be linked
                return ValidationOutcome(
                    is_valid=True,
                    is_duplicate=True,
                    duplicate_of_id=ex_finding.finding_id,
                    action="duplicate_linked",
                    reason=f"Linked additional evidence to existing finding '{ex_finding.finding_id}'",
                )

    # Step 6: Uncertainty preservation for semantic claims
    adjusted_conf = confidence
    if confidence == FindingConfidence.CONFIRMED:
        # Static LLM inspection cannot confirm a defect without reproduction verification
        adjusted_conf = FindingConfidence.HIGH

    limitations = "Semantic correctness could not be established deterministically without an isolated execution reproduction."

    return ValidationOutcome(
        is_valid=True,
        is_duplicate=False,
        action="accepted",
        adjusted_confidence=adjusted_conf,
        limitations=limitations,
    )
