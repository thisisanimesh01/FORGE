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
                        ws_f = ws_root / rel_p
                        try:
                            if not ws_f.exists() or ws_f.read_bytes() != snap_f.read_bytes():
                                ws_f.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(snap_f, ws_f)
                                restored_files.append(rel_p)
                        except Exception:
                            continue

            # 2. Secondary fallback: restore from in-memory backups if available
            elif backups:
                for rel_path, original_bytes in backups.items():
                    norm_path = rel_path.replace("\\", "/").strip()
                    target_file = assert_contained_path(ws_root / norm_path, ws_root)
                    if original_bytes is None:
                        if target_file.exists():
                            target_file.unlink()
                            restored_files.append(norm_path)
                    else:
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        target_file.write_bytes(original_bytes)
                        restored_files.append(norm_path)
                    files_to_restore.discard(norm_path)

            # 3. Tertiary fallback: synchronize from original read-only repo
            elif original_repo.exists() and original_repo.is_dir():
                for rel_path in files_to_restore:
                    norm_path = rel_path.replace("\\", "/").strip()
                    orig_file = original_repo / norm_path
                    ws_file = assert_contained_path(ws_root / norm_path, ws_root)

                    if orig_file.exists() and orig_file.is_file():
                        ws_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(orig_file, ws_file)
                        restored_files.append(norm_path)
                    elif ws_file.exists():
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
