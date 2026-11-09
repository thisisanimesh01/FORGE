# Phase 12 Specification: Advanced Intelligence & Autonomous Defect Repair

## Overview

Phase 12 builds upon FORGE's foundational repository understanding (Phase 11) to deliver autonomous, evidence-driven software engineering capabilities. It empowers FORGE to diagnose runtime and test failures, evaluate diagnostic hypotheses against disk facts, perform fault localization, and plan autonomous repairs while upholding core safety and security invariants:
- **Fail-Secure Investigation**: Zero arbitrary code execution on the host machine.
- **Evidence-Grounded Invariant**: A model suspicion never automatically becomes a confirmed defect. Confirmation requires verified disk facts, real line numbers, and concrete failure signals.
- **Strict Typed Contracts**: All inputs, hypotheses, candidate locations, and results are governed by strict Pydantic models with `extra="forbid"`.
- **Zero Cloud Leakage**: LLM interactions strictly target local Ollama endpoints without falling back to cloud services.
- **Boundary & Metadata Safety**: Repository protected files, sensitive secrets, and Git metadata are strictly isolated from analysis inputs.

---

## Milestone Roadmap

- **Milestone 1**: Evidence-Driven Defect Diagnosis `[COMPLETED]`
  - Structured input contracts ingesting user descriptions, test failure reports, error messages, tracebacks, changesets, and static findings with strict character budgets.
  - Deterministic 12-category defect classification taxonomy.
  - Multi-source failure localizer extracting and ranking candidate locations from traceback crash frames, error messages, test callee invocations, target symbols, and change-impact analysis.
  - Hypothesis formulation and scope-confined contradiction detection detecting existing guards and handlers.
  - Strict defect confirmation rules requiring grounded source evidence, verified disk lines, failure signals, and zero unrefuted contradictions.
  - Persistent storage integration in SQLite via `ForgeStore` and `EvidenceGroundingEngine`.
  - Dedicated milestone documentation in [`docs/milestones/phase-12-m1-evidence-driven-diagnosis.md`](../milestones/phase-12-m1-evidence-driven-diagnosis.md).
- **Milestone 2**: Root-Cause Analysis Engine `[COMPLETED]`
  - Backward causal tracing and forward failure propagation over cross-file call and module dependency graphs.
  - AST-based semantic contradiction and guard inspection for conditional guards and exception handlers.
  - Strict certainty semantics: PROVEN, PLAUSIBLE, MULTIPLE_PLAUSIBLE_ROOT_CAUSES, INSUFFICIENT_EVIDENCE, UNRESOLVED.
  - Dedicated milestone documentation in [`docs/milestones/phase-12-m2-root-cause-analysis.md`](../milestones/phase-12-m2-root-cause-analysis.md).
- **Milestone 3**: Repair Planning Engine `[COMPLETED]`
  - Strict Pydantic contracts for `RepairPlan`, `RepairStrategy`, `RepairRiskAssessment`, `VerificationPlan`, `RollbackPlan`.
  - Deterministic strategy generation prioritizing minimal local corrections over architectural refactoring.
  - Multi-candidate strategy preservation and ranking across complexity, invasiveness, and reversibility.
  - Transparent risk analysis assessing file count, dependency reach, public API compatibility, security boundaries, and data integrity.
  - Verification test identification distinguishing on-disk test suites from proposed new tests.
  - Explicit rollback procedures defining reverse steps, triggers, and acknowledged limitations.
  - Hardcoded safety invariants in code: `approval_required = True`, `execution_permitted = False`.
  - ToolRegistry integration exposing 6 bounded, read-only tools.
  - Dedicated milestone documentation in [`docs/milestones/phase-12-m3-repair-planning.md`](../milestones/phase-12-m3-repair-planning.md).
- **Milestone 4**: Controlled Autonomous Repair `[COMPLETED]`
  - Explicit human approval validation bound to plan digest, strategy, and file scope allowlist.
  - Isolated workspace allocation preserving read-only immutability of target repository.
  - Pure in-memory unified diff application with automatic pre-patch backup reversion on context mismatch.
  - Workspace integrity verification and automatic rollback restoration to baseline tree digest.
  - Immutable audit event trail with automatic secret redaction.
  - ToolRegistry integration exposing 8 structured, typed repair execution tools.
