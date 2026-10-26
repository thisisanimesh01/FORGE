"""Real Docker container sandbox integration tests.

These tests are opt-in and verify genuine runtime isolation against a running
Docker daemon on the host. They execute only when FORGE_DOCKER_TESTS=1 is set.

Properties verified:
1. Container launch: executes in configured python:3.11-slim image.
2. Network isolation: verifies --network none by asserting network calls fail.
3. Read-only target mount: verifies write attempts to /workspace fail with EROFS.
4. Non-root user: verifies execution as UID:GID 1000:1000.
5. Dropped capabilities: verifies NoNewPrivs and CapEff==0 in /proc/self/status.
6. Resource & time limits: verifies wall-clock timeout kills container cleanly.
7. Output bounding & secret redaction: verifies large outputs truncated and tokens scrubbed.
8. Guaranteed cleanup: verifies no orphan containers left running or stopped.
9. Target immutability: verifies zero mutations to host workspace.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import pytest

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
        reason="Opt-in real Docker integration tests. Run with FORGE_DOCKER_TESTS=1 to execute.",
    ),
]


def _hash_directory(directory: Path) -> dict[str, str]:
    """Compute sha256 hashes of all files in a directory to detect any mutations."""
    hashes = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            hashes[str(p.relative_to(directory))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def test_real_docker_sandbox_isolation_properties(tmp_path: Path):
    """Verify all required sandbox isolation properties against live Docker daemon."""
    sandbox = DockerSandbox()
    healthy, msg = sandbox.check_health()
    if not healthy:
        pytest.fail(f"Docker health check failed on host: {msg}. Cannot verify sandbox security.")

    # 1. Create disposable synthetic target workspace
    workspace = tmp_path / "synthetic_repo"
    workspace.mkdir(parents=True, exist_ok=True)
    src_dir = workspace / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    (src_dir / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        "# Synthetic Test Repository\nDo not mutate.\n",
        encoding="utf-8",
    )

    # Probe script testing container security constraints from inside
    probe_py = workspace / "probe.py"
    probe_py.write_text(
        r'''
import os
import socket
import sys

print("[START_ISOLATION_PROBE]")

# Check 1: User UID/GID
uid = os.getuid()
gid = os.getgid()
if uid == 1000 and gid == 1000:
    print("[PASS: NON_ROOT_USER: uid=1000, gid=1000]")
else:
    print(f"[FAIL: NON_ROOT_USER: uid={uid}, gid={gid}]")

# Check 2: Read-only workspace filesystem mount
try:
    with open("/workspace/unauthorized_write.txt", "w") as f:
        f.write("mutation")
    print("[FAIL: READ_ONLY_TARGET - File creation succeeded in /workspace!]")
except OSError as e:
    print(f"[PASS: READ_ONLY_TARGET - Write blocked: {e.strerror}]")

# Check 3: Network isolation (--network none)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2.0)
try:
    sock.connect(("1.1.1.1", 80))
    print("[FAIL: NETWORK_DISABLED - External connection succeeded!]")
except OSError as e:
    print(f"[PASS: NETWORK_DISABLED - Connection blocked: {e}]")
finally:
    sock.close()

# Check 4: Dropped capabilities and no-new-privileges
try:
    with open("/proc/self/status", "r") as f:
        proc_status = f.read()
    no_new_privs = "NoNewPrivs:\t1" in proc_status
    cap_eff_zero = (
        "CapEff:\t0000000000000000" in proc_status
        or "CapEff:\t0000000000000000\n" in proc_status
        or "CapEff:\t0\n" in proc_status
    )
    if no_new_privs and cap_eff_zero:
        print("[PASS: CAPABILITIES_DROPPED: NoNewPrivs=1 and CapEff=0]")
    else:
        print(f"[FAIL: PRIVILEGE_ESCALATION_PERMITTED: {proc_status[:200]}]")
except Exception as e:
    print(f"[WARN: PROC_STATUS_UNAVAILABLE: {e}]")

# Check 5: Secret emission test for outbound scrubbing
print("Emitting token: sk-live-dockerisolationtoken1234567890abcdef")

# Check 6: Large output test for head/tail truncation
print("X" * 60000)

print("[COMPLETE_ISOLATION_PROBE]")
''',
        encoding="utf-8",
    )

    hashes_before = _hash_directory(workspace)

    req_id = f"req_iso_{int(time.time())}"
    request = SandboxRequest(
        request_id=req_id,
        run_id="run_docker_integration",
        workspace_path=workspace,
        command=["python3", "probe.py"],
        limits=ExecutionLimits(
            timeout_seconds=30.0,
            max_stdout_bytes=20000,
        ),
        network_policy=NetworkPolicy.DISABLED,
    )

    result = sandbox.run(request)

    # Verify execution outcome
    assert result.status == ExecutionStatus.SUCCEEDED, f"Execution failed: {result.error_message}"
    assert result.isolation_established is True, "Isolation was not established"
    assert result.exit_code == 0, f"Probe script exited with code {result.exit_code}: {result.stderr}"

    # Verify each isolation check in container stdout
    assert "[PASS: NON_ROOT_USER" in result.stdout, f"Non-root check failed:\n{result.stdout[:500]}"
    assert "[PASS: READ_ONLY_TARGET" in result.stdout, f"Read-only mount check failed:\n{result.stdout[:500]}"
    assert "[PASS: NETWORK_DISABLED" in result.stdout, f"Network isolation check failed:\n{result.stdout[:500]}"
    assert "[PASS: CAPABILITIES_DROPPED" in result.stdout, f"Capability drop check failed:\n{result.stdout[:500]}"

    # Verify secret redaction
    assert "sk-live-dockerisolationtoken1234567890abcdef" not in result.stdout
    assert "sk-live-" not in result.stdout or "[REDACTED]" in result.stdout

    # Verify output truncation
    assert result.stdout_truncated is True or "[TRUNCATED:" in result.stdout

    # Verify container was cleaned up
    container_name = f"forge_{req_id}"
    ps_check = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    assert container_name not in ps_check.stdout.split(), f"Container {container_name} was not cleaned up!"

    # Verify target repository immutability on host
    hashes_after = _hash_directory(workspace)
    assert hashes_before == hashes_after, "Host workspace files were mutated during sandboxed execution!"
    assert not (workspace / "unauthorized_write.txt").exists(), "Unauthorized file was written to host workspace!"


def test_real_docker_sandbox_timeout_enforcement(tmp_path: Path):
    """Verify that execution exceeding timeout is terminated and container cleaned up."""
    sandbox = DockerSandbox()
    healthy, msg = sandbox.check_health()
    if not healthy:
        pytest.fail(f"Docker health check failed on host: {msg}.")

    workspace = tmp_path / "timeout_repo"
    workspace.mkdir(parents=True, exist_ok=True)

    sleep_py = workspace / "sleep_forever.py"
    sleep_py.write_text(
        "import time\nprint('Sleeping...')\ntime.sleep(20)\nprint('Done.')\n",
        encoding="utf-8",
    )

    req_id = f"req_timeout_{int(time.time())}"
    request = SandboxRequest(
        request_id=req_id,
        run_id="run_timeout_test",
        workspace_path=workspace,
        command=["python3", "sleep_forever.py"],
        limits=ExecutionLimits(timeout_seconds=2.0),
        network_policy=NetworkPolicy.DISABLED,
    )

    start_mono = time.monotonic()
    result = sandbox.run(request)
    elapsed = time.monotonic() - start_mono

    assert result.status == ExecutionStatus.TIMED_OUT, f"Expected TIMED_OUT, got {result.status}"
    assert result.timed_out is True
    assert elapsed < 10.0, f"Timeout took too long to terminate: {elapsed:.1f}s"

    # Verify container removed
    container_name = f"forge_{req_id}"
    ps_check = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    assert container_name not in ps_check.stdout.split(), f"Container {container_name} was not removed after timeout!"
