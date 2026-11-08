"""Phase 12 M4: Workspace Integrity Verification.

Provides:
- Comparison between baseline tree digest and post-modification tree digest.
- Verification that modifications are strictly confined to approved file paths.
- Detection of unauthorized file creation, modification, or deletion outside scope.
- Structured integrity reports for audit and execution gatekeeping.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from forge.repository.protection import is_protected_name, is_protected_path
from forge.repository.validation import assert_contained_path
from forge.repair_execution.schemas import RepairWorkspace
from forge.repair_execution.workspace import compute_tree_digest


@dataclass
class WorkspaceIntegrityReport:
    """Structured outcome of workspace integrity verification."""
    is_valid: bool
    baseline_digest: str
    current_digest: str
    modified_files: List[str] = field(default_factory=list)
    unexpected_files: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)


class WorkspaceIntegrityChecker:
    """Verifies that an isolated workspace was modified only within authorized boundaries."""

    def __init__(self) -> None:
        pass

    def verify_integrity(
        self,
        workspace: RepairWorkspace,
        allowed_paths: Optional[List[str]] = None,
    ) -> WorkspaceIntegrityReport:
        """Verify the post-repair state of an isolated workspace against baseline.

        Args:
            workspace: The target RepairWorkspace.
            allowed_paths: Explicit list of allowed paths (defaults to workspace.allowed_paths).

        Returns:
            WorkspaceIntegrityReport with detailed findings.
        """
        ws_root = Path(workspace.isolated_workspace_path).resolve()
        baseline_source = (
            Path(workspace.baseline_snapshot_path).resolve()
            if workspace.baseline_snapshot_path and Path(workspace.baseline_snapshot_path).is_dir()
            else Path(workspace.original_repository_reference).resolve()
        )
        approved = set(
            p.replace("\\", "/").strip()
            for p in (allowed_paths if allowed_paths is not None else workspace.allowed_paths)
        )

        current_digest = compute_tree_digest(ws_root)
        baseline_digest = workspace.baseline_tree_digest

        modified_files: List[str] = []
        unexpected_files: List[str] = []
        violations: List[str] = []

        # Scan files in isolated workspace
        for root, dirs, files in os.walk(ws_root, followlinks=False):
            dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]

            for file_name in files:
                if is_protected_name(file_name):
                    continue

                file_path = Path(root) / file_name
                try:
                    rel_path = str(file_path.relative_to(ws_root)).replace("\\", "/")
                except ValueError:
                    continue

                if is_protected_path(rel_path, base_dir=ws_root):
                    violations.append(f"Protected path found in workspace: '{rel_path}'")
                    unexpected_files.append(rel_path)
                    continue

                base_file = baseline_source / rel_path
                if not base_file.exists():
                    # Newly created file
                    if rel_path in approved:
                        modified_files.append(rel_path)
                    else:
                        unexpected_files.append(rel_path)
                        violations.append(f"Unauthorized new file created: '{rel_path}'")
                else:
                    # Compare content with baseline snapshot
                    try:
                        if file_path.read_bytes() != base_file.read_bytes():
                            if rel_path in approved:
                                modified_files.append(rel_path)
                            else:
                                unexpected_files.append(rel_path)
                                violations.append(f"Unauthorized file modified: '{rel_path}'")
                    except Exception as e:
                        violations.append(f"Failed to read file '{rel_path}': {e}")

        # Check for deleted files from baseline
        if baseline_source.exists() and baseline_source.is_dir():
            for root, dirs, files in os.walk(baseline_source, followlinks=False):
                dirs[:] = [d for d in dirs if d != ".git" and not is_protected_name(d)]
                for file_name in files:
                    if is_protected_name(file_name):
                        continue
                    base_file = Path(root) / file_name
                    try:
                        rel_path = str(base_file.relative_to(baseline_source)).replace("\\", "/")
                    except ValueError:
                        continue
                    if is_protected_path(rel_path, base_dir=baseline_source):
                        continue

                    ws_file = ws_root / rel_path
                    if not ws_file.exists():
                        # File was deleted in workspace
                        if rel_path in approved:
                            modified_files.append(rel_path)
                        else:
                            unexpected_files.append(rel_path)
                            violations.append(f"Unauthorized file deleted: '{rel_path}'")

        modified_files = sorted(list(set(modified_files)))
        unexpected_files = sorted(list(set(unexpected_files)))

        is_valid = len(unexpected_files) == 0 and len(violations) == 0

        return WorkspaceIntegrityReport(
            is_valid=is_valid,
            baseline_digest=baseline_digest,
            current_digest=current_digest,
            modified_files=modified_files,
            unexpected_files=unexpected_files,
            violations=violations,
        )
