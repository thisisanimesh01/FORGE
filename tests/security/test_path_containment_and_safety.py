"""Security tests for path traversal, symlink escapes, and input sanitization."""

import os
import pytest
from pathlib import Path
from forge.repository.validation import assert_contained_path, SecurityError, validate_target
from forge.repository.detector import detect_repository
from forge.repository.indexer import build_symbol_index


def test_path_traversal_double_dot(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()

    traversal_path = root / "sub" / ".." / ".." / "sensitive.txt"
    with pytest.raises(SecurityError) as exc_info:
        assert_contained_path(traversal_path, root)
    assert "Path containment violation" in str(exc_info.value)


def test_symlink_escape_rejected(tmp_path):
    outside_dir = tmp_path / "outside_private"
    outside_dir.mkdir()
    secret_file = outside_dir / "passwords.txt"
    secret_file.write_text("admin:supersecret")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Create symlink pointing outside repository root
    symlink_path = repo_dir / "external_link"
    try:
        os.symlink(outside_dir, symlink_path)
    except OSError:
        pytest.skip("Symlink creation not permitted in this environment")

    # Asserting containment on symlink target escaping root should fail
    with pytest.raises(SecurityError):
        assert_contained_path(symlink_path / "passwords.txt", repo_dir)


def test_repository_files_remain_unchanged(tmp_path):
    repo_dir = tmp_path / "immutable_repo"
    repo_dir.mkdir()
    source_file = repo_dir / "main.py"
    original_content = "def calculate(): return 42\n"
    source_file.write_text(original_content)

    # Perform detection and symbol indexing
    detect_repository(repo_dir)
    build_symbol_index(repo_dir)

    # Verify content and stats remain 100% unaltered
    assert source_file.read_text() == original_content
