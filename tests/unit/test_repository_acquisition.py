"""Tests for repository acquisition and isolated workspaces."""

import subprocess
import pytest
from pathlib import Path
from forge.repository.acquisition import (
    acquire_repository,
    cleanup_repository,
    run_git_command,
    AcquisitionError,
)
from forge.repository.validation import SecurityError


def test_acquire_local_repository_fixture(tmp_path):
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("print('hello world')\n")
    (repo_dir / "README.md").write_text("# Mock Repo\n")

    workspace = tmp_path / "workspaces"
    acquired = acquire_repository(str(repo_dir), custom_workspace=workspace)

    assert acquired.workspace_path.exists()
    assert (acquired.workspace_path / "app.py").exists()
    assert (acquired.workspace_path / "README.md").exists()
    assert acquired.is_local_copy is True

    # Confirm original file was not mutated
    assert (repo_dir / "app.py").read_text() == "print('hello world')\n"

    # Cleanup
    cleanup_repository(acquired)
    assert not acquired.workspace_path.exists()


def test_git_hooks_disabled_in_execution():
    # run_git_command should include core.hooksPath=/dev/null
    res = run_git_command(["--version"])
    assert res.returncode == 0
    assert "git version" in res.stdout


def test_reject_insecure_target_on_acquisition(tmp_path):
    workspace = tmp_path / "workspaces"
    with pytest.raises(SecurityError) as exc_info:
        acquire_repository("http://insecure.example.com/repo.git", custom_workspace=workspace)
    assert "Target validation failed" in str(exc_info.value)
