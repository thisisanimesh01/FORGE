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
Phase 4: Targeted Autonomous Repair     ── [COMPLETED] (Milestones 1–6)
Phase 5: Supervised End-to-End Engineering Workflow ── [COMPLETED] (Milestones 1–6)
Phase 6: Autonomous Release & Production Hardening  ── [PLANNED]
```

### Phase 5: Supervised End-to-End Engineering Workflow (Completed)
- **Kickoff & Architectural Foundation (Completed)**: Unifies repository acquisition, diff review, defect reproduction, and bounded repair verification into a supervised defect-to-verified-fix workflow. Codifies a 19-state typed state machine (`CREATED` through `AWAITING_HUMAN_REVIEW` to terminal `APPROVED` / `REJECTED`), transactional event ledger, cryptographic revision pinning, fail-closed container boundaries (`SANDBOX_UNAVAILABLE`), honest outcome taxonomy (`NOT_REPRODUCED`, `FLAKY`, `BROKEN_BUILD`, `REGRESSION_DETECTED`), and an exportable certified Engineering Dossier (`patch.diff`, verification proof, SARIF).
- **M1 — Unified Task Intake & Repository Context Pipeline (Implemented & Verified)**: Standardizes defect intake across review findings (`review_finding`), unified git diffs (`unified_diff`), issue problem statements (`issue_description`), and test specifications (`test_specification`) into a validated `WorkflowIntakeSpec`. Cryptographically pins exact 40-character commit SHA (`resolve_and_pin_revision`), intercepts revision drift (`RevisionDriftError`), detects dirty working trees (`DirtyRepositoryError`), and sanitizes affected file paths (rejecting path traversal `..` and absolute paths). Assembles scoped, deterministic repository context (`RepositoryContext`) under explicit character and token budgets (`ContextBudget`, `ContextBudgetSummary`), filtering relevant AST symbols, extracting line-numbered source excerpts, wrapping discovered repository instructions in passive untrusted boundary banners, and providing CLI commands (`forge workflow intake create/validate/show` and `forge workflow context build/show`). Verified with 26 focused unit tests and 713 regression tests.
- **M2 — Supervised Workflow State Machine & Transactional Ledger (Implemented & Verified)**: Delivers a typed, guarded 19-state lifecycle machine with code-enforced transition validation, transactional SQLite storage (`workflow_intakes`, `engineering_workflows`, `engineering_workflow_events`), optimistic concurrency control via monotonic integer versioning (`WorkflowConcurrencyError` on conflicting updates), append-only sequential event auditing with automatic secret redaction, safe crash recovery and unblocking (`REPRO_BLOCKED` and `VERIFICATION_BLOCKED` safely resumed; terminal states strictly reject resumption), and a mandatory human authorization gate (`AWAITING_HUMAN_REVIEW` strictly required before `APPROVED`). Features CLI commands (`forge workflow start`, `forge workflow status`, `forge workflow list`, `forge workflow resume`, `forge workflow review`). Verified with 21 unit and integration tests and 734 full-suite tests.
- **M3 — Automated Evidence Chaining from Reproduction to Repair (Implemented & Verified)**: Connects verified defect reproduction directly into bounded repair planning. Safely derives non-shell reproduction commands from test specifications or issue reproduction hints (`extract_reproduction_command_from_intake`). Validates reproduction evidence integrity against commit revision pinning, finding identity, non-empty/untruncated content, exit code contradictions, and trusted oracle provenance (`validate_reproduction_evidence`). Manages guarded transitions through `reproduce_defect`, `plan_repair`, and sequential `advance_workflow` in `EngineeringWorkflowOrchestrator`, halting on inconclusive or failed outcomes, blocking safely without host fallback on Docker absence (`REPRO_BLOCKED`), and atomically attaching valid candidate patches with optimistic concurrency in SQLite (`attach_repair_plan_to_workflow`). Features CLI commands (`forge workflow repro`, `forge workflow plan-repair`, `forge workflow advance`). Verified with 31 unit tests and 765 full-suite tests.
- **M4 — Multi-Check Regression & Safety Verification (Implemented & Verified)**: Delivers a bounded, isolated multi-check verification pipeline executing across baseline and candidate disposable scratch workspaces inside Docker containers (`MultiCheckVerifier`). Chains reproduction check (defect oracle), baseline test suite (`pytest -q tests/`), static linting (`flake8`/`ruff`), and security analysis (`bandit`), with command allowlisting and shell injection defense (`validate_verification_check_command`). Categorizes outcomes honestly (`PASSED`, `FAILED`, `BLOCKED`, `TIMED_OUT`, `INCONCLUSIVE`, `BASELINE_FAILED`). Distinguishes candidate regressions (`REGRESSION_DETECTED`) from pre-existing baseline failures (`BASELINE_FAILED`). Transitions clean repairs safely through `VERIFIED -> REGRESSION_CHECKING -> AWAITING_HUMAN_REVIEW` while strictly halting for human review before any approval. Persists verification attempts in SQLite `multi_check_verifications`. Features CLI commands (`forge workflow verify` and `forge workflow advance --verify`). Verified with 24 unit and integration tests and 789 full-suite tests.
- **M5 — Certified Engineering Dossier & Operator Export (Implemented & Verified)**: Delivers certified engineering dossier generation and offline operator export for approved workflows. Strictly conditions export on explicit human operator approval (`APPROVED` state with `human_decision == "approve"`), revalidating workflow version, pinned commit SHA, patch digest, evidence provenance, and sequential ledger integrity. Exports a deterministic package: `patch.diff` (unmodified bounded diff $\le 100$ lines, $\le 20$ KB), `verification_certificate.json` (machine-readable proof and delta comparison), `findings.sarif` (standards-compliant SARIF 2.1.0 document), `audit_ledger.json` (complete sequential secret-scrubbed audit trail), `APPLY.md` (dry-run, apply, verification, and rollback runbook), and `manifest.json` (canonical SHA-256 integrity checksums + optional HMAC-SHA256 signature). Enforces path containment, directory safety policy, and offline validation (`validate_engineering_dossier`). Features CLI commands (`forge workflow export` and `forge workflow validate-dossier`). Verified with 40 unit and integration tests and 829 full-suite tests.
- **M6 — End-to-End Workflow Acceptance, Reliability & Release (Implemented & Verified)**: Final acceptance and release audit establishing a 30-scenario deterministic acceptance corpus across 10 reliability domains (intake & revision pinning, context budgeting, adversarial instructions, prompt injection, path traversal, state machine transitions, illegal jump rejection, optimistic concurrency, transactional ledger & secret scrubbing, defect reproduction, non-reproduction fail-closed, flaky oracle handling, reproduction timeouts, protected path rejection, Docker absence fail-closed, sandbox resource limits, network isolation, evidence tamper detection, revision drift interception, duplicate start idempotency, blocked workflow resumption, clean repair verification, baseline regression interception, pre-existing defect honest attribution, graceful degradation for optional linters, mandatory human review gate enforcement, operator rejection, unapproved export refusal, and certified dossier export with SHA-256 manifest and HMAC signing). Features quantitative metrics with honest denominators, automated secret scrubbing, and CLI commands (`forge workflow test` and `forge workflow acceptance`). Verified with 100.0% pass rate (30/30) and 838 full regression tests.
- **Milestone Sequence (Roadmap)**:
  - *M1 — Unified Task Intake & Repository Context Pipeline*: Completed and verified.
  - *M2 — Supervised Workflow State Machine & Transactional Ledger*: Completed and verified.
  - *M3 — Automated Evidence Chaining from Reproduction to Repair*: Completed and verified.
  - *M4 — Multi-Check Regression & Safety Verification*: Completed and verified.
  - *M5 — Certified Engineering Dossier & Operator Export*: Completed and verified.
  - *M6 — End-to-End Workflow Acceptance, Reliability & Release*: Completed and verified.
- **Strict Boundary Notice**: Target repository immutability is strictly enforced. Autonomous git commits, branch pushes, pull requests, merges, and deployments remain strictly forbidden. Automated workflow strictly halts at `AWAITING_HUMAN_REVIEW`. Only explicit operator approval unlocks dossier export.

### Phase 4: Targeted Autonomous Repair (Completed)
- **M1 — Evidence-Grounded Repair Planning & Patch Generation (Implemented)**: Converts calibrated findings and verified reproduction evidence into structured, bounded repair plans and candidate unified diffs. Enforces strict evidence grounding (ungrounded findings rejected), prompt-injection mitigation via untrusted context framing, protected file immunity (`AGENTS.md`, `README.md`, `.gitignore`, `pyproject.toml`, `.github/*`), strict size budgeting ($\le 100$ lines, $\le 5$ hunks, $\le 1$ file, $\le 20$ KB), target repository immutability, mandatory human approval gates, and SQLite persistence.
- **M2 — Isolated Patch Application & Verification (Implemented)**: Safely applies validated candidate patches in isolated disposable scratch workspaces and verifies them against independent test reproduction oracles inside the Docker container sandbox boundary. Features a pure-Python AST-verifying diff applier, tamper detection (SHA256 mismatch rejection), command allowlisting and shell injection defense, fail-closed host protection (`SANDBOX_UNAVAILABLE` when Docker is absent without executing code on the host), deterministic outcome taxonomy (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `TIMEOUT`, `INCONCLUSIVE`), SQLite attempt persistence (`repair_attempts`), and CLI commands (`forge repair verify`, `forge repair attempts`).
- **M3 — Repair Quality & Regression Benchmarking (Implemented)**: Deterministic, offline benchmark suite evaluating repair-plan quality, patch validation, and verification classification across 11 representative defect categories (syntax errors, incorrect logic, boundary conditions, missing validation, exception handling, security-sensitive mistakes, clean controls, ambiguous findings, insufficient evidence, adversarial instructions, and regression-inducing patches). Features independent ground truth, honest denominators (`None` / `N/A` for empty classes), secret redaction, and CLI integration (`forge benchmark repair` and `forge repair benchmark`).
- **M4 — Controlled Repair Orchestration & Lifecycle (Implemented)**: Resumable, auditable end-to-end orchestration workflow connecting verified evidence, bounded repair planning, static patch validation, containerized sandbox verification, and a mandatory human review gate. Features a typed state machine with strict transition enforcement (`CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED`), fail-closed container boundaries (`SANDBOX_UNAVAILABLE` when Docker is missing), anti-tampering SHA256 digest validation, stale base revision detection, plan-only dry-run bypass, optimistic concurrency control with monotonic versioning, append-only sequential event auditing with automated secret scrubbing, strict target repository immutability (zero file modifications, no commits/pushes/PRs), and full CLI management (`forge repair workflow start/status/list/resume/review`).
- **M5 — End-to-End Repair Reliability & Adversarial Testing (Implemented)**: Hardened end-to-end acceptance suite executing 26 deterministic scenarios across 10 failure domains: happy-path repair, evidence integrity gating, budget and protected-file safety, revision drift interception, all 6 verification outcomes, fail-closed container boundaries, provider timeout/malformed/auth resilience, resumability, optimistic concurrency conflict detection, adversarial prompt injection untrusted framing, path traversal rejection, and mandatory human review enforcement. Features quantitative metrics with honest denominators, automated secret scrubbing, and CLI commands (`forge repair test`, `forge repair acceptance`).
- **M6 — Final Acceptance & Release Readiness (Implemented)**: End-to-end audit across M1–M5, validation of real vs. simulated container isolation contracts, 100% pass across all 26 acceptance scenarios and 11 repair quality benchmarks, 100% pass on 686-test regression suite, usability and error-signaling audit, and formal release decision (`CONDITIONALLY READY FOR SUPERVISED USE` due to host Docker dependency for live container execution).
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

### Supervised End-to-End Engineering Workflow: Intake & Context (Phase 5 Milestone 1)
```bash
# 1. Create a task intake specification from an issue description
forge workflow intake create --type issue --repo /path/to/repo --issue-title "Division by zero" --issue-text "divide(10, 0) crash" --affected-file pkg/calculator.py --output intake.json

# 2. Create intake from a specific failing test command
forge workflow intake create --type test --repo /path/to/repo --test-file tests/test_calc.py --test-command "pytest tests/test_calc.py::test_zero" --output intake_test.json

# 3. Create intake from raw unified diff or patch file
forge workflow intake create --type diff --repo /path/to/repo --diff-file changes.diff --output intake_diff.json

# 4. Create intake from an existing review finding in SQLite
forge workflow intake create --type finding --repo /path/to/repo --finding-id find_01 --output intake_finding.json

# 5. Validate an intake specification offline and against repository
forge workflow intake validate intake.json --repo /path/to/repo

# 6. Display intake specification
forge workflow intake show intake.json

# 7. Assemble scoped repository context for a validated intake specification
forge workflow context build intake.json --output context.json

# 8. Assemble context with custom character budget
forge workflow context build intake.json --budget 15000 --output context.json

# 9. Inspect assembled context metadata
forge workflow context show context.json

# 10. Display the rendered markdown prompt ready for model consumption
forge workflow context show context.json --prompt-only
```

### Supervised End-to-End Engineering Workflow: Lifecycle & State Machine (Phase 5 Milestone 2)
```bash
# 1. Start an engineering workflow from a validated task intake specification
forge workflow start --intake intake.json

# 2. Start and auto-advance to INTAKE_VALIDATED and REPO_ANALYZED
forge workflow start --intake intake.json --repo /path/to/repo --auto-advance

# 3. Inspect workflow status and append-only event ledger timeline
forge workflow status wf_eng_38b1f8ac9210
forge workflow status wf_eng_38b1f8ac9210 --format json

# 4. List engineering workflows stored in SQLite
forge workflow list
forge workflow list --state awaiting_human_review
forge workflow list --intake-id intake_001

# 5. Safely resume a blocked or interrupted workflow
forge workflow resume wf_eng_38b1f8ac9210

# 6. Submit mandatory human review decision (approve or reject)
forge workflow review wf_eng_38b1f8ac9210 --decision approve --notes "Verified fix offline"
forge workflow review wf_eng_38b1f8ac9210 --decision reject --notes "Fix violates policy"
```

### Automated Evidence Chaining & Defect Reproduction (Phase 5 Milestone 3)
```bash
# 1. Execute defect reproduction and chain validated evidence into workflow
forge workflow repro wf_eng_38b1f8ac9210

# 2. Execute reproduction with explicit repository path and test command override
forge workflow repro wf_eng_38b1f8ac9210 --repo /path/to/repo --test-command "pytest tests/test_calc.py"

# 3. Plan evidence-grounded repair candidate for a reproduced workflow
forge workflow plan-repair wf_eng_38b1f8ac9210 --repo /path/to/repo

# 4. Plan repair with specific provider, model, and scratch directory
forge workflow plan-repair wf_eng_38b1f8ac9210 --provider ollama --model qwen2.5-coder:7b --scratch-dir /tmp/scratch

# 5. Sequentially advance workflow through intake, context, reproduction, and repair planning
forge workflow advance wf_eng_38b1f8ac9210 --repo /path/to/repo
```

### Multi-Check Regression & Safety Verification (Phase 5 Milestone 4)
```bash
# 1. Execute isolated multi-check verification against repair candidate
forge workflow verify wf_eng_38b1f8ac9210

# 2. Verify with repository override and dedicated scratch directory
forge workflow verify wf_eng_38b1f8ac9210 --repo /path/to/repo --scratch-dir /tmp/scratch

# 3. Verify skipping optional lint or security checks
forge workflow verify wf_eng_38b1f8ac9210 --no-lint --no-security

# 4. Verify with explicit baseline test command override
forge workflow verify wf_eng_38b1f8ac9210 --baseline-test "pytest -q tests/test_core.py"

# 5. Verify and output structured JSON report
forge workflow verify wf_eng_38b1f8ac9210 --format json

# 6. Sequentially advance workflow all the way through multi-check verification to human review gate
forge workflow advance wf_eng_38b1f8ac9210 --repo /path/to/repo --verify
```

### Certified Engineering Dossier & Operator Export (Phase 5 Milestone 5)
```bash
# 1. Export an approved workflow into a certified engineering dossier package
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output

# 2. Export with repository path for HEAD revision drift validation
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --repo /path/to/repo

# 3. Export with cryptographic HMAC-SHA256 signature using an operator key
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --key "operator-secret-token" --key-id "sec-ops-1"

# 4. Export with key loaded securely from file and force overwrite
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --force --key-file /path/to/key.txt

# 5. Export and output machine-readable JSON summary
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --format json

# 6. Validate an exported engineering dossier offline (checks SHA-256 digests and file completeness)
forge workflow validate-dossier ./dossier_output

# 7. Validate an exported engineering dossier with HMAC-SHA256 signature verification
forge workflow validate-dossier ./dossier_output --key "operator-secret-token"
forge workflow validate-dossier ./dossier_output --key-file /path/to/key.txt --format json
```

### End-to-End Workflow Acceptance & Reliability Suite (Phase 5 Milestone 6)
```bash
# 1. Execute the full Phase 5 end-to-end acceptance suite (30 scenarios across 10 domains)
forge workflow test

# 2. Execute acceptance suite and output machine-readable JSON report
forge workflow test --format json

# 3. Export acceptance test report to a markdown or json file
forge workflow test --output report.md
forge workflow test --format json --output report.json

# 4. Filter acceptance suite by specific scenario ID
forge workflow test --scenario P5_ACC01

# 5. Filter acceptance suite by domain category
forge workflow test --category human_review_gate
forge workflow test --category adversarial_intake

# 6. Opt-in to live Docker sandbox execution if available
forge workflow test --docker-live

# 7. Alias command for acceptance suite
forge workflow acceptance
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

### End-to-End Repair Reliability & Adversarial Acceptance Suite (Phase 4 Milestone 5)
```bash
# Execute the full 26-scenario end-to-end acceptance suite (Markdown report)
forge repair test

# Run via acceptance alias
forge repair acceptance

# Run a specific acceptance scenario by ID (e.g., ACC01, ACC15, ACC26)
forge repair test --scenario ACC01

# Export machine-readable JSON acceptance report with automated secret scrubbing
forge repair test --format json --output reports/acceptance_report.json
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

FORGE enforces strict container isolation when running code from target repositories:
- **Network Isolation**: Containers are launched with `--network none`; dynamic calls have zero external network access.
- **Read-Only Filesystem**: The target workspace is mounted read-only (`--mount type=bind,...,readonly`); repository code cannot mutate host files.
- **Non-Root Execution**: Runs strictly as UID:GID `1000:1000`.
- **Privilege Dropping**: Configured with `--security-opt no-new-privileges` and `--cap-drop ALL`.
- **Fail-Closed Host Protection**: When Docker is unavailable, dynamic execution is marked `SKIPPED_SANDBOX_UNAVAILABLE` (or `BLOCKED`). FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- **Real Integration Validation**: Opt-in integration tests verify all isolation boundaries against a live Docker daemon:
  ```bash
  FORGE_DOCKER_TESTS=1 pytest tests/integration/test_docker_sandbox_integration.py
  ```

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
  - End-to-End Repair Reliability & Adversarial Testing: [`docs/specs/phase-4-m5-reliability.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m5-reliability.md)
  - Final Acceptance & Release Readiness: [`docs/specs/phase-4-m6-acceptance.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-4-m6-acceptance.md)
  - Docker Integration Validation & Deterministic Test Repair: [`docs/specs/docker-integration-validation.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/docker-integration-validation.md)
- Phase 5 Specifications:
  - Supervised End-to-End Engineering Workflow Architecture & Roadmap: [`docs/specs/phase-5-roadmap.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-roadmap.md)
  - Unified Task Intake & Repository Context Pipeline: [`docs/specs/phase-5-m1-intake-context.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m1-intake-context.md)
  - Supervised Workflow State Machine & Transactional Ledger: [`docs/specs/phase-5-m2-workflow-ledger.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m2-workflow-ledger.md)
  - Automated Evidence Chaining from Reproduction to Repair: [`docs/specs/phase-5-m3-evidence-chaining.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m3-evidence-chaining.md)
  - Multi-Check Regression & Safety Verification: [`docs/specs/phase-5-m4-multi-check-verification.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m4-multi-check-verification.md)
  - Certified Engineering Dossier & Operator Export: [`docs/specs/phase-5-m5-dossier-export.md`](file:///Users/animeshyadav/Desktop/project/forge/docs/specs/phase-5-m5-dossier-export.md)
- Example CI Policy: [`docs/examples/ci_review_policy.json`](file:///Users/animeshyadav/Desktop/project/forge/docs/examples/ci_review_policy.json)
