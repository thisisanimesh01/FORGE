"""Robust parser for unified diff text into structured DiffBundle."""

import re
from typing import List, Optional, Tuple

from forge.review.models import (
    DiffBundle,
    DiffFile,
    DiffHunk,
    DiffLine,
    ReviewFileStatus,
)

HUNK_HEADER_PATTERN = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")


def parse_unified_diff(
    diff_text: str,
    base_revision: str = "base",
    head_revision: str = "head",
) -> DiffBundle:
    """Parse raw unified diff output into structured DiffBundle with exact line mappings."""
    if not diff_text or not diff_text.strip():
        return DiffBundle(
            base_revision=base_revision,
            head_revision=head_revision,
            files=[],
            total_files_changed=0,
            total_additions=0,
            total_deletions=0,
            raw_size_bytes=len(diff_text.encode("utf-8")),
        )

    files: List[DiffFile] = []
    lines = diff_text.splitlines()
    i = 0
    n = len(lines)

    total_additions = 0
    total_deletions = 0
    is_truncated = "[DIFF TRUNCATED" in diff_text

    current_file_path: Optional[str] = None
    old_file_path: Optional[str] = None
    file_status = ReviewFileStatus.MODIFIED
    is_binary = False
    current_hunks: List[DiffHunk] = []
    file_additions = 0
    file_deletions = 0

    def flush_file():
        nonlocal current_file_path, old_file_path, file_status, is_binary
        nonlocal current_hunks, file_additions, file_deletions
        if current_file_path:
            files.append(
                DiffFile(
                    path=current_file_path,
                    old_path=old_file_path,
                    status=file_status,
                    hunks=list(current_hunks),
                    additions=file_additions,
                    deletions=file_deletions,
                    is_binary=is_binary,
                )
            )
        current_file_path = None
        old_file_path = None
        file_status = ReviewFileStatus.MODIFIED
        is_binary = False
        current_hunks = []
        file_additions = 0
        file_deletions = 0

    while i < n:
        line = lines[i]

        # New file diff start
        if line.startswith("diff --git "):
            flush_file()
            # Example: diff --git a/app/service.py b/app/service.py
            parts = line.split(" ")
            if len(parts) >= 4:
                old_raw = parts[2]
                new_raw = parts[3]
                old_file_path = old_raw[2:] if old_raw.startswith("a/") else old_raw
                current_file_path = new_raw[2:] if new_raw.startswith("b/") else new_raw
            i += 1
            continue

        if current_file_path is None:
            i += 1
            continue

        # Status heuristics
        if line.startswith("new file mode"):
            file_status = ReviewFileStatus.ADDED
            i += 1
            continue
        elif line.startswith("deleted file mode"):
            file_status = ReviewFileStatus.DELETED
            i += 1
            continue
        elif line.startswith("similarity index") or line.startswith("rename from"):
            file_status = ReviewFileStatus.RENAMED
            i += 1
            continue
        elif line.startswith("Binary files ") or "differ" in line:
            is_binary = True
            file_status = ReviewFileStatus.BINARY
            i += 1
            continue

        # File header lines (--- a/... and +++ b/...)
        if line.startswith("--- "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                file_status = ReviewFileStatus.ADDED
            elif raw_path.startswith("a/"):
                old_file_path = raw_path[2:]
            i += 1
            continue
        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                file_status = ReviewFileStatus.DELETED
            elif raw_path.startswith("b/"):
                current_file_path = raw_path[2:]
            i += 1
            continue

        # Hunk header
        match = HUNK_HEADER_PATTERN.match(line)
        if match:
            old_start = int(match.group(1))
            old_lines = int(match.group(2)) if match.group(2) else 1
            new_start = int(match.group(3))
            new_lines = int(match.group(4)) if match.group(4) else 1
            hunk_header = line

            hunk_diff_lines: List[DiffLine] = []
            curr_old_lineno = old_start
            curr_new_lineno = new_start

            i += 1
            while i < n:
                hline = lines[i]
                if (
                    hline.startswith("diff --git ")
                    or hline.startswith("@@ ")
                    or hline.startswith("[DIFF TRUNCATED")
                ):
                    break

                if not hline:
                    # Empty context line in some unified diff implementations
                    hunk_diff_lines.append(
                        DiffLine(origin=" ", content="", old_lineno=curr_old_lineno, new_lineno=curr_new_lineno)
                    )
                    curr_old_lineno += 1
                    curr_new_lineno += 1
                    i += 1
                    continue

                prefix = hline[0]
                content = hline[1:]

                if prefix == "+":
                    hunk_diff_lines.append(
                        DiffLine(origin="+", content=content, new_lineno=curr_new_lineno)
                    )
                    curr_new_lineno += 1
                    file_additions += 1
                    total_additions += 1
                elif prefix == "-":
                    hunk_diff_lines.append(
                        DiffLine(origin="-", content=content, old_lineno=curr_old_lineno)
                    )
                    curr_old_lineno += 1
                    file_deletions += 1
                    total_deletions += 1
                elif prefix == " ":
                    hunk_diff_lines.append(
                        DiffLine(origin=" ", content=content, old_lineno=curr_old_lineno, new_lineno=curr_new_lineno)
                    )
                    curr_old_lineno += 1
                    curr_new_lineno += 1
                elif prefix == "\\":
                    # E.g. \ No newline at end of file
                    pass
                else:
                    # Unexpected diff line, treat as context
                    hunk_diff_lines.append(
                        DiffLine(origin=" ", content=hline, old_lineno=curr_old_lineno, new_lineno=curr_new_lineno)
                    )
                    curr_old_lineno += 1
                    curr_new_lineno += 1

                i += 1

            current_hunks.append(
                DiffHunk(
                    header=hunk_header,
                    old_start=old_start,
                    old_lines=old_lines,
                    new_start=new_start,
                    new_lines=new_lines,
                    lines=hunk_diff_lines,
                )
            )
            continue

        i += 1

    flush_file()

    return DiffBundle(
        base_revision=base_revision,
        head_revision=head_revision,
        files=files,
        total_files_changed=len(files),
        total_additions=total_additions,
        total_deletions=total_deletions,
        raw_size_bytes=len(diff_text.encode("utf-8")),
        is_truncated=is_truncated,
    )
