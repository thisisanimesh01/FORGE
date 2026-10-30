# FORGE Beta: Known Issues & Model Limitations

**Document Version:** 1.0.0-beta.1
**Phase:** Phase 6 (Supervised Beta Packaging & Operational Readiness)
**Evidence Base:** Supervised Pilots 01 through 08

---

## 1. Executive Summary

FORGE has undergone rigorous empirical validation across eight supervised pilot challenges. While the underlying safety framework, container sandbox, AST semantic verifiers, and deterministic patch construction engine operate with 100% fail-closed reliability, model inference capabilities exhibit known boundaries when utilizing compact local models (`qwen2.5-coder:1.5b`).

This document provides transparent, evidence-backed disclosure of observed capabilities, failure patterns, and recommended operator practices.

---

## 2. Empirical Findings from Pilots 01–08

| Defect Class / Pattern | Empirical Pilot Reference | Observed 1.5B Model Behavior | FORGE Engine Defense & Outcome |
| :--- | :--- | :--- | :--- |
| **Arithmetic / Formula Replacement** | Pilot 06 (`GEN07`, Exponential Backoff) | Generated exact, minimal 1-line formula repair (`calculated_delay = self.base_delay * (2 ** attempt)`). | **PASS**: Deterministic coordinate locking spliced clean diff; all Docker tests passed. Workflow reached `awaiting_human_review`. |
| **Division by Zero Guard** | Pilot 01, Attempt 8 (Calculator) | Generated valid conditional zero check. | **PASS**: Verified in Docker sandbox; reached `awaiting_human_review`. |
| **Multi-Statement Branching** | Pilot 07 (`GEN02`, Empty Inventory SKU) | Inverted condition (`len(skus) > 0`), but failed to introduce required sentinel return (`"UNAVAILABLE"`). | **FAIL-CLOSED**: Re-check in Docker caught persistent `IndexError`. Workflow safely transitioned to `verification_failed`. |
| **Statement Type Substitution** | Pilot 08 (`GEN03`, Temperature Evaluation) | Identified `if` statement but stripped 4-space indentation when attempting to replace with `raise ValueError`. | **FAIL-CLOSED**: AST parser caught syntax error; indentation validator trapped 0-space replacement. Workflow transitioned to `repair_failed`. |
| **Validation Guard Bypass** | Pilot 03 / Pilot 04 (`GEN06`, Pricing Discount) | Attempted early return calculating discount, bypassing preceding validation guard (`price < 0`). | **FAIL-CLOSED**: AST semantic verifier detected bypassed validation guard and unreachable statements; candidate rejected. |
| **Diff Header Hallucination** | Pilot 01, Attempts 1–4 (Calculator) | Emitted incorrect unified diff headers and line numbers (`@@ -1,5 +1,5 @@`). | **RESOLVED**: Replaced freeform diff authoring with typed Grounded Repair Representation and `difflib` synthesis. |
| **Wire Schema Key Omission** | Pilot 01, Attempt 5 (Calculator) | LLM grammar engine omitted nullable patch keys on the wire. | **RESOLVED**: Configured wire schema `json_schema_extra` declaring fields required and nullable. |

---

## 3. Detailed Known Issues & Mitigations

### Issue 1: Multi-Statement Control-Flow Synthesis Limitations
- **Description:** When a defect requires simultaneously altering a conditional header AND modifying fallback exit points (such as returning a default sentinel string in an empty collection check), compact local models (1.5B parameters) frequently execute partial edits—such as inverting the `if` check while leaving broken fallback statements intact.
- **Engine Protection:** The candidate patch is tested against the reproduction oracle inside Docker. If the defect is still reproducible, verification fails immediately (`verification_failed`), preventing the patch from reaching human review.
- **Operator Mitigation:** For multi-statement refactorings, operators should utilize larger quantized local models (e.g., `qwen2.5-coder:7b` or `qwen2.5-coder:14b`) or review the diagnostic trace in `forge workflow status <workflow_id>`.

