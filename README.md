# FORGE — Autonomous Software Engineering Intelligence

FORGE is an enterprise-grade autonomous software engineering intelligence platform. It investigates software repositories, reviews code diffs with evidence calibration, evaluates CI/CD security policies, and conducts isolated defect reproduction and verification without compromising host machine integrity or mutating target repositories during audit phases.

---

## Maturity & Capability Boundaries

- **Audit-Only & Immutable**: FORGE strictly investigates repositories. It never modifies target repository source code, creates git commits, pushes branches, opens pull requests, merges code, or deploys software.
- **Untrusted Input Invariant**: All repository contents, documentation, commit messages, PR diffs, and tool outputs are treated as untrusted data.
- **Fail-Secure Container Sandboxing**: Any dynamic execution of repository code occurs strictly within an isolated Docker container with dropped capabilities, read-only root filesystems, non-root user execution, and disabled network access.
- **Refusal of Host Fallback**: When Docker is unavailable, runtime execution is marked `SKIPPED_SANDBOX_UNAVAILABLE`. FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- **Zero Fabrication**: Claims must be backed by concrete, verified evidence. Confidence is automatically downgraded for unbacked assertions.

---

## Phase Roadmap & Status

```
Phase 1: Secure Repository Intelligence  ── [COMPLETED] (Milestones 1–7)
Phase 2: Diff-Aware Intelligent Review   ── [COMPLETED] (Milestones 1–6)
Phase 3: Defect Reproduction & Verification ── [COMPLETED] (Milestones 1–6)
Pre-Phase 4: Local Ollama & Multi-Provider LLM Integration ── [COMPLETED]
Phase 4 Readiness & Sandbox Gate        ── [COMPLETED]
Phase 4: Targeted Autonomous Repair     ── [IN PROGRESS] (Milestones 1–4 Completed)
Phase 5: Regression & Safety Verification ── [PLANNED]
```

### Phase 4: Targeted Autonomous Repair (In Progress)
- **M1 — Evidence-Grounded Repair Planning & Patch Generation (Implemented)**: Converts calibrated findings and verified reproduction evidence into structured, bounded repair plans and candidate unified diffs. Enforces strict evidence grounding (ungrounded findings rejected), prompt-injection mitigation via untrusted context framing, protected file immunity (`AGENTS.md`, `README.md`, `.gitignore`, `pyproject.toml`, `.github/*`), strict size budgeting ($\le 100$ lines, $\le 5$ hunks, $\le 1$ file, $\le 20$ KB), target repository immutability, mandatory human approval gates, and SQLite persistence.
- **M2 — Isolated Patch Application & Verification (Implemented)**: Safely applies validated candidate patches in isolated disposable scratch workspaces and verifies them against independent test reproduction oracles inside the Docker container sandbox boundary. Features a pure-Python AST-verifying diff applier, tamper detection (SHA256 mismatch rejection), command allowlisting and shell injection defense, fail-closed host protection (`SANDBOX_UNAVAILABLE` when Docker is absent without executing code on the host), deterministic outcome taxonomy (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `TIMEOUT`, `INCONCLUSIVE`), SQLite attempt persistence (`repair_attempts`), and CLI commands (`forge repair verify`, `forge repair attempts`).
- **M3 — Repair Quality & Regression Benchmarking (Implemented)**: Deterministic, offline benchmark suite evaluating repair-plan quality, patch validation, and verification classification across 11 representative defect categories (syntax errors, incorrect logic, boundary conditions, missing validation, exception handling, security-sensitive mistakes, clean controls, ambiguous findings, insufficient evidence, adversarial instructions, and regression-inducing patches). Features independent ground truth, honest denominators (`None` / `N/A` for empty classes), secret redaction, and CLI integration (`forge benchmark repair` and `forge repair benchmark`).
- **M4 — Controlled Repair Orchestration & Lifecycle (Implemented)**: Resumable, auditable end-to-end orchestration workflow connecting verified evidence, bounded repair planning, static patch validation, containerized sandbox verification, and a mandatory human review gate. Features a typed state machine with strict transition enforcement (`CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED`), fail-closed container boundaries (`SANDBOX_UNAVAILABLE` when Docker is missing), anti-tampering SHA256 digest validation, stale base revision detection, plan-only dry-run bypass, optimistic concurrency control with monotonic versioning, append-only sequential event auditing with automated secret scrubbing, strict target repository immutability (zero file modifications, no commits/pushes/PRs), and full CLI management (`forge repair workflow start/status/list/resume/review`).
- **M5 — Multi-Candidate Repair Ranking & Export (Planned)**: Multi-candidate ranking, Pareto trade-off assessment, and exportable patch bundle generation.
- **Strict Boundary Notice**: **Automated modification of target repositories, autonomous git commits, branch pushes, pull requests, merges, and deployments remain strictly FORBIDDEN.**

