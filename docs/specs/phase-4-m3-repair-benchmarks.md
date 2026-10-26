# Phase 4, Milestone 3: Repair Quality & Regression Benchmarking

- **Milestone:** Phase 4 M3 — Repair Quality & Regression Benchmarking
- **Status:** COMPLETED & VERIFIED
- **Phase 4 Autonomous Execution / Target Mutation:** NOT STARTED / FORBIDDEN
- **Commit Message:** `test(repair): add deterministic repair quality benchmarks`
- **Traceability:** Implements Phase 4 Milestone 3 as defined in `AGENTS.md`, `docs/specs/phase-4-readiness.md`, `docs/specs/phase-4-m1-repair-planning.md`, and `docs/specs/phase-4-m2-patch-verification.md`.

---

## 1. Executive Summary & Objective

Phase 4 Milestone 3 establishes an offline, deterministic evaluation harness and benchmark suite for FORGE's repair planning and verification subsystems. The benchmark evaluates repair plan validity, patch candidate generation and bounds compliance, evidence grounding, regression detection, and outcome classification accuracy across a versioned corpus of representative defect categories and negative control cases.

### Core Principles & Safety Invariants
1. **Audit-Only / Target Repo Immutability**: The benchmark suite executes strictly against disposable synthetic test fixtures. It **never modifies target repository source code, never checks out revisions in the target, and never executes git commits, pushes branches, opens pull requests, merges code, or deploys software**.
2. **Fail-Closed Container Sandboxing**: When Docker or an isolated container backend is unavailable, verification executes under fail-closed skip mode (`SANDBOX_UNAVAILABLE`), preventing any untrusted code execution directly on the host.
3. **Deterministic Offline Execution by Default**: The standard benchmark run executes 100% offline without live network calls or LLM API usage. Live provider execution requires explicit opt-in (`--mode provider --provider <name>`).
4. **No OpenAI API Provider**: As mandated in the provider specification, OpenAI API is unsupported. Supported opt-in providers are `fake`, `ollama` (local), `ollama_cloud`, `gemini`, `deepseek`, and `groq`.
5. **Honest Denominators & Mathematical Integrity**: Metric calculations avoid fabricated 0% or 100% metrics when denominator classes are empty. Undefined rates (e.g. valid plan rate when 0 plans were generated, precision when 0 positive predictions exist) strictly return `None` (`N/A`), maintaining statistical rigor.
6. **Synthetic Scope Disclaimer**: Every generated report (Markdown, JSON, stdout) includes a prominent disclaimer separating synthetic fixture compliance from real-world repair guarantees.

---

## 2. Benchmark Corpus & Ground Truth Taxonomy

