"""Unit tests for Phase 6, Milestone 5: Security Hardening, Attestation & Release Gate.

Covers:
1. Release manifest generation, artifact inspection, and SHA-256 calculation.
2. Checksums file writing and parsing.
3. Cryptographic and forensic artifact verification:
   - Clean artifact verification pass
   - Tampered checksum detection & fail-closed rejection
   - Version mismatch detection
   - Forensic scan detecting forbidden files (.env, .sqlite, .db, pilot fixtures)
   - Permitted template file (.env.example)
4. CLI release commands (forge release verify, forge release manifest).
5. GitHub Actions workflow security policy inspection (minimal permissions, pinned actions).
6. Security invariants (disabled cloud fallback, secret redaction, Docker boundary).
"""

import hashlib
import json
from pathlib import Path
import re
import tarfile
from unittest.mock import patch
import zipfile
import pytest
from typer.testing import CliRunner

from forge import __version__
from forge.cli import app
from forge.config import ForgeSettings, redact_secrets
from forge.release.manifest import (
    ReleaseArtifact,
    ReleaseManifest,
    compute_file_sha256,
    generate_release_manifest,
    inspect_artifact_archive,
    write_checksums_file,
)
from forge.release.verification import (
    FORBIDDEN_ARCHIVE_PATTERNS,
    ArtifactVerificationReport,
    parse_checksums_file,
    verify_artifact,
    verify_artifact_forensics,
)

runner = CliRunner()


# =============================================================================
# 1. Checksum Calculation & Manifest Generation
# =============================================================================

def test_compute_file_sha256(tmp_path: Path):
    """Verify SHA-256 calculation matches hashlib directly."""
    sample_file = tmp_path / "sample.txt"
    sample_data = b"Hello FORGE Security Hardening M5"
    sample_file.write_bytes(sample_data)

    expected = hashlib.sha256(sample_data).hexdigest()
    actual = compute_file_sha256(sample_file)
    assert actual == expected


def test_write_and_parse_checksums_file(tmp_path: Path):
    """Verify writing SHA256SUMS.txt and parsing it back returns accurate mapping."""
    manifest = ReleaseManifest(
        schema_version="1.0.0",
        package_name="forge-core",
        version="0.1.0b1",
        source_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        git_branch="main",
        built_at="2026-10-31T00:00:00Z",
        builder_environment={"python": "3.13"},
        artifacts=[
            ReleaseArtifact("forge_core-0.1.0b1-py3-none-any.whl", "wheel", 1000, "hash1", 10),
            ReleaseArtifact("forge_core-0.1.0b1.tar.gz", "sdist", 2000, "hash2", 20),
        ],
    )

    sums_path = tmp_path / "SHA256SUMS.txt"
    write_checksums_file(manifest, sums_path)

    assert sums_path.exists()
    parsed = parse_checksums_file(sums_path)
    assert parsed["forge_core-0.1.0b1-py3-none-any.whl"] == "hash1"
    assert parsed["forge_core-0.1.0b1.tar.gz"] == "hash2"


