# FORGE Security Policy

## Supported Versions

FORGE is currently in human-supervised developer beta. Security updates and patches are provided for the active release branch.

| Version | Supported | Status |
| :--- | :---: | :--- |
| `0.1.0b1` (current beta) | Yes | Active Beta |
| `< 0.1.0b1` | No | Historical Milestones |

---

## Reporting a Vulnerability

The FORGE team takes the security of its execution sandboxes, secret redaction, and repository integrity seriously. If you believe you have discovered a security vulnerability in FORGE, please report it responsibly.

### How to Report

**Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.**

Instead, please send an encrypted or direct email to the maintainer:
- **Email:** `thisisanimesh01@gmail.com`
- **Subject:** `[SECURITY] FORGE Vulnerability Report - <Brief Description>`

### Information to Include in Your Report

To help us triage and investigate the issue effectively, please provide:
1. **Description of the vulnerability:** A clear explanation of the flaw, affected components, and potential impact.
2. **Steps to reproduce:** Minimal, self-contained reproduction steps or a proof of concept (PoC).
3. **Environment details:** Operating system, Python version, Docker version, and FORGE version/commit hash.
4. **Threat model relevance:** Whether the vulnerability impacts sandbox containment, secret leakage, target repository immutability, or human approval bypass.
5. **Mitigation suggestions (if known):** Any proposed fixes or defensive checks.

---

## Response & Disclosure Process

- **Acknowledgment:** Reports will be acknowledged on a best-effort basis.
- **Investigation:** The issue will be investigated in an isolated environment to verify impact and formulate a remediation plan.
- **Coordination:** We request that you maintain confidentiality until an official fix is validated and published.
- **Publication:** Once resolved, a security advisory and patched release will be published with attribution to the reporter (unless anonymity is requested).

---

## Security Boundaries & Design Invariants

When evaluating potential vulnerabilities, please note FORGE's foundational security invariants:
- **Docker Sandbox Isolation:** Target code execution must always occur in an isolated Docker container (`--network none`, `--read-only`, dropped capabilities, non-root user). Any behavior allowing untrusted target code to execute directly on the host is considered a Critical security defect.
- **Target Repository Immutability:** FORGE operates strictly as an investigative and analytical intelligence system. Target repositories must never be modified directly on disk.
- **Mandatory Human Approval:** Patch candidates must never self-apply without explicit operator review and authorization.
- **Local-Only Inference:** Cloud fallback is disabled by default (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
