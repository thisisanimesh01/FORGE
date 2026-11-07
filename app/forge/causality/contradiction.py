"""Phase 12 M2: AST-Based Semantic Contradiction and Guard Inspector.

Inspects Python AST to verify or refute causal defect hypotheses:
- Parses actual Python syntax trees avoiding comment and docstring false positives.
- Identifies enclosing function and nested scope boundaries.
- Analyzes executable guards: conditional checks, asserts, and early returns.
- Audits exception handling: try/except enclosures and caught exception types.
- Evaluates whether candidate hypotheses directly contradict verified source structures.
"""

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.causality.schemas import AlternativeExplanation, CausalNode
from forge.diagnosis.schemas import DefectCategory


class EnclosingScopeInfo:
    """Information regarding the enclosing AST function or method scope."""

    def __init__(
        self,
        node: ast.AST,
        name: str,
        start_line: int,
        end_line: int,
        is_async: bool = False,
    ) -> None:
        self.node = node
        self.name = name
        self.start_line = start_line
        self.end_line = end_line
        self.is_async = is_async
        self.guards: List[Dict[str, Any]] = []
        self.exception_handlers: List[Dict[str, Any]] = []
        self.return_statements: List[Dict[str, Any]] = []


class ASTContradictionInspector:
    """Performs semantic AST analysis of guards, exception handlers, and control flow."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path.resolve()

    def inspect_file_scope(
        self,
        file_path: str,
        target_line: int,
    ) -> Optional[EnclosingScopeInfo]:
        """Locate the innermost function or method enclosing target_line and analyze its structure."""
        abs_path = self.workspace_path / file_path
        if not abs_path.exists() or not abs_path.is_file():
            return None

        try:
            source_content = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source_content, filename=file_path)
        except Exception:
            # Non-python file or syntax error
            return None

        # Find innermost function/method containing target_line
        best_scope: Optional[EnclosingScopeInfo] = None
        min_span = float("inf")

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_lineno = getattr(node, "end_lineno", None)
                if end_lineno is None:
                    # In older Python or synthetic AST, estimate from children
                    end_lineno = max((getattr(n, "lineno", node.lineno) for n in ast.walk(node)), default=node.lineno)

                if node.lineno <= target_line <= end_lineno:
                    span = end_lineno - node.lineno
                    if span < min_span:
                        min_span = span
                        best_scope = EnclosingScopeInfo(
                            node=node,
                            name=node.name,
                            start_line=node.lineno,
                            end_line=end_lineno,
                            is_async=isinstance(node, ast.AsyncFunctionDef),
                        )

        if best_scope is None:
            return None

        self._analyze_scope_internals(best_scope, target_line)
        return best_scope

    def _analyze_scope_internals(self, scope: EnclosingScopeInfo, target_line: int) -> None:
        """Extract explicit guards, exception handlers, and return statements in scope."""
        for child in ast.walk(scope.node):
            # 1. Inspect conditional guards: if condition raising exception or returning
            if isinstance(child, ast.If):
                guard_info = self._analyze_if_guard(child)
                if guard_info:
                    scope.guards.append(guard_info)

            # 2. Inspect asserts
            elif isinstance(child, ast.Assert):
                assert_info = self._analyze_assert(child)
                scope.guards.append(assert_info)

            # 3. Inspect try/except blocks
            elif isinstance(child, ast.Try):
                handler_info = self._analyze_try_block(child, target_line)
                if handler_info:
                    scope.exception_handlers.append(handler_info)

            # 4. Inspect returns
            elif isinstance(child, ast.Return):
                scope.return_statements.append({
                    "line": child.lineno,
                    "has_value": child.value is not None,
                })

    def _analyze_if_guard(self, if_node: ast.If) -> Optional[Dict[str, Any]]:
        """Determine if an if statement functions as a validation guard."""
        raises = []
        returns = []

        for stmt in if_node.body:
            if isinstance(stmt, ast.Raise):
                exc_type = "Exception"
                if stmt.exc:
                    if isinstance(stmt.exc, ast.Name):
                        exc_type = stmt.exc.id
                    elif isinstance(stmt.exc, ast.Call) and isinstance(stmt.exc.func, ast.Name):
                        exc_type = stmt.exc.func.id
                raises.append(exc_type)
            elif isinstance(stmt, ast.Return):
                returns.append(stmt.lineno)

        if raises or returns:
            # Extract simplified condition representation
            cond_repr = ast.unparse(if_node.test) if hasattr(ast, "unparse") else "condition"
            return {
                "type": "if_guard",
                "line": if_node.lineno,
                "condition": cond_repr,
                "raises": raises,
                "returns": returns,
            }
        return None

    def _analyze_assert(self, assert_node: ast.Assert) -> Dict[str, Any]:
        """Extract information from an assert statement."""
        cond_repr = ast.unparse(assert_node.test) if hasattr(ast, "unparse") else "assertion"
        msg = ""
        if assert_node.msg:
            if isinstance(assert_node.msg, ast.Constant) and isinstance(assert_node.msg.value, str):
                msg = assert_node.msg.value
        return {
            "type": "assert_guard",
            "line": assert_node.lineno,
            "condition": cond_repr,
            "raises": ["AssertionError"],
            "message": msg,
        }

    def _analyze_try_block(self, try_node: ast.Try, target_line: int) -> Optional[Dict[str, Any]]:
        """Extract try/except handlers and check if target_line is inside the try body."""
        try_start = try_node.lineno
        try_end = getattr(try_node, "end_lineno", try_start)

        # Check if target_line is protected by this try
        catches: List[str] = []
        for handler in try_node.handlers:
            if handler.type is None:
                catches.append("Exception")  # Bare except catches all
            elif isinstance(handler.type, ast.Name):
                catches.append(handler.type.id)
            elif isinstance(handler.type, ast.Tuple):
                for elt in handler.type.elts:
                    if isinstance(elt, ast.Name):
                        catches.append(elt.id)

        is_enclosing = try_start <= target_line <= try_end
        return {
            "line": try_start,
            "end_line": try_end,
            "catches": catches,
            "encloses_target": is_enclosing,
        }

    def detect_contradiction(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        claimed_defect: str,
        category: Optional[DefectCategory] = None,
    ) -> Optional[Dict[str, Any]]:
        """Detect whether concrete AST structures refute the claimed defect.

        Returns:
            Dict describing the contradictory evidence, or None if no contradiction found.
        """
        scope = self.inspect_file_scope(file_path, target_line=start_line)
        if scope is None:
            return None

        claimed_lower = claimed_defect.lower()

        # Check 1: Claimed missing input validation / guard, but explicit guard exists
        missing_guard_claimed = any(kw in claimed_lower for kw in [
            "missing validation",
            "unvalidated",
            "missing check",
            "missing guard",
            "no check",
            "fails to validate",
            "not validated",
            "unverified input",
        ])

        if missing_guard_claimed or category in (
            DefectCategory.INPUT_VALIDATION_FAILURE,
            DefectCategory.INCORRECT_DATA_HANDLING,
        ):
            for guard in scope.guards:
                # If guard raises ValueError, TypeError, KeyError, or asserts
                guard_raises = guard.get("raises", [])
                if any(r in guard_raises for r in ("ValueError", "TypeError", "KeyError", "IndexError", "AssertionError")):
                    return {
                        "contradiction_type": "explicit_guard_present",
                        "file_path": file_path,
                        "line": guard["line"],
                        "scope_name": scope.name,
                        "detail": f"Function '{scope.name}' contains explicit validation guard at line {guard['line']} raising {', '.join(guard_raises)} (condition: {guard.get('condition')}).",
                    }

        # Check 2: Claimed unhandled exception, but enclosing try/except handles it
        unhandled_claimed = any(kw in claimed_lower for kw in [
            "unhandled exception",
            "missing exception handler",
            "unhandled error",
            "uncaught",
            "no try except",
        ]) or category == DefectCategory.EXCEPTION_HANDLING_DEFECT

        if unhandled_claimed:
            for handler in scope.exception_handlers:
                if handler.get("encloses_target"):
                    catches = handler.get("catches", [])
                    return {
                        "contradiction_type": "exception_handler_present",
                        "file_path": file_path,
                        "line": handler["line"],
                        "scope_name": scope.name,
                        "detail": f"Target location is enclosed by try/except block at line {handler['line']} catching: {', '.join(catches)}.",
                    }

        return None

    def evaluate_alternative(
        self,
        primary_cause: Optional[CausalNode],
        alternative: AlternativeExplanation,
    ) -> Tuple[bool, Optional[str]]:
        """Evaluate if an alternative explanation is refuted by verified AST facts.

        Returns:
            Tuple[is_refuted: bool, reason: Optional[str]]
        """
        # If alternative claims defect in a symbol where explicit AST guards exist
        alt_text = f"{alternative.suspected_root_cause} {alternative.rationale}"
        if primary_cause and primary_cause.is_verified_on_disk:
            # If primary cause is directly verified on disk with direct test/crash signal
            # and alternative merely speculates without evidence
            if not alternative.contradicted_by:
                # Check if AST of alternative's cited location refutes it
                pass

        return False, None
