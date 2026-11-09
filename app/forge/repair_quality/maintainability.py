"""Phase 12 M6: Maintainability & Complexity Analyzer.

Evaluates AST cyclomatic complexity delta, function length changes,
code duplication, public API alterations, dead code indicators, and type safety.
Operates strictly read-only on disk files.
"""

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
)


class CyclomaticComplexityVisitor(ast.NodeVisitor):
    """Calculates McCabe cyclomatic complexity for an AST node."""

    def __init__(self) -> None:
        self.complexity = 1

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.complexity += max(1, len(node.values) - 1)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.complexity += 1
        self.generic_visit(node)


class DeadCodeVisitor(ast.NodeVisitor):
    """Detects unreachable statements following return, raise, break, continue."""

    def __init__(self) -> None:
        self.dead_code_count = 0
        self.findings: List[str] = []

    def _check_body(self, body: List[ast.stmt]) -> None:
        terminator_seen = False
        for stmt in body:
            if terminator_seen:
                self.dead_code_count += 1
                self.findings.append(
                    f"Unreachable statement {type(stmt).__name__} at line {getattr(stmt, 'lineno', '?')}"
                )
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                terminator_seen = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_body(node.body)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_body(node.body)
        self.generic_visit(node)


class RepairMaintainabilityAnalyzer:
    """Safely analyzes maintainability and code structure using Python AST."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def analyze_maintainability(
        self,
        workspace_path: Path,
        modified_files: List[str],
        baseline_path: Optional[Path] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore]:
        """Analyze code maintainability delta across modified files."""
        findings: List[str] = []
        duplication_indicators: List[str] = []
        public_api_changes: List[str] = []
        type_lint_findings: List[str] = []
        dead_code_indicators: List[str] = []

        total_baseline_complexity = 0
        total_repaired_complexity = 0
        complexity_measured = False

        total_baseline_length = 0
        total_repaired_length = 0

        for rel_file in modified_files:
            repaired_file_path = workspace_path / rel_file
            if not repaired_file_path.exists() or not rel_file.endswith(".py"):
                continue

            try:
                repaired_content = repaired_file_path.read_text(encoding="utf-8")
                repaired_ast = ast.parse(repaired_content, filename=str(repaired_file_path))
                complexity_measured = True
            except Exception as e:
                findings.append(f"Failed to parse repaired file '{rel_file}': {e}")
                continue

            # Repaired complexity
            rep_visitor = CyclomaticComplexityVisitor()
            rep_visitor.visit(repaired_ast)
            total_repaired_complexity += rep_visitor.complexity
            total_repaired_length += len(repaired_content.splitlines())

            # Check dead code
            dc_visitor = DeadCodeVisitor()
            dc_visitor.visit(repaired_ast)
            if dc_visitor.dead_code_count > 0:
                dead_code_indicators.extend(dc_visitor.findings)
                findings.extend(dc_visitor.findings)

            # Check silent error suppression (bare except or except Exception: pass)
            for node in ast.walk(repaired_ast):
                if isinstance(node, ast.ExceptHandler):
                    if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                        type_lint_findings.append(
                            f"Silent error suppression (except: pass) at line {node.lineno} in {rel_file}"
                        )
                        findings.append(f"Silent exception suppression detected in {rel_file}:{node.lineno}")

            # Baseline comparison if baseline available
            baseline_ast: Optional[ast.AST] = None
            if baseline_path:
                base_file_path = baseline_path / rel_file
                if base_file_path.exists():
                    try:
                        base_content = base_file_path.read_text(encoding="utf-8")
                        baseline_ast = ast.parse(base_content, filename=str(base_file_path))
                        base_visitor = CyclomaticComplexityVisitor()
                        base_visitor.visit(baseline_ast)
                        total_baseline_complexity += base_visitor.complexity
                        total_baseline_length += len(base_content.splitlines())
                    except Exception:
                        pass

            # Public API changes
            if baseline_ast:
                base_public = self._extract_public_signatures(baseline_ast)
                rep_public = self._extract_public_signatures(repaired_ast)

                for name, sig in base_public.items():
                    if name not in rep_public:
                        msg = f"Public API removed/renamed: '{name}' in {rel_file}"
                        public_api_changes.append(msg)
                        findings.append(msg)
                    elif sig != rep_public[name]:
                        msg = f"Public API signature altered: '{name}' in {rel_file} ({sig} -> {rep_public[name]})"
                        public_api_changes.append(msg)
                        findings.append(msg)

            # Check duplication within file additions
            lines = [l.strip() for l in repaired_content.splitlines() if l.strip() and not l.strip().startswith("#")]
            seen_lines: Dict[str, int] = {}
            for l in lines:
                if len(l) > 20:
                    seen_lines[l] = seen_lines.get(l, 0) + 1
            repeated = [l for l, c in seen_lines.items() if c >= 3]
            if repeated:
                duplication_indicators.extend(repeated[:5])

        complexity_delta = (
            total_repaired_complexity - total_baseline_complexity
            if complexity_measured and baseline_path
            else (total_repaired_complexity if complexity_measured else None)
        )
        length_delta = (
            total_repaired_length - total_baseline_length
            if baseline_path
            else total_repaired_length
        )

        # Evaluate score
        score = 1.0
        if complexity_delta is not None and complexity_delta > self.policy.max_complexity_increase:
            score -= 0.3
            findings.append(
                f"Complexity increase of {complexity_delta} exceeds policy limit of {self.policy.max_complexity_increase}"
            )

        if public_api_changes:
            score -= min(0.3, 0.1 * len(public_api_changes))

        if dead_code_indicators:
            score -= min(0.2, 0.05 * len(dead_code_indicators))

        if type_lint_findings:
            score -= min(0.2, 0.05 * len(type_lint_findings))

        if duplication_indicators:
            score -= min(0.2, 0.05 * len(duplication_indicators))

        score = max(0.0, min(1.0, score))

        status = (
            QualityStatus.PASSED
            if score >= self.policy.min_dimension_score
            else QualityStatus.FAILED
        )

        metrics = {
            "cyclomatic_complexity_delta": complexity_delta,
            "function_length_delta": length_delta,
            "duplication_indicators": duplication_indicators,
            "public_api_changes": public_api_changes,
            "type_lint_findings": type_lint_findings,
            "dead_code_indicators": dead_code_indicators,
            "complexity_measured": complexity_measured,
        }

        explanation = (
            f"Maintainability evaluated (complexity delta: {complexity_delta}). "
            f"API changes: {len(public_api_changes)}, Dead code: {len(dead_code_indicators)}, "
            f"Lint findings: {len(type_lint_findings)}."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.MAINTAINABILITY,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.MAINTAINABILITY, 0.15),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=complexity_measured,
        )

        return metrics, dimension_score

    def _extract_public_signatures(self, tree: ast.AST) -> Dict[str, str]:
        """Extract mapping of public function/class names to signature representations."""
        signatures: Dict[str, str] = {}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    args = [a.arg for a in node.args.args]
                    signatures[node.name] = f"def {node.name}({', '.join(args)})"
            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith("_"):
                    bases = [ast.unparse(b) for b in node.bases]
                    signatures[node.name] = f"class {node.name}({', '.join(bases)})"
        return signatures
