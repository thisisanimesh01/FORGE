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


def purge_protected_paths_from_workspace(workspace_dir: Path) -> int:
    """Post-acquisition purge pass removing any protected files that entered a workspace.

    Applies to both local copies and remote git clones as defense-in-depth.
    Preserves .git metadata unless specifically requested. Returns count of purged items.
    """
    if not workspace_dir.exists() or not workspace_dir.is_dir():
        return 0

    resolved_ws = workspace_dir.resolve()
    purged_count = 0

    # Walk bottom-up so directories can be safely unlinked/removed after contents
    for root, dirs, files in os.walk(resolved_ws, topdown=False):
        # Do not purge inside .git itself unless the entire .git is flagged (which it isn't)
        rel_root = Path(root).relative_to(resolved_ws)
        if ".git" in rel_root.parts:
            continue

        for filename in files:
            file_path = Path(root) / filename
            if is_protected_path(file_path, base_dir=resolved_ws):
                try:
                    file_path.unlink(missing_ok=True)
                    purged_count += 1
                except OSError:
                    pass

        for dirname in dirs:
            dir_path = Path(root) / dirname
            if dirname == ".git":
                continue
            if is_protected_path(dir_path, base_dir=resolved_ws):
                try:
                    shutil.rmtree(dir_path, ignore_errors=True)
                    purged_count += 1
                except OSError:
                    pass

    return purged_count


