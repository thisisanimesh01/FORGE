from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple, runtime_checkable

from forge.analysis.evidence import normalize_relative_path
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    Finding,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)


@runtime_checkable
class FindingEvidenceProvider(Protocol):
    """Narrow read-only interface for finding and evidence querying during validation."""

    def list_findings(self, run_id: Optional[str] = None) -> List[Finding]:
        ...

    def list_evidence_for_finding(self, finding_id: str) -> List[EvidenceRecord]:
        ...



# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ToolExecutionError(Exception):
    """Raised when a tool execution fails or violates constraints."""
    pass


def build_rejection_directive(
    rejection_type: str,
    message: str,
    affected_file: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    defect_category: Optional[str] = None,
) -> str:
    """Generate concise, actionable corrective directive for rejected findings."""
    loc_str = f" for '{affected_file}:{start_line}-{end_line}'" if affected_file and start_line else (f" in '{affected_file}'" if affected_file else "")

    if rejection_type == "SyntaxClaimValidationError":
        return (
            f"Deterministic static syntax analysis confirmed that lines{loc_str} have valid Python syntax. "
            f"Do not resubmit this disproven syntax claim with altered wording or categories. "
            f"Investigate other source locations or produce a truthful final synthesis."
        )
    elif rejection_type == "ContradictionValidationError":
        return (
            f"Contradiction detected{loc_str}: {message}. "
            f"Do not submit claims contradicted by project configuration, implementation, or test assertions. "
            f"Verify actual behavior across files or produce your final synthesis."
        )
    elif rejection_type == "SeverityJustificationError":
        return (
            f"Unjustified severity level{loc_str}: {message}. "
            f"HIGH and CRITICAL findings require demonstrable, severe impact (e.g. exploitability, crash, data corruption). "
            f"Downgrade severity or provide concrete evidence of impact."
        )
    elif rejection_type == "TestAssertionMisinterpretationError":
        return (
            f"Test assertion misinterpretation{loc_str}: A test verifying error handling or negative cases "
            f"(e.g. expecting ValueError on invalid input) demonstrates intended validation, not a defect. "
            f"Do not treat test names or expected errors as defects without proof of test failure."
        )
    elif rejection_type == "UnrelatedEvidenceError":
        return (
            f"The cited evidence{loc_str} does not contain elements related to the claimed defect ('{defect_category or 'unspecified'}'). "
            f"Cite the actual source lines demonstrating the defect or investigate other modules."
        )
    elif rejection_type == "InsufficientEvidenceError":
        return (
            f"The cited source evidence{loc_str} does not substantiate a defect under category '{defect_category or 'unspecified'}'. "
            f"Do not resubmit this claim for this line range without concrete observed evidence. "
            f"Investigate other source locations or conclude your investigation."
        )
    elif rejection_type == "ConsistencyValidationError":
        return (
            f"Finding classification contradicts the cited code or description{loc_str}. "
            f"Do not submit contradictory defect classifications. Verify the actual code construct or produce your final synthesis."
        )
    elif rejection_type == "DuplicateFindingError":
        return (
            f"This defect is already recorded under an existing finding{loc_str}. "
            f"Do not resubmit duplicate findings. Inspect other modules or synthesize your findings."
        )
    elif rejection_type == "RepeatedRejectedClaimError":
        return (
            f"A substantially equivalent claim{loc_str} was already rejected earlier in this investigation. "
            f"Do not repeatedly retry claims for this line range. Investigate other files or provide your final synthesis."
        )
    else:
        return (
            f"Finding claim rejected{loc_str}: {message}. "
            f"Do not resubmit this claim. Investigate other source locations or produce a truthful final synthesis."
        )


