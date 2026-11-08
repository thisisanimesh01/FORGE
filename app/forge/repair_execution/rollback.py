"""Phase 12 M4: Workspace Rollback and Recovery Management.

Provides:
- Safe reversal of modifications within isolated repair workspaces.
- Exact verification that restored workspace matches baseline tree digest.
- Transition to PRESERVED_FOR_INSPECTION when rollback fails or recovery is required.
- Clear reporting of restored files and final rollback status.
"""

from pathlib import Path
import shutil
from typing import Dict, List, Optional, Tuple

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
        original_repo = Path(workspace.original_repository_reference).resolve()
        files_to_restore = set(modified_files or workspace.allowed_paths or [])

        try:
            # 1. Restore from in-memory backups if available
            if backups:
                for rel_path, original_bytes in backups.items():
                    norm_path = rel_path.replace("\\", "/").strip()
                    target_file = assert_contained_path(ws_root / norm_path, ws_root)
                    if original_bytes is None:
                        # File was created during patch; delete it
                        if target_file.exists():
                            target_file.unlink()
                            restored_files.append(norm_path)
                    else:
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        target_file.write_bytes(original_bytes)
                        restored_files.append(norm_path)
                    files_to_restore.discard(norm_path)

            # 2. For any remaining files, synchronize from original read-only repo
            if original_repo.exists() and original_repo.is_dir():
                for rel_path in files_to_restore:
                    norm_path = rel_path.replace("\\", "/").strip()
                    orig_file = original_repo / norm_path
                    ws_file = assert_contained_path(ws_root / norm_path, ws_root)

                    if orig_file.exists() and orig_file.is_file():
                        ws_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(orig_file, ws_file)
                        restored_files.append(norm_path)
                    elif ws_file.exists():
                        # File does not exist in original repo; remove it
                        ws_file.unlink()
                        restored_files.append(norm_path)

            # 3. Compute post-rollback tree digest
            post_digest = compute_tree_digest(ws_root)

            # 4. Verify against baseline digest
            if post_digest == workspace.baseline_tree_digest:
                workspace.workspace_status = WorkspaceStatus.READY
                return RollbackStatus.SUCCEEDED, sorted(list(set(restored_files))), post_digest
            else:
                # Digest mismatch: workspace could not be perfectly restored
                workspace.workspace_status = WorkspaceStatus.PRESERVED_FOR_INSPECTION
                return RollbackStatus.FAILED, sorted(list(set(restored_files))), post_digest

        except Exception:
            workspace.workspace_status = WorkspaceStatus.PRESERVED_FOR_INSPECTION
            try:
                curr_digest = compute_tree_digest(ws_root)
            except Exception:
                curr_digest = ""
            return RollbackStatus.FAILED, sorted(list(set(restored_files))), curr_digest
