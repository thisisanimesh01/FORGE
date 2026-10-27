# Phase 5, Milestone 6: End-to-End Workflow Acceptance, Reliability & Release

- **Document ID:** `docs/specs/phase-5-m6-acceptance.md`
- **Milestone:** Phase 5 M6 — End-to-End Workflow Acceptance, Reliability & Release
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Audit-only; zero mutation, commits, pushes, merges, PRs, or deployments)
- **Mandatory Human Review Gate:** ENFORCED (Automated workflows strictly halt at `AWAITING_HUMAN_REVIEW`)
- **Release Decision:** `READY FOR SUPERVISED OPERATOR DEPLOYMENT`
- **Commit Message:** `feat(workflow): finalize phase 5 acceptance and release readiness`
- **Traceability:** Implements Phase 5 Milestone 6 per `AGENTS.md` (Sections 1, 2, 3, 4, 5, 7), closing Phase 5 milestones M1 through M5 (`docs/specs/phase-5-roadmap.md`, `docs/specs/phase-5-m1-intake-context.md`, `docs/specs/phase-5-m2-workflow-ledger.md`, `docs/specs/phase-5-m3-evidence-chaining.md`, `docs/specs/phase-5-m4-multi-check-verification.md`, `docs/specs/phase-5-m5-dossier-export.md`).

---

## 1. Executive Summary & Objective

Phase 5 Milestone 6 completes the final end-to-end acceptance, reliability hardening, and release audit of FORGE's supervised engineering workflow. It establishes a comprehensive 30-scenario deterministic acceptance corpus covering 10 distinct failure and reliability domains, audits cross-milestone integration (M1–M5), validates real vs. mocked container isolation contracts, conducts full regression testing across all 838 tests, and delivers an evidence-grounded release decision.

### Core Architectural Invariants & Safety Contracts
1. **Target Repository Immutability**: All workflow stages—task intake, context extraction, defect reproduction, repair planning, multi-check verification, and dossier generation—operate strictly on read-only targets or within isolated, disposable scratch workspaces. FORGE **never modifies target repository files, never creates git commits, never pushes branches, never opens pull requests, never merges code, and never deploys software**.
2. **Mandatory Human Review Gate**: Candidate repairs are **never autonomously approved or applied**. Every workflow must halt strictly at `AWAITING_HUMAN_REVIEW`. Only an explicit operator review command (`forge workflow review <id> --decision approve`) can advance the state to `APPROVED`, which in turn is a mandatory prerequisite for exporting an engineering dossier.
3. **Fail-Closed Container Sandboxing**: Any dynamic execution of code from target repositories requires containerized isolation (`DockerSandbox`). When Docker is unavailable, the workflow transitions safely to `REPRO_BLOCKED` or `VERIFICATION_BLOCKED`. Target repository code is **strictly forbidden from executing directly on the host machine**.
4. **Cryptographic Revision & Tamper Detection**: Workflows are pinned to the verified target repository commit SHA. Divergence between repository HEAD and the pinned SHA triggers immediate blocking under `RevisionDriftError`. Patch candidates are locked by canonical SHA-256 digests.
5. **Honest Outcome Attribution**: Pre-existing baseline test failures are honestly classified as `BASELINE_FAILED` and never credited to candidate patches. Dynamic failures are categorized deterministically (`REPRO_FAILED`, `REPRO_INCONCLUSIVE`, `REGRESSION_FAILED`, `VERIFICATION_FAILED`).
6. **Outbound Secret Containment**: All event payloads, findings, verification logs, and exported dossier packages are scrubbed via `redact_secrets` before storage and serialization.

---

## 2. Acceptance Corpus & 10 Reliability Domains

The Phase 5 acceptance harness (`app/forge/workflow/acceptance.py`) implements 30 deterministic scenarios across 10 essential reliability domains:

