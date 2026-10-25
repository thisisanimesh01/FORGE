# Phase 3, Milestone 2: Reproduction Case Management and Evidence Capture

## 1. Overview & Objective

Phase 3, Milestone 2 builds a persistent, transactionally consistent case-management layer on top of FORGE's isolated sandbox reproduction contracts. It enables software engineering teams to manage, execute, retry, and inspect defect reproduction attempts while maintaining strict evidence provenance and audit trails without executing untrusted code on the host machine.

---

## 2. Core Architecture (`forge.reproduction`)

### 2.1 Domain Models (`forge.reproduction.models`)
- **`ReproductionCase`**: Tracks finding ID, target repository, immutable pinned commit SHA, validated contract JSON, resource policy version, current aggregate status (`pending`, `running`, `reproduced`, `not_reproduced`, `inconclusive`, `blocked`, `failed`), execution counts, retry limits, and field-level provenance map.
- **`generate_case_id(finding_id, target_repo, commit_sha)`**: Deterministic case ID generation via SHA256 digest (`case_{finding_id}_{hash}`).
- **`ReproductionAttempt`**: Structured record of an individual execution attempt capturing:
  - Attempt number (1-indexed) and correlation run ID.
  - Exit code, execution duration in milliseconds, and cleanup status.
  - Raw output SHA256 digests (`stdout_digest`, `stderr_digest`).
  - Bounded excerpts (max 5,000 characters) with truncation indicators.
  - Linked `EvidenceRecord` ID in persistent store.
  - Matched pattern array and isolation environment details.
- **`RetryPolicy`**: Enforces max retry bounds (0–3) and explicit command idempotency constraints (`is_idempotent: bool`).
- **`FieldProvenance`**: Explicit categorization of field origins (`trusted_fixture`, `user_input`, `static_analysis`, `untrusted_llm`, `untrusted_repository`, `sandbox_execution`, `system_policy`).

### 2.2 Case Management & Execution (`forge.reproduction.manager`)
- **`ReproductionCaseManager`**:
  - `create_case(...)`: Validates target finding in store, initializes case state in SQLite, and emits `EventType.REPRODUCTION_CASE_CREATED`.
  - `run_attempt(...)`: Coordinates reproduction execution:
    1. Revalidates repository revision against pinned commit SHA; rejects stale revisions (`StaleRevisionError`).
    2. Enforces retry policy: checks max retries and refuses retry of non-idempotent commands (`NonIdempotentRetryError`).
    3. Emits `REPRODUCTION_ATTEMPT_STARTED` / `REPRODUCTION_CASE_RESUMED` events.
    4. Evaluates contract inside sandbox harness (or records fail-closed `BLOCKED`/`SKIPPED_SANDBOX_UNAVAILABLE` when Docker is absent).
    5. Computes digests and bounds/redacts stdout/stderr excerpts before persistence.
    6. Creates and attaches verified `EvidenceRecord` of type `REPRODUCTION_RESULT` to the finding.
    7. Atomically advances finding lifecycle state to `REPRODUCED` only when observed reproduction criteria are satisfied and evidence is verified.
  - `list_cases(...)` & `get_case(...)`: Deterministic retrieval from SQLite.
  - `list_attempts(...)`: Chronological history of all executed attempts.

---

## 3. Evidence Capture & Lifecycle Rules

1. **Outcome Differentiation**:
   - `REPRODUCED`: Expected exit code and all expected regex patterns matched.
   - `NOT_REPRODUCED`: Command completed cleanly (exit 0) or failed without matching expected defect patterns.
   - `INCONCLUSIVE`: Exit code matched but only a partial pattern match was observed.
   - `BLOCKED` / `SKIPPED_SANDBOX_UNAVAILABLE`: Execution skipped due to missing sandbox runtime. Host fallback is strictly refused.
   - `TIMED_OUT`: Container exceeded timeout quota.
   - `FAILED` / `ERROR`: Unexpected exit code or initialization failure.
2. **Non-Fabrication & Lifecycle Enforcement**:
   - Passing tests alone do not prove absence of defect.
   - Arbitrary nonzero exits do not prove defect presence.
   - Untrusted LLM claims or unverified command outputs cannot transition findings to `reproduced`.
3. **Immutability & Redaction**:
   - All secret tokens (`sk-...`, Authorization headers) are redacted before persisting in SQLite or rendering.
   - Prior attempt records are strictly preserved; later attempts append to history without overwriting failed runs.
   - Target repository files are verified immutable.

---

## 4. CLI Usage

### 4.1 Create a Reproduction Case
```bash
forge repro create contracts/auth_defect.json --commit a1b2c3d4e5f6 --max-retries 1
```

### 4.2 List Reproduction Cases
```bash
forge repro list
forge repro list --finding-id find_01
forge repro list --format json
```

### 4.3 Inspect a Case and Attempts History
```bash
forge repro show case_find_01_7e3b9a1d4c20
forge repro show case_find_01_7e3b9a1d4c20 --format json
```

### 4.4 Run or Resume an Attempt
```bash
forge repro run case_find_01_7e3b9a1d4c20
forge repro run case_find_01_7e3b9a1d4c20 --resume
forge repro run case_find_01_7e3b9a1d4c20 --format json
```
