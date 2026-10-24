"""Tests for repository target validation and URL security."""

import pytest
from pathlib import Path
from forge.repository.validation import (
    validate_target,
    assert_contained_path,
    SecurityError,
    TargetType,
)


def test_valid_https_url():
    valid_urls = [
        "https://github.com/psf/requests.git",
        "https://github.com/pallets/flask",
        "https://gitlab.com/group/subgroup/project.git",
        "https://bitbucket.org/owner/repo",
    ]
    for url in valid_urls:
        res = validate_target(url)
        assert res.is_valid is True
        assert res.target_type == TargetType.REMOTE_GIT
        assert res.error is None


def test_reject_insecure_http():
    res = validate_target("http://github.com/psf/requests.git")
    assert res.is_valid is False
    assert "Insecure HTTP protocol is not permitted" in res.error


def test_reject_unsupported_schemes():
    unsupported = [
        "git://github.com/psf/requests.git",
        "ssh://git@github.com/psf/requests.git",
        "file:///etc/passwd",
        "ftp://example.com/repo.git",
    ]
    for url in unsupported:
        res = validate_target(url)
        assert res.is_valid is False
        assert "Unsupported protocol" in res.error


def test_reject_embedded_credentials():
    credentials_urls = [
        "https://username:password@github.com/owner/repo.git",
        "https://token@gitlab.com/owner/repo.git",
        "https://user:secret123@bitbucket.org/owner/repo.git",
    ]
    for url in credentials_urls:
        res = validate_target(url)
        assert res.is_valid is False
        assert "embedded credentials" in res.error.lower()
        # Credentials must not leak into sanitized output
        assert "password" not in res.sanitized_target
        assert "secret123" not in res.sanitized_target


def test_validate_local_directory(tmp_path):
    sub_dir = tmp_path / "valid_local_repo"
    sub_dir.mkdir()
    res = validate_target(str(sub_dir), allow_local=True)
    assert res.is_valid is True
    assert res.target_type == TargetType.LOCAL_DIRECTORY
    assert res.sanitized_target == str(sub_dir.resolve())


def test_reject_nonexistent_local_directory(tmp_path):
    fake_path = tmp_path / "does_not_exist"
    res = validate_target(str(fake_path), allow_local=True)
    assert res.is_valid is False
    assert "does not exist" in res.error.lower()


def test_path_containment_enforcement(tmp_path):
    root = tmp_path / "sandbox_root"
    root.mkdir()
    inside_file = root / "allowed.py"
    inside_file.write_text("print('ok')")

    # Valid containment
    assert assert_contained_path(inside_file, root) == inside_file.resolve()

    # Traversal attempt
    outside_file = tmp_path / "secret.env"
    outside_file.write_text("SECRET=123")
    with pytest.raises(SecurityError) as exc_info:
        assert_contained_path(outside_file, root)
    assert "Path containment violation" in str(exc_info.value)