### Phase 4 Readiness & Sandbox Gate (Completed)
- **Container Sandbox & Host Isolation Audit**: Complete audit of container sandbox boundaries (`DockerSandbox`, `ReproductionHarness`). Verified that when Docker is absent (`shutil.which("docker") is None`), FORGE fails closed with `SKIPPED_SANDBOX_UNAVAILABLE` and strictly refuses to execute target repository code directly on the host.
- **Multi-Provider & Cloud Guard Invariants**: Retained 6 supported providers (`fake`, `ollama`, `ollama_cloud`, `gemini`, `deepseek`, `groq`) with complete elimination of OpenAI API dependencies and strict enforcement of `FORGE_ALLOW_CLOUD_FALLBACK=false`.
- **Formal Repair Safety Contract (`forge.repair.contracts`)**: Codified formal safety boundaries required before Phase 4 patch generation can begin:
  - *Target Repository Immutability*: Target repository remains strictly read-only; repairs are strictly isolated to independent scratch workspaces.
  - *Base Revision Pinning*: Repairs are cryptographically pinned to the finding's verified commit SHA (`validate_base_sha`).
  - *Patch Candidate Constraints*: Enforces strict bounds on lines changed (max 100), hunks (max 5), files modified (max 1), and diff bytes (max 20KB).
  - *Scope Containment*: Path traversal, absolute paths, and symlink escapes outside the scratch workspace are strictly rejected via `verify_workspace_containment`.
  - *Pre/Post Outcome Taxonomy*: Deterministic classification (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `SANDBOX_UNAVAILABLE`).
  - *Mandatory Human Approval Gate*: Autonomous git commits, pushes, branch merges, and pull request generation are strictly forbidden (`assert_human_approval_gate`).
- **Phase 4 Autonomous Repair Notice**: **Phase 4 autonomous repair is NOT STARTED**. No automated patch generation, target repository mutation, git commit, push, or deployment has been implemented or enabled.

### Pre-Phase 4: Local Ollama & Multi-Provider LLM Integration (Completed)
- **Local Ollama Runtime**: Default local execution via Ollama (`http://localhost:11434/v1`) with zero cloud credential requirements. Hardware resource analysis (8 GB M1, 13 GiB available disk) establishing suitability of compact coding models (`qwen2.5-coder:1.5b` or `llama3.2:3b`).
- **Ollama Cloud Integration**: Managed inference at `https://ollama.com/v1` via `ollama_cloud` (`ollama-cloud`) requiring `OLLAMA_API_KEY` with default model `gemma4:cloud`. Decoupled from local Ollama to prevent silent routing.
- **Multi-Cloud Integration**: Standard `httpx` OpenAI-compatible support for Groq (`GROQ_API_KEY` / `GROQ_API`), Google Gemini (`GEMINI_API_KEY` / `GEMINI_API`), and DeepSeek (`DEEPSEEK_API_KEY` / `DEEPSEEK_API`) with zero extra SDK dependencies.
- **Fail-Secure Architecture**: Strict cloud fallback prohibition (`FORGE_ALLOW_CLOUD_FALLBACK=false`), bounded transient 5xx retries, typed rate-limit and authentication error mapping, and automated token scrubbing.


### Phase 1: Secure Repository Intelligence (Completed)
- **M1 — Architecture Baseline & Workspace Isolation**: Safe HTTPS/local repository acquisition, path traversal guards, and git hook suppression.
- **M2 — Static Repository Intelligence**: AST symbol indexing, language/framework detection, instruction file extraction, and token-bounded context budgeting.
- **M3 — Evidence Records, State Machine, & Storage**: Structured evidence, finding lifecycle transitions (`suspected -> reproduced -> root_caused -> fixed -> verified`), and SQLite event store.
- **M4 — Secure Container Sandbox Boundary**: Docker adapter with non-root user (`1000:1000`), `--network none`, `--read-only`, dropped capabilities, memory/CPU quotas, and secret redaction.
- **M5 — Bounded Investigation Orchestration**: Multi-turn typed LLM agent with tool calling (`list_dir`, `search`, `read_file`, `get_symbol_index`, `run_check`).
- **M6 — Evidence-Linked Reports & Benchmarking**: Seeded-bug test fixtures, precision/recall evaluation, and JSON/Markdown report generation.
- **M7 — Checkpoint & Comprehensive Verification**: End-to-end security audits, secret scanning, and host immutability verification.

