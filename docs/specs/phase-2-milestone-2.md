# FORGE Phase 2, Milestone 2: Evidence-Calibrated Review Findings

## Status
Accepted

## Context
Phase 2 Milestone 1 introduced diff-aware intelligent code review (`forge review <target> --base <rev> --head <rev>`). Review findings were generated with basic location and description attributes.

Phase 2 Milestone 2 establishes **evidence calibration** for code review findings. In real-world autonomous software engineering, reviewers (both static tools and LLMs) frequently hallucinate or conflate raw observations with speculation. Milestone 2 enforces a strict tripartite separation (**observed evidence**, **inference**, **recommendation**), deterministic content-addressable finding IDs, evidence verification against actual diff hunks, finding lifecycle enforcement via Phase 1 state machine rules, and calibration downgrades for unbacked claims.

---

## 1. Architecture & Design

### Modules & Enhancements (`app/forge/review/`)

1. **`app/forge/review/evidence.py`**:
   - `EvidenceCalibrationStatus`: Enum containing `VERIFIED`, `INSUFFICIENT`, `MISMATCHED`, `CONTRADICTORY`, and `REJECTED`.
   - `CalibratedEvidence`: Immutable model capturing `evidence_id`, `evidence_type` (`STATIC_ANALYSIS`, `REPRODUCTION_RESULT`, `SOURCE_EXCERPT`, etc.), `status`, `provenance`, `description`, `file_path`, `start_line`, `end_line`, `content_excerpt`, `content_hash` (deterministic SHA-256 digest), and optional `contradiction_reason`.
   - `create_calibrated_evidence()`: Factory ensuring secret redaction, 1-indexed line validation, path normalization, and deterministic content hashing.

2. **`app/forge/review/models.py`** (`REVIEW_SCHEMA_VERSION = "2.0.0"`):
   - `FindingCategory`: Classifies findings into `SECURITY`, `DEFECT`, `LOGIC`, `PERFORMANCE`, `STYLE`, `PROMPT_INJECTION`, or `SECRET_EXPOSURE`.
   - `ReviewFinding`: Updated to require structured tripartite separation:
     - `observed_evidence`: Concrete verifiable facts extracted from diff hunks or code context.
     - `inference`: Deduction explaining why the observed code is defective or risky.
     - `recommendation`: Concrete actionable guidance for fixing or preventing the issue.
     - `calibrated_evidence`: List of `CalibratedEvidence` records supporting the finding.
     - `evidence_status`: Calibration state (`VERIFIED`, `INSUFFICIENT`, `MISMATCHED`, `CONTRADICTORY`).
     - `uncertainty_note`: Explicit record of missing, contradictory, or unverified claims.

3. **`app/forge/review/dedup.py`**:
   - `generate_finding_id()`: Computes deterministic content-addressable IDs (`rev_{rule_id.lower()}_{sha256[:12]}`) based on rule ID, normalized file path, line number, and code snippet. Eliminates random UUIDs across repeated runs.
   - `deduplicate_findings()`: Deterministically deduplicates findings while preserving distinct defects on adjacent or nearby lines.

4. **`app/forge/review/calibration.py`**:
   - `calibrate_finding_evidence()`:
     - If evidence is absent: marks status as `INSUFFICIENT`, downgrades confidence to `LOW`, and attaches an explicit uncertainty note.
     - If evidence file path contradicts the finding's reported path: marks `MISMATCHED`, downgrades confidence to `LOW`.
     - If contradictory evidence exists: marks `CONTRADICTORY`, preserves contradiction reason in `uncertainty_note`, and sets confidence to `LOW`.
   - `enforce_review_lifecycle_transition()`: Reuses Phase 1 finding lifecycle state machine (`validate_transition`) to prevent advancing finding state (e.g. from `SUSPECTED` to `REPRODUCED` or `ROOT_CAUSED`) without corresponding typed evidence records (e.g., `REPRODUCTION_RESULT`). Model assertions alone cannot elevate lifecycle states.

5. **`app/forge/review/reporting.py`**:
   - Formats calibrated reports in JSON and Markdown with dedicated tripartite sections:
     - `### [Category] Title (ID)`
     - **Observed Evidence**, **Inference**, **Recommendation**
     - **Uncertainty Warning** (if status is not `VERIFIED`)
     - **Calibrated Evidence Records** table with evidence IDs, types, provenance, line bounds, and SHA-256 content hashes.

---

## 2. Invariants & Security Rules

1. **Tripartite Separation Invariant**: Every finding must distinguish what was observed from what is inferred and what is recommended.
2. **Deterministic Outputs**: Same diff + same options = byte-identical finding IDs and serialized report structures (excluding execution timestamps).
3. **No Hallucinated Lifecycles**: LLM claims of "confirmed" or "reproduced" defects without verifiable test output remain `SUSPECTED`.
4. **Secret Sanitization**: All exported evidence content excerpts and uncertainty notes pass through `redact_secrets()`.
5. **Read-Only Invariant Maintained**: Review engine and calibration logic perform strictly read-only inspection.
