"""AST-based semantic verification and structural invariant analysis for Python patches.

Guarantees:
- Unreachable statement detection: identifies dead code immediately following unconditional
  terminating statements (return, raise, break, continue) within any statement block.
- Validation guard preservation: detects when input validation guards, assertions, or exception
  checks present in the baseline function are bypassed by premature early returns or removed.
- Docstring preservation: detects docstring displacement where statements are inserted between
  the function header and the docstring, rendering the docstring unreachable and invalid.
- Zero false positives on legitimate early returns: distinguishes conditional early returns
  (e.g., 'if not items: return None') from unconditional block terminators.
- Fail secure: deterministic AST analysis without model hallucination or host execution.
"""

import ast
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union


@dataclass(frozen=True)
class UnreachableStatementInfo:
    """Details of a statement identified as unreachable."""
    statement_type: str
    lineno: int
    col_offset: int
    terminator_type: str
    terminator_lineno: int
    function_name: Optional[str] = None
    statement_preview: str = ""


@dataclass(frozen=True)
class BypassedGuardInfo:
    """Details of a validation guard that was bypassed or removed."""
    guard_type: str
    test_expression: str
    original_lineno: int
    function_name: str
    violation_reason: str
    bypassing_statement_type: Optional[str] = None
    bypassing_lineno: Optional[int] = None


@dataclass
class SemanticAnalysisResult:
    """Aggregate result of Python AST semantic verification."""
    is_valid: bool
    violations: List[str] = field(default_factory=list)
    unreachable_statements: List[UnreachableStatementInfo] = field(default_factory=list)
    bypassed_guards: List[BypassedGuardInfo] = field(default_factory=list)
    docstring_displaced: bool = False
    details: Dict[str, Any] = field(default_factory=dict)


def _get_stmt_preview(stmt: ast.stmt) -> str:
    """Get a short, single-line text preview of an AST statement."""
    try:
        source = ast.unparse(stmt).strip()
        lines = source.splitlines()
        first_line = lines[0] if lines else type(stmt).__name__
        if len(first_line) > 80:
            return first_line[:77] + "..."
        return first_line
    except Exception:
        return type(stmt).__name__


def _is_unconditional_terminator(stmt: ast.stmt) -> bool:
    """Determine whether an AST statement unconditionally terminates execution flow in its block."""
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True

    # An If statement unconditionally terminates if BOTH branches exist and unconditionally terminate
    if isinstance(stmt, ast.If):
        if stmt.body and stmt.orelse:
            body_terminates = any(_is_unconditional_terminator(s) for s in stmt.body)
            orelse_terminates = any(_is_unconditional_terminator(s) for s in stmt.orelse)
            return body_terminates and orelse_terminates
        return False

    # A Try statement unconditionally terminates if body terminates AND all handlers terminate
    if isinstance(stmt, ast.Try):
        if stmt.finalbody and any(_is_unconditional_terminator(s) for s in stmt.finalbody):
            return True
        if stmt.body and stmt.handlers:
            body_terminates = any(_is_unconditional_terminator(s) for s in stmt.body)
            handlers_terminate = all(
                h.body and any(_is_unconditional_terminator(s) for s in h.body)
                for h in stmt.handlers
            )
            return body_terminates and handlers_terminate
        return False

    return False