- **Milestone 5**: Repair Verification Engine `[COMPLETED]`
  - Bounded test discovery mapping modified source files to targeted, regression, and static check vectors.
  - Fail-closed container sandbox execution with strict binary allowlisting and zero host fallback.
  - Baseline comparison against immutable pre-repair snapshots distinguishing regressions from pre-existing defects.
  - Structured test result parser detecting collection errors, abnormal aborts, and exit code contradictions.
  - Deterministic regression engine categorizing outcomes into NO_REGRESSION_OBSERVED, REGRESSION_DETECTED, PRE_EXISTING_FAILURE, and IMPROVED.
  - Immutable VerificationResult contract enforcing permanent `requires_human_review = True`.
  - ToolRegistry integration exposing 9 structured, typed repair verification tools.
- **Milestone 6**: Repair Quality & Reliability `[COMPLETED]`
  - Multi-dimensional quality evaluation spanning correctness, completeness, maintainability, patch quality, reliability, and security risk.
  - Strict Pydantic contracts with immutable invariants (`requires_human_review = True`), bounded metrics, and digest bindings.
  - AST-based cyclomatic complexity delta, function length changes, code duplication, dead code markers, and public API change detection.
  - Transparent deterministic scoring model with non-overridable score clamping on regressions and critical risks.
  - Bounded SQLite persistence for historical reliability tracking and strategy stability analysis.
  - Fail-closed quality gatekeeper state machine enforcing 10 non-negotiable authorization criteria.
  - ToolRegistry integration exposing 12 structured, typed repair quality tools.
  - Dedicated milestone documentation in [`docs/milestones/phase-12-m6-repair-quality-reliability.md`](../milestones/phase-12-m6-repair-quality-reliability.md).

---

## Milestone 1 Architecture: Evidence-Driven Defect Diagnosis

### 1. Data Contracts & Schemas (`forge.diagnosis.schemas`)

#### Input Contracts (`DiagnosisInput`)
Ingests multi-modal diagnostic signals:
- `user_description`: Optional string bounded to 10,000 characters.
- `test_failure`: Structured `TestFailureReport` containing `test_name`, `test_file`, `failure_message` (clamped to 5,000 chars), `stack_trace` (clamped to 15,000 chars), and `line_number`. Marked with `__test__ = False` to prevent pytest collection conflicts.
- `static_finding`: Structured `StaticFindingReport` containing `rule_id`, `file_path`, `line_number`, `message`, and `severity`.
- `target_files` / `target_symbols`: Explicit focus sets.
- `diff` / `modified_files`: Unified diff (clamped to 50,000 chars) and change set for correlation.
- `error_message` / `stack_trace`: Direct failure strings.
- `expected_behavior` / `actual_behavior`: Observed vs expected behavior strings (each $\le 5,000$ chars).
- Validation: Requires at least one signal; forbids extra inputs (`extra="forbid"`).

#### Output Contracts (`DiagnosisResult` & `DiagnosisHypothesis`)
- `DiagnosisResult`: Structured outcome including `diagnosis_id`, `failing_behavior`, `defect_category`, `severity`, `status` (`CONFIRMED`, `SUSPECTED`, `UNRESOLVED`), `confidence`, `confidence_score` (in `[0.0, 1.0]`), `root_cause_locations`, `hypotheses`, `supporting_evidence_ids`, `contradicting_evidence_ids`, and actionable `verification_requirements`.
- `DiagnosisHypothesis`: Candidate explanation linking a `CandidateLocation`, `DefectCategory`, `confidence_score`, `supporting_evidence_ids`, and `contradicting_evidence_ids`.

---

### 2. Failure Localizer (`forge.diagnosis.localization`)

