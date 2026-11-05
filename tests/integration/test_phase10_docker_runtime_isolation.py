"""Real Docker runtime isolation verification for Phase 10 Git metadata isolation.

Verifies against a live Docker daemon:
1. Ordinary files in /workspace can be accessed by the container.
2. The trusted Git mirror directory cannot be accessed by the container.
3. Symlinks pointing to the trusted Git mirror cannot be traversed inside the container.
4. Trusted Git object files cannot be discovered through container mounts.
5. Trusted mirror path is absent from mounts and environment variables.
6. Container runs with non-root user (1000:1000), no network, read-only root filesystem,
   dropped capabilities (CapEff=0, NoNewPrivs=1).
"""

import os
from pathlib import Path
import pytest
import subprocess

from forge.repository.acquisition import acquire_repository, cleanup_repository
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.models import (
    ExecutionLimits,
    ExecutionStatus,
    NetworkPolicy,
    SandboxRequest,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not os.environ.get("FORGE_DOCKER_TESTS"),
        reason="Real Docker runtime test. Opt-in via FORGE_DOCKER_TESTS=1.",
    ),
]


def test_docker_runtime_git_metadata_isolation(tmp_path: Path):
    """Verify live Docker container cannot access trusted Git mirror under any vector."""
    sandbox = DockerSandbox()
    healthy, msg = sandbox.check_health()
    if not healthy:
        pytest.fail(f"Docker health check failed: {msg}")

    # 1. Create a synthetic upstream repository
    upstream = tmp_path / "upstream_repo"
    upstream.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "audit@forge.local"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Audit Runner"], cwd=upstream, check=True, capture_output=True)

    (upstream / "app.py").write_text("print('hello world')\n", encoding="utf-8")
    (upstream / ".env").write_text("SUPER_SECRET_KEY=secret123\n", encoding="utf-8")
    (upstream / "contacts.json").write_text('{"contacts": ["alice", "bob"]}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with secrets"], cwd=upstream, check=True, capture_output=True)

    # 2. Acquire via FORGE acquisition (creates workspace_path and trusted_git_path)
    acquired = acquire_repository(upstream.as_posix(), branch="main")
    try:
        workspace = acquired.workspace_path
        trusted_git = acquired.trusted_git_path

        assert workspace.exists()
        assert trusted_git.exists()
        assert not (workspace / ".env").exists()
        assert not (workspace / "contacts.json").exists()

        # Workspace local git contains only sanitized files
        ls_files = subprocess.run(["git", "ls-files"], cwd=workspace, capture_output=True, text=True, check=True)
        assert ".env" not in ls_files.stdout
        assert "contacts.json" not in ls_files.stdout
        assert "app.py" in ls_files.stdout

        # 3. Create a malicious symlink in the workspace attempting to escape to trusted mirror
        symlink_to_trusted = workspace / "escape_symlink"
        try:
            symlink_to_trusted.symlink_to(trusted_git)
        except OSError:
            pass

        # 4. Create the probe script inside the workspace
        trusted_git_host_path = trusted_git.resolve().as_posix()
        probe_py = workspace / "probe_isolation.py"
        probe_py.write_text(
            f'''
import os
import socket
import sys

print("[START_RUNTIME_AUDIT]")

# Check 1: Access ordinary files in /workspace
try:
    with open("/workspace/app.py", "r") as f:
        content = f.read()
    if "hello world" in content:
        print("[PASS: ORDINARY_FILES_READABLE]")
    else:
        print("[FAIL: ORDINARY_FILES_CONTENT_MISMATCH]")
except Exception as e:
    print(f"[FAIL: ORDINARY_FILES_READ_ERROR: {{e}}]")

# Check 2: Attempt direct access to host trusted Git mirror path
trusted_host_path = "{trusted_git_host_path}"
if os.path.exists(trusted_host_path):
    print(f"[FAIL: TRUSTED_MIRROR_ACCESSIBLE: {{trusted_host_path}} exists in container!]")
else:
    print("[PASS: TRUSTED_MIRROR_UNREACHABLE_BY_PATH]")

# Check 3: Attempt traversal through symlink pointing to host trusted mirror
symlink_path = "/workspace/escape_symlink"
if os.path.islink(symlink_path):
    try:
        target = os.readlink(symlink_path)
        # Attempt to open or list through symlink
        os.listdir(symlink_path)
        print("[FAIL: SYMLINK_TRAVERSAL_SUCCEEDED]")
    except (OSError, FileNotFoundError):
        print("[PASS: SYMLINK_TRAVERSAL_BROKEN_IN_CONTAINER]")
else:
    print("[PASS: NO_SYMLINK_OR_REMOVED]")

# Check 4: Inspect container mounts and environment for trusted mirror path
with open("/proc/mounts", "r") as f:
    mounts_content = f.read()

if "{trusted_git_host_path}" in mounts_content or "trusted_" in mounts_content:
    print("[FAIL: TRUSTED_PATH_IN_MOUNTS]")
else:
    print("[PASS: NO_TRUSTED_PATH_IN_MOUNTS]")

# Check environment variables
env_leaked = any("{trusted_git_host_path}" in v or "trusted_" in v for v in os.environ.values())
if env_leaked:
    print("[FAIL: TRUSTED_PATH_IN_ENV]")
else:
    print("[PASS: NO_TRUSTED_PATH_IN_ENV]")

# Check 5: Container isolation constraints
uid = os.getuid()
gid = os.getgid()
if uid == 1000 and gid == 1000:
    print("[PASS: NON_ROOT_USER]")
else:
    print(f"[FAIL: USER_IS_ROOT_OR_UNEXPECTED: uid={{uid}}, gid={{gid}}]")

# Network isolation
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1.0)
try:
    sock.connect(("1.1.1.1", 80))
    print("[FAIL: NETWORK_CONNECTED]")
except OSError:
    print("[PASS: NETWORK_DISABLED]")
finally:
    sock.close()

# Read-only root filesystem
try:
    with open("/etc/test_write.txt", "w") as f:
        f.write("mutation")
    print("[FAIL: ROOT_FS_WRITABLE]")
except OSError:
    print("[PASS: ROOT_FS_READ_ONLY]")

# Dropped capabilities & no-new-privs
with open("/proc/self/status", "r") as f:
    status_content = f.read()

no_new_privs = "NoNewPrivs:\t1" in status_content
cap_eff_zero = (
    "CapEff:\t0000000000000000" in status_content
    or "CapEff:\t0" in status_content
)
if no_new_privs and cap_eff_zero:
    print("[PASS: CAPABILITIES_DROPPED]")
else:
    print("[FAIL: CAPABILITIES_RETAINED]")

print("[END_RUNTIME_AUDIT]")
''',
            encoding="utf-8",
        )

        # 5. Run probe inside Docker container using DockerSandbox
        req = SandboxRequest(
            request_id="phase10_runtime_audit_01",
            run_id="run_phase10_audit",
            workspace_path=workspace,
            command=["python3", "probe_isolation.py"],
            limits=ExecutionLimits(timeout_seconds=20.0),
            network_policy=NetworkPolicy.DISABLED,
        )

        res = sandbox.run(req)

        # 6. Verify assertions
        assert res.status == ExecutionStatus.SUCCEEDED, f"Probe failed: {res.error_message}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert res.exit_code == 0, f"Probe script exited with code {res.exit_code}:\n{res.stderr}"
        assert "[PASS: ORDINARY_FILES_READABLE]" in res.stdout
        assert "[PASS: TRUSTED_MIRROR_UNREACHABLE_BY_PATH]" in res.stdout
        assert "[PASS: SYMLINK_TRAVERSAL_BROKEN_IN_CONTAINER]" in res.stdout
        assert "[PASS: NO_TRUSTED_PATH_IN_MOUNTS]" in res.stdout
        assert "[PASS: NO_TRUSTED_PATH_IN_ENV]" in res.stdout
        assert "[PASS: NON_ROOT_USER]" in res.stdout
        assert "[PASS: NETWORK_DISABLED]" in res.stdout
        assert "[PASS: ROOT_FS_READ_ONLY]" in res.stdout
        assert "[PASS: CAPABILITIES_DROPPED]" in res.stdout

    finally:
        cleanup_repository(acquired)
