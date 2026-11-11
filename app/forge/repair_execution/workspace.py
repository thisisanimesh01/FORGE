"""Phase 12 M4: Isolated Repair Workspace Lifecycle Management.

Guarantees:
- Original repository is strictly read-only and never modified.
- Repair operations occur exclusively inside an isolated workspace.
- Captures baseline commit SHA and SHA-256 baseline tree digest.
- Path protection and containment enforced (symlink escape, protected files blocked).
- Sensitive metadata and credentials (.env, .git/config, keys) are inaccessible.
- Clean and safe lifecycle: create -> isolate -> verify -> cleanup/preserve.
"""

import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Dict, List, Optional, Set, Tuple
import uuid

from forge.config import get_settings
from forge.repository.protection import is_protected_name, is_protected_path, purge_protected_paths_from_workspace
from forge.repository.validation import assert_contained_path, SecurityError
from forge.repair_execution.schemas import RepairWorkspace, WorkspaceStatus


def _safe_copy_tree(src_dir: Path, dst_dir: Path) -> None:
    """Safely copy directory tree, skipping special files, escaping symlinks, and protected paths."""
    src_dir = src_dir.resolve()
    dst_dir = dst_dir.resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src_dir, followlinks=False):
        # Prune VCS and protected directories
        dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]
        rel_root = Path(root).relative_to(src_dir)
        target_dir = dst_dir / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)

        for file_name in sorted(files):
            if is_protected_name(file_name):
                continue
            src_file = Path(root) / file_name

            # Check special files (FIFO, socket, character/block device)
            try:
                st = os.lstat(src_file)
                if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                    continue
            except OSError:
                continue

            # Reject all symlinks cleanly inside isolated repair workspaces (prevents symlink chains/TOCTOU)
            if stat.S_ISLNK(st.st_mode) or src_file.is_symlink():
                continue

            rel_path = str((rel_root / file_name)).replace("\\", "/")
            if is_protected_path(rel_path, base_dir=src_dir):
                continue

            dst_file = target_dir / file_name
            try:
                if stat.S_ISREG(st.st_mode):
                    shutil.copy2(src_file, dst_file, follow_symlinks=False)
            except Exception:
                continue


def compute_tree_digest(workspace_path: Path, allowed_paths: Optional[List[str]] = None) -> str:
    """Compute a deterministic SHA-256 hash across workspace files and contents."""
    resolved_ws = workspace_path.resolve()
    hasher = hashlib.sha256()

    file_hashes: List[Tuple[str, str]] = []

    for root, dirs, files in os.walk(resolved_ws, followlinks=False):
        # Skip git internal metadata and hidden VCS dirs
        dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]

        for file_name in sorted(files):
            if is_protected_name(file_name):
                continue

            file_path = Path(root) / file_name

            # Skip special files and escaping symlinks
            try:
                st = os.lstat(file_path)
                if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                    continue
            except OSError:
                continue

            if file_path.is_symlink():
                try:
                    resolved = file_path.resolve()
                    if not resolved.is_relative_to(resolved_ws):
                        continue
                except (ValueError, RuntimeError, OSError):
                    continue

            try:
                rel_path = str(file_path.relative_to(resolved_ws)).replace("\\", "/")
            except ValueError:
                continue

            if is_protected_path(rel_path, base_dir=resolved_ws):
                continue

            if allowed_paths is not None and rel_path not in allowed_paths:
                pass

            try:
                content = file_path.read_bytes()
                f_hash = hashlib.sha256(content).hexdigest()
                file_hashes.append((rel_path, f_hash))
            except Exception:
                continue

    file_hashes.sort(key=lambda x: x[0])
    for rel_p, f_h in file_hashes:
        hasher.update(f"{rel_p}:{f_h}\n".encode("utf-8"))

    return hasher.hexdigest()


import threading


def _set_directory_read_only(path: Path) -> None:
    """Recursively set read-only permissions on files and directories in snapshot."""
    for root, dirs, files in os.walk(path, followlinks=False):
        for f in files:
            p = Path(root) / f
            try:
                p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass
        for d in dirs:
            p = Path(root) / d
            try:
                p.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            except OSError:
                pass
    try:
        path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    except OSError:
        pass


def _set_directory_writable(path: Path) -> None:
    """Recursively restore write permissions so directory can be removed."""
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass
    for root, dirs, files in os.walk(path, followlinks=False):
        for f in files:
            p = Path(root) / f
            try:
                p.chmod(stat.S_IRWXU)
            except OSError:
                pass
        for d in dirs:
            p = Path(root) / d
            try:
                p.chmod(stat.S_IRWXU)
            except OSError:
                pass