The `FailureLocalizer` resolves candidate defect locations using:
1. **Traceback Analysis**: Extracts frames matching repository files; scores top crash frames at 0.90 confidence weight.
2. **Error Message Parsing**: Regex-based extraction of `File "...", line X` and `path.py:X`.
3. **Test Callee Correlation**: Inquires `CallGraph` for direct production callees of failing test functions.
4. **Target Symbols Query**: Queries `SymbolIndex` for matching symbol declarations.
5. **Change-Impact Analysis**: Integrates `ChangeImpactAnalyzer` to map modified files and impacted symbols to candidate locations.
6. **Path Containment & Security**: Drops non-repository paths, traversal attempts, and protected paths (`.git/config`, `AGENTS.md`).
7. **Production Precedence**: Test harness files are discounted and sorted behind production source code.

---

### 3. Hypothesis Generation & Contradiction Detection (`forge.diagnosis.hypothesis`)

- **Deterministic Taxonomy Inference**: Maps error patterns to 12 standard defect categories (`LOGIC_ERROR`, `INCORRECT_DATA_HANDLING`, `INPUT_VALIDATION_FAILURE`, etc.).
- **Enclosing Function Contradiction Scope**: Inspects code within the enclosing `def ` boundary. If a candidate claims a missing check/guard, but an explicit check raising an exception exists on disk, a contradicting evidence record is registered and the hypothesis is penalized (-0.40).
- **Disk Grounding Rule**: If candidate lines do not exist on disk, confidence score drops to `0.0`.
- **Local Ollama Integration**: Optionally enriches explanations via local Ollama inference (`localhost:11434`) using structured JSON formats with fail-closed error handling.

---

### 4. Diagnosis Engine (`forge.diagnosis.engine`)

Coordinates the entire pipeline:
1. Dispatches inputs to `FailureLocalizer`.
2. Formulates and grounds hypotheses via `HypothesisEvaluator` and `EvidenceGroundingEngine`.
3. Synthesizes overall status:
   - `CONFIRMED`: Verified location + direct failure signal + direct grounded evidence + zero unrefuted contradictions + no competing unrefuted hypotheses ($\ge 0.70$) + known category + high confidence ($\ge 0.75$).
   - `SUSPECTED`: Plausible hypothesis backed by partial evidence or presence of competing hypotheses.
   - `UNRESOLVED`: Inconclusive, unverified, or missing failure signals.
4. Persists findings and attaches evidence records in `ForgeStore` when enabled.

---

## Milestone 2 Architecture: Root-Cause Analysis Engine

### 1. Causal Data Contracts (`forge.causality.schemas`)
- **Causal Roles**: `ROOT_CAUSE`, `CAUSAL_MECHANISM`, `CONTRIBUTING_FACTOR`, `PROPAGATOR`, `SYMPTOM`.
- **Semantic Relationships**: `CALLS`, `DEPENDS_ON`, `PASSES_INVALID_DATA`, `VIOLATES_PRECONDITION`, `MISUSES_RESOURCE`, `PROPAGATES_EXCEPTION`, `INVALIDATES_STATE`, `TRIGGERS_FAILURE`, `CONTRIBUTES_TO`, `UNKNOWN`.
- **Causal Nodes & Edges**: `CausalNode` and `CausalEdge` tracking disk verification, line bounds, and supporting evidence references.
- **Linear Causal Chains**: `CausalChain` models ordered paths from origin cause to symptom with deterministic confidence scores.
- **Status Classification**:
  - `PROVEN`: All steps in primary chain directly verified with exact source evidence.
  - `PLAUSIBLE`: Plausible chain backed by grounded evidence but containing inferred links.
  - `MULTIPLE_PLAUSIBLE_ROOT_CAUSES`: Multiple distinct candidate causes supported by evidence without differentiating signals.
  - `INSUFFICIENT_EVIDENCE`: Missing, ungrounded, or stale evidence.
  - `UNRESOLVED`: Conflicting or contradictory evidence.

---

### 2. AST Contradiction & Semantic Guard Inspector (`forge.causality.contradiction`)
- Inspects executable Python AST structures within the innermost enclosing function scope.
- Identifies `ast.If` validation guards, `ast.Assert` statements, and `ast.Try`/`ast.ExceptHandler` blocks.
- Disregards comments and string literals, preventing false-positive contradictions from text mentioning validation.

---

