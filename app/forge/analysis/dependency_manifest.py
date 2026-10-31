"""Dependency manifest parser and cross-file relationship discovery.

Inspects workspace dependency declarations (pyproject.toml, requirements.txt, setup.cfg)
and resolves cross-file relationships (implementations <-> tests <-> configurations)
to support evidence-driven investigations without executing target code.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class DependencyManifest:
    """Parsed dependency declarations from workspace configuration files."""
    dependencies: Dict[str, str] = field(default_factory=dict)
    python_requires: Optional[str] = None
    manifest_sources: List[str] = field(default_factory=list)
    raw_details: Dict[str, Any] = field(default_factory=dict)

    def get_constraint(self, package_name: str) -> Optional[str]:
        """Get version constraint for a package (case-insensitive, normalized)."""
        norm = re.sub(r"[-_.]+", "-", package_name.strip().lower())
        return self.dependencies.get(norm)

    def has_package(self, package_name: str) -> bool:
        """Check if package is declared in dependencies."""
        return self.get_constraint(package_name) is not None


def _normalize_package_name(name: str) -> str:
    """Normalize PEP 503 package name."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _parse_requirement_spec(spec: str) -> Tuple[str, str]:
    """Split requirement string into (normalized_package_name, constraint_string)."""
    # Remove comments and environment markers
    cleaned = spec.split("#")[0].split(";")[0].strip()
    match = re.match(r"^([a-zA-Z0-9_\-\.]+)\s*(.*)$", cleaned)
    if not match:
        return "", ""
    pkg_name = _normalize_package_name(match.group(1))
    constraint = match.group(2).strip()
    return pkg_name, constraint


def parse_workspace_dependencies(workspace_path: Path) -> DependencyManifest:
    """Parse all available dependency manifests in the workspace root or standard locations."""
    deps: Dict[str, str] = {}
    sources: List[str] = []
    py_requires: Optional[str] = None
    details: Dict[str, Any] = {}

    # 1. pyproject.toml
    pyproject_path = workspace_path / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            content = pyproject_path.read_text(encoding="utf-8", errors="replace")
            data = tomllib.loads(content)
            sources.append("pyproject.toml")
            details["pyproject"] = data

            # PEP 621 dependencies
            project_sec = data.get("project", {})
            if isinstance(project_sec, dict):
                py_requires = project_sec.get("requires-python")
                for req in project_sec.get("dependencies", []):
                    if isinstance(req, str):
                        pkg, constraint = _parse_requirement_spec(req)
                        if pkg:
                            deps[pkg] = constraint or "*"

                # Optional dependencies
                for group, reqs in project_sec.get("optional-dependencies", {}).items():
                    if isinstance(reqs, list):
                        for req in reqs:
                            if isinstance(req, str):
                                pkg, constraint = _parse_requirement_spec(req)
                                if pkg and pkg not in deps:
                                    deps[pkg] = constraint or "*"

            # Poetry dependencies
            poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            if isinstance(poetry_deps, dict):
                for pkg_raw, val in poetry_deps.items():
                    pkg = _normalize_package_name(pkg_raw)
                    if pkg == "python":
                        if isinstance(val, str) and not py_requires:
                            py_requires = val
                        continue
                    if isinstance(val, str):
                        deps[pkg] = val
                    elif isinstance(val, dict) and "version" in val:
                        deps[pkg] = str(val["version"])
                    elif pkg not in deps:
                        deps[pkg] = "*"

            # Flit dependencies
            flit_reqs = data.get("tool", {}).get("flit", {}).get("metadata", {}).get("requires", [])
            if isinstance(flit_reqs, list):
                for req in flit_reqs:
                    if isinstance(req, str):
                        pkg, constraint = _parse_requirement_spec(req)
                        if pkg and pkg not in deps:
                            deps[pkg] = constraint or "*"
        except Exception:
            pass

    # 2. requirements*.txt
    for req_file in workspace_path.glob("requirements*.txt"):
        if req_file.is_file():
            rel_name = req_file.name
            try:
                for line in req_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("-", "#")):
                        continue
                    pkg, constraint = _parse_requirement_spec(line)
                    if pkg and pkg not in deps:
                        deps[pkg] = constraint or "*"
                if rel_name not in sources:
                    sources.append(rel_name)
            except Exception:
                pass

    # Also check requirements/*.txt
    req_dir = workspace_path / "requirements"
    if req_dir.is_dir():
        for req_file in req_dir.glob("*.txt"):
            if req_file.is_file():
                rel_name = f"requirements/{req_file.name}"
                try:
                    for line in req_file.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line or line.startswith(("-", "#")):
                            continue
                        pkg, constraint = _parse_requirement_spec(line)
                        if pkg and pkg not in deps:
                            deps[pkg] = constraint or "*"
                    if rel_name not in sources:
                        sources.append(rel_name)
                except Exception:
                    pass

    # 3. setup.cfg
    setup_cfg = workspace_path / "setup.cfg"
    if setup_cfg.is_file():
        try:
            content = setup_cfg.read_text(encoding="utf-8", errors="replace")
            # Simple line-based section parsing for install_requires
            in_install = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    in_install = False
                elif "install_requires" in stripped and "=" in stripped:
                    in_install = True
                    rhs = stripped.split("=", 1)[1].strip()
                    if rhs:
                        pkg, constraint = _parse_requirement_spec(rhs)
                        if pkg and pkg not in deps:
                            deps[pkg] = constraint or "*"
                elif in_install:
                    if line.startswith((" ", "\t")) and stripped:
                        pkg, constraint = _parse_requirement_spec(stripped)
                        if pkg and pkg not in deps:
                            deps[pkg] = constraint or "*"
                    else:
                        in_install = False
            if "setup.cfg" not in sources:
                sources.append("setup.cfg")
        except Exception:
            pass

    return DependencyManifest(
        dependencies=deps,
        python_requires=py_requires,
        manifest_sources=sources,
        raw_details=details,
    )


