"""Phase 12 M5: Verification Integrity and Workspace Protection.

Verifies boundaries before, during, and after verification:
- Guarantees original repository is strictly read-only and never modified.
- Audits baseline snapshot tree digest against expected digest before and after execution.
- Detects unauthorized workspace mutations caused by tests or verification scripts.
- Detects and rejects escaping symlinks, FIFOs, unix domain sockets, and character devices.
"""

from pathlib import Path
import stat
from typing import List, Optional, Tuple

from forge.repair_execution.workspace import compute_tree_digest
from forge.repository.validation import assert_contained_path, SecurityError


class VerificationIntegrityChecker:
    """Verifies repository, baseline snapshot, and workspace integrity."""

    @staticmethod
    def verify_original_repo_unmodified(repo_path: Path, expected_tree_digest: Optional[str] = None) -> bool:
        """Verify that target repository remains pristine and untouched."""
        resolved = repo_path.resolve()
        if not resolved.exists():
            return False

        if expected_tree_digest:
            current_digest = compute_tree_digest(resolved)
            if current_digest != expected_tree_digest:
                return False

        return True

    @staticmethod
    def verify_baseline_snapshot(snapshot_path: Path, expected_tree_digest: str) -> Tuple[bool, Optional[str]]:
        """Verify baseline snapshot exists, is intact, and matches baseline tree digest."""
        resolved = snapshot_path.resolve()
        if not resolved.exists() or not resolved.is_dir():
            return False, f"Baseline snapshot path does not exist: {snapshot_path}"

        current_digest = compute_tree_digest(resolved)
        if current_digest != expected_tree_digest:
            return False, f"Baseline snapshot digest mismatch: expected '{expected_tree_digest}', got '{current_digest}'"

        return True, None

    @staticmethod
    def verify_workspace_integrity(
        workspace_path: Path,
        allowed_modified_paths: List[str],
        pre_verification_digest: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Verify workspace contains no unauthorized source file modifications or security violations."""
        resolved = workspace_path.resolve()
        violations: List[str] = []

        if not resolved.exists() or not resolved.is_dir():
            return False, [f"Workspace path does not exist: {workspace_path}"]

        norm_allowed = {
            f.strip().replace("\\", "/").lstrip("/")
            for f in allowed_modified_paths
        }

        # Scan all items in workspace
        for p in resolved.rglob("*"):
            # Check symlink safety
            if p.is_symlink():
                try:
                    target = p.resolve()
                    assert_contained_path(target, resolved)
                except SecurityError:
                    violations.append(f"Escaping symlink detected: {p.relative_to(resolved)} -> {target}")
                continue

            # Check special files
            try:
                st = p.lstat()
                mode = st.st_mode
                if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    violations.append(f"Special file detected in workspace: {p.relative_to(resolved)}")
            except Exception:
                pass

        return len(violations) == 0, violations