### Phase 2: Diff-Aware Intelligent Review (Completed)
- **M1 — Diff-Aware Code Review Engine**: Read-only Git diff analysis comparing two revisions, accurate hunk and changed-line mapping, security vulnerability and defect detection on modified lines, and automated secret redaction.
- **M2 — Evidence-Calibrated Review Findings**: Tripartite finding structure separating observed facts from inferences and recommendations. Deterministic finding IDs, evidence verification against diff hunks, automatic confidence downgrading for unbacked claims, and strict lifecycle state machine enforcement.
- **M3 — Review Quality Benchmarking**: Versioned benchmark suite evaluating review quality covering genuine security/logic defects, harmless lookalikes, clean controls, attribution challenges, prompt injection, and secret redaction.
- **M4 — Adversarial Review Validation & Generalization**: Genuinely held-out adversarial fixture corpus, resolution of case-level vs finding-level counting units, evaluator self-tests, and defensible regression thresholds.
- **M5 — Security-Sensitive Dataflow & Secret-Exposure Detection**: AST-based security analysis and bounded dataflow tracking. Detects hardcoded credentials in nested dictionaries and Authorization headers, formatted token assembly via string interpolation, and sensitive values leaking into logging, print, or HTTP sinks (`--corpus security`).
- **M6 — CI Review Policies & SARIF Export**: Declarative CI policy enforcement with configurable severity/confidence thresholds, category filtering, auditable scoped rule suppressions, fail-closed evidence handling, and full OASIS SARIF 2.1.0 compliance.

### Phase 3: Defect Reproduction & Verification (Completed)
- **M1 — Sandbox Execution Contracts (Implemented)**: Typed execution contracts (`ReproductionRequest`, `ReproductionCommand`, `ExecutionEnvironmentSpec`, `ReproductionResult`) with strict schema validation, shell injection prevention, approved executable allowlists, resource limits, and fail-closed degradation.
- **M2 — Reproduction Case Management & Evidence Capture (Implemented)**: Deterministic reproduction case tracking, revision pinning, multi-attempt history, bounded/redacted evidence capture, SHA256 digests, field-level provenance, safe retry policy (forbidding automatic non-idempotent retries), resume validation against stale revisions, and lifecycle transition integration.
- **M3 — Deterministic Reproduction Planning (Implemented)**: Typed planning layer converting calibrated findings and trusted context into bounded, validated reproduction plans. Five strategies (`RUN_SPECIFIC_TEST`, `RUN_SCOPED_TEST_FILE`, `VALIDATE_BOUNDARY_CONDITION`, `CHECK_EXPECTED_OBSERVED`, `INSUFFICIENT_INFORMATION`). Deterministic plan IDs, full policy validator (14+ rejection criteria), CLI `forge plan` command group.
- **M4 — Outcome Classification & Evidence Validation (Implemented)**: Strongly typed 8-outcome taxonomy (`REPRODUCED`, `NOT_REPRODUCED`, `UNRELATED_FAILURE`, `ENVIRONMENT_BLOCKED`, `TIMEOUT`, `FLAKY`, `INVALID_PLAN`, `INCONCLUSIVE`) with deterministic reason codes, strict oracle presence and provenance checks (never infer reproduction from generic exit code or model assertion), precedence enforcement, and flaky retry detection.
- **M5 — Reproduction Reliability and Regression Hardening (Implemented)**: Full pipeline hardening against concurrency races, partial write rollback, SQLite unique attempt number constraints, immediate git revision TOCTOU detection, symlink escape checks using Path.is_relative_to, head/tail output bounding preservation with explicit markers, interruption/cleanup failure classification as INCONCLUSIVE, and standardized CLI exit codes (0: reproduced, 1: not reproduced/failed, 2: invalid plan/error, 3: environment blocked, 4: timeout/inconclusive/flaky).
- **M6 — End-to-End Reproduction Acceptance (Implemented)**: Comprehensive 12-scenario acceptance pass verifying end-to-end integration across case management, plan generation, offline validation, oracle provenance, fail-closed host protection, complete 8-outcome classification, SQLite persistence across process restarts, retry caps, finding lifecycle transition gating, standardized CLI exit codes (0–4), Phase 1/2 backward compatibility, and target repository immutability.