### Issue 2: Indentation Stripping on Statement Type Alteration
- **Description:** When an edit changes the AST node type (e.g., changing a `Return` statement inside an `If` body into an exception `Raise` statement), small local models occasionally output replacement code with 0 leading spaces rather than matching the enclosing block indentation.
- **Engine Protection:** The deterministic repair construction layer measures leading whitespace and verifies structural alignment against the grounded statement. If indentation does not match, or if splicing generates an AST `SyntaxError`, the proposal is rejected before patch creation (`repair_failed`).
- **Operator Mitigation:** Inspect `violations` in the repair plan payload. Ensure defect specifications clearly anchor the exact line needing modification.

### Issue 3: Missing Test Infrastructure in Minimal Target Repositories
- **Description:** Target repositories lacking standard test runners (e.g., missing `pytest` in bare-bones Python containers) cause secondary baseline suite checks to report `exit_code: 127` (`executable file not found`).
- **Engine Protection:** FORGE detects missing tools and records `tool_available: False` without mislabeling environment limitations as code regressions.
- **Operator Mitigation:** Ensure the configured container image (`FORGE_DOCKER_IMAGE`) includes the required testing framework for the target repository.

### Issue 4: Docker Desktop macOS Socket Access & Permissions
- **Description:** On macOS with Docker Desktop, sandboxed applications or subshells may lack permission to access `/Users/<user>/.docker/run/docker.sock`, causing Docker connectivity checks to fail with `permission denied`.
- **Engine Protection:** `forge doctor` and `forge sandbox doctor` catch permission errors deterministically, flagging `[PERMISSION DENIED]` and providing specific guidance rather than generic failure traces. FORGE safely falls back to `READY_FOR_STATIC_INSPECTION_ONLY` without executing target code on the host.
- **Operator Mitigation:** Ensure Docker Desktop is active and verify that "Allow the default Docker socket to be used" is enabled in Docker Desktop > Settings > Advanced.

### Issue 5: Local Ollama Endpoint Disconnect or Unpulled Model Weights
- **Description:** If the Ollama server is stopped (`Connection refused`) or the requested model is not downloaded locally, workflows requesting repair planning fail during inference.
- **Engine Protection:** `forge provider doctor` queries `http://localhost:11434/api/tags` directly, audits the local model inventory, and returns `[MISCONFIGURED]` with the exact `ollama pull <model>` command required. Automatic cloud fallback is strictly blocked.
- **Operator Mitigation:** Run `forge provider doctor` before starting repair workflows to verify local model availability.

### Issue 6: Cross-Platform Byte-for-Byte Bitstream Reproducibility
- **Description:** Due to ZIP and tar archive header timestamps (mtime) and filesystem inode ordering variations across macOS, Linux, and Windows build environments, rebuilding wheel or sdist packages on different host machines may result in different SHA-256 binary bitstreams despite identical member files and source code.
- **Engine Protection:** FORGE enforces strict structural and forensic determinism (identical member list, identical file sizes, zero leaked files), publishes authoritative cryptographic manifests (`release_manifest.json`), and provides SLSA build provenance attestations through GitHub Actions rather than falsely claiming byte-for-byte binary bitstream reproducibility.
- **Operator Mitigation:** Verify downloaded release artifacts against the official `release_manifest.json` or `SHA256SUMS.txt` published with the release, or verify via GitHub CLI (`gh attestation verify`).

---

## 4. Operator Best Practices

1. **Keep Docker Running:** Docker is required for dynamic reproduction and verification. If Docker is offline, FORGE operates strictly in static inspection mode.
2. **Review the Unified Diff:** Always inspect `forge workflow show <workflow_id>` before approving a patch.
3. **Use Focused Test Specifications:** Provide the exact test file and failing command in `forge workflow intake test-spec` to enable deterministic AST grounding.
4. **Deploy Appropriate Model Quantizations:**
   - `qwen2.5-coder:1.5b`: Recommended for fast static triage and single-statement logic/arithmetic corrections.
   - `qwen2.5-coder:7b` / `14b`: Recommended for multi-statement control flow, exception handling, and complex branching.