The benchmark corpus consists of 11 versioned, synthetic defect fixtures located in [`tests/fixtures/repair_benchmarks/`](file:///Users/animeshyadav/Desktop/project/forge/tests/fixtures/repair_benchmarks/). Each fixture contains a target file, test reproduction oracle (`test_repro.py`), baseline unit test suite (`test_baseline.py`), metadata manifest (`fixture.json`), and validated patch candidate (`patch.diff`).

### 2.1 Representative Fixture Matrix

| Fixture ID | Category | Defect Description | Expected Outcome | Ground Truth Class |
|---|---|---|---|---|
| `REP01_syntax_error` | `syntax_error` | Unterminated string literal in token parser | `REPAIR_VERIFIED` | True Positive |
| `REP02_incorrect_logic` | `incorrect_logic` | Inverted subtraction order in discount calculator | `REPAIR_VERIFIED` | True Positive |
| `REP03_boundary_condition` | `boundary_condition` | Off-by-one `<=` index loop in linear search | `REPAIR_VERIFIED` | True Positive |
| `REP04_missing_validation` | `missing_validation` | Missing negative item count validation | `REPAIR_VERIFIED` | True Positive |
| `REP05_exception_handling` | `exception_handling` | Unhandled `FileNotFoundError` in config reader | `REPAIR_VERIFIED` | True Positive |
| `REP06_security_sensitive` | `security_sensitive` | Directory traversal (`../../`) vulnerability | `REPAIR_VERIFIED` | True Positive |
| `REP07_clean_control` | `clean_control` | Correct utility code with no defect | `REPAIR_REJECTED` | True Negative (Clean Pass) |
| `REP08_ambiguous_finding` | `ambiguous_finding` | Finding lacking verified test oracle | `INCONCLUSIVE` | Inconclusive |
| `REP09_insufficient_evidence` | `insufficient_evidence`| Finding lacking verified reproduction | `REPAIR_REJECTED` | False Negative (Gate Refusal)|
| `REP10_adversarial_instruction`| `adversarial_instruction`| Prompt injection in comments targeting `AGENTS.md` | `REPAIR_VERIFIED` | True Positive (Scoped) |
| `REP11_regression_inducing_patch`| `regression_inducing`| Patch modifying return signature breaking baseline | `REGRESSION_DETECTED` | False Positive (Regression) |

### 2.2 Corpus Containment & Integrity
Fixture discovery is managed by `RepairCorpusLoader` in [`app/forge/benchmarks/repair_corpus.py`](file:///Users/animeshyadav/Desktop/project/forge/app/forge/benchmarks/repair_corpus.py). It enforces strict path containment (`assert_contained_path`), duplicate ID detection, and schema validation via Pydantic model `RepairBenchmarkFixtureMeta`.

---

## 3. Metrics & Statistical Definitions

Metric calculation is implemented in [`app/forge/benchmarks/repair_metrics.py`](file:///Users/animeshyadav/Desktop/project/forge/app/forge/benchmarks/repair_metrics.py).

### 3.1 Metric Equations & Denominators

| Metric Name | Formula | Denominator Condition |
|---|---|---|
| **Outcome Accuracy** | $\frac{\text{Passed Cases}}{\text{Total Cases}}$ | Returns `None` if $\text{Total Cases} = 0$ |
| **Plan Generation Rate** | $\frac{\text{Plans Generated}}{\text{Total Cases}}$ | Returns `None` if $\text{Total Cases} = 0$ |
| **Valid Plan Rate** | $\frac{\text{Valid Plans}}{\text{Plans Generated}}$ | Returns `None` if $\text{Plans Generated} = 0$ |
| **Evidence Grounding Rate** | $\frac{\text{Evidence Grounded}}{\text{Total Cases}}$ | Returns `None` if $\text{Total Cases} = 0$ |
| **Patch Acceptance Rate** | $\frac{\text{Patches Verified}}{\text{Patches Generated}}$ | Returns `None` if $\text{Patches Generated} = 0$ |
| **Scope Compliance Rate** | $\frac{\text{Scope Compliant Patches}}{\text{Patches Generated}}$ | Returns `None` if $\text{Patches Generated} = 0$ |
| **Regression Rate** | $\frac{\text{Regressions Detected}}{\text{Patches Generated}}$ | Returns `None` if $\text{Patches Generated} = 0$ |
| **False-Fix Rate** | $\frac{\text{Failed or Broken Builds}}{\text{Patches Generated}}$ | Returns `None` if $\text{Patches Generated} = 0$ |
| **Average Lines Changed** | $\frac{\sum \text{Lines Changed}}{\text{Patches Generated}}$ | $0.0$ if $\text{Patches Generated} = 0$ |
| **Average Diff Bytes** | $\frac{\sum \text{Diff Bytes}}{\text{Patches Generated}}$ | $0.0$ if $\text{Patches Generated} = 0$ |
| **Precision** | $\frac{\text{TP}}{\text{TP} + \text{FP}}$ | Returns `None` if $\text{TP} + \text{FP} = 0$ |
| **Recall** | $\frac{\text{TP}}{\text{TP} + \text{FN}}$ | Returns `None` if $\text{TP} + \text{FN} = 0$ |
| **F1 Score** | $2 \cdot \frac{\text{Precision} \cdot \text{Recall}}{\text{Precision} + \text{Recall}}$ | Returns `None` if Precision or Recall is `None` |

---

## 4. Benchmark Runner & Isolation Architecture

The evaluation harness in [`app/forge/benchmarks/repair_runner.py`](file:///Users/animeshyadav/Desktop/project/forge/app/forge/benchmarks/repair_runner.py) supports two execution modes:

1. **Offline Mode (`mode="offline"`)**:
   - Uses pre-validated synthetic diff fixtures or scripted responses.
   - Evaluates verification via AST syntax checking, unified diff patching, and simulated pre/post reproduction outcome matrices.
   - Operates deterministically without network access or LLM billing.
2. **Provider Mode (`mode="provider"`)**:
   - Opt-in evaluation requiring explicit `--provider` flag.
   - Generates repair plans via live provider (`RepairPlanner`) and evaluates against the verification harness (`RepairVerifier`).
   - Strictly enforces provider allowlist (`fake`, `ollama`, `ollama_cloud`, `gemini`, `deepseek`, `groq`).

### 4.1 Reporting & Secret Sanitization
Reporting logic in [`app/forge/benchmarks/repair_reporting.py`](file:///Users/animeshyadav/Desktop/project/forge/app/forge/benchmarks/repair_reporting.py) converts run results into sanitized Markdown tables, JSON documents, or Python dictionaries. Every output channel passes through `redact_secrets()` to ensure zero tokens or sensitive values leak into benchmark artifacts.

---

## 5. Verified CLI Usage & Integration

The repair benchmark is accessible via Typer CLI under `forge benchmark repair` and aliased under `forge repair benchmark`.

### 5.1 CLI Command Reference

```bash
# Run full offline repair benchmark suite (Markdown output)
forge benchmark repair

# Run using the repair command alias
forge repair benchmark

# Export benchmark results as JSON
forge benchmark repair --format json

# Filter for a specific fixture category
forge benchmark repair --fixture REP01_syntax_error

# Export benchmark report to file
forge benchmark repair --output reports/repair_benchmark_report.md

# Opt-in live provider evaluation (requires explicit provider flag)
forge benchmark repair --mode provider --provider ollama --model qwen2.5-coder:7b
```

### 5.2 Command Line Options

| Option | Flag | Type | Default | Description |
|---|---|---|---|---|
| `--mode` | `-m` | `str` | `offline` | Evaluation mode (`offline` or `provider`) |
| `--fixture` | `-f` | `str` | `None` | Filter for a specific fixture ID |
| `--provider` | `-p` | `str` | `None` | Opt-in LLM provider override |
| `--model` | | `str` | `None` | LLM model override |
| `--format` | | `str` | `markdown` | Output format (`markdown` or `json`) |
| `--output` | `-o` | `str` | `None` | Optional file path to export benchmark results |

---

## 6. Verification & Test Evidence

### 6.1 Test Suite Coverage
The test suite in [`tests/unit/test_phase4_m3_repair_benchmarks.py`](file:///Users/animeshyadav/Desktop/project/forge/tests/unit/test_phase4_m3_repair_benchmarks.py) exercises 29 focused unit tests across 6 verification areas:
- **Corpus Loading**: Discovery of all 11 fixtures in deterministic alphabetical order, schema validation, single fixture retrieval, and path traversal security.
- **Suite Execution**: Deterministic offline evaluation achieving 100% ground-truth outcome match.
- **Fixture Evaluations**: Individual assertions for all 11 representative fixture categories.
- **Statistical Integrity**: Honest `None` denominators for zero division, precision/recall/F1 edge cases.
- **Reporting & Secret Redaction**: Valid JSON and Markdown formatting, and scrubbing of secret tokens.
- **CLI Subcommands**: Command help, markdown printing, JSON printing, file export, and fail-closed validation.

### 6.2 Full Regression Suite
All unit, integration, and security checks across FORGE pass without failure:
- **M3 Tests**: 29 passed in 1.10s.
- **Full Suite**: 294 passed in 26.96s.
- **Health Checks**: `forge doctor` and `forge sandbox doctor` report system healthy.
