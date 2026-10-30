# Phase 4, Milestone 1: Evidence-Grounded Repair Planning & Patch Generation

**Milestone:** Phase 4 M1 — Evidence-Grounded Repair Planning & Patch Generation  
**Status:** COMPLETED & VERIFIED  
**Phase 4 Autonomous Execution / Target Mutation:** NOT STARTED / FORBIDDEN  
**Commit Message:** `feat(repair): add evidence-grounded repair planning`  
**Traceability:** Implements Phase 4 Milestone 1 as defined in `AGENTS.md` and `docs/specs/phase-4-readiness.md`.

---

## 1. Executive Summary & Objective

Phase 4 Milestone 1 establishes the initial planning layer for targeted defect repair. Building on Phase 3 verified findings and reproduction evidence, Milestone 1 introduces an evidence-grounded repair planner that diagnoses defect mechanisms and proposes strictly bounded unified-diff patch candidates.

**Safety Invariants & Acceptance Boundaries:**
- **Audit-Only / Target Repo Immutability:** M1 strictly generates and validates repair plans as standalone artifacts or SQLite store records. It **never modifies target repository code, applies patches to the target, executes git commits, pushes branches, opens PRs, merges, or deploys software**.
- **Evidence-Grounded Only:** Repair plans are rejected if the cited finding lacks verified supporting evidence (`reproduction_result`, `source_excerpt`, etc.). Plans are never fabricated from descriptions alone.
- **Untrusted Context Framing:** All repository contents, commit messages, and tool outputs are treated as untrusted data within explicit boundary markers to prevent prompt injection attacks.
- **Strict Patch Budget:** Enforces upper limits: $\le 100$ changed lines, $\le 5$ hunks, $\le 1$ modified file, and $\le 20$ KB diff size.
- **Protected File Immunity:** Protected files (`AGENTS.md`, `README.md`, `.gitignore`, `pyproject.toml`, `.github/*`, `.git/*`, `.env*`) are immune to modification.
- **Mandatory Human Approval Gate:** Every plan sets `require_human_approval = True`.

---

## 2. Architecture & Data Model

### 2.1 Repair Plan (`RepairPlan`)
Defined in [`app/forge/repair/models.py`](../../app/forge/repair/models.py):

| Field | Type | Description |
|---|---|---|
| `plan_id` | `str` | Deterministic ID (e.g. `rpl_<finding_id>_<sha[:8]>_<digest[:8]>`) |
| `finding_id` | `str` | Finding identifier being addressed |
| `run_id` | `Optional[str]` | Associated run ID in SQLite database |
| `target_repo` | `str` | Absolute path or URL of target repository |
| `base_commit_sha` | `str` | Pinned base commit revision |
| `cited_evidence_ids` | `List[str]` | Non-empty list of verified evidence IDs |
| `root_cause` | `str` | Structured diagnosis of defect mechanism |
| `proposed_change` | `str` | High-level summary of proposed code change |
| `target_file` | `str` | Relative path of single file targeted for modification |
| `affected_paths` | `List[str]` | Files touched by patch candidate (must match `target_file`) |
| `expected_behavior` | `str` | Correct post-fix behavior |
| `regression_risks` | `List[str]` | Identified regression or compatibility risks |
| `verification_plan` | `str` | Verification command or check for sandbox execution |
| `confidence` | `float` | Model confidence score ($0.0 \dots 1.0$) |
| `require_human_approval`| `bool` | Mandatory human approval gate (`True`) |
| `status` | `RepairPlanStatus` | `VALIDATED`, `REJECTED`, or `BLOCKED` |
| `provider` | `str` | Provider used (`fake`, `ollama`, `groq`, etc.) |
| `model` | `str` | Model identifier used |
| `patch_candidate` | `Optional[PatchCandidate]` | Validated unified diff candidate |
| `validation` | `Optional[PlanValidationResult]` | Detailed validation report with violations |