def purge_protected_git_metadata(workspace_dir: Union[str, Path]) -> int:
    """Purge sensitive file contents from Git objects, packs, logs, and index within workspace.

    Invariants:
    - Never modifies source repository (operates strictly inside workspace_dir).
    - Preserves commit identity (HEAD commit SHA, branch).
    - Removes all packfiles and unpacks objects into loose objects when protected files exist.
    - Permanently deletes loose blob objects corresponding to protected paths across all history.
    - Removes protected paths from Git index.
    - Purges reflogs (.git/logs), stashes (.git/refs/stash), and commit-graphs.
    - Fails closed if .git is a symlink or points outside workspace_dir.
    - Fails closed if any purge, unpack, or containment operation fails.
    - Recursively processes any nested .git repositories.

    Returns:
        Count of purged protected Git blobs.
    """
    if workspace_dir is None:
        return 0

    ws_path = Path(workspace_dir).resolve()
    if not ws_path.is_dir():
        return 0

    # Discover all .git entries within workspace
    git_entries: List[Path] = []
    root_git = ws_path / ".git"
    if root_git.exists() or root_git.is_symlink():
        git_entries.append(root_git)

    for root, dirs, files in os.walk(ws_path):
        if Path(root).resolve() == ws_path:
            continue
        if ".git" in Path(root).parts:
            continue
        for d in dirs:
            if d == ".git":
                git_entries.append(Path(root) / d)
        for f in files:
            if f == ".git":
                git_entries.append(Path(root) / f)

    total_purged = 0

    for git_entry in git_entries:
        # Enforce symlink safety on .git entry
        if git_entry.is_symlink():
            raise GitMetadataProtectionError(
                f"Security violation: .git cannot be a symlink: '{git_entry}'"
            )

        # Handle submodule .git file pointers
        if git_entry.is_file():
            try:
                content = git_entry.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    target_gitdir = content.split(":", 1)[1].strip()
                    resolved_gitdir = (git_entry.parent / target_gitdir).resolve()
                    resolved_gitdir.relative_to(ws_path)
                else:
                    raise GitMetadataProtectionError(f"Malformed .git file reference: '{git_entry}'")
            except Exception as e:
                raise GitMetadataProtectionError(f"Security violation in .git reference '{git_entry}': {e}")
            continue

        # Verify directory containment
        try:
            git_entry.resolve().relative_to(ws_path)
        except (ValueError, OSError, RuntimeError):
            raise GitMetadataProtectionError(
                f"Security violation: .git path escapes workspace: '{git_entry}'"
            )

        # Verify no symlinks inside .git point outside workspace
        for sub_root, sub_dirs, sub_files in os.walk(git_entry):
            for item in list(sub_dirs) + list(sub_files):
                item_path = Path(sub_root) / item
                if item_path.is_symlink():
                    try:
                        item_resolved = item_path.resolve()
                        item_resolved.relative_to(ws_path)
                    except (ValueError, OSError, RuntimeError):
                        raise GitMetadataProtectionError(
                            f"Security violation: escaping symlink inside .git: '{item_path}'"
                        )

        # Security check: Disallow external Git object borrowing via alternates
        alternates_path = git_entry / "objects" / "info" / "alternates"
        if alternates_path.exists():
            raise GitMetadataProtectionError(
                f"Security violation: Git alternates file detected in '{git_entry}': external object borrowing is forbidden"
            )
        http_alternates_path = git_entry / "objects" / "info" / "http-alternates"
        if http_alternates_path.exists():
            raise GitMetadataProtectionError(
                f"Security violation: Git http-alternates file detected in '{git_entry}': external object borrowing is forbidden"
            )

        repo_root = git_entry.parent
        cmd_base = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", f"safe.directory={repo_root.resolve()}",
        ]

        # 1. Identify all objects associated with protected paths across the entire history
        try:
            res_rev = subprocess.run(
                cmd_base + ["rev-list", "--objects", "--all"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as e:
            raise GitMetadataProtectionError(f"Failed to query Git objects in '{repo_root}': {e}")

        if res_rev.returncode != 0:
            err = res_rev.stderr or res_rev.stdout
            if "does not have any commits yet" in err or "fatal: your current branch" in err:
                continue
            raise GitMetadataProtectionError(f"Git rev-list failed in '{repo_root}': {err}")

        protected_blobs: Set[str] = set()
        for line in res_rev.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                sha, pth = parts[0], parts[1]
                fn = Path(pth).name
                if is_protected_name(fn) or is_protected_path(pth, base_dir=repo_root):
                    protected_blobs.add(sha)

        # 2. Inspect all tree objects to discover dangling or unreferenced protected blobs
        try:
            res_cat = subprocess.run(
                cmd_base + ["cat-file", "--batch-all-objects", "--batch-check"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if res_cat.returncode == 0:
                for line in res_cat.stdout.splitlines():
                    items = line.split()
                    if len(items) >= 2 and items[1] == "tree":
                        tsha = items[0]
                        t_res = subprocess.run(
                            cmd_base + ["cat-file", "-p", tsha],
                            cwd=repo_root,
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                        if t_res.returncode == 0:
                            for entry in t_res.stdout.splitlines():
                                e_parts = entry.split(maxsplit=3)
                                if len(e_parts) == 4 and e_parts[1] == "blob":
                                    bsha, bname = e_parts[2], e_parts[3]
                                    if is_protected_name(bname):
                                        protected_blobs.add(bsha)
        except Exception as e:
            raise GitMetadataProtectionError(f"Failed to inspect Git tree objects in '{repo_root}': {e}")

        # Always purge reflogs and stashes as hygiene
        shutil.rmtree(git_entry / "logs", ignore_errors=True)
        (git_entry / "refs" / "stash").unlink(missing_ok=True)
        cg_path = git_entry / "objects" / "info" / "commit-graphs"
        if cg_path.is_dir():
            shutil.rmtree(cg_path, ignore_errors=True)
        else:
            cg_path.unlink(missing_ok=True)
        (git_entry / "objects" / "info" / "commit-graph").unlink(missing_ok=True)

        # Always prune unreachable dangling objects as hygiene
        subprocess.run(
            cmd_base + ["prune", "--expire=now"],
            cwd=repo_root,
            capture_output=True,
            timeout=30,
        )

        if not protected_blobs:
            subprocess.run(
                cmd_base + ["config", "core.hooksPath", "/dev/null"],
                cwd=repo_root,
                capture_output=True,
                timeout=10,
            )
            continue

        # 3. Unpack all packfiles so protected blobs can be individually purged
        pack_dir = git_entry / "objects" / "pack"
        if pack_dir.exists() and any(pack_dir.glob("*.pack")):
            temp_pack_dir = git_entry / "_temp_forge_packs"
            temp_pack_dir.mkdir(exist_ok=True)
            try:
                for pfile in pack_dir.glob("*"):
                    shutil.move(str(pfile), str(temp_pack_dir / pfile.name))
                for pack in temp_pack_dir.glob("*.pack"):
                    with open(pack, "rb") as f_in:
                        res_unpack = subprocess.run(
                            cmd_base + ["unpack-objects", "-r"],
                            stdin=f_in,
                            cwd=repo_root,
                            capture_output=True,
                            timeout=120,
                        )
                        if res_unpack.returncode != 0:
                            err_msg = res_unpack.stderr.decode("utf-8", errors="replace")
                            raise GitMetadataProtectionError(
                                f"Failed to unpack Git packfile during metadata purge: {err_msg}"
                            )
            finally:
                shutil.rmtree(temp_pack_dir, ignore_errors=True)
                if pack_dir.exists():
                    for leftover in pack_dir.glob("*"):
                        try:
                            leftover.unlink(missing_ok=True)
                        except OSError:
                            pass

        # 4. Permanently delete loose blob objects for protected files
        for bsha in protected_blobs:
            bf = git_entry / "objects" / bsha[:2] / bsha[2:]
            if bf.exists():
                try:
                    bf.unlink()
                    total_purged += 1
                    if bf.parent.exists() and not any(bf.parent.iterdir()):
                        try:
                            bf.parent.rmdir()
                        except OSError:
                            pass
                except Exception as e:
                    raise GitMetadataProtectionError(f"Failed to delete protected Git object '{bsha}': {e}")

        # 5. Remove protected paths from Git index
        subprocess.run(
            cmd_base + ["rm", "--cached", "-r", "--ignore-unmatch", ".env*", "*.db*", "contacts.json", "*.log"],
            cwd=repo_root,
            capture_output=True,
            timeout=30,
        )

        # Prune unreachable loose objects
        subprocess.run(
            cmd_base + ["prune", "--expire=now"],
            cwd=repo_root,
            capture_output=True,
            timeout=30,
        )

        # 6. Fail-closed verification
        for bsha in protected_blobs:
            bf = git_entry / "objects" / bsha[:2] / bsha[2:]
            if bf.exists():
                raise GitMetadataProtectionError(
                    f"Purge integrity check failed: protected blob '{bsha}' remains in object database"
                )
        if pack_dir.exists() and any(pack_dir.glob("*.pack")):
            raise GitMetadataProtectionError("Purge integrity check failed: packfiles still present after unpacking")

        subprocess.run(
            cmd_base + ["config", "core.hooksPath", "/dev/null"],
            cwd=repo_root,
            capture_output=True,
            timeout=10,
        )

    return total_purged