class RepairWorkspaceManager:
    """Manages the lifecycle of isolated repair workspaces."""

    def __init__(
        self,
        original_repo_path: Path,
        base_workspaces_dir: Optional[Path] = None,
    ) -> None:
        self.original_repo_path = original_repo_path.resolve()
        if not self.original_repo_path.exists() or not self.original_repo_path.is_dir():
            raise ValueError(f"Original repository path does not exist or is not a directory: '{self.original_repo_path}'")

        if base_workspaces_dir:
            self.base_workspaces_dir = base_workspaces_dir.resolve()
        else:
            settings = get_settings()
            self.base_workspaces_dir = (settings.workspace_dir / "repair_workspaces").resolve()

        self.base_workspaces_dir.mkdir(parents=True, exist_ok=True)
        self._workspaces: Dict[str, RepairWorkspace] = {}
        self._lock = threading.Lock()

    def allocate_workspace(
        self,
        allowed_paths: List[str],
        plan_id: str,
        baseline_commit: Optional[str] = None,
    ) -> RepairWorkspace:
        """Create an isolated workspace initialized with clean repository files.

        Original repository is never modified.
        """
        with self._lock:
            workspace_id = None
            for _ in range(10):
                cand = f"rws_{uuid.uuid4().hex[:12]}"
                cand_ws = self.base_workspaces_dir / cand
                cand_snap = self.base_workspaces_dir / f"{cand}_baseline"
                if not cand_ws.exists() and not cand_snap.exists():
                    workspace_id = cand
                    break

            if not workspace_id:
                raise WorkspaceError("Failed to allocate unique workspace directory: namespace collision.")

            isolated_dir = self.base_workspaces_dir / workspace_id
            snapshot_dir = self.base_workspaces_dir / f"{workspace_id}_baseline"

            # Safely copy files into isolated directory and baseline snapshot
            _safe_copy_tree(src_dir=self.original_repo_path, dst_dir=isolated_dir)
            _safe_copy_tree(src_dir=self.original_repo_path, dst_dir=snapshot_dir)

            # Deep purge of any nested protected files
            purge_protected_paths_from_workspace(isolated_dir)
            purge_protected_paths_from_workspace(snapshot_dir)

            # Compute clean baseline tree digest before write-protecting snapshot
            baseline_digest = compute_tree_digest(isolated_dir)

            # Write-protect baseline snapshot directory to prevent accidental or malicious tampering
            _set_directory_read_only(snapshot_dir)

            # Baseline commit resolution
            resolved_commit = baseline_commit or "baseline_unversioned"
            git_dir = self.original_repo_path / ".git"
            if git_dir.exists() and git_dir.is_dir() and baseline_commit is None:
                head_file = git_dir / "HEAD"
                if head_file.exists():
                    try:
                        ref_text = head_file.read_text().strip()
                        if ref_text.startswith("ref: "):
                            ref_path = git_dir / ref_text[5:]
                            if ref_path.exists():
                                resolved_commit = ref_path.read_text().strip()
                        elif len(ref_text) == 40:
                            resolved_commit = ref_text
                    except Exception:
                        pass

            workspace = RepairWorkspace(
                workspace_id=workspace_id,
                original_repository_reference=str(self.original_repo_path),
                isolated_workspace_path=str(isolated_dir),
                baseline_commit=resolved_commit,
                baseline_tree_digest=baseline_digest,
                baseline_snapshot_path=str(snapshot_dir),
                allowed_paths=allowed_paths,
                workspace_status=WorkspaceStatus.READY,
            )

            self._workspaces[workspace_id] = workspace
            return workspace

    def get_workspace(self, workspace_id: str) -> Optional[RepairWorkspace]:
        """Retrieve workspace record by ID."""
        with self._lock:
            return self._workspaces.get(workspace_id)

    def verify_workspace_containment(self, workspace: RepairWorkspace, relative_path: str) -> Path:
        """Assert that a target relative path is safely contained in the isolated workspace.

        Raises:
            SecurityError: If path attempts to escape the isolated workspace.
        """
        norm_path = relative_path.replace("\\", "/").strip()
        ws_root = Path(workspace.isolated_workspace_path).resolve()
        target = ws_root / norm_path
        return assert_contained_path(target, ws_root)

    def cleanup_workspace(self, workspace_id: str, force: bool = False) -> bool:
        """Safely destroy an isolated workspace unless preserved for inspection."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if not workspace:
                return False

            if workspace.workspace_status == WorkspaceStatus.PRESERVED_FOR_INSPECTION and not force:
                return False

            ws_dir = Path(workspace.isolated_workspace_path).resolve()
            try:
                assert_contained_path(ws_dir, self.base_workspaces_dir)
                if ws_dir.exists() and ws_dir.is_dir():
                    _set_directory_writable(ws_dir)
                    shutil.rmtree(ws_dir, ignore_errors=True)
            except Exception:
                return False

            if workspace.baseline_snapshot_path:
                snap_dir = Path(workspace.baseline_snapshot_path).resolve()
                try:
                    assert_contained_path(snap_dir, self.base_workspaces_dir)
                    if snap_dir.exists() and snap_dir.is_dir():
                        _set_directory_writable(snap_dir)
                        shutil.rmtree(snap_dir, ignore_errors=True)
                except Exception:
                    pass

            workspace.workspace_status = WorkspaceStatus.CLEANED_UP
            workspace.cleanup_status = "cleaned_up"
            return True

    def preserve_for_inspection(self, workspace_id: str) -> bool:
        """Mark workspace as preserved for inspection when rollback fails or recovery is required."""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if not workspace:
                return False
            workspace.workspace_status = WorkspaceStatus.PRESERVED_FOR_INSPECTION
            workspace.cleanup_status = "preserved"
            return True
