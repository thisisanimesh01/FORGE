# Phase 3, Milestone 3: Deterministic Reproduction Planning

**Status:** Implemented and verified
**Branch:** `main`
**Implemented:** Phase 3, Milestone 3

---

## Overview

Milestone 3 adds a typed, deterministic reproduction planning layer that converts calibrated findings and trusted reproduction context into bounded, validated execution plans. Plans are fully typed, verifiable without execution, and fail-closed when expected behaviour cannot be independently sourced.

---

## Modules

### `app/forge/reproduction/planner.py`

The primary planning module. Key types:

| Type | Purpose |
|------|---------|
| `ReproductionStrategy` | Enum: `RUN_SPECIFIC_TEST`, `RUN_SCOPED_TEST_FILE`, `VALIDATE_BOUNDARY_CONDITION`, `CHECK_EXPECTED_OBSERVED`, `INSUFFICIENT_INFORMATION` |
| `PlanProvenance` | Immutable provenance record for each plan field |
| `PlanValidationResult` | Validation outcome with violations list |
| `ReproductionPlan` | Frozen, fully typed plan (strategy + command + expected behaviour + resource limits + provenance) |
| `ReproductionPlanner` | Factory class: `create_plan()` |
| `generate_plan_id()` | Deterministic 12-hex-char SHA-256 plan ID |

**Design invariants:**
- Expected behaviour MUST be independently sourced (TRUSTED_FIXTURE or STATIC_ANALYSIS provenance). LLM assertions and repository descriptions are never accepted as expected behaviour.
- When `trusted_expected_behavior` is `None`, the strategy is forced to `INSUFFICIENT_INFORMATION` regardless of `preferred_strategy`.
- Plans with `INSUFFICIENT_INFORMATION` have `command = None` and cannot be executed.
- Strategy is selected deterministically from command structure (pytest node ID → `RUN_SPECIFIC_TEST`, file-only → `RUN_SCOPED_TEST_FILE`, other → `CHECK_EXPECTED_OBSERVED`).
- Plans are immutable after creation (`ConfigDict(frozen=True)`).
- All plans have `network_approved=False` by default.

### `app/forge/reproduction/validator.py`

Policy validator for `ReproductionPlan` objects. Enforces all AGENTS.md security constraints before execution.

**Rejection criteria:**
1. `INSUFFICIENT_INFORMATION` strategy (not executable)
2. Missing command for executable strategy
3. Unapproved executable (not in `APPROVED_EXECUTABLES` allowlist)
4. Shell interpolation / injection metacharacters in any argument
5. Credential / secret-access argument patterns (`--token`, `--password`, `GITHUB_TOKEN`, etc.)
6. `pip install` / `pip download` / `pip wheel` / `pip uninstall`
7. Git modification commands (`push`, `commit`, `merge`, `rebase`, `reset`, etc.)
8. Absolute paths in `relative_cwd`
9. Path traversal (`../`) in `relative_cwd`
10. Symlink escape (when workspace path is provided)
11. Empty or missing `commit_sha`
12. Timeout outside `[1.0, 300.0]` seconds
13. `network_approved=True`
14. Untrusted `expected_behavior` provenance (when `expected_behavior` is set)

### `app/forge/reproduction/__init__.py` (updated)

Exports all planner and validator symbols.

---

## CLI Commands (`forge plan`)

| Command | Description | Exit Codes |
|---------|-------------|------------|
| `forge plan generate <finding_id>` | Generate a reproduction plan | 0=ok, 2=error |
| `forge plan validate <plan.json>` | Validate a plan (dry run, no execution) | 0=valid, 2=violations |
| `forge plan show <plan.json>` | Display plan details | 0=ok, 2=error |
| `forge plan run <plan.json>` | Validate + execute via harness | 0=reproduced, 1=not reproduced, 2=invalid, 3=blocked, 4=inconclusive |

### CLI Examples