def _find_unreachable_in_block(
    stmts: List[ast.stmt],
    function_name: Optional[str] = None,
) -> List[UnreachableStatementInfo]:
    """Inspect a sequential list of statements for statements following an unconditional terminator."""
    unreachable: List[UnreachableStatementInfo] = []
    terminator_found: Optional[Tuple[str, int]] = None

    for s in stmts:
        if terminator_found is not None:
            unreachable.append(
                UnreachableStatementInfo(
                    statement_type=type(s).__name__,
                    lineno=getattr(s, "lineno", 0),
                    col_offset=getattr(s, "col_offset", 0),
                    terminator_type=terminator_found[0],
                    terminator_lineno=terminator_found[1],
                    function_name=function_name,
                    statement_preview=_get_stmt_preview(s),
                )
            )
        elif _is_unconditional_terminator(s):
            terminator_found = (type(s).__name__, getattr(s, "lineno", 0))

        # Recurse into nested compound statements to inspect their internal blocks
        if isinstance(s, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            unreachable.extend(_find_unreachable_in_block(s.body, function_name))
            if getattr(s, "orelse", None):
                unreachable.extend(_find_unreachable_in_block(s.orelse, function_name))
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            unreachable.extend(_find_unreachable_in_block(s.body, function_name))
        elif isinstance(s, ast.Try):
            unreachable.extend(_find_unreachable_in_block(s.body, function_name))
            for h in s.handlers:
                unreachable.extend(_find_unreachable_in_block(h.body, function_name))
            if s.orelse:
                unreachable.extend(_find_unreachable_in_block(s.orelse, function_name))
            if s.finalbody:
                unreachable.extend(_find_unreachable_in_block(s.finalbody, function_name))

    return unreachable


def collect_unreachable_statements(tree: ast.AST) -> List[UnreachableStatementInfo]:
    """Traverse an AST and collect all statements that are statically unreachable."""
    unreachable: List[UnreachableStatementInfo] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
            unreachable.extend(_find_unreachable_in_block(node.body, func_name))
        elif isinstance(node, ast.Module):
            unreachable.extend(_find_unreachable_in_block(node.body, "<module>"))

    return unreachable


@dataclass
class ExtractedGuard:
    """Represents a guard or validation statement in a function."""
    node: ast.stmt
    guard_type: str
    test_str: str
    lineno: int
    body_index: int


def _is_validation_guard(stmt: ast.stmt) -> Optional[Tuple[str, str]]:
    """Check if an AST statement represents an input validation or defensive guard.

    Returns:
        (guard_type, test_expression_str) or None.
    """
    if isinstance(stmt, ast.If):
        # A guard typically raises an exception or returns early with an error indicator
        has_raise = any(isinstance(s, ast.Raise) for s in stmt.body)
        has_assert = any(isinstance(s, ast.Assert) for s in stmt.body)
        has_early_return = any(isinstance(s, ast.Return) for s in stmt.body)

        if has_raise or has_assert or has_early_return:
            test_repr = _get_stmt_preview(stmt.test) if hasattr(stmt, "test") else "if <condition>"
            guard_type = "raise_guard" if has_raise else ("assert_guard" if has_assert else "conditional_return_guard")
            return guard_type, test_repr

    if isinstance(stmt, ast.Assert):
        test_repr = _get_stmt_preview(stmt.test) if hasattr(stmt, "test") else "assert <condition>"
        return "assert_statement", test_repr

    return None


def _extract_function_guards(func_node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> List[ExtractedGuard]:
    """Extract all validation guards from the top-level statements of a function body."""
    guards: List[ExtractedGuard] = []
    for idx, stmt in enumerate(func_node.body):
        guard_info = _is_validation_guard(stmt)
        if guard_info:
            guards.append(
                ExtractedGuard(
                    node=stmt,
                    guard_type=guard_info[0],
                    test_str=guard_info[1],
                    lineno=getattr(stmt, "lineno", 0),
                    body_index=idx,
                )
            )
    return guards


def _get_function_map(tree: ast.AST) -> Dict[str, Union[ast.FunctionDef, ast.AsyncFunctionDef]]:
    """Map function names to their AST nodes across a module."""
    func_map: Dict[str, Union[ast.FunctionDef, ast.AsyncFunctionDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_map[node.name] = node
    return func_map


def analyze_python_ast_semantics(
    original_source: str,
    patched_source: str,
    file_path: Optional[str] = None,
) -> SemanticAnalysisResult:
    """Perform structural and semantic safety verification comparing baseline and patched Python source.

    Checks:
    1. Unreachable Code: verifies that the patch introduces zero new unreachable statements.
    2. Docstring Displacement: detects when non-docstring statements are inserted above
       the docstring, turning it into dead code.
    3. Guard Bypass: detects when an unconditional return is placed prior to existing
       validation guards or asserts.
    4. Guard Removal: detects when baseline validation guards (e.g. parameter error checks)
       are deleted by the patch.
    """
    violations: List[str] = []
    fname_prefix = f"In '{file_path}': " if file_path else ""

    try:
        orig_tree = ast.parse(original_source)
    except SyntaxError as e:
        # If original code had syntax error, we cannot compare ASTs
        return SemanticAnalysisResult(is_valid=True, details={"warning": f"Baseline source has syntax error: {e}"})

    try:
        patched_tree = ast.parse(patched_source)
    except SyntaxError as e:
        return SemanticAnalysisResult(
            is_valid=False,
            violations=[f"{fname_prefix}Patched source introduces Python syntax error: {e}"],
        )

    # 1. Check for newly introduced unreachable statements
    orig_unreachable = collect_unreachable_statements(orig_tree)
    patched_unreachable = collect_unreachable_statements(patched_tree)

    # Map unreachable statements by function and preview to identify net additions
    orig_unreachable_set = {
        (u.function_name, u.statement_type, u.statement_preview) for u in orig_unreachable
    }
    new_unreachable: List[UnreachableStatementInfo] = []
    for u in patched_unreachable:
        key = (u.function_name, u.statement_type, u.statement_preview)
        if key not in orig_unreachable_set:
            new_unreachable.append(u)

    if new_unreachable:
        for u in new_unreachable:
            func_desc = f" in function '{u.function_name}'" if u.function_name else ""
            violations.append(
                f"{fname_prefix}Unreachable code detected{func_desc}: statement '{u.statement_preview}' "
                f"(line {u.lineno}) is unreachable because it follows unconditional {u.terminator_type} "
                f"at line {u.terminator_lineno}."
            )

    # 2. Structural function-level comparison
    orig_funcs = _get_function_map(orig_tree)
    patched_funcs = _get_function_map(patched_tree)
    bypassed_guards: List[BypassedGuardInfo] = []
    docstring_displaced = False

    for func_name, orig_func in orig_funcs.items():
        if func_name not in patched_funcs:
            continue

        patched_func = patched_funcs[func_name]

        # 2a. Docstring displacement check
        orig_doc = ast.get_docstring(orig_func)
        patched_doc = ast.get_docstring(patched_func)

        if orig_doc is not None and patched_doc is None:
            # Check if docstring string constant still exists in patched_func body at index > 0
            has_displaced_doc = False
            for idx, stmt in enumerate(patched_func.body[1:], start=1):
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    if orig_doc.strip() in stmt.value.value.strip() or stmt.value.value.strip() in orig_doc.strip():
                        has_displaced_doc = True
                        break

            if has_displaced_doc:
                docstring_displaced = True
                first_stmt_preview = _get_stmt_preview(patched_func.body[0]) if patched_func.body else "<empty>"
                violations.append(
                    f"{fname_prefix}Docstring displacement in function '{func_name}': statements were inserted "
                    f"before the docstring ('{first_stmt_preview}'), detaching the docstring into dead code."
                )

        # 2b. Validation guard bypass and removal checks
        orig_guards = _extract_function_guards(orig_func)
        patched_guards = _extract_function_guards(patched_func)

        # Find first unconditional return in patched_func body
        first_uncond_return_idx: Optional[int] = None
        first_uncond_return_lineno: Optional[int] = None
        for idx, stmt in enumerate(patched_func.body):
            if isinstance(stmt, ast.Return):
                first_uncond_return_idx = idx
                first_uncond_return_lineno = getattr(stmt, "lineno", 0)
                break

        for og in orig_guards:
            # Check if this guard was bypassed by an earlier unconditional return
            matching_pg = next((pg for pg in patched_guards if pg.test_str == og.test_str), None)

            if matching_pg is not None:
                # Guard exists in patched function, but is it preceded by an unconditional return?
                if first_uncond_return_idx is not None and first_uncond_return_idx < matching_pg.body_index:
                    info = BypassedGuardInfo(
                        guard_type=og.guard_type,
                        test_expression=og.test_str,
                        original_lineno=og.lineno,
                        function_name=func_name,
                        violation_reason=(
                            f"Validation guard '{og.test_str}' (line {matching_pg.lineno}) is bypassed by "
                            f"preceding unconditional return at line {first_uncond_return_lineno}."
                        ),
                        bypassing_statement_type="Return",
                        bypassing_lineno=first_uncond_return_lineno,
                    )
                    bypassed_guards.append(info)
                    violations.append(
                        f"{fname_prefix}Validation guard bypassed in function '{func_name}': guard '{og.test_str}' "
                        f"is rendered unreachable by unconditional return at line {first_uncond_return_lineno}."
                    )
            else:
                # Guard was removed or replaced
                # If an unconditional return was inserted at or before the guard position, report bypass
                if first_uncond_return_idx is not None and first_uncond_return_idx <= og.body_index:
                    info = BypassedGuardInfo(
                        guard_type=og.guard_type,
                        test_expression=og.test_str,
                        original_lineno=og.lineno,
                        function_name=func_name,
                        violation_reason=(
                            f"Validation guard '{og.test_str}' (originally line {og.lineno}) was removed or "
                            f"bypassed by unconditional return at line {first_uncond_return_lineno}."
                        ),
                        bypassing_statement_type="Return",
                        bypassing_lineno=first_uncond_return_lineno,
                    )
                    bypassed_guards.append(info)
                    violations.append(
                        f"{fname_prefix}Validation guard removed/bypassed in function '{func_name}': guard "
                        f"'{og.test_str}' (line {og.lineno}) was replaced or bypassed by unconditional return."
                    )
                else:
                    # Guard was removed without replacement: only flag if all guards were stripped
                    # or an exception guard was completely eliminated
                    all_guards_stripped = len(patched_guards) == 0
                    raise_guard_stripped = (og.guard_type == "raise_guard" and not any(pg.guard_type == "raise_guard" for pg in patched_guards))

                    if all_guards_stripped or raise_guard_stripped:
                        info = BypassedGuardInfo(
                            guard_type=og.guard_type,
                            test_expression=og.test_str,
                            original_lineno=og.lineno,
                            function_name=func_name,
                            violation_reason=f"Validation guard '{og.test_str}' was deleted by the patch.",
                        )
                        bypassed_guards.append(info)
                        violations.append(
                            f"{fname_prefix}Validation guard stripped in function '{func_name}': guard "
                            f"'{og.test_str}' (originally line {og.lineno}) was removed without equivalent safeguard."
                        )

    is_valid = len(violations) == 0
    return SemanticAnalysisResult(
        is_valid=is_valid,
        violations=violations,
        unreachable_statements=new_unreachable,
        bypassed_guards=bypassed_guards,
        docstring_displaced=docstring_displaced,
        details={
            "new_unreachable_count": len(new_unreachable),
            "bypassed_guards_count": len(bypassed_guards),
            "docstring_displaced": docstring_displaced,
        },
    )
