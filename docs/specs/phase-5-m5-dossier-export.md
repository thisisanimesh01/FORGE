# Phase 5 — Milestone 5: Certified Engineering Dossier & Operator Export

**Document ID**: `docs/specs/phase-5-m5-dossier-export.md`  
**Status**: APPROVED & IMPLEMENTED  
**Date**: 2026-10-27  
**Author**: FORGE Engineering Team  
**Preceding Milestone**: [Phase 5, Milestone 4: Multi-Check Regression & Safety Verification](phase-5-m4-multi-check-verification.md)
**Succeeding Milestone**: Phase 5, Milestone 6: End-to-End Workflow Acceptance, Reliability & Release  

---

## 1. Executive Summary

Milestone 5 implements certified engineering dossier generation and offline operator export for approved Phase 5 workflows. Under FORGE's audit-only engineering principles, the system strictly investigates, reproduces, plans, and verifies repairs in isolated ephemeral sandboxes, but **never modifies target repository files, never creates git commits, never pushes branches, never opens pull requests, and never deploys software**.

To close the loop between automated verification and human application safely, Milestone 5 provides an offline export pipeline that packages the approved repair candidate, multi-check verification proof, SARIF 2.1.0 review findings, sequential audit ledger, and human-readable application instructions into a self-contained, deterministic directory package. Every exported file is digest-locked via canonical SHA-256 integrity checksums in a machine-verifiable manifest, with optional HMAC-SHA256 cryptographic signing. Operators can inspect and validate the complete dossier offline before applying the patch to their repository.

---

## 2. Invariants & Safety Boundaries

1. **Target Repository Immutability**: Export operations write exclusively to a user-specified output directory. The target repository remains completely untouched. No auto-application of `patch.diff`, no staging, and no git repository manipulation are performed.
2. **Mandatory Human Operator Approval Gate**: Export is strictly conditioned on the workflow being in `APPROVED` state with recorded `human_decision == "approve"`. Export attempts on workflows in any intermediate, unapproved, rejected, or blocked state are immediately refused under `DossierWorkflowNotApprovedError` or `DossierExportRefusedError`.
3. **Pre-Export Revalidation**: Prior to exporting, the workflow state, sequential audit ledger, intake revision, patch digest, protected file immunity, verification outcome, and local git HEAD (if accessible) are strictly re-verified. Any discrepancies abort the export without writing partial data.
4. **Integrity vs. Authentication Separation**:
   - **Artifact Integrity**: Provided by deterministic, canonical SHA-256 file hashes recorded in `manifest.json`. Any byte modification to `patch.diff` or metadata files immediately breaks validation.
   - **Operator Authentication**: Provided by an optional HMAC-SHA256 signature calculated over the canonical sorted file digests using a pre-shared operator key.
5. **Path Containment & Symlink Defense**: The export destination directory cannot be a symlink or point to a symlink. Overwriting an existing directory requires `--force`, and directories containing symlinks or foreign files are rejected under `DossierSecurityError`.
6. **Outbound Secret Scrubbing**: All exported artifacts (including findings SARIF, verification logs, audit event payloads, and operator instructions) are scrubbed via `redact_secrets` before serialization to prevent credential leaks.

---

## 3. Package Layout & File Specifications

An exported engineering dossier consists of five canonical required files and a root integrity manifest:

```
<dossier_dir>/
├── patch.diff                       # Candidate unified diff (<= 100 lines, <= 20 KB)
├── verification_certificate.json    # Machine-readable multi-check verification certificate
├── findings.sarif                   # Standards-compliant SARIF 2.1.0 defect findings report
├── audit_ledger.json                # Complete sequential audit event history from inception
├── APPLY.md                         # Step-by-step human review, apply, and rollback instructions
└── manifest.json                    # Canonical SHA-256 integrity manifest + optional HMAC signature
```

### 3.1 `patch.diff`
- Contains the unmodified, candidate unified diff generated during repair planning.
- Bound by strict size budgets: $\le 100$ lines modified, $\le 20\text{ KB}$ byte size.
- Pre-export validated against stored SHA-256 digest and single-file target policy.

### 3.2 `verification_certificate.json`
- Schema-validated document conforming to `VerificationCertificate`.
- Records:
  - `certificate_id`, `workflow_id`, `base_commit_sha`, `patch_digest`.
  - `aggregate_outcome` (`passed`, `failed`, `blocked`, `inconclusive`).
  - `checks_evaluated`: List of individual check executions (command, arguments, exit code, duration, stdout digest).
  - `baseline_vs_candidate`: Delta comparison proving baseline failure and candidate fix.
  - `human_review`: Recorded operator decision, timestamp, and review notes.
  - `limitations`: Explicit notes regarding offline test environment or skipped container checks.

### 3.3 `findings.sarif`
- Standards-compliant OASIS SARIF 2.1.0 document.
- Links the investigated defect, rule ID, severity, confidence, affected file URI, and region.
- Includes FORGE tool metadata and analysis run context.

### 3.4 `audit_ledger.json`
- Complete chronological audit event history conforming to `AuditLedgerExport`.
- Contains strictly sequential events ($1, 2, \dots, N$) tracing every lifecycle transition from `CREATED` through `APPROVED`.
- Guaranteed sanitized: all event payloads scrubbed of API keys, tokens, and private paths.

