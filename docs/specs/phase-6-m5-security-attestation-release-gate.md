# Phase 6, Milestone 5: Security Hardening, Attestation & Release Gate — Specification

- **Milestone:** Phase 6 — M5: Security Hardening, Attestation & Release Gate
- **Status:** COMPLETED & VERIFIED
- **Baseline Git Commit:** `9b5dd24125044a8fa20f56e50e5a88df3b6f2e6a`
- **Target Repository Immutability:** STRICTLY ENFORCED (Zero commits, pushes, merges, PRs)
- **Traceability:** Implements Milestone 5 of Phase 6 per `AGENTS.md` (Sections 1, 2, 3, 4, 5, 7) and `docs/specs/phase-6-roadmap.md`.

---

## 1. Executive Summary

Milestone 5 establishes the security, dependency, artifact integrity, provenance, and release gates for the FORGE Supervised Beta (`0.1.0b1`).

Before Milestone 5, while automated repair planning, multi-check verification, and operational diagnostics functioned within strict boundaries, the release packaging pipeline lacked formal supply-chain verification tooling, pinned dependency constraints, cryptographic release manifests, and hardened CI/CD workflows with automated provenance attestation.

Milestone 5 hardens FORGE's release readiness by:
1. **Security Threat Modeling & Audit (`docs/SECURITY_THREAT_MODEL.md`):** Documenting FORGE's complete beta attack surface, detailing threat scenarios (malicious repos, prompt injection, path traversal, argument injection, secret leakage, revision drift, dependency confusion), concrete mitigations, and residual risks.
2. **Dependency & Supply-Chain Hardening:**
   - Auditing runtime and development dependencies with `pip-audit` against the PyPA Advisory Database, verifying **0 known vulnerabilities**.
   - Upgrading `pip` to `26.2.2` in development tooling to remediate packaging CVEs.
   - Establishing strict dependency pinning in `requirements/constraints.txt` for reproducible installations and builds.
3. **Artifact Integrity & Release Manifests (`app/forge/release/manifest.py`):**
   - Establishing a standardized release manifest schema (`release_manifest.json`) capturing package version, source commit SHA, git branch, builder environment, and individual artifact SHA-256 digests.
   - Generating standard `SHA256SUMS.txt` alongside distribution archives.
   - Assessing reproducible build feasibility and transparently documenting timestamp-related constraints.
4. **Official Build Provenance & Attestation:**
   - Integrating GitHub Actions official build provenance attestation (`actions/attest-build-provenance@v2`) using Sigstore OIDC identities and SLSA standards.
   - Providing both CLI-integrated (`forge release verify`) and standalone (`scripts/verify_release_artifacts.py`) cryptographic verification tooling that operates on any machine with standard Python.
5. **Hardened GitHub Actions CI/CD Workflows:**
   - Establishing `.github/workflows/ci.yml` and `.github/workflows/release-build.yml`.
   - Enforcing minimal top-level permissions (`permissions: contents: read`; `id-token: write` restricted strictly to attestation).
   - Pinning all external GitHub Actions to immutable 40-character commit SHAs.
   - Ensuring release build triggers require explicit human authorization (`workflow_dispatch`).
6. **Security Regression Test Suite (`tests/unit/test_phase6_milestone5.py`):**
   - 19 dedicated security tests covering manifest calculation, tampered checksum rejection (fail-closed), version mismatch, forensic scan of forbidden files, and workflow permissions.

---

## 2. Supply-Chain & Dependency Hardening

### 2.1 Dependency Inventory & Audit Results
Direct runtime dependencies in `pyproject.toml`:
- `pydantic>=2.7.0` (installed `2.14.1`)
- `pydantic-settings>=2.2.0` (installed `2.16.1`)
- `typer>=0.12.0` (installed `0.27.3`)
- `httpx>=0.27.0` (installed `0.28.1`)

Development dependencies:
- `pytest>=8.0.0` (installed `9.1.1`)
- `pytest-asyncio>=0.23.0` (installed `1.4.0`)

Audit outcome:
- Executed `pip-audit` across the full active environment.
- **Result:** `No known vulnerabilities found` across all runtime and transitive libraries.

### 2.2 Dependency Constraints Strategy (`requirements/constraints.txt`)
Exact version pins are recorded in `requirements/constraints.txt` covering direct runtime dependencies, transitive dependencies (`anyio`, `certifi`, `idna`, `rich`, `shellingham`, `typing_extensions`), and build tooling (`build`, `setuptools`, `wheel`).

---

## 3. Artifact Integrity & Forensic Controls