---

## Installation

Ensure Python 3.11+ is installed.
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Verified CLI Usage

### System & Sandbox Diagnostics
```bash
# Verify environment, Python, Git, SQLite, and Docker status
forge doctor

# Inspect sandbox runtime health, container configuration, and isolation limits
forge sandbox doctor
```

### Evidence-Grounded Repair Planning & Isolated Verification (Phase 4 Milestones 1–2)
```bash
# Generate a bounded repair plan and candidate patch from an audited finding
forge repair plan find_01 --repo /path/to/repo --commit a1b2c3d4e5f6 --provider ollama

# Generate plan and export full structured JSON artifact
forge repair plan find_01 --output plans/plan_find_01.json --format json

# Inspect an existing repair plan and unified diff patch candidate
forge repair show plans/plan_find_01.json
forge repair show rpl_find_01_a1b2c3d4_7e3b9a1d

# Validate an existing repair plan offline against safety, scope, and budget rules
forge repair validate plans/plan_find_01.json

# List historical repair plans stored in SQLite
forge repair list
forge repair list --finding-id find_01

# Safely apply a candidate patch in an isolated scratch workspace and verify inside sandbox
forge repair verify rpl_find_01_a1b2c3d4_7e3b9a1d --repo /path/to/repo

# Verify a plan JSON file with custom verification command and JSON output
forge repair verify plans/plan_find_01.json --repo /path/to/repo --command "pytest tests/test_parser.py" --format json

# List historical verification attempts for a repair plan
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d --format json
```

### Repair Quality & Regression Benchmarking (Phase 4 Milestone 3)
```bash
# Run full offline repair quality benchmark suite (Markdown report)
forge benchmark repair

# Run using the repair command alias
forge repair benchmark

# Export benchmark results as formatted JSON
forge benchmark repair --format json

# Filter for a specific fixture category
forge benchmark repair --fixture REP01_syntax_error

# Export benchmark report to file
forge benchmark repair --output reports/repair_benchmark_report.md

# Opt-in live provider evaluation (requires explicit provider flag)
forge benchmark repair --mode provider --provider ollama --model qwen2.5-coder:7b
```

### Controlled Repair Orchestration & Human Review Gate (Phase 4 Milestone 4)
```bash
# Start an end-to-end repair workflow for a finding (plan-only dry-run mode)
forge repair workflow start find_001 --repo . --commit a1b2c3d4e5f6 --dry-run

# Start full workflow with sandboxed verification execution
forge repair workflow start find_001 --repo . --commit a1b2c3d4e5f6

# View workflow lifecycle state, candidate diff, and sequential audit trail
forge repair workflow status rwf_find_001_a1b2c3d4_abc123

# Export status report as formatted JSON
forge repair workflow status rwf_find_001_a1b2c3d4_abc123 --format json

# List repair workflows, optionally filtered by state or finding
forge repair workflow list
forge repair workflow list --state awaiting_human_review
forge repair workflow list --finding-id find_001 --format json

# Resume a blocked or paused workflow
forge repair workflow resume rwf_find_001_a1b2c3d4_abc123

# Submit mandatory human review decision (approve or reject)
forge repair workflow review rwf_find_001_a1b2c3d4_abc123 --decision approve --notes "Verified fix against regression suite"
forge repair workflow review rwf_find_001_a1b2c3d4_abc123 --decision reject --notes "Incomplete edge case handling"
```

### Isolated Defect Reproduction Contracts (Phase 3 Milestone 1)
```bash
# Validate reproduction contract schema and security bounds offline
forge sandbox check contracts/reproduce_example.json

# Execute defect reproduction inside an isolated sandbox or report fail-closed skip
forge sandbox reproduce contracts/reproduce_example.json -o reports/repro_result.json
```