### 2.2 Model Structured Schema (`RepairModelResponseSchema`)
The model is prompted to return strictly typed JSON:
```json
{
  "root_cause": "Explanation of why the defect occurs",
  "proposed_change": "Summary of proposed code modification",
  "target_file": "src/parser.py",
  "expected_behavior": "Expected behavior after fix",
  "regression_risks": ["Potential side effect or compatibility issue"],
  "verification_plan": "pytest tests/test_parser.py",
  "confidence": 0.95,
  "uncertainty_notes": null,
  "unified_diff": "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1,3 +1,3 @@\n..."
}
```

### 2.3 Protected Path Policy (`ProtectedPathPolicy`)
Immune from automated repair patches:
- System documentation & rules: `AGENTS.md`, `README.md`, `LICENSE`, `NOTICE`.
- Build & dependencies: `pyproject.toml`, `setup.py`, `setup.cfg`.
- Version control & CI: `.git/*`, `.github/*`, `.gitignore`, `.gitattributes`.
- Environment & databases: `.env*`, `forge.sqlite3`, `.venv/*`.

---

## 3. Verified CLI Surface

```bash
# Generate an evidence-grounded repair plan and patch candidate
forge repair plan find_01 --repo /path/to/repo --commit a1b2c3d4e5f6 --provider ollama

# Generate plan and export to JSON artifact
forge repair plan find_01 --output plans/plan_find_01.json --format json

# Display an existing repair plan (from SQLite or JSON file)
forge repair show plans/plan_find_01.json
forge repair show rpl_find_01_a1b2c3d4_7e3b9a1d

# Validate an existing repair plan offline against safety, scope, and budget rules
forge repair validate plans/plan_find_01.json

# List historical repair plans stored in SQLite
forge repair list
forge repair list --finding-id find_01
```

---

## 4. Test Evidence & Offline Verification

All 22 dedicated test cases in `tests/unit/test_phase4_m1_repair_planning.py` pass offline using the deterministic fake LLM provider without network access or live API billing:

| Test Case | Invariant Verified | Outcome |
|---|---|---|
| `test_generate_valid_repair_plan_success` | Valid plan & patch generated and validated | PASSED |
| `test_reject_plan_when_finding_lacks_supporting_evidence` | Ungrounded finding without evidence rejected | PASSED |
| `test_reject_plan_for_nonexistent_finding` | Unknown finding ID rejected cleanly | PASSED |
| `test_reject_plan_modifying_protected_file` | Protected files (7 variants) rejected | PASSED |
| `test_reject_oversized_diff_lines` | Diff exceeding line budget rejected | PASSED |
| `test_reject_diff_modifying_unallowed_file` | Scope escape to unapproved file rejected | PASSED |
| `test_reject_diff_with_path_traversal` | Diff header traversal rejected | PASSED |
| `test_handle_malformed_model_output` | Non-JSON LLM output rejected cleanly | PASSED |
| `test_prompt_injection_in_untrusted_repository_content` | Adversarial injection treated as data | PASSED |
| `test_blocked_status_when_scratch_overlaps_target` | Target/scratch overlap returns `BLOCKED` | PASSED |
| `test_target_repository_immutability_during_planning` | Target files remain 100% bit-for-bit unchanged | PASSED |
| `test_secret_redaction_in_patch_candidate` | Sensitive tokens scrubbed from patch diff | PASSED |
| `test_sqlite_persistence_and_run_events` | SQLite storage and `RunEvents` recorded | PASSED |
| `test_cli_repair_plan_command_success` | CLI `forge repair plan` execution | PASSED |
| `test_cli_repair_show_and_validate` | CLI `forge repair show` and `validate` | PASSED |
| `test_cli_repair_list_empty` | CLI `forge repair list` execution | PASSED |

**Full Regression Test Suite:** 595 passed in 9.56s. Zero regressions across all prior milestones.
