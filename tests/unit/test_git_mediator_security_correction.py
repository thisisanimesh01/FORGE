"""Security verification tests for GitMediator hardening.

Covers:
1. get_safe_diff(paths=None) excludes every protected file.
2. Explicit protected paths are rejected with GitMediatorSecurityError.
3. Wildcard, glob, and Git pathspec magic attempts fail closed.
4. Ordinary source diffs still work.
5. Host Git environment variables cannot redirect repository or object access.
6. Error output does not reveal protected content.
"""

import os
from pathlib import Path
import subprocess
import pytest

from forge.repository.git_mediator import (
    GitMediator,
    GitMediatorError,
    GitMediatorSecurityError,
)


@pytest.fixture
def synthetic_security_repo(tmp_path: Path):
    """Create a synthetic Git repository containing protected and ordinary files with commit history."""
    repo = tmp_path / "trusted_upstream.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Security Auditor"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "auditor@forge.local"], cwd=repo, check=True, capture_output=True)

    # Base commit: initial state
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def run():\n    return 'v1'\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v1\n", encoding="utf-8")
    (repo / ".env").write_text("SUPER_SECRET_KEY=initial_secret_111\n", encoding="utf-8")
    (repo / "contacts.json").write_text('{"contacts": ["alice"]}\n', encoding="utf-8")
    (repo / "app.db").write_text("sqlite-db-binary-initial\n", encoding="utf-8")
    (repo / "app.log").write_text("initial application log\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 1: Initial files"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Target commit: updates to ordinary files AND protected files
    (repo / "src" / "main.py").write_text("def run():\n    return 'v2'\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v2\n", encoding="utf-8")
    (repo / ".env").write_text("SUPER_SECRET_KEY=updated_secret_999\n", encoding="utf-8")
    (repo / "contacts.json").write_text('{"contacts": ["alice", "bob"]}\n', encoding="utf-8")
    (repo / "app.db").write_text("sqlite-db-binary-updated\n", encoding="utf-8")
    (repo / "app.log").write_text("appended application log line\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 2: Update all files"], cwd=repo, check=True, capture_output=True)
    target_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    workspace = tmp_path / "analysis_workspace"
    workspace.mkdir()

    return {
        "repo_path": repo,
        "workspace_path": workspace,
        "base_sha": base_sha,
        "target_sha": target_sha,
    }


# =============================================================================
# Test 1: get_safe_diff(paths=None) excludes every protected file
# =============================================================================

def test_safe_diff_unrestricted_paths_excludes_every_protected_file(synthetic_security_repo):
    """When paths=None, diff must exclude .env, contacts.json, app.db, and app.log."""
    data = synthetic_security_repo
    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    diff = mediator.get_safe_diff(data["base_sha"], data["target_sha"], paths=None)

    # Ordinary files must be present in diff
    assert "src/main.py" in diff
    assert "README.md" in diff
    assert "-    return 'v1'" in diff
    assert "+    return 'v2'" in diff

    # Protected files must NOT appear in diff text anywhere
    assert ".env" not in diff
    assert "contacts.json" not in diff
    assert "app.db" not in diff
    assert "app.log" not in diff
    assert "initial_secret_111" not in diff
    assert "updated_secret_999" not in diff
    assert "alice" not in diff
    assert "bob" not in diff


