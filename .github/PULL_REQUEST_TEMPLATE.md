## Description

Briefly describe the change and its rationale. Reference any related issue (e.g. `Fixes #123`).

## Type of Change

- [ ] Bug fix (non-breaking change fixing an issue)
- [ ] New feature (non-breaking change adding functionality)
- [ ] Breaking change (fix or feature that changes existing contract/behavior)
- [ ] Documentation update
- [ ] Test hardening / Refactoring

## Architectural & Security Invariant Checklist

Please verify that your pull request conforms to FORGE's foundational security rules:

- [ ] **Target Repository Immutability:** No code mutates target repository files on disk, commits, branches, or pushes.
- [ ] **Docker Isolation:** Any execution of repository code executes exclusively within `DockerSandbox` with dropped capabilities and network disabled.
- [ ] **Fail-Closed:** When Docker or required tools are unavailable, checks fail closed or skip safely without host fallback.
- [ ] **Zero Autonomous Modification:** Patches and workflows halt at `AWAITING_HUMAN_REVIEW`.
- [ ] **Secret Redaction:** Logs, events, and reports sanitize sensitive tokens via `redact_secrets()`.
- [ ] **Type Annotations:** Public modules, classes, and functions include complete type annotations.
- [ ] **Tests Included:** Unit and/or integration tests have been added to verify the change.

## Verification Steps

Describe how you tested and verified your changes:
```bash
pytest tests/unit/
```
