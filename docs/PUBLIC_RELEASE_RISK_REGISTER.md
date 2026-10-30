# FORGE Public Release Risk Register

**Document Version:** 1.1.0  
**Phase:** Phase 7, Milestone 1 (Public Readiness Remediation)  
**Date:** October 31, 2026  
**Auditor:** Senior Security Engineer & Release Auditor  
**Repository:** `https://github.com/thisisanimesh01/FORGE`  
**Current HEAD:** `dcf0cd844817810b35899b80bc6a3b59db07087f`

---

## 1. Overview & Risk Assessment Framework

This Risk Register tracks all identified security, operational, architectural, legal, and supply-chain risks associated with transitioning FORGE from a private internal repository to a publicly accessible open-source project.

Risks are categorized using standard OWASP / NIST risk scoring:
- **Severity:** Critical, High, Medium, Low, Informational
- **Likelihood:** High, Medium, Low
- **Impact:** High, Medium, Low
- **Release Blocker:** YES (Must be resolved before public transition) / NO (Operational limitation or deferred improvement) / RESOLVED / CONDITIONAL (Resolved during transition)

---

## 2. Risk Register

| Risk ID | Category | Risk Description | Severity | Likelihood | Impact | Blocker? | Existing Mitigations | Current Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **RISK-01** | Legal / Licensing | Repository lacks an open-source `LICENSE` file and `pyproject.toml` lacks a license identifier. | **HIGH** | High | High | **RESOLVED** | Standard Apache License 2.0 created in root; `license = { text = "Apache-2.0" }` added to `pyproject.toml`. | **CLOSED** |
| **RISK-02** | Supply Chain | Sigstore build provenance attestation cannot be persisted or verified on private user repository (`gh attestation verify` returns HTTP 404). | **MEDIUM** | High | Medium | **CONDITIONAL** | Cryptographic SHA-256 manifests (`release_manifest.json` and `SHA256SUMS.txt`) verify package integrity. | Platform restriction lifts when repository becomes public. Re-run `release-build.yml` post-public transition. |
| **RISK-03** | Community Security | Missing public vulnerability disclosure policy (`SECURITY.md`), creating risk of uncoordinated public zero-day disclosure. | **MEDIUM** | Medium | Medium | **RESOLVED** | Created `SECURITY.md` establishing private reporting channel (`thisisanimesh01@gmail.com`). | **CLOSED** |
| **RISK-04** | Execution Isolation | Malicious target repository executes malicious code during audit or repair. | **CRITICAL (Addressed)** | Low | High | **NO** | Docker container enforces `--network none`, `--read-only`, `--cap-drop ALL`, non-root UID 1000, 60s timeout, memory/PID caps, allowlisted binaries. | Host execution fallback is strictly refused. Keep Docker updated on operator host. |
| **RISK-05** | Target Repository Mutation | Agent autonomously corrupts target repository git history or pushes unapproved changes. | **CRITICAL (Addressed)** | Low | High | **NO** | Target repository workspace is mounted strictly read-only (`type=bind,...,readonly`). Zero git commit/push logic in core application. | Mandatory invariant enforced in code. |
| **RISK-06** | Unauthorized Patch Application | AI agent generates and applies patch without operator review or approval. | **HIGH (Addressed)** | Low | High | **NO** | Workflow halts at `AWAITING_HUMAN_REVIEW`. Transition to `APPROVED` strictly requires explicit human CLI approval (`human_decision == "approve"`). | Mandatory human gate enforced in code. |
| **RISK-07** | Prompt Injection via Target Repo | Adversarial target code contains instructions attempting to hijack LLM behavior. | **HIGH (Addressed)** | High | Low | **NO** | Model tool calling is strictly typed; tools accept structured arguments, never raw shell strings. Outputs parsed through rigid Pydantic schemas. | Multi-check verification re-tests patch in Docker sandbox before presenting to human. |
| **RISK-08** | Secret Exposure | Proprietary API keys or tokens leak into generated reports, logs, or evidence ledgers. | **HIGH (Addressed)** | Low | High | **NO** | `redact_secrets()` intercepts 8 secret patterns and sanitizes to `[REDACTED_SECRET]`. Outbound report sanitization enforced. | Pinned `constraints.txt` and forensic scans exclude `.env` and databases from distribution packages. |
| **RISK-09** | Silent Cloud Exfiltration | Local Ollama failure triggers silent fallback to external commercial cloud LLMs, leaking proprietary code. | **HIGH (Addressed)** | Low | High | **NO** | `FORGE_ALLOW_CLOUD_FALLBACK=false` enforced by default. System fails closed with structured diagnostics on local model failure. | Cloud fallback prohibited by architectural invariant. |
| **RISK-10** | Small Model Synthesis Limits | Small local model (`qwen2.5-coder:1.5b`) generates flawed control flow or invalid syntax for complex defects. | **MEDIUM** | Medium | Low | **NO** | Deterministic edit anchoring validates indentation, AST structure, and scope before disk write; multi-check verification rejects regressions. | Documented in `BETA_KNOWN_ISSUES.md`. Recommend 7B/14B models for complex multi-statement defects. |
| **RISK-11** | Local Machine Path Exposure | Local macOS developer paths (`/Users/animeshyadav/...`) appear in documentation links. | **LOW (Addressed)** | Low | Low | **RESOLVED** | Sanitized 96 absolute link occurrences across 14 Markdown files to relative links. Zero machine paths remain. | **CLOSED** |
| **RISK-12** | Dependency Vulnerabilities | Third-party dependency introduces known vulnerability or supply chain compromise. | **HIGH (Addressed)** | Low | High | **NO** | `pip-audit` confirms 0 known CVEs across all installed runtime and test dependencies. Pinned in `requirements/constraints.txt`. | Scheduled CI audits via `.github/workflows/ci.yml`. |

---

## 3. Residual Risk Summary

```text
Critical Blockers:   0
High Blockers:       0 (RISK-01 resolved by Apache-2.0 adoption)
Medium Blockers:     0 (RISK-02 resolves automatically upon public transition)
Low / Informational: 0
```

### Risk Assessment Conclusion:
All identified critical and high-severity risks have been fully addressed or resolved. With the adoption of the Apache License 2.0 and the creation of community governance documents, FORGE has **zero open blockers** to public repository transition.
