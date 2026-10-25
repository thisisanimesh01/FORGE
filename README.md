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
- **LLM Investigation Loop** *(Milestone 5)*: Provider-independent typed LLM interface with bounded multi-turn investigation. Supports offline fake provider and real OpenAI-compatible APIs.

## Installation
Ensure Python 3.11+ is installed.
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## CLI Usage
```bash
forge doctor
forge sandbox doctor
forge inspect <repo-url-or-path>
forge investigate <repo-url-or-path>     # LLM-powered investigation
forge investigate <repo> --no-llm        # Static inspection only
forge report <run-id>
forge runs
forge findings list
forge findings show <finding-id>
forge benchmark
```

## Configuration
Copy `.env.example` to `.env` and configure as needed:

```bash
cp .env.example .env
```

Key LLM environment variables:
| Variable | Default | Description |
|---|---|---|
| `FORGE_LLM_PROVIDER` | `fake` | Provider: `fake` (offline) or `openai` (real) |
| `FORGE_LLM_API_KEY` | *(empty)* | API key (never commit) |
| `FORGE_LLM_MODEL` | `gpt-4o` | Model identifier |
| `FORGE_LLM_API_BASE` | *(empty)* | Custom endpoint URL (Ollama, Azure, vLLM) |
| `FORGE_MAX_STEPS` | `10` | Max investigation iterations |

## Offline Mode (No API Key)
FORGE works without an LLM API key via the built-in `FakeLLMProvider`:
```bash
FORGE_LLM_PROVIDER=fake forge investigate .
forge investigate . --no-llm   # Static only
```

See `docs/specs/phase-1.md` and `docs/phase-1-plan.md` for detailed specifications and milestones.
