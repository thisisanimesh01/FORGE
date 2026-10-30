#!/usr/bin/env python3
"""Standalone FORGE Release Artifact Verifier.

Can be run on any machine with standard Python 3 (no third-party dependencies required).
Verifies SHA-256 digests against SHA256SUMS.txt or release_manifest.json, checks version
alignment, and performs forensic scanning for forbidden/leaked files.

Usage:
    python3 scripts/verify_release_artifacts.py dist/forge_core-0.1.0b1-py3-none-any.whl
    python3 scripts/verify_release_artifacts.py dist/*.whl dist/*.tar.gz
"""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from typing import Dict, List, Optional
import zipfile

EXPECTED_VERSION = "0.1.0b1"

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


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def inspect_archive(path: Path) -> List[str]:
    if path.suffix == ".whl" or path.name.endswith(".zip"):
        with zipfile.ZipFile(path, "r") as z:
            return sorted(z.namelist())
    elif path.name.endswith(".tar.gz") or path.suffix == ".tgz":
        with tarfile.open(path, "r:gz") as t:
            return sorted(t.getnames())
    else:
        raise ValueError(f"Unsupported archive format: {path.name}")


def load_checksums(directory: Path) -> Dict[str, str]:
    sums_file = directory / "SHA256SUMS.txt"
    manifest_file = directory / "release_manifest.json"
    mapping: Dict[str, str] = {}

    if manifest_file.exists():
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            for art in data.get("artifacts", []):
                mapping[art["filename"]] = art["sha256"]
        except Exception:
            pass

    if sums_file.exists():
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    mapping[Path(parts[1].strip().lstrip("*")).name] = parts[0].strip().lower()

    return mapping


def verify_file(artifact: Path, expected_sums: Dict[str, str]) -> bool:
    print("=" * 60)
    print(f"Verifying: {artifact.name}")
    print(f"Path:      {artifact}")
    print(f"Size:      {artifact.stat().st_size:,} bytes")

    calc_sha = compute_sha256(artifact)
    expected_sha = expected_sums.get(artifact.name)

    print(f"SHA-256:   {calc_sha}")

    is_valid = True

    # 1. Checksum
    if not expected_sha:
        print("  [WARN] No checksum found in SHA256SUMS.txt or release_manifest.json!")
        is_valid = False
    elif calc_sha.lower() == expected_sha.lower():
        print("  [PASS] SHA-256 checksum matched manifest record.")
    else:
        print(f"  [FAIL] SHA-256 mismatch! Expected: {expected_sha}")
        is_valid = False

    # 2. Version
    if EXPECTED_VERSION in artifact.name:
        print(f"  [PASS] Version '{EXPECTED_VERSION}' matched in artifact filename.")
    else:
        print(f"  [FAIL] Version '{EXPECTED_VERSION}' not found in artifact filename!")
        is_valid = False

    # 3. Forensic scanning
    try:
        members = inspect_archive(artifact)
        is_wheel = artifact.suffix == ".whl"
        violations = []
        for m in members:
            for pat in FORBIDDEN_ARCHIVE_PATTERNS:
                if pat.pattern == r"(^|/)tests/" and not is_wheel:
                    continue
                if pat.search(m):
                    violations.append(m)
                    break

        if violations:
            print(f"  [FAIL] Forensic violations ({len(violations)} forbidden files found):")
            for v in violations[:5]:
                print(f"         - {v}")
            is_valid = False
        else:
            print(f"  [PASS] Forensic scan clean ({len(members)} archived files, 0 forbidden).")
    except Exception as e:
        print(f"  [FAIL] Archive inspection failed: {e}")
        is_valid = False

    if is_valid:
        print(f"Outcome:   [PASSED] Artifact {artifact.name} is cryptographically and forensically valid.\n")
    else:
        print(f"Outcome:   [FAILED] Artifact {artifact.name} failed verification.\n")

    return is_valid


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <artifact_file> [artifact_file_2 ...]")
        return 2

    overall_pass = True
    for arg in sys.argv[1:]:
        p = Path(arg).resolve()
        if not p.exists():
            print(f"File not found: {p}", file=sys.stderr)
            overall_pass = False
            continue

        sums = load_checksums(p.parent)
        passed = verify_file(p, sums)
        if not passed:
            overall_pass = False

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
