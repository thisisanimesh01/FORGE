# FORGE Supervised Beta Contract

**Document Version:** 1.0.0-beta.1
**Phase:** Phase 6 (Supervised Beta Packaging & Operational Readiness)
**Effective Date:** October 2026
**Status:** ACTIVE CONTRACTUAL BASELINE

---

## 1. Purpose & Scope

FORGE is an enterprise-grade autonomous software engineering intelligence platform engineered for **audit, code review, isolated reproduction, bounded repair planning, and verified patch generation**.

This Beta Contract governs the limited, human-supervised developer beta of FORGE. It establishes the non-negotiable boundaries, safety invariants, supported use cases, and operational guarantees under which FORGE may be operated.

---

## 2. Fundamental Safety Invariants

### Invariant 1: Target Repository Immutability
- FORGE operates strictly as an **investigative and analytical intelligence system**.
- FORGE **never modifies target repository source code, never creates git commits, never pushes branches, never opens pull requests, never merges code, and never deploys software**.
- All check execution, reproduction attempts, patch candidate applications, and multi-check verification suites occur exclusively inside isolated, ephemeral scratch workspaces.
- The target repository remains bit-for-bit immutable throughout all audit, reproduction, and repair operations.

### Invariant 2: Mandatory Human Approval Gate
- Autonomously generated patch candidates are **never automatically applied, committed, or published**.
- Every workflow producing a verified patch candidate must halt at the terminal review state: `AWAITING_HUMAN_REVIEW`.
- Only an authenticated human operator may inspect the generated unified diff, review the cryptographic evidence chain and verification certificates, and execute a formal `approve` or `reject` decision.
- The final artifact of a successful workflow is an exportable **Engineering Dossier** (`patch.diff`, verification proof, audit ledger) designed for manual operator review and application.

### Invariant 3: Fail-Closed Container Sandboxing
- Any execution of code originating from or influenced by a target repository (unit tests, reproduction scripts, linters, builds) must execute within an isolated container sandbox (`DockerSandbox`).
- **Container Isolation Controls:**
  - Non-root user execution (`uid 1000:gid 1000`).
  - Dropped Linux capabilities (`--cap-drop ALL`).
  - Read-only workspace root mount with strict temporary workspace boundaries.
  - Complete network isolation (`--network none`).
  - Memory, CPU, process count, and runtime timeout caps.
- **Fail-Secure Refusal:** If the Docker daemon is unreachable or the sandbox environment cannot be established, FORGE marks the operation as `SKIPPED_SANDBOX_UNAVAILABLE` or `REPRO_BLOCKED`. FORGE **strictly refuses to fall back to executing repository code on the host machine**.