### 3. Backward Causal Tracer & Forward Propagation (`forge.causality.tracer`)
- Traverses backward callers via `CallGraph.get_call_edges(target_id=...)` and module dependents via `ModuleDependencyGraph.get_dependents()`.
- Traces downstream effect propagation from suspected root causes to symptoms.
- Enforces maximum depth and node count bounds, guaranteeing cycle safety and recursion termination.
- Distinguishes directly verified call edges (`is_directly_verified = True`, 0.90 confidence) from inferred data/state edges (0.65 confidence).

---

### 4. Root-Cause Analysis Engine (`forge.causality.engine`)
- Ingests verified M1 `DiagnosisResult`, `TestFailureReport`, or direct symbol/location inputs.
- Audits evidence freshness with `verify_evidence_freshness()`.
- Synthesizes linear causal chains and determines root-cause status without forcing a single cause when multiple are plausible.
- Optionally enriches narratives via local Ollama inference, failing closed if unavailable.

---

### 5. Causality Tools & ToolRegistry Integration (`forge.causality.tools`)
- Exposes 5 structured read-only tools to `ToolRegistry`: `analyze_root_cause`, `trace_causal_chain`, `inspect_contributing_factors`, `compare_root_cause_hypotheses`, `explain_failure_propagation`.

---

## Milestone 3 Architecture: Repair Planning Engine

### 1. Data Contracts & Safety Invariants (`forge.repair_planning.schemas`)
- **Strict Pydantic Models**: Governed by `ConfigDict(extra="forbid")` and explicit character budgets.
- **Mandatory Safety Invariants**:
  - `approval_required` is hardcoded to `True` and enforced via `model_validator`.
  - `execution_permitted` is hardcoded to `False` and enforced via `model_validator`.
- **Certainty Gating**:
  - `PROVEN` root causes support `ACTIONABLE` repair plans.
  - `PLAUSIBLE` causes yield `CONDITIONAL` plans with explicit limitations.
  - `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` preserves separate candidate repair plans.
  - `INSUFFICIENT_EVIDENCE` and `UNRESOLVED` causes strictly produce non-actionable plans.

### 2. Evidence Collection & Freshness Validation (`forge.repair_planning.evidence`)
- Gathers grounded evidence from `RootCauseReport`, `DiagnosisResult`, and `ForgeStore`.
- Validates path containment via `assert_contained_path` and rejects protected files via `is_protected_path`.
- Audits line ranges against disk files and compares content SHA-256 digests to detect stale evidence.

### 3. Candidate Strategy Generation (`forge.repair_planning.strategies`)
- Maps defect taxonomy and causal relationships into structured `RepairApproach` categories (`LOCAL_LOGIC_CORRECTION`, `INPUT_VALIDATION`, `ERROR_HANDLING`, `RESOURCE_MANAGEMENT`, `SECURITY_HARDENING`, etc.).
- Preserves alternative approaches while disfavoring broad rewrites to enforce the minimality principle.
- Scores candidates deterministically across complexity, invasiveness, and reversibility.

### 4. Deterministic Risk Assessment (`forge.repair_planning.risk`)
- Evaluates scope (file and symbol counts) and dependency reach (call graph incoming caller count).
- Audits public API stability, security boundaries, data integrity impacts, and automated test coverage.
- Assigns explainable risk ratings (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) and likelihood scores in $[0.0, 1.0]$.

### 5. Verification & Rollback Planning (`forge.repair_planning.rollback` & `planner`)
- Identifies on-disk test files for target modules; explicitly labels proposed new tests with `[PROPOSED]`.
- Formulates exact reverse code modifications, dependency pins, cache invalidations, and rollback triggers.

### 6. Repair Planning Tools (`forge.repair_planning.tools`)
- Exposes 6 typed, read-only tools to `ToolRegistry`: `create_repair_plan`, `compare_repair_strategies`, `assess_repair_risk`, `explain_repair_plan`, `validate_repair_plan`, `generate_verification_plan`.

---

## Milestone 4 Architecture: Controlled Autonomous Repair