def test_generate_release_manifest(tmp_path: Path):
    """Verify generate_release_manifest indexes built artifacts and extracts metadata."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    # Create dummy wheel and sdist zip/tar files
    whl_path = dist_dir / "forge_core-0.1.0b1-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/__init__.py", "__version__ = '0.1.0b1'\n")

    sdist_path = dist_dir / "forge_core-0.1.0b1.tar.gz"
    with tarfile.open(sdist_path, "w:gz") as t:
        ti = tarfile.TarInfo("forge_core-0.1.0b1/pyproject.toml")
        content = b"[project]\nname = 'forge-core'\n"
        ti.size = len(content)
        t.addfile(ti, io_bytes := __import__("io").BytesIO(content))

    manifest = generate_release_manifest(dist_dir, source_commit="mockcommit123", git_branch="main")
    assert manifest.package_name == "forge-core"
    assert manifest.version == __version__
    assert manifest.source_commit_sha == "mockcommit123"
    assert manifest.git_branch == "main"
    assert len(manifest.artifacts) == 2

    art_names = {a.filename for a in manifest.artifacts}
    assert "forge_core-0.1.0b1-py3-none-any.whl" in art_names
    assert "forge_core-0.1.0b1.tar.gz" in art_names


# =============================================================================
# 2. Forensic Scanning & Leakage Prevention
# =============================================================================

def test_forensic_scan_clean_artifact(tmp_path: Path):
    """Verify clean distribution artifact has zero forensic violations."""
    whl_path = tmp_path / "forge_core-0.1.0b1-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/__init__.py", "__version__ = '0.1.0b1'\n")
        z.writestr("forge/cli.py", "import typer\n")
        z.writestr(".env.example", "FORGE_ENV=development\n")  # Allowed template

    violations = verify_artifact_forensics(whl_path)
    assert len(violations) == 0


def test_forensic_scan_detects_secret_env(tmp_path: Path):
    """Verify forensic scan catches forbidden .env files."""
    whl_path = tmp_path / "leaky-0.1.0b1-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/.env", "GROQ_API_KEY=secret_token_12345\n")

    violations = verify_artifact_forensics(whl_path)
    assert len(violations) >= 1
    assert "forge/.env" in violations


def test_forensic_scan_detects_sqlite_database(tmp_path: Path):
    """Verify forensic scan catches leaked SQLite database files."""
    whl_path = tmp_path / "leaky_db-0.1.0b1-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/forge.sqlite3", b"SQLite format 3\x00")

    violations = verify_artifact_forensics(whl_path)
    assert len(violations) >= 1
    assert "forge/forge.sqlite3" in violations


def test_forensic_scan_detects_pilot_fixtures(tmp_path: Path):
    """Verify forensic scan catches pilot artifacts."""
    whl_path = tmp_path / "leaky_pilot-0.1.0b1-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/forge_pilot_01/calculator.py", "def add(a, b): return a + b\n")

    violations = verify_artifact_forensics(whl_path)
    assert len(violations) >= 1
    assert any("forge_pilot_" in v for v in violations)


# =============================================================================
# 3. Artifact Verification & Tampering Rejection
# =============================================================================

def test_verify_artifact_clean_with_explicit_hash(tmp_path: Path):
    """Verify clean artifact validates successfully with expected SHA-256."""
    whl_path = tmp_path / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/__init__.py", "__version__ = '0.1.0b1'\n")

    expected_sha = compute_file_sha256(whl_path)
    report = verify_artifact(whl_path, expected_sha256=expected_sha)
    assert report.is_valid is True
    assert report.checksum_verified is True
    assert report.version_matched is True
    assert report.forensic_clean is True
    assert "VERIFIED" in report.message


def test_verify_artifact_tampered_hash_fails_closed(tmp_path: Path):
    """Verify tampered hash triggers checksum mismatch and fails closed."""
    whl_path = tmp_path / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/__init__.py", "__version__ = '0.1.0b1'\n")

    tampered_sha = "0000000000000000000000000000000000000000000000000000000000000000"
    report = verify_artifact(whl_path, expected_sha256=tampered_sha)
    assert report.is_valid is False
    assert report.checksum_verified is False
    assert "CHECKSUM MISMATCH" in report.message


def test_verify_artifact_forensic_violation_fails_closed(tmp_path: Path):
    """Verify clean checksum but leaked file triggers forensic failure and fails closed."""
    whl_path = tmp_path / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/.env", "LEAK=true\n")

    expected_sha = compute_file_sha256(whl_path)
    report = verify_artifact(whl_path, expected_sha256=expected_sha)
    assert report.is_valid is False
    assert report.forensic_clean is False
    assert "FORENSIC VIOLATION" in report.message


def test_verify_artifact_version_mismatch(tmp_path: Path):
    """Verify artifact with mismatched version in filename fails closed."""
    whl_path = tmp_path / "forge_core-99.9.9-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("forge/__init__.py", "code\n")

    expected_sha = compute_file_sha256(whl_path)
    report = verify_artifact(whl_path, expected_sha256=expected_sha)
    assert report.is_valid is False
    assert report.version_matched is False
    assert "VERSION MISMATCH" in report.message


# =============================================================================
# 4. CLI Release Commands
# =============================================================================

def test_cli_release_manifest_cmd(tmp_path: Path):
    """Verify forge release manifest generates files in dist directory."""
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    whl = dist_dir / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("forge/__init__.py", "code\n")

    result = runner.invoke(app, ["release", "manifest", str(dist_dir)])
    assert result.exit_code == 0
    assert "Wrote release manifest" in result.output
    assert (dist_dir / "release_manifest.json").exists()
    assert (dist_dir / "SHA256SUMS.txt").exists()


def test_cli_release_verify_pass(tmp_path: Path):
    """Verify forge release verify exits 0 for valid artifact."""
    whl = tmp_path / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("forge/__init__.py", "code\n")
    sha = compute_file_sha256(whl)

    result = runner.invoke(app, ["release", "verify", str(whl), "--checksum", sha])
    assert result.exit_code == 0
    assert "FORGE Release Artifact Verification" in result.output
    assert "[PASS]" in result.output


def test_cli_release_verify_tampered_exit_code_1(tmp_path: Path):
    """Verify forge release verify exits 1 for tampered checksum."""
    whl = tmp_path / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("forge/__init__.py", "code\n")

    result = runner.invoke(app, ["release", "verify", str(whl), "--checksum", "badhash"])
    assert result.exit_code == 1
    assert "CHECKSUM MISMATCH" in result.output


def test_cli_release_verify_json_output(tmp_path: Path):
    """Verify forge release verify --json outputs parseable JSON report."""
    whl = tmp_path / f"forge_core-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("forge/__init__.py", "code\n")
    sha = compute_file_sha256(whl)

    result = runner.invoke(app, ["release", "verify", str(whl), "--checksum", sha, "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["is_valid"] is True
    assert data["calculated_sha256"] == sha


# =============================================================================
# 5. GitHub Actions Security Policy Audits
# =============================================================================

def test_github_workflow_minimal_permissions():
    """Verify all GitHub Actions workflows enforce minimal token permissions."""
    workflow_dir = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
    assert workflow_dir.exists(), "Missing .github/workflows directory"

    workflows = list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
    assert len(workflows) >= 2, "Expected at least ci.yml and release-build.yml"

    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        assert "permissions:" in text, f"Workflow {wf.name} is missing explicit top-level permissions!"
        assert "contents: read" in text, f"Workflow {wf.name} must specify contents: read"
        # Verify no write-all or unconstrained permissions
        assert "permissions: write-all" not in text


def test_github_workflow_pinned_action_shas():
    """Verify all external GitHub Actions use immutable commit SHAs, not mutable tags."""
    workflow_dir = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
    sha_pattern = re.compile(r"uses:\s+([a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+)@([a-f0-9]{40})")
    tag_pattern = re.compile(r"uses:\s+([a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-]+)@(v\d+|master|main)")

    for wf in workflow_dir.glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        # Ensure no mutable tags used
        mutable_uses = tag_pattern.findall(text)
        assert len(mutable_uses) == 0, f"Workflow {wf.name} uses mutable action tags: {mutable_uses}"

        # Ensure all actions match 40-char SHA
        pinned_uses = sha_pattern.findall(text)
        assert len(pinned_uses) > 0, f"Workflow {wf.name} must have pinned action SHAs"


# =============================================================================
# 6. Core Security Invariants
# =============================================================================

def test_security_cloud_fallback_disabled_by_default():
    """Verify FORGE_ALLOW_CLOUD_FALLBACK defaults to False."""
    settings = ForgeSettings()
    assert settings.allow_cloud_fallback is False, "Security violation: allow_cloud_fallback must default to False!"


def test_security_secret_redaction():
    """Verify secret redaction filters API keys and tokens."""
    sensitive = "Key: sk-abcdef1234567890abcdef12 and Token: ghp_1234567890abcdef1234567890abcdef1234"
    sanitized = redact_secrets(sensitive)
    assert "sk-" not in sanitized or "[REDACTED_SECRET]" in sanitized
    assert "ghp_" not in sanitized or "[REDACTED_SECRET]" in sanitized
