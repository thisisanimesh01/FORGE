# Contributing to FORGE

Thank you for your interest in contributing to **FORGE**!

FORGE is an enterprise-grade autonomous software engineering intelligence platform. Because FORGE audits, reproduces, and repairs code within secure boundaries, all contributions must uphold strict architectural and security standards.

---

## Code of Conduct

All contributors and participants are expected to adhere to the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Development Environment Setup

### Prerequisites
- **Python:** 3.11, 3.12, or 3.13
- **Git:** 2.30+
- **Docker Engine / Desktop:** 20.10+ (required for sandboxed reproduction and verification)
- **Local Ollama Server:** Running at `http://localhost:11434` with `qwen2.5-coder:1.5b` (optional for local inference testing)

### Installation from Source
1. Fork and clone the repository:
   ```bash
   git clone https://github.com/<your-username>/FORGE.git
   cd FORGE
   ```
2. Create and activate a clean virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Install development dependencies using pinned constraints:
   ```bash
   pip install --upgrade pip
   pip install -e ".[dev]" -c requirements/constraints.txt
   ```
4. Initialize the development database:
   ```bash
   cp .env.example .env
   forge db init
   ```

---

## Core Engineering Invariants

Before writing code, please review [`AGENTS.md`](AGENTS.md) and [`docs/BETA_CONTRACT.md`](docs/BETA_CONTRACT.md). Any pull request violating the following invariants will be rejected:

1. **Target Repository Immutability:** Never modify target repository files on disk, create git commits, push branches, or merge pull requests autonomously.
2. **Fail-Closed Container Sandboxing:** Target code (tests, reproduction scripts, linters) must execute strictly within an isolated Docker container (`--network none`, `--read-only`, non-root user, dropped capabilities). Never introduce host-execution fallbacks.
3. **Mandatory Human Review Gate:** Workflows producing patch proposals must halt at `AWAITING_HUMAN_REVIEW`. Only explicit human approval may permit patch export.
4. **Local-Only Inference by Default:** Automatic cloud fallback is strictly forbidden (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
5. **No Secrets in Code or Logs:** Never commit credentials, tokens, or private paths. Secret redaction (`redact_secrets()`) is mandatory across all logging and reporting.

---

## Running Verification & Tests

### Offline Regression Test Suite
Run the full unit and security test suite:
```bash
pytest tests/unit/
```

### Dependency Security Audit
Verify dependencies against known vulnerabilities:
```bash
pip-audit --no-deps --disable-pip -r requirements/constraints.txt
```

### Bytecode Compilation Check
Ensure all modules compile cleanly across supported Python versions:
```bash
python -m compileall app/forge
```

### Live Docker Integration Tests (Optional)
If Docker is running locally, execute live container integration tests:
```bash
pytest tests/integration/test_docker_sandbox_integration.py
```

---

## Pull Request Guidelines

1. **Focused Changes:** Keep pull requests focused on a single feature, bug fix, or documentation improvement.
2. **Test Coverage:** Every bug fix or new capability must include corresponding unit tests under `tests/unit/`.
3. **Type Annotations:** All public modules, functions, and classes must have complete type annotations.
4. **Conventional Commits:** Use conventional commit messages (e.g., `feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
5. **Clean Working Tree:** Ensure zero untracked files, debug logs, or temporary database files are committed.
