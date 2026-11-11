"""Static workspace import and module resolver for FORGE Phase 11.

Resolves Python imports strictly within the sanitized analysis workspace without executing code.
"""

from pathlib import Path
import sys
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union
from pydantic import BaseModel, Field

from forge.repository.graph_schemas import CallResolutionStatus, ImportRecord
from forge.repository.indexer import discover_workspace_python_files
from forge.repository.protection import is_protected_name, is_protected_path
from forge.repository.validation import assert_contained_path


class ResolvedImport(BaseModel):
    """Result of resolving an import statement."""
    imported_name: str
    local_name: str
    target_file: Optional[str] = None
    target_symbol: Optional[str] = None
    is_external: bool = False
    is_ambiguous: bool = False
    resolution_status: CallResolutionStatus = CallResolutionStatus.UNRESOLVED
    reason: str = Field(..., description="Deterministic explanation of the resolution")


class WorkspaceImportResolver:
    """Static resolver that maps import statements to workspace files or external packages."""

    def __init__(
        self,
        repo_root: Path,
        known_python_files: Optional[Iterable[Union[str, Path]]] = None,
    ):
        self.repo_root = repo_root.resolve()
        self.module_to_file: Dict[str, str] = {}
        self.file_to_module: Dict[str, str] = {}
        self.package_roots: Set[str] = set()
        self._stdlib_modules: Set[str] = set(getattr(sys, "stdlib_module_names", set()))
        self._build_workspace_module_map(known_python_files=known_python_files)

    def _build_workspace_module_map(
        self,
        known_python_files: Optional[Iterable[Union[str, Path]]] = None,
    ) -> None:
        """Scan workspace and map dotted module names to workspace-relative file paths."""
        if not self.repo_root.exists() or not self.repo_root.is_dir():
            return

        # Find potential source roots (repo_root, src, app, lib, etc.) as normalized relative prefixes
        candidate_prefixes: List[str] = []
        seen_prefixes: Set[str] = set()

        def _add_prefix_from_path(root_dir: Path) -> None:
            try:
                rel_p = root_dir.relative_to(self.repo_root).as_posix().strip("/")
            except ValueError:
                return
            if not rel_p or rel_p == ".":
                return
            pref = f"{rel_p}/"
            if pref not in seen_prefixes:
                seen_prefixes.add(pref)
                candidate_prefixes.append(pref)

        for candidate_name in ("src", "app", "lib", "packages"):
            candidate_dir = self.repo_root / candidate_name
            if candidate_dir.is_dir() and not is_protected_path(candidate_dir, base_dir=self.repo_root):
                try:
                    contained = assert_contained_path(candidate_dir, self.repo_root)
                    _add_prefix_from_path(contained)
                except Exception:
                    pass

        rel_files: List[Tuple[str, Tuple[str, ...]]] = []
        if known_python_files is not None:
            for item in known_python_files:
                if isinstance(item, Path):
                    abs_path = item if item.is_absolute() else (self.repo_root / item)
                    try:
                        rel_str = abs_path.relative_to(self.repo_root).as_posix()
                    except ValueError:
                        continue
                else:
                    rel_str = str(item).replace("\\", "/").lstrip("/")
                if not rel_str:
                    continue
                rel_parts = tuple(rel_str.split("/"))
                if is_protected_name(rel_parts[-1]):
                    continue
                if rel_parts[-1] == "__init__.py" and len(rel_parts) >= 3:
                    parent_pref = "/".join(rel_parts[:-2]) + "/"
                    if parent_pref not in seen_prefixes:
                        seen_prefixes.add(parent_pref)
                        candidate_prefixes.append(parent_pref)
                rel_files.append((rel_str, rel_parts))
        else:
            py_files = discover_workspace_python_files(
                self.repo_root,
                max_python_files=10_000,
                max_file_size_bytes=50_000_000,
                exclude_forge=True,
            )
            for resolved_file in py_files:
                rel_str = resolved_file.relative_to(self.repo_root).as_posix()
                rel_parts = tuple(rel_str.split("/"))
                if rel_parts[-1] == "__init__.py" and len(rel_parts) >= 3:
                    parent_pref = "/".join(rel_parts[:-2]) + "/"
                    if parent_pref not in seen_prefixes:
                        seen_prefixes.add(parent_pref)
                        candidate_prefixes.append(parent_pref)
                rel_files.append((rel_str, rel_parts))

        excluded = {".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", ".git"}
        # Map all .py files deterministically
        for rel_str, rel_parts in rel_files:
            if not rel_parts or any(p in excluded for p in rel_parts):
                continue

            # Primary module name from repo root
            if rel_parts[-1] == "__init__.py":
                mod_from_root = ".".join(rel_parts[:-1])
            elif rel_str.endswith(".py"):
                mod_from_root = ".".join((*rel_parts[:-1], rel_parts[-1][:-3]))
            else:
                continue

            if mod_from_root:
                self.module_to_file[mod_from_root] = rel_str
                self.file_to_module[rel_str] = mod_from_root

            # Also index relative to candidate source root prefixes without Path.relative_to()
            for pref in candidate_prefixes:
                if rel_str.startswith(pref):
                    sub_str = rel_str[len(pref):]
                    sub_parts = sub_str.split("/")
                    if sub_parts[-1] == "__init__.py":
                        alt_mod = ".".join(sub_parts[:-1])
                    else:
                        alt_mod = ".".join((*sub_parts[:-1], sub_parts[-1][:-3]))
                    if alt_mod and alt_mod not in self.module_to_file:
                        self.module_to_file[alt_mod] = rel_str

    def get_module_name_for_file(self, rel_path: str) -> str:
        """Get the canonical dotted module name for a workspace relative file path."""
        if rel_path in self.file_to_module:
            return self.file_to_module[rel_path]
        parts = Path(rel_path).parts
        if not parts:
            return ""
        if parts[-1] == "__init__.py":
            return ".".join(parts[:-1])
        if parts[-1].endswith(".py"):
            return ".".join(list(parts[:-1]) + [parts[-1][:-3]])
        return ".".join(parts)

    def is_external_module(self, mod_name: str) -> bool:
        """Check whether a module is a standard library module or external package."""
        if not mod_name:
            return False
        top_name = mod_name.split(".")[0]
        if top_name in self._stdlib_modules:
            return True
        # If top_name is not anywhere in workspace modules, treat as external
        has_workspace_prefix = any(
            m == top_name or m.startswith(f"{top_name}.") for m in self.module_to_file
        )
        return not has_workspace_prefix

    def is_stdlib_module(self, mod_name: str) -> bool:
        """Check whether a module belongs to the Python standard library."""
        if not mod_name:
            return False
        top_name = mod_name.split(".")[0]
        return top_name in self._stdlib_modules

    def is_third_party_module(self, mod_name: str) -> bool:
        """Check whether a module is a third-party external package (not stdlib and not internal)."""
        if not mod_name:
            return False
        top_name = mod_name.split(".")[0]
        if top_name in self._stdlib_modules:
            return False
        return self.is_external_module(mod_name)

    def resolve_relative_import_base(self, current_file: str, level: int) -> Tuple[Optional[str], Optional[str]]:
        """Compute the base directory and dotted package for a relative import.

        Returns (base_dir_relative_path, base_module_name), or (None, None) if escaping root.
        """
        curr_p = Path(current_file)
        dir_parts = list(curr_p.parent.parts)

        # level 1 climbs 0 dirs from package dir
        # level N climbs (N - 1) dirs from package dir
        climbs = level - 1
        if climbs > len(dir_parts):
            # Escapes above repo root
            return None, None

        if climbs == len(dir_parts):
            target_parts: List[str] = []
        else:
            target_parts = dir_parts[:len(dir_parts) - climbs]

        rel_target = "/".join(target_parts) if target_parts else ""
        try:
            abs_target = (self.repo_root / rel_target).resolve()
            assert_contained_path(abs_target, self.repo_root)
            base_mod = self.get_module_name_for_file(rel_target) if rel_target else ""
            return rel_target, base_mod
        except Exception:
            return None, None

    def resolve_import(self, current_file: str, imp: ImportRecord) -> ResolvedImport:
        """Resolve a single import record in the context of current_file."""
        # 1. Handle relative import
        if imp.level > 0:
            base_dir, base_mod = self.resolve_relative_import_base(current_file, imp.level)
            if base_dir is None:
                return ResolvedImport(
                    imported_name=imp.name,
                    local_name=imp.alias or imp.name,
                    resolution_status=CallResolutionStatus.UNRESOLVED,
                    reason=f"Relative import level {imp.level} escapes workspace root",
                )

            # Target module can be from_module or imp.name
            full_mod_parts = []
            if base_mod:
                full_mod_parts.append(base_mod)
            if imp.from_module:
                full_mod_parts.append(imp.from_module)

            full_mod = ".".join(full_mod_parts)

            # Check if (full_mod + "." + imp.name) is a module in workspace
            candidate_submod = f"{full_mod}.{imp.name}" if full_mod else imp.name
            if candidate_submod in self.module_to_file:
                return ResolvedImport(
                    imported_name=imp.name,
                    local_name=imp.alias or imp.name,
                    target_file=self.module_to_file[candidate_submod],
                    target_symbol=None,
                    resolution_status=CallResolutionStatus.RESOLVED_INTERNAL,
                    reason=f"Resolved relative submodule '{candidate_submod}' to '{self.module_to_file[candidate_submod]}'",
                )

            # Check if full_mod itself is a module
            if full_mod in self.module_to_file:
                return ResolvedImport(
                    imported_name=imp.name,
                    local_name=imp.alias or imp.name,
                    target_file=self.module_to_file[full_mod],
                    target_symbol=imp.name,
                    resolution_status=CallResolutionStatus.RESOLVED_INTERNAL,
                    reason=f"Resolved symbol '{imp.name}' in relative module '{full_mod}'",
                )

            return ResolvedImport(
                imported_name=imp.name,
                local_name=imp.alias or imp.name,
                resolution_status=CallResolutionStatus.UNRESOLVED,
                reason=f"Relative import '{imp.name}' could not be located in workspace",
            )

        # 2. Handle absolute from-import
        if imp.from_module:
            from_mod = imp.from_module

            # Check if (from_mod.name) is a submodule in workspace
            combined_mod = f"{from_mod}.{imp.name}"
            if combined_mod in self.module_to_file:
                return ResolvedImport(
                    imported_name=imp.name,
                    local_name=imp.alias or imp.name,
                    target_file=self.module_to_file[combined_mod],
                    target_symbol=None,
                    resolution_status=CallResolutionStatus.RESOLVED_INTERNAL,
                    reason=f"Resolved imported submodule '{combined_mod}' to '{self.module_to_file[combined_mod]}'",
                )

            # Check if from_mod is a workspace module
            if from_mod in self.module_to_file:
                return ResolvedImport(
                    imported_name=imp.name,
                    local_name=imp.alias or imp.name,
                    target_file=self.module_to_file[from_mod],
                    target_symbol=imp.name,
                    resolution_status=CallResolutionStatus.RESOLVED_INTERNAL,
                    reason=f"Resolved symbol '{imp.name}' in workspace module '{from_mod}'",
                )

            # Check if from_mod is external
            if self.is_external_module(from_mod):
                return ResolvedImport(
                    imported_name=imp.name,
                    local_name=imp.alias or imp.name,
                    is_external=True,
                    resolution_status=CallResolutionStatus.RESOLVED_EXTERNAL,
                    reason=f"External module import from '{from_mod}'",
                )

            return ResolvedImport(
                imported_name=imp.name,
                local_name=imp.alias or imp.name,
                resolution_status=CallResolutionStatus.UNRESOLVED,
                reason=f"Module '{from_mod}' not found in workspace or standard library",
            )

        # 3. Handle plain absolute import: import a.b [as c]
        mod_name = imp.name
        if mod_name in self.module_to_file:
            local_name = imp.alias or (mod_name.split(".")[0] if not imp.alias else imp.alias)
            return ResolvedImport(
                imported_name=mod_name,
                local_name=local_name,
                target_file=self.module_to_file[mod_name],
                target_symbol=None,
                resolution_status=CallResolutionStatus.RESOLVED_INTERNAL,
                reason=f"Resolved module '{mod_name}' to workspace file '{self.module_to_file[mod_name]}'",
            )

        if self.is_external_module(mod_name):
            local_name = imp.alias or mod_name.split(".")[0]
            return ResolvedImport(
                imported_name=mod_name,
                local_name=local_name,
                is_external=True,
                resolution_status=CallResolutionStatus.RESOLVED_EXTERNAL,
                reason=f"External module '{mod_name}'",
            )

        return ResolvedImport(
            imported_name=mod_name,
            local_name=imp.alias or mod_name,
            resolution_status=CallResolutionStatus.UNRESOLVED,
            reason=f"Module '{mod_name}' not found in workspace",
        )