def test_safe_diff_when_only_protected_files_changed(synthetic_security_repo):
    """When only protected files are modified between refs, diff must return empty string."""
    data = synthetic_security_repo
    repo = data["repo_path"]

    # Commit 3: Only update .env and app.log
    (repo / ".env").write_text("SUPER_SECRET_KEY=secret_333\n", encoding="utf-8")
    (repo / "app.log").write_text("log 333\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env", "app.log"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 3: Only protected changes"], cwd=repo, check=True, capture_output=True)
    commit3_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    diff = mediator.get_safe_diff(data["target_sha"], commit3_sha, paths=None)
    assert diff == ""


# =============================================================================
# Test 2: Explicit protected paths are rejected
# =============================================================================

@pytest.mark.parametrize("protected_path", [
    ".env",
    ".env.local",
    "contacts.json",
    "app.db",
    "app.log",
    "nested/.env.prod",
    "sub/contacts.json",
    "deep/cache.sqlite3.db",
    "logs/trace.log",
])
def test_explicit_protected_paths_rejected(synthetic_security_repo, protected_path: str):
    """Explicitly requesting a protected path in get_safe_diff or get_file_content_at_ref must raise GitMediatorSecurityError."""
    data = synthetic_security_repo
    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    with pytest.raises(GitMediatorSecurityError, match="access to protected sensitive path|diff requested for protected"):
        mediator.get_safe_diff(data["base_sha"], data["target_sha"], paths=[protected_path])

    with pytest.raises(GitMediatorSecurityError, match="access to protected sensitive path"):
        mediator.get_file_content_at_ref(data["target_sha"], protected_path)


# =============================================================================
# Test 3: Wildcards and Git pathspec magic fail closed
# =============================================================================

@pytest.mark.parametrize("malicious_path", [
    ":*",
    ":(top)src/main.py",
    ":(glob)*.py",
    ":(literal).env",
    ":(icase)readme.md",
    ":!src/main.py",
    ":^src/main.py",
    ":/src/main.py",
    "*.py",
    "src/*",
    "src/?.py",
    "src/[a-z].py",
    "../trusted_upstream.git/.env",
    "src/../../.env",
    "/etc/passwd",
    "\\Windows\\System32",
    "src/main.py\x00.env",
])
def test_wildcards_and_git_pathspec_magic_fail_closed(synthetic_security_repo, malicious_path: str):
    """Wildcards, pathspec magic, traversals, and control characters must fail closed with GitMediatorSecurityError."""
    data = synthetic_security_repo
    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    with pytest.raises(GitMediatorSecurityError):
        mediator.get_safe_diff(data["base_sha"], data["target_sha"], paths=[malicious_path])

    with pytest.raises(GitMediatorSecurityError):
        mediator.get_file_content_at_ref(data["target_sha"], malicious_path)


# =============================================================================
# Test 4: Ordinary source diffs still work
# =============================================================================

def test_ordinary_source_diffs_work(synthetic_security_repo):
    """Diffing specific ordinary source files must work properly and return diff content."""
    data = synthetic_security_repo
    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    diff = mediator.get_safe_diff(data["base_sha"], data["target_sha"], paths=["src/main.py"])
    assert "diff --git a/src/main.py b/src/main.py" in diff
    assert "-    return 'v1'" in diff
    assert "+    return 'v2'" in diff
    assert "README.md" not in diff

    # Diff multiple ordinary files
    diff_multi = mediator.get_safe_diff(data["base_sha"], data["target_sha"], paths=["src/main.py", "README.md"])
    assert "src/main.py" in diff_multi
    assert "README.md" in diff_multi


# =============================================================================
# Test 5: Host Git environment variables cannot redirect repository or objects
# =============================================================================

def test_host_git_environment_cannot_redirect_repository_or_objects(synthetic_security_repo, tmp_path: Path, monkeypatch):
    """Setting hostile GIT_* environment variables in os.environ must NOT affect GitMediator operations."""
    data = synthetic_security_repo
    fake_repo = tmp_path / "hostile_fake_repo"
    fake_repo.mkdir()

    # Set up hostile environment variables
    monkeypatch.setenv("GIT_DIR", str(fake_repo))
    monkeypatch.setenv("GIT_WORK_TREE", str(fake_repo))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(fake_repo))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(fake_repo))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_repo / "fake_config"))
    monkeypatch.setenv("GIT_HOOKS_PATH", str(fake_repo / "hooks"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "echo_hack")

    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    # 1. get_upstream_commit_sha must resolve authentic commit, not hostile dir
    sha = mediator.get_upstream_commit_sha()
    assert sha == data["target_sha"]

    # 2. list_tree_paths must work against trusted repo
    paths = mediator.list_tree_paths("HEAD")
    assert "src/main.py" in paths
    assert "README.md" in paths

    # 3. get_safe_diff must succeed against trusted repo
    diff = mediator.get_safe_diff(data["base_sha"], data["target_sha"], paths=["src/main.py"])
    assert "diff --git a/src/main.py b/src/main.py" in diff

    # 4. verify_trusted_integrity must pass against trusted repo
    assert mediator.verify_trusted_integrity() is True


# =============================================================================
# Test 6: Error output does not reveal protected content
# =============================================================================

def test_error_output_does_not_reveal_protected_content(synthetic_security_repo):
    """Error messages from Git or mediator must never leak protected keys or sensitive strings."""
    data = synthetic_security_repo
    mediator = GitMediator(
        trusted_git_path=data["repo_path"],
        workspace_path=data["workspace_path"],
    )

    # 1. Attempting to access protected path produces clean security error without leaking secrets
    try:
        mediator.get_file_content_at_ref("HEAD", ".env")
    except GitMediatorSecurityError as e:
        err_msg = str(e)
        assert "SUPER_SECRET_KEY" not in err_msg
        assert "initial_secret" not in err_msg

    # 2. Non-existent path error does not leak secrets
    try:
        mediator.get_file_content_at_ref("HEAD", "sk-secret1234567890abcdef.txt")
    except Exception as e:
        err_msg = str(e)
        assert "sk-secret1234567890abcdef" not in err_msg
        assert "[REDACTED" in err_msg or "sk-secret" not in err_msg


# =============================================================================
# Test 7: Protected file renamed to ordinary filename cannot bypass policy
# =============================================================================

def test_protected_file_renamed_to_ordinary_filename_cannot_bypass_policy(tmp_path: Path):
    """If .env is renamed to ordinary_config.txt, it must not be exposed in get_safe_diff."""
    repo = tmp_path / "rename_repo.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True, capture_output=True)

    # Commit 1: Add .env and README.md
    (repo / ".env").write_text("HIGHLY_CONFIDENTIAL_SECRET=secret999\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project Docs v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 1"], cwd=repo, check=True, capture_output=True)
    commit1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 2: Rename .env to ordinary_config.txt and modify README.md
    subprocess.run(["git", "mv", ".env", "ordinary_config.txt"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Project Docs v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 2: Rename .env to ordinary_config.txt"], cwd=repo, check=True, capture_output=True)
    commit2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    workspace = tmp_path / "analysis_workspace"
    workspace.mkdir()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=workspace)

    # 1. Automatic enumeration (paths=None) MUST exclude ordinary_config.txt
    diff = mediator.get_safe_diff(commit1, commit2, paths=None)
    assert "README.md" in diff
    assert "ordinary_config.txt" not in diff
    assert ".env" not in diff
    assert "HIGHLY_CONFIDENTIAL_SECRET" not in diff

    # 2. Explicit request for ordinary_config.txt MUST fail closed with GitMediatorSecurityError
    with pytest.raises(GitMediatorSecurityError, match="path renamed from protected sensitive path"):
        mediator.get_safe_diff(commit1, commit2, paths=["ordinary_config.txt"])


# =============================================================================
# Test 8: NUL-delimited paths with spaces, tabs, and newlines
# =============================================================================

def test_path_enumeration_with_spaces_tabs_and_newlines(tmp_path: Path):
    """Filenames with spaces, tabs, and newlines must be parsed with 100% fidelity."""
    repo = tmp_path / "special_chars_repo.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True, capture_output=True)

    # Commit 1
    (repo / "file with spaces.txt").write_text("v1 spaces\n", encoding="utf-8")
    (repo / "file\twith\ttabs.txt").write_text("v1 tabs\n", encoding="utf-8")
    (repo / "file\nwith\nnewlines.txt").write_text("v1 newlines\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=xyz\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 2
    (repo / "file with spaces.txt").write_text("v2 spaces\n", encoding="utf-8")
    (repo / "file\twith\ttabs.txt").write_text("v2 tabs\n", encoding="utf-8")
    (repo / "file\nwith\nnewlines.txt").write_text("v2 newlines\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=xyz_updated\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit 2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    workspace = tmp_path / "analysis_workspace"
    workspace.mkdir()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=workspace)

    # Automatic diff: spaces, tabs, and newlines must diff properly, .env excluded
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "file with spaces.txt" in diff
    assert ("file\twith\ttabs.txt" in diff or "file\\twith\\ttabs.txt" in diff)
    assert ("file\nwith\nnewlines.txt" in diff or "file\\nwith\\nnewlines.txt" in diff)
    assert "v2 tabs" in diff
    assert "v2 newlines" in diff
    assert "v2 spaces" in diff
    assert ".env" not in diff
    assert "SECRET" not in diff


# =============================================================================
# Test 9: Ordinary file additions, deletions, modifications, and renames
# =============================================================================

def test_ordinary_file_lifecycle_operations(tmp_path: Path):
    """Ordinary files undergoing add, delete, modify, and rename must diff cleanly."""
    repo = tmp_path / "lifecycle_repo.git"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True, capture_output=True)

    (repo / "math_old.py").write_text("def calc(): return 1\n", encoding="utf-8")
    (repo / "to_delete.py").write_text("def obsolete(): pass\n", encoding="utf-8")
    (repo / "modified.py").write_text("def mod(): return 'old'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 2: Rename math_old -> math_new, delete to_delete, modify modified, add brand_new
    subprocess.run(["git", "mv", "math_old.py", "math_new.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "rm", "to_delete.py"], cwd=repo, check=True, capture_output=True)
    (repo / "modified.py").write_text("def mod(): return 'new'\n", encoding="utf-8")
    (repo / "brand_new.py").write_text("def fresh(): return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    workspace = tmp_path / "analysis_workspace"
    workspace.mkdir()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=workspace)

    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "math_new.py" in diff or "math_old.py" in diff
    assert "to_delete.py" in diff
    assert "modified.py" in diff
    assert "brand_new.py" in diff


# =============================================================================
# Test 10: Enumeration failure fails closed
# =============================================================================

def test_enumeration_failure_fails_closed(synthetic_security_repo):
    """Invalid refs or enumeration failure must fail closed with GitMediatorError."""
    data = synthetic_security_repo
    mediator = GitMediator(trusted_git_path=data["repo_path"], workspace_path=data["workspace_path"])

    with pytest.raises(GitMediatorError):
        mediator.get_safe_diff("invalid_nonexistent_ref_1", "invalid_nonexistent_ref_2")