class FindingValidationError(ToolExecutionError):
    """Base exception for finding claim validation failures with structured context."""

    def __init__(
        self,
        message: str,
        *,
        affected_file: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        defect_category: Optional[str] = None,
        directive: Optional[str] = None,
        rejection_type: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.affected_file = affected_file
        self.start_line = start_line
        self.end_line = end_line
        self.defect_category = defect_category
        self.rejection_type = rejection_type or self.__class__.__name__
        self.directive = directive

    def to_rejection_dict(self) -> Dict[str, Any]:
        """Convert finding rejection into structured, actionable feedback dictionary."""
        line_range = None
        if self.start_line is not None and self.end_line is not None:
            line_range = {"start": self.start_line, "end": self.end_line}

        directive = self.directive or build_rejection_directive(
            rejection_type=self.rejection_type,
            message=self.message,
            affected_file=self.affected_file,
            start_line=self.start_line,
            end_line=self.end_line,
            defect_category=self.defect_category,
        )

        return {
            "status": "rejected",
            "rejection_type": self.rejection_type,
            "error_type": self.rejection_type,
            "reason": self.message,
            "error": self.message,  # Backward compatibility
            "affected_file": self.affected_file,
            "line_range": line_range,
            "defect_category": self.defect_category,
            "directive": directive,
        }


class SyntaxClaimValidationError(FindingValidationError):
    """Raised when a syntax claim is deterministically disproven or unrelated."""
    pass


class ConsistencyValidationError(FindingValidationError):
    """Raised when defect category, title, description, and source are internally contradictory."""
    pass


class InsufficientEvidenceError(FindingValidationError):
    """Raised when evidence context is insufficient or unrelated to the alleged defect."""
    pass


class UnrelatedEvidenceError(InsufficientEvidenceError):
    """Raised when cited lines have no relevance to the alleged defect."""
    pass


class ContradictionValidationError(FindingValidationError):
    """Raised when an LLM claim is contradicted by source code, dependency manifests, or test semantics."""
    pass


class SeverityJustificationError(FindingValidationError):
    """Raised when HIGH or CRITICAL severity is unjustified or unsupported by demonstrated impact."""
    pass


class TestAssertionMisinterpretationError(FindingValidationError):
    """Raised when a test assertion verifying error handling or negative cases is misinterpreted as a defect."""
    __test__ = False


class DuplicateFindingError(FindingValidationError):
    """Raised when a candidate finding is rejected as an unhandled duplicate."""
    pass


class RepeatedRejectedClaimError(FindingValidationError):
    """Raised when a candidate finding was already rejected earlier in the same investigation."""
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
    adjusted_severity: Optional[FindingSeverity] = None
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
# Per-Run Rejected Claim Memory & Anti-Retry Tracker
# ---------------------------------------------------------------------------

@dataclass
class RejectedClaimRecord:
    """Record of a rejected candidate finding within an audit run."""
    norm_path: str
    start_line: int
    end_line: int
    canonical_category: str
    title_tokens: Set[str]
    description_tokens: Set[str]
    raw_title: str
    raw_description: str
    rejection_type: str
    rejection_reason: str


class RejectedClaimTracker:
    """Tracks rejected finding submissions per run to prevent duplicate/spam retries."""

    def __init__(self) -> None:
        self._records: List[RejectedClaimRecord] = []

    def record_rejected_claim(
        self,
        norm_path: str,
        start_line: int,
        end_line: int,
        defect_category: Optional[str],
        title: str,
        description: str,
        rejection_type: str,
        rejection_reason: str,
    ) -> None:
        """Store a rejected finding candidate to detect subsequent retries."""
        cand_cat = canonicalize_category(defect_category)
        if cand_cat == "general":
            cand_cat = canonicalize_category(title)
        self._records.append(
            RejectedClaimRecord(
                norm_path=norm_path,
                start_line=start_line,
                end_line=end_line,
                canonical_category=cand_cat,
                title_tokens=normalize_tokens(title),
                description_tokens=normalize_tokens(description),
                raw_title=title,
                raw_description=description,
                rejection_type=rejection_type,
                rejection_reason=rejection_reason,
            )
        )

    def check_repeated_rejection(
        self,
        norm_path: str,
        start_line: int,
        end_line: int,
        defect_category: Optional[str],
        title: str,
        description: str,
    ) -> Optional[RejectedClaimRecord]:
        """Detect whether this submission is a repeated or rephrased attempt of an already rejected claim.

        Returns matching RejectedClaimRecord if repeated, or None if genuinely distinct.
        """
        cand_cat = canonicalize_category(defect_category)
        if cand_cat == "general":
            cand_cat = canonicalize_category(title)
        cand_desc_tokens = normalize_tokens(description)
        cand_title_tokens = normalize_tokens(title)

        for rec in self._records:
            if rec.norm_path != norm_path:
                continue

            # Check for range overlap
            has_range_overlap = max(start_line, rec.start_line) <= min(end_line, rec.end_line)
            if not has_range_overlap:
                continue

            exact_range_match = (start_line == rec.start_line and end_line == rec.end_line)

            desc_sim = compute_token_jaccard_similarity(cand_desc_tokens, rec.description_tokens)
            title_sim = compute_token_jaccard_similarity(cand_title_tokens, rec.title_tokens)
            is_same_category = (cand_cat == rec.canonical_category)

            # Condition 1: Substantially identical description on overlapping range (even if title changed)
            if desc_sim >= 0.5 or (description.strip().lower() == rec.raw_description.strip().lower() and len(description.strip()) > 5):
                return rec

            # Condition 2: Same category and moderate similarity on overlapping range
            if is_same_category and (desc_sim >= 0.3 or title_sim >= 0.4):
                return rec

            # Condition 3: Exact range match with moderate description or title similarity
            if exact_range_match and (desc_sim >= 0.35 or title_sim >= 0.45):
                return rec

            # Condition 4: Disproven syntax error on the exact range
            if rec.rejection_type == "SyntaxClaimValidationError" and exact_range_match:
                if cand_cat == "syntax_error" or "syntax" in cand_title_tokens or "syntax" in cand_desc_tokens:
                    return rec

            # Condition 5: Inconsistent or insufficient evidence on exact range
            if rec.rejection_type in {"InsufficientEvidenceError", "UnrelatedEvidenceError"} and exact_range_match:
                if is_same_category or desc_sim >= 0.25:
                    return rec

            # Condition 6: Contradiction on overlapping range (e.g. dependency or implementation contradiction)
            if rec.rejection_type == "ContradictionValidationError" and has_range_overlap:
                if is_same_category or desc_sim >= 0.25 or title_sim >= 0.3:
                    return rec

            # Condition 7: Test assertion misinterpretation on test file
            if rec.rejection_type == "TestAssertionMisinterpretationError" and has_range_overlap:
                return rec

            # Condition 8: Severity justification rejection on overlapping range
            if rec.rejection_type == "SeverityJustificationError" and has_range_overlap:
                if desc_sim >= 0.3 or title_sim >= 0.35:
                    return rec

        return None



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
# Phase 7: Evidence-Driven Investigation & Contradiction Validators
# ---------------------------------------------------------------------------

def validate_dependency_claim(
    workspace_path: Path,
    title: str,
    description: str,
    defect_category: Optional[str],
) -> None:
    """Validate dependency-related claims against workspace manifests (pyproject.toml, requirements.txt, etc.).

    Detects false claims such as alleging an outdated dependency or unsupported version
    when project manifests explicitly require the current version.
    """
    text = f"{title} {description}".lower()

    # Keywords indicating dependency version or compatibility claims
    dep_keywords = [
        "outdated", "deprecated", "incompatible", "version", "pydantic",
        "dependency", "upgrade", "downgrade", "v1", "v2", "unsupported",
    ]
    if not any(kw in text for kw in dep_keywords):
        return

    from forge.analysis.dependency_manifest import parse_workspace_dependencies
    manifest = parse_workspace_dependencies(workspace_path)

    # Pydantic-specific check (FastAPI failure case)
    if "pydantic" in text:
        pydantic_constraint = manifest.get_constraint("pydantic")
        if pydantic_constraint:
            is_alleging_pydantic_v1_or_outdated = any(
                phrase in text for phrase in [
                    "outdated pydantic",
                    "pydantic v1",
                    "pydantic 1",
                    "invalid pydantic v2",
                    "not compatible with pydantic v2",
                    "incompatible with pydantic v2",
                    "pydantic v2 support",
                    "pydantic version is outdated",
                    "pydantic version",
                ]
            )
            has_pydantic_v2_manifest = any(
                token in pydantic_constraint
                for token in [">=2", ">= 2", "^2", "~=2", "==2", "2."]
            )
            if is_alleging_pydantic_v1_or_outdated and has_pydantic_v2_manifest:
                raise ContradictionValidationError(
                    f"Contradiction detected: Project manifest specifies 'pydantic{pydantic_constraint}', "
                    f"which contradicts the claim that an outdated Pydantic version is used or that Pydantic v2 is unsupported."
                )

    # General declared dependency contradiction checks
    for pkg_name, constraint in manifest.dependencies.items():
        if pkg_name in text:
            if f"missing {pkg_name}" in text or f"no {pkg_name}" in text:
                raise ContradictionValidationError(
                    f"Contradiction detected: Package '{pkg_name}' is declared in project dependencies ('{constraint}')."
                )


def validate_test_file_claim(
    workspace_path: Path,
    norm_path: str,
    slice_lines: List[str],
    full_content: str,
    start_line: int,
    end_line: int,
    title: str,
    description: str,
    defect_category: Optional[str],
) -> None:
    """Validate claims made against test files.

    Prevents:
    1. Misinterpreting negative test assertions (e.g. pytest.raises(ValueError) on invalid input) as production bugs.
    2. Using test function names (e.g. test_upload_file_invalid_pydantic_v2) as proof of a defect.
    3. Claiming security vulnerabilities based on negative test cases.
    """
    path_lower = norm_path.lower()
    is_test_file = (
        "tests/" in path_lower
        or "test/" in path_lower
        or path_lower.startswith("test_")
        or "/test_" in path_lower
        or path_lower.endswith("_test.py")
    )
    if not is_test_file:
        return

    text = f"{title} {description}".lower()
    excerpt_text = "\n".join(slice_lines).lower()

    # Check 1: Negative test assertions checking error handling
    negative_assertion_markers = [
        "pytest.raises",
        "assertraises",
        "with pytest.raises",
        "pytest.warns",
    ]
    has_negative_assertion = any(m in excerpt_text for m in negative_assertion_markers)

    # Does the claim allege a bug because error/invalid behavior was tested?
    alleges_error_handling_defect = any(
        kw in text for kw in [
            "invalid", "valueerror", "raises error", "exception", "failed",
            "pydantic v2", "rejects", "fails to", "upload file invalid",
        ]
    )

    if has_negative_assertion and alleges_error_handling_defect:
        # Check if the test is expecting an exception for invalid input
        if "raises" in excerpt_text and ("invalid" in excerpt_text or "valueerror" in excerpt_text):
            raise TestAssertionMisinterpretationError(
                f"Test assertion misinterpretation at '{norm_path}:{start_line}-{end_line}': "
                f"The cited test explicitly asserts expected error handling (expecting exception on invalid input). "
                f"Verifying that invalid input is rejected is correct validation behavior, not a defect in production code."
            )

    # Check 2: Treating test function name as proof of defect
    for line in slice_lines:
        line_stripped = line.strip()
        if line_stripped.startswith("def test_"):
            func_name = line_stripped.split("def ", 1)[1].split("(", 1)[0].strip()
            title_words = set(re.findall(r"[a-zA-Z0-9]+", title.lower()))
            func_tokens = [tok for tok in re.findall(r"[a-zA-Z0-9]+", func_name.lower()) if tok != "test"]
            func_words = set(func_tokens)
            if func_words and (len(func_words & title_words) / len(func_words)) >= 0.7:
                if "traceback" not in excerpt_text and "assert false" not in excerpt_text:
                    raise TestAssertionMisinterpretationError(
                        f"Test name false positive: Finding title ('{title}') derives from test function '{func_name}'. "
                        f"A test function name is not evidence of a defect. Cite the production implementation demonstrating the defect."
                    )


def validate_implementation_contradiction(
    workspace_path: Path,
    norm_path: str,
    title: str,
    description: str,
) -> None:
    """Validate claims alleging missing validation against the actual implementation."""
    text = f"{title} {description}".lower()

    is_validation_claim = any(
        phrase in text for phrase in [
            "missing validation",
            "does not validate",
            "fails to validate",
            "accepts invalid",
            "no validation",
            "unvalidated",
            "invalid input",
            "upload file invalid",
        ]
    )
    if not is_validation_claim:
        return

    from forge.analysis.dependency_manifest import find_related_workspace_files
    related = find_related_workspace_files(workspace_path, norm_path)
    impl_files = related.get("implementations", [])

    for impl_rel in impl_files:
        impl_path = workspace_path / impl_rel
        if impl_path.is_file():
            try:
                impl_content = impl_path.read_text(encoding="utf-8", errors="replace")
                if "_validate" in impl_content and "raise ValueError" in impl_content:
                    raise ContradictionValidationError(
                        f"Contradiction detected: Implementation at '{impl_rel}' explicitly implements input validation "
                        f"raising ValueError on invalid input, contradicting the claim that validation is missing or that invalid input is accepted."
                    )
            except Exception:
                pass


def validate_severity_and_impact(
    severity: FindingSeverity,
    confidence: FindingConfidence,
    defect_category: Optional[str],
    title: str,
    description: str,
    slice_lines: List[str],
    norm_path: str,
) -> Tuple[FindingSeverity, FindingConfidence, Optional[str]]:
    """Verify that HIGH/CRITICAL severities have concrete impact justifications.

    Returns:
        (adjusted_severity, adjusted_confidence, limitations_addition)
    """
    text = f"{title} {description}".lower()
    cat = canonicalize_category(defect_category)
    adjusted_sev = severity
    limitations_notes: List[str] = []

    # Rule 1: High/Critical severity justification
    if severity in (FindingSeverity.HIGH, FindingSeverity.CRITICAL):
        severe_impact_markers = [
            "remote code execution", "rce", "arbitrary code execution",
            "sql injection", "sqli", "command injection",
            "privilege escalation", "authentication bypass", "auth bypass",
            "arbitrary file read", "arbitrary file write", "path traversal",
            "denial of service", "dos", "memory corruption", "data loss",
            "unhandled crash", "infinite recursion", "infinite loop",
            "syntax error", "parse error", "missing colon",
            "out of bounds", "index error", "buffer overflow",
            "resource leak", "memory leak", "deadlock",
        ]
        has_severe_impact = any(marker in text for marker in severe_impact_markers)

        concrete_failure_indicators = [
            "crash", "corrupt", "leak", "bypass", "failure", "breaks",
            "exception", "unhandled", "error", "traceback", "index",
            "bounds", "array", "loop", "syntax", "colon", "zero division",
            "null", "overflow", "hang", "recursion", "typeerror", "valueerror",
        ]
        has_concrete_failure = (
            has_severe_impact
            or any(ind in text for ind in concrete_failure_indicators)
            or cat in ("syntax_error", "loop", "resource_leak", "boundary_condition")
        )

        speculative_phrases = [
            "may introduce security vulnerabilities",
            "potential security vulnerability",
            "might be vulnerable",
            "could introduce security",
            "potential vulnerability",
            "possible security risk",
            "could lead to vulnerability",
            "security risk",
            "may be vulnerable",
            "outdated pydantic",
        ]
        is_speculative = any(phrase in text for phrase in speculative_phrases)

        is_test_file = (
            "tests/" in norm_path.lower()
            or "test/" in norm_path.lower()
            or norm_path.lower().startswith("test_")
            or "/test_" in norm_path.lower()
            or norm_path.lower().endswith("_test.py")
        )

        if is_speculative:
            raise SeverityJustificationError(
                f"Unjustified {severity.value.upper()} severity: Speculative security claim ('{description[:80]}...') "
                f"lacks demonstrable exploitability, attack vector, or concrete severe impact."
            )

        if is_test_file and not has_severe_impact:
            raise SeverityJustificationError(
                f"Unjustified {severity.value.upper()} severity: Test files cannot support high or critical vulnerability findings "
                f"without demonstrable production exploitability."
            )

        is_security_claim = (
            cat == "security"
            or "security" in text
            or "vulnerability" in text
            or "vulnerable" in text
            or "cve" in text
        )

        if is_security_claim and not has_severe_impact:
            adjusted_sev = FindingSeverity.MEDIUM
            limitations_notes.append(
                f"Severity adjusted from '{severity.value}' to 'medium': Security claim lacks demonstrable exploitability or concrete severe operational impact."
            )

    # Rule 2: Confidence calibration
    adjusted_conf = confidence

    if confidence == FindingConfidence.CONFIRMED:
        adjusted_conf = FindingConfidence.HIGH
        limitations_notes.append("Confidence adjusted from 'confirmed' to 'high': Static inspection cannot confirm defects without isolated execution verification.")
    elif ("test" in norm_path.lower()) and confidence == FindingConfidence.HIGH:
        adjusted_conf = FindingConfidence.MEDIUM
        limitations_notes.append("Confidence adjusted to 'medium': Citing test code rather than production implementation increases uncertainty.")

    limitations_str = " ".join(limitations_notes) if limitations_notes else None
    return adjusted_sev, adjusted_conf, limitations_str


def validate_claim_evidence_relevance(
    slice_lines: List[str],
    defect_category: Optional[str],
    title: str,
    description: str,
) -> None:
    """Ensure that the cited evidence has direct semantic relevance to the finding description."""
    text = f"{title} {description}".lower()
    excerpt_text = "\n".join(slice_lines).lower()

    specific_defect_types = {
        "sql injection": ["sql", "select ", "insert ", "update ", "delete ", "query", "execute", "cursor", "db"],
        "buffer overflow": ["buffer", "overflow", "memcpy", "strcpy", "array", "pointer", "size"],
        "cross-site scripting": ["xss", "html", "script", "render", "escape", "template", "sanitize"],
        "resource leak": ["open(", "close(", "file", "socket", "connection", "handle", "fd", "stream", "acquire", "release"],
    }

    for defect_name, required_tokens in specific_defect_types.items():
        if defect_name in text:
            has_any = any(tok in excerpt_text for tok in required_tokens)
            if not has_any:
                raise UnrelatedEvidenceError(
                    f"Evidence line range cited for '{defect_name}' contains none of the expected constructs "
                    f"({', '.join(required_tokens[:5])}). The cited evidence is unrelated to the finding description."
                )


# ---------------------------------------------------------------------------
# Duplicate Finding Detection
# ---------------------------------------------------------------------------

def check_duplicate_finding(
    store: Optional[FindingEvidenceProvider] = None,
    run_id: Optional[str] = None,
    norm_path: str = "",
    start_line: int = 1,
    end_line: int = 1,
    defect_category: Optional[str] = None,
    title: str = "",
    description: str = "",
    content_digest: str = "",
    *,
    existing_findings: Optional[Sequence[Finding]] = None,
    evidence_by_finding: Optional[Dict[str, List[EvidenceRecord]]] = None,
    finding_provider: Optional[FindingEvidenceProvider] = None,
) -> Optional[Tuple[Finding, bool]]:
    """Detect whether a candidate finding is a duplicate of an existing finding in this run.

    Findings and evidence can be supplied directly via `existing_findings` and
    `evidence_by_finding`, or queried dynamically via `finding_provider` / `store`.

    Returns:
        (existing_finding, is_identical_evidence) or None if not a duplicate.
    """
    provider = finding_provider or store

    if existing_findings is None:
        if not provider or not run_id:
            return None
        try:
            findings_list = provider.list_findings(run_id=run_id)
        except Exception:
            return None
    else:
        findings_list = list(existing_findings)

    if not findings_list:
        return None

    cand_tokens = normalize_tokens(description)
    cand_title_tokens = normalize_tokens(title)
    cand_cat = canonicalize_category(defect_category)

    for ex in findings_list:
        if norm_path not in ex.affected_files:
            continue

        if evidence_by_finding is not None and ex.finding_id in evidence_by_finding:
            ex_evidence = evidence_by_finding[ex.finding_id]
        elif provider:
            try:
                ex_evidence = provider.list_evidence_for_finding(ex.finding_id)
            except Exception:
                ex_evidence = []
        else:
            ex_evidence = []

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
    store: Optional[FindingEvidenceProvider] = None,
    run_id: Optional[str] = None,
    *,
    severity: Optional[FindingSeverity] = None,
    finding_provider: Optional[FindingEvidenceProvider] = None,
    existing_findings: Optional[Sequence[Finding]] = None,
    evidence_by_finding: Optional[Dict[str, List[EvidenceRecord]]] = None,
    rejected_tracker: Optional[RejectedClaimTracker] = None,
) -> ValidationOutcome:
    """Perform deterministic claim validation, static syntax checks, and deduplication.

    Raises:
        SyntaxClaimValidationError: If a claimed syntax error is disproven by static analysis.
        ConsistencyValidationError: If category, title, description, and code are contradictory.
        ContradictionValidationError: If claim contradicts dependency manifests or code implementations.
        TestAssertionMisinterpretationError: If test assertions are misinterpreted as production bugs.
        SeverityJustificationError: If high/critical severity lacks demonstrated impact.
        UnrelatedEvidenceError: If cited code is unrelated to the claimed defect.
        InsufficientEvidenceError: If evidence context does not support the defect claim.
        DuplicateFindingError: If finding is an exact duplicate of an existing finding.
        RepeatedRejectedClaimError: If finding matches a previously rejected claim in this run.
    """
    # Step 0: Check per-run rejected claim tracker
    if rejected_tracker:
        prior_rejection = rejected_tracker.check_repeated_rejection(
            norm_path=norm_path,
            start_line=start_line,
            end_line=end_line,
            defect_category=defect_category,
            title=title,
            description=description,
        )
        if prior_rejection:
            raise RepeatedRejectedClaimError(
                f"Repeated rejected finding submission: A claim on '{norm_path}:{start_line}-{end_line}' "
                f"was already rejected ({prior_rejection.rejection_reason}).",
                affected_file=norm_path,
                start_line=start_line,
                end_line=end_line,
                defect_category=defect_category,
                rejection_type="RepeatedRejectedClaimError",
            )

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

    # Step 5: Contradiction checks against dependencies
    validate_dependency_claim(
        workspace_path=workspace_path,
        title=title,
        description=description,
        defect_category=defect_category,
    )

    # Step 6: Test file assertion misinterpretation checks
    validate_test_file_claim(
        workspace_path=workspace_path,
        norm_path=norm_path,
        slice_lines=slice_lines,
        full_content=full_content,
        start_line=start_line,
        end_line=end_line,
        title=title,
        description=description,
        defect_category=defect_category,
    )

    # Step 7: Implementation contradiction checks
    validate_implementation_contradiction(
        workspace_path=workspace_path,
        norm_path=norm_path,
        title=title,
        description=description,
    )

    # Step 8: Claim-to-evidence relevance checks
    validate_claim_evidence_relevance(
        slice_lines=slice_lines,
        defect_category=defect_category,
        title=title,
        description=description,
    )

    # Step 9: Severity & impact justification, confidence calibration
    sev_to_check = severity or FindingSeverity.MEDIUM
    adj_sev, adj_conf, sev_limitations = validate_severity_and_impact(
        severity=sev_to_check,
        confidence=confidence,
        defect_category=defect_category,
        title=title,
        description=description,
        slice_lines=slice_lines,
        norm_path=norm_path,
    )

    # Step 10: Duplicate finding detection
    provider = finding_provider or store
    if (provider and run_id) or (existing_findings is not None):
        dup_match = check_duplicate_finding(
            store=provider,
            run_id=run_id or "",
            norm_path=norm_path,
            start_line=start_line,
            end_line=end_line,
            defect_category=defect_category,
            title=title,
            description=description,
            content_digest=content_digest,
            existing_findings=existing_findings,
            evidence_by_finding=evidence_by_finding,
            finding_provider=provider,
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

    base_limitations = "Semantic correctness could not be established deterministically without an isolated execution reproduction."
    if sev_limitations:
        limitations = f"{sev_limitations} {base_limitations}".strip()
    else:
        limitations = base_limitations

    return ValidationOutcome(
        is_valid=True,
        is_duplicate=False,
        action="accepted",
        adjusted_confidence=adj_conf,
        adjusted_severity=adj_sev,
        limitations=limitations,
    )
