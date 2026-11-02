"""Phase 8 Repair Execution Architecture — Safe Patch Applier.

Applies validated candidate patches strictly within isolated scratch workspaces,
guaranteeing in-memory recovery on failure without destroying the workspace or
mutating the original target repository.
"""

from pathlib import Path
from typing import List, Optional, Tuple

from forge.repair.execution.failures import (
    PatchApplicationError,
    ScopeContainmentViolationError,
    TargetRepoImmutabilityViolationError,
    WorkspaceIntegrityViolationError,
)
from forge.repair.execution.interfaces import RepairWorkspaceProtocol
from forge.repair.execution.models import PatchArtifact
from forge.repair.verifier import apply_unified_diff_to_file


class SafePatchApplier:
    """Safely applies candidate patches inside isolated repair workspaces with atomic recovery."""

    def apply_patch(
        self,
        patch: PatchArtifact,
        workspace: RepairWorkspaceProtocol,
        allowed_files: List[str],
    ) -> Tuple[bool, Optional[str]]:
        """Apply a validated candidate patch to the target file inside the scratch workspace.

        Args:
            patch: Validated candidate PatchArtifact.
            workspace: Isolated RepairWorkspace instance.
            allowed_files: Permitted modification scope.

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        # 1. Target containment and existence check
        try:
            target_disk = workspace.validate_containment(patch.target_file)
        except Exception as e:
            return False, f"Target file '{patch.target_file}' escapes workspace boundary: {e}"

        if not target_disk.is_file():
            return False, f"Target file '{patch.target_file}' does not exist in workspace"

        # 2. In-memory backup of target file before modification
        orig_content = target_disk.read_text(encoding="utf-8", errors="replace")

        # 3. Apply unified diff to target file
        try:
            apply_unified_diff_to_file(
                target_disk,
                patch.diff_content,
                check_semantics=True,
            )
        except Exception as app_err:
            # Atomic rollback of target file
            target_disk.write_text(orig_content, encoding="utf-8")
            return False, f"Failed to apply unified diff to '{patch.target_file}': {app_err}"

        # 4. Verify post-application workspace filesystem integrity
        try:
            workspace.check_integrity(allowed_files=allowed_files)
        except Exception as integ_err:
            # Rollback file modification
            target_disk.write_text(orig_content, encoding="utf-8")
            return False, f"Workspace integrity check failed after patch application: {integ_err}"

        # 5. Verify target repository immutability
        if not workspace.check_target_immutability():
            target_disk.write_text(orig_content, encoding="utf-8")
            return False, "Target repository was mutated during patch application"

        return True, None
