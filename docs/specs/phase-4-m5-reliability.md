# Phase 4, Milestone 5: End-to-End Repair Reliability & Adversarial Testing

- **Milestone:** Phase 4 M5 — End-to-End Repair Reliability & Adversarial Testing
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED (Audit-only; zero mutation, commits, pushes, merges, or PRs)
- **Commit Message:** `test(repair): harden end-to-end repair reliability`
- **Traceability:** Implements Phase 4 Milestone 5 as defined in `AGENTS.md`, `docs/specs/phase-4-readiness.md`, `docs/specs/phase-4-m1-repair-planning.md`, `docs/specs/phase-4-m2-patch-verification.md`, `docs/specs/phase-4-m3-repair-benchmarks.md`, and `docs/specs/phase-4-m4-repair-orchestration.md`.

---

## 1. Executive Summary & Objective

Phase 4 Milestone 5 delivers the end-to-end acceptance testing, adversarial resilience, and failure-injection harness for FORGE's autonomous repair subsystem. It hardens the integrated workflow from audited finding through plan generation, patch validation, sandboxed verification, and mandatory human review across 26 comprehensive, deterministic acceptance scenarios.

### Core Principles & Safety Invariants
1. **Target Repository Immutability**: All operations occur in temporary test harnesses, disposable git fixtures, or in-memory models. Target repositories remain 100% read-only. FORGE **never modifies target repository files, never creates git commits, never pushes branches, never opens pull requests, and never deploys code**.
2. **Mandatory Human Review Gate**: A verified patch candidate is **never automatically approved or committed**. Verified workflows strictly pause at `AWAITING_HUMAN_REVIEW`. Only explicit human operator decision (`forge repair review --decision approve`) can transition to `APPROVED`.
3. **Fail-Closed Container Sandboxing**: When container isolation (e.g. Docker) is unavailable, verification immediately blocks with outcome `SANDBOX_UNAVAILABLE`. Target repository code is **never executed directly on the host**.
4. **Anti-Tampering Integrity**: Patch candidates are bound by a cryptographic SHA256 digest calculated at plan validation time. Any mutation or tampering with unified-diff content prior to execution triggers immediate detection and transition to `REJECTED`.
5. **TOCTOU Drift Detection**: Changes to the target repository's git HEAD revision between workflow initiation and verification are proactively intercepted, transitioning to `BLOCKED`.
6. **Synthetic Scope Disclaimer**: Acceptance scenarios evaluate synthetic defect models, adversarial inputs, and deterministic state transitions. Synthetic test compliance does **not** represent real-world repair accuracy on arbitrary production software repositories.

---

## 2. Comprehensive Acceptance Matrix (26 Scenarios)

The acceptance suite in [`app/forge/repair/acceptance.py`](../../app/forge/repair/acceptance.py) executes 26 deterministic, offline acceptance scenarios categorized into 10 distinct failure domains:

| ID | Scenario Name | Category | Execution Mode | Expected State | Expected Outcome | Immutability |
|---|---|---|---|---|---|---|
| `ACC01` | Happy Path E2E Workflow to Human Review | `happy_path` | `mocked_sandbox` | `awaiting_human_review` | `repair_verified` | Enforced |
| `ACC02` | Unverified Suspected Finding Planning Refusal | `evidence_integrity` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC03` | Oversized Patch Candidate Budget Rejection | `patch_safety` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC04` | Multi-File Scope Budget Rejection | `patch_safety` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC05` | Protected File Immunity Rejection (`AGENTS.md`) | `patch_safety` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC06` | Anti-Tampering SHA256 Digest Rejection | `patch_safety` | `synthetic_e2e` | `rejected` | `-` | Enforced |
| `ACC07` | Stale Base Revision Blocking (Planning Stage) | `revision_drift` | `synthetic_e2e` | `blocked` | `-` | Enforced |
| `ACC08` | Stale Base Revision Blocking (Verification Stage) | `revision_drift` | `synthetic_e2e` | `blocked` | `-` | Enforced |
| `ACC09` | Verification Outcome: Defect Unresolved (`REPAIR_FAILED`) | `verification_outcomes` | `mocked_sandbox` | `failed` | `repair_failed` | Enforced |
| `ACC10` | Verification Outcome: Broken Build (`BROKEN_BUILD`) | `verification_outcomes` | `mocked_sandbox` | `failed` | `broken_build` | Enforced |
| `ACC11` | Verification Outcome: Regression (`REGRESSION_DETECTED`) | `verification_outcomes` | `mocked_sandbox` | `failed` | `regression_detected` | Enforced |
| `ACC12` | Verification Outcome: Timeout (`TIMEOUT`) | `verification_outcomes` | `mocked_sandbox` | `failed` | `timeout` | Enforced |
| `ACC13` | Verification Outcome: Inconsistent Oracle (`FLAKY`) | `verification_outcomes` | `mocked_sandbox` | `failed` | `flaky` | Enforced |
| `ACC14` | Verification Outcome: Inconclusive (`INCONCLUSIVE`) | `verification_outcomes` | `mocked_sandbox` | `failed` | `inconclusive` | Enforced |
| `ACC15` | Fail-Closed Sandbox Boundary (Docker Inactive) | `sandbox_isolation` | `unit_boundary` | `blocked` | `sandbox_unavailable` | Enforced |
| `ACC16` | LLM Provider Timeout Resilience | `provider_resilience` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC17` | Malformed Model Output Rejection | `provider_resilience` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC18` | Provider Authentication Error Resilience | `provider_resilience` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC19` | Prohibition of Unauthorized Provider Fallback (`openai`) | `provider_resilience` | `unit_boundary` | `N/A` | `-` | Enforced |
| `ACC20` | Resumption of Blocked Workflow Once Unblocked | `workflow_recovery` | `synthetic_e2e` | `awaiting_human_review` | `repair_verified` | Enforced |
| `ACC21` | Idempotent Duplicate Workflow Start Reuse | `workflow_recovery` | `synthetic_e2e` | `created` | `-` | Enforced |
| `ACC22` | Optimistic Concurrency Conflict Detection | `workflow_recovery` | `synthetic_e2e` | `evidence_validated` | `-` | Enforced |
| `ACC23` | Adversarial Prompt Injection Untrusted Framing | `adversarial_security` | `synthetic_e2e` | `rejected` | `-` | Enforced |
| `ACC24` | Path Traversal Scope Escape Rejection | `adversarial_security` | `unit_boundary` | `rejected` | `-` | Enforced |
| `ACC25` | Credential Redaction in Workflow Ledger | `adversarial_security` | `synthetic_e2e` | `evidence_validated` | `-` | Enforced |
| `ACC26` | Mandatory Human Review Gate & Immutability | `human_gate` | `synthetic_e2e` | `approved` | `-` | Enforced |

---

## 3. Discovered Vulnerabilities & Hardening Fixes

During the implementation and adversarial testing audit in Milestone 5, several critical bugs and edge cases were identified and hardened:

1. **Planner & Verifier Crash Resilience**:
   - *Issue*: An unhandled exception or GPU OOM in `RepairPlanner.plan_repair()` or `RepairVerifier.verify_plan()` would terminate the process, leaving the workflow stuck in an indeterminate or active state without an audit event.
   - *Fix*: Wrapped both execution points in defensive `try...except Exception as exc:` blocks in [`app/forge/repair/orchestrator.py`](../../app/forge/repair/orchestrator.py), transitioning cleanly to `FAILED` with sanitized diagnostics and emitting corresponding ledger events.
2. **State Machine Transitions for Failure & Resumption**:
   - *Issue*: `ALLOWED_WORKFLOW_TRANSITIONS` lacked `EVIDENCE_VALIDATED -> FAILED` (causing transition rejections on planning failure), `FAILED -> EVIDENCE_VALIDATED` (blocking retry of failed planning), and `BLOCKED -> CREATED` (blocking retry of drifted base repos).
   - *Fix*: Added valid recovery transitions to `ALLOWED_WORKFLOW_TRANSITIONS` in [`app/forge/repair/orchestrator_models.py`](../../app/forge/repair/orchestrator_models.py).
3. **Defense-in-Depth Patch Revalidation**:
   - *Issue*: If a patch candidate was modified or loaded from an external record, `_step_validate_patch` only checked the cryptographic digest and line bounds.
   - *Fix*: Added revalidation for hunk counts (`max_hunks`), total patch byte size (`max_patch_bytes`), modified file counts (`max_files_modified`), and directory traversal patterns (`..`, `/`, `\`) across all candidate `target_files`.
4. **Pre-Sandbox Revision Drift Precedence**:
   - *Issue*: `_step_execute_verification` checked sandbox availability before verifying repository revision consistency. In environments where Docker was inactive, a drifted repository would falsely report `SANDBOX_UNAVAILABLE` instead of detecting base SHA drift.
   - *Fix*: Placed git HEAD extraction and drift verification before the sandbox acquisition gate.
5. **Safe JSON Secret Redaction**:
   - *Issue*: Applying string-based regex redaction to raw serialized JSON text occasionally stripped trailing string quote characters (e.g. when tokens were surrounded by quotes), producing invalid JSON syntax upon deserialization.
   - *Fix*: Implemented recursive dictionary tree sanitization in `render_acceptance_json()` prior to calling `json.dumps()`, guaranteeing structural JSON validity and secret scrubbing.

---

## 4. Quantitative Metrics & Honest Denominators

The acceptance framework computes quantitative verification metrics via `compute_acceptance_metrics()`. To avoid fabricating 0% or 100% metrics when specific categories or modes are unpopulated:
- If `total == 0` for any category or execution mode, `pass_rate` is set to `None` and rendered as `N/A`.
- Equations strictly use observed totals:
  $$\text{Pass Rate} = \frac{\text{Passed}}{\text{Total}}$$

---

## 5. CLI Verification Commands

Milestone 5 exposes the acceptance suite via standard CLI commands in [`app/forge/cli.py`](../../app/forge/cli.py):

```bash
# Execute the full 26-scenario end-to-end acceptance suite (Markdown report)
forge repair test

# Run via acceptance alias
forge repair acceptance

# Run a specific acceptance scenario by ID (e.g., ACC01, ACC15, ACC26)
forge repair test --scenario ACC01

# Export machine-readable JSON acceptance report
forge repair test --format json --output docs/artifacts/acceptance_report.json

# Exit codes:
# 0 = All evaluated scenarios passed
# 1 = One or more scenarios failed
# 2 = Execution or configuration error
```

---

## 6. Verification & Release Gate Results

All mandatory release gates defined in `AGENTS.md` Section 7 have passed:

- **Acceptance Suite**: 26 / 26 scenarios passed (100.0% pass rate).
- **Unit Test Suite**: 17 / 17 tests passed in `tests/unit/test_phase4_m5_reliability.py`.
- **Full Regression Suite**: 685 / 685 tests passed across the entire repository in 20.18s.
- **Diagnostics**: `forge doctor` and `forge sandbox doctor` pass with proper fail-closed mode reported.
- **Git Hygiene**: `git diff --check` clean; `.env` verified untracked and excluded in `.gitignore`.
