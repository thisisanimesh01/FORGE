"""Phase 10 Final Security Boundary Audit Tests.

Verifies the actual enforcement boundaries protecting the trusted Git mirror (Option C)
against direct access, same-user access, investigation tool bypasses, path traversal,
symlink escapes, arbitrary commands, and Docker sandbox mounts.
"""

import json
import os
import stat
import subprocess
from pathlib import Path
import pytest

from forge.repository.acquisition import (
    acquire_repository,
    cleanup_repository,
    run_git_command,
)
from forge.repository.git_mediator import (
    GitMediator,
    GitMediatorError,
    GitMediatorSecurityError,
)
from forge.investigation.tools import ToolRegistry
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import SandboxRequest, ExecutionLimits
from forge.sandbox.policy import validate_sandbox_request, PolicyViolationError
from forge.publication.workspace import PublicationWorkspace
from forge.publication.commit import GitCommitBuilder, PatchStagingError
from forge.repair.execution.models import PatchArtifact


@pytest.fixture
def synthetic_audit_repo(tmp_path: Path) -> Path:
    """Create a synthetic upstream repository containing protected and ordinary files."""
    src = tmp_path / "upstream_repo"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Audit Dev"], cwd=src)
    run_git_command(["config", "user.email", "dev@audit.local"], cwd=src)

    # Commit 1: Add ordinary and protected files
    (src / "app.py").write_text("def run(): return 42\n", encoding="utf-8")
    (src / ".env").write_text("SYNTHETIC_API_KEY=secret_token_12345\n", encoding="utf-8")
    (src / "contacts.json").write_text('{"users": [{"name": "Alice"}]}\n', encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Initial commit with synthetic secrets"], cwd=src)

    return src


# =============================================================================
# 1. Same-User OS Identity & Permission Analysis
# =============================================================================

def test_same_user_identity_and_limitation_of_mode_0700(synthetic_audit_repo: Path, tmp_path: Path):
    """Verify that mode 0700 isolates between OS accounts but NOT within the same UID.

    Confirms that the host process and the mirror owner share the same UID,
    demonstrating why mode 0700 alone is not an in-process barrier, and proving
    that application-level tool boundaries and container boundaries are required.
    """
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        assert acquired.trusted_git_path is not None
        assert acquired.trusted_git_path.exists()

        # Check directory mode is 0700
        mirror_stat = acquired.trusted_git_path.stat()
        mode_permissions = stat.S_IMODE(mirror_stat.st_mode)
        assert mode_permissions == 0o700, f"Expected mode 0700, got {oct(mode_permissions)}"

        # The process owner and the directory owner are identical (same UID)
        current_uid = os.getuid()
        assert mirror_stat.st_uid == current_uid, "Mirror is owned by the current process UID"

        # Because UIDs match, standard POSIX filesystem permissions allow the host Python process
        # to read the directory directly if using raw unmediated open()
        raw_config = acquired.trusted_git_path / ".git" / "config"
        if not raw_config.exists():
            raw_config = acquired.trusted_git_path / "config"
        assert raw_config.exists()
        # Direct raw read succeeds for the same user:
        direct_data = raw_config.read_text(encoding="utf-8", errors="replace")
        assert "core" in direct_data

        # This proves the security invariant: mode 0700 alone does NOT prevent same-user access.
        # Enforcement against agents/models/sandboxes MUST rely on tool gating and Docker isolation.
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 2. Tool Boundaries: Absolute Path & Traversal Protection
# =============================================================================

def test_investigation_tool_rejects_reading_trusted_mirror_by_absolute_path(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """Investigation tool 'read_source_excerpt' strictly rejects reading the trusted mirror by absolute path."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        registry = ToolRegistry(workspace_path=acquired.workspace_path)
        trusted_config_file = str(acquired.trusted_git_path / ".git" / "config")

        # Attempt to read trusted mirror file via absolute path
        raw_result = registry.execute_tool(
            "read_source_excerpt",
            {"file_path": trusted_config_file, "start_line": 1, "end_line": 10},
        )
        res = json.loads(raw_result)
        assert "error" in res
        assert (
            "Security violation" in res["error"]
            or "Invalid repository-relative path" in res["error"]
            or "path traversal" in res["error"]
        )

        # Attempt to read protected file in mirror via absolute path
        raw_result_env = registry.execute_tool(
            "read_source_excerpt",
            {"file_path": str(acquired.trusted_git_path / ".env"), "start_line": 1, "end_line": 10},
        )
        res_env = json.loads(raw_result_env)
        assert "error" in res_env
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_investigation_tool_rejects_listing_trusted_mirror_directory(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """Investigation tool 'list_directory' strictly rejects listing the trusted mirror directory."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        registry = ToolRegistry(workspace_path=acquired.workspace_path)

        # 1. Attempt relative traversal to peer trusted mirror
        mirror_name = acquired.trusted_git_path.name
        traversal_query = f"../{mirror_name}"
        raw_result_rel = registry.execute_tool(
            "list_directory",
            {"relative_path": traversal_query},
        )
        res_rel = json.loads(raw_result_rel)
        assert "error" in res_rel
        assert "Security violation" in res_rel["error"]
        assert "outside permitted root" in res_rel["error"]

        # 2. Attempt absolute path query to trusted mirror
        raw_result_abs = registry.execute_tool(
            "list_directory",
            {"relative_path": str(acquired.trusted_git_path)},
        )
        res_abs = json.loads(raw_result_abs)
        assert "error" in res_abs
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_investigation_tool_rejects_symlink_pointing_to_trusted_mirror(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """Investigation tools reject symlinks within analysis workspace that point to trusted mirror."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        # Create a symlink inside the analysis workspace pointing to the trusted mirror
        escape_symlink = acquired.workspace_path / "mirror_symlink"
        try:
            os.symlink(acquired.trusted_git_path, escape_symlink)
        except OSError:
            pytest.skip("Symlink creation not permitted in environment")

        registry = ToolRegistry(workspace_path=acquired.workspace_path)

        # Attempt to read config through symlink
        raw_read = registry.execute_tool(
            "read_source_excerpt",
            {"file_path": "mirror_symlink/.git/config", "start_line": 1, "end_line": 5},
        )
        res_read = json.loads(raw_read)
        assert "error" in res_read
        assert "Security violation" in res_read["error"] or "symlink escape" in res_read["error"].lower()

        # Attempt to list through symlink: list_directory skips outside symlinks
        raw_list = registry.execute_tool(
            "list_directory",
            {"relative_path": "mirror_symlink"},
        )
        res_list = json.loads(raw_list)
        # assert_contained_path triggers on the symlink target
        assert "error" in res_list
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 3. Arbitrary Command Execution & Sandbox Policy Gating
# =============================================================================

def test_sandbox_policy_strictly_rejects_git_executable(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """Command execution policy strictly rejects invoking 'git' in any sandbox check."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        # Attempt: git -C <trusted_git_path> status
        req_status = SandboxRequest(
            request_id="req_test_01",
            run_id="run_audit_01",
            workspace_path=acquired.workspace_path,
            command=["git", "-C", str(acquired.trusted_git_path), "status"],
        )
        with pytest.raises(PolicyViolationError) as exc_status:
            validate_sandbox_request(req_status)
        assert "is not in the approved sandbox allowlist" in str(exc_status.value)

        # Attempt: git -C <trusted_git_path> show HEAD:.env
        req_show = SandboxRequest(
            request_id="req_test_02",
            run_id="run_audit_01",
            workspace_path=acquired.workspace_path,
            command=["git", "-C", str(acquired.trusted_git_path), "show", "HEAD:.env"],
        )
        with pytest.raises(PolicyViolationError) as exc_show:
            validate_sandbox_request(req_show)
        assert "is not in the approved sandbox allowlist" in str(exc_show.value)

        # Attempt invocation through ToolRegistry
        registry = ToolRegistry(workspace_path=acquired.workspace_path)
        raw_res = registry.execute_tool(
            "run_sandbox_check",
            {"command": ["git", "-C", str(acquired.trusted_git_path), "status"]},
        )
        parsed = json.loads(raw_res)
        assert parsed["status"] == "rejected"
        assert "not in the approved sandbox allowlist" in parsed["error_message"]
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_docker_sandbox_mount_never_includes_trusted_mirror(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """DockerSandbox mount builder mounts ONLY the analysis workspace, never trusted storage."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        sandbox = DockerSandbox(docker_binary="docker", docker_image="python:3.11-slim")
        req = SandboxRequest(
            request_id="req_mount_01",
            run_id="run_audit_01",
            workspace_path=acquired.workspace_path,
            command=["pytest"],
        )

        args = sandbox.build_docker_arguments(req, "test_container_name")

        # 1. Mount arguments inspect
        mount_args = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
        assert len(mount_args) == 2  # /workspace and /tmp

        # The workspace mount points strictly to acquired.workspace_path
        ws_mount = next(m for m in mount_args if "target=/workspace" in m)
        assert f"source={acquired.workspace_path.resolve().as_posix()}" in ws_mount

        # The trusted_git_path is completely absent from all container arguments
        trusted_posix = acquired.trusted_git_path.resolve().as_posix()
        for arg in args:
            assert trusted_posix not in arg, f"Trusted mirror path leaked in Docker arg: {arg}"

        # 2. Strict isolation options present
        assert "--network" in args and args[args.index("--network") + 1] == "none"
        assert "--read-only" in args
        assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
        assert "--user" in args and args[args.index("--user") + 1] == "1000:1000"
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 4. GitMediator Authorized Operations & Fail-Closed Enforcement
# =============================================================================

def test_git_mediator_authorized_interface_and_fail_closed(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """GitMediator permits only approved queries, rejects protected paths, and redacts secrets."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        mediator: GitMediator = acquired.mediator
        assert mediator is not None

        # 1. Approved queries succeed
        commit_sha = mediator.get_upstream_commit_sha()
        assert commit_sha == acquired.resolved_commit_sha

        meta = mediator.get_commit_metadata(commit_sha)
        assert meta["commit_sha"] == commit_sha

        paths = mediator.list_tree_paths(commit_sha)
        assert "app.py" in paths
        # Protected files are filtered from tree paths
        assert ".env" not in paths
        assert "contacts.json" not in paths

        # 2. Ordinary file content query succeeds
        app_content = mediator.get_file_content_at_ref(commit_sha, "app.py")
        assert "def run():" in app_content

        # 3. Protected file query strictly fails closed
        with pytest.raises(GitMediatorSecurityError, match="access to protected sensitive path"):
            mediator.get_file_content_at_ref(commit_sha, ".env")

        with pytest.raises(GitMediatorSecurityError, match="access to protected sensitive path"):
            mediator.get_file_content_at_ref(commit_sha, "contacts.json")

        # 4. Safe diff query fails closed for protected files
        with pytest.raises(GitMediatorSecurityError, match="diff requested for protected sensitive path"):
            mediator.get_safe_diff(commit_sha, commit_sha, paths=[".env"])

        # 5. Safe diff for ordinary file succeeds
        diff_text = mediator.get_safe_diff(commit_sha, commit_sha, paths=["app.py"])
        assert diff_text is not None

        # 6. Arbitrary git operations are NOT exposed on GitMediator
        # (GitMediator has no generic execute() or run_arbitrary() method)
        assert not hasattr(mediator, "execute")
        assert not hasattr(mediator, "run_command")
        assert not hasattr(mediator, "shell")
    finally:
        cleanup_repository(acquired, allowed_base=ws)


# =============================================================================
# 5. Publication Boundary: Protection Against Modifying Protected Paths
# =============================================================================

def test_publication_workspace_rejects_protected_patch_staging(
    synthetic_audit_repo: Path, tmp_path: Path
):
    """PublicationWorkspace only accesses mirror for controlled publication and rejects protected patches."""
    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(synthetic_audit_repo), custom_workspace=ws, allow_local=True)

    try:
        pub_ws = PublicationWorkspace(
            target_repo_path=acquired.trusted_git_path,
            base_commit_sha=acquired.resolved_commit_sha,
            scratch_root=tmp_path / "pub_scratch",
        )
        pub_ws.prepare()

        diff_malicious = """--- a/.env
+++ b/.env
@@ -1,1 +1,1 @@
-SYNTHETIC_API_KEY=secret_token_12345
+SYNTHETIC_API_KEY=modified_token_99999
"""
        import hashlib
        diff_hash = hashlib.sha256(diff_malicious.encode("utf-8")).hexdigest()

        patch = PatchArtifact(
            artifact_id="art_mal_01",
            repair_id="rep_mal_01",
            finding_id="find_mal_01",
            target_file=".env",
            base_commit_sha=acquired.resolved_commit_sha,
            diff_content=diff_malicious,
            content_hash=diff_hash,
            affected_paths=[".env"],
            lines_added=1,
            lines_deleted=1,
            hunks_count=1,
        )

        builder = GitCommitBuilder()
        with pytest.raises(PatchStagingError, match="Security violation: patch attempts to modify protected sensitive file"):
            builder.apply_and_stage_patch(
                workspace=pub_ws,
                patch=patch,
                allowed_files=[".env"],
            )

        pub_ws.cleanup()
    finally:
        cleanup_repository(acquired, allowed_base=ws)
