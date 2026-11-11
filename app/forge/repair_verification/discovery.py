"""Phase 12 M5: Test Discovery Engine for Repair Verification.

Performs bounded, evidence-driven test discovery across repository workspaces:
- Enforces strict path containment and rejects symlink traversal escaping workspace.
- Maps modified files to targeted tests by naming conventions and import dependency analysis.
- Detects regression candidates across importing modules and dependent test files.
- Identifies duplicate test identifiers.
- Bounded file discovery and output limits.
"""

import ast
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Dict, List, Optional, Set, Tuple

from forge.repository.indexer import discover_workspace_python_files
from forge.repository.validation import assert_contained_path, SecurityError
from forge.repair_verification.schemas import (
    TestIdentifier,
    TestScopeCategory,
    MAX_TESTS_PER_SUITE,
)


MAX_DISCOVERY_FILES = 1_000
MAX_FILE_BYTES = 500_000


@dataclass
class DiscoveryResult:
    """Outcome of test discovery."""
    workspace_path: Path
    all_test_files: List[str] = field(default_factory=list)
    targeted_tests: List[TestIdentifier] = field(default_factory=list)
    regression_tests: List[TestIdentifier] = field(default_factory=list)
    other_tests: List[TestIdentifier] = field(default_factory=list)
    duplicate_identifiers: List[str] = field(default_factory=list)
    scan_limit_exceeded: bool = False
    limitations: List[str] = field(default_factory=list)


