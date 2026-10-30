# Phase 4, Milestone 2: Isolated Patch Application & Verification

- **Milestone:** Phase 4 M2 — Isolated Patch Application & Verification
- **Status:** COMPLETED & VERIFIED
- **Phase 4 Autonomous Execution / Target Mutation:** NOT STARTED / FORBIDDEN
- **Commit Message:** `feat(repair): add isolated patch verification`
- **Traceability:** Implements Phase 4 Milestone 2 as defined in `AGENTS.md`, `docs/specs/phase-4-readiness.md`, and `docs/specs/phase-4-m1-repair-planning.md`.

---

## 1. Executive Summary & Objective

Phase 4 Milestone 2 establishes the isolated execution and verification layer for candidate patches generated in Milestone 1. A candidate patch must be safely applied to a disposable scratch workspace and verified against pre/post test reproduction checks inside the container sandbox boundary.

**Safety Invariants & Acceptance Boundaries:**
- **Audit-Only / Target Repo Immutability:** M2 verification strictly executes against a disposable scratch copy of the target repository. It **never modifies target repository source code, never checks out revisions in the target, and never executes git commits, pushes branches, opens PRs, merges, or deploys software**.
- **Sandbox Boundary & Fail-Closed Host Protection:** All code execution (tests, builds, commands) occurs strictly within an isolated Docker container with dropped capabilities (`cap-drop ALL`), `no-new-privileges`, non-root user (`1000:1000`), no network (`--network none`), and resource limits (CPU, memory, process count, timeout). When Docker is unavailable, FORGE strictly returns `SANDBOX_UNAVAILABLE` and refuses to run untrusted code on the host.
- **Anti-Tampering Integrity Check:** Verifies the candidate patch diff content bit-for-bit against the stored SHA256 digest in the plan prior to application.
- **Pure-Python AST-Verifying Patch Applier:** Applies bounded unified diffs in memory/file, verifies context lines, and validates Python syntax via `ast.parse` to catch syntax breaks early.
- **Command Allowlisting & Injection Defense:** Verification commands must use allowlisted executables (`APPROVED_EXECUTABLES`) and are validated against shell metacharacters (semicolons, pipes, subshells, substitutions).
- **Deterministic Outcome Taxonomy:** Evaluates independent pre/post outcomes into `REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `TIMEOUT`, `INCONCLUSIVE`, `FLAKY`, `SANDBOX_UNAVAILABLE`, or `REPAIR_REJECTED`.
- **Persistent Attempt History:** Records every verification attempt in SQLite (`repair_attempts` table) with sequential attempt numbers, timing, exit codes, and secret-scrubbed stdout/stderr excerpts.
- **Mandatory Human Approval Gate:** Human approval remains strictly mandatory (`require_human_approval = True`) before any patch could be exported or applied to an external target.

---

## 2. Architecture & Data Model

### 2.1 Repair Attempt Record (`RepairAttemptRecord`)
Defined in [`app/forge/repair/models.py`](../../app/forge/repair/models.py) and stored in SQLite table `repair_attempts`:

| Field | Type | Description |
|---|---|---|
| `attempt_id` | `str` | Unique deterministic identifier (e.g. `att_<plan_id>_001`) |
| `plan_id` | `str` | Reference to parent `repair_plans(plan_id)` |
| `attempt_number` | `int` | Monotonically increasing attempt number per plan ($\ge 1$) |
| `finding_id` | `str` | Finding identifier being addressed |
| `run_id` | `str` | Associated audit/investigation run ID |
| `base_commit_sha` | `str` | Pinned base commit revision |
| `patch_digest` | `str` | SHA256 digest of applied unified diff |
| `outcome` | `RepairOutcome` | Classified outcome enum |
| `pre_repro_outcome` | `ReproductionOutcome` | Defect reproduction status before patch |
| `post_repro_outcome`| `ReproductionOutcome` | Defect reproduction status after patch |
| `exit_code` | `Optional[int]` | Process exit code in sandbox |
| `duration_ms` | `float` | Verification execution duration |
| `sandbox_available` | `bool` | Whether container isolation was active |
| `oracle_verified` | `bool` | Whether independent test oracle was verified |
| `diagnostic_message`| `str` | Detailed secret-scrubbed explanation |
| `stdout_excerpt` | `str` | Redacted standard output excerpt (max 2000 chars) |
| `stderr_excerpt` | `str` | Redacted standard error excerpt (max 2000 chars) |
| `created_at` | `datetime` | Creation timestamp in UTC |

### 2.2 Outcome Classification Taxonomy (`RepairOutcome`)
Defined in [`app/forge/repair/contracts.py`](../../app/forge/repair/contracts.py):

| Outcome | Exit Code | Description |
|---|---|---|
| `REPAIR_VERIFIED` | 0 | Defect reproduction is resolved (`NOT_REPRODUCED`/`PASSED`) and baseline tests pass |
| `REPAIR_FAILED` | 1 | Target defect remains reproducible after patch application |
| `BROKEN_BUILD` | 1 | Patch caused syntax errors, patch conflict, or harness crash |
| `REGRESSION_DETECTED` | 1 | Baseline verification checks or unrelated tests failed after patch |
| `REPAIR_REJECTED` | 2 | Patch rejected by tampering check, protected path policy, or budget validator |
| `SANDBOX_UNAVAILABLE` | 3 | Container sandbox unavailable; fail-closed without host execution |
| `TIMEOUT` | 4 | Verification execution exceeded allocated timeout quota |
| `INCONCLUSIVE` | 4 | Test oracle is missing or unverified |
| `FLAKY` | 4 | Inconsistent outcomes across retries |

### 2.3 Pure-Python AST-Verifying Patch Applier (`apply_unified_diff_to_file`)
- Parses unified diff `@@ -old,len +new,len @@` hunk headers.
- Verifies exact matching context lines within search window.
- Splices replacement lines in reverse order (bottom-to-top) to preserve offsets.
- Parses AST syntax via `ast.parse` for Python files (`.py`) to reject syntactically broken patches immediately as `BROKEN_BUILD`.

### 2.4 Disposable Workspace Manager (`prepare_disposable_workspace`)
- Copies repository tree excluding `.git`, `.venv`, `__pycache__`, and `forge.sqlite3*`.
- Verifies scratch parent is not equal to or contained within target repo.
- Returns disposable directory and a safe cleanup callback.
- Cleanup callback enforces root and home directory immunity (`temp_resolved not in (Path('/'), Path.home(), target_resolved)`).

---

## 3. Verified CLI Surface

```bash
# Verify a repair plan in an isolated disposable workspace
forge repair verify rpl_find_01_a1b2c3d4_7e3b9a1d --repo /path/to/repo

