# Phase 6, Milestone 2: Packaging, CLI Usability & Distribution

- **Milestone:** Phase 6, Milestone 2 (M2) — Packaging, CLI Usability & Distribution
- **Status:** COMPLETED & VERIFIED
- **Target Repo Immutability:** STRICTLY ENFORCED
- **Traceability:** Implements Phase 6 Milestone 2 per `AGENTS.md` (Sections 1, 2, 3, 4, 5, 7), providing standard Python distribution packaging and standalone CLI usability for the Supervised Developer Beta.

---

## 1. Executive Summary & Objective

Phase 6 Milestone 2 packages FORGE into standard, production-ready Python distribution artifacts (wheel `.whl` and source distribution `.tar.gz`). M2 ensures that human operators can install and use FORGE in any standard Python environment outside the git source repository, while maintaining 100% of FORGE's security invariants, container isolation boundaries, and fail-closed policies.

Prior to M2, executing FORGE required working within an editable development checkout (`pip install -e .`). M2 establishes standardized packaging metadata, strict source inclusion/exclusion rules via `MANIFEST.in`, standalone CLI entrypoints (`forge`), verified shell completion, and automated clean-install verification in fresh virtual environments.

---

## 2. Packaging Architecture & Metadata Configuration

### Build System & Entrypoints (`pyproject.toml`)
FORGE uses PEP 517/PEP 621 compliant packaging standards:
- **Build Backend:** `setuptools.build_meta` with `setuptools>=68.0`.
- **Package Name:** `forge-core`
- **Version:** `0.1.0b1` (PEP 440 beta format).
- **Console Script Entrypoint:** `forge = "forge.cli:app"`
- **Python Compatibility:** `>=3.11`
- **Runtime Dependencies:** Pydantic 2, Pydantic Settings, Typer (with Rich formatting), HTTPX. Zero bloated or unneeded packages.

### Strict Distribution Inclusions & Exclusions (`MANIFEST.in`)
To prevent accidental leakage of sensitive credentials, local database files, test fixtures, or pilot repositories into published distribution archives, `MANIFEST.in` specifies strict inclusion and exclusion policies:
- **Included:** `README.md`, `pyproject.toml`, `.env.example`, core Python modules (`app/forge/**/*.py`), documentation (`docs/**/*.md`).
- **Strictly Excluded:**
  - Local environment secrets: `.env`, `.env.*` (preserving `.env.example`).
  - SQLite databases: `*.sqlite`, `*.sqlite3`, `*.sqlite-wal`, `*.sqlite-shm`, `*.db`, `*.db-journal`.
  - Historical pilot repositories and scratch dirs: `/private/tmp/forge_pilot_*`, `workspace_tmp/`, `.forge/`, `secrets/`, `data/`.
  - Test fixtures: `tests/fixtures/` and pilot reproduction targets.
  - Bytecode and caches: `__pycache__`, `*.pyc`, `.pytest_cache`, `.DS_Store`.

---

## 3. Distribution Artifact Generation & Verification

Artifacts were generated using standard PEP 517 tooling (`build==1.6.1`):
```bash
python -m build
```

Generated artifacts in `dist/`:
1. **Wheel:** `dist/forge_core-0.1.0b1-py3-none-any.whl` (351,931 bytes, 101 files)
2. **Source Distribution:** `dist/forge_core-0.1.0b1.tar.gz` (488,631 bytes, 183 files)

### Artifact Content Forensics
Both the wheel and source distribution archives were systematically inspected:
- **Zero Database Files:** Confirmed 0 `.sqlite` or `.db` files packaged.
- **Zero Credentials:** Confirmed 0 `.env` files packaged (only `.env.example` included).
- **Zero Test Fixtures:** Confirmed no test fixtures or pilot repositories packaged.
- **Pure Python:** Architecture-independent pure-Python wheel (`py3-none-any`).

---

## 4. Clean-Install Verification in Isolated Environments

Clean installation and execution were tested in temporary virtual environments completely isolated from the git source repository:

### Wheel Installation Test (`/tmp/forge_clean_wheel_venv`)
```bash
python3 -m venv /tmp/forge_clean_wheel_venv
PYTHONPATH="" /tmp/forge_clean_wheel_venv/bin/pip install dist/forge_core-0.1.0b1-py3-none-any.whl
```
- **CLI Version:** `forge version` emitted `FORGE version: 0.1.0b1`.
- **Diagnostics:** `forge doctor` executed cleanly, correctly identifying local Python, Docker status, and secret redaction.
- **Database Initialization:** `forge db init` successfully created and initialized an isolated SQLite database.
- **Module Import:** `import forge; print(forge.__version__)` confirmed `0.1.0b1`.

### Source Distribution Test (`/tmp/forge_clean_sdist_venv`)
```bash
python3 -m venv /tmp/forge_clean_sdist_venv
PYTHONPATH="" /tmp/forge_clean_sdist_venv/bin/pip install dist/forge_core-0.1.0b1.tar.gz
```
- **Build from sdist:** Successfully built wheel and installed dependencies.
- **CLI Commands:** Tested `forge version`, `forge --help`, and `forge doctor` from `/tmp`. All commands executed with exit code 0.

---

## 5. CLI Ergonomics & Shell Completion

### Typer Native Completion
The `forge` CLI provides native shell auto-completion for `bash`, `zsh`, `fish`, and `powershell`:
- **Install completion:** `forge --install-completion [bash|zsh|fish|powershell]`
- **Show completion script:** `forge --show-completion [bash|zsh|fish|powershell]`

### Diagnostic & Error Messaging
- Uninitialized database states provide helpful, actionable instructions (`run 'forge db init'`).
- Missing Docker daemon safely warns that runtime sandboxing will fall back to skip mode, explicitly advising that host execution is refused.
- Secret redaction is verified active in `forge doctor` across all output streams.

---

## 6. Security Invariants & Compliance

1. **Target Repository Immutability:** Standalone installation does not alter target repository immutability. No git write permissions are ever required or requested.
2. **Safe Packaging (No Install Hooks):** Standard wheels and pyproject-based builds contain zero executable install hooks or scripts, preventing arbitrary code execution during `pip install`.
3. **Container Isolation Preserved:** The installed CLI invokes Docker with `--network none`, dropped capabilities, read-only root filesystems, and non-root users.
4. **Cloud Fallback Disabled:** `FORGE_ALLOW_CLOUD_FALLBACK=false` remains enforced by default.
5. **No Secrets or Credentials Packaged:** Distribution archives are completely sanitised.