### Reproduction Case Management & Evidence Capture (Phase 3 Milestone 2)
```bash
# Create a tracked reproduction case pinned to target commit SHA
forge repro create contracts/auth_defect.json --commit a1b2c3d4e5f6 --max-retries 1

# List reproduction cases (optionally filter by finding ID)
forge repro list
forge repro list --finding-id find_01
forge repro list --format json

# Inspect a reproduction case and its full attempt history
forge repro show case_find_01_7e3b9a1d4c20
forge repro show case_find_01_7e3b9a1d4c20 --format json

# Classify a reproduction case and its attempt history
forge repro classify case_find_01_7e3b9a1d4c20
forge repro classify case_find_01_7e3b9a1d4c20 --format json

# Execute or resume a reproduction case attempt inside the sandbox
forge repro run case_find_01_7e3b9a1d4c20
forge repro run case_find_01_7e3b9a1d4c20 --resume
forge repro run case_find_01_7e3b9a1d4c20 --format json
```

### Deterministic Reproduction Planning (Phase 3 Milestone 3)
```bash
# Generate a bounded reproduction plan for a finding
forge plan generate find_01 \
  --commit a1b2c3d4e5f6 \
  --repo https://github.com/example/repo.git \
  --command '["pytest", "tests/test_parser.py::test_off_by_one"]' \
  --expected "Loop index must remain < len(arr)" \
  --provenance trusted_fixture \
  --output plans/plan_find_01.json

# Validate a plan offline without execution (fail-secure policy check)
forge plan validate plans/plan_find_01.json

# Classify a plan's execution viability and oracle provenance
forge plan classify plans/plan_find_01.json

# Inspect plan metadata, strategy rationale, and provenance
forge plan show plans/plan_find_01.json

# Validate and execute a reproduction plan inside the sandbox
forge plan run plans/plan_find_01.json --workspace /path/to/workspace
```

### Diff-Aware Code Review (Phase 2 Milestones 1, 2, 5, & 6)
```bash
# Review latest commit changes against previous revision
forge review <repo-path> --base HEAD~1 --head HEAD

# Review branch differences
forge review <repo-path> --base main --head feature-branch

# Offline static review without LLM assistance
forge review <repo-path> --base main --head HEAD --no-llm

# Output review as JSON or standards-compliant SARIF 2.1.0
forge review <repo-path> --format json
forge review <repo-path> --format sarif -o reports/review.sarif
```

### CI Review Policy Enforcement (Phase 2 Milestone 6)
```bash
# Evaluate review findings against default CI policy
forge policy check reports/review.json

# Gate CI pipeline using custom declarative policy
forge policy check reports/review.json --policy docs/examples/ci_review_policy.json

# Output machine-readable policy decision for CI/CD pipelines
forge policy check reports/review.json --format json
```

### Review Quality & Security Benchmarks (Phase 2 Milestones 3, 4, & 5)
```bash
# Run standard review quality benchmarks
forge benchmark review

# Run held-out adversarial benchmark corpus
forge benchmark review --corpus heldout

# Run security dataflow benchmark corpus
forge benchmark review --corpus security

# Output benchmark metrics to JSON
forge benchmark review --format json -o reports/review_benchmark.json
```

### Repository Auditing & Investigation (Phase 1)
```bash
# Safely acquire and inspect repository structure (read-only)
forge inspect <repo-url-or-path> --no-llm

# Multi-turn LLM investigation
forge investigate <repo-url-or-path>

# Export audit report
forge report <run-id> --format markdown -o reports/run.md

# Audit history & findings
forge runs
forge findings list
forge findings show <finding-id>

# Phase 1 repository benchmark suite
forge benchmark run --mode static
forge benchmark run --mode llm
```

---

## Benchmark Metrics & Evaluation

FORGE measures detection accuracy against curated ground-truth fixtures:
- **True Positive (TP)**: Target defect correctly detected in affected file and symbol.
- **False Positive (FP)**: Defect falsely claimed on clean control fixtures or unaffected files.
- **False Negative (FN)**: Target defect missed by the analysis.
- **True Negative (TN)**: Clean control fixture correctly identified with 0 defect findings.
- **Precision**: $\frac{TP}{TP + FP}$ (N/A if no defects reported).
- **Recall**: $\frac{TP}{TP + FN}$ (N/A if no seeded defects present).
- **F1 Score**: Harmonic mean of Precision and Recall.
- **Case Pass Rate**: Percentage of benchmark cases where outcome matched ground truth.

---

## Sandboxing & Fail-Closed Security Policy

When Docker is unavailable on the host:
- Execution commands are safely skipped with status `SKIPPED_SANDBOX_UNAVAILABLE`.
- Host execution fallback is strictly prohibited to guarantee host machine safety.
- Clear diagnostics are output detailing how to configure Docker for isolated execution.

---

## Configuration

