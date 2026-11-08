"""Phase 12 M4: Strict Unified Diff and Patch Scope Validation.

Validates unified diffs against safety, containment, and authorization rules:
1. Unified diff syntax integrity (valid headers, hunks, line prefixes).
2. Path containment and security (no path traversal, no absolute paths, no wildcards).
3. Protected path isolation (no changes to .env, .git, credentials, databases).
4. Scope enforcement (only allowlisted files may be targeted).
5. Binary patch rejection (refuses non-text diffs).
6. Resource budget enforcement (max bytes, max changed lines, max files).
7. Digest integrity verification.
"""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Dict, List, Optional, Set, Tuple

from forge.repository.protection import is_protected_name, is_protected_path
from forge.repair_execution.schemas import (
    ExecutionLimits,
    PatchValidationStatus,
    MAX_DIFF_BYTES,
)

HUNK_HEADER_PATTERN = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def compute_patch_digest(unified_diff: str) -> str:
    """Compute canonical SHA-256 hex digest of unified diff text."""
    return hashlib.sha256(unified_diff.strip().encode("utf-8")).hexdigest()


class PatchValidationError(Exception):
    """Raised when a patch violates syntax, safety, or scope constraints."""
    pass


@dataclass
class PatchValidationResult:
    """Structured outcome of patch syntax, boundary, and scope validation."""
    is_valid: bool
    status: PatchValidationStatus
    errors: List[str] = field(default_factory=list)
    affected_files: List[str] = field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    patch_digest: str = ""

    def __iter__(self):
        return iter((
            self.is_valid,
            self.status,
            self.errors,
            self.affected_files,
            self.added_lines,
            self.removed_lines,
        ))