### 1. Data Contracts & Enums (`forge.repair_execution.schemas`)
- **Strict Pydantic Contracts**: Governed by `ConfigDict(extra="forbid")` and explicit character/event limits.
- **Key Enums**: `ExecutionStatus`, `ApprovalStatus`, `WorkspaceStatus`, `PatchValidationStatus`, `FailureClassification`, `RollbackStatus`, `RepairEventType`.
- **Core Models**:
  - `RepairApproval`: Frozen contract binding approval ID, plan ID, SHA-256 plan digest, strategy ID, approved file paths allowlist, timestamps, and human approver reference.
  - `RepairWorkspace`: Tracks workspace ID, original repository reference, isolated directory path, baseline commit, baseline tree digest, and workspace status.
  - `RepairPatch`: Structured unified diff representation with added/removed line metrics and canonical SHA-256 patch digest.
  - `RepairEvent`: Immutable audit record capturing event category, actor, action description, related evidence, and tree/patch digest.
  - `RepairExecutionResult`: Complete outcome including execution status, modified files, diff summary, rollback status, failure classification, and `verification_pending = True`.

### 2. Mandatory Human Approval Invariants (`forge.repair_execution.approval`, `ledger`)
- `RepairApprovalValidator` audits non-negotiable authorization rules:
  1. Approval status must be strictly `APPROVED`.
  2. Approval expiry must be in the future (`current_time < approval_expiry`).
  3. `repair_plan_id` must match target plan ID.
  4. SHA-256 digest of normalized plan content (including required change description and rationale) must match approved `plan_digest`.
  5. `approved_strategy_id` must match `plan.selected_strategy_id`.
  6. Plan status must be `ACTIONABLE`.
  7. Supporting causal evidence must be verified and fresh (zero stale evidence).
  8. All affected target files must fall strictly within `approved_file_paths`.
  9. All required changes must fall strictly within `approved_file_paths`.
  10. Non-empty human approver reference is mandatory (rejects synthetic/AI references: 'unknown', 'auto', 'llm', 'agent', 'model', 'bot').
  11. Trusted approver allowlisting: when configured, approver identity must belong to `trusted_approver_identities`.
  12. Single-use consumption & durability: tracked atomically via `ApprovalLedger` (SQLite-backed, thread-locked); concurrent replay attempts are blocked with `UNAUTHORIZED_EXECUTION`.
  13. Revocation tracking: revoked approvals are immediately blocked with `APPROVAL_REVOKED`.

### 3. Isolated Workspace Lifecycle & Protection (`forge.repair_execution.workspace`)
- Target repository is strictly read-only and never modified.
- Workspaces are allocated in isolated directories under `workspace_dir / "repair_workspaces" / <workspace_id>`.
- Uses `_safe_copy_tree()` filtering out FIFO special files, unix domain sockets, character/block devices, and escaping symlinks. Symlinks pointing outside repository root are strictly forbidden.
- Allocates an immutable, write-protected baseline snapshot directory (`<workspace_id>_baseline`, mode 0400 files, 0500 directories) alongside the workspace with collision resilience.
- Calls `purge_protected_paths_from_workspace()` to remove credentials (`.env`, secrets, databases).
- Computes canonical SHA-256 tree digest over all workspace files.

### 4. Controlled In-Memory Patch Application (`forge.repair_execution.patch_applier`, `patch_validator`)
- Pure in-memory Python implementation with zero host shell commands, zero subprocess execution, and zero script interpolation.
- Backs up pre-patch file states in memory.
- `RepairPatchValidator` verifies hunk line counts (`actual_old == old_len`, `actual_new == new_len`), ascending monotonic ordering, absence of overlapping spans, single file occurrences, and rejects dangerous git metadata (`rename`, `old mode`, `new mode`).
- Enforces context-line matching before hunk substitution. Context mismatches immediately abort application and restore in-memory backups.
- Applies hunks in reverse line order to prevent offset disruption.
- Preserves native CRLF line endings and missing trailing EOF newlines.

