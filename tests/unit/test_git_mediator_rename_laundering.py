"""Focused verification tests for protected-file rename laundering prevention in GitMediator.

Covers the 6 required audit scenarios:
1. Rename .env to ordinary_config.txt without modification.
2. Rename .env to ordinary_config.txt while substantially changing its contents (50% modification).
3. Rename .env and modify almost all of its contents (90% modification).
4. Copy .env to ordinary_config.txt without deleting .env.
5. Delete .env and independently add an ordinary-looking file in the same commit.
6. Perform ordinary source-file renames and copies.
"""

from pathlib import Path
import subprocess
import pytest

from forge.repository.git_mediator import (
    GitMediator,
    GitMediatorError,
    GitMediatorSecurityError,
)


@pytest.fixture
def repo_factory(tmp_path: Path):
    """Factory creating isolated synthetic Git repositories for rename laundering tests."""
    counter = 0

    def _create_repo():
        nonlocal counter
        counter += 1
        repo = tmp_path / f"repo_{counter}.git"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Security Auditor"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "auditor@forge.local"], cwd=repo, check=True, capture_output=True)
        ws = tmp_path / f"ws_{counter}"
        ws.mkdir()
        return repo, ws

    return _create_repo


# =============================================================================
# Scenario 1: Rename .env to ordinary_config.txt without modification
# =============================================================================