```bash
# Generate a plan without expected behaviour (produces INSUFFICIENT_INFORMATION)
forge plan generate find_abc123 \
    --commit deadbeef1234 \
    --repo https://github.com/example/repo.git

# Generate an executable plan with trusted fixture provenance
forge plan generate find_abc123 \
    --commit deadbeef1234 \
    --repo https://github.com/example/repo.git \
    --command '["pytest", "tests/test_parser.py::test_off_by_one"]' \
    --expected "Loop index must remain < len(arr)" \
    --provenance trusted_fixture \
    --output plan.json

# Validate a plan without executing it
forge plan validate plan.json

# Show plan details
forge plan show plan.json

# Execute a plan (requires Docker; fails closed without it)
forge plan run plan.json --workspace /path/to/workspace
```

---

## Tests

`tests/unit/test_phase3_milestone3.py` — **37 tests, all passing**

Test coverage:
- `ReproductionStrategy` enum completeness (all 5 values present)
- `generate_plan_id()` determinism, stability, format
- `ReproductionPlanner.create_plan()` with valid inputs
- Strategy selection: `RUN_SPECIFIC_TEST` (pytest node ID), `RUN_SCOPED_TEST_FILE` (file-only)
- Preferred strategy override
- Plan immutability (frozen Pydantic model)
- `INSUFFICIENT_INFORMATION` when `trusted_expected_behavior=None`
- `INSUFFICIENT_INFORMATION` overrides `preferred_strategy`
- `ValueError` raised for `UNTRUSTED_LLM` and `UNTRUSTED_REPOSITORY` provenance with expected behaviour
- Provenance recording: `TRUSTED_FIXTURE`, `STATIC_ANALYSIS`, `SYSTEM_POLICY`
- `network_approved=False` by default
- Validator: valid pytest plan accepted
- Validator: valid python3 plan accepted
- Validator: `INSUFFICIENT_INFORMATION` rejected
- Validator: forbidden shell binary rejected
- Validator: shell injection in args rejected
- Validator: `pip install` rejected
- Validator: path traversal in `relative_cwd` rejected
- Validator: `network_approved=True` rejected
- Validator: empty `commit_sha` rejected
- Validator: credential patterns in args rejected
- Validator: absolute `relative_cwd` rejected
- Validator: symlink escape with workspace path
- CLI `forge plan generate` without expected behaviour → `INSUFFICIENT_INFORMATION`
- CLI `forge plan generate` with trusted fixture → `RUN_SPECIFIC_TEST`
- CLI `forge plan generate` with untrusted provenance → exit 2
- CLI `forge plan validate` valid plan → exit 0, "VALID"
- CLI `forge plan validate` insufficient_information → exit 2
- CLI `forge plan show` → human-readable output
- CLI `forge plan run` → blocked (Docker unavailable on this machine), exit 3

---

## Security Properties

| Property | Enforcement |
|----------|-------------|
| No LLM oracle fabrication | `expected_behavior_provenance` checked against `{TRUSTED_FIXTURE, STATIC_ANALYSIS}`; ValueError if untrusted |
| No shell injection | `FORBIDDEN_ARGUMENT_PATTERNS` applied to every argument |
| No credential leakage | `_CREDENTIAL_PATTERNS` scan all arguments |
| No package installation | `pip install` and variants rejected at validator |
| Audit-only (no repo modification) | Git modification subcommands rejected |
| Fail-closed Docker | `ReproductionHarness.execute()` returns `SKIPPED_SANDBOX_UNAVAILABLE`; never falls back to host |
| Bounded execution | Timeout enforced at `[1.0, 300.0]` seconds |
| Network disabled | `network_approved=True` rejected; harness uses `network_enabled=False` |

---

## Deferred Functionality

- **Multi-step plans:** Plans currently specify a single command. Chained or conditional execution is deferred.
- **Plan versioning / migration:** Schema version `1.0.0` is recorded but no migration logic exists.
- **Authenticated registry commands:** `pip install` from private registries is explicitly deferred.
- **Plan result persistence:** `forge plan run` does not persist findings-store evidence atomically with the plan; the `ReproductionCaseManager` provides this for `repro run`.
