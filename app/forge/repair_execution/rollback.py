"""Phase 12 M4: Workspace Rollback and Recovery Management.

Provides:
- Safe reversal of modifications within isolated repair workspaces.
- Exact verification that restored workspace matches baseline tree digest.
- Transition to PRESERVED_FOR_INSPECTION when rollback fails or recovery is required.
- Clear reporting of restored files and final rollback status.
"""

import os
from pathlib import Path
import shutil
from typing import Dict, List, Optional, Tuple

from forge.repository.protection import is_protected_name
from forge.repository.validation import assert_contained_path
from forge.repair_execution.schemas import (
    RepairWorkspace,
    RollbackStatus,
    WorkspaceStatus,
)
from forge.repair_execution.workspace import compute_tree_digest


class WorkspaceRollbackManager:
    """Manages rollback and recovery of isolated repair workspaces."""

    def __init__(self) -> None:
        pass

    def rollback(
        self,
        workspace: RepairWorkspace,
        modified_files: Optional[List[str]] = None,
        backups: Optional[Dict[str, bytes]] = None,
    ) -> Tuple[RollbackStatus, List[str], str]:
        """Roll back modifications in an isolated workspace to its baseline state.

        Args:
            workspace: Target RepairWorkspace to restore.
            modified_files: Optional explicit list of modified relative paths.
            backups: Optional in-memory backups of original file contents prior to patch.

        Returns:
            Tuple of (RollbackStatus, list_of_restored_files, post_rollback_tree_digest).
        """
        ws_root = Path(workspace.isolated_workspace_path).resolve()
        if not ws_root.exists() or not ws_root.is_dir():
            workspace.workspace_status = WorkspaceStatus.CORRUPTED
            return RollbackStatus.FAILED, [], ""

        restored_files: List[str] = []
        snap_path = Path(workspace.baseline_snapshot_path).resolve() if workspace.baseline_snapshot_path else None
        original_repo = Path(workspace.original_repository_reference).resolve()
        files_to_restore = set(modified_files or workspace.allowed_paths or [])

        try:
            def _assert_safe_ws_dest(rel_p: str) -> Path:
                cur = ws_root
                for part in Path(rel_p).parts:
                    cur = cur / part
                    if cur.is_symlink():
                        raise RuntimeError(f"Symlink detected along rollback path '{cur}'.")
                return assert_contained_path(ws_root / rel_p, ws_root)

            def _safe_write_file(dest: Path, data: bytes) -> None:
                if dest.is_symlink():
                    raise RuntimeError(f"Refusing rollback write to symlink '{dest}'.")
                if dest.exists():
                    st = os.lstat(dest)
                    if st.st_nlink > 1:
                        dest.unlink()
                oflags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(dest), oflags, 0o644)
                try:
                    with os.fdopen(fd, "wb", closefd=False) as fh:
                        fh.write(data)
                finally:
                    os.close(fd)

            # 1. Primary restoration: restore from immutable baseline snapshot captured at allocation
            if snap_path and snap_path.exists() and snap_path.is_dir():
                # Verify that baseline snapshot has not been corrupted or tampered with
                snap_digest = compute_tree_digest(snap_path)
                if snap_digest != workspace.baseline_tree_digest:
                    workspace.workspace_status = WorkspaceStatus.CORRUPTED
                    return RollbackStatus.FAILED, [], compute_tree_digest(ws_root)

                # 1a. Delete any files in workspace that did not exist in the baseline snapshot
                for root, dirs, files in os.walk(ws_root, followlinks=False):
                    dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]
                    for f in files:
                        if is_protected_name(f):
                            continue
                        ws_f = Path(root) / f
                        try:
                            rel_p = str(ws_f.relative_to(ws_root)).replace("\\", "/")
                        except ValueError:
                            continue
                        if ws_f.is_symlink():
                            ws_f.unlink()
                            restored_files.append(rel_p)
                            continue
                        _assert_safe_ws_dest(rel_p)
                        snap_f = snap_path / rel_p
                        if not snap_f.exists():
                            ws_f.unlink()
                            restored_files.append(rel_p)

                # 1b. Restore or replace any files from baseline snapshot whose content differs
                for root, dirs, files in os.walk(snap_path, followlinks=False):
                    dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]
                    for f in files:
                        if is_protected_name(f):
                            continue
                        snap_f = Path(root) / f
                        try:
                            rel_p = str(snap_f.relative_to(snap_path)).replace("\\", "/")
                        except ValueError:
                            continue
                        ws_f = _assert_safe_ws_dest(rel_p)
                        snap_bytes = snap_f.read_bytes()
                        ws_bytes = ws_f.read_bytes() if (ws_f.exists() and not ws_f.is_symlink() and ws_f.is_file()) else None
                        if ws_bytes != snap_bytes:
                            ws_f.parent.mkdir(parents=True, exist_ok=True)
                            _assert_safe_ws_dest(rel_p)
                            _safe_write_file(ws_f, snap_bytes)
                            restored_files.append(rel_p)

            # 2. Secondary fallback: restore from in-memory backups if available
            elif backups:
                for rel_path, original_bytes in backups.items():
                    norm_path = rel_path.replace("\\", "/").strip()
                    target_file = _assert_safe_ws_dest(norm_path)
                    if original_bytes is None:
                        if target_file.exists():
                            target_file.unlink()
                            restored_files.append(norm_path)
                    else:
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        _assert_safe_ws_dest(norm_path)
                        _safe_write_file(target_file, original_bytes)
                        restored_files.append(norm_path)
                    files_to_restore.discard(norm_path)

            # 3. Tertiary fallback: synchronize from original read-only repo
            elif original_repo.exists() and original_repo.is_dir():
                for rel_path in files_to_restore:
                    norm_path = rel_path.replace("\\", "/").strip()
                    orig_file = assert_contained_path(original_repo / norm_path, original_repo)
                    ws_file = _assert_safe_ws_dest(norm_path)

                    if orig_file.exists() and orig_file.is_file() and not orig_file.is_symlink():
                        ws_file.parent.mkdir(parents=True, exist_ok=True)
                        _assert_safe_ws_dest(norm_path)
                        _safe_write_file(ws_file, orig_file.read_bytes())
                        restored_files.append(norm_path)
                    elif ws_file.exists() or ws_file.is_symlink():
                        ws_file.unlink()
                        restored_files.append(norm_path)

            # 4. Compute post-rollback tree digest
            post_digest = compute_tree_digest(ws_root)

            # 5. Verify against baseline digest
            if post_digest == workspace.baseline_tree_digest:
                workspace.workspace_status = WorkspaceStatus.READY
                return RollbackStatus.SUCCEEDED, sorted(list(set(restored_files))), post_digest
            else:
                workspace.workspace_status = WorkspaceStatus.PRESERVED_FOR_INSPECTION
                return RollbackStatus.FAILED, sorted(list(set(restored_files))), post_digest

        except Exception:
            workspace.workspace_status = WorkspaceStatus.PRESERVED_FOR_INSPECTION
            try:
                curr_digest = compute_tree_digest(ws_root)
            except Exception:
                curr_digest = ""
            return RollbackStatus.FAILED, sorted(list(set(restored_files))), curr_digest