### 5. Workspace Integrity Verification & Rollback (`forge.repair_execution.integrity`, `rollback`)
- `WorkspaceIntegrityChecker` compares post-repair workspace against the immutable baseline snapshot and approved file allowlist.
- Detects unauthorized file modification, creation, or deletion outside scope, triggering immediate rollback.
- `WorkspaceRollbackManager` verifies that `compute_tree_digest(snap_path)` matches `baseline_tree_digest` before restoration. If corrupted, it **refuses** restoration, marks workspace `CORRUPTED`, and returns `FAILED` to preserve forensic evidence.
- Successful rollback restores original files directly from the baseline snapshot, verifying post-rollback tree digest equals baseline digest.
- Failed rollback transitions workspace to `PRESERVED_FOR_INSPECTION` with `RECOVERY_REQUIRED`.

### 6. Audit Trail & Secret Redaction (`forge.repair_execution.events`)
- Emits sequential `RepairEvent` audit records for all lifecycle stages.
- Forms an append-only SHA-256 cryptographic hash chain verifying event history integrity, supporting `expected_head_hash` checks to detect complete chain rewriting.
- Automatically redacts secrets via `redact_secrets()`. Enforces `MAX_EVENTS_PER_EXECUTION = 200`.

### 7. Repair Execution Tools (`forge.repair_execution.tools`)
- Exposes 8 structured, typed tools to `ToolRegistry`: `prepare_repair_workspace`, `validate_repair_approval`, `generate_repair_patch`, `validate_repair_patch`, `apply_approved_repair`, `inspect_repair_execution`, `rollback_repair_workspace`, `get_repair_execution_history`.

---

## Milestone 5 Architecture: Repair Verification Engine

### 1. Data Contracts & Invariants (`forge.repair_verification.schemas`)
- **Strict Pydantic Contracts**: Governed by `ConfigDict(extra="forbid", validate_assignment=True, frozen=True)`.
- **Mandatory Human Review Invariant**: `requires_human_review = True` is hardcoded and validated via field and model validators. Revalidation is enforced across `model_copy(update=...)`.
- **Integrity Validation**: Rejects `PASSED` verification status whenever required checks fail, regressions are detected, or gate decision is not `PASSED`.
- **Policy Boundaries**: Enforces `network_disabled = True`, execution binary allowlisting (`pytest`, `python`, `python3`, `flake8`, `mypy`), and bounded memory/CPU/PIDs.

### 2. Test Discovery & Selection (`forge.repair_verification.discovery`, `test_selection`)
- Bounded discovery asserting `assert_contained_path` and rejecting escaping symlinks.
- Categorizes tests into `TARGETED` (directly verifying modified files), `REGRESSION` (verifying dependent/caller modules), `STATIC_ANALYSIS` (`py_compile`), and `TYPE_CHECK` (`mypy`).
- Flags `is_partial_verification = True` when unselected tests exist in the repository.

### 3. Baseline Comparison & Deterministic Regression (`forge.repair_verification.baseline`, `regression`)
- Audits baseline snapshot tree digest against expected digest before execution.
- Ephemeral execution workspace allocation for baseline check execution with guaranteed cleanup.
- Deterministic outcome classification into `NO_REGRESSION_OBSERVED`, `REGRESSION_DETECTED`, `PRE_EXISTING_FAILURE`, and `IMPROVED`.

