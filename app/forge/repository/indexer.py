"""AST-based Python source code indexing without executing code."""

import ast
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from forge.repository.graph_schemas import CallSiteRecord, GraphNode, ImportRecord, NodeKind
from forge.repository.protection import is_protected_name, is_protected_path
from forge.repository.validation import assert_contained_path

DEFAULT_EXCLUDED_WALK_DIRS: Set[str] = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    ".git",
    "node_modules",
}


def discover_workspace_python_files(
    repo_root: Path,
    max_python_files: int = 500,
    max_file_size_bytes: int = 500_000,
    exclude_forge: bool = True,
) -> List[Path]:
    """Safely discover eligible Python files using a pruned os.walk without descending into VCS/venv/cache dirs."""
    root = Path(repo_root).resolve()
    if not root.exists() or not root.is_dir():
        return []

    excluded_dirs = set(DEFAULT_EXCLUDED_WALK_DIRS)
    if exclude_forge:
        excluded_dirs.add(".forge")

    root_str = str(root)
    prefix_len = len(root_str) + (0 if root_str.endswith(os.sep) else 1)
    discovered: List[Tuple[str, Path]] = []

    for dirpath_str, dirnames, filenames in os.walk(root_str, followlinks=False):
        dirpath = Path(dirpath_str)
        if dirpath_str != root_str and os.path.islink(dirpath_str):
            dirnames[:] = []
            continue

        kept_dirs: List[str] = []
        for d in dirnames:
            if d in excluded_dirs or d.endswith(".egg-info") or is_protected_name(d):
                continue
            cand_dir = os.path.join(dirpath_str, d)
            if os.path.islink(cand_dir):
                continue
            kept_dirs.append(d)
        dirnames[:] = sorted(kept_dirs)

        rel_dir = "" if dirpath_str == root_str else dirpath_str[prefix_len:].replace(os.sep, "/")
        for fname in sorted(filenames):
            if not fname.endswith(".py") or is_protected_name(fname):
                continue
            full_path_str = os.path.join(dirpath_str, fname)
            try:
                st = os.lstat(full_path_str)
            except OSError:
                continue

            if (st.st_mode & 0o170000) == 0o120000:  # Symlink: run full boundary check
                py_file = Path(full_path_str)
                try:
                    resolved = assert_contained_path(py_file, root)
                except Exception:
                    continue
                if is_protected_path(resolved, base_dir=root):
                    continue
                try:
                    if resolved.stat().st_size > max_file_size_bytes:
                        continue
                except OSError:
                    continue
                rel_str = str(resolved.relative_to(root))
                discovered.append((rel_str, resolved))
            else:
                if st.st_size > max_file_size_bytes:
                    continue
                rel_str = f"{rel_dir}/{fname}" if rel_dir else fname
                discovered.append((rel_str, Path(full_path_str)))

    discovered.sort(key=lambda item: item[0])
    if len(discovered) > max_python_files:
        discovered = discovered[:max_python_files]
    return [item[1] for item in discovered]


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
    qualified_name: Optional[str] = None
    node_id: Optional[str] = None


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


def rel_path_to_module_name(rel_path: str) -> str:
    """Convert a workspace-relative file path to a canonical dotted module name."""
    parts = list(Path(rel_path).parts)
    if not parts:
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


