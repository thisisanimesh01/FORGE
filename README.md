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
forge report <run-id>
forge runs
forge findings list
forge benchmark
```


See `docs/specs/phase-1.md` and `docs/phase-1-plan.md` for detailed specifications and milestones.
