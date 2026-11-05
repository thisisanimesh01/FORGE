"""Narrow, strictly-typed, path-protected security gateway for Git operations.

Enforces Option C (Safe Git Metadata Isolation):
- Decouples the trusted Git repository mirror from the agent-accessible analysis workspace.
- Enforces path protection before any Git operation reads or returns file content.
- Hardens subprocess environment against Git redirection attacks (GIT_DIR, hooks, alternates).
- Never allows unrestricted diffs over the trusted repository; safely restricts diffs to eligible non-protected paths.
- Redacts secrets from all command outputs, diffs, and diagnostics.
- Fails closed upon any mediation failure, containment escape, or security violation.
"""

import os
from pathlib import Path
import shutil
import subprocess
from typing import Dict, List, Optional, Set, Tuple, Union

from forge.config import redact_secrets
from forge.repository.protection import is_protected_name, is_protected_path


class GitMediatorError(Exception):
    """Base exception for Git mediation failures."""
    pass


class GitMediatorSecurityError(GitMediatorError):
    """Raised when an operation attempts to access protected paths or escape containment."""
    pass


class GitMediator:
    """Narrow, strictly-typed, path-protected security gateway for Git operations.

    Invariants:
    1. Operates on a trusted Git repository mirror that is physically isolated from the analysis workspace.
    2. Verifies that the trusted Git directory and analysis workspace do not contain each other.
    3. Enforces path protection (is_protected_path) BEFORE executing any content-reading operation.
    4. Automatically passes all outputs through redact_secrets().
    5. Fails closed with GitMediatorSecurityError upon detecting forbidden paths, traversal, or symlink escapes.
    6. Constructs a hardened, sanitized subprocess environment stripped of host Git control variables.
    7. Never executes unrestricted diffs over the trusted repository.
    """

    def __init__(self, trusted_git_path: Union[str, Path], workspace_path: Union[str, Path]):
        self._trusted_git_path = Path(trusted_git_path).resolve()
        self._workspace_path = Path(workspace_path).resolve()
        self._assert_isolation_boundary()

    def _assert_isolation_boundary(self) -> None:
        """Enforce physical and directory-level isolation between trusted Git storage and analysis workspace."""
        if not self._trusted_git_path.exists():
            raise GitMediatorError(
                f"Trusted Git repository path does not exist: '{self._trusted_git_path}'"
            )
        if not self._workspace_path.exists():
            raise GitMediatorError(
                f"Analysis workspace path does not exist: '{self._workspace_path}'"
            )

        # Disallow collision or mutual containment
        if self._trusted_git_path == self._workspace_path:
            raise GitMediatorSecurityError(
                "Security violation: trusted Git path and analysis workspace cannot be identical."
            )

        try:
            if self._workspace_path.is_relative_to(self._trusted_git_path):
                raise GitMediatorSecurityError(
                    "Security violation: analysis workspace cannot be inside trusted Git repository."
                )
        except ValueError:
            pass

        try:
            if self._trusted_git_path.is_relative_to(self._workspace_path):
                raise GitMediatorSecurityError(
                    "Security violation: trusted Git repository cannot be inside analysis workspace."
                )
        except ValueError:
            pass

    @property
    def trusted_git_path(self) -> Path:
        """Path to the isolated, trusted Git repository."""
        return self._trusted_git_path

    @property
    def workspace_path(self) -> Path:
        """Path to the agent-accessible analysis workspace."""
        return self._workspace_path

    # =========================================================================
    # Environment Hardening & Subprocess Security
    # =========================================================================

    def _get_sanitized_git_env(self, target_cwd: Path) -> Dict[str, str]:
        """Construct a minimal, hardened environment preventing Git redirection attacks.

        Removes host Git environment variables that can redirect repository discovery,
        object storage, configuration, hooks, or worktree selection.
        Explicitly binds repository location to target_cwd and disables alternates and hooks.
        """
        resolved_cwd = target_cwd.resolve()
        git_dir = resolved_cwd / ".git" if (resolved_cwd / ".git").is_dir() else resolved_cwd
        work_tree = resolved_cwd

        # 1. Start with clean base: preserve ONLY safe, necessary system variables
        safe_keys = {
            "PATH",
            "TMPDIR",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "LC_MESSAGES",
            "TZ",
        }
        sanitized_env: Dict[str, str] = {}
        for k in safe_keys:
            if k in os.environ:
                sanitized_env[k] = os.environ[k]

        if "PATH" not in sanitized_env:
            sanitized_env["PATH"] = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"

        # 2. Explicitly bind repository location to trusted mirror
        sanitized_env["GIT_DIR"] = str(git_dir)
        sanitized_env["GIT_WORK_TREE"] = str(work_tree)
        objects_dir = git_dir / "objects"
        if objects_dir.is_dir():
            sanitized_env["GIT_OBJECT_DIRECTORY"] = str(objects_dir)

        # 3. Explicitly disable alternates, hooks, prompt, locks, and external configurations
        sanitized_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = ""
        sanitized_env["GIT_CONFIG_NOSYSTEM"] = "1"
        sanitized_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        sanitized_env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        sanitized_env["GIT_CONFIG_PARAMETERS"] = ""
        sanitized_env["GIT_CONFIG_COUNT"] = "0"
        sanitized_env["GIT_OPTIONAL_LOCKS"] = "0"
        sanitized_env["GIT_TERMINAL_PROMPT"] = "0"
        sanitized_env["GIT_HOOKS_PATH"] = "/dev/null"
        sanitized_env["GIT_CEILING_DIRECTORIES"] = str(resolved_cwd.parent)
        sanitized_env["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "0"

        return sanitized_env

    def _run_git(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        timeout_seconds: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a fixed Git argument sequence in an isolated execution environment."""
        target_cwd = cwd or self._trusted_git_path
        cmd = [
            "git",
            "-C", str(target_cwd),
            "-c", "core.hooksPath=/dev/null",
            "-c", "diff.external=",
            "-c", f"safe.directory={target_cwd}",
        ] + args

        env = self._get_sanitized_git_env(target_cwd)

        try:
            res = subprocess.run(
                cmd,
                cwd=str(target_cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
            return res
        except subprocess.TimeoutExpired as e:
            raise GitMediatorError(f"Git command timed out after {timeout_seconds}s: git {' '.join(args)}") from e
        except Exception as e:
            raise GitMediatorError(f"Failed to execute Git command: {redact_secrets(str(e))}") from e

    # =========================================================================
    # Path Validation & Hardening
    # =========================================================================

    def _validate_and_normalize_path(self, raw_path: str, for_diff: bool = False) -> str:
        """Validate and normalize a repository-relative path, rejecting magic, wildcards, and traversals.

        Enforces:
        - Must be non-empty string.
        - Must not use Git pathspec magic (starts with ':').
        - Must not contain wildcard or glob characters (*, ?, [, ], {, }).
        - Must not contain NUL bytes or disallowed control characters.
        - Preserves filenames containing spaces, tabs, and newlines.
        - Must not be an absolute path.
        - Must not contain path traversal sequences (..).
        - Must not match protected file patterns.
        """
        if not isinstance(raw_path, str):
            raise GitMediatorError(f"Invalid path type: expected str, got {type(raw_path).__name__}")

        if not raw_path:
            raise GitMediatorError("Path cannot be empty.")

        # Reject Git pathspec magic (e.g., ':(top)', ':(glob)', ':!', ':^', ':/')
        if raw_path.startswith(":"):
            raise GitMediatorSecurityError(
                f"Security violation: Git pathspec magic is forbidden: '{raw_path}'"
            )

        # Reject wildcard / glob characters
        if any(c in raw_path for c in "*?[]{}"):
            raise GitMediatorSecurityError(
                f"Security violation: wildcards and globs are forbidden in path: '{raw_path}'"
            )

        # Reject NUL bytes or unprintable control characters (allow space, tab \t, and newline \n)
        if any(c == "\x00" or (ord(c) < 32 and c not in ("\t", "\n")) or ord(c) == 127 for c in raw_path):
            raise GitMediatorSecurityError(
                "Security violation: control characters are forbidden in path."
            )

        # Normalize slashes
        normalized = raw_path.replace("\\", "/")

        # Reject absolute paths or root escapes
        if normalized.startswith("/"):
            raise GitMediatorSecurityError(
                f"Security violation: absolute paths are forbidden: '{raw_path}'"
            )

        # Disallow path traversal components
        path_obj = Path(normalized)
        if any(part in ("..", ".") for part in path_obj.parts):
            raise GitMediatorSecurityError(
                f"Security violation: path traversal sequence detected: '{raw_path}'"
            )

        # Upfront path protection check
        if is_protected_name(path_obj.name) or is_protected_path(normalized, base_dir=self._workspace_path):
            if for_diff:
                raise GitMediatorSecurityError(
                    f"Security violation: diff requested for protected sensitive path: '{normalized}'"
                )
            raise GitMediatorSecurityError(
                f"Security violation: access to protected sensitive path '{normalized}' is strictly forbidden."
            )

        return normalized

    # =========================================================================
    # Metadata Operations (Safe, Non-Content-Reading)
    # =========================================================================

    def get_upstream_commit_sha(self) -> str:
        """Resolve the authentic upstream HEAD commit SHA directly from the trusted mirror."""
        res = self._run_git(["rev-parse", "HEAD"])
        if res.returncode != 0:
            raise GitMediatorError(f"Failed to resolve upstream commit SHA: {res.stderr.strip()}")
        sha = res.stdout.strip().lower()
        if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
            raise GitMediatorError(f"Resolved invalid Git commit SHA: '{sha}'")
        return sha

    def get_target_branch(self) -> Optional[str]:
        """Resolve the authentic target branch name from the trusted mirror."""
        res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if res.returncode == 0:
            branch = res.stdout.strip()
            if branch and branch != "HEAD":
                return branch
        return None

    def get_commit_metadata(self, commit_sha: Optional[str] = None) -> Dict[str, str]:
        """Retrieve sanitized author, committer, date, and commit message for a commit."""
        target_ref = commit_sha or "HEAD"
        res = self._run_git(["log", "-1", "--format=%an%x00%ae%x00%cI%x00%s", target_ref])
        if res.returncode != 0:
            raise GitMediatorError(f"Failed to query commit metadata for '{target_ref}': {res.stderr.strip()}")

        parts = res.stdout.strip().split("\x00")
        if len(parts) < 4:
            return {"commit_sha": target_ref, "message": redact_secrets(res.stdout.strip())}

        return {
            "commit_sha": target_ref,
            "author_name": redact_secrets(parts[0]),
            "author_email": redact_secrets(parts[1]),
            "commit_date": parts[2],
            "subject": redact_secrets(parts[3]),
        }

    def check_revision_drift(self, expected_sha: str) -> bool:
        """Verify whether the current upstream HEAD matches the expected base commit SHA."""
        expected = expected_sha.strip().lower()
        current = self.get_upstream_commit_sha()
        return current == expected

    def list_tree_paths(self, ref: str = "HEAD", recursive: bool = True) -> List[str]:
        """List repository paths at a given ref, strictly filtering out any protected paths."""
        args = ["ls-tree", "--name-only"]
        if recursive:
            args.append("-r")
        args.append(ref)

        res = self._run_git(args)
        if res.returncode != 0:
            raise GitMediatorError(f"Failed to list tree at '{ref}': {res.stderr.strip()}")

        paths: List[str] = []
        for line in res.stdout.splitlines():
            path_str = line.strip()
            if not path_str:
                continue
            # Enforce path protection check
            if not is_protected_name(Path(path_str).name) and not is_protected_path(path_str, base_dir=self._workspace_path):
                paths.append(path_str)

        return sorted(paths)

    def verify_trusted_integrity(self) -> bool:
        """Run git fsck --full on the trusted repository and assert 0 broken links and 0 errors."""
        res = self._run_git(["fsck", "--full"])
        if res.returncode != 0:
            raise GitMediatorError(
                f"Trusted Git repository integrity failure (exit code {res.returncode}):\n{res.stderr.strip() or res.stdout.strip()}"
            )
        return True

    # =========================================================================
    # Path-Protected Content Operations
    # =========================================================================

    def get_file_content_at_ref(self, ref: str, relative_path: str) -> str:
        """Read file content from Git object store with mandatory upfront path protection.

        Fails closed with GitMediatorSecurityError if the path matches protected patterns,
        uses Git pathspec magic, wildcards, or traversal sequences.
        All returned content is passed through redact_secrets().
        """
        clean_path = self._validate_and_normalize_path(relative_path)

        res = self._run_git(["show", f"{ref}:{clean_path}"])
        if res.returncode != 0:
            err = res.stderr.strip()
            if "does not exist in" in err or "fatal: path" in err or "exists on disk, but not in" in err:
                raise FileNotFoundError(f"Path '{redact_secrets(clean_path)}' not found at ref '{ref}'.")
            raise GitMediatorError(f"Failed to read '{clean_path}' at ref '{ref}': {redact_secrets(err)}")

        return redact_secrets(res.stdout)

    def _audit_provenance_and_metadata(self, base_ref: str, target_ref: str) -> Tuple[Set[str], Set[str]]:
        """Audit Git tree metadata and provenance between base_ref and target_ref without reading blob content.

        Enforces a conservative defense-in-depth policy:
        1. Identical blob object ID matching: Collects blob SHAs of protected files at base_ref.
           Any added or modified path in target_ref matching a protected blob ID is marked tainted.
        2. Rename / copy detection: Uses git diff -M -C to detect explicit renames and copies.
           Any path derived from a protected path is marked tainted. Ordinary renames are recorded.
        3. Ambiguous addition quarantining: If protected files were deleted between base_ref and
           target_ref, any newly added path whose provenance cannot be proven to originate from
           an ordinary file is quarantined as ambiguous.
        """
        tainted_paths: Set[str] = set()
        ambiguous_paths: Set[str] = set()

        # Step 1: Collect blob object IDs of all protected files at base_ref
        protected_blob_shas: Set[str] = set()
        res_ls = self._run_git(["ls-tree", "-r", "-z", base_ref])
        if res_ls.returncode == 0:
            for entry in res_ls.stdout.split("\x00"):
                if not entry or "\t" not in entry:
                    continue
                meta_part, p = entry.split("\t", 1)
                p_clean = p.replace("\\", "/")
                if is_protected_name(Path(p_clean).name) or is_protected_path(p_clean, base_dir=self._workspace_path):
                    tokens = meta_part.split()
                    if len(tokens) >= 3:
                        protected_blob_shas.add(tokens[2])

        # Step 2: Query tree diff metadata without renames
        res_dt = self._run_git(["diff-tree", "-r", "--no-renames", "-z", base_ref, target_ref])
        if res_dt.returncode != 0:
            err_msg = redact_secrets(res_dt.stderr.strip() or res_dt.stdout.strip())
            raise GitMediatorError(f"Failed to audit diff-tree between '{base_ref}' and '{target_ref}': {err_msg}")

        dt_parts = res_dt.stdout.split("\x00")
        protected_deletions: Dict[str, str] = {}
        additions: Dict[str, str] = {}
        modifications: Dict[str, str] = {}

        i = 0
        while i < len(dt_parts) - 1:
            header = dt_parts[i]
            path = dt_parts[i + 1]
            if not header:
                i += 1
                continue
            meta = header.split()
            if len(meta) >= 5:
                src_sha, dst_sha, status = meta[2], meta[3], meta[4]
                path_clean = path.replace("\\", "/")
                is_prot = is_protected_name(Path(path_clean).name) or is_protected_path(path_clean, base_dir=self._workspace_path)

                if status == "D":
                    if is_prot:
                        protected_deletions[path_clean] = src_sha
                        protected_blob_shas.add(src_sha)
                elif status == "A":
                    additions[path_clean] = dst_sha
                elif status == "M":
                    modifications[path_clean] = dst_sha
            i += 2

        # Step 3: Identical blob object ID matching (zero content read)
        for p, sha in additions.items():
            if sha in protected_blob_shas:
                tainted_paths.add(p)
        for p, sha in modifications.items():
            if sha in protected_blob_shas:
                tainted_paths.add(p)

        # Step 4: Rename and copy detection via git diff -M -C5% --find-copies-harder
        proven_ordinary_renames: Set[str] = set()
        res_m = self._run_git([
            "diff",
            "--no-ext-diff",
            "-M",
            "-C5%",
            "--find-copies-harder",
            "--name-status",
            "-z",
            base_ref,
            target_ref,
        ])
        if res_m.returncode == 0:
            m_parts = res_m.stdout.split("\x00")
            j = 0
            while j < len(m_parts) - 1:
                status = m_parts[j]
                if not status:
                    j += 1
                    continue
                if status.startswith("R") or status.startswith("C"):
                    if j + 2 >= len(m_parts):
                        break
                    old_path = m_parts[j + 1].replace("\\", "/")
                    new_path = m_parts[j + 2].replace("\\", "/")
                    if is_protected_name(Path(old_path).name) or is_protected_path(old_path, base_dir=self._workspace_path):
                        tainted_paths.add(new_path)
                    else:
                        proven_ordinary_renames.add(new_path)
                    j += 3
                else:
                    j += 2

        # Step 5: Quarantining ambiguous additions
        # If protected files were deleted in this commit transition, any addition without proven ordinary provenance is quarantined.
        # If protected files exist at base_ref (even if unmodified), any unproven addition exhibiting configuration/credential
        # characteristics is quarantined under a fail-closed policy because heuristic similarity cannot serve as cryptographic proof.
        if protected_deletions:
            for p in additions:
                if p not in tainted_paths and p not in proven_ordinary_renames:
                    ambiguous_paths.add(p)
        elif protected_blob_shas:
            sensitive_keywords = (
                "config", "setting", "secret", "credential", "token", "key",
                "env", "auth", "passwd", "password", "database", "admin"
            )
            sensitive_exts = {".cfg", ".ini", ".conf", ".env", ".properties"}
            for p in additions:
                if p not in tainted_paths and p not in proven_ordinary_renames:
                    p_path = Path(p)
                    stem_lower = p_path.stem.lower()
                    suffix_lower = p_path.suffix.lower()
                    if any(kw in stem_lower for kw in sensitive_keywords) or suffix_lower in sensitive_exts:
                        ambiguous_paths.add(p)

        return tainted_paths, ambiguous_paths

    def get_safe_diff(
        self,
        base_ref: str,
        target_ref: str,
        paths: Optional[List[str]] = None,
    ) -> str:
        """Generate unified diff between two refs, strictly restricted to non-protected paths.

        Invariants:
        1. Never runs unrestricted git diff over the trusted repository.
        2. When paths is None, enumerates changed files via diff-tree (--name-only -z --no-renames)
           and explicitly restricts the diff to verified non-protected paths.
        3. Rename detection is explicitly disabled during path enumeration using --no-renames.
        4. Tainted renames & copies: protected files renamed or copied to ordinary-looking filenames
           are detected via identical blob object IDs and rename tracking, and quarantined.
        5. Ambiguous additions: additions occurring in the same transition as protected deletions
           are quarantined, preventing rename-laundering of modified protected content.
        6. When paths is provided, validates each path, rejecting wildcards, Git pathspec
           magic, traversal, protected paths, tainted renames, and ambiguous additions.
        7. If zero eligible non-protected paths exist, returns empty string immediately
           without invoking diff content reading.
        8. Double-checks generated diff output to guarantee zero protected headers appear.
        9. Secret redaction is applied to all outputs and errors.
        """
        safe_paths: List[str] = []
        tainted_paths, ambiguous_paths = self._audit_provenance_and_metadata(base_ref, target_ref)
        disallowed_renames = tainted_paths | ambiguous_paths

        if paths is not None:
            # Caller specified paths: every path must be validated and non-protected
            if not paths:
                # Explicit empty list provided -> diff nothing
                return ""
            for p in paths:
                clean_p = self._validate_and_normalize_path(p, for_diff=True)
                if clean_p in tainted_paths:
                    raise GitMediatorSecurityError(
                        f"Security violation: diff requested for path renamed from protected sensitive path: '{clean_p}'"
                    )
                if clean_p in ambiguous_paths:
                    raise GitMediatorSecurityError(
                        f"Security violation: diff requested for ambiguous addition in commit transition with protected deletion or unproven provenance: '{clean_p}'"
                    )
                safe_paths.append(clean_p)
        else:
            # Caller did not specify paths: safely enumerate changed paths between refs without content
            # Rename detection is explicitly disabled during path enumeration using --no-renames
            res_names = self._run_git(["diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", base_ref, target_ref])
            if res_names.returncode != 0:
                err_msg = redact_secrets(res_names.stderr.strip() or res_names.stdout.strip())
                raise GitMediatorError(
                    f"Failed to enumerate changed paths between '{base_ref}' and '{target_ref}': {err_msg}"
                )

            changed_candidates = [p for p in res_names.stdout.split("\x00") if p]
            for p in changed_candidates:
                p_clean = p.replace("\\", "/")
                p_name = Path(p_clean).name
                # Exclude protected paths upfront without reading their content
                if is_protected_name(p_name) or is_protected_path(p_clean, base_dir=self._workspace_path):
                    continue
                # Exclude tainted renames and ambiguous additions
                if p_clean in disallowed_renames:
                    continue
                try:
                    valid_p = self._validate_and_normalize_path(p_clean)
                    safe_paths.append(valid_p)
                except (GitMediatorError, GitMediatorSecurityError):
                    continue

        if not safe_paths:
            # Zero eligible non-protected paths to diff: return empty string safely
            return ""

        # Execute diff explicitly restricted to verified safe_paths
        args = ["diff", "--no-ext-diff", base_ref, target_ref, "--"] + safe_paths
        res = self._run_git(args)
        if res.returncode != 0:
            err_msg = redact_secrets(res.stderr.strip() or res.stdout.strip())
            raise GitMediatorError(
                f"Failed to generate diff between {base_ref} and {target_ref}: {err_msg}"
            )

        diff_text = res.stdout
        # Double-check diff text does not contain protected file headers
        for line in diff_text.splitlines():
            if line.startswith("diff --git"):
                parts = line.split()
                for part in parts[2:]:
                    part_clean = part.lstrip("ab/")
                    if is_protected_name(Path(part_clean).name) or is_protected_path(part_clean, base_dir=self._workspace_path):
                        raise GitMediatorSecurityError(
                            f"Security violation: generated diff exposed protected path: '{part_clean}'"
                        )

        return redact_secrets(diff_text)

    # =========================================================================
    # Workspace Bridge Operations
    # =========================================================================

    def export_sanitized_workspace(self, dest_dir: Path, ref: str = "HEAD") -> int:
        """Export clean source files at ref into dest_dir, ensuring zero protected files or .git enter dest_dir."""
        dest_path = Path(dest_dir).resolve()
        dest_path.mkdir(parents=True, exist_ok=True)

        cmd_archive = [
            "git",
            "-C", str(self._trusted_git_path),
            "-c", "core.hooksPath=/dev/null",
            "-c", f"safe.directory={self._trusted_git_path}",
            "archive",
            ref,
        ]

        tar_proc = subprocess.Popen(
            cmd_archive,
            cwd=str(self._trusted_git_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._get_sanitized_git_env(self._trusted_git_path),
        )

        import tarfile

        exported_count = 0
        if tar_proc.stdout is None:
            raise GitMediatorError("Failed to open pipe from git archive")

        with tarfile.open(fileobj=tar_proc.stdout, mode="r|") as tar:
            for member in tar:
                m_path = Path(member.name)
                # Disallow path traversal
                if member.name.startswith("/") or ".." in m_path.parts:
                    continue
                # Disallow .git entries
                if ".git" in m_path.parts:
                    continue
                # Disallow protected files
                if is_protected_name(m_path.name) or is_protected_path(member.name, base_dir=dest_path):
                    continue

                if hasattr(tarfile, "data_filter"):
                    tar.extract(member, path=dest_path, filter="data")
                else:
                    tar.extract(member, path=dest_path)
                exported_count += 1

        tar_proc.wait()
        if tar_proc.returncode != 0:
            err = tar_proc.stderr.read().decode("utf-8", errors="replace") if tar_proc.stderr else ""
            raise GitMediatorError(f"git archive failed during workspace export: {redact_secrets(err)}")

        return exported_count