### Invariant 4: Local-Only Inference by Default & Zero Silent Cloud Fallback
- Model inference is configured for local Ollama execution by default (`http://localhost:11434/v1`).
- Automatic cloud fallback is strictly disabled (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
- If local model inference fails, times out, or returns invalid syntax, FORGE fails closed with structured diagnostics. It will never silently route proprietary code, repository contents, or tokens to third-party cloud APIs.

### Invariant 5: Honest Outcome Attribution & Zero Fabrication
- Every finding and verification result must be anchored to concrete, cryptographic evidence (reproduction logs, exit codes, output digests, AST spans).
- An unavailable sandbox or failed check is never mislabeled as a verified defect or successful fix.
- Confidence scores and status codes reflect strictly observed facts. Repair states are strictly partitioned from audit and reproduction states.

---

## 3. Supported Beta Use Cases (Python-First)

The Phase 6 Beta focuses on Python-first repositories using standard tooling:

| Use Case | Description | Operating Mode |
| :--- | :--- | :--- |
| **Static Intelligence & Mapping** | Repository structure, architecture, symbols, and instruction detection. | Fully Offline & Static |
| **Diff-Aware Code Review** | SARIF-calibrated code review, defect detection, and CI security policy evaluation. | Model-Assisted (Local) |
| **Automated Defect Reproduction** | Reproduce failing unit test or defect command in Docker isolation; capture cryptographic evidence. | Isolated Docker Sandbox |
| **Evidence-Grounded Repair** | Deterministic edit anchoring and minimal statement-level repair generation for verified defects. | Local Model + Deterministic Anchoring |
| **Multi-Check Patch Verification** | Candidate patch verification (reproduction re-check, regression tests, AST invariants, linting). | Isolated Docker Sandbox |
| **Audit Ledger & Dossier Export** | Export transactional SQLite audit history, signed diffs, and verification certificates. | Operator Command |

---

## 4. Unsupported Workflows & Beta Boundaries

The following capabilities are **explicitly out of scope** for the Phase 6 Supervised Beta:

- **Autonomous Git Mutations:** Automatic branch creation, git commits, PR creation, git pushing, and branch merging are strictly forbidden.
- **Host Code Execution:** Executing test commands or build scripts directly on the operator host without Docker is strictly forbidden.
- **Multi-File & Architecture-Wide Refactoring:** Repairs spanning multiple files or requiring complex architectural restructuring are unsupported.
- **Unsupervised / Autonomous Deployment:** Automated deployments, release tagging, or production pushes are strictly forbidden.
- **Non-Python Toolchains:** Multi-language compilation (e.g., C/C++, Rust, Java, Go) is deferred to future enterprise phases.

---

## 5. Model Capabilities & Known Limitations

Based on empirical evidence across Supervised Pilots 01 through 08 using local Ollama (`qwen2.5-coder:1.5b`):

### Supported Repair Patterns (High Reliability)
- **Single-Statement Arithmetic & Logical Fixes:** Correcting calculation expressions, operator precedence, off-by-one constants, and bounded mathematical formulas within a localized statement.
- **Guard & Invariant Preservation:** Maintaining pre-existing parameter validations, exception raises, and docstring integrity.

### Known Limitations (Supervision Required)
- **Multi-Statement Control-Flow Synthesis:** Small local models (1.5B parameters) struggle to coordinate multiple exit paths when altering control-flow structures (observed in Pilot 07).
- **Cross-Type Statement Replacement:** Replacing a `return` statement with an exception `raise` statement frequently triggers indentation stripping on 1.5B models (observed in Pilot 08).
- **Mitigation:** FORGE's deterministic repair construction layer traps these errors before disk mutation, rejects invalid proposals, and fails closed safely. For complex multi-statement defects, operators should deploy larger quantized local models (7B/14B) or inspect the generated diagnostic feedback.

---

## 6. Auditability & Incident Recovery

- **Transactional Ledger:** All workflow transitions are monotonically recorded in SQLite with sequential integer IDs (`sequence`), immutable timestamps, and state tracking.
- **Evidence Immutability:** Evidence records are cryptographically hashed (SHA-256) and cannot be altered post-creation.
- **Revision Drift Interception:** Workflows are pinned to base commit SHAs. Any unexpected shift in target repository HEAD aborts the workflow with `RevisionDriftError`.
- **Fail-Closed Diagnostics:** Every rejection produces structured, secret-scrubbed diagnostics explaining the exact policy, AST, or runtime check violation.

### 6.1 Operational Diagnostics & Tiered Readiness Invariant
- **Categorized Health Auditing:** System dependencies are audited through deterministic categories (`AVAILABLE_HEALTHY`, `MISSING_OPTIONAL`, `MISCONFIGURED_ENV`, `UNAVAILABLE_REQUIRED`, `PERMISSION_DENIED`, `NETWORK_FAILURE`, `UNSUPPORTED_RUNTIME`, `UNSAFE_CONFIGURATION`).
- **Zero False Readiness:** FORGE strictly refuses to report the system as ready when mandatory runtime dependencies fail. If the Docker daemon is unreachable or socket permissions are restricted, FORGE downgrades readiness to `READY_FOR_STATIC_INSPECTION_ONLY` and halts live reproduction attempts.
- **Dedicated Diagnostic CLI:** Operators verify dependency health using `forge doctor` (composite), `forge sandbox doctor` (Docker/isolation), and `forge provider doctor` (Ollama/model inventory).

---

## 7. Supply Chain Security, Provenance & Release Integrity

### Invariant 6: Cryptographic Artifact Verification & Forensic Cleanliness
- **Digest Matching:** Every official FORGE distribution package (.whl and .tar.gz) is cryptographically indexed in `release_manifest.json` and `SHA256SUMS.txt`. Operators must verify SHA-256 digests prior to execution using `forge release verify` or `scripts/verify_release_artifacts.py`.
- **Zero Sensitive Data Leakage:** Distribution packages are forensically scanned to guarantee 100% exclusion of environment files (`.env`), SQLite databases (`*.db`, `*.sqlite3`), test fixtures, pilot directories, and internal caches.
- **Distinction Between Integrity & Authenticity:** Checksums prove that an artifact has not been modified or corrupted since manifest creation (integrity). Cryptographic attestation (`gh attestation verify`) proves that the artifact was built from the official GitHub Actions workflow and repository source commit (authenticity).
- **Pinned Dependencies & Zero Known Vulnerabilities:** Production installations must adhere to `requirements/constraints.txt`. All dependencies are audited against the PyPA vulnerability database (`pip-audit`) with zero tolerated CVEs.
