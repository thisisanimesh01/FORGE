# FORGE — Autonomous Software Engineering Intelligence

Phase 1: Secure Repository Intelligence.

FORGE is designed to understand software repositories, investigate defects, run isolated baseline checks, and assist software engineering teams without risking host machine integrity or mutating target repositories during audit phases.

## Phase 1 Scope & Status
Phase 1 operates strictly in **Audit-Only** mode. FORGE does not modify source code, create git commits, push branches, or open pull requests.

## Key Features
- **Deterministic Sandboxing**: Executes checks strictly inside isolated execution environments (or skips with clear diagnostic reporting when unconfigured).
- **Python Intelligence**: AST-based symbol indexing, dependency identification, and context budgeting.
- **Typed Evidence & Findings**: Findings are evidence-backed with strict lifecycle tracking (`suspected -> reproduced -> root_caused -> fixed -> verified`).
- **Event-Sourced Runs**: SQLite-backed append-only audit trail.
- **LLM Investigation Loop**: Provider-independent typed LLM interface with bounded multi-turn investigation. Supports offline fake provider and real OpenAI-compatible APIs.
- **Reporting & Benchmark Suite**: Deterministic JSON & Markdown reports, controlled seeded-bug fixtures, and automated benchmark evaluation with precision, recall, and pass rates.
- **Phase 1 Security & Verification** *(Milestone 7)*: Verified zero-mutation invariant, fail-secure Docker degradation, secret redaction, and bounded investigation safety.

## Installation
Ensure Python 3.11+ is installed.
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## CLI Usage
```bash
# System Diagnostics
forge doctor
forge sandbox doctor

# Repository Auditing & Investigation
forge inspect <repo-url-or-path>          # Static inspection
forge investigate <repo-url-or-path>     # LLM-powered investigation
forge investigate <repo> --no-llm        # Offline static inspection only

# Reporting
forge report <run-id>                    # Markdown report to stdout
forge report <run-id> --format json      # JSON report to stdout
forge report <run-id> -o reports/run.md  # Export to file

# Audit History & Findings
forge runs                               # List historical runs
forge findings list                      # List all findings
forge findings show <finding-id>         # Inspect specific finding details & evidence

# Benchmark Suite
forge benchmark list                     # List all available seeded-bug fixtures
forge benchmark run --mode static        # Run offline static benchmark suite
forge benchmark run --mode llm           # Run LLM-assisted benchmark suite
forge benchmark run --case <case-id>     # Run an individual benchmark fixture
forge benchmark run -o results.json      # Export benchmark metrics to JSON
```

## Benchmark Metrics Interpretation
FORGE evaluates benchmark cases by comparing detected findings against expected fixture specifications:
- **True Positive (TP)**: Seeded defect correctly detected in target file/symbol.
- **False Positive (FP)**: Defect falsely claimed on clean control fixture or unaffected files.
- **False Negative (FN)**: Seeded defect missed by the analysis.
- **True Negative (TN)**: Clean control fixture correctly identified with 0 defect findings.
- **Precision**: $\frac{TP}{TP + FP}$ (N/A if no defects reported).
- **Recall**: $\frac{TP}{TP + FN}$ (N/A if no seeded defects present).
- **F1 Score**: Harmonic mean of Precision and Recall.
- **Case Pass Rate**: Percentage of benchmark cases where outcome matched ground truth.

### Static vs. LLM-Assisted Modes
- **`--mode static`**: Runs rule-based AST syntax and pattern analysis. Fast, fully deterministic, offline, and does not require an LLM API key or Docker.
- **`--mode llm`**: Executes multi-turn agent reasoning and tool calling (`read_source_excerpt`, `search_symbols`, `list_directory`). Compatible with `FakeLLMProvider` for offline testing.

## Sandbox & Docker-Unavailable Behavior
When Docker is unavailable on the host, FORGE fails closed:
- Commands requiring isolated container execution are marked `SKIPPED_UNAVAILABLE`.
- Host execution fallback is strictly prohibited.

## Configuration
Copy `.env.example` to `.env` and configure as needed:

```bash
cp .env.example .env
```

Key environment variables:
| Variable | Default | Description |
|---|---|---|
| `FORGE_LLM_PROVIDER` | `fake` | Provider: `fake` (offline) or `openai` (real) |
| `FORGE_LLM_API_KEY` | *(empty)* | API key (never commit) |
| `FORGE_LLM_MODEL` | `gpt-4o` | Model identifier |
| `FORGE_LLM_API_BASE` | *(empty)* | Custom endpoint URL (Ollama, Azure, vLLM) |
| `FORGE_MAX_STEPS` | `10` | Max investigation iterations |

See `docs/specs/phase-1.md` and `docs/phase-1-plan.md` for detailed specifications and architecture.