class ASTSymbolVisitor(ast.NodeVisitor):
    """Walks the AST of a single Python file to extract symbols, scopes, imports, and calls."""

    def __init__(self, rel_path: str, module_name: Optional[str] = None):
        self.rel_path = rel_path
        self.module_name = module_name or rel_path_to_module_name(rel_path)
        self.symbols: List[SymbolRecord] = []
        self.nodes: List[GraphNode] = []
        self.call_sites: List[CallSiteRecord] = []
        self.imports: List[ImportRecord] = []
        self.class_bases: Dict[str, List[str]] = {}
        self.callable_local_vars: Dict[str, Set[str]] = {}
        self.module_docstring: Optional[str] = None

        self.current_class: Optional[str] = None
        self._scope_stack: List[str] = []
        self._enclosing_callable_stack: List[str] = []

    def _get_current_caller_id(self) -> str:
        """Return the node_id of the currently enclosing function, method, or module."""
        if self._enclosing_callable_stack:
            return self._enclosing_callable_stack[-1]
        return f"{self.rel_path}:<module>"

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
            self.imports.append(
                ImportRecord(
                    name=alias.name,
                    alias=alias.asname,
                    from_module=None,
                    level=0,
                    line=node.lineno,
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
            self.imports.append(
                ImportRecord(
                    name=alias.name,
                    alias=alias.asname,
                    from_module=node.module,
                    level=node.level or 0,
                    line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        doc = ast.get_docstring(node)
        base_names: List[str] = []
        for b in node.bases:
            try:
                base_names.append(ast.unparse(b))
            except Exception:
                pass
        self.class_bases[node.name] = base_names

        # Compute qualified name and node ID
        scoped_parts = list(self._scope_stack) + [node.name]
        scoped_str = ".".join(scoped_parts)
        qual_name = f"{self.module_name}.{scoped_str}" if self.module_name else scoped_str
        node_id = f"{self.rel_path}:{scoped_str}"

        self.symbols.append(
            SymbolRecord(
                name=node.name,
                symbol_type=SymbolType.CLASS,
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=doc.strip() if doc else None,
                qualified_name=qual_name,
                node_id=node_id,
            )
        )

        self.nodes.append(
            GraphNode(
                node_id=node_id,
                kind=NodeKind.CLASS,
                name=node.name,
                qualified_name=qual_name,
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=doc.strip() if doc else None,
                parent_scope=".".join(self._scope_stack) if self._scope_stack else None,
            )
        )

        prev_class = self.current_class
        self.current_class = node.name
        self._scope_stack.append(node.name)

        self.generic_visit(node)

        self._scope_stack.pop()
        self.current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._record_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._record_function(node, is_async=True)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool):
        doc = ast.get_docstring(node)
        params = [arg.arg for arg in node.args.args]
        sym_type = SymbolType.METHOD if self.current_class else SymbolType.FUNCTION
        node_kind = NodeKind.METHOD if self.current_class else NodeKind.FUNCTION

        # Compute scoped name
        scoped_parts = list(self._scope_stack) + [node.name]
        scoped_str = ".".join(scoped_parts)
        qual_name = f"{self.module_name}.{scoped_str}" if self.module_name else scoped_str
        node_id = f"{self.rel_path}:{scoped_str}"

        # Collect all parameters for local shadowing detection
        all_params = [arg.arg for arg in getattr(node.args, "posonlyargs", []) + node.args.args]
        if node.args.vararg:
            all_params.append(node.args.vararg.arg)
        all_params.extend([arg.arg for arg in node.args.kwonlyargs])
        if node.args.kwarg:
            all_params.append(node.args.kwarg.arg)
        self.callable_local_vars[node_id] = set(all_params)

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
                qualified_name=qual_name,
                node_id=node_id,
            )
        )

        self.nodes.append(
            GraphNode(
                node_id=node_id,
                kind=node_kind,
                name=node.name,
                qualified_name=qual_name,
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=doc.strip() if doc else None,
                parent_scope=".".join(self._scope_stack) if self._scope_stack else None,
                parameters=params,
                is_async=is_async,
            )
        )

        # Enter callable scope
        self._scope_stack.append(node.name)
        self._scope_stack.append("<locals>")
        self._enclosing_callable_stack.append(node_id)

        # Visit inner nodes (calls, nested functions, assignments, etc.)
        self.generic_visit(node)

        self._enclosing_callable_stack.pop()
        self._scope_stack.pop()  # Pop "<locals>"
        self._scope_stack.pop()  # Pop node.name

    def _add_local_var(self, name: str) -> None:
        if self._enclosing_callable_stack:
            curr_id = self._enclosing_callable_stack[-1]
            if curr_id in self.callable_local_vars:
                self.callable_local_vars[curr_id].add(name)

    def _extract_target_names(self, target: ast.AST) -> List[str]:
        names = []
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                names.extend(self._extract_target_names(elt))
        elif isinstance(target, ast.Starred):
            names.extend(self._extract_target_names(target.value))
        return names

    def visit_Assign(self, node: ast.Assign):
        for target in node.targets:
            for name in self._extract_target_names(target):
                self._add_local_var(name)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        for name in self._extract_target_names(node.target):
            self._add_local_var(name)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        for name in self._extract_target_names(node.target):
            self._add_local_var(name)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr):
        for name in self._extract_target_names(node.target):
            self._add_local_var(name)
        self.generic_visit(node)

    def visit_For(self, node: ast.For):
        for name in self._extract_target_names(node.target):
            self._add_local_var(name)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        for name in self._extract_target_names(node.target):
            self._add_local_var(name)
        self.generic_visit(node)

    def visit_With(self, node: ast.With):
        for item in node.items:
            if item.optional_vars:
                for name in self._extract_target_names(item.optional_vars):
                    self._add_local_var(name)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith):
        for item in node.items:
            if item.optional_vars:
                for name in self._extract_target_names(item.optional_vars):
                    self._add_local_var(name)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.name:
            self._add_local_var(node.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        caller_id = self._get_current_caller_id()
        callee_expr, receiver_kind, callee_name, receiver_name, is_dynamic = self._analyze_callee(node.func)

        try:
            raw_expr = ast.unparse(node)
            if len(raw_expr) > 200:
                raw_expr = raw_expr[:197] + "..."
        except Exception:
            raw_expr = f"{callee_expr}(...)"

        call_site = CallSiteRecord(
            caller_id=caller_id,
            callee_expr=callee_expr,
            raw_expr=raw_expr,
            line=node.lineno,
            col_offset=getattr(node, "col_offset", None),
            receiver_kind=receiver_kind,
            callee_name=callee_name,
            receiver_name=receiver_name,
        )
        self.call_sites.append(call_site)

        # Crucial: generic_visit visits arguments so nested calls like foo(bar()) are captured
        self.generic_visit(node)

    def _analyze_callee(self, func_node: ast.AST) -> Tuple[str, str, Optional[str], Optional[str], bool]:
        """Analyze a call target expression.

        Returns: (callee_expr, receiver_kind, callee_name, receiver_name, is_dynamic)
        """
        if isinstance(func_node, ast.Name):
            return func_node.id, "name", func_node.id, None, False

        if isinstance(func_node, ast.Attribute):
            attr_name = func_node.attr
            val = func_node.value

            if isinstance(val, ast.Name):
                if val.id == "self":
                    return f"self.{attr_name}", "self", attr_name, "self", False
                elif val.id == "cls":
                    return f"cls.{attr_name}", "cls", attr_name, "cls", False
                else:
                    return f"{val.id}.{attr_name}", "module_or_instance", attr_name, val.id, False

            # Check for getattr-based call: getattr(obj, "method")()
            if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "getattr":
                return "getattr(...)", "dynamic", attr_name, None, True

            # Chained attribute access (e.g. a.b.c())
            try:
                unparsed = ast.unparse(func_node)
            except Exception:
                unparsed = f"...{attr_name}"
            return unparsed, "chained", attr_name, None, False

        # Direct getattr call: getattr(obj, "method")()
        if isinstance(func_node, ast.Call) and isinstance(func_node.func, ast.Name) and func_node.func.id == "getattr":
            return "getattr(...)", "dynamic", None, None, True

        # Any other complex expression (lambdas, subscripts, factory calls)
        try:
            unparsed = ast.unparse(func_node)
        except Exception:
            unparsed = "dynamic_call"
        return unparsed, "dynamic", None, None, True


def parse_python_file_details(
    file_path: Path,
    repo_root: Path,
    preloaded_content: Optional[str] = None,
    preverified: bool = False,
) -> Optional[ASTSymbolVisitor]:
    """Parse a single Python file into AST and return the populated ASTSymbolVisitor."""
    if not preverified:
        if is_protected_path(file_path, base_dir=repo_root):
            return None

        try:
            resolved_file = assert_contained_path(file_path, repo_root)
        except Exception:
            return None

        if is_protected_path(resolved_file, base_dir=repo_root):
            return None
    else:
        resolved_file = file_path

    rel_path = str(resolved_file.relative_to(repo_root))

    try:
        content = preloaded_content if preloaded_content is not None else resolved_file.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content, filename=rel_path)
    except Exception:
        return None

    visitor = ASTSymbolVisitor(rel_path)
    try:
        visitor.module_docstring = ast.get_docstring(tree)
    except Exception:
        visitor.module_docstring = None

    # Register top-level module node
    end_line = getattr(tree, "end_lineno", 1) or len(content.splitlines()) or 1
    module_node = GraphNode(
        node_id=f"{rel_path}:<module>",
        kind=NodeKind.MODULE,
        name=Path(rel_path).stem,
        qualified_name=visitor.module_name,
        file_path=rel_path,
        start_line=1,
        end_line=max(end_line, 1),
    )
    visitor.nodes.append(module_node)

    visitor.visit(tree)
    return visitor


def index_python_file(file_path: Path, repo_root: Path) -> List[SymbolRecord]:
    """Parse a single Python file into AST and collect symbols (backward-compatible API)."""
    visitor = parse_python_file_details(file_path, repo_root)
    return visitor.symbols if visitor else []


def build_symbol_index(
    repo_root: Path,
    max_python_files: int = 500,
    max_file_size_bytes: int = 500_000,
) -> SymbolIndex:
    """Index all Python files in the repository (backward-compatible API)."""
    root = repo_root.resolve()
    all_symbols: List[SymbolRecord] = []
    file_map: Dict[str, List[SymbolRecord]] = {}

    classes = 0
    funcs = 0
    methods = 0
    imports = 0

    for resolved in discover_workspace_python_files(
        root,
        max_python_files=max_python_files,
        max_file_size_bytes=max_file_size_bytes,
        exclude_forge=False,
    ):
        visitor = parse_python_file_details(resolved, root, preverified=True)
        symbols = visitor.symbols if visitor else []
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

    return SymbolIndex(
        symbols=all_symbols,
        file_symbols=file_map,
        total_classes=classes,
        total_functions=funcs,
        total_methods=methods,
        total_imports=imports,
    )
