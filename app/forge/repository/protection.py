"""Centralized protection policy for sensitive repository files.

Invariants:
- Case-insensitive pattern matching on all platforms.
- Deep nested directory inspection across all path components.
- Symlink-aware inspection checking both link source and resolved target.
- Fail-closed behavior on unresolvable, cyclic, or inaccessible paths.
- Zero file-content reads during path classification (operates purely on paths/metadata).
- Reusable APIs for single paths, batch collections, and tree traversals.
"""

from fnmatch import fnmatchcase
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Iterable, List, Optional, Set, Tuple, Union


class GitMetadataProtectionError(Exception):
    """Raised when Git metadata fails protection or containment checks."""
    pass


PROTECTED_FILE_PATTERNS: Tuple[str, ...] = (
    ".env*",
    "*.db*",
    "contacts.json",
    "*.log",
)


def _matches_protected_component(component: str) -> bool:
    """Return whether a path component matches a protected filename pattern (case-insensitive)."""
    normalized = component.casefold()
    return any(
        fnmatchcase(normalized, pattern.casefold())
        for pattern in PROTECTED_FILE_PATTERNS
    )


def is_protected_name(name: str) -> bool:
    """Check if an individual filename or directory name matches protected patterns."""
    if not name or not str(name).strip():
        return False
    return _matches_protected_component(str(name).strip())


def is_protected_path(
    path: Union[str, Path],
    base_dir: Optional[Union[str, Path]] = None,
) -> bool:
    """Identify protected paths, including nested paths and resolved symlink targets.

    This function does NOT read file contents under any circumstances.
    Guarantees fail-closed evaluation on path resolution failures or external escaping symlinks.

    Args:
        path: File or directory path to check.
        base_dir: Optional base directory to enforce containment. If provided and
            the resolved path escapes base_dir, it is treated as protected/forbidden.

    Returns:
        True if the path is protected or cannot be safely verified; False otherwise.
    """
    if path is None:
        return True

    raw_str = str(path).strip()
    if not raw_str:
        return True

    candidate = Path(path)

    # 1. Lexical check of all path components
    lexical_parts = candidate.parts
    if any(_matches_protected_component(part) for part in lexical_parts):
        return True

    # Anchor candidate to base_dir if relative and base_dir is supplied
    if base_dir is not None and not candidate.is_absolute():
        candidate_target = Path(base_dir) / candidate
    else:
        candidate_target = candidate

    # 2. Check raw symlink target before full resolution if it is a symlink
    try:
        if candidate_target.is_symlink():
            try:
                link_target = os.readlink(candidate_target)
                target_parts = Path(link_target).parts
                if any(_matches_protected_component(part) for part in target_parts):
                    return True
            except (OSError, RuntimeError):
                # Fail closed on broken / cyclic symlink
                return True
    except (OSError, RuntimeError):
        return True

    # 3. Full path resolution (checks canonical target for symlinks and traversal)
    try:
        resolved = candidate_target.resolve(strict=False)
        resolved_parts = resolved.parts
    except (OSError, RuntimeError):
        # Fail closed when a path cannot be resolved safely.
        return True

    if any(_matches_protected_component(part) for part in resolved_parts):
        return True

    # 4. Containment check against base_dir if supplied
    if base_dir is not None:
        try:
            resolved_base = Path(base_dir).resolve(strict=False)
            resolved.relative_to(resolved_base)
        except (ValueError, OSError, RuntimeError):
            # Escaping symlink or path outside base_dir: fail closed
            return True

    return False


def filter_unprotected_paths(
    paths: Iterable[Union[str, Path]],
    base_dir: Optional[Union[str, Path]] = None,
) -> List[Path]:
    """Filter an iterable of paths, returning only those that are not protected."""
    result: List[Path] = []
    for p in paths:
        candidate = Path(p)
        if not is_protected_path(candidate, base_dir=base_dir):
            result.append(candidate)
    return result