def test_scenario_1_rename_without_modification(repo_factory):
    """Exact rename of .env to ordinary_config.txt must be detected via identical blob ID and rename tracking."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("API_KEY=secret_key_111\nDATABASE_URL=db://secret_url\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    subprocess.run(["git", "mv", ".env", "ordinary_config.txt"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Project v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    # Automatic diff must exclude ordinary_config.txt and .env
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "README.md" in diff
    assert "ordinary_config.txt" not in diff
    assert ".env" not in diff
    assert "secret_key_111" not in diff

    # Explicit request must fail closed
    with pytest.raises(GitMediatorSecurityError, match="diff requested for path renamed from protected sensitive path"):
        mediator.get_safe_diff(c1, c2, paths=["ordinary_config.txt"])


# =============================================================================
# Scenario 2: Rename .env while substantially changing contents (~50%)
# =============================================================================

def test_scenario_2_rename_with_substantial_modification(repo_factory):
    """Renaming .env and substantially changing content defeats git diff -M similarity, but is caught by ambiguous addition quarantine."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("LINE1=val1\nLINE2=val2\nLINE3=val3\nLINE4=val4\nLINE5=val5\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Delete .env and create ordinary_config.txt with 50% altered content
    subprocess.run(["git", "rm", ".env"], cwd=repo, check=True, capture_output=True)
    (repo / "ordinary_config.txt").write_text("LINE1=val1\nLINE2=val2\nMODIFIED3=xxx\nMODIFIED4=yyy\nMODIFIED5=zzz\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    # Automatic diff must quarantine ordinary_config.txt
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "README.md" in diff
    assert "ordinary_config.txt" not in diff
    assert ".env" not in diff

    # Explicit request must fail closed
    with pytest.raises(GitMediatorSecurityError, match="(path renamed from protected sensitive path|ambiguous addition)"):
        mediator.get_safe_diff(c1, c2, paths=["ordinary_config.txt"])


# =============================================================================
# Scenario 3: Rename .env and modify almost all of its contents (~90%)
# =============================================================================

def test_scenario_3_rename_with_almost_all_content_modified(repo_factory):
    """Renaming .env and modifying 90% of content is quarantined as an ambiguous addition in a protected deletion transition."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("A=1\nB=2\nC=3\nD=4\nE=5\nF=6\nG=7\nH=8\nI=9\nJ=10\n", encoding="utf-8")
    (repo / "app.py").write_text("def run(): return 'v1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    subprocess.run(["git", "rm", ".env"], cwd=repo, check=True, capture_output=True)
    (repo / "ordinary_config.txt").write_text("DIFFERENT1=1\nDIFFERENT2=2\nDIFFERENT3=3\nDIFFERENT4=4\nDIFFERENT5=5\n", encoding="utf-8")
    (repo / "app.py").write_text("def run(): return 'v2'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "app.py" in diff
    assert "ordinary_config.txt" not in diff
    assert ".env" not in diff

    with pytest.raises(GitMediatorSecurityError, match="ambiguous addition in commit transition with protected deletion"):
        mediator.get_safe_diff(c1, c2, paths=["ordinary_config.txt"])


# =============================================================================
# Scenario 4: Copy .env to ordinary_config.txt
# =============================================================================

def test_scenario_4_copy_protected_file_to_ordinary_name(repo_factory):
    """Copying .env to ordinary_config.txt without deleting .env must be detected via identical blob object ID."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("SECRET_DATABASE_PASSWORD=production_secret_pass\n", encoding="utf-8")
    (repo / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Copy .env to ordinary_config.txt, .env remains
    (repo / "ordinary_config.txt").write_text("SECRET_DATABASE_PASSWORD=production_secret_pass\n", encoding="utf-8")
    (repo / "main.py").write_text("def main(): return 'ok'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "main.py" in diff
    assert "ordinary_config.txt" not in diff
    assert "production_secret_pass" not in diff

    with pytest.raises(GitMediatorSecurityError, match="diff requested for path renamed from protected sensitive path"):
        mediator.get_safe_diff(c1, c2, paths=["ordinary_config.txt"])


# =============================================================================
# Scenario 5: Delete .env and independently add an ordinary-looking file
# =============================================================================

def test_scenario_5_delete_protected_and_independent_ordinary_addition(repo_factory):
    """When .env is deleted and an independent file is added in the same commit, metadata cannot prove provenance: it fails closed."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("OLD_SECRET=expired_key\n", encoding="utf-8")
    (repo / "docs.md").write_text("# Documentation v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Developer deletes .env and adds unrelated utility script
    subprocess.run(["git", "rm", ".env"], cwd=repo, check=True, capture_output=True)
    (repo / "utils.py").write_text("def format_string(s: str) -> str: return s.strip()\n", encoding="utf-8")
    (repo / "docs.md").write_text("# Documentation v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    # In automatic diff, utils.py is quarantined as ambiguous because .env was deleted in the same transition
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "docs.md" in diff
    assert "utils.py" not in diff
    assert ".env" not in diff

    # Explicit request fails closed
    with pytest.raises(GitMediatorSecurityError, match="ambiguous addition in commit transition with protected deletion"):
        mediator.get_safe_diff(c1, c2, paths=["utils.py"])


# =============================================================================
# Scenario 6: Ordinary source-file renames and copies
# =============================================================================

def test_scenario_6_ordinary_source_file_renames_and_copies(repo_factory):
    """Ordinary source files undergoing rename and copy when no protected files are deleted must work normally."""
    repo, ws = repo_factory()

    (repo / "math_old.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (repo / "source_copy.py").write_text("def helper(): return 'common'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Rename math_old -> math_new, and copy source_copy -> dest_copy
    subprocess.run(["git", "mv", "math_old.py", "math_new.py"], cwd=repo, check=True, capture_output=True)
    (repo / "dest_copy.py").write_text("def helper(): return 'common'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    # Automatic diff must cleanly include math_new.py / math_old.py and dest_copy.py
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "math_new.py" in diff or "math_old.py" in diff
    assert "dest_copy.py" in diff

    # Explicit requests must succeed
    diff_rename = mediator.get_safe_diff(c1, c2, paths=["math_new.py"])
    assert "math_new.py" in diff_rename

    diff_copy = mediator.get_safe_diff(c1, c2, paths=["dest_copy.py"])
    assert "dest_copy.py" in diff_copy


# =============================================================================
# Scenario 7: Copy protected file with unmodified source and substantial modification
# =============================================================================

def test_scenario_7a_copy_with_unmodified_source_detected_by_find_copies_harder(repo_factory):
    """When .env is unchanged and copied to ordinary_config.txt with substantial modification (~50%),
    --find-copies-harder inspects unmodified base files and identifies the copy from .env."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("API_KEY=123456789\nDB_PASS=secret\nHOST=localhost\nPORT=5432\nUSER=admin\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # .env is UNCHANGED. ordinary_config.txt is added with 50% modified lines.
    (repo / "ordinary_config.txt").write_text("API_KEY=123456789\nDB_PASS=secret\nHOST=0.0.0.0\nPORT=8080\nUSER=root\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    # Verify audit categorizes it as tainted
    tainted, ambiguous = mediator._audit_provenance_and_metadata(c1, c2)
    assert "ordinary_config.txt" in tainted

    # Automatic diff must exclude ordinary_config.txt and .env
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "README.md" in diff
    assert "ordinary_config.txt" not in diff
    assert ".env" not in diff

    # Explicit request fails closed
    with pytest.raises(GitMediatorSecurityError, match="diff requested for path renamed from protected sensitive path"):
        mediator.get_safe_diff(c1, c2, paths=["ordinary_config.txt"])


def test_scenario_7b_copy_with_unmodified_source_fails_closed_when_similarity_fails(repo_factory):
    """When .env is unchanged and copied content is completely rewritten (0% similarity),
    similarity heuristics report status A. Because provenance cannot be established and the addition
    exhibits configuration characteristics in the presence of protected repository files,
    it is quarantined under the conservative fail-closed policy."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("API_KEY=123456789\nDB_PASS=secret\nHOST=localhost\nPORT=5432\nUSER=admin\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # .env is UNCHANGED. ordinary_config.txt is added with 100% altered lines (0% similarity).
    (repo / "ordinary_config.txt").write_text("FOO=bar\nBAZ=qux\nALPHA=beta\nGAMMA=delta\nZETA=omega\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    # Verify audit categorizes it under ambiguous additions via fail-closed policy
    tainted, ambiguous = mediator._audit_provenance_and_metadata(c1, c2)
    assert "ordinary_config.txt" in ambiguous

    # Automatic diff must exclude ordinary_config.txt and .env
    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "README.md" in diff
    assert "ordinary_config.txt" not in diff
    assert ".env" not in diff

    # Explicit request fails closed
    with pytest.raises(GitMediatorSecurityError, match="ambiguous addition in commit transition with protected deletion or unproven provenance"):
        mediator.get_safe_diff(c1, c2, paths=["ordinary_config.txt"])


def test_scenario_7c_ordinary_code_addition_works_when_protected_files_exist(repo_factory):
    """Adding an ordinary source file (e.g. service.py) when .env exists in base_ref works normally."""
    repo, ws = repo_factory()

    (repo / ".env").write_text("SECRET=xyz\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Add regular code file service.py
    (repo / "service.py").write_text("def compute(): return 42\n", encoding="utf-8")
    (repo / "README.md").write_text("# Project v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=repo, check=True, capture_output=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    mediator = GitMediator(trusted_git_path=repo, workspace_path=ws)

    diff = mediator.get_safe_diff(c1, c2, paths=None)
    assert "service.py" in diff
    assert "README.md" in diff
    assert ".env" not in diff

    diff_explicit = mediator.get_safe_diff(c1, c2, paths=["service.py"])
    assert "service.py" in diff_explicit