| Scenario ID | Name | Category | Execution Mode | Expected Final State | Verified Status |
|---|---|---|---|---|---|
| `P5_ACC01` | Valid Task Intake & Exact Revision Pinning | `intake_and_context` | `synthetic_e2e` | `REPO_ANALYZED` | **PASSED** |
| `P5_ACC02` | Bounded Repository Context & Truncation Budget | `intake_and_context` | `unit_boundary` | `context_budgeted` | **PASSED** |
| `P5_ACC03` | Issue Description Intake Normalization | `intake_and_context` | `synthetic_e2e` | `CREATED` | **PASSED** |
| `P5_ACC04` | Hostile Repository Instruction File Containment | `adversarial_intake` | `unit_boundary` | `instructions_contained` | **PASSED** |
| `P5_ACC05` | Prompt Injection in Defect Intake Containment | `adversarial_intake` | `unit_boundary` | `injection_defended` | **PASSED** |
| `P5_ACC06` | Intake Path Traversal Escape Rejection | `adversarial_intake` | `unit_boundary` | `REJECTED` | **PASSED** |
| `P5_ACC07` | Legal Workflow State Machine Transitions | `state_machine_ledger` | `synthetic_e2e` | `REPO_ANALYZED` | **PASSED** |
| `P5_ACC08` | Illegal State Transition Rejection & Preservation | `state_machine_ledger` | `synthetic_e2e` | `CREATED` | **PASSED** |
| `P5_ACC09` | Optimistic Concurrency Conflict Detection | `state_machine_ledger` | `unit_boundary` | `concurrency_defended` | **PASSED** |
| `P5_ACC10` | Transactional Ledger Monotonic Sequence & Redaction | `state_machine_ledger` | `unit_boundary` | `ledger_verified` | **PASSED** |
| `P5_ACC11` | Defect Reproduction Success with Trusted Oracle | `reproduction_success` | `mocked_sandbox` | `REPRODUCED` | **PASSED** |
| `P5_ACC12` | Defect Not Observed Fails Closed (`REPRO_FAILED`) | `reproduction_outcomes` | `mocked_sandbox` | `REPRO_FAILED` | **PASSED** |
| `P5_ACC13` | Flaky Test Oracle Marked Inconclusive | `reproduction_outcomes` | `mocked_sandbox` | `REPRO_INCONCLUSIVE` | **PASSED** |
| `P5_ACC14` | Reproduction Timeout Fails Closed Gracefully | `reproduction_outcomes` | `mocked_sandbox` | `REPRO_FAILED` | **PASSED** |
| `P5_ACC15` | Protected Path Repair Plan Rejection | `reproduction_outcomes` | `unit_boundary` | `repair_rejected` | **PASSED** |
| `P5_ACC16` | Docker Unavailable Fails Closed (`REPRO_BLOCKED`) | `sandbox_fail_closed` | `unit_boundary` | `REPRO_BLOCKED` | **PASSED** |
| `P5_ACC17` | Sandbox Resource Exhaustion & Timeout Boundaries | `sandbox_fail_closed` | `unit_boundary` | `limits_enforced` | **PASSED** |
| `P5_ACC18` | Sandbox Network Isolation Enforcement | `sandbox_fail_closed` | `unit_boundary` | `network_isolated` | **PASSED** |
| `P5_ACC19` | Reproduction Evidence Tampering Detection | `evidence_integrity_drift` | `unit_boundary` | `tamper_detected` | **PASSED** |
| `P5_ACC20` | Target Repository Revision Drift Interception | `evidence_integrity_drift` | `synthetic_e2e` | `drift_detected` | **PASSED** |
| `P5_ACC21` | Idempotent Duplicate Workflow Start | `evidence_integrity_drift` | `synthetic_e2e` | `CREATED` | **PASSED** |
| `P5_ACC22` | Safe Resumption of Blocked Workflows | `evidence_integrity_drift` | `synthetic_e2e` | `resume_rules_enforced` | **PASSED** |
| `P5_ACC23` | Clean Repair Verification & Regression Checking | `multi_check_regression` | `mocked_sandbox` | `AWAITING_HUMAN_REVIEW` | **PASSED** |
| `P5_ACC24` | Baseline Test Regression Interception | `multi_check_regression` | `mocked_sandbox` | `REGRESSION_FAILED` | **PASSED** |
| `P5_ACC25` | Pre-Existing Baseline Defect Honest Attribution | `multi_check_regression` | `unit_boundary` | `honest_attribution` | **PASSED** |
| `P5_ACC26` | Graceful Degradation for Missing Optional Tools | `multi_check_regression` | `unit_boundary` | `optional_tools_supported` | **PASSED** |
| `P5_ACC27` | Mandatory Human Review Gate Enforcement | `human_review_gate` | `synthetic_e2e` | `CREATED` | **PASSED** |
| `P5_ACC28` | Operator Rejection Terminates Workflow Cleanly | `human_review_gate` | `synthetic_e2e` | `REJECTED` | **PASSED** |
| `P5_ACC29` | Unapproved Workflow Dossier Export Refusal | `human_review_gate` | `synthetic_e2e` | `CREATED` | **PASSED** |
| `P5_ACC30` | Dossier Export, SHA-256 Manifest & HMAC Verification | `dossier_integrity_export` | `synthetic_e2e` | `dossier_exported` | **PASSED** |

**Summary Statistics:**
- Total Scenarios: **30**
- Passed: **30** (100.0%)
- Failed: **0** (0.0%)
- Skipped: **0** (0.0%)
- Target Repositories Mutated: **0** (100% verified immutable)

---

## 3. Cross-Milestone Integration Audit (M1–M6)

The entire Phase 5 pipeline was systematically audited across all milestone contracts:

