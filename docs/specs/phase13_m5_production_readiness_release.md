# Phase 13, Milestone 5: Production Readiness & Final Release Specification

## 1. Executive Summary

Phase 13 Milestone 5 marks the final release milestone of the FORGE autonomous software engineering intelligence platform. This milestone consolidates and formally closes the full autonomous engineering lifecycle developed across Phases 1 through 13.

FORGE operates as a strictly local, deterministic, audit-backed software engineering agent designed to:
- **F** — Find bugs
- **O** — Observe and understand code
- **R** — Repair defects
- **G** — Generate and verify tests
- **E** — Engineer pull requests

## 2. Release Gates & Production Invariants

### 2.1 Local Ollama Inference & Cloud Fallback Rejection
- FORGE is engineered for 100% private, on-premises execution using local Ollama model hosting (`deepseek-r1:8b`, `qwen2.5-coder:7b`).
- Cloud fallbacks (e.g. Gemini, Groq, DeepSeek API, OpenAI) are strictly prohibited from silent or automatic selection.
- `allow_cloud_fallback` defaults to `False`. Enabling it triggers an immediate `UNSAFE_CONFIGURATION` critical security warning.
- All outbound HTTP requests enforce SSRF validation, canonical loopback checking, and IP pinning via `forge.llm.url_safety`.

### 2.2 Container Sandbox Boundary & Fail-Closed Host Protection
- Untrusted repository code (builds, tests, scripts) is strictly confined to Docker container sandboxes.
- When Docker is unavailable, runtime execution is marked `SKIPPED_SANDBOX_UNAVAILABLE`.
- FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- Container boundaries enforce dropped capabilities, non-root users, read-only root filesystems, disabled networking, and strict resource limits.

### 2.3 Non-Negotiable Human Approval Boundary
- Autonomous defect repair transitions to `AWAITING_HUMAN_APPROVAL` prior to any repository mutation.
- Human approval is cryptographically bound to:
  - Canonical SHA-256 repair plan digest (`plan_digest`)
  - Target workflow identifier (`workflow_id`)
  - Selected repair strategy ID
  - Bidirectional file scope allowlist (preventing scope breach and scope expansion)
  - Authorized human principal in the configured allowlist (`FORGE_TRUSTED_APPROVERS`)
  - Interactive authentication channel (`interactive_cli`, `trusted_operator`, `cli`)
  - Expiration timestamp and revocation state in SQLite `ApprovalLedger`
- Synthetic, agentic, automated, or LLM-generated identities are rejected fail-closed.

### 2.4 Cryptographic Event Chaining & State Tampering Detection
- All stage transitions, tool calls, and lifecycle events are appended to an immutable SHA-256 hash chain (`prev_event_hash -> event_hash`).
- Direct tampering with SQLite approval records or audit events triggers immediate verification failure.
- Interrupted workflows require explicit operator reconciliation (`forge orchestrate reconcile`) before resuming.

## 3. Package & Build Architecture

- Standardized PEP 517 / PEP 621 build definition in `pyproject.toml`.
- Distribution packages:
  - `dist/forge_core-0.1.0b1-py3-none-any.whl`
  - `dist/forge_core-0.1.0b1.tar.gz`
- Excludes test files, scratch data, logs, and development artifacts from production distributions.
- Multi-tier diagnostic probes:
  - `READY_FOR_FULL_AUTONOMOUS_OPERATIONS`: Local Ollama + Docker running.
  - `READY_FOR_STATIC_INSPECTION_ONLY`: Docker or Ollama unavailable; fail-closed mode active.
  - `UNSAFE_CONFIGURATION`: Insecure options (cloud fallback, disabled sandboxing) detected.

## 4. Verification and Acceptance Results

- **Unit and Security Test Suite**: 2,169 tests passed, 2 skipped across `tests/unit/` and `tests/security/`.
- **Milestone 5 Acceptance Suite**: 13 dedicated end-to-end and security invariant tests in `tests/unit/test_phase13_m5_release_acceptance.py` passed with 100% success rate.
- **Python Compatibility**: Full compatibility across Python 3.11, 3.12, and 3.13.
