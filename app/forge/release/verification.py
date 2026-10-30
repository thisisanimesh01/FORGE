"""Release artifact verification and forensic inspection for FORGE.

Validates:
1. SHA-256 cryptographic checksum matching against manifest and SHA256SUMS.txt.
2. Forensic exclusion: ensures no `.env`, SQLite databases, pilot fixtures, or private files leaked into archives.
3. Package version alignment against FORGE runtime version.
4. Fails closed on any tamper, mismatch, or unknown artifact.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from forge import __version__
from forge.release.manifest import compute_file_sha256, inspect_artifact_archive


# Forbidden patterns that must NEVER appear in release distribution archives
FORBIDDEN_ARCHIVE_PATTERNS = [
    re.compile(r"(^|/)\.env($|\.(?!example)[a-zA-Z0-9_\-]+$)"),
    re.compile(r"\.sqlite3?$"),
    re.compile(r"\.db$"),
    re.compile(r"(^|/)tests/fixtures/"),
    re.compile(r"(^|/)tests/"),  # Tests must not be in pure wheel runtime
    re.compile(r"(^|/)forge_pilot_"),
    re.compile(r"(^|/)\.DS_Store$"),
    re.compile(r"(^|/)__pycache__/"),
]


class ArtifactVerificationError(Exception):
    """Base exception for release artifact verification failures."""
    pass


class ChecksumMismatchError(ArtifactVerificationError):
    """Raised when an artifact's SHA-256 digest does not match the manifest."""
    pass


class ForensicViolationError(ArtifactVerificationError):
    """Raised when an archive contains forbidden, private, or leaked files."""
    pass


@dataclass
class ArtifactVerificationReport:
    """Detailed report from verifying a release artifact."""
    artifact_name: str
    artifact_path: str
    artifact_size: int
    calculated_sha256: str
    expected_sha256: Optional[str]
    checksum_verified: bool
    version_matched: bool
    forensic_clean: bool
    forbidden_matches: List[str]
    manifest_commit_sha: Optional[str]
    is_valid: bool
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_name": self.artifact_name,
            "artifact_path": self.artifact_path,
            "artifact_size": self.artifact_size,
            "calculated_sha256": self.calculated_sha256,
            "expected_sha256": self.expected_sha256,
            "checksum_verified": self.checksum_verified,
            "version_matched": self.version_matched,
            "forensic_clean": self.forensic_clean,
            "forbidden_matches": self.forbidden_matches,
            "manifest_commit_sha": self.manifest_commit_sha,
            "is_valid": self.is_valid,
            "message": self.message,
        }


def parse_checksums_file(checksums_path: Path) -> Dict[str, str]:
    """Parse standard SHA256SUMS.txt file into {filename: sha256_hash} mapping."""
    if not checksums_path.exists():
        raise FileNotFoundError(f"Checksums file not found: {checksums_path}")
    mapping: Dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            digest, fname = parts[0].strip().lower(), parts[1].strip().lstrip("*")
            # Store by basename
            mapping[Path(fname).name] = digest
    return mapping


def verify_artifact_forensics(artifact_path: Path) -> List[str]:
    """Scan archive members against forbidden patterns.

    Returns list of offending member paths found inside archive.
    """
    _, member_names = inspect_artifact_archive(artifact_path)
    is_wheel = artifact_path.suffix == ".whl"
    violations: List[str] = []

    for name in member_names:
        for pattern in FORBIDDEN_ARCHIVE_PATTERNS:
            # Special case: sdist includes tests/ directory by design for package testing,
            # but wheel must NEVER include tests/.
            if pattern.pattern == r"(^|/)tests/" and not is_wheel:
                continue
            if pattern.search(name):
                violations.append(name)
                break
    return violations


def verify_artifact(
    artifact_path: Path,
    expected_sha256: Optional[str] = None,
    checksums_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> ArtifactVerificationReport:
    """Verify an individual release artifact against expected checksum, manifest, and forensic rules.

    Fails closed if checksum mismatch, forensic violation, or version discrepancy is found.
    """
    resolved_path = artifact_path.resolve()
    if not resolved_path.exists() or not resolved_path.is_file():
        raise FileNotFoundError(f"Artifact not found: {resolved_path}")

    size = resolved_path.stat().st_size
    calc_sha = compute_file_sha256(resolved_path)
    target_sha = expected_sha256
    manifest_commit: Optional[str] = None

    # 1. Resolve expected checksum from manifest or checksums file if not explicitly supplied
    if not target_sha and manifest_path and manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_commit = data.get("source_commit_sha")
            for art in data.get("artifacts", []):
                if art.get("filename") == resolved_path.name:
                    target_sha = art.get("sha256")
                    break
        except Exception as e:
            raise ArtifactVerificationError(f"Failed parsing manifest at {manifest_path}: {e}")

    if not target_sha and checksums_path and checksums_path.exists():
        sums = parse_checksums_file(checksums_path)
        target_sha = sums.get(resolved_path.name)

    # 2. Checksum verification
    checksum_verified = False
    if target_sha:
        checksum_verified = (calc_sha.lower() == target_sha.lower())
    else:
        # If no expected checksum was provided or found, cannot verify integrity
        checksum_verified = False

    # 3. Version alignment
    version_matched = __version__ in resolved_path.name

    # 4. Forensic scan
    violations = verify_artifact_forensics(resolved_path)
    forensic_clean = (len(violations) == 0)

    # 5. Composite validity
    is_valid = bool(target_sha and checksum_verified and version_matched and forensic_clean)

    if not target_sha:
        msg = f"UNVERIFIED: No expected SHA-256 checksum found for {resolved_path.name}."
    elif not checksum_verified:
        msg = (
            f"CHECKSUM MISMATCH: Calculated SHA-256 ({calc_sha[:16]}...) does not match "
            f"expected ({target_sha[:16]}...). Artifact may be corrupted or tampered!"
        )
    elif not version_matched:
        msg = f"VERSION MISMATCH: Artifact filename does not match FORGE version {__version__}."
    elif not forensic_clean:
        msg = f"FORENSIC VIOLATION: Archive contains forbidden files: {violations[:5]}."
    else:
        msg = (
            f"VERIFIED: SHA-256 digest ({calc_sha[:16]}...) matches expected record. "
            f"Forensic scan clean (0 forbidden files). Version alignment confirmed."
        )

    return ArtifactVerificationReport(
        artifact_name=resolved_path.name,
        artifact_path=str(resolved_path),
        artifact_size=size,
        calculated_sha256=calc_sha,
        expected_sha256=target_sha,
        checksum_verified=checksum_verified,
        version_matched=version_matched,
        forensic_clean=forensic_clean,
        forbidden_matches=violations,
        manifest_commit_sha=manifest_commit,
        is_valid=is_valid,
        message=msg,
    )
