"""Release artifact manifest schema and generator for FORGE.

Generates structured provenance records, cryptographic digests, and build environment
metadata for release distribution artifacts.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
from typing import Any, Dict, List, Optional
import zipfile

from forge import __version__


@dataclass
class ReleaseArtifact:
    """Cryptographic and forensic record for a single distribution artifact."""
    filename: str
    artifact_type: str  # 'wheel' or 'sdist'
    size_bytes: int
    sha256: str
    file_count: int
    contents: List[str] = field(default_factory=list)


@dataclass
class ReleaseManifest:
    """Comprehensive release manifest associating artifacts with source commit and environment."""
    schema_version: str
    package_name: str
    version: str
    source_commit_sha: str
    git_branch: str
    built_at: str
    builder_environment: Dict[str, str]
    artifacts: List[ReleaseArtifact]
    provenance_mode: str = "github-actions-oidc"
    verification_instructions: str = (
        "Verify SHA-256 digests against SHA256SUMS.txt or via 'forge release verify <artifact>'. "
        "When built in GitHub Actions, verify SLSA provenance via 'gh attestation verify <artifact> --owner thisisanimesh01'."
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert manifest to serializable dictionary."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Render manifest as formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


def compute_file_sha256(file_path: Path) -> str:
    """Compute the hexadecimal SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def inspect_artifact_archive(file_path: Path) -> tuple[int, List[str]]:
    """Forensically inspect zip or tar.gz archive contents."""
    contents: List[str] = []
    if file_path.suffix == ".whl" or file_path.name.endswith(".zip"):
        with zipfile.ZipFile(file_path, "r") as z:
            contents = sorted(z.namelist())
    elif file_path.name.endswith(".tar.gz") or file_path.suffix == ".tgz":
        with tarfile.open(file_path, "r:gz") as t:
            contents = sorted(t.getnames())
    else:
        raise ValueError(f"Unsupported archive format: {file_path.name}")
    return len(contents), contents


def get_current_git_commit(cwd: Optional[Path] = None) -> tuple[str, str]:
    """Retrieve current git HEAD commit SHA and active branch name."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return commit, branch
    except Exception:
        return "unknown", "unknown"


def generate_release_manifest(
    dist_dir: Path,
    source_commit: Optional[str] = None,
    git_branch: Optional[str] = None,
    include_contents_list: bool = False,
) -> ReleaseManifest:
    """Generate a ReleaseManifest for all distribution artifacts in dist_dir."""
    resolved_dist = dist_dir.resolve()
    if not resolved_dist.exists() or not resolved_dist.is_dir():
        raise FileNotFoundError(f"Distribution directory not found: {resolved_dist}")

    commit_sha, branch_name = get_current_git_commit(resolved_dist.parent)
    if source_commit:
        commit_sha = source_commit
    if git_branch:
        branch_name = git_branch

    artifacts: List[ReleaseArtifact] = []

    # Sort filenames for deterministic manifest generation
    for item in sorted(resolved_dist.iterdir()):
        if not item.is_file():
            continue
        if item.suffix == ".whl":
            art_type = "wheel"
        elif item.name.endswith(".tar.gz"):
            art_type = "sdist"
        else:
            continue

        size = item.stat().st_size
        sha256 = compute_file_sha256(item)
        file_count, contents = inspect_artifact_archive(item)

        artifacts.append(
            ReleaseArtifact(
                filename=item.name,
                artifact_type=art_type,
                size_bytes=size,
                sha256=sha256,
                file_count=file_count,
                contents=contents if include_contents_list else [],
            )
        )

    builder_env = {
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
    }

    return ReleaseManifest(
        schema_version="1.0.0",
        package_name="forge-core",
        version=__version__,
        source_commit_sha=commit_sha,
        git_branch=branch_name,
        built_at=datetime.now(timezone.utc).isoformat(),
        builder_environment=builder_env,
        artifacts=artifacts,
    )


def write_checksums_file(manifest: ReleaseManifest, output_path: Path) -> None:
    """Write standard SHA256SUMS.txt format file."""
    lines = [f"{a.sha256}  {a.filename}" for a in manifest.artifacts]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
