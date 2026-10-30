# Phase 7, Milestone 2: Official Public Beta Release v0.1.0b1

- **Milestone:** Phase 7, Milestone 2 — Official Public Beta Release Publication
- **Status:** COMPLETE
- **Release Version:** `0.1.0b1`
- **Git Tag:** `v0.1.0b1`
- **Target Source Commit:** `d78bbb336644b91dd19040f6ec1b0cf590229681`
- **Release URL:** [https://github.com/thisisanimesh01/FORGE/releases/tag/v0.1.0b1](https://github.com/thisisanimesh01/FORGE/releases/tag/v0.1.0b1)
- **License:** Apache License 2.0
- **Repository Visibility:** PUBLIC

---

## 1. Executive Summary

Milestone 2 represents the official milestone where FORGE is published to the open-source community as a public supervised beta release (`v0.1.0b1`).

All publication prerequisites identified in Phase 6 and Phase 7 Milestone 1 were strictly resolved:
1. Public repository visibility verified on GitHub (`thisisanimesh01/FORGE`).
2. GitHub Actions release build (`.github/workflows/release-build.yml`, Run `41131395242`) produced verified SLSA build provenance attestations via Sigstore OIDC (Attestation ID `61944148`).
3. Cryptographic digest verification of the release wheel and source tarball against `SHA256SUMS.txt` succeeded.
4. Git annotated tag `v0.1.0b1` was anchored to the exact source commit (`d78bbb336644b91dd19040f6ec1b0cf590229681`) matching the build attestation.
5. GitHub Pre-Release `v0.1.0b1` was published with full release notes and the 4 verified release assets attached.
6. Post-publication client verification confirmed that all assets download cleanly and that `gh attestation verify` succeeds against the published release wheel.

---

## 2. Release Artifacts & Cryptographic Verification

The published release artifacts were generated hermetically in GitHub Actions:

| Artifact Name | Artifact Type | Size (Bytes) | SHA-256 Checksum |
| :--- | :--- | :--- | :--- |
| `forge_core-0.1.0b1-py3-none-any.whl` | Wheel | 376,816 | `6ba111b2584da5971723f4cd86c55e24f7bf6dabec24cfa6087d9892d2590be0` |
| `forge_core-0.1.0b1.tar.gz` | Source Dist | 555,256 | `5c5004de56ff0e07586f1afe4bcc54b83642341a5ba616d8f3ede2cc49b31a15` |
| `SHA256SUMS.txt` | Checksum Manifest | 194 | `dd22b0461b1e4312348b72c25e6c42c223957915549b9741902f2f02f31b1b05` |
| `release_manifest.json` | Build Metadata | 1,256 | `e01bb8eb4b8d4e894daabe39cb43ccd2abdf3461c86c9e2f205beccebd96e084` |

### Sigstore OIDC Provenance Verification
```bash
gh attestation verify forge_core-0.1.0b1-py3-none-any.whl --repo thisisanimesh01/FORGE
```
- **Policy Result:** `Verification succeeded!`
- **Signer Workflow:** `.github/workflows/release-build.yml@refs/heads/main`
- **Signer Repo:** `thisisanimesh01/FORGE`
- **Predicate Type:** `https://slsa.dev/provenance/v1`

---

## 3. Mandatory Invariants Maintained

- **No PyPI Distribution:** PyPI publishing remains disabled per beta guidelines. Distribution is conducted exclusively via GitHub Releases with verified cryptographic attestations.
- **Target Repository Immutability:** Target repositories are mounted read-only and never modified.
- **Fail-Closed Isolation:** Dynamic execution runs exclusively inside non-root, capability-dropped, unnetworked Docker containers.
- **Human Approval Gate:** All patch proposals require operator review and sign-off.