### 4. Container Sandbox Execution (`forge.repair_verification.sandbox`)
- Strict fail-closed execution: if container sandbox is offline or socket permissions denied, returns `SANDBOX_UNAVAILABLE` without host fallback.
- Rejects arbitrary shell strings, metacharacters, and disallowed executable binaries.
- Strips host environment variables, passing only safe minimal variables (`PYTHONPATH`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`).

### 5. Multi-Stage Integrity & Gatekeeper (`forge.repair_verification.integrity`, `gates`)
- Verifies original repository immutability before and after verification.
- Detects unauthorized workspace mutations, escaping symlinks, and special device files (FIFOs, sockets).
- Transition gates require applied execution status, intact baseline, completed required checks, and zero regressions.

### 6. Verification Tools (`forge.repair_verification.tools`)
- Exposes 9 structured, typed tools to `ToolRegistry`: `create_repair_verification_plan`, `discover_repair_tests`, `inspect_verification_environment`, `run_repair_verification`, `run_baseline_verification`, `compare_repair_verification`, `inspect_verification_evidence`, `validate_verification_integrity`, `get_repair_verification_summary`.

---

## Milestone 6 Architecture: Repair Quality & Reliability Engine

### 1. Data Contracts & Invariants (`forge.repair_quality.schemas`)
- **Strict Pydantic Contracts**: Governed by `ConfigDict(extra="forbid", validate_assignment=True, frozen=True)`.
- **Mandatory Safety Invariant**: `requires_human_review = True` is hardcoded with `@field_validator` and `@model_validator` rejecting `False`. Direct assignment raises `ValidationError`, and `model_copy(update=...)` revalidates all invariants through `model_validate()`.
- **Integrity Validation**: Rejects `PASSED` status if `gate_decision != "PASSED"`, `policy_violations` is non-empty, `missing_evidence` is non-empty, `regressions_detected_count > 0`, `risk_level in (CRITICAL, HIGH)`, or historical reliability store is `corrupted` / `unavailable`.
- **Trusted Policy Boundaries**: Enforces minimum weight floor (0.05) and maximum ceiling (0.50) across all 6 mandatory dimensions, normalized total weight (0.95–1.05), bounded thresholds (`min_overall_score >= 0.50`, `min_dimension_score >= 0.30`, `max_scope_expansion_files == 0`), and cryptographic policy digest binding (`policy_digest`) preventing policy substitution attacks.

### 2. Multi-Dimensional Quality Analysis (`forge.repair_quality`)
- **Correctness (`correctness.py`)**: Evaluates root-cause alignment, strategy conformance, and M5 verification outcome. Capped at 0.0 if verification fails or regressions are detected.
- **Completeness (`completeness.py`)**: Audits whether all M3 `RequiredChange` items are modified, distinguishing analyzed context files from mandatory code modifications and penalizing extraneous unapproved file modifications.
- **Maintainability (`maintainability.py`)**: AST-based cyclomatic complexity delta, function length changes, code duplication, dead code markers, and public API change detection.
- **Patch Quality (`patch_analysis.py`)**: Measures patch proportionality, lines added/removed, change concentration, and forbids protected path touches (`.git/`, `AGENTS.md`, credentials).
- **Reliability (`reliability.py`)**: Evaluates M5 baseline regression classification and statistical strategy stability ($N \ge 5$). Genuinely empty history is distinguished from corrupted/unavailable databases; database corruption triggers fail-closed scoring ($\le 0.50$), `INSUFFICIENT_EVIDENCE` status, and gate blocking.
- **Security Risk (`risk.py`)**: Hybrid AST visitor and stateful scanner detecting dangerous calls (`eval`, `exec`, `subprocess.run`, `Popen`, `os.system`, `pickle.loads`), aliased imports, indirect `getattr` calls, disabled TLS (`verify=False`), and live credentials. Prevents false positives by stripping comments and docstrings.

### 3. Transparent Scoring & Gatekeeper (`forge.repair_quality.scoring`, `gates`)
- Composite scoring with configurable dimension weights and fail-closed non-overridable score clamping on regressions ($\le 0.20$), critical risks ($\le 0.20$), high risks ($\le 0.40$), and unverified historical reliability ($\le 0.50$).
- Quality Gatekeeper evaluates non-negotiable criteria before transitioning to `PASSED`, verifying policy digest consistency between scoring and gating.

### 4. Bounded Historical Storage & Events (`forge.repair_quality.history`, `events`)
- Thread-safe SQLite table `repair_quality_records` bounded by record limits (`MAX_HISTORY_RECORDS = 1,000`).
- Database health verification (`check_health()`) using `PRAGMA quick_check` and schema verification. Database failures are explicitly exposed and never reported as zero historical failures.
- Append-only SHA-256 cryptographic audit event chaining with automatic secret redaction.

### 5. Repair Quality Tools (`forge.repair_quality.tools`)
- Exposes 12 structured, typed tools to `ToolRegistry`: `create_repair_quality_request`, `validate_repair_quality_evidence`, `analyze_repair_patch_quality`, `analyze_repair_maintainability`, `analyze_repair_completeness`, `evaluate_repair_correctness`, `analyze_repair_reliability`, `analyze_repair_risk`, `calculate_repair_quality_score`, `evaluate_repair_quality_gates`, `inspect_repair_quality_history`, `get_repair_quality_summary`.