def find_related_workspace_files(
    workspace_path: Path,
    relative_file: str,
    max_results: int = 10,
) -> Dict[str, List[str]]:
    """Discover cross-file relationships for a given file (implementations, tests, dependencies).

    Handles patterns like:
    - tests/test_datastructures.py -> fastapi/datastructures.py
    - fastapi/datastructures.py -> tests/test_datastructures.py
    - any python file -> pyproject.toml, requirements.txt
    """
    clean_path = relative_file.replace("\\", "/").strip().lstrip("/")
    path_obj = Path(clean_path)
    stem = path_obj.stem
    ext = path_obj.suffix

    implementations: List[str] = []
    tests: List[str] = []
    dependencies: List[str] = []

    # Manifests are always relevant dependencies
    for manifest_name in ["pyproject.toml", "setup.cfg", "requirements.txt", "Pipfile"]:
        if (workspace_path / manifest_name).is_file():
            dependencies.append(manifest_name)

    is_test_file = (
        "test" in clean_path.lower()
        or stem.startswith("test_")
        or stem.endswith("_test")
    )

    if is_test_file:
        # Looking for corresponding implementations
        # If stem is 'test_datastructures' -> look for 'datastructures.py' or '*/datastructures.py'
        impl_stem = re.sub(r"^test_", "", stem)
        impl_stem = re.sub(r"_test$", "", impl_stem)

        for match_file in workspace_path.glob(f"**/{impl_stem}{ext}"):
            if match_file.is_file():
                rel = match_file.relative_to(workspace_path).as_posix()
                if "test" not in rel.lower() and rel not in implementations:
                    implementations.append(rel)
                    if len(implementations) >= max_results:
                        break
    else:
        # Looking for corresponding tests
        # If stem is 'datastructures' -> look for 'test_datastructures.py' or 'datastructures_test.py'
        test_patterns = [
            f"**/test_{stem}{ext}",
            f"**/{stem}_test{ext}",
            f"tests/**/{stem}{ext}",
        ]
        for pat in test_patterns:
            for match_file in workspace_path.glob(pat):
                if match_file.is_file():
                    rel = match_file.relative_to(workspace_path).as_posix()
                    if rel != clean_path and rel not in tests:
                        tests.append(rel)
                        if len(tests) >= max_results:
                            break

    return {
        "implementations": implementations,
        "tests": tests,
        "dependencies": dependencies,
    }
