"""AST-based Python source code indexing without executing code."""

import ast
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from forge.repository.validation import assert_contained_path


class SymbolType:
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    IMPORT = "import"


class SymbolRecord(BaseModel):
    """An individual indexed Python code symbol."""
    name: str
    symbol_type: str
    file_path: str
    start_line: int
    end_line: int
    docstring: Optional[str] = None
    parameters: List[str] = Field(default_factory=list)
    parent_class: Optional[str] = None
    is_async: bool = False


class SymbolIndex(BaseModel):
    """Searchable catalog of symbols within a repository."""
    symbols: List[SymbolRecord] = Field(default_factory=list)
    file_symbols: Dict[str, List[SymbolRecord]] = Field(default_factory=dict)
    total_classes: int = 0
    total_functions: int = 0
    total_methods: int = 0
    total_imports: int = 0

    def query(self, name: str, symbol_type: Optional[str] = None) -> List[SymbolRecord]:
        """Search for symbols by name substring and optional type."""
        lower_name = name.lower()
        results = []
        for sym in self.symbols:
            if lower_name in sym.name.lower():
                if symbol_type is None or sym.symbol_type == symbol_type:
                    results.append(sym)
        return results


class ASTSymbolVisitor(ast.NodeVisitor):
    """Walks the AST of a single Python file to extract symbols."""

    def __init__(self, rel_path: str):
        self.rel_path = rel_path
        self.symbols: List[SymbolRecord] = []
        self.current_class: Optional[str] = None

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.symbols.append(
                SymbolRecord(
                    name=alias.name,
                    symbol_type=SymbolType.IMPORT,
                    file_path=self.rel_path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        for alias in node.names:
            full_name = f"{mod}.{alias.name}" if mod else alias.name
            self.symbols.append(
                SymbolRecord(
                    name=full_name,
                    symbol_type=SymbolType.IMPORT,
                    file_path=self.rel_path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        doc = ast.get_docstring(node)
        self.symbols.append(
            SymbolRecord(
                name=node.name,
                symbol_type=SymbolType.CLASS,
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=doc.strip() if doc else None,
            )
        )
        prev_class = self.current_class
        self.current_class = node.name
        self.generic_visit(node)
        self.current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._record_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._record_function(node, is_async=True)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool):
        doc = ast.get_docstring(node)
        params = [arg.arg for arg in node.args.args]
        sym_type = SymbolType.METHOD if self.current_class else SymbolType.FUNCTION

        self.symbols.append(
            SymbolRecord(
                name=node.name,
                symbol_type=sym_type,
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=doc.strip() if doc else None,
                parameters=params,
                parent_class=self.current_class,
                is_async=is_async,
            )
        )
        # Visit inner nodes (e.g. nested functions)
        self.generic_visit(node)


def index_python_file(file_path: Path, repo_root: Path) -> List[SymbolRecord]:
    """Parse a single Python file into AST and collect symbols."""
    resolved_file = assert_contained_path(file_path, repo_root)
    rel_path = str(resolved_file.relative_to(repo_root))

    try:
        content = resolved_file.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=rel_path)
    except Exception:
        # Syntax error or unparseable file; return empty symbol list
        return []

    visitor = ASTSymbolVisitor(rel_path)
    visitor.visit(tree)
    return visitor.symbols


def build_symbol_index(
    repo_root: Path,
    max_python_files: int = 500,
    max_file_size_bytes: int = 500_000,
) -> SymbolIndex:
    """Index all Python files in the repository."""
    root = repo_root.resolve()
    all_symbols: List[SymbolRecord] = []
    file_map: Dict[str, List[SymbolRecord]] = {}

    classes = 0
    funcs = 0
    methods = 0
    imports = 0

    processed_count = 0
    for py_file in root.rglob("*.py"):
        # Prevent traversal outside root
        try:
            resolved = assert_contained_path(py_file, root)
        except Exception:
            continue

        # Skip virtualenvs and caches
        parts = resolved.relative_to(root).parts
        if any(p in [".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"] for p in parts):
            continue

        if py_file.stat().st_size > max_file_size_bytes:
            continue

        symbols = index_python_file(py_file, root)
        if symbols:
            rel_str = str(resolved.relative_to(root))
            file_map[rel_str] = symbols
            all_symbols.extend(symbols)

            for sym in symbols:
                if sym.symbol_type == SymbolType.CLASS:
                    classes += 1
                elif sym.symbol_type == SymbolType.FUNCTION:
                    funcs += 1
                elif sym.symbol_type == SymbolType.METHOD:
                    methods += 1
                elif sym.symbol_type == SymbolType.IMPORT:
                    imports += 1

        processed_count += 1
        if processed_count >= max_python_files:
            break

    return SymbolIndex(
        symbols=all_symbols,
        file_symbols=file_map,
        total_classes=classes,
        total_functions=funcs,
        total_methods=methods,
        total_imports=imports,
    )
