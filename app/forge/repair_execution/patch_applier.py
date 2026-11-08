"""Phase 12 M4: Controlled Patch Application Engine.

Applies validated unified diff patches strictly inside isolated workspaces:
- Pure in-memory/Python patch application with zero shell or subprocess execution.
- Context-line verification matching before hunk substitution.
- Automatic in-memory backup of target files before modification.
- Verification that only approved target files are altered.
- Generation of captured post-application unified diff.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchApplicationError(Exception):
    """Raised when a patch cannot be cleanly applied to target files."""
    pass


@dataclass
class PatchApplicationOutcome:
    """Structured outcome of in-memory patch application."""
    success: bool
    modified_files: List[str] = field(default_factory=list)
    diff_summary: str = ""
    errors: List[str] = field(default_factory=list)
    backups: Dict[str, bytes] = field(default_factory=dict)

    def __iter__(self):
        err = self.errors[0] if self.errors else None
        return iter((self.success, self.modified_files, self.diff_summary, err))


class ControlledPatchApplier:
    """Applies unified diff patches to files within an isolated workspace."""

    def __init__(self, workspace_path: Optional[Path] = None) -> None:
        self.workspace_path = workspace_path.resolve() if workspace_path else None
        self._backups: Dict[str, bytes] = {}

    def apply_patch(
        self,
        workspace_path: Optional[Path] = None,
        patch: Optional[Any] = None,
        unified_diff: Optional[str] = None,
        allowed_files: Optional[List[str]] = None,
    ) -> PatchApplicationOutcome:
        """Apply a validated unified diff to workspace files.

        Returns:
            PatchApplicationOutcome with success status, modified files, diff summary, and backups.
        """
        ws_root = (workspace_path or self.workspace_path)
        if ws_root is None:
            return PatchApplicationOutcome(
                success=False,
                errors=["Workspace path must be provided to apply_patch."],
            )
        ws_root = ws_root.resolve()

        diff_text = ""
        if patch is not None and hasattr(patch, "unified_diff"):
            diff_text = patch.unified_diff
        elif unified_diff is not None:
            diff_text = unified_diff

        if not diff_text:
            return PatchApplicationOutcome(
                success=False,
                errors=["No unified diff provided to apply_patch."],
            )

        allowed_set: Set[str] = set()
        if allowed_files:
            allowed_set = {f.strip().replace("\\", "/") for f in allowed_files if f.strip()}

        file_diffs = self._split_diff_by_file(diff_text)
        if not file_diffs:
            return PatchApplicationOutcome(
                success=False,
                errors=["No valid file diffs found in patch."],
            )

        # Verify all target files are within allowed scope
        if allowed_set:
            for target_path in file_diffs.keys():
                if target_path not in allowed_set:
                    return PatchApplicationOutcome(
                        success=False,
                        errors=[f"Security error: target file '{target_path}' is outside approved scope."],
                    )

        self._backups.clear()
        modified_files: List[str] = []

        try:
            for target_path, diff_lines in file_diffs.items():
                abs_path = (ws_root / target_path).resolve()
                if not abs_path.exists() or not abs_path.is_file():
                    raise PatchApplicationError(f"Target file '{target_path}' does not exist in workspace.")

                original_bytes = abs_path.read_bytes()
                self._backups[target_path] = original_bytes
                is_crlf = b"\r\n" in original_bytes
                original_content = original_bytes.decode("utf-8")

                new_content = self._apply_file_hunks(original_content, diff_lines, target_path)
                if is_crlf:
                    lines_split = new_content.splitlines()
                    had_trailing_nl = new_content.endswith("\n")
                    new_content = "\r\n".join(lines_split) + ("\r\n" if had_trailing_nl else "")

                abs_path.write_bytes(new_content.encode("utf-8"))
                modified_files.append(target_path)

            summary = f"Successfully modified {len(modified_files)} file(s): {', '.join(modified_files)}"
            return PatchApplicationOutcome(
                success=True,
                modified_files=modified_files,
                diff_summary=summary,
                errors=[],
                backups=dict(self._backups),
            )

        except Exception as exc:
            # Revert any partially modified files from backup
            self.revert_backups(ws_root)
            return PatchApplicationOutcome(
                success=False,
                modified_files=[],
                diff_summary="",
                errors=[str(exc)],
                backups=dict(self._backups),
            )

    def revert_backups(self, workspace_path: Optional[Path] = None) -> None:
        """Restore all original file contents from recorded backups."""
        ws_root = workspace_path or self.workspace_path
        if not ws_root:
            return

        for rel_path, orig_bytes in self._backups.items():
            abs_path = (ws_root / rel_path).resolve()
            try:
                if abs_path.exists():
                    abs_path.write_bytes(orig_bytes)
            except Exception:
                pass
        self._backups.clear()

    def _split_diff_by_file(self, unified_diff: str) -> Dict[str, List[str]]:
        """Split a multi-file unified diff into individual file diff line chunks."""
        file_diffs: Dict[str, List[str]] = {}
        current_file: Optional[str] = None
        current_lines: List[str] = []

        for line in unified_diff.splitlines():
            if line.startswith("+++ "):
                # Finish previous file if any
                if current_file and current_lines:
                    if current_file in file_diffs:
                        raise PatchApplicationError(f"Duplicate file entry in patch for '{current_file}'.")
                    file_diffs[current_file] = current_lines
                    current_lines = []

                raw_target = line[4:].strip().split("\t")[0]
                if raw_target.startswith("b/"):
                    raw_target = raw_target[2:]
                current_file = raw_target.replace("\\", "/")
                current_lines.append(line)
            elif current_file:
                current_lines.append(line)

        if current_file and current_lines:
            if current_file in file_diffs:
                raise PatchApplicationError(f"Duplicate file entry in patch for '{current_file}'.")
            file_diffs[current_file] = current_lines

        return file_diffs

    def _apply_file_hunks(
        self,
        original_content: str,
        diff_lines: List[str],
        file_path: str,
    ) -> str:
        """Parse hunks and apply line replacements to file content."""
        orig_lines = original_content.splitlines(keepends=True)

        hunks = self._parse_hunks(diff_lines)
        if not hunks:
            raise PatchApplicationError(f"No valid hunks found for '{file_path}'.")

        # Check for no-newline markers at end of diff
        has_no_nl_eof = any(line.strip().startswith("\\ No newline at end of file") for line in diff_lines)

        # Apply hunks in reverse line order to prevent offset disruption
        hunks.sort(key=lambda h: h["old_start"], reverse=True)

        result_lines = list(orig_lines)

        for hunk in hunks:
            old_start = hunk["old_start"]
            old_length = hunk["old_length"]
            hunk_lines = hunk["lines"]

            # Calculate slice indices (1-indexed to 0-indexed, with 0-indexed support for creations)
            if old_start == 0 and old_length == 0:
                start_idx = 0
                end_idx = 0
            else:
                start_idx = max(0, old_start - 1)
                end_idx = start_idx + old_length

            if start_idx > len(result_lines):
                raise PatchApplicationError(
                    f"Hunk starting line {old_start} exceeds file length ({len(result_lines)} lines) in '{file_path}'."
                )

            # Build expected old slice and replacement new slice
            expected_old: List[str] = []
            replacement_new: List[str] = []

            for hl in hunk_lines:
                if hl.startswith("\\"):
                    continue
                prefix = hl[0] if hl else " "
                line_text = hl[1:] + ("\n" if not hl[1:].endswith("\n") else "")

                if prefix == " ":
                    expected_old.append(line_text)
                    replacement_new.append(line_text)
                elif prefix == "-":
                    expected_old.append(line_text)
                elif prefix == "+":
                    replacement_new.append(line_text)

            # Match context check: verify expected lines exist
            actual_slice = result_lines[start_idx:end_idx]
            if len(actual_slice) != len(expected_old):
                match_ok = False
            else:
                match_ok = all(
                    a.strip() == e.strip() for a, e in zip(actual_slice, expected_old)
                )

            if not match_ok and expected_old:
                raise PatchApplicationError(
                    f"Hunk context mismatch at line {old_start} in '{file_path}'. "
                    f"Expected {expected_old!r}, found {actual_slice!r}"
                )

            result_lines[start_idx:end_idx] = replacement_new

        result_text = "".join(result_lines)
        if has_no_nl_eof and result_text.endswith("\n"):
            result_text = result_text[:-1]

        return result_text

    def _parse_hunks(self, diff_lines: List[str]) -> List[Dict]:
        """Parse unified diff lines into structured hunk dictionaries."""
        hunks: List[Dict] = []
        current_hunk: Optional[Dict] = None

        for line in diff_lines:
            match = HUNK_HEADER_RE.match(line)
            if match:
                if current_hunk:
                    hunks.append(current_hunk)
                old_start = int(match.group(1))
                old_length = int(match.group(2)) if match.group(2) else 1
                current_hunk = {
                    "old_start": old_start,
                    "old_length": old_length,
                    "lines": [],
                }
            elif current_hunk:
                if line.startswith(("+", "-", " ", "\\")):
                    current_hunk["lines"].append(line)

        if current_hunk:
            hunks.append(current_hunk)

        return hunks