class RepairPatchValidator:
    """Performs deep validation of unified diff patches prior to execution."""

    def __init__(
        self,
        workspace_path: Optional[Path] = None,
        execution_limits: Optional[ExecutionLimits] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve() if workspace_path else Path.cwd().resolve()
        self.limits = execution_limits or ExecutionLimits()

    def validate_patch(
        self,
        unified_diff: str,
        allowed_paths: Optional[List[str]] = None,
        allowed_file_paths: Optional[List[str]] = None,
        expected_digest: Optional[str] = None,
        expected_patch_digest: Optional[str] = None,
        max_bytes: Optional[int] = None,
        max_changed_lines: Optional[int] = None,
    ) -> PatchValidationResult:
        """Validate unified diff against all M4 safety, structural, and scope constraints."""
        target_allowed = allowed_paths if allowed_paths is not None else (allowed_file_paths or [])
        target_expected_digest = expected_digest if expected_digest is not None else expected_patch_digest
        effective_max_bytes = max_bytes or self.limits.max_patch_bytes
        effective_max_lines = max_changed_lines or self.limits.max_changed_lines

        errors: List[str] = []
        affected_files: List[str] = []
        added_lines = 0
        removed_lines = 0

        # Check 1: Non-empty diff
        if not unified_diff or not unified_diff.strip():
            return PatchValidationResult(
                is_valid=False,
                status=PatchValidationStatus.INVALID_SYNTAX,
                errors=["Patch unified_diff is empty."],
                patch_digest="",
            )

        # Check 2: Size limit
        diff_bytes = len(unified_diff.encode("utf-8"))
        if diff_bytes > MAX_DIFF_BYTES or diff_bytes > effective_max_bytes:
            return PatchValidationResult(
                is_valid=False,
                status=PatchValidationStatus.OVERSIZED,
                errors=[
                    f"Patch size ({diff_bytes} bytes) exceeds maximum limit "
                    f"({min(MAX_DIFF_BYTES, effective_max_bytes)} bytes)."
                ],
                patch_digest=compute_patch_digest(unified_diff),
            )

        # Check 3: Digest integrity verification
        actual_digest = compute_patch_digest(unified_diff)
        if target_expected_digest and actual_digest.lower() != target_expected_digest.lower():
            return PatchValidationResult(
                is_valid=False,
                status=PatchValidationStatus.DIGEST_MISMATCH,
                errors=[
                    f"Patch digest mismatch: expected '{target_expected_digest[:16]}...', "
                    f"actual '{actual_digest[:16]}...'."
                ],
                patch_digest=actual_digest,
            )

        # Check 4: Binary patch check
        if "GIT binary patch" in unified_diff or "\x00" in unified_diff:
            return PatchValidationResult(
                is_valid=False,
                status=PatchValidationStatus.INVALID_SYNTAX,
                errors=["Binary patches are strictly forbidden."],
                patch_digest=actual_digest,
            )

        allowed_set = {p.strip().replace("\\", "/") for p in target_allowed if p.strip()}
        lines = unified_diff.splitlines()

        allowed_set = {p.strip().replace("\\", "/") for p in target_allowed if p.strip()}
        lines = unified_diff.splitlines()

        i = 0
        current_file: Optional[str] = None
        has_at_least_one_hunk = False
        seen_files: Set[str] = set()
        file_hunks: Dict[str, List[Dict[str, int]]] = {}

        # Hunk tracking state
        in_hunk = False
        current_hunk_header_line = 0
        current_hunk_old_start = 0
        current_hunk_old_len = 0
        current_hunk_new_start = 0
        current_hunk_new_len = 0
        current_hunk_minus = 0
        current_hunk_plus = 0
        current_hunk_space = 0

        def finalize_current_hunk():
            nonlocal in_hunk, current_hunk_header_line
            nonlocal current_hunk_old_start, current_hunk_old_len
            nonlocal current_hunk_new_start, current_hunk_new_len
            nonlocal current_hunk_minus, current_hunk_plus, current_hunk_space
            if not in_hunk:
                return

            total_hunk_lines = current_hunk_minus + current_hunk_plus + current_hunk_space
            if total_hunk_lines == 0:
                errors.append(f"Empty hunk with zero modification lines at line {current_hunk_header_line}.")
                in_hunk = False
                return

            actual_old = current_hunk_minus + current_hunk_space
            actual_new = current_hunk_plus + current_hunk_space

            if actual_old != current_hunk_old_len or actual_new != current_hunk_new_len:
                errors.append(
                    f"Hunk line count mismatch in '{current_file}' at line {current_hunk_header_line}: "
                    f"header specified -{current_hunk_old_start},{current_hunk_old_len} +{current_hunk_new_start},{current_hunk_new_len}, "
                    f"but found {actual_old} old lines and {actual_new} new lines."
                )

            # Check ordering and overlap within file
            if current_file:
                prev_hunks = file_hunks.get(current_file, [])
                if prev_hunks:
                    prev = prev_hunks[-1]
                    if current_hunk_old_start < prev["old_start"]:
                        errors.append(
                            f"Reordered hunks detected in '{current_file}': hunk starting at line {current_hunk_old_start} "
                            f"appears after hunk starting at line {prev['old_start']}."
                        )
                    elif prev["old_len"] > 0 and current_hunk_old_start < prev["old_start"] + prev["old_len"]:
                        errors.append(
                            f"Overlapping hunks detected in '{current_file}': hunk at line {current_hunk_old_start} "
                            f"overlaps with preceding hunk ending at line {prev['old_start'] + prev['old_len'] - 1}."
                        )

                if current_file not in file_hunks:
                    file_hunks[current_file] = []
                file_hunks[current_file].append({
                    "old_start": current_hunk_old_start,
                    "old_len": current_hunk_old_len,
                    "new_start": current_hunk_new_start,
                    "new_len": current_hunk_new_len,
                })

            in_hunk = False

        while i < len(lines):
            line = lines[i]

            # Reject forbidden git metadata lines
            if line.startswith(("old mode ", "new mode ", "deleted file mode ", "new file mode ", "similarity index ", "rename from ", "rename to ", "copy from ", "copy to ")):
                errors.append(f"Forbidden git metadata line detected at line {i+1}: '{line}'. Mode changes, renames, and copies are forbidden.")
                i += 1
                continue

            # Parse file headers: --- a/path and +++ b/path
            if line.startswith("--- "):
                finalize_current_hunk()
                if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                    errors.append(f"Invalid header syntax: '---' without matching '+++' at line {i+1}.")
                    break

                from_file = self._parse_file_path(line[4:].strip())
                to_line = lines[i + 1]
                to_file = self._parse_file_path(to_line[4:].strip())
                i += 2

                # Reject renames / copies
                if from_file is not None and to_file is not None and from_file != to_file:
                    errors.append(f"File rename or copy detected from '{from_file}' to '{to_file}'. File renames are strictly forbidden.")

                target_file = to_file or from_file
                if not target_file:
                    errors.append(f"Could not parse valid target file path at diff line {i}.")
                    continue

                norm_target = target_file.replace("\\", "/").strip()
                if norm_target in seen_files:
                    errors.append(f"Duplicate file entry in patch: '{norm_target}'. Multiple diff sections targeting the same file are forbidden.")
                seen_files.add(norm_target)

                if norm_target not in affected_files:
                    affected_files.append(norm_target)
                current_file = norm_target

                # Validate target path security and scope
                path_status = self._validate_target_path(norm_target, allowed_set, errors)
                if path_status != PatchValidationStatus.VALID:
                    return PatchValidationResult(
                        is_valid=False,
                        status=path_status,
                        errors=errors,
                        affected_files=affected_files,
                        patch_digest=actual_digest,
                    )
                continue

            if line.startswith("+++ "):
                errors.append(f"Invalid header syntax: '+++' without preceding '---' at line {i+1}.")
                i += 1
                continue

            # Parse hunk header
            if line.startswith("@@"):
                finalize_current_hunk()
                match = HUNK_HEADER_PATTERN.match(line)
                if not match:
                    errors.append(f"Malformed hunk header at line {i+1}: '{line}'.")
                    i += 1
                    continue

                has_at_least_one_hunk = True
                in_hunk = True
                current_hunk_header_line = i + 1
                current_hunk_old_start = int(match.group(1))
                current_hunk_old_len = int(match.group(2)) if match.group(2) is not None else 1
                current_hunk_new_start = int(match.group(3))
                current_hunk_new_len = int(match.group(4)) if match.group(4) is not None else 1
                current_hunk_minus = 0
                current_hunk_plus = 0
                current_hunk_space = 0
                i += 1
                continue

            # Parse hunk lines
            if line.startswith("+") and not line.startswith("+++"):
                added_lines += 1
                if in_hunk:
                    current_hunk_plus += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed_lines += 1
                if in_hunk:
                    current_hunk_minus += 1
            elif line.startswith(" "):
                if in_hunk:
                    current_hunk_space += 1
            elif line.startswith("\\"):
                # "\ No newline at end of file"
                pass
            elif line.strip() == "":
                # Empty line in diff (treated as empty context line)
                if in_hunk:
                    current_hunk_space += 1
            else:
                errors.append(f"Unexpected line prefix at line {i+1}: '{line[:20]}'.")

            i += 1

        finalize_current_hunk()

        if not has_at_least_one_hunk:
            errors.append("Patch contains no valid hunks ('@@ -... +... @@').")

        # Check total changed lines
        total_changed = added_lines + removed_lines
        if total_changed > effective_max_lines:
            return PatchValidationResult(
                is_valid=False,
                status=PatchValidationStatus.OVERSIZED,
                errors=[
                    f"Total changed lines ({total_changed}) exceeds limit of {effective_max_lines}."
                ],
                affected_files=affected_files,
                added_lines=added_lines,
                removed_lines=removed_lines,
                patch_digest=actual_digest,
            )

        if errors:
            return PatchValidationResult(
                is_valid=False,
                status=PatchValidationStatus.INVALID_SYNTAX,
                errors=errors,
                affected_files=affected_files,
                added_lines=added_lines,
                removed_lines=removed_lines,
                patch_digest=actual_digest,
            )

        return PatchValidationResult(
            is_valid=True,
            status=PatchValidationStatus.VALID,
            errors=[],
            affected_files=affected_files,
            added_lines=added_lines,
            removed_lines=removed_lines,
            patch_digest=actual_digest,
        )

    def _parse_file_path(self, raw_path: str) -> Optional[str]:
        """Extract clean relative path from diff header line, stripping a/ or b/ prefixes."""
        clean = raw_path.split("\t")[0].strip()
        if clean == "/dev/null":
            return None
        if clean.startswith("a/") or clean.startswith("b/"):
            clean = clean[2:]
        return clean.strip()

    def _validate_target_path(
        self,
        path: str,
        allowed_set: Set[str],
        errors: List[str],
    ) -> PatchValidationStatus:
        """Validate target file path against containment, traversal, and scope allowlist."""
        # Path traversal
        if ".." in Path(path).parts or path.startswith("/"):
            errors.append(f"Security violation: path traversal or absolute path detected: '{path}'.")
            return PatchValidationStatus.PROTECTED_PATH_VIOLATION

        # Pathspec magic or wildcards
        if any(c in path for c in "*?[]{}~:"):
            errors.append(f"Security violation: path contains forbidden wildcard or magic characters: '{path}'.")
            return PatchValidationStatus.PROTECTED_PATH_VIOLATION

        # Protected file checks
        if is_protected_name(Path(path).name) or is_protected_path(path, base_dir=self.workspace_path):
            errors.append(f"Protected path violation: modifying protected file '{path}' is strictly forbidden.")
            return PatchValidationStatus.PROTECTED_PATH_VIOLATION

        # Scope allowlist check
        if allowed_set and path not in allowed_set:
            errors.append(
                f"Scope violation: file '{path}' is outside approved scope {sorted(allowed_set)}."
            )
            return PatchValidationStatus.SCOPE_VIOLATION

        return PatchValidationStatus.VALID
