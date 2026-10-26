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
Phase 4: Targeted Autonomous Repair     ── [NOT STARTED]
Phase 5: Regression & Safety Verification ── [PLANNED]
```

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
- Example CI Policy: [`docs/examples/ci_review_policy.json`](file:///Users/animeshyadav/Desktop/project/forge/docs/examples/ci_review_policy.json)