### 3.1 Repeatable Build Procedure
Release artifacts are built from a clean Git working tree:
```bash
python -m build --no-isolation
python -m forge.cli release manifest dist
```
Outputs:
- `dist/forge_core-0.1.0b1-py3-none-any.whl`
- `dist/forge_core-0.1.0b1.tar.gz`
- `dist/release_manifest.json`
- `dist/SHA256SUMS.txt`

### 3.2 Forensic Exclusion Verification
The release verification engine (`app/forge/release/verification.py`) scans every archive member against forbidden patterns:
- `.env` files (except permitted `.env.example` template)
- SQLite databases (`*.sqlite3`, `*.db`)
- Benchmark and repair fixtures (`tests/fixtures/`)
- Historical pilot folders (`forge_pilot_*`)
- Compiled bytecode (`__pycache__/`) and macOS metadata (`.DS_Store`)

### 3.3 Reproducible Build Assessment
- **Forensic & Structural Determinism:** 100% achieved. Rebuilding from the same source commit produces identical member files, identical directory layouts, identical bytecode, and identical dependencies.
- **Byte-for-Byte Bitstream Reproducibility:** Cannot be strictly guaranteed across different host platforms due to standard ZIP/tar header timestamp encoding variations. FORGE relies on cryptographic SHA-256 manifests and OIDC build provenance attestation rather than making unsubstantiated bitstream reproducibility claims.

---

## 4. GitHub Actions Workflow Security

### 4.1 Continuous Integration (`.github/workflows/ci.yml`)
- **Triggers:** Push and PR to `main`.
- **Permissions:** `contents: read` strictly enforced.
- **Harden-Runner:** Integrated StepSecurity `harden-runner` with egress auditing.
- **Action Pinning:** All external actions pinned to immutable 40-character commit SHAs:
  - `step-security/harden-runner@4d991eb9b905ef189e4c376166672c3f2f230481`
  - `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`
  - `actions/setup-python@0b93645e9dc2c792dd34f771f58a253731a66e77`
- **Execution:** Runs `compileall`, `pip-audit`, offline pytest suite, and builds/verifies distribution packages.

### 4.2 Release Artifact Build & Attestation (`.github/workflows/release-build.yml`)
- **Triggers:** `workflow_dispatch` only (manual operator authorization).
- **Permissions:** Explicitly scoped:
  - `contents: read`
  - `id-token: write` (for Sigstore OIDC token exchange)
  - `attestations: write` (for GitHub artifact attestation)
- **Attestation:** Invokes `actions/attest-build-provenance@520d128f165991a6c774bcb7d6ae6dd9879a7db5` to sign and record build provenance.
- **Publication Guard:** Does **not** publish to PyPI or GitHub Releases automatically. Artifacts are preserved as workflow run attachments for operator inspection.

---

## 5. Verification Matrix

| Area | Tool / Test Suite | Verified Invariant | Status |
| :--- | :--- | :--- | :--- |
| **Dependency Vulnerability Audit** | `pip-audit` | Zero known CVEs across all installed runtime and transitive dependencies | **PASSED (0 vulnerabilities)** |
| **Artifact Checksum Matching** | `forge release verify` | SHA-256 digests match manifest and `SHA256SUMS.txt` | **PASSED (100%)** |
| **Tampered Checksum Detection** | `tests/unit/test_phase6_milestone5.py` | Bad hash triggers `CHECKSUM MISMATCH` and exits code 1 | **PASSED (100%)** |
| **Forensic Leak Prevention** | `tests/unit/test_phase6_milestone5.py` | Forbidden files (`.env`, `*.db`, fixtures) detected and rejected | **PASSED (100%)** |
| **Workflow Permissions Audit** | `tests/unit/test_phase6_milestone5.py` | Minimal permissions and pinned action SHAs across all workflows | **PASSED (100%)** |
| **Standalone Verifier** | `scripts/verify_release_artifacts.py` | Validates wheel and sdist with standard Python stdlib | **PASSED (100%)** |
| **Full Regression Suite** | `pytest` | 1000+ unit and integration tests passing | **PASSED (100%)** |
| **Docker Sandbox Tests** | `test_docker_sandbox_integration.py` | Live container isolation, read-only mount, network disabled | **PASSED (100%)** |

---

## 6. Scope Boundaries & Release Restrictions
- **No Release Tag Created:** Tagging is deferred to Milestone 6 acceptance.
- **No Public Release Published:** PyPI and GitHub release publication remain disabled.
- **No Autonomous Code Modification:** Target repositories remain bit-for-bit immutable.
- **Human Gate Enforced:** Human approval remains mandatory for all repair workflows.