# Verify a repair plan file with custom scratch parent and specific verification test
forge repair verify plans/plan_find_01.json --repo /path/to/repo --scratch /tmp/forge_scratch --command "pytest tests/test_defect.py"

# Output verification results in machine-readable JSON format
forge repair verify plans/plan_find_01.json --repo /path/to/repo --format json

# List historical verification attempts for a repair plan
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d --format json
```

### CLI Exit Codes:
- `0`: `REPAIR_VERIFIED`
- `1`: `REPAIR_FAILED`, `BROKEN_BUILD`, or `REGRESSION_DETECTED`
- `2`: `REPAIR_REJECTED` or invalid arguments
- `3`: `SANDBOX_UNAVAILABLE` (fail-closed host protection)
- `4`: `TIMEOUT`, `INCONCLUSIVE`, or `FLAKY`

---

## 4. Verification & Testing

The Milestone 2 verification test suite ([`tests/unit/test_phase4_m2_patch_verification.py`](../../tests/unit/test_phase4_m2_patch_verification.py)) confirms all invariants:
- **Patch Application**: Clean diff application, Python AST syntax verification, context mismatch rejection, missing target file rejection.
- **Workspace Isolation**: Bit-for-bit target repository immutability, safe cleanup callback, scratch directory nesting validation.
- **Command Security**: Allowlisting approved binaries, rejecting shell metacharacters and injections.
- **Outcome Classification**: Verified resolutions, regression detection, timeout handling, missing test oracle handling, broken builds.
- **Sandbox Fail-Closed**: Docker absence returns `SANDBOX_UNAVAILABLE` (exit code 3) without executing target code on the host.
- **Integrity**: Patch tampering detection (SHA256 mismatch rejection), protected file immunity (`AGENTS.md`, `.gitignore`, `pyproject.toml`).
- **Persistence & Auditing**: Sequential attempt numbers in SQLite, run event logging (`REPAIR_EVALUATION_STARTED`, `REPAIR_EVALUATION_COMPLETED`, `REPAIR_ROLLBACK_EXECUTED`), secret redaction in command outputs.
- **CLI Commands**: `forge repair verify` (exit codes 0, 1, 2, 3, 4, JSON output) and `forge repair attempts` (table, JSON).