def create_copytree_ignore(
    base_src: Path,
    extra_ignores: Optional[Set[str]] = None,
) -> Callable[[str, List[str]], Set[str]]:
    """Create an ignore callable for shutil.copytree enforcing protected boundaries.

    Ensures that protected files, directories matching protected patterns,
    cache directories, and external/protected symlinks are never copied.
    """
    standard_ignores = {
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    if extra_ignores:
        standard_ignores.update(extra_ignores)

    resolved_base = base_src.resolve()

    def ignore_callable(dir_path: str, names: List[str]) -> Set[str]:
        ignored: Set[str] = set()
        curr = Path(dir_path)
        for name in names:
            if name in standard_ignores:
                ignored.add(name)
                continue

            item = curr / name
            if is_protected_path(item, base_dir=resolved_base):
                ignored.add(name)
                continue

            if item.is_symlink():
                try:
                    resolved_target = item.resolve()
                    resolved_target.relative_to(resolved_base)
                    if is_protected_path(resolved_target, base_dir=resolved_base):
                        ignored.add(name)
                except (ValueError, RuntimeError, OSError):
                    # Points outside resolved_base or resolution failed
                    ignored.add(name)

        return ignored

    return ignore_callable


def verify_workspace_sanitized(workspace_dir: Union[str, Path]) -> None:
    """Independently walk the workspace and verify that zero protected paths remain.

    Invariants:
    - Never follows symlinks outside the workspace (followlinks=False).
    - Preserves only the analysis workspace's root .git directory.
    - Rejects any nested .git directories, nested .git files, or nested .git symlinks.
    - Rejects a symlinked or regular-file root .git.
    - Detects protected regular files, directories, and symlinks.
    - Detects paths whose symlink resolution escapes the workspace or points to protected files.
    - Fails closed on any traversal or resolution error.
    - Never reads or hashes protected file contents.
    - Preserves legitimate ordinary files and directories.
    """
    if workspace_dir is None:
        raise GitMetadataProtectionError("Workspace directory cannot be None for sanitization verification.")

    ws_path = Path(workspace_dir).resolve()
    if not ws_path.is_dir():
        raise GitMetadataProtectionError(f"Workspace directory does not exist or is not a directory: '{ws_path}'")

    root_git = ws_path / ".git"
    if root_git.is_symlink():
        raise GitMetadataProtectionError(
            f"Security violation: .git cannot be a symlink in analysis workspace: '{root_git}'"
        )
    if root_git.is_file():
        raise GitMetadataProtectionError(
            f"Security violation: root .git cannot be a file in analysis workspace: '{root_git}'"
        )

    def _on_walk_error(err: OSError):
        raise GitMetadataProtectionError(f"Post-purge verification traversal failed: {err}") from err

    for root, dirs, files in os.walk(ws_path, topdown=True, followlinks=False, onerror=_on_walk_error):
        current_path = Path(root).resolve()
        is_root = (current_path == ws_path)

        if is_root:
            if ".git" in dirs:
                # Legitimate root analysis .git directory: preserve and do not descend into it
                dirs.remove(".git")
            if ".git" in files:
                raise GitMetadataProtectionError(
                    f"Security violation: root .git cannot be a file in analysis workspace: '{ws_path / '.git'}'"
                )
        else:
            rel_root = current_path.relative_to(ws_path)
            if ".git" in rel_root.parts:
                raise GitMetadataProtectionError(
                    f"Security violation: nested Git metadata directory detected in workspace: '{root}'"
                )

            if ".git" in dirs:
                nested_git = Path(root) / ".git"
                raise GitMetadataProtectionError(
                    f"Security violation: nested Git directory detected in workspace: '{nested_git}'"
                )

            if ".git" in files:
                nested_git = Path(root) / ".git"
                raise GitMetadataProtectionError(
                    f"Security violation: nested Git file detected in workspace: '{nested_git}'"
                )

        for d in dirs:
            d_path = Path(root) / d
            if is_protected_name(d) or is_protected_path(d_path, base_dir=ws_path):
                raise GitMetadataProtectionError(
                    f"Post-purge verification failed: protected directory or symlink '{d}' detected in workspace."
                )

        for f in files:
            f_path = Path(root) / f
            if is_protected_name(f) or is_protected_path(f_path, base_dir=ws_path):
                raise GitMetadataProtectionError(
                    f"Post-purge verification failed: protected file or symlink '{f}' detected in workspace."
                )


def purge_protected_paths_from_workspace(workspace_dir: Union[str, Path]) -> int:
    """Post-acquisition purge pass removing any protected files that entered a workspace.

    Applies to both local copies and remote git clones as defense-in-depth.
    Preserves only the analysis workspace's root .git directory.
    Fails closed if any deletion fails, if traversal fails, if nested Git metadata
    is encountered, or if protected paths remain post-purge.

    Returns:
        Count of purged items.
    """
    if workspace_dir is None:
        return 0

    ws_path = Path(workspace_dir).resolve()
    if not ws_path.is_dir():
        return 0

    root_git = ws_path / ".git"
    if root_git.is_symlink():
        raise GitMetadataProtectionError(
            f"Security violation: .git cannot be a symlink in analysis workspace: '{root_git}'"
        )
    if root_git.is_file():
        raise GitMetadataProtectionError(
            f"Security violation: root .git cannot be a file in analysis workspace: '{root_git}'"
        )

    purged_count = 0

    def _on_walk_error(err: OSError):
        raise GitMetadataProtectionError(f"Workspace traversal failed during sanitization purge: {err}") from err

    # Walk bottom-up (followlinks=False) so child files and subdirectories are removed before parent directories
    for root, dirs, files in os.walk(ws_path, topdown=False, followlinks=False, onerror=_on_walk_error):
        current_path = Path(root).resolve()
        rel_root = current_path.relative_to(ws_path)

        # If inside the root analysis .git directory (rel_root.parts[0] == ".git"), preserve it
        if rel_root.parts and rel_root.parts[0] == ".git":
            continue

        # If inside a nested .git directory, fail closed immediately
        if ".git" in rel_root.parts:
            raise GitMetadataProtectionError(
                f"Security violation: nested Git metadata directory detected in workspace: '{root}'"
            )

        is_root = (current_path == ws_path)

        for filename in files:
            file_path = Path(root) / filename
            if filename == ".git":
                if is_root:
                    raise GitMetadataProtectionError(
                        f"Security violation: root .git cannot be a file in analysis workspace: '{file_path}'"
                    )
                else:
                    raise GitMetadataProtectionError(
                        f"Security violation: nested Git file detected in workspace: '{file_path}'"
                    )

            if is_protected_path(file_path, base_dir=ws_path):
                try:
                    file_path.unlink()
                    purged_count += 1
                except Exception as e:
                    raise GitMetadataProtectionError(
                        f"Security failure: unable to delete protected file '{file_path.name}' from workspace: {e}"
                    ) from e
                if file_path.exists() or file_path.is_symlink():
                    raise GitMetadataProtectionError(
                        f"Security failure: protected file '{file_path.name}' remains in workspace after deletion attempt."
                    )

        for dirname in dirs:
            dir_path = Path(root) / dirname
            if dirname == ".git":
                if is_root:
                    if dir_path.is_symlink():
                        raise GitMetadataProtectionError(
                            f"Security violation: .git cannot be a symlink in analysis workspace: '{dir_path}'"
                        )
                    # Legitimate root analysis .git directory: preserved
                    continue
                else:
                    raise GitMetadataProtectionError(
                        f"Security violation: nested Git directory detected in workspace: '{dir_path}'"
                    )

            if dir_path.is_symlink():
                if is_protected_path(dir_path, base_dir=ws_path):
                    try:
                        dir_path.unlink()
                        purged_count += 1
                    except Exception as e:
                        raise GitMetadataProtectionError(
                            f"Security failure: unable to delete protected directory symlink '{dir_path.name}' from workspace: {e}"
                        ) from e
                    if dir_path.exists() or dir_path.is_symlink():
                        raise GitMetadataProtectionError(
                            f"Security failure: protected directory symlink '{dir_path.name}' remains in workspace after deletion attempt."
                        )
            elif is_protected_path(dir_path, base_dir=ws_path):
                try:
                    shutil.rmtree(dir_path)
                    purged_count += 1
                except Exception as e:
                    raise GitMetadataProtectionError(
                        f"Security failure: unable to delete protected directory '{dir_path.name}' from workspace: {e}"
                    ) from e
                if dir_path.exists() or dir_path.is_symlink():
                    raise GitMetadataProtectionError(
                        f"Security failure: protected directory '{dir_path.name}' remains in workspace after deletion attempt."
                    )

    # Independent post-purge verification pass
    verify_workspace_sanitized(ws_path)

    return purged_count
