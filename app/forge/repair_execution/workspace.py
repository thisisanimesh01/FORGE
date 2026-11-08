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
import tempfile
from typing import Dict, List, Optional, Set, Tuple
import uuid

from forge.config import get_settings
from forge.repository.protection import is_protected_name, is_protected_path, purge_protected_paths_from_workspace
from forge.repository.validation import assert_contained_path, SecurityError
from forge.repair_execution.schemas import RepairWorkspace, WorkspaceStatus


def compute_tree_digest(workspace_path: Path, allowed_paths: Optional[List[str]] = None) -> str:
    """Compute a deterministic SHA-256 hash across workspace files and contents."""
    resolved_ws = workspace_path.resolve()
    hasher = hashlib.sha256()

    file_hashes: List[Tuple[str, str]] = []

    for root, dirs, files in os.walk(resolved_ws):
        # Skip git internal metadata and hidden VCS dirs
        dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]

        for file_name in sorted(files):
            if is_protected_name(file_name):
                continue

            file_path = Path(root) / file_name
            try:
                rel_path = str(file_path.relative_to(resolved_ws)).replace("\\", "/")
            except ValueError:
                continue

            if is_protected_path(rel_path, base_dir=resolved_ws):
                continue

            if allowed_paths is not None and rel_path not in allowed_paths:
                # Still include in overall tree digest if evaluating full workspace
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

    def allocate_workspace(
        self,
        allowed_paths: List[str],
        plan_id: str,
        baseline_commit: Optional[str] = None,
    ) -> RepairWorkspace:
        """Create an isolated workspace initialized with clean repository files.

        Original repository is never modified.
        """
        workspace_id = f"rws_{uuid.uuid4().hex[:12]}"
        isolated_dir = self.base_workspaces_dir / workspace_id
        isolated_dir.mkdir(parents=True, exist_ok=False)

        # Copy repository files into isolated directory excluding protected patterns and .git
        def ignore_patterns(path: str, names: List[str]) -> Set[str]:
            ignored = set()
            for name in names:
                if name == ".git" or is_protected_name(name):
                    ignored.add(name)
            return ignored

        shutil.copytree(
            src=self.original_repo_path,
            dst=isolated_dir,
            dirs_exist_ok=True,
            ignore=ignore_patterns,
            symlinks=False,  # Resolve symlinks to avoid escapes
        )

        # Deep purge of any nested protected files that might have been copied
        purge_protected_paths_from_workspace(isolated_dir)

        # Baseline commit resolution
        resolved_commit = baseline_commit or "baseline_unversioned"
        git_dir = self.original_repo_path / ".git"
        if git_dir.exists() and git_dir.is_dir() and baseline_commit is None:
            # Safely query HEAD commit
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

        # Compute clean baseline tree digest
        baseline_digest = compute_tree_digest(isolated_dir)

        workspace = RepairWorkspace(
            workspace_id=workspace_id,
            original_repository_reference=str(self.original_repo_path),
            isolated_workspace_path=str(isolated_dir),
            baseline_commit=resolved_commit,
            baseline_tree_digest=baseline_digest,
            allowed_paths=allowed_paths,
            workspace_status=WorkspaceStatus.READY,
        )

        self._workspaces[workspace_id] = workspace
        return workspace

    def get_workspace(self, workspace_id: str) -> Optional[RepairWorkspace]:
        """Retrieve workspace record by ID."""
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
        workspace = self._workspaces.get(workspace_id)
        if not workspace:
            return False

        if workspace.workspace_status == WorkspaceStatus.PRESERVED_FOR_INSPECTION and not force:
            return False

        ws_dir = Path(workspace.isolated_workspace_path)
        if ws_dir.exists() and ws_dir.is_dir():
            try:
                shutil.rmtree(ws_dir)
            except Exception:
                return False

        workspace.workspace_status = WorkspaceStatus.CLEANED_UP
        workspace.cleanup_status = "cleaned_up"
        return True

    def preserve_for_inspection(self, workspace_id: str) -> bool:
        """Mark workspace as preserved for inspection when rollback fails or recovery is required."""
        workspace = self._workspaces.get(workspace_id)
        if not workspace:
            return False
        workspace.workspace_status = WorkspaceStatus.PRESERVED_FOR_INSPECTION
        workspace.cleanup_status = "preserved"
        return True