Copy `.env.example` to `.env` and configure as needed:
```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `FORGE_LLM_PROVIDER` | `ollama` | Provider: `ollama` (default local), `ollama_cloud` (hosted), `groq`, `gemini`, `deepseek`, or `fake` (offline) |
| `FORGE_LLM_MODEL` | *(empty)* | Model identifier override (provider defaults used when empty) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Base URL for local Ollama server (compatible with Friday convention) |
| `OLLAMA_API_KEY` | *(empty)* | API key for Ollama Cloud (`https://ollama.com/v1`) |
| `GROQ_API_KEY` | *(empty)* | API key for Groq Cloud (compatible with Friday convention) |
| `GEMINI_API_KEY` | *(empty)* | API key for Google Gemini (compatible with Friday convention) |
| `DEEPSEEK_API_KEY` | *(empty)* | API key for DeepSeek (compatible with Friday convention) |
| `FORGE_ALLOW_CLOUD_FALLBACK` | `false` | Security guard: silent cloud fallback is strictly forbidden |
| `FORGE_LLM_API_BASE` | *(empty)* | Custom endpoint base URL override (vLLM, custom proxies) |
| `FORGE_MAX_STEPS` | `10` | Max investigation iterations |
| `FORGE_DOCKER_IMAGE` | `python:3.11-slim` | Base container image for sandboxed execution |

---

## Documentation & Architecture References

- Engineering Rules & Mandatory Release Gates: [`AGENTS.md`](file:///Users/animeshyadav/Desktop/project/forge/AGENTS.md)
- Phase 1 Specification: [`docs/specs/phase-1.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-1.md)
- Phase 2 Specifications:
  - Diff Review: [`docs/specs/phase-2-milestone-1.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-1.md)
  - Evidence Calibration: [`docs/specs/phase-2-milestone-2.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-2.md)
  - Review Benchmarks: [`docs/specs/phase-2-milestone-3.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-3.md)
  - Adversarial Generalization: [`docs/specs/phase-2-milestone-4.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-4.md)
  - Security Dataflow Analysis: [`docs/specs/phase-2-milestone-5.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-5.md)
  - CI Review Policy & SARIF: [`docs/specs/phase-2-milestone-6.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-2-milestone-6.md)
- Phase 3 Specifications:
  - Sandbox Execution Contracts: [`docs/specs/phase-3-milestone-1.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-1.md)
  - Case Management & Evidence Capture: [`docs/specs/phase-3-milestone-2.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-2.md)
  - Deterministic Reproduction Planning: [`docs/specs/phase-3-milestone-3.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-3.md)
  - Outcome Classification & Evidence Validation: [`docs/specs/phase-3-milestone-4.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-4.md)
  - Reproduction Reliability & Regression Hardening: [`docs/specs/phase-3-milestone-5.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-5.md)
  - End-to-End Reproduction Acceptance: [`docs/specs/phase-3-milestone-6.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-milestone-6.md)
- Phase 3→4 Transition & Pre-Phase 4:
  - Multi-Provider Configuration & Credential Reuse: [`docs/specs/phase-3-provider-transition.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-3-provider-transition.md)
  - Local Ollama & Multi-Provider LLM Integration: [`docs/specs/pre-phase-4-llm-providers.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/pre-phase-4-llm-providers.md)
  - Ollama Cloud Provider Integration: [`docs/specs/ollama-cloud-provider.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/ollama-cloud-provider.md)
  - Removal of OpenAI API Provider: [`docs/specs/remove-openai-provider.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/remove-openai-provider.md)
  - Phase 4 Readiness & Sandbox Gate: [`docs/specs/phase-4-readiness.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-readiness.md)
- Phase 4 Specifications:
  - Evidence-Grounded Repair Planning & Patch Generation: [`docs/specs/phase-4-m1-repair-planning.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m1-repair-planning.md)
  - Isolated Patch Application & Verification: [`docs/specs/phase-4-m2-patch-verification.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m2-patch-verification.md)
  - Repair Quality & Regression Benchmarking: [`docs/specs/phase-4-m3-repair-benchmarks.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m3-repair-benchmarks.md)
  - Controlled Repair Orchestration & Lifecycle: [`docs/specs/phase-4-m4-repair-orchestration.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m4-repair-orchestration.md)
- Example CI Policy: [`docs/examples/ci_review_policy.json`](file:///Users/animeshyadav/Desktop/project/forge/docs/examples/ci_review_policy.json)