| Milestone | Subsystem / Contract | Verification Method | Integration Status | Observations & Hardening |
|---|---|---|---|---|
| **M1** | Unified Task Intake & Repository Context | `tests/unit/test_phase5_milestone1.py` | Verified Pass | Validates all 4 intake source types (`review_finding`, `unified_diff`, `issue_description`, `test_specification`), revision pinning, token/character budgeting, path traversal rejection, instruction containment. |
| **M2** | State Machine & Transactional Ledger | `tests/unit/test_phase5_milestone2.py` | Verified Pass | 19-state lifecycle machine with transition guards, optimistic concurrency (`version`), monotonic integer sequence, secret-scrubbed append-only audit events, crash recovery, unblocking, and human review gate. |
| **M3** | Automated Evidence Chaining | `tests/unit/test_phase5_milestone3.py` | Verified Pass | Non-shell reproduction command extraction, evidence validation (provenance, non-empty, exit code consistency), atomic repair plan attachment, pipeline progression through `advance_workflow`. |
| **M4** | Multi-Check Regression Verification | `tests/unit/test_phase5_milestone4.py` | Verified Pass | Multi-check verification plan execution (`MultiCheckVerifier`) in container sandbox, shell injection defense, regression detection, honest baseline failure attribution, transition to `AWAITING_HUMAN_REVIEW`. |
| **M5** | Certified Dossier & Operator Export | `tests/unit/test_phase5_milestone5.py` | Verified Pass | Pre-export validation (approval, revision, patch digest, ledger), package layout (`patch.diff`, `verification_certificate.json`, `findings.sarif`, `audit_ledger.json`, `APPLY.md`, `manifest.json`), canonical SHA-256 manifest, HMAC signing, offline validation. |
| **M6** | End-to-End Acceptance & Release | `tests/unit/test_phase5_milestone6.py`, `forge workflow test` | Verified Pass | 30-scenario 10-domain acceptance corpus, metric calculation with honest denominators, secret scrubbing, clean separation of live Docker tests, CLI integration. |

---

## 4. Real vs. Mocked Sandbox Isolation & Fail-Closed Enforcement

FORGE maintains a strict separation between test execution modes:
1. **Offline & Deterministic Execution**:
   - The standard acceptance suite and all unit tests run completely offline and deterministically without requiring a running Docker daemon.
   - Fail-closed behavior (`REPRO_BLOCKED`, `VERIFICATION_BLOCKED`) is verified under both simulated and real environments.
   - When Docker is absent or permission-denied, FORGE strictly refuses to fall back to running repository code directly on the host machine.
2. **Opt-In Live Container Execution**:
   - Live Docker execution is supported via the `--docker-live` CLI flag.
   - Requires an accessible Docker daemon with standard security profiles: `--network none`, `--read-only`, non-root user (`1000:1000`), dropped Linux capabilities (`cap-drop ALL`), and strict resource limits (CPU, memory, process count).

---

## 5. Honest Metrics & Denial of False Certainty

To adhere to `AGENTS.md` Section 5 ("No Fabrication") and Section 7:
1. **Honest Denominators**: Empty scenario classes or filtered sets report `N/A` or `None` rather than fabricated `0%` or `100%`.
2. **Separation of Concerns**: Case-level execution metrics and finding-level defect metrics are reported separately with explicit denominators.
3. **Explicit Synthetic Scope Disclosure**: Every acceptance report embeds the required disclaimer:
   > *Phase 5 acceptance scenarios evaluate synthetic defect models, adversarial inputs, and deterministic state transitions. Synthetic test compliance does NOT represent real-world repair accuracy on arbitrary production repositories. Audit-only policy enforced: target repositories are strictly immutable with zero automated commits, pushes, merges, or deployments.*

---

## 6. Verification Suite & Release Gate Results

All mandatory release gates defined in `AGENTS.md` Section 7 were executed and passed:

1. **Phase 5 Acceptance Suite**:
   ```
   forge workflow test
   Pass Rate: 100.0% (30/30 passed)
   ```
2. **Phase 4 Acceptance & Benchmarks**:
   ```
   forge repair test: 26/26 passed (100.0%)
   forge benchmark repair: 11/11 passed (100.0%)
   ```
3. **Full Regression Test Suite**:
   ```
   pytest
   838 passed, 2 skipped, 0 failures (78.01s)
   ```
4. **Health Diagnostics**:
   ```
   forge doctor: OK (Phase 1-5 ready)
   forge sandbox doctor: OK (Fail-secure active)
   ```
5. **Code Quality & Hygiene**:
   - `git diff --check`: 0 errors.
   - Secrets scan: 0 credentials or sensitive tokens detected in tracked files, logs, or reports.

---

## 7. Release Decision & Operational Guidance

### Formal Decision
**READY FOR SUPERVISED OPERATOR DEPLOYMENT**

### Operational Guidance for Human Operators
- **Workflow Execution**: Use `forge workflow start --intake <spec> --auto-advance` or sequential stage commands (`repro`, `plan-repair`, `verify`, `advance`).
- **Human Review**: Inspect the workflow status and regression summary via `forge workflow status <id>`. Submit approval or rejection via `forge workflow review <id> --decision [approve|reject]`.
- **Dossier Export**: Once approved, export the certified dossier via `forge workflow export <id> --output-dir <path>`.
- **Offline Application**: Inspect `APPLY.md` inside the exported dossier. Verify `manifest.json` via `forge workflow validate-dossier <path>`. Review the diff and apply manually using standard git tooling (`git apply patch.diff`).
