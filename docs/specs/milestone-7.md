# FORGE Milestone 7 Specification: Phase 1 Final Integration and Security Verification

## Status
Accepted

## Context
Milestones 1 through 6 implemented:
1. Foundation, configuration, Pydantic schemas, and CLI diagnostics.
2. Repository validation, safe acquisition, AST parsing, and context budgeting.
3. Evidence records, finding lifecycle state machine, and SQLite event sourcing.
4. Default-deny Docker execution sandbox and strict execution policy.
5. Provider-independent LLM interface, read-only typed tools, and bounded investigation loop.
6. Structured investigation reports, 7 benchmark fixtures, and benchmark evaluation harness.

Milestone 7 represents the final verification, security hardening, and closure of Phase 1 (Audit-Only Repository Intelligence).

---

## 1. Scope & Verification Boundaries

### In Scope
- Comprehensive end-to-end audit verification across all integrated subsystems (Milestones 1–6).
- Verification of zero-mutation invariant: auditing a repository never modifies files, git history, or working tree state.
- Hardening against prompt injection, directory traversal, symlink escapes, and secret leakage.
- Enforcement of fail-secure behavior when container isolation (Docker) is unavailable.
- Ensuring finding lifecycle transitions remain strictly evidence-backed and cannot be fabricated by model assertions or reporting.
- Ensuring event history in SQLite maintains sequential ordering, transaction atomicity, and durable replay.
- Bounded context and resource limits across all public APIs and CLI entry points.

### Explicitly Deferred to Phase 2
- Autonomous patch generation, code modification, or file editing in target repositories.
- Git commits, branch creation, or pull request submission.
- Dynamic multi-agent debate or swarm orchestration.
- Live web dashboards or external telemetry services.

---

## 2. Phase 1 Core Invariants

1. **Audit-Only**: FORGE investigates repositories without mutating target source code, branches, tags, or commits.
2. **Untrusted Input**: All repository files, paths, commit messages, and external inputs are treated as untrusted data.
3. **Fail-Secure Execution**: Sandboxed execution requires container isolation. If Docker is unavailable, runtime execution is cleanly refused/skipped (`ExecutionStatus.UNAVAILABLE` / `CaseOutcome.SKIPPED_UNAVAILABLE`). Host fallback is strictly forbidden.
4. **Evidence-Backed Findings**: Findings require verified evidence records for lifecycle advancement. Model responses cannot directly transition states or fabricate verification.
5. **Secret Redaction**: API keys, access tokens, and sensitive paths are automatically redacted from logs, reports, and events.
6. **Bounded Budgets**: Strict token, character, step, and timeout limits are enforced throughout repository mapping, tool calling, and investigation loops.

---

## 3. End-to-End Verification Matrix

| Area | Verification Method | Status |
|---|---|---|
| Repository Immutability | Pre/post hash comparison during full audit | Verified |
| Prompt Injection Defense | Fixture `BM07` execution under static & LLM modes | Verified |
| Path Traversal Defense | `assert_contained_path` rejects escapes & symlinks | Verified |
| Docker Fail-Closed | Missing Docker causes clean skip, zero host execution | Verified |
| LLM Investigation Loop | Fake provider deterministic bounded execution (<= 10 steps) | Verified |
| Reporting & Redaction | Markdown/JSON generation with active secret masks | Verified |
| Benchmark Integrity | Heuristic static evaluation independent of metadata labels | Verified |

---

## 4. Phase 1 Completion Criteria

- [x] All 7 milestones specified, implemented, and passing regression suites.
- [x] All CLI entrypoints operational (`doctor`, `sandbox doctor`, `inspect`, `investigate`, `report`, `runs`, `findings`, `benchmark`).
- [x] Full regression test suite passing with 0 failures, 0 errors.
- [x] Zero network or paid API requirements for offline test execution.
- [x] Documentation complete (`README.md`, `docs/phase-1-plan.md`, `docs/specs/*`, `docs/decisions/*`).
