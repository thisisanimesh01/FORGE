"""Repository framework, structure, and configuration detector."""

import os
from pathlib import Path
from typing import Dict, List, Set
from pydantic import BaseModel, Field

from forge.repository.validation import assert_contained_path


class ProjectIntelligence(BaseModel):
    """Discovered structure and properties of the repository."""
    is_python: bool = False
    detected_frameworks: List[str] = Field(default_factory=list)
    configuration_files: List[str] = Field(default_factory=list)
    entrypoints: List[str] = Field(default_factory=list)
    test_directories: List[str] = Field(default_factory=list)
    package_directories: List[str] = Field(default_factory=list)
    instruction_files: List[str] = Field(default_factory=list)
    total_files: int = 0
    total_python_files: int = 0
    total_lines: int = 0
    file_type_breakdown: Dict[str, int] = Field(default_factory=dict)


EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "ENV",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".tox",
    ".eggs",
}

KNOWN_FRAMEWORK_SIGNATURES: Dict[str, List[str]] = {
    "pytest": ["pytest", "pytest.ini", "tests/conftest.py"],
    "django": ["django", "manage.py", "settings.py", "wsgi.py"],
    "fastapi": ["fastapi", "uvicorn"],
    "flask": ["flask"],
    "pydantic": ["pydantic"],
    "typer": ["typer"],
    "celery": ["celery"],
    "sqlalchemy": ["sqlalchemy", "alembic"],
    "httpx": ["httpx"],
    "requests": ["requests"],
}

CONFIG_FILENAMES: Set[str] = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "pytest.ini",
    "tox.ini",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    ".pylintrc",
    ".flake8",
    "ruff.toml",
}

INSTRUCTION_FILENAMES: Set[str] = {
    "AGENTS.md",
    "README.md",
    "README.rst",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "DEVELOPMENT.md",
}


def detect_repository(
    repo_root: Path,
    max_files: int = 1000,
    max_file_size_bytes: int = 500_000,
) -> ProjectIntelligence:
    """Safely traverse and inspect repository layout without executing code."""
    root = repo_root.resolve()
    intel = ProjectIntelligence()

    frameworks_detected: Set[str] = set()
    config_files: List[str] = []
    entrypoints: List[str] = []
    test_dirs: Set[str] = set()
    package_dirs: Set[str] = set()
    instruction_files: List[str] = []
    file_types: Dict[str, int] = {}
    config_contents_text = ""

    scanned_count = 0

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)

        # Enforce containment
        assert_contained_path(dirpath, root)

        # Prune excluded directories
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")]

        rel_dir = dirpath.relative_to(root)
        rel_dir_str = str(rel_dir)

        # Check for package dirs
        if "__init__.py" in filenames and rel_dir_str != ".":
            package_dirs.add(rel_dir_str)

        # Check for test directories
        if rel_dir_str in ["tests", "test"] or rel_dir_str.startswith("tests/") or rel_dir_str.startswith("test/"):
            test_dirs.add(rel_dir_str)

        for filename in filenames:
            scanned_count += 1
            if scanned_count > max_files:
                break

            file_path = dirpath / filename
            # Prevent following symlinks outside root
            try:
                resolved_file = assert_contained_path(file_path, root)
            except Exception:
                continue

            rel_file_str = str(resolved_file.relative_to(root))
            ext = file_path.suffix.lower() or "<no_ext>"
            file_types[ext] = file_types.get(ext, 0) + 1

            # Match instruction files
            if filename in INSTRUCTION_FILENAMES:
                instruction_files.append(rel_file_str)

            # Match configuration files
            if filename in CONFIG_FILENAMES or filename.startswith("requirements"):
                config_files.append(rel_file_str)
                # Read config file to detect framework mentions (size capped)
                try:
                    if file_path.stat().st_size <= max_file_size_bytes:
                        config_contents_text += "\n" + file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    pass

            # Match Python files
            if filename.endswith(".py"):
                intel.total_python_files += 1
                if filename in ["main.py", "app.py", "cli.py", "manage.py", "wsgi.py", "__main__.py"]:
                    entrypoints.append(rel_file_str)
                # Count lines safely
                try:
                    if file_path.stat().st_size <= max_file_size_bytes:
                        line_count = len(file_path.read_text(encoding="utf-8", errors="ignore").splitlines())
                        intel.total_lines += line_count
                except Exception:
                    pass

        if scanned_count > max_files:
            break

    # Analyze framework signatures
    lower_config = config_contents_text.lower()
    for framework, signatures in KNOWN_FRAMEWORK_SIGNATURES.items():
        if any(sig in lower_config for sig in signatures):
            frameworks_detected.add(framework)

    intel.is_python = intel.total_python_files > 0 or "pyproject.toml" in [Path(f).name for f in config_files]
    intel.detected_frameworks = sorted(list(frameworks_detected))
    intel.configuration_files = sorted(config_files)
    intel.entrypoints = sorted(entrypoints)
    intel.test_directories = sorted(list(test_dirs))
    intel.package_directories = sorted(list(package_dirs))
    intel.instruction_files = sorted(instruction_files)
    intel.total_files = scanned_count
    intel.file_type_breakdown = file_types

    return intel