class RepairTestDiscoverer:
    """Discovers test suites and maps modified files to targeted and regression tests."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = assert_contained_path(workspace_path.resolve(), workspace_path.resolve())

    def discover_tests(
        self,
        modified_files: Optional[List[str]] = None,
    ) -> DiscoveryResult:
        """Scan workspace for test files and map them to modified source files."""
        result = DiscoveryResult(workspace_path=self.workspace_path)
        normalized_mods: List[str] = []
        if modified_files:
            for mf in modified_files:
                norm = mf.strip().replace("\\", "/").lstrip("/")
                normalized_mods.append(norm)

        # 1. Discover all test files and candidate non-test python files in one pruned walk
        all_py_paths = discover_workspace_python_files(
            self.workspace_path,
            max_python_files=MAX_DISCOVERY_FILES,
            max_file_size_bytes=MAX_FILE_BYTES,
            exclude_forge=True,
        )
        all_tests_set: Set[str] = set()
        non_test_paths: List[Path] = []
        for p in all_py_paths:
            rel_str = str(p.relative_to(self.workspace_path)).replace("\\", "/")
            name = p.name
            if (
                name.startswith("test_")
                or name.endswith("_test.py")
                or "tests/" in rel_str
                or "test/" in rel_str
                or rel_str.startswith("tests/")
                or rel_str.startswith("test/")
            ):
                all_tests_set.add(rel_str)
            else:
                non_test_paths.append(p)

        all_tests = sorted(all_tests_set)
        result.all_test_files = all_tests
        if len(all_py_paths) >= MAX_DISCOVERY_FILES:
            result.scan_limit_exceeded = True
            result.limitations.append(f"Reached maximum file scan cap of {MAX_DISCOVERY_FILES} files")

        # 2. Extract module identifiers from modified files
        mod_stems = {Path(m).stem: m for m in normalized_mods}
        mod_module_paths = {
            m.replace("/", ".").removesuffix(".py"): m
            for m in normalized_mods
            if m.endswith(".py")
        }

        # 2b. Map 1-hop indirect dependencies only for files that textually mention any modified stem
        indirect_dependent_stems: Set[str] = set()
        if mod_stems:
            mod_stem_keys = list(mod_stems.keys())
            for p in non_test_paths:
                try:
                    src_content = p.read_text(encoding="utf-8", errors="replace")
                    if not any(st in src_content for st in mod_stem_keys):
                        continue
                    src_tree = ast.parse(src_content, filename=str(p))
                    for node in ast.iter_child_nodes(src_tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                alias_parts = alias.name.split(".")
                                if any(st == alias_parts[-1] or st in alias_parts for st in mod_stem_keys):
                                    indirect_dependent_stems.add(p.stem)
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                mod_parts = node.module.split(".")
                                if any(st == mod_parts[-1] or st in mod_parts for st in mod_stem_keys):
                                    indirect_dependent_stems.add(p.stem)
                except Exception:
                    pass

        seen_test_ids: Set[str] = set()

        # 3. Inspect each test file
        for test_file_rel in all_tests:
            full_test_path = self.workspace_path / test_file_rel
            try:
                assert_contained_path(full_test_path, self.workspace_path)
            except SecurityError:
                continue

            test_names, imported_modules, file_limits = self._parse_test_file(full_test_path)
            result.limitations.extend(file_limits)
            test_stem = Path(test_file_rel).stem

            # Check if this test file is directly targeted
            is_targeted = False
            for stem, orig in mod_stems.items():
                if test_stem == f"test_{stem}" or test_stem == f"{stem}_test":
                    is_targeted = True
                    break
                # Check direct import
                if any(stem == imp.split(".")[-1] or stem in imp.split(".") for imp in imported_modules):
                    is_targeted = True
                    break
                for mpath in mod_module_paths:
                    if any(imp == mpath or imp.startswith(f"{mpath}.") for imp in imported_modules):
                        is_targeted = True
                        break

            # Assign test items
            for t_name in test_names:
                t_id = f"{test_file_rel}::{t_name}"
                if t_id in seen_test_ids:
                    result.duplicate_identifiers.append(t_id)
                seen_test_ids.add(t_id)

                if is_targeted:
                    item = TestIdentifier(
                        test_id=t_id,
                        test_file=test_file_rel,
                        test_name=t_name,
                        category=TestScopeCategory.TARGETED,
                    )
                    result.targeted_tests.append(item)
                else:
                    # Check if it imports something related (direct or indirect regression)
                    is_regression = False
                    for stem in mod_stems:
                        if any(stem in imp for imp in imported_modules):
                            is_regression = True
                            break
                    if not is_regression and indirect_dependent_stems:
                        for ind_stem in indirect_dependent_stems:
                            if any(ind_stem in imp for imp in imported_modules):
                                is_regression = True
                                break

                    cat = TestScopeCategory.REGRESSION if is_regression else TestScopeCategory.FULL_SUITE
                    item = TestIdentifier(
                        test_id=t_id,
                        test_file=test_file_rel,
                        test_name=t_name,
                        category=cat,
                    )
                    if is_regression:
                        result.regression_tests.append(item)
                    else:
                        result.other_tests.append(item)

        if result.duplicate_identifiers:
            result.limitations.append(
                f"Duplicate test identifier(s) detected: {', '.join(result.duplicate_identifiers)}"
            )

        return result

    def _find_test_files(self) -> List[str]:
        """Find Python test files matching standard patterns."""
        test_files: List[str] = []
        for p in discover_workspace_python_files(
            self.workspace_path,
            max_python_files=MAX_DISCOVERY_FILES,
            max_file_size_bytes=MAX_FILE_BYTES,
            exclude_forge=True,
        ):
            rel_str = str(p.relative_to(self.workspace_path)).replace("\\", "/")
            name = p.name
            if (
                name.startswith("test_")
                or name.endswith("_test.py")
                or "tests/" in rel_str
                or "test/" in rel_str
                or rel_str.startswith("tests/")
                or rel_str.startswith("test/")
            ):
                test_files.append(rel_str)

        return sorted(test_files)

    def _parse_test_file(self, file_path: Path) -> Tuple[List[str], Set[str], List[str]]:
        """Parse Python test file to extract test function names, imported modules, and discovery limitations."""
        test_names: List[str] = []
        imported_modules: Set[str] = set()
        file_limitations: List[str] = []

        try:
            if file_path.stat().st_size > MAX_FILE_BYTES:
                return ["test_suite_file"], set(), [f"File {file_path.name} exceeds max size limit"]
            content = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=str(file_path))
        except Exception as e:
            file_limitations.append(f"AST parse failure in {file_path.name}: {e}")
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                func_matches = re.findall(r"def\s+(test_[a-zA-Z0-9_]+)\s*\(", content)
                return sorted(set(func_matches)) or ["test_suite_fallback"], set(), file_limitations
            except Exception:
                return ["test_suite_fallback"], set(), file_limitations

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name.startswith("test_") or node.name.endswith("_test"):
                    test_names.append(node.name)
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("Test") or node.name.endswith("Test") or node.name.endswith("Tests"):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                            test_names.append(f"{node.name}::{item.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
            elif isinstance(node, ast.Call):
                # Detect dynamic import attempts: importlib.import_module or __import__
                func = node.func
                is_dyn = False
                mod_arg = None
                if isinstance(func, ast.Name) and func.id == "__import__":
                    is_dyn = True
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        mod_arg = node.args[0].value
                elif isinstance(func, ast.Attribute) and func.attr == "import_module":
                    is_dyn = True
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        mod_arg = node.args[0].value
                if is_dyn:
                    if mod_arg:
                        imported_modules.add(mod_arg)
                    file_limitations.append(
                        f"Dynamic import detected in {file_path.name}; test dependency graph may be incomplete"
                    )

        if not test_names:
            test_names = [f"test_{Path(file_path).stem}"]

        return sorted(set(test_names)), imported_modules, file_limitations