### 3.5 `APPLY.md`
- Clear, human-readable review instructions for the operator.
- Contains:
  - Audit-only and human supervision safety disclaimer.
  - Git pre-condition check (`git rev-parse HEAD == <base_commit_sha>`).
  - Safe dry-run command (`git apply --check patch.diff`).
  - Patch application command (`git apply patch.diff`).
  - Recommended regression verification commands (`pytest`, `flake8`, `bandit`).
  - Rollback instructions in case of unexpected runtime anomalies (`git checkout -- <target_file>`).

### 3.6 `manifest.json`
- Cryptographic integrity anchor conforming to `DossierManifest`.
- Maps canonical relative file paths to their exact byte sizes and SHA-256 digests.
- Contains optional `DossierSignature` object with key ID, algorithm (`HMAC-SHA256`), and hex signature.

---

## 4. Cryptographic Integrity & Signing Model

### 4.1 SHA-256 File Manifest
For every file in the package (excluding `manifest.json` itself):
$$\text{sha256} = \text{Hex}(\text{SHA-256}(\text{file\_bytes}))$$
The manifest records each file entry:
```json
{
  "byte_size": 284,
  "sha256": "4b92b67...",
  "format": "diff"
}
```

### 4.2 HMAC-SHA256 Cryptographic Signature
When an operator signing key is provided (`--key` or `--key-file`):
1. All file paths in the manifest are sorted alphabetically.
2. A canonical signature payload is assembled:
   $$\text{payload} = \text{workflow\_id} \mathbin{\Vert} \text{base\_commit\_sha} \mathbin{\Vert} \text{patch\_digest} \mathbin{\Vert} \sum_{i} (\text{path}_i \mathbin{\Vert} \text{sha256}_i \mathbin{\Vert} \text{byte\_size}_i)$$
3. The signature is computed via HMAC-SHA256:
   $$\text{sig} = \text{Hex}(\text{HMAC-SHA256}(\text{key}, \text{payload}))$$
4. The signature is embedded in `manifest.json`:
   ```json
   "signature": {
     "algorithm": "HMAC-SHA256",
     "key_id": "operator-key-1",
     "signature": "3f8a1...",
     "signed_at": "2026-10-27T06:30:00Z"
   }
   ```

---

## 5. Verification & Validation Engine

The offline validation function `validate_engineering_dossier` validates an exported package without requiring a database, Docker, or network connection:
1. **Manifest Existence & Schema**: Reads and validates `manifest.json` against `DossierManifest`.
2. **File Completeness**: Ensures all required files (`patch.diff`, `verification_certificate.json`, `findings.sarif`, `audit_ledger.json`, `APPLY.md`) are present.
3. **No Foreign Files / Symlinks**: Ensures no unrecognized extra files or symlinks exist in the directory.
4. **Byte-Level SHA-256 Matching**: Recomputes SHA-256 for each file on disk and verifies exact match against manifest.
5. **Schema Validation**: Validates `verification_certificate.json` and `findings.sarif` structures.
6. **Signature Verification**: If signed and a key is provided, recomputes HMAC and verifies authentication.

---

## 6. CLI Command Reference

### Export Dossier
```bash
# Export an approved workflow into a certified dossier
forge workflow export <workflow_id> --output-dir ./dossier_output

# Overwrite existing directory and sign with operator key
forge workflow export <workflow_id> --output-dir ./dossier_output --force --key "secret-operator-key" --key-id "sec-ops-1"

# JSON output mode for scripting and CI pipelines
forge workflow export <workflow_id> --output-dir ./dossier_output --format json
```

### Validate Exported Dossier
```bash
# Offline structural and digest verification
forge workflow validate-dossier ./dossier_output

# Offline verification with HMAC signature check
forge workflow validate-dossier ./dossier_output --key "secret-operator-key"

# JSON output mode
forge workflow validate-dossier ./dossier_output --format json
```

---

## 7. Verification & Test Coverage

Milestone 5 is validated by a dedicated test suite in `tests/unit/test_phase5_milestone5.py`:
- **40 Dedicated Unit & Integration Tests**:
  - Gating: Refusal for all 14 unapproved states (`CREATED`, `REPRO_FAILED`, `AWAITING_HUMAN_REVIEW`, etc.).
  - Ledger: Refusal on empty ledger or non-monotonic sequence gaps.
  - Tampering: Rejection on modified patch diffs or protected file violations.
  - Immutability: Bit-for-bit target repository immutability verified before and after export.
  - Containment: Rejection of symlink output directories and traversal attacks.
  - Secret Scrubbing: Verification that tokens and keys are redacted in all output files.
  - Cryptography: HMAC-SHA256 signing, validation, wrong-key failure, and digest verification.
  - CLI: End-to-end testing of `forge workflow export` and `forge workflow validate-dossier`.
- **Regression Suite**: All 829 test cases in the full test suite passed with zero regressions.
- **Acceptance & Benchmark Gates**: `forge repair test` (26/26 passed, 100%) and `forge benchmark repair` (11/11 passed, 100%).
