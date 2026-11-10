# FORGE — Autonomous Software Engineering Intelligence

FORGE is an enterprise-grade autonomous software engineering intelligence platform. It investigates software repositories, reviews code diffs with evidence calibration, evaluates CI/CD security policies, and conducts isolated defect reproduction and verification without compromising host machine integrity or mutating target repositories during audit phases.

---

## Maturity & Capability Boundaries

- **Audit-Only & Immutable**: FORGE strictly investigates repositories. It never modifies target repository source code, creates git commits, pushes branches, opens pull requests, merges code, or deploys software.
- **Untrusted Input Invariant**: All repository contents, documentation, commit messages, PR diffs, and tool outputs are treated as untrusted data.
- **Fail-Secure Container Sandboxing**: Any dynamic execution of repository code occurs strictly within an isolated Docker container with dropped capabilities, read-only root filesystems, non-root user execution, and disabled network access.
- **Refusal of Host Fallback**: When Docker is unavailable, runtime execution is marked `SKIPPED_SANDBOX_UNAVAILABLE`. FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- **Zero Fabrication**: Claims must be backed by concrete, verified evidence. Confidence is automatically downgraded for unbacked assertions.

---

## Phase Roadmap & Status

```
Phase 1: Secure Repository Intelligence  ── [COMPLETED] (Milestones 1–7)
Phase 2: Diff-Aware Intelligent Review   ── [COMPLETED] (Milestones 1–6)
Phase 3: Defect Reproduction & Verification ── [COMPLETED] (Milestones 1–6)
Pre-Phase 4: Local Ollama & Multi-Provider LLM Integration ── [COMPLETED]
Phase 4 Readiness & Sandbox Gate        ── [COMPLETED]
Phase 4: Targeted Autonomous Repair     ── [COMPLETED] (Milestones 1–6)
Phase 5: Supervised End-to-End Engineering Workflow ── [COMPLETED] (Milestones 1–6)
Repair Planner Reliability & Grounded Verification ── [COMPLETED]
Local Ollama Structured Output Reliability ── [COMPLETED]
Patch Generation Reliability & Diff Syntax Enforcement ── [COMPLETED]
Structured Patch Construction & Deterministic Synthesis ── [COMPLETED]
Structured Proposal Wire-Schema Reliability & Patch Representation Contract ── [COMPLETED]
Deterministic Patch Anchoring & Minimal Replacements ── [COMPLETED]
Deterministic Edit Anchoring & Source-Grounded Repair ── [COMPLETED]
Repair Reliability & Generalization ── [COMPLETED]
Candidate Semantic Verification Hardening ── [COMPLETED]
Deterministic Repair Construction Architecture ── [COMPLETED]
Phase 6: Supervised Beta Packaging & Operational Readiness ── [COMPLETED] (Milestones 1–6 Complete — Supervised Beta Release Gate Reached)
Phase 7: Evidence-Driven Investigation Engine ── [COMPLETED] (Cross-File Evidence, Contradiction Detection, Investigation Orchestration)
Phase 9: Safe Git Publication & Upstream Integration ── [COMPLETED] (Milestones 1–4 Complete — Phase 9 Complete)
Phase 10: Protected-File Boundary Enforcement ── [COMPLETED] (Centralized Boundary Policy, Acquisition Filter, Ingestion Guard, Context Hardening, Git Metadata Remediation)
Phase 11: Intelligent Repository Understanding ── [COMPLETED] (M1 Architecture Audit, M2 Symbol Graph & Call-Graph Engine, M3 Module Dependency Graph & Cycle Detection, M4 Change-Impact Analysis, M5 Incremental Indexing & Caching, M6 LLM Tooling & Context Integration)
Phase 12: Advanced Intelligence & Autonomous Defect Repair ── [COMPLETED] (Milestones 1–6 Complete — Full Autonomous Engineering Pipeline Reached)
Phase 13: Autonomous Engineering Orchestration ── [IN PROGRESS] (Milestones 1 & 2 Complete)
```

### Phase 13: Autonomous Engineering Orchestration (In Progress)
- **Objective & Scope**: Unify the discrete Phase 12 defect diagnosis, root cause analysis, repair planning, controlled execution, verification, and quality evaluation engines into a single end-to-end, deterministic, auditable engineering pipeline. Enforce strict lifecycle transitions, non-negotiable human approval boundaries, durable replay protection, fail-closed verification and quality gates, cryptographic event chaining, and permanent human review invariants.
- **Milestone 1: End-to-End Engineering Orchestration (`[COMPLETED]`)**:
  - *Explicit 18-State Lifecycle State Machine (`forge.orchestration.state`)*: Governs end-to-end engineering progression across 18 explicit states (`CREATED`, `DIAGNOSING`, `DIAGNOSIS_COMPLETED`, `ROOT_CAUSE_ANALYSIS`, `ROOT_CAUSE_COMPLETED`, `PLANNING`, `PLAN_READY`, `AWAITING_HUMAN_APPROVAL`, `APPROVED`, `REPAIR_EXECUTING`, `REPAIR_COMPLETED`, `VERIFICATION_RUNNING`, `VERIFICATION_COMPLETED`, `QUALITY_EVALUATION`, `COMPLETED`, `BLOCKED`, `FAILED`, `CANCELLED`). Enforces strict transition validation rules, preventing bypasses or skipping of stages, and raises `TerminalStateError` on actions after terminal states.
  - *Non-Negotiable Human Approval Boundary (`forge.orchestration.approval`)*: Hardcodes `requires_human_review = True` and mandates explicit human approval between planning and execution. Binds approval to canonical SHA-256 plan digest, strategy ID, expiration timestamp, active revocation status, and bidirectional file scope allowlists (preventing both scope breach and scope expansion). Integrates with SQLite `ApprovalLedger` for durable atomic replay and concurrency protection.
  - *Fail-Closed Verification & Quality Gate Integration (`forge.orchestration.orchestrator`)*: Directly ties execution outcomes to Phase 12 M5 verification and M6 quality evaluation. Automatically transitions workflows to `BLOCKED` if verification fails or detects regressions, or if quality scoring/gates fail. Enforces that `COMPLETED` workflows strictly require `verification_status == PASSED` and `regression_classification` in `("NO_REGRESSION_OBSERVED", "IMPROVED")`.
  - *Cryptographic Audit Event Chaining (`forge.orchestration.events`)*: Implemented `OrchestrationEventRecorder` producing append-only `OrchestrationEvent` records linked via SHA-256 hash chaining (`prev_event_hash -> event_hash`), with automated secret redaction and verifiable sequence continuity via `verify_event_chain()`.
  - *Stage Adapters & Execution Fabric (`forge.orchestration.adapters`)*: Implemented abstract `StageAdapter` interface, production adapters invoking Phase 12 engines, and deterministic fake adapters for 100% reproducible testing.
  - *Engineering Dossier Generator (`forge.orchestration.report`)*: Synthesizes publication-ready Markdown dossiers documenting executive summaries, state progression timelines, diagnosis findings, causal chains, repair plan details, human approval metadata, patch statistics, verification test results, quality score radar, and safety notices.
  - *ToolRegistry & Agent Tools Integration (`forge.orchestration.tools`)*: Exposes 6 typed tools to `ToolRegistry`: `start_orchestrated_repair`, `submit_orchestration_approval`, `step_orchestration_workflow`, `get_orchestration_status`, `get_orchestration_audit_trail`, and `generate_orchestration_report`.
  - *CLI Subcommand Group (`forge orchestrate`)*: Integrated CLI commands: `forge orchestrate run` (with `--repo`, `--desc`), `forge orchestrate approve` (with `--approver`, `--plan-id`, `--plan-digest`, `--strategy`, `--file`), `forge orchestrate status`, and `forge orchestrate report`. Automatic approval is strictly forbidden.
  - *Empirical Performance*: State transition validation in **3.70 ms** for 7,000 ops (~1.89M ops/sec); event recording in **13.56 ms** for 200 events; approval invariant validation in **62.66 ms** for 500 ops; end-to-end execution in **97.98 ms** for 50 workflows (~1.96 ms/workflow); peak memory overhead of **478.89 KB**.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-13-m1-engineering-orchestration.md`](docs/milestones/phase-13-m1-engineering-orchestration.md) and [`docs/specs/phase13_end_to_end_engineering_orchestration.md`](docs/specs/phase13_end_to_end_engineering_orchestration.md).
  - *Verification Suite*: 27 dedicated unit tests in `tests/unit/test_phase13_m1_orchestration.py`, 141 Phase 12 M4–M6 regression tests passing, and 100% test pass rate across the full suite.
- **Milestone 2: Agent Autonomy, Coordination & Recovery (`[COMPLETED]`)**:
  - *Durable State Persistence (`forge.orchestration.persistence`)*: Implemented `OrchestrationStateStore` managing SQLite tables (`orchestration_workflows`, `orchestration_events`, `orchestration_stage_records`, `orchestration_stage_outputs`, `orchestration_recovery_checkpoints`, `orchestration_tasks`, `orchestration_idempotency_records`, `orchestration_mutation_reconciliations`) under WAL mode with foreign keys, transactional rollbacks, and schema migrations.
  - *Crash & Interruption Recovery Engine (`forge.orchestration.recovery`)*: Implemented `WorkflowRecoveryEngine` validating cryptographic SHA-256 event chains, repository root path bindings, and state integrity upon process recovery. Preserves `AWAITING_HUMAN_APPROVAL` states without self-approval.
  - *Fail-Closed Mutation Reconciliation & Operator Authorization*: Workflows interrupted during controlled repair (`REPAIR_EXECUTING`) are automatically blocked with `reconciliation_required = True`. Unblocking requires human operator review via `forge orchestrate reconcile <wf_id> --resolution <confirm|discard|revert>`. Synthetic/AI identities are rejected fail-closed, and `trusted_approvers` allowlists are strictly enforced. `confirm` strictly verifies concrete evidence of completed execution prior to interruption and transitions to `REPAIR_COMPLETED` without re-executing. `discard` / `revert` resets state to `AWAITING_HUMAN_APPROVAL` and permanently preserves approval consumption in `ApprovalLedger` so consumed authorizations cannot be reused.
  - *Bounded Deterministic Retry Engine (`forge.orchestration.retry`)*: Categorizes failures into 9 deterministic categories (`FailureClassifier`). Enforces exponential backoff with a hard cap of 5 attempts and 30s delay for transient errors, while immediately failing closed on authorization, integrity, policy, or mutating execution failures.
  - *Task DAG Coordination (`forge.orchestration.coordination`)*: Implemented `TaskCoordinator` supporting multi-task DAG dependencies, Kahn's algorithm cycle detection (`DependencyCycleError`), priority scheduling, failure propagation (`BLOCKED`), and cascading cancellation.
  - *At-Most-Once Idempotency Ledger & Concurrency Hardening (`forge.orchestration.idempotency`)*: Computes canonical SHA-256 idempotency keys, prevents duplicate concurrent executions with `IdempotencyConflictError` via `BEGIN IMMEDIATE` transactions, fails closed (`CorruptedStateError`) on corrupted cached payloads, and replays completed results directly from cache.
  - *CLI Subcommands & ToolRegistry*: Added CLI commands (`forge orchestrate inspect`, `recover`, `tasks`, `reconcile`) and 3 typed tools (`inspect_workflow_recovery`, `recover_orchestration_workflow`, `inspect_workflow_tasks`), bringing the total tool count to 68.
  - *Empirical Performance*: 100 workflow persistence in **303.22 ms**; 50 recovery inspections in **96.49 ms**; 100-task DAG resolution in **125.16 ms**; 500 idempotency operations in **574.14 ms**; 1,000 retry classifications in **14.41 ms**; total benchmark runtime **1.25s** with peak memory overhead of **52.38 KB**.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-13-m2-agent-autonomy-recovery.md`](docs/milestones/phase-13-m2-agent-autonomy-recovery.md) and [`docs/specs/phase13_m2_agent_autonomy_recovery.md`](docs/specs/phase13_m2_agent_autonomy_recovery.md).
  - *Verification Suite*: 53 dedicated unit tests in `tests/unit/test_phase13_m2_autonomy_recovery.py` (including 16 adversarial security audit tests) and 2,068 tests passing with zero regressions across the entire project test suite.

### Phase 12: Advanced Intelligence & Autonomous Defect Repair (Completed)
- **Objective & Scope**: Advance FORGE from read-only static repository understanding into autonomous, evidence-driven software engineering intelligence. Synthesize diagnostic hypotheses, perform multi-source failure localization, evaluate code contradictions, and enforce strict defect confirmation invariants before orchestrating autonomous repair workflows.
- **Milestone 1: Evidence-Driven Defect Diagnosis (`[COMPLETED]`)**:
  - *Data Contracts & Input Budgeting (`forge.diagnosis.schemas`)*: Implemented strict Pydantic models (`extra="forbid"`) for `DiagnosisInput`, `TestFailureReport` (with `__test__ = False` to prevent pytest collection conflicts), `StaticFindingReport`, `CandidateLocation`, `DiagnosisHypothesis`, and `DiagnosisResult`. Enforces strict character budgets across user descriptions (10,000 chars), stack traces (15,000 chars), diffs (50,000 chars), and error messages (5,000 chars).
  - *Defect Classification Taxonomy*: Established a deterministic 12-category taxonomy (`LOGIC_ERROR`, `INCORRECT_DATA_HANDLING`, `INPUT_VALIDATION_FAILURE`, `EXCEPTION_HANDLING_DEFECT`, `RESOURCE_MANAGEMENT_ISSUE`, `DEPENDENCY_INTEGRATION_DEFECT`, `CONCURRENCY_ISSUE`, `SECURITY_VULNERABILITY`, `PERFORMANCE_DEFECT`, `CONFIGURATION_DEFECT`, `TEST_BUILD_FAILURE`, `UNKNOWN`).
  - *Failure Localization Engine (`forge.diagnosis.localization`)*: Implemented `FailureLocalizer` extracting and ranking candidate locations from traceback crash frames (0.90 confidence weight), inline error message references (0.85 weight), failing test callee invocations via `CallGraph.get_direct_callees()` (0.80 weight), explicit target symbols via `SymbolIndex`, and change-impact analysis via `ChangeImpactAnalyzer`. Enforces Phase 10 security boundaries fail-closed, dropping directory traversal attempts and protected files. Automatically discounts and sorts test files behind production source code.
  - *Hypothesis Formulation & Scope-Confined Contradiction Detection (`forge.diagnosis.hypothesis`)*: Evaluates candidate locations against repository facts. Inspects code within enclosing function scopes without bleeding across `def ` boundaries. Generates contradicting evidence records when hypotheses claim missing checks or guards but source code on disk already contains explicit validation or exception handlers. Automatically zeroes confidence (`0.0`, `FindingConfidence.LOW`) for unverified disk locations.
  - *Defect Confirmation vs Suspicion Invariant (`forge.diagnosis.engine`)*: Enforces the core invariant that model-generated suspicions never automatically become confirmed defects. Confirmation strictly requires:
    1. Candidate location is verified against current disk content and line bounds.
    2. Direct grounded source evidence is attached.
    3. Direct failure signal exists (runtime crash traceback frame, test assertion failure, or error message).
    4. Top hypothesis confidence is `HIGH` (numeric score $\ge 0.75$).
    5. Zero unrefuted contradicting evidence on disk.
    6. Defect category is classified into a known deterministic category (not `UNKNOWN`).
    7. Zero competing unrefuted hypotheses with confidence score $\ge 0.70$.
  - *Storage Integration & Stale Evidence Detection*: Integrates with SQLite `ForgeStore` and `EvidenceGroundingEngine` to persist findings and linked evidence records atomically. Exposes `verify_evidence_freshness()` to detect source drift and stale evidence via SHA-256 slice digests.
  - *Local Ollama Isolation & Safety*: Restricts model reasoning strictly to local Ollama endpoints (`localhost` / `127.0.0.1`), failing closed with structured heuristic evaluation without attempting cloud API calls or cloud fallbacks.
  - *Empirical Performance*: Warmup latency of 2,955 ms (full repository index and call-graph resolution) and subsequent incremental diagnosis query latency of **2.05 ms** across the FORGE repository.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-12-m1-evidence-driven-diagnosis.md`](docs/milestones/phase-12-m1-evidence-driven-diagnosis.md).
  - *Verification Suite*: 24 dedicated unit tests in `tests/unit/test_phase12_m1_diagnosis.py` and 1,772 tests passing with zero regressions across the entire test suite.
- **Milestone 2: Root-Cause Analysis Engine (`[COMPLETED]`)**:
  - *Data Contracts & Causal Schemas (`forge.causality.schemas`)*: Implemented strict Pydantic models (`extra="forbid"`) for `CausalNode`, `CausalEdge`, `CausalChain`, `AlternativeExplanation`, and `RootCauseReport`. Established semantic relationships (`CALLS`, `DEPENDS_ON`, `PASSES_INVALID_DATA`, `VIOLATES_PRECONDITION`, `MISUSES_RESOURCE`, `PROPAGATES_EXCEPTION`, `INVALIDATES_STATE`, `TRIGGERS_FAILURE`, `CONTRIBUTES_TO`, `UNKNOWN`) and causal roles (`ROOT_CAUSE`, `CAUSAL_MECHANISM`, `CONTRIBUTING_FACTOR`, `PROPAGATOR`, `SYMPTOM`).
  - *AST Semantic Contradiction & Guard Inspector (`forge.causality.contradiction`)*: Replaced syntactic substring checks with AST analysis inspecting `ast.If` guards raising `ValueError`/`TypeError`, `ast.Assert` statements, and enclosing `ast.Try`/`ast.ExceptHandler` blocks. Ignores comments and docstrings, eliminating false-positive contradiction triggers.
  - *Backward Causal Tracing & Forward Propagation (`forge.causality.tracer`)*: Implemented `CausalTracer` following reverse call edges and module dependencies from failure symptoms to identify origin components and recent changes. Models downstream failure cascades through forward callee exploration with cycle-safe visited sets and recursion immunity.
  - *Root-Cause Status Invariants (`forge.causality.engine`)*: Enforces the core invariant that causal explanations must NEVER be labeled `PROVEN` unless independently verified against direct source evidence and call/dependency graphs. Classifies status into `PROVEN` (all steps directly verified), `PLAUSIBLE` (grounded evidence with inferred links), `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` (competing candidate causes without discriminating evidence), `INSUFFICIENT_EVIDENCE` (missing, ungrounded, or stale evidence), or `UNRESOLVED` (conflicts). Never forces an arbitrary single root cause.
  - *ToolRegistry & Agent Tools Integration (`forge.causality.tools`)*: Exposes 5 structured read-only tools to `ToolRegistry`: `analyze_root_cause`, `trace_causal_chain`, `inspect_contributing_factors`, `compare_root_cause_hypotheses`, and `explain_failure_propagation`.
  - *Local Ollama Isolation & Safety*: Restricts model reasoning strictly to local Ollama endpoints (`localhost:11434`), failing closed with deterministic results if unavailable. Zero cloud inference, zero cloud fallback.
  - *Empirical Performance*: Warmup latency of 2,299 ms on the full FORGE codebase; warm backward causal tracing latency of **23.34 ms**; single-chain report synthesis in **20.19 ms**; evidence freshness validation overhead of **0.228 ms**.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-12-m2-root-cause-analysis.md`](docs/milestones/phase-12-m2-root-cause-analysis.md).
  - *Verification Suite*: 22 dedicated unit tests in `tests/unit/test_phase12_m2_causality.py` and 1,794 tests passing with zero regressions across the entire project test suite.
- **Milestone 3: Repair Planning Engine (`[COMPLETED]`)**:
  - *Data Contracts & Safety Invariants (`forge.repair_planning.schemas`)*: Implemented strict Pydantic models (`extra="forbid"`) for `RepairPlan`, `RepairStrategy`, `RepairRiskAssessment`, `VerificationPlan`, `RollbackPlan`, and `RequiredChange`. Enforces non-negotiable safety invariants directly in model validators: `approval_required = True` and `execution_permitted = False`.
  - *Evidence Freshness & Containment (`forge.repair_planning.evidence`)*: Implemented `RepairEvidenceCollector` collecting grounded evidence from `RootCauseReport`, `DiagnosisResult`, and `ForgeStore`. Enforces workspace containment (`assert_contained_path`), protected-file isolation (`is_protected_path`), line bounds sanity, and SHA-256 freshness verification against disk state.
  - *Candidate Strategy Generation & Minimality (`forge.repair_planning.strategies`)*: Implemented `RepairStrategyGenerator` mapping defect taxonomy and causal chains into structured categories (`LOCAL_LOGIC_CORRECTION`, `INPUT_VALIDATION`, `ERROR_HANDLING`, `RESOURCE_MANAGEMENT`, `SECURITY_HARDENING`, etc.). Prioritizes minimal local fixes over broad refactoring and ranks strategies across invasiveness, complexity, and reversibility.
  - *Deterministic Risk Assessment (`forge.repair_planning.risk`)*: Implemented `RepairRiskAnalyzer` evaluating affected files/symbols, call graph dependency reach (incoming caller count), public API compatibility, security boundaries, data integrity impacts, and automated test coverage. Produces explainable risk ratings (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) and scores in $[0.0, 1.0]$.
  - *Verification & Rollback Planning (`forge.repair_planning.rollback`, `planner`)*: Queries disk for existing test suites covering modified modules, explicitly formats recommended new tests with `[PROPOSED]`, and builds structured rollback procedures specifying reverse code changes, dependency lockfile pins, cache invalidations, and concrete rollback triggers.
  - *Plan Validation & Certainty Gating (`forge.repair_planning.validation`)*: Enforces certainty constraints (`PROVEN` $\rightarrow$ `ACTIONABLE`, `PLAUSIBLE` $\rightarrow$ `CONDITIONAL`, `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` $\rightarrow$ `MULTIPLE_CANDIDATES`, `INSUFFICIENT_EVIDENCE` / `UNRESOLVED` $\rightarrow$ non-actionable status).
  - *ToolRegistry & Agent Tools Integration (`forge.repair_planning.tools`)*: Exposes 6 typed, read-only tools: `create_repair_plan`, `compare_repair_strategies`, `assess_repair_risk`, `explain_repair_plan`, `validate_repair_plan`, and `generate_verification_plan`.
  - *Empirical Performance*: Evidence collection and freshness audit in **5.11 ms**; strategy generation in **0.12 ms**; risk assessment in **0.32 ms**; full deterministic plan construction in **8.22 ms** warm latency (mean) on the FORGE codebase.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-12-m3-repair-planning.md`](docs/milestones/phase-12-m3-repair-planning.md).
  - *Verification Suite*: 34 dedicated unit tests in `tests/unit/test_phase12_m3_repair_planning.py` passing with zero regressions across the entire project test suite.
- **Milestone 4: Controlled Autonomous Repair (`[COMPLETED]`)**:
  - *Data Contracts & Safety Enums (`forge.repair_execution.schemas`)*: Implemented strict Pydantic models (`extra="forbid"`) for `ExecutionLimits`, `RepairApproval`, `RepairWorkspace`, `RepairExecutionRequest`, `RepairPatch`, `RepairEvent`, and `RepairExecutionResult`. Defined lifecycle enums (`ExecutionStatus`, `ApprovalStatus`, `WorkspaceStatus`, `PatchValidationStatus`, `FailureClassification`, `RollbackStatus`, `RepairEventType`).
  - *Human Approval & Replay Protection (`forge.repair_execution.approval`, `executor`, `ledger`)*: Enforces non-negotiable authorization rules via `RepairApprovalValidator`, binding approval to canonical SHA-256 plan digest (including change descriptions and rationales), strategy ID, expiry timestamp, and an approved file allowlist. Implemented `ApprovalLedger` backed by SQLite and thread-locks for durable atomic replay and race protection, alongside trusted approver allowlists and explicit approval revocation tracking.
  - *Isolated Workspace Lifecycle & Immutable Baseline Snapshot (`forge.repair_execution.workspace`)*: Allocates isolated directories under the workspace directory via `_safe_copy_tree()` filtering out FIFOs, unix sockets, device nodes, and escaping symlinks. Captures an immutable, write-protected baseline snapshot directory (`<workspace_id>_baseline`, mode 0400/0500) alongside the active workspace with collision resilience. Purges sensitive files via `purge_protected_paths_from_workspace()`, computes baseline SHA-256 tree digests, and guarantees target repository immutability.
  - *Controlled In-Memory Patch Application & Strict Validation (`forge.repair_execution.patch_applier`, `patch_generator`, `patch_validator`)*: Implements pure Python patch application with zero shell execution and zero subprocess calls. Synthesizes bounded unified diffs. `RepairPatchValidator` audits exact hunk line counts (`actual_old == old_len`, `actual_new == new_len`), ensures monotonic line ordering, prevents overlapping hunks, and rejects dangerous git metadata (`rename`, `old mode`, `new mode`). `ControlledPatchApplier` checks context lines, applies modifications in reverse line order with automatic in-memory backup reversion on failure, and preserves native CRLF and EOF newline formatting.
  - *Integrity Auditing & Pre-Rollback Verified Snapshot Recovery (`forge.repair_execution.integrity`, `rollback`)*: `WorkspaceIntegrityChecker` audits post-application directory trees against the immutable baseline snapshot, detecting modifications outside approved scope. `WorkspaceRollbackManager` verifies snapshot digest before restoring workspaces, refusing restoration from corrupted baselines, guaranteeing 100% deterministic baseline recovery even if the original host repository is mutated.
  - *Cryptographic Tamper-Evident Audit Event Chaining (`forge.repair_execution.events`)*: Implemented `RepairEventRecorder` emitting append-only `RepairEvent` records linked via SHA-256 cryptographic hash chaining (`prev_event_hash`), verified by `verify_event_chain()` with head-hash commitment checks, automatic secret redaction, and a strict cap at `MAX_EVENTS_PER_EXECUTION = 200`.
  - *ToolRegistry & Agent Tools Integration (`forge.repair_execution.tools`)*: Exposes 8 typed tools to `ToolRegistry`: `prepare_repair_workspace`, `validate_repair_approval`, `generate_repair_patch`, `validate_repair_patch`, `apply_approved_repair`, `inspect_repair_execution`, `rollback_repair_workspace`, and `get_repair_execution_history`.
  - *Empirical Performance*: Cold workspace allocation in **29.45 ms**; warm allocation in **27.65 ms**; approval invariant validation in **0.081 ms**; patch validation in **0.754 ms**; in-memory patch application in **1.15 ms**; workspace integrity check in **8.24 ms**; rollback restoration in **7.56 ms**; end-to-end execution in **2.15 ms**; peak memory footprint of **190.37 KB**.
  - *Verification-Pending Handoff*: Strictly confines execution to safe patch application; test execution is deferred to Phase 12 M5 (`verification_pending = True`).
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-12-m4-controlled-repair.md`](docs/milestones/phase-12-m4-controlled-repair.md).
  - *Verification Suite*: 39 dedicated unit tests in `tests/unit/test_phase12_m4_controlled_repair.py` and 1,859 tests passing with zero regressions across the entire project test suite.
- **Milestone 5: Repair Verification Engine (`[COMPLETED]`)**:
  - *Data Contracts & Safety Invariants (`forge.repair_verification.schemas`)*: Implemented strict Pydantic models (`extra="forbid"`, `frozen=True`) for `VerificationPolicy`, `TestIdentifier`, `SelectedCheck`, `VerificationPlan`, `CheckExecutionResult`, `SuiteExecutionSummary`, `RegressionFinding`, `VerificationEvidenceRecord`, `VerificationEvent`, and `VerificationResult`. Enforces non-negotiable safety invariant: `requires_human_review = True` (permanently required; rejects `False` across construction and `model_copy(update=...)`).
  - *Bounded Test Discovery & Intelligent Selection (`forge.repair_verification.discovery`, `test_selection`)*: Implemented `RepairTestDiscoverer` mapping modified files to targeted and regression test suites while enforcing `assert_contained_path` and rejecting escaping symlinks. `IntelligentTestSelector` selects targeted test vectors, dependent regression checks, and static analysis checks (`py_compile`), with explicit partial verification disclosure.
  - *Baseline-Aware Regression Analysis (`forge.repair_verification.baseline`, `regression`)*: Executes identical check vectors against an isolated copy of the immutable baseline snapshot. `DeterministicRegressionEngine` maps outcomes into `NO_REGRESSION_OBSERVED`, `REGRESSION_DETECTED`, `PRE_EXISTING_FAILURE`, and `IMPROVED` without LLM hallucinations.
  - *Container Sandbox Execution & Fail-Closed Refusal (`forge.repair_verification.sandbox`)*: Enforces strict executable binary allowlist (`pytest`, `python`, `python3`, `flake8`, `mypy`), disallowing shell metacharacters and arbitrary strings. Fails closed with `SANDBOX_UNAVAILABLE` when container isolation is offline, with zero host execution fallback.
  - *Result Parsing & Anomaly Detection (`forge.repair_verification.result_parser`)*: Parses pytest outputs, identifying test counts, timeouts, collection errors, and contradictions (e.g. exit code 0 with failed tests or missing reports).
  - *Multi-Stage Integrity & Gatekeeper (`forge.repair_verification.integrity`, `gates`)*: Audits original repository immutability, verifies baseline snapshot digests, detects unauthorized workspace modifications, and evaluates strict release gates.
  - *ToolRegistry & Agent Tools Integration (`forge.repair_verification.tools`)*: Exposes 9 typed tools to `ToolRegistry`: `create_repair_verification_plan`, `discover_repair_tests`, `inspect_verification_environment`, `run_repair_verification`, `run_baseline_verification`, `compare_repair_verification`, `inspect_verification_evidence`, `validate_verification_integrity`, and `get_repair_verification_summary`.
  - *Empirical Performance*: Test discovery latency in **4.02 ms**; plan generation in **4.70 ms**; baseline snapshot audit in **3.72 ms**; test result parsing in **0.013 ms**; regression comparison in **0.009 ms**; integrity audit in **7.66 ms**; event recording in **0.060 ms**; end-to-end fake-adapter orchestration in **21.54 ms**; peak memory footprint of **369.18 KB**.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-12-m5-repair-verification.md`](docs/milestones/phase-12-m5-repair-verification.md).
  - *Verification Suite*: 29 dedicated unit tests in `tests/unit/test_phase12_m5_repair_verification.py`, 39 M4 regression tests, and full project regression suite passing with zero regressions.
- **Milestone 6: Repair Quality & Reliability Engine (`[COMPLETED]`)**:
  - *Multi-Dimensional Quality Evaluation (`forge.repair_quality`)*: Evaluates engineering quality across 6 independent, explainable dimensions: Correctness, Completeness, Maintainability, Patch Quality, Reliability, and Security Risk.
  - *Cryptographic Evidence Validation (`forge.repair_quality.evidence`)*: Enforces strict cryptographic anchoring to M2 diagnoses, M3 repair plan digests, M4 execution and patch digests, and M5 verification results with freshness audits.
  - *AST Maintainability & Complexity Analysis (`forge.repair_quality.maintainability`)*: Analyzes McCabe cyclomatic complexity deltas, function length changes, code duplication, dead code markers, and public API signature modifications using Python's `ast` parser.
  - *Bounded SQLite Historical Reliability Store (`forge.repair_quality.history`)*: Thread-safe persistence of historical repair attempts, strategy success rates, and repeated regressions bounded by `MAX_HISTORY_RECORDS = 1,000`.
  - *Transparent Scoring & Non-Overridable Clamps (`forge.repair_quality.scoring`)*: Deterministic composite scoring model with configurable dimension weights and hard clamps: regressions and critical risks strictly cap overall scores at $\le 0.20$.
  - *Fail-Closed Quality Gatekeeper (`forge.repair_quality.gates`)*: Evaluates 10 non-negotiable criteria before granting `PASSED`. The permanent invariant `requires_human_review = True` ensures quality approval never authorizes automatic merge or deployment.
  - *ToolRegistry & Agent Tools Integration (`forge.repair_quality.tools`)*: Exposes 12 typed tools: `create_repair_quality_request`, `validate_repair_quality_evidence`, `analyze_repair_patch_quality`, `analyze_repair_maintainability`, `analyze_repair_completeness`, `evaluate_repair_correctness`, `analyze_repair_reliability`, `analyze_repair_risk`, `calculate_repair_quality_score`, `evaluate_repair_quality_gates`, `inspect_repair_quality_history`, and `get_repair_quality_summary`.
  - *Empirical Performance*: Evidence validation in **0.007 ms/op**; patch analysis in **0.004 ms/op**; AST maintainability analysis in **0.040 ms/op**; composite scoring in **0.0000 ms/op**; SQLite history query in **11.83 ms**; end-to-end evaluation latency of **9.93 ms**; peak memory footprint of **430.48 KB**.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-12-m6-repair-quality-reliability.md`](docs/milestones/phase-12-m6-repair-quality-reliability.md).
  - *Verification Suite*: 45 dedicated unit tests in `tests/unit/test_phase12_m6_repair_quality.py` and 1,946 tests passing with zero regressions across the entire project test suite.

### Phase 11: Intelligent Repository Understanding (Completed)
- **Objective & Scope**: Enable comprehensive static understanding of software repositories by introducing qualified symbol naming, cross-file import resolution, forward and reverse call graphs, module dependency DAGs, cycle detection, change-impact analysis, incremental caching, and LLM reasoning integration without dynamic execution or compromising Phase 10 isolation boundaries.
- **Milestone 1: Architecture Audit (`[COMPLETED]`)**: Completed read-only architecture assessment classifying 13 repository understanding capabilities and defining modular architecture for Milestones 2–6.
- **Milestone 2: Symbol Graph & Cross-File Call-Graph Engine (`[COMPLETED]`)**:
  - *Data Models & Schemas (`forge.repository.graph_schemas`)*: Implemented `GraphNode`, `CallEdge`, `CallGraphSummary`, `NodeKind`, `EdgeKind`, and `CallResolutionStatus` (`RESOLVED_INTERNAL`, `RESOLVED_EXTERNAL`, `UNRESOLVED`, `AMBIGUOUS`, `DYNAMIC`).
  - *Workspace Import Resolver (`forge.repository.resolver`)*: Implemented `WorkspaceImportResolver` mapping workspace modules, resolving absolute, relative, aliased, and package `__init__.py` imports while rejecting root-escaping traversal fail-closed.
  - *AST Symbol & Call Extractor (`forge.repository.indexer`)*: Enhanced `ASTSymbolVisitor` to extract stable qualified symbol names, track `<locals>` function scopes, and record call-site expressions from `ast.Call` nodes with complete backward compatibility.
  - *Call-Graph Engine (`forge.repository.call_graph`)*: Implemented `CallGraph` and `build_call_graph()` resolving direct calls, module-qualified calls, aliased imports, `self.method()`/`cls.method()` inheritance across files, recursion, and mutual recursion.
  - *Cycle-Safe Bounded Traversal*: Implemented `get_callers()`, `get_callees()`, `get_direct_callers()`, and `get_direct_callees()` with deterministic sorting, `visited` set protection, and `max_depth`/`max_nodes` budgets.
  - *Dedicated Specification*: Created [`docs/specs/phase11_intelligent_repository_understanding.md`](docs/specs/phase11_intelligent_repository_understanding.md).
  - *Verification Suite*: 35 dedicated unit tests across `tests/unit/test_repository_resolver.py` (12 tests) and `tests/unit/test_call_graph.py` (23 tests); full regression suite passing.
- **Milestone 3: Module Dependency Graph & Cycle Detection (`[COMPLETED]`)**:
  - *Data Models & Schemas (`forge.repository.graph_schemas`)*: Implemented `ModuleNode`, `ModuleEdge`, `ImportEvidence`, `ExternalImportRecord`, `UnresolvedImportRecord`, `DependencyCycle`, `StronglyConnectedComponent`, and `ModuleDependencyGraphSummary`. Added `EdgeKind.IMPORTS`.
  - *Import Coverage & Classification (`forge.repository.resolver`, `forge.repository.module_graph`)*: Supported plain absolute imports, from-module imports, aliased imports (`import x as y`), relative multi-level imports (`.`, `..`), and star imports (`*`). Distinguished internal workspace modules, standard-library packages (`is_stdlib_module`), and third-party packages (`is_third_party_module`). Consolidated multiple imports into single weighted edges with individual statement provenance.
  - *Deterministic Strongly Connected Components & Cycle Detection (`forge.repository.module_graph`)*: Implemented Tarjan's SCC algorithm to partition the internal module graph into strongly connected components, identifying cyclic components. Engineered canonical elementary cycle detection extracting direct self-dependencies (length 1), 2-module cycles, and multi-module cycles, rotated canonically to start with the lexicographically smallest module.
  - *Query & Traversal APIs*: Implemented `get_dependencies()`, `get_dependents()`, `get_transitive_dependencies()`, `get_transitive_dependents()`, `detect_cycles()`, `get_strongly_connected_components()`, `get_isolated_modules()`, `get_edge()`, `is_in_cycle()`, and `get_dependency_summary()`.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-11-m3-module-dependency-graph.md`](docs/milestones/phase-11-m3-module-dependency-graph.md).
  - *Verification Suite*: 24 dedicated unit tests in `tests/unit/test_module_graph.py` and 60 total Phase 11 unit tests passing with zero regressions.
- **Milestone 4: Change-Impact Analysis (`[COMPLETED]`)**:
  - *Data Models & Schemas (`forge.analysis.impact.schemas`)*: Implemented `ChangeSet`, `FileChange`, `SymbolChange`, `ImpactPathStep`, `ImpactedEntity`, `ImpactSummary`, and `ImpactReport`. Defined 5-tier classification taxonomy: `DIRECT`, `TRANSITIVE`, `POTENTIAL`, `UNRESOLVED`, `UNAFFECTED` with deterministic heuristic confidence scoring (ranking weights, not calibrated probabilities).
  - *Changeset Extraction & Boundary Enforcement (`forge.analysis.impact.extractor`)*: Implemented `extract_changeset_from_diff()` and `extract_changeset_from_files()`. Maps unified diff hunks and modified line numbers to AST symbol boundaries without executing code. Rejects path traversal (`..`), absolute paths, escaping symlinks, and protected paths (`.env*`, `*.db*`, `contacts.json`, `*.log`, `.git/`) fail-closed.
  - *Reverse Propagation Engine (`forge.analysis.impact.analyzer`)*: Implemented `ChangeImpactAnalyzer` traversing reverse invocation paths via `CallGraph.get_direct_callers()` and reverse module dependency paths via `ModuleDependencyGraph.get_dependents()`. Propagates method changes to enclosing classes, tracks multi-level caller/dependent chains via cycle-safe BFS, classifies dynamic dispatch and star imports as `POTENTIAL`, and classifies broken references to deleted or renamed symbols/modules as `UNRESOLVED`.
  - *Traversal Budgets & Performance*: Enforces configurable `max_depth` and `max_nodes` caps with explicit truncation warnings and deduplication. Delivers ~0.07s to 0.10s warm impact propagation query latency across 269 workspace modules.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-11-m4-change-impact-analysis.md`](docs/milestones/phase-11-m4-change-impact-analysis.md).
  - *Verification Suite*: 31 dedicated unit tests in `tests/unit/test_change_impact.py` and 91 total Phase 11 unit tests passing with zero regressions.
- **Milestone 5: Incremental Indexing & Caching (`[COMPLETED]`)**:
  - *Data Models & Schemas (`forge.repository.cache_schemas`)*: Implemented `FileIndexRecord`, `CacheEntry`, `CacheStats`, `IncrementalUpdateSummary`, and `RepositoryIndexState` with schema versioning (`1.0.0`) and parser versioning (`1.0.0`).
  - *Cache Storage Engine (`forge.repository.cache`)*: Implemented `IndexCacheManager` executing atomic disk writes (`.tmp.{pid}.{uuid}` + `os.replace`), automated SHA-256 payload checksums, corruption recovery, orphaned temp file cleanup, and LRU eviction under bounded storage (`max_entries = 5000`, `max_bytes = 50 MB`).
  - *Incremental Graph Coordinator (`forge.repository.incremental`)*: Implemented `IncrementalRepositoryIndex` detecting deltas across added, modified, deleted, renamed, and unchanged files via SHA-256 content hashes. Dynamically invalidates downstream dependents and safely falls back to clean full rebuilds on untrusted cache states.
  - *Syntax Error Resilience*: Safely records and caches empty index envelopes for unparseable files, guaranteeing consistent module node representation across full and incremental builds.
  - *Exact Semantic Equivalence*: Enforced 100% semantic identity with clean full rebuilds via `IncrementalRepositoryIndex.verify_equivalence()` across all 14 graph invariants and M4 change-impact analysis.
  - *Empirical Performance on 279 Files*: Delivers 2.32s warm build latency (**100.0% cache hit rate**, 279 hits, 0 misses, **3.19x speedup** vs 7.38s cold build) with 19.35 MB disk footprint.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-11-m5-incremental-indexing-and-caching.md`](docs/milestones/phase-11-m5-incremental-indexing-and-caching.md).
  - *Verification Suite*: 19 dedicated unit tests in `tests/unit/test_incremental_indexing.py` and 110 total Phase 11 unit tests passing with zero regressions.
- **Milestone 6: LLM Tooling & Context Integration (`[COMPLETED]`)**:
  - *Strict Typed Tool Schemas (`forge.llm.repo_tool_schemas`)*: Implemented Pydantic models with `extra="forbid"` for 6 intelligence tools: `get_repository_overview`, `search_symbols`, `get_symbol_details`, `explore_call_graph`, `explore_module_dependencies`, `analyze_change_impact`, and `get_source_context`.
  - *Repository Intelligence Tools (`forge.llm.repo_tools`)*: Implemented `RepositoryIntelligenceTools` providing typed dispatch for LLM models. Integrates lazy cached graph access via `IncrementalRepositoryIndex` without re-indexing unchanged modules. Detects ambiguous symbols, returns candidates, and enforces path containment and protected-file boundaries.
  - *Repository Context Builder (`forge.llm.context`)*: Implemented `RepositoryContextBuilder` assembling task-focused repository context within character budgets (default 16,000 characters). Automatically selects relevant symbols, expands forward/reverse call edges and module dependencies, surfaces circular dependency warnings, merges and deduplicates source excerpt ranges, redacts secrets, and appends explicit truncation notices when capped.
  - *Evidence Grounding Engine (`forge.llm.evidence_grounding`)*: Implemented `EvidenceGroundingEngine` preventing LLM hallucinations and fabrication. Candidate findings are strictly verified against actual disk file existence and line counts before acceptance and persistence in SQLite `ForgeStore`. Creates cryptographically verifiable `SOURCE_EXCERPT` evidence records with SHA-256 digests.
  - *Local Ollama Isolation & Single-Process Safety*: Restricts model inference strictly to local Ollama endpoints (`localhost` / `127.0.0.1`). Bypasses or connection failures fail closed with `LLMUnavailableError` without attempting cloud API calls or cloud fallbacks. Preserves single-process cache operational constraints.
  - *Sub-Millisecond Query Performance*: Delivers 2.15ms repository overview queries, sub-millisecond graph and symbol lookups (<0.01ms), 0.44ms change-impact analysis, 0.15ms source extraction, and 1.04ms budgeted context construction.
  - *Dedicated Milestone Documentation*: Created [`docs/milestones/phase-11-m6-llm-tooling-context-integration.md`](docs/milestones/phase-11-m6-llm-tooling-context-integration.md).
  - *Verification Suite*: 35 dedicated unit tests in `tests/unit/test_phase11_m6_llm_tooling.py` and 145 total Phase 11 unit tests passing with zero regressions across all 1,748 tests in the full suite.


### Phase 10: Protected-File Boundary Enforcement & Safe Git Metadata Isolation (Completed)
- **Objective & Scope**: Centralize and enforce strict protected-file boundaries across all FORGE ingestion, static analysis, dynamic verification, context assembly, and investigation subsystems, ensuring sensitive repository files (`.env*`, `*.db*`, `contacts.json`, `*.log`) are never read, indexed, copied to scratch analysis workspaces, hashed, or included in LLM context. Implement Option C: Safe Git Metadata Isolation to separate trusted Git storage from agent-accessible workspaces, introducing a narrow, path-protected Git mediator while maintaining 100% upstream commit identity, uncorrupted repository integrity (`git fsck --full`), and seamless Phase 9 publication compatibility.
- **Architectural Deliverables**:
  - *Centralized Security Policy (`forge.repository.protection`)*: Implemented `is_protected_path`, `is_protected_name`, `filter_unprotected_paths`, `create_copytree_ignore`, and `purge_protected_paths_from_workspace`. Operates purely on path metadata with zero file-reading side effects. Anchors relative paths to base directories to prevent false containment escapes, and resolves symlinks defensively, failing closed on resolution or traversal errors.
  - *Safe Git Metadata Isolation Architecture (`forge.repository.git_mediator`, `forge.repository.acquisition`)*: Implemented Option C architecture separating trusted Git metadata from the agent-accessible analysis workspace:
    - *Physical & Permission Isolation*: Upstream Git clone is stored in a private trusted mirror (`trusted_<id>.git`) with mode `0700` user-only permissions completely outside the analysis workspace (`repo_<id>`).
    - *Agent Workspace Sanitization*: The analysis workspace (`repo_<id>`) contains exclusively non-protected files exported via streaming tar archive, followed by initialization of a clean, isolated analysis Git repository (`analysis_commit_sha`). Zero protected file bytes, historical secret blobs, or packfiles exist within the analysis workspace.
    - *Narrow Mediated Git Interface (`GitMediator`)*: Mediates all Git operations against the trusted mirror. Enforces strict upfront `is_protected_path()` checks on all file reads (`get_file_content_at_ref`), tree listings, and diffs (`get_safe_diff`). Sanitizes command output and redacts secrets before data reaches logs or LLMs.
    - *Complete Historical Mirror Guarantee*: The trusted mirror preserves the complete, intact historical Git object graph without deleted blobs or broken trees. Passes `git fsck --full` with exit code 0 and zero broken links.
    - *Upstream Commit Identity Preservation*: Returns authentic upstream commit SHA via `AcquiredRepository.resolved_commit_sha` and `AcquiredRepository.upstream_commit_sha`, ensuring complete backward compatibility with Phase 6 revision validators, drift checkers, and Phase 9 PR publishers.
  - *Static Detection & AST Indexing Guards (`forge.repository.detector`, `forge.repository.indexer`)*: In `detect_repository`, prunes protected directory trees in-place during `os.walk` and skips protected files before reading configurations or counting lines. In `build_symbol_index` and `index_python_file`, rejects protected paths upfront before AST parsing.
  - *Repository Instructions Hardening (`forge.repository.instructions`)*: Excludes instruction files targeting protected files or symlinks in `load_instruction_files`.
  - *Workflow Intake & Context Isolation (`forge.workflow.intake`, `forge.workflow.context`)*: In `sanitize_affected_paths`, immediately rejects caller-declared protected paths with `IntakeValidationError`. In `extract_source_excerpts`, skips protected files before `read_text` and before SHA-256 calculation. In `assemble_repository_context` and `render_context_prompt`, filters declared affected files to drop protected entries from prompt headers.
  - *Verification & Dossier Validation (`forge.workflow.verification`, `forge.workflow.dossier`)*: In `_run_single_check`, rejects patches targeting protected files and skips reading baseline or candidate files. In `validate_engineering_dossier`, enforces that dossier manifests and files on disk contain zero protected paths.
  - *Publication & Staging Protection (`forge.publication.commit`)*: Upgraded `GitCommitBuilder.apply_and_stage_patch` to strictly reject patches modifying protected paths (`.env`, `contacts.json`, etc.), raising `PatchStagingError`.
  - *Evidence & Investigation Tool Guards (`forge.analysis.evidence`, `forge.analysis.validation`, `forge.investigation.tools`)*: Rejects protected paths in `validate_repository_path` and `create_evidence`. Excludes protected files from `search` tool results and rejects protected paths in `read_file` tool calls.
  - *Dedicated Specification ([`docs/specs/phase10_protected_file_boundaries.md`](docs/specs/phase10_protected_file_boundaries.md))*: Formal architecture specification detailing Option C Git metadata isolation, threat modeling, pattern registry, multi-stage pipeline enforcement, `GitMediator` contract, access control boundaries, and empirical test coverage.
  - *Verification Suite*: 60 dedicated unit and security tests across `tests/unit/test_git_metadata_isolation.py` (9 tests) and `tests/unit/test_repository_protection.py` (51 tests), 19 security tests in `tests/security/`, and 1,506 unit tests passed (1 skipped) across the full suite (1,525 total passing tests).
  - *Scope Boundary*: Phase 10 strictly hardens repository inspection and ingest boundaries. No target repository files are mutated.

- **Objective & Scope**: Implement a comprehensive operator audit, publication ledger, and end-to-end governance system for FORGE, providing a trustworthy, queryable, reconstructable, and cryptographically verified record of the entire repair-to-pull-request lifecycle.
- **Architectural Deliverables**:
  - *Cryptographic Hash-Chained Event Ledger (`forge.publication.audit_crypto`)*: Engineered deterministic canonical JSON serialization (RFC 8785 sorting and compact formatting) with SHA-256 hash chaining (`prev_event_hash`, `event_hash`). Enforces continuous sequence integrity, duplicate/gap detection, and backward compatibility with unsealed legacy records (`AuditIntegrityStatus.LEGACY_UNSEALED`). Any event alteration, insertion, deletion, or reordering triggers `TAMPERED` or `BROKEN_CHAIN`.
  - *Unified Publication Ledger & 22-Stage Chronological Timeline (`forge.publication.audit`)*: Reconstructs the complete lifecycle across 22 stages (defect investigation, evidence collection, candidate patch generation, validation, verification, review dossier compilation, operator approval, publication planning, local commit staging, remote branch push, PR creation, provider operations) without fabricating missing events.
  - *14-Point Publication Security Auditor (`forge.publication.audit`)*: Implemented `PublicationSecurityAuditor` executing 14 independent security and invariant checks (`SEC-01` through `SEC-14`) covering authoritative approval binding, action match (`CREATE_PR` required for remote PRs), non-expiration, non-invalidation, patch hash alignment, base commit SHA match, target repo immutability, branch name alignment, remote commit SHA verification, non-destructive fast-forward push enforcement, target branch validity, audit ledger cryptographic integrity, and zero credential leakage.
  - *Non-Destructive State Reconciler (`forge.publication.audit`)*: Implemented `PublicationReconciler` evaluating observed repository and hosting provider states against persisted records (`CONFIRMED_SUCCESS`, `PARTIAL_SUCCESS`, `CONFIRMED_FAILURE`, `EXTERNAL_MODIFICATION`, `AUTHORIZATION_INVALIDATED`). Strictly non-destructive: never deletes remote branches, merges code, or closes pull requests.
  - *Fact-Segregated Audit Reporting (`forge.publication.audit`)*: Implemented `PublicationAuditReportBuilder` rendering comprehensive audit reports in Markdown and JSON formats, strictly segregating verified empirical facts from automated model explanations, with automated secret redaction.
  - *Typed SQLite Persistence & Audit Ledger (`forge.storage.database`, `forge.publication.storage`)*: Bumped database schema to Version 5 with tables `publication_audit_verifications`, `publication_reconciliations`, and hash-chained columns on `repair_execution_events`.
  - *CLI Commands (`forge.cli`)*: Added `forge repair publish audit`, `forge repair publish timeline`, `forge repair publish audit-verify`, `forge repair publish report`, `forge repair publish history`, and top-level aliases under `forge publish ...`.
  - *Dedicated Specification ([`docs/specs/phase-9-m4-publication-ledger-governance.md`](docs/specs/phase-9-m4-publication-ledger-governance.md))*: Formal architecture specification detailing cryptographic hash chaining, 14-point security auditor, non-destructive reconciliation, fact segregation, and SQLite schemas.
  - *Verification Suite*: 37 unit & governance tests in `tests/unit/test_phase9_m4_governance.py` and 140 tests across Phase 9 passing with zero regressions.
  - *Scope Boundary*: Phase 9 is complete. FORGE strictly investigates repositories, stages branches and creates PRs under operator authorization, and audits the entire lifecycle. Automated merging, deployment, and unsupervised mutations remain strictly forbidden.

### Phase 9, Milestone 3: Remote Pull Request Creation & Upstream Integration (Completed)
- **Objective & Scope**: Implement controlled remote Git branch publication and upstream pull request creation for Phase 8 verified and operator-authorized candidate repairs, validating authoritative `CREATE_PR` approvals, publishing dedicated branches via standard Git push, independently querying remote refs, generating evidence-grounded pull requests with strict separation of verified facts from AI text, and persisting a full audit ledger in SQLite.
- **Architectural Deliverables**:
  - *Strict Authorization Gate (`forge.publication.remote_coordinator`)*: Re-validates operator review decisions directly from SQLite before remote push and before PR creation. Enforces invariant that only `AuthorizedAction.CREATE_PR` in state `APPROVED` may proceed. Historical or alternative approvals (`EXPORT_PATCH`, `MANUAL_INTEGRATION`, `STAGE_FOR_PR`, `STAGE_BRANCH`) fail closed and are strictly forbidden from remote publication.
  - *Safe Remote Branch Publishing (`forge.publication.remote_publisher`)*: Verifies remote repository identity (`verify_remote_repository_identity`), enforces protected branch defense (`ProtectedBranchViolationError` for `main`, `master`, etc.), executes safe Git push (`git push origin <commit_sha>:refs/heads/<branch>`) without force flags, and proactively checks for remote collisions (`RemoteBranchCollisionError` on commit mismatch; safe idempotent reconciliation on identical commit).
  - *Post-Push Independent Remote Verification (`forge.publication.remote_publisher`)*: Directly inspects the remote ref using `git ls-remote` and revision parsing, independently verifying that remote HEAD matches the expected commit SHA, that commit parent matches the base commit SHA, and that the tree SHA matches the verified candidate patch tree.
  - *Evidence-Grounded Pull Request Construction (`forge.publication.pr_template`)*: Implemented `PullRequestFormatter` generating deterministic, structured PR descriptions strictly separating verified empirical facts (finding ID, base commit SHA, patch SHA-256, test summaries, 0 regressions, touched files) from automated model explanations, and including operator authorization notices and verification limitations.
  - *Extensible Provider Architecture (`forge.publication.providers`)*: Defined `PullRequestProviderProtocol` and production `GitHubPullRequestProvider` with token resolution from `FORGE_GITHUB_TOKEN`/`GITHUB_TOKEN`, zero credential leakage in logs or exceptions, duplicate PR detection, rate-limit exponential backoff, and lost-response recovery.
  - *Typed SQLite Persistence & Audit Ledger (`forge.storage.database`, `forge.publication.storage`)*: Bumped database schema to Version 4 with tables `publication_remote_branches`, `publication_pull_requests`, `publication_provider_operations`, `publication_remote_records`. Recorded sequential, secret-redacted audit events (`remote_identity_verified`, `remote_branch_published`, `remote_branch_verified`, `pull_request_created`, `pull_request_verified`) in `repair_execution_events`.
  - *CLI Commands (`forge.cli`)*: Added `forge repair publish remote-validate`, `forge repair publish push`, `forge repair publish pr`, `forge repair publish remote-status`, `forge repair publish pr-status`, `forge repair publish remote-inspect`, and top-level aliases under `forge publish ...`.
  - *Dedicated Specification ([`docs/specs/phase-9-m3-remote-pull-request-publication.md`](docs/specs/phase-9-m3-remote-pull-request-publication.md))*: Formal architecture specification detailing provider contracts, threat model, authorization gates, remote verification invariants, SQLite schemas, and real provider acceptance instructions.
  - *Verification Suite*: 26 unit & security tests in `tests/unit/test_phase9_m3_remote_publication.py` and 1,409 tests across the full pytest suite passing with zero regressions.
  - *Scope Boundary*: Branch publishing and PR opening only; automated pull-request merging, automated branch deletion, and production deployment remain strictly forbidden. Upstream CI feedback, review monitoring, and merge readiness gating are deferred to Milestone 4.

### Phase 9, Milestone 2: Isolated Branch Staging & Commit Creation (Completed)
- **Objective & Scope**: Implement safe, isolated local Git branch staging and commit creation for Phase 8 verified and operator-authorized candidate repairs, provisioning dedicated scratch Git clones, creating deterministic branches, staging approved patches, and constructing verified commits with strict target repository immutability.
- **Architectural Deliverables**:
  - *Strict Target Repository Immutability (`forge.publication.workspace`)*: Target repository is treated as strictly read-only and immutable. An initial cryptographic snapshot (`HEAD` SHA, current branch, git status, tree SHA, remote URL) is captured at intake and re-verified before and after every staging, branch creation, patch application, and commit operation. Any modification raises `TargetRepoMutationError`.
  - *Isolated Scratch Publication Workspace (`forge.publication.workspace`)*: Engineered `PublicationWorkspace` provisioning isolated scratch clones outside the target repository via `git clone --no-hardlinks`, configured with `core.hooksPath=/dev/null`, push disabled (`git remote set-url --push origin disabled://no-push`), and detached checkout at approved `base_commit_sha`. Enforces path traversal containment and symlink escape defenses (`SymlinkEscapeError`).
  - *Deterministic Branch Management (`forge.publication.branch`)*: Implemented `GitBranchManager` generating deterministic branch names (`forge/fix-<finding-id>-<patch-hash[:8]>`), validating ref syntax against dangerous Git characters, proactively detecting branch collisions across both target repo and workspace (`BranchCollisionError`), and strictly rejecting protected branches (`main`, `master`, `release/*`, `production/*`, `stable/*`).
  - *Normal Descriptive Commit Builder (`forge.publication.commit`)*: Implemented `GitCommitBuilder` applying approved patch bytes, verifying touched files against `allowed_files`, rejecting binary changes or unexpected staged files, enforcing normal descriptive commit messages without conventional commit prefixes (`feat:`, `fix:`, `docs:`, etc.), and scanning outbound commit messages and author/committer identities for exposed secrets.
  - *Post-Commit Cryptographic Verification*: Asserts commit parent SHA equals approved `base_commit_sha`, commit tree SHA equals workspace tree SHA, touched files match `allowed_files` via `git diff-tree`, and workspace working tree is clean.
  - *Real-Time Operator Approval Re-Validation (`forge.publication.staging`)*: Directly queries authoritative `ApprovalDecisionRecord` from SQLite storage and re-validates approval status, expiration, and artifact binding immediately before branch creation and immediately before commit creation. Revoked or expired approvals abort staging immediately.
  - *Typed SQLite Persistence & Audit Ledger (`forge.storage.database`, `forge.publication.storage`)*: Added `publication_staged_commits` table and indices to SQLite schema (Schema Version 3). Implemented `StagedCommitRecord` persistence in `PublicationStore` and recorded sequential, secret-redacted audit events (`workspace_provisioned`, `branch_created`, `patch_applied`, `commit_created`, `commit_verified`) with `RepairActor.ORCHESTRATOR` in `repair_execution_events`.
  - *CLI Commands (`forge.cli`)*: Added `forge repair publish stage`, `forge repair publish commit`, `forge repair publish status`, `forge repair publish inspect`, and top-level aliases `forge publish stage`, `forge publish status`, `forge publish inspect`.
  - *Dedicated Specification ([`docs/specs/phase-9-m2-isolated-branch-staging.md`](docs/specs/phase-9-m2-isolated-branch-staging.md))*: Formal architecture specification detailing workspace isolation, deterministic branch naming, commit construction, post-commit verification, and SQLite schemas.
  - *Verification Suite*: 25 unit tests in `tests/unit/test_phase9_m2_branch_staging.py` and 1,331 tests across the full pytest suite passing with zero regressions.
  - *Scope Boundary*: Staging and commit creation execute strictly inside isolated scratch publication workspaces; automated Git pushes to remote repositories, pull-request creation, and merges remain strictly forbidden. Pull request creation is deferred to Milestone 3.

### Phase 9, Milestone 1: Git Integration Architecture & Publication Policy (Completed)
- **Objective & Scope**: Design and implement the architectural foundation for safe Git publication of verified, operator-authorized candidate repairs, establishing Git integration contracts, publication policies, repository identity verification, authorization checks, planning models, and SQLite persistence.
- **Architectural Deliverables**:
  - *Defensive Git Inspection Engine (`forge.publication.inspector`)*: Implemented `GitRepositoryInspector` querying target repository metadata (canonical path, head commit SHA, current branch, remote URLs, clean working tree status) with hooks disabled (`-c core.hooksPath=/dev/null`), read-only queries, and safe directory configurations.
  - *Fail-Closed Publication Policies (`forge.publication.policy`)*: Enforced default-deny publication policies strictly forbidding force pushes (`allow_force_push=False`) and history rewrites (`allow_history_rewrite=False`). Protected branch defenses unconditionally reject target branches matching protected patterns (`main`, `master`, `release/*`, `production/*`, `stable/*`). Branch name syntax guards enforce `^[a-zA-Z0-9._/-]+$`, `forge/` prefix, and reject dangerous Git ref characters.
  - *Authorization Compatibility & Anti-Escalation Architecture (`forge.publication.authorization`)*: Implemented formal authorization mapping and compatibility layer between Phase 8 M6 operator approvals (`AuthorizedAction`) and Phase 9 publication actions (`PublicationAction`). Guarantees `MANUAL_INTEGRATION` strictly authorizes patch export only; `STAGE_FOR_PR` and `STAGE_BRANCH` authorize local branch staging only and strictly refuse remote `CREATE_PR` without exception; remote PR publication unconditionally requires a fresh explicit `CREATE_PR` operator approval; prerequisite local operations within a `CREATE_PR` workflow are permitted and audited separately; shortcut aliases and database tampering attempts are prevented from privilege escalation. 100% backward compatible with historical database records.
  - *Outbound Secret Scanning (`forge.publication.policy`)*: Implemented regex-based secret scanning inspecting commit messages, branch names, and publication metadata for exposed credentials (`sk-`, `ghp_`, `AKIA`, private keys), rejecting contaminated publications.
  - *Independent Publication Precondition Validator (`forge.publication.validator`)*: Implemented `GitPublicationValidator` validating that: (1) target repair exists in `VERIFIED` state, (2) an active, non-expired `APPROVED` decision record exists from Phase 8 M6, (3) the requested publication action matches the authorized action (preventing privilege escalation), (4) exact cryptographic artifact bindings match (`patch_sha256`, `base_commit_sha`, repository identity), (5) target repository working tree is clean (`git status --porcelain`), and (6) all outbound content is free of secrets.
  - *Deterministic Publication Planner (`forge.publication.planner`)*: Implemented `GitPublicationPlanner` formulating complete, deterministic `PublicationPlan` records with structured commit messages, author/committer attestation, and proposed branch names without executing any mutations on the target repository or creating remote branches.
  - *Typed SQLite Persistence & Audit Ledger (`forge.storage.database`, `forge.publication.storage`)*: Added `publication_requests` and `publication_plans` tables and indices to SQLite schema. Implemented persistence in `PublicationStore` and recorded sequential, secret-redacted audit events in `repair_execution_events`.
  - *CLI Commands (`forge.cli`)*: Added `forge repair publish plan`, `forge repair publish validate`, `forge repair publish show`, `forge repair publish list`, and top-level alias `forge publish ...`.
  - *Dedicated Specification ([`docs/specs/phase-9-m1-git-integration-architecture.md`](docs/specs/phase-9-m1-git-integration-architecture.md))*: Formal architecture specification detailing publication contracts, threat model, authorization compatibility mapping, invariant enforcement, and SQLite schemas.
  - *Verification Suite*: 51 unit & security tests across `tests/unit/test_phase9_m1_git_publication.py` (34 tests) and `tests/unit/test_phase9_authorization_compatibility.py` (17 tests) passing with zero regressions.
  - *Scope Boundary*: Planning and validation only; no target repository mutations, no local or remote branch creation, no PR opening, and no code merges. Branch staging is deferred to Milestone 2; PR creation is deferred to Milestone 3.

### Phase 8, Milestone 6: Operator Review, Approval & Authorization Workflows (Completed)
- **Objective & Scope**: Build an explicit, operator-in-the-loop review, approval, and authorization workflow for candidate repairs that have completed independent patch validation (M4) and sandbox regression verification (M5), enforcing strict artifact binding, dynamic tamper invalidation, and fail-closed human authorization boundaries.
- **Architectural Deliverables**:
  - *Factual Review Dossier Engine (`forge.repair.execution.dossier`)*: Implemented `ReviewDossierBuilder` synthesizing verified audit records into an immutable `ReviewDossier` containing repository target, pinned base commit, finding details, candidate patch diff, size metrics, M4 validation outcomes, M5 sandbox verification outcomes, dual-workspace comparative regression results, and traceable audit facts. AI-generated explanations are strictly segregated from verified facts.
  - *Artifact-Bound Authorization Matrix (`forge.repair.execution.approval`)*: Approvals are cryptographically and deterministically bound to an exact 11-tuple: `(repair_id, finding_id, repository_target, base_commit_sha, patch_artifact_id, patch_sha256, validation_id, verification_id, dossier_id, dossier_version, policy_version)`. Approvals are never generic or transferable across candidate revisions.
  - *Dynamic Invalidation on Drift & Expiration*: Any modification to the underlying patch artifact, workspace content, base commit revision, or expiration of the configured time-to-live (TTL) immediately invalidates the approval (`ApprovalState.INVALIDATED` or `ApprovalState.EXPIRED`).
  - *Guarded Approval State Machine (`forge.repair.execution.approval`)*: Enforces strict lifecycle transitions across `PENDING_REVIEW`, `APPROVED`, `REJECTED`, `CHANGES_REQUESTED`, `WITHDRAWN`, `EXPIRED`, and `INVALIDATED`, preventing duplicate decisions or transitions out of terminal review states.
  - *Preconditions Enforcement Gate*: Approvals are strictly refused unless the repair is `VERIFIED`, M4 validation is `ACCEPTED`, M5 sandbox verification is `VERIFIED`, patch SHA-256 matches all records, pinned base commit SHA matches, 0 regressions are detected, the defect is demonstrably resolved, operator identity is authenticated, and a non-empty justification reason is supplied.
  - *Human Authorization Boundary*: Approved repairs record an explicit `AuthorizedAction` (`EXPORT_PATCH`, `STAGE_FOR_PR`, or `MANUAL_INTEGRATION`). FORGE strictly refuses to automatically modify target repositories, run `git commit`, run `git push`, open PRs, merge code, or deploy software.
  - *Typed SQLite Persistence & Audit Ledger (`forge.storage.database`, `forge.repair.execution.storage`)*: Added `repair_review_dossiers` and `repair_approval_decisions` database tables and indices. Implemented persistence in `RepairExecutionStore` and recorded immutable, secret-redacted audit events (`operator_decision_submitted`, `approval_invalidated`) in `repair_execution_events`.
  - *CLI Operator Commands (`forge.cli`)*: Added `forge repair review`, `forge repair approve`, `forge repair reject`, `forge repair request-changes`, and `forge repair approval-history`.
  - *Dedicated Specification ([`docs/specs/phase-8-m6-operator-approval.md`](docs/specs/phase-8-m6-operator-approval.md))*: Formal architecture specification detailing review dossier schemas, approval lifecycle, binding invariants, and tamper invalidation workflows.
  - *Verification Suite*: 33 unit tests in `tests/unit/test_phase8_m6_operator_approval.py` and 1,306 tests across the full pytest suite passing with zero regressions.

### Phase 8, Milestone 5: Sandbox Repair Verification & Regression Testing (Completed)
- **Objective & Scope**: Build a production-quality sandbox verification and regression testing engine that executes dual-workspace comparative analysis in isolated Docker containers, verifying defect resolution without introducing regressions while strictly enforcing fail-closed container boundaries and zero target repository mutation.
- **Architectural Deliverables**:
  - *Strict Fail-Closed Sandbox Execution (`forge.repair.execution.verifier`)*: All verification test commands execute strictly within non-root Docker containers (`1000:1000`, `--network none`, dropped capabilities, resource limits). If Docker is unavailable, runtime execution is refused without host fallback, classifying the run as `BLOCKED` with `FailureCategory.SANDBOX_UNAVAILABLE`.
  - *Independent Validation Preconditions Gate*: Enforces mandatory M4 validation status `ACCEPTED`, verifies patch content SHA-256 integrity, verifies base commit SHA consistency across request and patch, verifies workspace path containment, and asserts target repository byte-for-byte immutability.
  - *Dual-Workspace Comparative Regression Analysis*: Provisions a clean baseline scratch workspace alongside the patched workspace at the exact base commit SHA. Executes selected test suites across both environments, categorizing test results into `FIXED`, `STABLE_PASS`, `PRE_EXISTING_FAILURE`, `NEW_REGRESSION`, `NEW_ERROR`, or `INCONCLUSIVE`.
  - *Zero-Fabrication Outcome Determination*: Candidates are granted `VerificationOutcome.VERIFIED` only when all preconditions pass, relevant tests genuinely execute, required defect reproduction checks pass, and zero new regressions or timeouts occur. Zero-test collections or empty outputs are classified as `INCONCLUSIVE`, never success.
  - *Deterministic Test Selection & Shell Injection Defense*: `TestSelector` discovers defect reproduction tests from `EvidenceRecord`s and module unit tests corresponding to modified files. Repair plan custom test commands are vetted against binary whitelists (`pytest`, `python`, `unittest`); arbitrary shell strings, chaining (`&&`, `||`, `;`), backticks, and command substitutions are rejected with `ArbitraryExecutionForbiddenError`.
  - *Evidence Generation & Report Synthesis*: Generates concrete `EvidenceRecord`s for reproduction and regression test outputs, attaching them to the finding. Produces structured Markdown and JSON verification summaries detailing baseline vs. patched comparisons, failure diagnostics, and duration metrics.
  - *Typed SQLite Persistence & Audit Event Ledger (`forge.storage.database`, `forge.repair.execution.storage`)*: Added `repair_verification_runs` and `repair_test_executions` tables and indices to SQLite schema. Implemented run and execution record persistence in `RepairExecutionStore` and recorded sequential, secret-redacted audit events (`sandbox_verification_completed`) in `repair_execution_events`.
  - *Lifecycle State Machine Transition (`forge.repair.execution.coordinator`)*: Integrated `execute_repair_verification` into `RepairExecutionCoordinator`, transitioning `RepairRequest` from `PATCH_VALIDATING` to `VERIFIED` upon complete verification, or to `FAILED` with categorized failure classification (`FailureCategory.REGRESSION_DETECTED`, `FailureCategory.TEST_FAILED`, `FailureCategory.TIMEOUT`, `FailureCategory.SANDBOX_UNAVAILABLE`).
  - *Dedicated Specification ([`docs/specs/phase-8-m5-repair-verification.md`](docs/specs/phase-8-m5-repair-verification.md))*: Formal architecture specification detailing dual-workspace comparison, outcome determination rules, test selection contracts, and SQLite storage schemas.
  - *Verification Suite*: 26 unit tests in `tests/unit/test_phase8_m5_verification.py` and 1,273 tests across the full pytest suite passing with zero regressions.
  - *Scope Boundary*: Patches are verified strictly within isolated scratch workspaces; automated Git commits, pushes, branches, PRs, merges, and deployments on target repositories remain strictly forbidden. Operator review and approval workflows remain deferred to Milestone 6.

### Phase 8, Milestone 4: Independent Patch Validation & Scope Enforcement (Completed)
- **Objective & Scope**: Build an independent, defense-in-depth patch validation and scope enforcement layer ensuring candidate patches are independently evaluated against cryptographic provenance, authorization scope, adversarial diff constructs, and Python AST semantic invariants before safe workspace application.
- **Architectural Deliverables**:
  - *Cryptographic & Provenance Integrity (`forge.repair.execution.validator`)*: Implemented `IndependentPatchValidator` (and enhanced `PatchValidator`) calculating exact SHA-256 patch bytes, matching artifact ID determinism (`patch_<sha256[:12]>`), validating repair ID and finding ID associations, confirming base commit SHA consistency across request, patch, and workspace, verifying workspace identity, and auditing workspace baseline file hash manifests to detect pre-application drift.
  - *Strict Scope Containment & Safety Boundaries*: Restricted touched files strictly to declared `allowed_files`; rejected path traversal (`..`), absolute paths, symlink escapes, protected paths (`.git/`, `.env`, `AGENTS.md`, `pyproject.toml`), unexpected file creations (`--- /dev/null`), and unexpected file deletions (`+++ /dev/null`). Enforced budget caps ($\le 1$ file, $\le 100$ lines changed, $\le 5$ hunks, $\le 20$ KB).
  - *Adversarial Diff & Syntax Invariant Enforcement*: Rejects null bytes (`\x00`), disallowed ASCII control characters, Git binary diffs (`GIT binary patch`), Git file mode modifications (`new file mode`), file renames/copies (`rename from`), non-positive line offsets ($\le 0$), duplicate hunks, overlapping hunks, and secret/credential introduction in additions (`sk-`, `ghp_`, `AKIA`, private keys, passwords).
  - *Python AST & Semantic Invariant Hardening (`forge.repair.semantic_verifier`)*: Strengthened structural Python AST checks: catches stripped validation guards (`BypassedGuardInfo`), weakened authorization logic (replacing conditionals with constant `True` or `1`), stripped `try/except` blocks or silenced exception handlers (`except: pass`), forbidden dangerous imports (`subprocess`, `socket`, `pty`, `ctypes`, `pickle`, `shutil`, `importlib`), dynamic execution calls (`eval`, `exec`, `os.system`), public API deletions, public parameter removals, type annotation removals, and module-level execution side effects upon import.
  - *Typed Diagnostics & Failure Classification (`forge.repair.execution.models`, `forge.repair.execution.failures`)*: Added `ValidationOutcome` enum (`ACCEPTED`, `REJECTED`, `INCONCLUSIVE`, `ERROR`), extended `FailureCategory` (`INTEGRITY_ERROR`, `ADVERSARIAL_DEFECT`, `SEMANTIC_VIOLATION`), created `IndependentValidationResult` capturing fine-grained diagnostic breakdowns (`scope_result`, `integrity_result`, `semantic_result`), and specialized failure classes (`PatchIntegrityError`, `PatchScopeViolationError`, `PatchAdversarialDefectError`, `PatchSemanticInvariantError`).
  - *Persistent Storage & Lifecycle Progression (`forge.storage.database`, `forge.repair.execution.storage`, `forge.repair.execution.coordinator`)*: Added `repair_validation_results` table and indices to SQLite schema. Implemented `save_validation_result`, `get_validation_result`, `get_validation_result_for_patch`, and `list_validation_results_for_repair` in `RepairExecutionStore`. Updated `RepairExecutionCoordinator` Step 5 to save validation results, log `patch_independently_validated` events, transition to `FAILED` with fine-grained error categories upon failure, and remain in `PATCH_VALIDATING` awaiting Milestone 5 verification.
  - *Dedicated Specification ([`docs/specs/phase-8-m4-independent-patch-validation.md`](docs/specs/phase-8-m4-independent-patch-validation.md))*: Complete architecture specification detailing validation layers, threat vectors, invariant enforcement rules, and audit schema.
  - *Verification Suite*: 33 unit tests in `tests/unit/test_phase8_m4_patch_validation.py` and 1,247 tests across the full pytest suite passing with zero regressions.
  - *Scope Boundary*: Patches are validated independently and applied strictly within isolated scratch workspaces; automated Git commits, pushes, branches, PRs, merges, and deployments on target repositories remain strictly forbidden. Transitions to `VERIFIED` state remain deferred to Milestone 5.


### Phase 8, Milestone 3: Evidence-Grounded Candidate Patch Generation (Completed)
- **Objective & Scope**: Implement controlled, evidence-grounded candidate patch generation using validated findings, persisted source evidence, repair safety policies, isolated scratch workspaces, and multi-layer patch validation.
- **Architectural Deliverables**:
  - *Precondition & Provenance Validation (`forge.repair.execution.validator`)*: Implemented `RepairRequestValidator` enforcing 10 precondition checks: finding existence, eligible lifecycle states (`SUSPECTED`, `REPRODUCED`, `ROOT_CAUSED`), commit SHA matching, substantive evidence provenance, workspace path containment, `allowed_files` compliance, protected path blocking, and workspace baseline integrity.
  - *Evidence-Grounded Planning (`forge.repair.execution.planner`)*: Implemented `RepairExecutionPlanner` formulating typed `RepairExecutionPlan` with verified facts separated from hypotheses, single target file scope, and minimal repair boundaries.
  - *Candidate Patch Generator (`forge.repair.execution.generator`)*: Implemented `RepairPatchGenerator` invoking local Ollama (`qwen2.5-coder:1.5b`) with zero cloud fallback, strict context budgeting, unified diff formatting, single-file scope defense, and budget caps ($\le 100$ lines, $\le 5$ hunks, $\le 1$ file, $\le 20$ KB). Supports deterministic synthesis for testing.
  - *Multi-Layer Patch Validator (`forge.repair.execution.validator`)*: Implemented `PatchValidator` validating unified diff headers, hunk marker consistency, exact context matching against isolated workspace files at the base revision, in-memory Python AST syntax validation, and static semantic invariants (`collect_unreachable_statements`, `_check_validation_guards`, `_check_docstring_integrity`).
  - *Safe In-Workspace Patch Applier (`forge.repair.execution.applier`)*: Implemented `SafePatchApplier` with pre- and post-application target repository immutability verification, in-memory backup, atomic rollback on failure, and workspace integrity diffing.
  - *Lifecycle Coordinator (`forge.repair.execution.coordinator`)*: Implemented `RepairExecutionCoordinator` orchestrating state progression (`CREATED -> PLANNED -> VALIDATING -> READY -> EXECUTING -> PATCH_GENERATED -> PATCH_VALIDATING`), guarded rollback to `FAILED` on validation errors, and zero premature transitions to `VERIFIED` (verification is reserved for Milestone 5).
  - *Persistent Storage & Schema Migration (`forge.storage.database`, `forge.repair.execution.storage`)*: Added `repair_execution_plans` table and index to SQLite schema. Integrated plan persistence and retrieval into `RepairExecutionStore`.
  - *Dedicated Specification ([`docs/specs/phase-8-m3-candidate-patch-generation.md`](docs/specs/phase-8-m3-candidate-patch-generation.md))*: Formal architecture specification detailing patch generation contracts, AST validation rules, semantic invariant enforcement, and safe application.
  - *Verification Suite*: 23 dedicated unit tests in `tests/unit/test_phase8_m3_patch_generation.py` and 1,214 tests across the full pytest suite passing with zero regressions.
  - *Scope Boundary*: Patches are applied strictly inside isolated disposable scratch workspaces; automated Git commits, pushes, branches, PRs, merges, and deployments on target repositories remain strictly forbidden. Multi-check candidate patch verification remains deferred to Milestone 5.

### Phase 8, Milestone 2: Isolated Workspace Management & Sandbox Integration (Completed)
- **Objective & Scope**: Implement secure, isolated scratch workspace provisioning, exact Git commit identity verification, baseline filesystem integrity auditing, and Docker sandbox integration with fail-closed guarantees for evidence-grounded repair execution.
- **Architectural Deliverables**:
  - *Isolated Scratch Workspace Engine (`forge.repair.execution.workspace`)*: Engineered `RepairWorkspace` implementing `RepairWorkspaceProtocol` with physical directory isolation outside the target repository, exact `git rev-parse HEAD` commit verification, scope containment, and symlink escape defenses.
  - *Filesystem Baseline Fingerprinting & Integrity Auditing*: Captured SHA-256 baseline file manifests upon workspace preparation. Implemented `check_integrity()` to detect modified, added, deleted, or symlinked files, rejecting unauthorized alterations outside declared `allowed_files` with `WorkspaceIntegrityViolationError`.
  - *Docker Sandbox Integration with Configurable Mounts (`forge.sandbox.docker`)*: Updated `DockerSandbox` to support configurable read-only vs writable workspace bind mounts (`readonly_opt`) while maintaining non-root execution (`UID:GID 1000:1000`), network isolation (`--network none`), dropped capabilities (`--cap-drop ALL`, `no-new-privileges`), and strict resource limits (CPU quota, memory caps, timeout bounds, and PIDs limit).
  - *Fail-Closed Boundary & Structured Failures (`forge.repair.execution.failures`)*: Established structured failure hierarchy: `WorkspaceExecutionError`, `WorkspaceProvisioningError`, `RepositoryAcquisitionError`, `CommitMismatchError`, `WorkspaceCollisionError`, `WorkspaceReuseForbiddenError`, `WorkspaceIntegrityViolationError`, `SandboxUnavailableError`, `SandboxInitializationError`, `ResourceLimitViolationError`, and `WorkspaceCleanupError`. When Docker is unavailable, runtime execution is strictly refused without host fallback.
  - *Workspace Manager Orchestrator (`forge.repair.execution.manager`)*: Implemented `RepairWorkspaceManager` coordinating end-to-end workspace provisioning, isolation checks, sandbox command execution, integrity verification, and directory cleanup.
  - *Persistent Storage & Audit Ledger (`forge.storage.database`, `forge.repair.execution.storage`)*: Added `repair_workspaces` table and indices to SQLite schema. Implemented `WorkspaceRecord` persistence and recorded sequential audit events (`workspace_provisioned`, `workspace_integrity_verified`, `sandbox_command_executed`, `workspace_cleaned`) in `repair_execution_events`.
  - *Dedicated Specification ([`docs/specs/phase-8-m2-workspace-sandbox-integration.md`](docs/specs/phase-8-m2-workspace-sandbox-integration.md))*: Formal architecture specification detailing workspace lifecycle, containment policies, integrity checks, and sandbox integration contracts.
  - *Verification Suite*: 25 unit tests in `tests/unit/test_phase8_m2_workspace_sandbox_integration.py` and 1,191 tests across full pytest suite passing with zero regressions.
  - *Scope Boundary*: Actual candidate source-code patch generation remains deferred to Milestone 3; automated commits, pushes, branches, PRs, and software deployments on target repositories remain strictly forbidden.

### Phase 8, Milestone 1: Repair Execution Architecture (Completed)
- **Objective & Scope**: Establish the typed domain models, guarded state machine, strict safety boundaries, component protocols, and SQLite audit event ledgers for controlled, evidence-grounded software repair execution.
- **Architectural Deliverables**:
  - *Repair Execution Domain Models (`forge.repair.execution.models`)*: Formulated explicit models for `RepairRequest`, `RepairExecutionState`, `RepairActor`, `FailureCategory`, `FailureClassification`, `PatchArtifact` (with SHA-256 content hash, line counts, and hunk metrics), `ValidationResult`, `ExecutionTestResult`, and `RepairExecutionPlan`.
  - *Guarded Lifecycle State Machine (`forge.repair.execution.state`)*: Implemented `RepairExecutionStateMachine` enforcing strict transition gating across 11 explicit states (`CREATED` -> `PLANNED` -> `VALIDATING` -> `READY` -> `EXECUTING` -> `PATCH_GENERATED` -> `PATCH_VALIDATING` -> `VERIFIED` / `FAILED` / `REJECTED` / `CANCELLED`), role-based actor authorization matrix, immutable terminal states, and zero-fabrication verification invariants.
  - *Strict Safety Policies & Boundary Enforcement (`forge.repair.execution.policy`)*: Enforced mandatory safety policies: target repository immutability (`target_repo_read_only=True`), isolated workspace execution, command binary whitelisting (`pytest`, `python`, `unittest`, `flake8`, `ruff`, `mypy`), shell injection defense (blocking `;`, `|`, `&`, `$`, backticks, redirection, newlines), environment variable sanitization with secret redaction, path traversal rejection, protected path blocking (`ProtectedPathPolicy`), and patch resource budgets ($\le 100$ lines, $\le 5$ hunks, $\le 1$ file, $\le 20$ KB).
  - *Component Protocols (`forge.repair.execution.interfaces`)*: Defined runtime-checkable protocols for `RepairPlannerProtocol`, `RepairExecutorProtocol`, `RepairWorkspaceProtocol`, `PatchValidatorProtocol`, `RepairVerifierProtocol`, and `RepairEventStoreProtocol`.
  - *Isolated Scratch Workspace (`forge.repair.execution.workspace`)*: Engineered `RepairWorkspace` implementing strict directory isolation, target repository immutability verification, workspace path containment checks, and symlink escape defenses.
  - *SQLite Persistence & Audit Event Ledger (`forge.repair.execution.storage`)*: Added `repair_executions`, `repair_execution_events`, and `patch_artifacts` database tables in SQLite (`database.py`) and implemented `RepairExecutionStore` recording sequential, secret-redacted audit events on every lifecycle state change.
  - *Dedicated Specification ([`docs/specs/phase-8-m1-repair-execution-architecture.md`](docs/specs/phase-8-m1-repair-execution-architecture.md))*: Complete architectural design, state transition diagrams, actor authorization matrices, safety policies, and storage schemas.
  - *Verification Suite*: 16 targeted unit tests in `tests/unit/test_phase8_m1_repair_execution_architecture.py` and 1,166 tests across the full pytest suite passing with zero regressions.
  - *Scope Boundary*: Actual candidate patch generation is deferred to Milestone 3; automated commits, pushes, and PRs are strictly forbidden.

### Phase 7: Evidence-Driven Investigation Engine (Completed)
- **Objective & Scope**: Harden the autonomous investigation engine against false positives, test assertion misinterpretations, and unverified vulnerability claims exposed during live repository pilots (specifically reproducing and resolving the FastAPI pilot failure case `run_8120cd03f318` on commit `45cb2252c737906f37544f5ba6bee0295fb81468`).
- **Architectural & Empirical Deliverables**:
  - *Cross-File Investigation & Dependency Tools*: Engineered `inspect_dependencies` (parsing workspace `pyproject.toml`, `requirements*.txt`, and `setup.cfg` via stdlib `tomllib`) and `find_related_files` (discovering cross-file relationships between test suites, implementation files, and dependency manifests) in `ToolRegistry`.
  - *Deterministic Contradiction Detection Engine*: Implemented multi-layered verification in `app/forge/analysis/validation.py`:
    - `validate_dependency_claim`: Deterministically rejects false claims alleging outdated, unsupported, or missing dependencies when project manifests contradict them.
    - `validate_test_file_claim`: Rejects test-assertion misinterpretations where negative error-handling tests (e.g. `pytest.raises(ValueError)`) are falsely reported as defects, and blocks test-name-based false positives.
    - `validate_implementation_contradiction`: Discovers related implementation files and detects explicit input validation logic contradicting "missing validation" allegations.
    - `validate_claim_evidence_relevance`: Validates semantic tokens ensuring cited source code ranges match claimed defect categories (e.g. SQL query operations for SQL injection claims).
  - *Severity Justification & Confidence Calibration*: Engineered `validate_severity_and_impact`, enforcing that HIGH and CRITICAL severities require concrete failure modes or demonstrable exploitability. Speculative security claims lacking attack vectors are rejected with `SeverityJustificationError`. Test files cannot support high-severity vulnerabilities without demonstrable production exploitability. Confidence is strictly calibrated, preserving uncertainty and limitations annotations.
  - *Transparent Agent Loop & Report Synthesis*: Enriched `InvestigationLoop` to track all inspected files and rejected candidate findings. Reserved synthesis turn ensures operator transparency, reporting rejected claim counts and validation failure details in JSON and Markdown reports.
  - *Failed Pilot Run Forensic Analysis & Engine Remediation (`run_ce01ff96700b`)*:
    - Diagnosed live investigation failure against FastAPI (`qwen2.5-coder:1.5b` via Ollama) which terminated at Turn 5 after an exact 120.0s gap. Reconstructed the execution path: `OpenAICompatibleProvider` defaulted to a 60s timeout with `max_retries=1`, meaning a single stalled generation attempt timed out twice before raising `LLMUnavailableError`.
    - Added configurable request timeout (`llm_timeout_seconds` in `ForgeSettings`, default `120.0s`) and distinct `LLMTimeoutError` exception classification.
    - Standardized structured failure event logging (`EventType.RUN_FAILED`) containing failure category, sanitized error message, execution stage, turn number, retryable flag, and UTC timestamp.
    - Surfaced failure details and synthesis assessments directly in `InvestigationReport` (JSON and Markdown) while guaranteeing that partial investigation history (tool calls, findings, evidence) is preserved upon failure.
    - Enforced synthesis integrity invariant: runs cannot be marked `completed` if final synthesis did not succeed.
  - *Investigation Intelligence & Synthesis Remediation (`run_747548f2008e` & `run_d7744d583e06`)*:
    - **Semantic Duplicate Claim Tracking**: Engineered `RejectedClaimTracker` to intercept exact duplicate claims and semantically equivalent variations with altered titles or categories, while allowing corrected submissions with downgraded severity or substantive code lines.
    - **Configurable Rejection Budget**: Added `rejection_budget` (default 3, configurable via `FORGE_REJECTION_BUDGET`). When exhausted, `record_finding` tool calls raise `RejectionBudgetExceededError`, and the tool is dynamically pruned from the model's active tool schema, guiding the agent to code/manifest inspection or final synthesis.
    - **Synthesis Quality Gate**: Engineered `validate_synthesis_quality` to reject conversational apologies (e.g. "I apologize for the confusion..."), generic chat fillers, and raw unparsed JSON tool call dumps.
    - **Synthesis Retry & Deterministic Fallback**: Automatic synthesis retry turn prompts the model for structured technical synthesis upon low-quality output. If retry fails, the engine deterministically generates a verifiable fallback synthesis labeled `[System-Generated Fallback Synthesis]` based on actual inspected files, accepted findings, and rejected claims.
    - **Investigation Quality Classification**: Decoupled execution status (`completed`, `failed`, `max_steps_exceeded`) from technical quality status (`completed`, `completed_with_limitations`, `insufficient_evidence`, `failed`).
    - **Report Transparency**: Enhanced JSON and Markdown reports to expose `investigation_quality`, `files_inspected`, `rejected_claims`, and explicit limitations.
  - *Investigation Orchestration & Tool-Selection Remediation (`run_f22cf284a17b`)*:
    - **Deterministic Investigation Planner (`InvestigationPlanner`)**: Generically schedules inspection across workspace manifests, core implementation modules, and relevant tests using symbol indexing without hardcoded repository paths.
    - **Utility Call Quotas & Dynamic Tool Pruning**: Enforces strict quotas on zero-argument local database utilities (`get_run_history`, capped at 1), dynamically pruning exhausted utilities from provider tool schemas to eliminate model looping on non-inspection tools.
    - **Autonomous Steering & Anti-Premature Termination**: Eliminates premature loop exits on tool-free turns when planned files remain uninspected. Injects structured steering directives and autonomous step fallbacks.
    - **Uninspected File Finding Protection**: Enforces that candidate finding files must have been previously inspected via `read_source_excerpt`, preventing speculative claims on unexamined files.
  - *Repository-Scoped Evidence Integrity & Quality-Gate Enforcement (`run_0a293dd14f45`)*:
    - **Commit-Pinned Target Acquisition**: Added `--commit` / `-c` (shallow single-commit fetch) and `--instruction` / `-i` flags to `forge investigate` to isolate audit targets against remote branch drift.
    - **Deterministic Path Containment & Existence Validation**: Engineered `validate_repository_path` and `RepositoryPathValidationError` in `app/forge/analysis/validation.py`, verifying workspace containment, rejecting path traversal (`../`), blocking symlink escapes, and rejecting non-existent files. Finding claims on uninspected or non-existent files are rejected with `RepositoryPathValidationError`, recorded in `rejected_claims`, and never added to `inspected_files`.
    - **Repository-Scoped Provenance & Evidence Visibility**: Attached target `commit_sha`, `repository_target`, `operation`, and SHA-256 `content_hash` to all `EvidenceRecord`s. Added `ForgeStore.list_evidence_for_run(run_id)` and enhanced Markdown and JSON reports to render verified evidence records even when zero findings are reported.
    - **Strict Investigation Quality Invariants**: Enforced invariants in `determine_investigation_quality` strictly downgrading investigations to `INSUFFICIENT_EVIDENCE` when zero substantive source files are inspected or zero evidence records are generated.
  - *Dedicated Specification ([`docs/specs/phase-7-evidence-driven-investigation-engine.md`](docs/specs/phase-7-evidence-driven-investigation-engine.md))*: Complete architectural design, error taxonomy, validation matrices, pilot failure root-cause analysis, synthesis quality, orchestration specifications, and repository-scoped evidence integrity.
  - *Comprehensive Regression Suite ([`tests/unit/test_phase7_investigation_evidence_validation.py`](tests/unit/test_phase7_investigation_evidence_validation.py), [`tests/unit/test_phase7_failed_run_root_cause.py`](tests/unit/test_phase7_failed_run_root_cause.py), [`tests/unit/test_phase7_investigation_intelligence.py`](tests/unit/test_phase7_investigation_intelligence.py), [`tests/unit/test_phase7_investigation_orchestration.py`](tests/unit/test_phase7_investigation_orchestration.py), [`tests/unit/test_phase7_evidence_integrity.py`](tests/unit/test_phase7_evidence_integrity.py))*: 78 targeted regression tests (10 claim validation + 12 failure reliability + 17 intelligence & synthesis + 18 orchestration & tool-selection + 21 evidence integrity & quality gates) passing cleanly with full test suite passing with zero regressions (1,150 unit tests).
  - *Final Live Acceptance Verification (`run_f8529a6e6791`)*: Empirically validated on FastAPI (`https://github.com/fastapi/fastapi.git`, pinned commit `45cb2252c737906f37544f5ba6bee0295fb81468`) via Ollama (`qwen2.5-coder:1.5b`) with zero cloud fallback:
    - 12 steps executed, 5 tool calls completed, 101.3s duration.
    - All 3 mandatory target files inspected: `pyproject.toml`, `fastapi/datastructures.py`, and `tests/test_datastructures.py`.
    - 4 verifiable `EvidenceRecord`s persisted in SQLite with commit SHA `45cb2252c737906f37544f5ba6bee0295fb81468`, file path, operation, and SHA-256 `content_hash`.
    - Zero false positive findings recorded (evaluating historical claim `find_42b375ce6f2a` as a passing negative test validating expected error rejection rather than a defect).
    - Execution Status: `COMPLETED`.
    - Investigation Quality: `COMPLETED`.


### Phase 6, Milestone 6: Final Verification & Supervised Beta Release Gate (Completed)
- **Objective & Scope**: Execute the final end-to-end acceptance audit of FORGE across distribution packages, live container isolation, local model inference, CI matrix testing, provenance attestation, and operational recovery, determining whether version 0.1.0b1 qualifies for a limited supervised developer beta release.
- **Architectural & Empirical Deliverables**:
  - *Full Multi-Environment CI/CD Gate Verification*: Confirmed 100% test pass rate across Python 3.11, 3.12, and 3.13 in GitHub Actions (`.github/workflows/ci.yml` Run 41124332752), verifying offline regression tests (1,017 test assertions total), code compilation, `pip-audit` zero-CVE dependency enforcement, and package builds.
  - *Live Docker Container Sandbox Acceptance*: Executed live integration tests against Docker Desktop (29.9.2), empirically validating UID:GID 1000:1000 non-root execution, `--network none` socket connection blocking, read-only workspace mount with EROFS on write attempts, dropped capabilities (`NoNewPrivs=1`, `CapEff=0`), sub-second timeout enforcement, output bounding, secret scrubbing, and guaranteed zero target repository mutation.
  - *Live Local Ollama Model Acceptance*: Verified endpoint connectivity at `http://localhost:11434`, audited model inventory for `qwen2.5-coder:1.5b`, executed structured completion generation, and verified strict enforcement of disabled cloud fallback (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
  - *Clean Standalone Distribution Acceptance*: Built fresh wheel (`dist/forge_core-0.1.0b1-py3-none-any.whl`, 371 KB) and source distribution (`dist/forge_core-0.1.0b1.tar.gz`, 530 KB) and successfully installed and verified execution in clean external virtual environments outside the repository with zero `PYTHONPATH` bleed, verifying entrypoints, `forge version`, `forge doctor`, and isolated database initialization.
  - *Cryptographic Manifests & Forensic Cleanliness*: Generated canonical `dist/release_manifest.json` and standard `dist/SHA256SUMS.txt`, verified 100% checksum match, and confirmed zero forbidden files (`.env`, `*.db`, `tests/fixtures/`, `forge_pilot_*`) via `forge release verify` and standalone `scripts/verify_release_artifacts.py`.
  - *Empirical Attestation Verification & Platform Boundary Disclosure*: Executed manual release build workflow on GitHub Actions (`.github/workflows/release-build.yml` Run 41124601553). Disclosed and documented GitHub platform restriction on user-owned private repositories regarding Sigstore provenance persistence (`actions/attest-build-provenance`), providing an honest assessment without fabricating verification evidence.
  - *Dedicated Milestone 6 Specification ([`docs/specs/phase-6-m6-final-verification-release-gate.md`](docs/specs/phase-6-m6-final-verification-release-gate.md))*: Formal acceptance matrix, threat-model reassessment, and release readiness audit.
  - *Absolute Release Authorization Invariant*: Zero Git tags created; zero public releases published. Awaiting explicit human authorization.
  - *Release Decision*: **CONDITIONAL GO** (Approved for supervised internal developer beta; public release gated on repository visibility transition to enable public Sigstore OIDC attestations).

### Phase 6, Milestone 5: Security Hardening, Attestation & Release Gate (Completed)
- **Objective & Scope**: Harden the FORGE software supply chain, dependency security posture, distribution artifact integrity, build provenance, and release verification gates for the supervised beta release (`0.1.0b1`).
- **Architectural Deliverables**:
  - *Comprehensive Threat Model & Security Audit ([`docs/SECURITY_THREAT_MODEL.md`](docs/SECURITY_THREAT_MODEL.md))*: Formalized threat model covering untrusted target repositories, malicious diffs, malicious LLM completions, Docker escapes, supply chain / dependency tampering, and unauthorized release distribution. Detailed concrete mitigations, fail-closed boundaries, and residual risks.
  - *Dependency & Supply Chain Hardening*: Audited runtime dependencies using `pip-audit`, resolving all vulnerabilities (0 known CVEs across all installed runtime packages). Pinned dependencies in [`requirements/constraints.txt`](requirements/constraints.txt) for deterministic builds.
  - *Release Manifest & Cryptographic Integrity Engine ([`app/forge/release/manifest.py`](app/forge/release/manifest.py))*: Engineered `ReleaseArtifact` and `ReleaseManifest` schemas, computing SHA-256 digests, file sizes, build timestamps, and generating canonical `release_manifest.json` and standard `SHA256SUMS.txt`.
  - *Forensic Archive Inspection ([`app/forge/release/verification.py`](app/forge/release/verification.py))*: Enforced strict forensic cleanliness rules for wheel (`.whl`) and sdist (`.tar.gz`) archives—forbidding embedded secrets (`.env*` except `.env.example`), test fixtures, pilot runs, and local database files (`*.db`, `*.sqlite*`).
  - *Release Verification CLI & Tooling*:
    - `forge release verify`: CLI command validating archive checksums against manifests, matching PEP 440 versions, and running forensic archive scans.
    - `forge release manifest`: CLI command to generate canonical manifests for built distribution artifacts.
    - Standalone verification script ([`scripts/verify_release_artifacts.py`](scripts/verify_release_artifacts.py)) requiring zero third-party dependencies.
  - *Hardened GitHub Actions CI/CD Workflows*:
    - [`.github/workflows/ci.yml`](.github/workflows/ci.yml): Least-privilege `contents: read` permissions, StepSecurity harden-runner, commit-pinned action SHAs, bytecode compilation, dependency auditing via `pip-audit`, offline pytest execution, artifact packaging, and verification.
    - [`.github/workflows/release-build.yml`](.github/workflows/release-build.yml): Minimal permissions (`contents: read`, `id-token: write`, `attestations: write`), building wheel/sdist, generating SHA-256 manifests, and establishing SLSA build provenance attestations via `actions/attest-build-provenance@v2`.
  - *Security & Provenance Documentation*: Updated [`docs/OPERATOR_GUIDE.md`](docs/OPERATOR_GUIDE.md) (release integrity & checksums), [`docs/BETA_CONTRACT.md`](docs/BETA_CONTRACT.md) (supply chain guarantees), [`docs/BETA_KNOWN_ISSUES.md`](docs/BETA_KNOWN_ISSUES.md) (reproducible build constraints), [`docs/specs/phase-6-roadmap.md`](docs/specs/phase-6-roadmap.md), and created dedicated milestone spec [`docs/specs/phase-6-m5-security-attestation-release-gate.md`](docs/specs/phase-6-m5-security-attestation-release-gate.md).
  - *Verification Suite*: 19 unit tests in `tests/unit/test_phase6_milestone5.py` and 1,017 tests across full test suite passing with zero regressions.

### Phase 6, Milestone 4: Operational Diagnostics & Live Environment Validation (Completed)
- **Objective & Scope**: Validate FORGE as an installed application, hardening diagnostics, verifying live dependencies and execution boundaries, and demonstrating that an operator can identify and resolve environment problems safely without false readiness claims or security regressions.
- **Architectural Deliverables**:
  - *Comprehensive Operational Diagnostics Engine ([`app/forge/diagnostics.py`](app/forge/diagnostics.py))*: Engineered structured audit probes for Python runtime version (>=3.11), Git executable availability, SQLite schema and file lock status, Docker daemon connectivity and container image caching, and LLM provider endpoint/credential configuration.
  - *Standardized Failure Classification & Badges*: Implemented deterministic `DiagnosticCategory` badges: `[OK]`, `[OPTIONAL]`, `[MISCONFIGURED]`, `[UNAVAILABLE]`, `[PERMISSION DENIED]`, `[CONNECTION FAILED]`, `[UNSUPPORTED]`, and `[UNSAFE]`, accompanied by copy-pasteable remediation guidance.
  - *Honest Tiered Readiness Evaluation*: Implemented `evaluate_system_readiness()`, categorizing operational capacity across Static Inspection, Docker Sandboxing, and Local Inference. When Docker is unavailable or permission-denied, system strictly downgrades to `[READY_FOR_STATIC_INSPECTION_ONLY]` rather than claiming full operational readiness.
  - *Dedicated CLI Diagnostic Subcommands*:
    - `forge doctor`: Composite system diagnostic auditing all runtime dependencies and displaying operational readiness tiers.
    - `forge sandbox doctor`: Container runtime diagnostic detailing isolation policies, macOS/Linux socket connectivity, and local image availability.
    - `forge provider doctor`: LLM provider diagnostic querying local Ollama `/api/tags`, verifying local model weights (`qwen2.5-coder:1.5b`), and enforcing forbidden cloud fallback.
  - *Live Docker Daemon & Local Ollama Validation*: Verified live Docker Desktop (29.9.2) container isolation properties (`tests/integration/test_docker_sandbox_integration.py`) and live local Ollama model completion (`tests/integration/test_ollama_live_validation.py`).
  - *Clean Installed-Beta Distribution Verification*: Rebuilt wheel distribution (`dist/forge_core-0.1.0b1-py3-none-any.whl`) and verified execution in clean external virtual environment (`/tmp/forge_m4_installed_test`).
  - *Milestone Specification ([`docs/specs/phase-6-m4-operational-diagnostics-validation.md`](docs/specs/phase-6-m4-operational-diagnostics-validation.md))*: Dedicated architecture specification, diagnostic taxonomy, and verification records.
  - *Full Regression Verification*: 28 unit tests in `tests/unit/test_phase6_milestone4.py` and 998 tests across full pytest suite passing with zero regressions.

### Phase 6, Milestone 3: Operator Workflow Reliability & Recovery (Completed)
- **Objective & Scope**: Harden FORGE's supervised operator workflow lifecycle against interruptions and failures, ensure idempotent recovery without loss of evidence or state corruption, unify CLI inspection experiences, and enforce human decision integrity.
- **Architectural Deliverables**:
  - *Interruption Recovery & Resumable States*: Added `AWAITING_HUMAN_REVIEW` to resumable states in `forge.workflow.state` and enabled idempotent resumption across all workflow stages (`forge workflow resume <WF_ID>`). Interrupted repair planning in `REPAIR_PENDING` without a validated plan can be safely resumed without raising transition errors.
  - *Human Review Gate & Decision Verification*: Approval in `review_workflow` validates the candidate patch SHA-256 digest against the stored unified diff, requires a passed multi-check verification result with zero regressions, and verifies that the target repository HEAD commit has not drifted away from the pinned `base_commit_sha` (raising `RevisionDriftError`). Permanent rejection records the decision in SQLite and appends an immutable event to the ledger.
  - *Candidate Immutability in Terminal States*: Workflows in `APPROVED` or `REJECTED` strictly reject candidate patch replacement or modification in `attach_repair_plan_to_workflow`.
  - *Enhanced Workflow Inspection Dashboard*: Upgraded `_render_eng_workflow_markdown` to display status categories (`[AWAITING OPERATOR REVIEW]`, `[PENDING]`, `[BLOCKED]`, `[APPROVED]`, `[REJECTED]`, `[FAILED]`), actionable next commands, candidate repair patch details (target file, lines changed, SHA-256 digest, syntax-highlighted unified diff), multi-check verification table, and monotonic event ledger timeline.
  - *CLI Usability Aliases*: Added `forge workflow show <WF_ID>` (aliasing `status`) and `forge workflow reproduce <WF_ID>` (aliasing `repro`). Added `--repo` flag to `forge workflow review` for explicit drift verification.
  - *Milestone Specification ([`docs/specs/phase-6-m3-workflow-reliability-recovery.md`](docs/specs/phase-6-m3-workflow-reliability-recovery.md))*: Comprehensive documentation of interruption recovery matrices, decision verification rules, and test verification records.
  - *Full Regression Verification*: 12 targeted unit/CLI tests in `tests/unit/test_phase6_milestone3.py` and full 970-test test suite passing with zero regressions.

### Phase 6, Milestone 2: Packaging, CLI Usability & Distribution (Completed)
- **Objective & Scope**: Package FORGE into standard Python distribution artifacts (wheel `.whl` and source distribution `.tar.gz`) enabling clean standalone installation and CLI execution outside the source repository without dependency bloat or security regressions.
- **Architectural Deliverables**:
  - *Distribution Artifact Generation & Verification*: Built pure-Python wheel (`dist/forge_core-0.1.0b1-py3-none-any.whl`, 351 KB) and source distribution (`dist/forge_core-0.1.0b1.tar.gz`, 488 KB).
  - *Strict Distribution Exclusion Policy ([`MANIFEST.in`](MANIFEST.in))*: Explicitly includes core code and documentation while barring local `.env` files, SQLite databases (`*.sqlite`, `*.db`), test fixtures, pilot directories, caches, and bytecode from distribution archives.
  - *Clean-Install Verification in Isolated Environments*: Verified installation of both wheel and sdist in temporary virtual environments (`/tmp/forge_clean_*`) outside the repository with zero `PYTHONPATH` bleed. Confirmed `forge version`, `forge doctor`, `forge db init`, and Python module imports execute cleanly without source checkout dependencies.
  - *CLI Usability & Shell Auto-Completion*: Integrated and verified Typer native shell completion for `bash`, `zsh`, `fish`, and `powershell` via `forge --install-completion` and `forge --show-completion`.
  - *Milestone Specification ([`docs/specs/phase-6-m2-packaging-distribution.md`](docs/specs/phase-6-m2-packaging-distribution.md))*: Codified distribution architecture, artifact forensics, clean-install verification records, and security invariants.

### Phase 6, Milestone 1: Beta Readiness Audit & Release Foundation (Completed)
- **Objective & Scope**: Establish the operational foundation for the limited, human-supervised developer beta of FORGE following the conclusion of Supervised Pilots 01–08.
- **Architectural Deliverables**:
  - *Supervised Beta Contract ([`docs/BETA_CONTRACT.md`](docs/BETA_CONTRACT.md))*: Codifies non-negotiable invariants: strict target repository immutability, mandatory human review gate (`awaiting_human_review`), fail-closed container sandboxing (`--network none`, read-only, non-root user), local-only model inference by default (`FORGE_ALLOW_CLOUD_FALLBACK=false`), and zero automated target code mutations, commits, PRs, merges, or deployments.
  - *Empirical Known Issues & Model Limitations ([`docs/BETA_KNOWN_ISSUES.md`](docs/BETA_KNOWN_ISSUES.md))*: Discloses observed model capabilities from Pilots 01–08. Single-statement arithmetic and logic replacements operate with high fidelity; multi-statement control-flow synthesis and cross-type statement replacements on compact local models (1.5B) require close operator oversight. Details how FORGE traps these failure modes before disk mutation.
  - *Operator Quickstart & Administration Guide ([`docs/OPERATOR_GUIDE.md`](docs/OPERATOR_GUIDE.md))*: Step-by-step instructions for installation, configuration, system diagnostics (`forge doctor`), task intake, Docker reproduction, local model repair planning, multi-check verification, human approval, and dossier export.
  - *Package Metadata & Version Alignment*: Aligned package version to PEP 440 beta version `0.1.0b1` across `pyproject.toml` and `app/forge/__init__.py`. Aligned CLI root help and `forge doctor` ready messages.
  - *Offline Test Baseline & Live Docker Validation*: Full test suite of 958 tests executes completely offline without external network or API keys. Opt-in Docker integration tests verified live container isolation properties.

### Deterministic Repair Construction Architecture & Coordinate Ownership (Completed)
- **Problem & Root Cause**: Analysis of Pilot 01–05 forensics revealed that repair candidate generation broke down when local models (`qwen2.5-coder:1.5b`) attempted to override coordinates or redeclare whole function headers (`def ...`) when defects were localized to internal statements. Because earlier proposal constructors allowed model-supplied search blocks to dictate coordinates, models could hijack coordinates to line 1 rather than the anchored defect statement at line 14.
- **Architectural Enhancements**:
  - *Typed Grounded Repair Representation (`GroundedRepairRepresentation`)*: Implemented in `app/forge/repair/deterministic_repair.py`, capturing `target_file`, `base_commit_sha`, SHA-256 `source_digest`, AST node type/identity, symbol scope, exact 1-indexed `[start_line, end_line]`, and verbatim statement text.
  - *Deterministic Coordinate Ownership & Scope Boundary Defense*: Locks coordinates to the grounded statement. If a model proposal re-declares a function/class header (`def ...`) for an internal statement anchor, it is strictly rejected as a `Scope violation` unless the search block is an exact matching function definition encompassing the anchor.
  - *Indentation & Formatting Verification*: Enforces leading whitespace matching the original statement's indentation, rejecting unindented or misaligned lines without silent normalization.
  - *AST Syntax & Static Semantic Verification*: Verifies that candidate code parses cleanly with `ast.parse` and satisfies semantic invariants (no unreachable code, no bypassed guards) before generating a diff.
  - *difflib Standard Diff Synthesis*: Synthesizes standard unified diffs via Python `difflib.unified_diff`, enforcing line and hunk budgets.
  - *Two-Tiered Proposal Validation in Planner*: `RepairPlanner.plan_repair` evaluates structured search/replace proposals through coordinate containment checks and deterministic repair grounding, propagating actionable diagnostics in retry feedback.
  - *Generalization Regression Fixture (`GEN07_nested_statement_anchor_scope`)*: Added fixture under `tests/fixtures/generalization_fixtures/` covering nested statement anchoring inside a class method with docstrings and guards (`BackoffPolicy.compute_delay`), verifying statement grounding, out-of-scope rejection, and verified execution.
  - *Fail-Secure Invariants*: Retained zero target repository mutation, Docker container sandboxing, and strict enforcement of `FORGE_ALLOW_CLOUD_FALLBACK=false`.
- **Verification**: Verified with 12 dedicated unit tests (`tests/unit/test_deterministic_repair_construction.py`), 939 full regression tests, and live local Ollama evaluation.

### Candidate Semantic Verification Hardening & AST Invariants (Completed)
- **Problem & Root Cause**: In Supervised Pilot 03, the local model proposed replacing a function definition header with an immediate `return` statement calculating the discount. While this satisfied the target positive-price test in the reproduction check, it introduced dead unreachable code, displaced the function docstring, and completely bypassed pre-existing negative-price validation guards (`if price < 0: raise ValueError(...)`). Because `ast.parse` only checks syntactic grammar and the reproduction check lacked negative-price assertions, the regression was missed and the candidate erroneously passed verification to reach `awaiting_human_review`.
- **Architectural Enhancements**:
  - *AST Reachability Analysis*: Implemented `collect_unreachable_statements` in `app/forge/repair/semantic_verifier.py` to traverse function and block statement sequences and flag any statements following unconditional terminators (`Return`, `Raise`, `Break`, `Continue`, terminating `If/Try`).
  - *Validation Guard Preservation*: Added `_check_validation_guards` to compare baseline AST functions against candidate functions, preventing guards (`If` statements raising exceptions or assertions) from being bypassed by preceding terminators or stripped from function bodies.
  - *Docstring Integrity Protection*: Added `_check_docstring_integrity` to ensure function docstrings are not displaced below executable statements.
  - *Verification Pipeline Integration*: Added `VerificationCheckType.SEMANTIC = "semantic"` to `app/forge/workflow/models.py` and integrated `semantic:ast_invariants` into `build_default_verification_plan` and `execute_verification` in `app/forge/workflow/verification.py` as check #5, strictly failing candidate verification if static semantic violations are found. Added explicit diagnostics when reproduction passes without broader regression test coverage.
  - *Generalization Regression Fixture (`GEN06_bypassed_validation_early_return`)*: Added fixture under `tests/fixtures/generalization_fixtures/` replicating the Pilot 03 bypass failure mode and verifying that early returns bypassing validation are rejected while correct minimal replacements pass.
  - *Fail-Closed Invariants*: Zero target repository mutations, Docker sandbox isolation strictly required for dynamic execution, and `FORGE_ALLOW_CLOUD_FALLBACK=false` preserved.
- **Verification**: Verified with 15 dedicated unit tests (`tests/unit/test_semantic_verifier.py`), 927 full regression tests, and bytecode compilation.

### Repair Reliability & Generalization (Completed)
- **Problem & Root Cause**: Analysis of all eight attempts of Supervised Pilot 01 revealed that while structural schema and edit-anchoring mechanisms functioned, repair generation broke down due to two interrelated issues: (1) model proposals inverted defect logic (e.g., proposing `return 0.0` or stripping guards when reproduction evidence confirmed an unhandled exception), and (2) deterministic AST statement localization contained calculator-specific biases (unconditionally selecting `ast.If` blocks) while suppressing specific anchor-grounding rejection reasons (`anch_reasons`) during planner retries.
- **Architectural Enhancements**:
  - *Multi-Signal Semantic Statement Scoring*: Updated `_locate_statement_in_python_ast` in `app/forge/repair/anchoring.py` to deterministically score candidate statements inside target files across three defect categories: unraised exceptions (`... not raised`), runtime errors (`IndexError`, `KeyError`), and value assertions (`AssertionError: X != Y`). Scores statements based on keyword and token overlap, numeric literal matching against reproduction traces (e.g., `0.5`, `-273.15`, `65535`), math operator matching, and statement compactness. Fails closed with `ANCHOR_UNRESOLVED` when positive evidence is absent (`best_score <= 0.0`).
  - *Semantic Safeguard Validation*: Added `validate_anchor_replacement_semantics` in `app/forge/repair/anchoring.py` to inspect proposed replacements against reproduction evidence. Explicitly rejects proposals that fail to raise cited unhandled exceptions as `Semantic safety violations`, and blocks guard-stripping proposals that replace conditional checks with unconditional returns.
  - *Diagnostic Reason Propagation in Planner Retries*: Updated `RepairPlanner.plan_repair` to preserve anchor-grounding rejection reasons (`anch_reasons`) when synthesizing diffs, feeding concrete diagnostic feedback into the retry prompt (such as indentation mismatches or unraised exception violations) rather than misleading generic errors.
  - *Five Independent Generalization Fixtures*: Built and verified five distinct defect fixtures under `tests/fixtures/generalization_fixtures/` covering incorrect logic calculation (`GEN01_incorrect_logic`), empty collection edge cases (`GEN02_edge_case_failure`), plausible incorrect repairs rejected by semantic safeguards (`GEN03_plausible_incorrect_rejected`), 16-space whitespace preservation (`GEN04_whitespace_sensitive`), and malformed/out-of-scope proposal rejection (`GEN05_malformed_semantically_wrong`).
  - *Fail-Secure Invariants*: Retained zero modifications to target repositories, `FORGE_ALLOW_CLOUD_FALLBACK=false`, and mandatory human authorization gate.
- **Verification**: Verified with 7 dedicated unit and live Ollama tests (`tests/unit/test_repair_generalization.py`), 913 full regression tests, 56/56 repair and review benchmarks, and 2/2 live Docker integration tests.

### Deterministic Edit Anchoring & Source-Grounded Repair (Completed)
- **Problem & Root Cause**: Forensics across Pilot 01 Attempts 1–7 demonstrated that requiring compact local coding models (`qwen2.5-coder:1.5b`) to author both the exact verbatim search coordinates and replacement blocks leads to recurring failures when files contain docstrings, comments, or multi-line structures. Models omit intervening docstrings or comments, failing verbatim matching. However, AST parsing and reproduction evidence can localize the exact statement deterministically without relying on model-generated coordinates.
- **Architectural Enhancements**:
  - *Separation of Localization from Replacement*: Separates deterministic source localization (executed against trusted repository files via Python AST analysis and test reproduction traces) from replacement proposal generation (proposed by the model).
  - *Typed Edit Anchor Contract (`EditAnchorContract`)*: Formalizes target path, base commit SHA, source SHA-256 digest, exact anchor text, 1-indexed line coordinates, surrounding context, evidence linkage, and typed resolution status (`AnchorResolutionStatus`).
  - *Safe Anchor Resolution (`resolve_source_anchor`)*: Enforces path traversal and protected path defense, scope containment, revision drift interception, and deterministic AST statement localization from failing test assertion traces to the target function body. Disambiguates or fails closed (`ANCHOR_AMBIGUOUS`, `ANCHOR_UNRESOLVED`).
  - *Deterministic Patch Construction (`construct_patch_from_edit_anchor`)*: Synthesizes standard unified diffs from verified anchors and replacement blocks via `difflib.unified_diff`. Rejects no-ops, empty content, and structural/indentation mismatches (such as whole-function definitions replacing internal statement anchors). Verifies in-memory patch applicability and Python AST syntax without modifying target files.
  - *Repair Planner Integration*: Injects deterministically resolved source anchors (`=== DETERMINISTICALLY RESOLVED SOURCE ANCHOR ===`) into prompt context and provides graceful anchor-grounded recovery when model search blocks omit intervening lines but replacement content is valid.
  - *Fail-Closed & Sandbox Invariants*: Retains strict target repository immutability, `FORGE_ALLOW_CLOUD_FALLBACK=false`, and mandatory human authorization gate.
- **Verification**: Verified with 12 dedicated unit tests (`tests/unit/test_deterministic_edit_anchoring.py`), 906 full regression tests, 56/56 repair and review benchmarks, and 2/2 live Docker integration tests.

### Deterministic Patch Anchoring & Minimal Replacements (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 6 revealed that while local Ollama (`qwen2.5-coder:1.5b`) emitted structured search/replace keys reliably, it attempted to replace entire multi-line functions while omitting docstrings or comments inside `search_block`. Because FORGE enforces strict verbatim matching without fuzzy trimming (`count == 1`), the search block failed to match. Furthermore, error diagnostics did not detect omitted intervening lines or guide the model toward minimal statement-level replacement.
- **Architectural Enhancements**:
  - *Minimal Statement Replacement Strategy*: Updated prompt contracts to mandate minimal, statement-level search/replace blocks (1–3 lines) targeting the exact faulty logic rather than whole functions, and strictly forbidding omission of intervening source lines.
  - *Defect Line Extraction & Focused Anchor Framing*: Added `_extract_defect_line_numbers` and `_build_focused_anchor_section` in `RepairPlanner` to extract defect lines from reproduction evidence and inject an un-numbered, verbatim source code reference block (`=== RELEVANT FAULTY SOURCE ANCHOR (DATA ONLY - REFERENCE) ===`) directly into the prompt.
  - *Omitted Intervening Lines Diagnostic*: Added detection in `construct_unified_diff_from_search_replace` that identifies when the start and end lines of a search block exist in the file but intervening lines (such as docstrings or comments) were omitted, returning clear, actionable guidance.
  - *Line Number Prefix Detection*: Detects when search blocks accidentally include leading line numbers (e.g., `108: `) and instructs the model to supply exact code lines.
  - *Focused Retry Feedback*: Automatically injects focused verbatim source excerpts into the retry prompt when search block matching fails, grounding model corrections in ground-truth repository text.
  - *Verbatim Precision Preserved*: Verbatim matching (`orig_text.count(search_block) == 1`) is strictly preserved without fuzzy heuristics, whitespace normalization, or comment stripping.
- **Verification**: Verified with 11 dedicated unit tests (`tests/unit/test_patch_anchoring_and_minimal_replacements.py`), 913 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Structured Proposal Wire-Schema Reliability & Patch Representation Contract (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 5 exposed that while local Ollama (`qwen2.5-coder:1.5b`) returned syntactically valid JSON with required diagnostic fields, it completely omitted `search_block`, `replace_block`, and `unified_diff` on the wire. Because those fields defaulted to `None` in Pydantic, `model_json_schema()` omitted them from `required`, allowing Ollama's grammar engine to minimize output by omitting all patch fields. Pydantic post-validation then failed closed because no patch representation was present.
- **Architectural Enhancements**:
  - *Explicit Wire Schema Contract*: Configured `RepairModelResponseSchema` via `json_schema_extra` to programmatically include `search_block`, `replace_block`, and `unified_diff` in `schema["required"]`. Because the fields are typed with `Optional[str]`, they generate nullable `anyOf: [string, null]`, forcing local LLM grammar engines to emit the keys without key omission on the wire.
  - *Mutual Exclusivity & Single Representation Enforcement*: `validate_patch_proposal_representation` strictly accepts either structured search/replace blocks or a valid `unified_diff`. It rejects missing representations, partial structured proposals (e.g., `search_block` without `replace_block`), and ambiguous proposals (providing both structured blocks and `unified_diff`).
  - *Prompt Framing & Schema Examples*: `RepairPlanner._build_prompt` includes `"unified_diff": null` in the schema template, explicitly instructing models to provide non-null structured blocks with null unified diffs.
  - *Targeted Retry Feedback*: If validation fails due to missing patch representations, partial proposals, or ambiguous representations, `RepairPlanner.plan_repair` feeds back targeted, actionable correction guidance.
  - *Full Backward Compatibility*: Older JSON payloads omitting `search_block` and `replace_block` keys validate cleanly as long as a valid `unified_diff` is provided.
- **Verification**: Verified with 12 dedicated unit tests (`tests/unit/test_structured_proposal_wire_schema.py`), 902 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Structured Patch Construction & Deterministic Synthesis (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 4 forensics confirmed that compact local coding models (`qwen2.5-coder:1.5b`) repeatedly emit Python code indented with whitespace instead of prefixing additions with `+` in column 0 inside unified diff hunks, despite explicit prompt instructions. Requiring compact models to calculate hunk line numbers, diff headers, and column-0 markers places unnecessary syntactic strain on the model.
- **Architectural Enhancements**:
  - *Strict Structured Proposal Schema*: Defined `StructuredPatchProposalSchema` requiring `target_file`, `search_block` (exact verbatim lines), `replace_block` (substitution lines), and `change_rationale`. `RepairModelResponseSchema` supports both structured search/replace and legacy unified diffs.
  - *Deterministic Diff Constructor*: `construct_unified_diff_from_search_replace` enforces exact verbatim matching (`orig_text.count(search_block) == 1`), path traversal and protected path rejection, test file rejection, base revision pinning, and size budgets. It uses Python standard `difflib.unified_diff` to synthesize 100% syntactically valid unified diffs with exact line headers and markers.
  - *In-Memory Applicability & Syntax Verification*: Passes constructed diffs through `parse_and_validate_unified_diff` and in-memory dry-run `validate_patch_applicability` (including Python AST parsing).
  - *Prompt Refinement & Targeted Retries*: Instructs models to provide exact verbatim code lines without line number prefixes, and supplies targeted diagnostics for missing or ambiguous search blocks, unchanged replacements, or empty blocks.
- **Verification**: Verified with 13 dedicated unit tests (`tests/unit/test_structured_patch_construction.py`), 890 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Patch Generation Reliability & Diff Syntax Enforcement (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 3 exposed that compact coding models emit Python code indented with spaces inside diff hunks instead of prefixing additions with `+` in column 0. The diff validator correctly detected zero additions and deletions, rejecting the patch as an empty diff. However, retry loops lacked explicit column 0 diff marker feedback, and static validation lacked checks for unchanged replacement / no-op diffs.
- **Architectural Enhancements**:
  - *Unified Diff Schema Contract*: `RepairModelResponseSchema.unified_diff` description explicitly mandates standard unified diff formatting starting with `--- a/<file>` and `+++ b/<file>`, with lines to add starting with `+`, lines to remove starting with `-`, and context lines starting with `' '`.
  - *Prompt Syntax Requirements*: `RepairPlanner._build_prompt` codifies column 0 diff marker requirements, warning explicitly that lines starting with indentation spaces are treated as unchanged context lines.
  - *Malformed Hunk Detection*: `parse_and_validate_unified_diff` scans all hunk body lines to ensure every line begins with a valid diff marker (`+`, `-`, `' '`, `\`, or empty string), rejecting un-prefixed code snippets immediately.
  - *Static Unchanged Replacement Detection*: `parse_and_validate_unified_diff` checks whether all added lines in each hunk match the removed lines, rejecting no-op replacements.
  - *In-Memory Net Change Verification*: `validate_patch_applicability` verifies that applying the patch candidate produces a non-zero net modification to the target file (`"".join(current_lines) != orig_text`).
  - *Targeted Retry Diagnostics*: `RepairPlanner.plan_repair` provides specific diagnostic guidance on empty diffs, context mismatches, missing headers, and no-op replacements, guiding the local model to correct syntax without weakening any validation boundary.
  - *Safe Candidate Construction*: Guards `PatchCandidate` instantiation with `max(1, hunk_count)` and `try/except` to ensure fail-closed plan rejection when diffs are malformed.
- **Verification**: Verified with 11 dedicated regression tests (`tests/unit/test_patch_generation_reliability.py`), 877 full regression tests, 2/2 live Docker integration tests, and 11/11 passing repair benchmarks.

### Local Ollama Structured Output Reliability (Completed)
- **Problem & Root Cause**: Pilot 01 Attempt 2 exposed that local coding models emitted trailing commas before closing braces/brackets, failing RFC 8259 JSON parsing. Requests did not pass OpenAI-compatible `response_format` schemas on the wire, and retry loops lacked syntax diagnostic feedback.
- **Architectural Enhancements**:
  - *Structured Output / JSON Schema Wire Formatting*: `LLMRequest` supports `response_format` containing OpenAI-compatible `json_schema` derived from `RepairModelResponseSchema`. `OpenAICompatibleProvider` serializes `response_format` on the wire and supports graceful single-provider 400 downgrade.
  - *Strict RFC 8259 Parsing*: `parse_json_payload` safely parses JSON from direct content, markdown code fences, or `{ ... }` bounds, strictly rejecting trailing commas without silent mutation, and returning non-secret syntax diagnostics.
  - *Bounded Same-Provider Retries*: Enforces a hard cap of 3 retries (total attempts $\le 4$), 120s wall-clock time limit, 50,000 char response bound, and 4096 output token limit.
- **Verification**: Verified with 17 dedicated unit tests (`tests/unit/test_structured_output_reliability.py`), 866 full regression tests, and 11/11 passing repair benchmarks.

### Repair Planner Reliability & Grounded Verification (Completed)
- **Problem & Root Cause**: Pilot 01 exposed that intakes created from failing test specifications defaulted `affected_files` solely to test files. Downstream repair planning targeted tests instead of implementation code, causing context mismatches and verification crashes.
- **Architectural Enhancements**:
  - *Candidate Target Resolution*: `resolve_candidate_targets` discovers candidate implementation targets from reproduction stack traces, AST test imports, and symbol indexing.
  - *Strict Test Immunity & Scope Restriction*: `is_test_path` identifies test files. Modifying test files to mask defects is strictly forbidden. Intakes restricted solely to tests reject repairs when implementation files require modification, requiring an authorized revised intake.
  - *Ambiguous Root-Cause Protection*: Halts with an ambiguous root-cause blocker if multiple candidate implementation files are found without explicit designation.
  - *Multi-File Context Framing*: Target implementation code is presented with 1-indexed line numbers; failing test oracles are explicitly demarcated as read-only reference data.
  - *In-Memory Patch Applicability Validation*: `validate_patch_applicability` verifies hunk headers, context line matching against base commit files, Python AST syntax, and rejects empty/no-op diffs before marking plans `VALIDATED`.
  - *Bounded Retry Feedback Loop*: Allows up to `1 + max_retries` attempts with structured diagnostic feedback, failing closed if retries are exhausted without silent cloud fallback.
  - *Target Repository Immutability*: All planning and applicability checks run strictly in-memory, ensuring zero modification to target files or Git history.
- **Verification**: Verified with 9 dedicated regression tests (`tests/unit/test_repair_planner_reliability.py`), 849 full regression tests, and 11/11 passing repair benchmarks.

### Phase 5: Supervised End-to-End Engineering Workflow (Completed)
- **Kickoff & Architectural Foundation (Completed)**: Unifies repository acquisition, diff review, defect reproduction, and bounded repair verification into a supervised defect-to-verified-fix workflow. Codifies a 19-state typed state machine (`CREATED` through `AWAITING_HUMAN_REVIEW` to terminal `APPROVED` / `REJECTED`), transactional event ledger, cryptographic revision pinning, fail-closed container boundaries (`SANDBOX_UNAVAILABLE`), honest outcome taxonomy (`NOT_REPRODUCED`, `FLAKY`, `BROKEN_BUILD`, `REGRESSION_DETECTED`), and an exportable certified Engineering Dossier (`patch.diff`, verification proof, SARIF).
- **M1 — Unified Task Intake & Repository Context Pipeline (Implemented & Verified)**: Standardizes defect intake across review findings (`review_finding`), unified git diffs (`unified_diff`), issue problem statements (`issue_description`), and test specifications (`test_specification`) into a validated `WorkflowIntakeSpec`. Cryptographically pins exact 40-character commit SHA (`resolve_and_pin_revision`), intercepts revision drift (`RevisionDriftError`), detects dirty working trees (`DirtyRepositoryError`), and sanitizes affected file paths (rejecting path traversal `..` and absolute paths). Assembles scoped, deterministic repository context (`RepositoryContext`) under explicit character and token budgets (`ContextBudget`, `ContextBudgetSummary`), filtering relevant AST symbols, extracting line-numbered source excerpts, wrapping discovered repository instructions in passive untrusted boundary banners, and providing CLI commands (`forge workflow intake create/validate/show` and `forge workflow context build/show`). Verified with 26 focused unit tests and 713 regression tests.
- **M2 — Supervised Workflow State Machine & Transactional Ledger (Implemented & Verified)**: Delivers a typed, guarded 19-state lifecycle machine with code-enforced transition validation, transactional SQLite storage (`workflow_intakes`, `engineering_workflows`, `engineering_workflow_events`), optimistic concurrency control via monotonic integer versioning (`WorkflowConcurrencyError` on conflicting updates), append-only sequential event auditing with automatic secret redaction, safe crash recovery and unblocking (`REPRO_BLOCKED` and `VERIFICATION_BLOCKED` safely resumed; terminal states strictly reject resumption), and a mandatory human authorization gate (`AWAITING_HUMAN_REVIEW` strictly required before `APPROVED`). Features CLI commands (`forge workflow start`, `forge workflow status`, `forge workflow list`, `forge workflow resume`, `forge workflow review`). Verified with 21 unit and integration tests and 734 full-suite tests.
- **M3 — Automated Evidence Chaining from Reproduction to Repair (Implemented & Verified)**: Connects verified defect reproduction directly into bounded repair planning. Safely derives non-shell reproduction commands from test specifications or issue reproduction hints (`extract_reproduction_command_from_intake`). Validates reproduction evidence integrity against commit revision pinning, finding identity, non-empty/untruncated content, exit code contradictions, and trusted oracle provenance (`validate_reproduction_evidence`). Manages guarded transitions through `reproduce_defect`, `plan_repair`, and sequential `advance_workflow` in `EngineeringWorkflowOrchestrator`, halting on inconclusive or failed outcomes, blocking safely without host fallback on Docker absence (`REPRO_BLOCKED`), and atomically attaching valid candidate patches with optimistic concurrency in SQLite (`attach_repair_plan_to_workflow`). Features CLI commands (`forge workflow repro`, `forge workflow plan-repair`, `forge workflow advance`). Verified with 31 unit tests and 765 full-suite tests.
- **M4 — Multi-Check Regression & Safety Verification (Implemented & Verified)**: Delivers a bounded, isolated multi-check verification pipeline executing across baseline and candidate disposable scratch workspaces inside Docker containers (`MultiCheckVerifier`). Chains reproduction check (defect oracle), baseline test suite (`pytest -q tests/`), static linting (`flake8`/`ruff`), and security analysis (`bandit`), with command allowlisting and shell injection defense (`validate_verification_check_command`). Categorizes outcomes honestly (`PASSED`, `FAILED`, `BLOCKED`, `TIMED_OUT`, `INCONCLUSIVE`, `BASELINE_FAILED`). Distinguishes candidate regressions (`REGRESSION_DETECTED`) from pre-existing baseline failures (`BASELINE_FAILED`). Transitions clean repairs safely through `VERIFIED -> REGRESSION_CHECKING -> AWAITING_HUMAN_REVIEW` while strictly halting for human review before any approval. Persists verification attempts in SQLite `multi_check_verifications`. Features CLI commands (`forge workflow verify` and `forge workflow advance --verify`). Verified with 24 unit and integration tests and 789 full-suite tests.
- **M5 — Certified Engineering Dossier & Operator Export (Implemented & Verified)**: Delivers certified engineering dossier generation and offline operator export for approved workflows. Strictly conditions export on explicit human operator approval (`APPROVED` state with `human_decision == "approve"`), revalidating workflow version, pinned commit SHA, patch digest, evidence provenance, and sequential ledger integrity. Exports a deterministic package: `patch.diff` (unmodified bounded diff $\le 100$ lines, $\le 20$ KB), `verification_certificate.json` (machine-readable proof and delta comparison), `findings.sarif` (standards-compliant SARIF 2.1.0 document), `audit_ledger.json` (complete sequential secret-scrubbed audit trail), `APPLY.md` (dry-run, apply, verification, and rollback runbook), and `manifest.json` (canonical SHA-256 integrity checksums + optional HMAC-SHA256 signature). Enforces path containment, directory safety policy, and offline validation (`validate_engineering_dossier`). Features CLI commands (`forge workflow export` and `forge workflow validate-dossier`). Verified with 40 unit and integration tests and 829 full-suite tests.
- **M6 — End-to-End Workflow Acceptance, Reliability & Release (Implemented & Verified)**: Final acceptance and release audit establishing a 30-scenario deterministic acceptance corpus across 10 reliability domains (intake & revision pinning, context budgeting, adversarial instructions, prompt injection, path traversal, state machine transitions, illegal jump rejection, optimistic concurrency, transactional ledger & secret scrubbing, defect reproduction, non-reproduction fail-closed, flaky oracle handling, reproduction timeouts, protected path rejection, Docker absence fail-closed, sandbox resource limits, network isolation, evidence tamper detection, revision drift interception, duplicate start idempotency, blocked workflow resumption, clean repair verification, baseline regression interception, pre-existing defect honest attribution, graceful degradation for optional linters, mandatory human review gate enforcement, operator rejection, unapproved export refusal, and certified dossier export with SHA-256 manifest and HMAC signing). Features quantitative metrics with honest denominators, automated secret scrubbing, and CLI commands (`forge workflow test` and `forge workflow acceptance`). Verified with 100.0% pass rate (30/30) and 838 full regression tests.
- **Milestone Sequence (Roadmap)**:
  - *M1 — Unified Task Intake & Repository Context Pipeline*: Completed and verified.
  - *M2 — Supervised Workflow State Machine & Transactional Ledger*: Completed and verified.
  - *M3 — Automated Evidence Chaining from Reproduction to Repair*: Completed and verified.
  - *M4 — Multi-Check Regression & Safety Verification*: Completed and verified.
  - *M5 — Certified Engineering Dossier & Operator Export*: Completed and verified.
  - *M6 — End-to-End Workflow Acceptance, Reliability & Release*: Completed and verified.
- **Strict Boundary Notice**: Target repository immutability is strictly enforced. Autonomous git commits, branch pushes, pull requests, merges, and deployments remain strictly forbidden. Automated workflow strictly halts at `AWAITING_HUMAN_REVIEW`. Only explicit operator approval unlocks dossier export.

### Phase 4: Targeted Autonomous Repair (Completed)
- **M1 — Evidence-Grounded Repair Planning & Patch Generation (Implemented)**: Converts calibrated findings and verified reproduction evidence into structured, bounded repair plans and candidate unified diffs. Enforces strict evidence grounding (ungrounded findings rejected), prompt-injection mitigation via untrusted context framing, protected file immunity (`AGENTS.md`, `README.md`, `.gitignore`, `pyproject.toml`, `.github/*`), strict size budgeting ($\le 100$ lines, $\le 5$ hunks, $\le 1$ file, $\le 20$ KB), target repository immutability, mandatory human approval gates, and SQLite persistence.
- **M2 — Isolated Patch Application & Verification (Implemented)**: Safely applies validated candidate patches in isolated disposable scratch workspaces and verifies them against independent test reproduction oracles inside the Docker container sandbox boundary. Features a pure-Python AST-verifying diff applier, tamper detection (SHA256 mismatch rejection), command allowlisting and shell injection defense, fail-closed host protection (`SANDBOX_UNAVAILABLE` when Docker is absent without executing code on the host), deterministic outcome taxonomy (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `TIMEOUT`, `INCONCLUSIVE`), SQLite attempt persistence (`repair_attempts`), and CLI commands (`forge repair verify`, `forge repair attempts`).
- **M3 — Repair Quality & Regression Benchmarking (Implemented)**: Deterministic, offline benchmark suite evaluating repair-plan quality, patch validation, and verification classification across 11 representative defect categories (syntax errors, incorrect logic, boundary conditions, missing validation, exception handling, security-sensitive mistakes, clean controls, ambiguous findings, insufficient evidence, adversarial instructions, and regression-inducing patches). Features independent ground truth, honest denominators (`None` / `N/A` for empty classes), secret redaction, and CLI integration (`forge benchmark repair` and `forge repair benchmark`).
- **M4 — Controlled Repair Orchestration & Lifecycle (Implemented)**: Resumable, auditable end-to-end orchestration workflow connecting verified evidence, bounded repair planning, static patch validation, containerized sandbox verification, and a mandatory human review gate. Features a typed state machine with strict transition enforcement (`CREATED -> EVIDENCE_VALIDATED -> PLAN_GENERATED -> PATCH_VALIDATED -> VERIFICATION_PENDING -> VERIFIED / FAILED / BLOCKED -> AWAITING_HUMAN_REVIEW -> APPROVED / REJECTED`), fail-closed container boundaries (`SANDBOX_UNAVAILABLE` when Docker is missing), anti-tampering SHA256 digest validation, stale base revision detection, plan-only dry-run bypass, optimistic concurrency control with monotonic versioning, append-only sequential event auditing with automated secret scrubbing, strict target repository immutability (zero file modifications, no commits/pushes/PRs), and full CLI management (`forge repair workflow start/status/list/resume/review`).
- **M5 — End-to-End Repair Reliability & Adversarial Testing (Implemented)**: Hardened end-to-end acceptance suite executing 26 deterministic scenarios across 10 failure domains: happy-path repair, evidence integrity gating, budget and protected-file safety, revision drift interception, all 6 verification outcomes, fail-closed container boundaries, provider timeout/malformed/auth resilience, resumability, optimistic concurrency conflict detection, adversarial prompt injection untrusted framing, path traversal rejection, and mandatory human review enforcement. Features quantitative metrics with honest denominators, automated secret scrubbing, and CLI commands (`forge repair test`, `forge repair acceptance`).
- **M6 — Final Acceptance & Release Readiness (Implemented)**: End-to-end audit across M1–M5, validation of real vs. simulated container isolation contracts, 100% pass across all 26 acceptance scenarios and 11 repair quality benchmarks, 100% pass on 686-test regression suite, usability and error-signaling audit, and formal release decision (`CONDITIONALLY READY FOR SUPERVISED USE` due to host Docker dependency for live container execution).
- **Strict Boundary Notice**: **Automated modification of target repositories, autonomous git commits, branch pushes, pull requests, merges, and deployments remain strictly FORBIDDEN.**

### Phase 4 Readiness & Sandbox Gate (Completed)
- **Container Sandbox & Host Isolation Audit**: Complete audit of container sandbox boundaries (`DockerSandbox`, `ReproductionHarness`). Verified that when Docker is absent (`shutil.which("docker") is None`), FORGE fails closed with `SKIPPED_SANDBOX_UNAVAILABLE` and strictly refuses to execute target repository code directly on the host.
- **Multi-Provider & Cloud Guard Invariants**: Retained 5 supported providers (`fake`, `ollama`, `gemini`, `deepseek`, `groq`) with complete elimination of OpenAI API and Ollama Cloud dependencies, standardizing Ollama strictly on local execution (`http://localhost:11434/v1`) and enforcing `FORGE_ALLOW_CLOUD_FALLBACK=false`.
- **Formal Repair Safety Contract (`forge.repair.contracts`)**: Codified formal safety boundaries required before Phase 4 patch generation can begin:
  - *Target Repository Immutability*: Target repository remains strictly read-only; repairs are strictly isolated to independent scratch workspaces.
  - *Base Revision Pinning*: Repairs are cryptographically pinned to the finding's verified commit SHA (`validate_base_sha`).
  - *Patch Candidate Constraints*: Enforces strict bounds on lines changed (max 100), hunks (max 5), files modified (max 1), and diff bytes (max 20KB).
  - *Scope Containment*: Path traversal, absolute paths, and symlink escapes outside the scratch workspace are strictly rejected via `verify_workspace_containment`.
  - *Pre/Post Outcome Taxonomy*: Deterministic classification (`REPAIR_VERIFIED`, `REPAIR_FAILED`, `BROKEN_BUILD`, `REGRESSION_DETECTED`, `SANDBOX_UNAVAILABLE`).
  - *Mandatory Human Approval Gate*: Autonomous git commits, pushes, branch merges, and pull request generation are strictly forbidden (`assert_human_approval_gate`).
- **Phase 4 Autonomous Repair Notice**: **Phase 4 autonomous repair is NOT STARTED**. No automated patch generation, target repository mutation, git commit, push, or deployment has been implemented or enabled.

### Pre-Phase 4: Local Ollama & Multi-Provider LLM Integration (Completed)
- **Local Ollama Standardization**: Standardized Ollama on strictly local inference (`http://localhost:11434/v1`) with zero cloud credential requirements. `ollama_cloud` is completely removed to prevent unauthorized or silent cloud routing. Hardware resource analysis (8 GB M1, 13 GiB available disk) establishing suitability of compact coding models (`qwen2.5-coder:1.5b` or `llama3.2:3b`).
- **Multi-Cloud Integration**: Standard `httpx` OpenAI-compatible support for Groq (`GROQ_API_KEY` / `GROQ_API`), Google Gemini (`GEMINI_API_KEY` / `GEMINI_API`), and DeepSeek (`DEEPSEEK_API_KEY` / `DEEPSEEK_API`) with zero extra SDK dependencies.
- **Fail-Secure Architecture**: Strict cloud fallback prohibition (`FORGE_ALLOW_CLOUD_FALLBACK=false`), fail-closed behavior on local endpoint connection failures, bounded transient 5xx retries, typed rate-limit and authentication error mapping, and automated token scrubbing.


### Phase 1: Secure Repository Intelligence (Completed)
- **M1 — Architecture Baseline & Workspace Isolation**: Safe HTTPS/local repository acquisition, path traversal guards, and git hook suppression.
- **M2 — Static Repository Intelligence**: AST symbol indexing, language/framework detection, instruction file extraction, and token-bounded context budgeting.
- **M3 — Evidence Records, State Machine, & Storage**: Structured evidence, finding lifecycle transitions (`suspected -> reproduced -> root_caused -> fixed -> verified`), and SQLite event store.
- **M4 — Secure Container Sandbox Boundary**: Docker adapter with non-root user (`1000:1000`), `--network none`, `--read-only`, dropped capabilities, memory/CPU quotas, and secret redaction.
- **M5 — Bounded Investigation Orchestration**: Multi-turn typed LLM agent with tool calling (`list_dir`, `search`, `read_file`, `get_symbol_index`, `run_check`).
- **M6 — Evidence-Linked Reports & Benchmarking**: Seeded-bug test fixtures, precision/recall evaluation, and JSON/Markdown report generation.
- **M7 — Checkpoint & Comprehensive Verification**: End-to-end security audits, secret scanning, and host immutability verification.

### Phase 2: Diff-Aware Intelligent Review (Completed)
- **M1 — Diff-Aware Code Review Engine**: Read-only Git diff analysis comparing two revisions, accurate hunk and changed-line mapping, security vulnerability and defect detection on modified lines, and automated secret redaction.
- **M2 — Evidence-Calibrated Review Findings**: Tripartite finding structure separating observed facts from inferences and recommendations. Deterministic finding IDs, evidence verification against diff hunks, automatic confidence downgrading for unbacked claims, and strict lifecycle state machine enforcement.
- **M3 — Review Quality Benchmarking**: Versioned benchmark suite evaluating review quality covering genuine security/logic defects, harmless lookalikes, clean controls, attribution challenges, prompt injection, and secret redaction.
- **M4 — Adversarial Review Validation & Generalization**: Genuinely held-out adversarial fixture corpus, resolution of case-level vs finding-level counting units, evaluator self-tests, and defensible regression thresholds.
- **M5 — Security-Sensitive Dataflow & Secret-Exposure Detection**: AST-based security analysis and bounded dataflow tracking. Detects hardcoded credentials in nested dictionaries and Authorization headers, formatted token assembly via string interpolation, and sensitive values leaking into logging, print, or HTTP sinks (`--corpus security`).
- **M6 — CI Review Policies & SARIF Export**: Declarative CI policy enforcement with configurable severity/confidence thresholds, category filtering, auditable scoped rule suppressions, fail-closed evidence handling, and full OASIS SARIF 2.1.0 compliance.

### Phase 3: Defect Reproduction & Verification (Completed)
- **M1 — Sandbox Execution Contracts (Implemented)**: Typed execution contracts (`ReproductionRequest`, `ReproductionCommand`, `ExecutionEnvironmentSpec`, `ReproductionResult`) with strict schema validation, shell injection prevention, approved executable allowlists, resource limits, and fail-closed degradation.
- **M2 — Reproduction Case Management & Evidence Capture (Implemented)**: Deterministic reproduction case tracking, revision pinning, multi-attempt history, bounded/redacted evidence capture, SHA256 digests, field-level provenance, safe retry policy (forbidding automatic non-idempotent retries), resume validation against stale revisions, and lifecycle transition integration.
- **M3 — Deterministic Reproduction Planning (Implemented)**: Typed planning layer converting calibrated findings and trusted context into bounded, validated reproduction plans. Five strategies (`RUN_SPECIFIC_TEST`, `RUN_SCOPED_TEST_FILE`, `VALIDATE_BOUNDARY_CONDITION`, `CHECK_EXPECTED_OBSERVED`, `INSUFFICIENT_INFORMATION`). Deterministic plan IDs, full policy validator (14+ rejection criteria), CLI `forge plan` command group.
- **M4 — Outcome Classification & Evidence Validation (Implemented)**: Strongly typed 8-outcome taxonomy (`REPRODUCED`, `NOT_REPRODUCED`, `UNRELATED_FAILURE`, `ENVIRONMENT_BLOCKED`, `TIMEOUT`, `FLAKY`, `INVALID_PLAN`, `INCONCLUSIVE`) with deterministic reason codes, strict oracle presence and provenance checks (never infer reproduction from generic exit code or model assertion), precedence enforcement, and flaky retry detection.
- **M5 — Reproduction Reliability and Regression Hardening (Implemented)**: Full pipeline hardening against concurrency races, partial write rollback, SQLite unique attempt number constraints, immediate git revision TOCTOU detection, symlink escape checks using Path.is_relative_to, head/tail output bounding preservation with explicit markers, interruption/cleanup failure classification as INCONCLUSIVE, and standardized CLI exit codes (0: reproduced, 1: not reproduced/failed, 2: invalid plan/error, 3: environment blocked, 4: timeout/inconclusive/flaky).
- **M6 — End-to-End Reproduction Acceptance (Implemented)**: Comprehensive 12-scenario acceptance pass verifying end-to-end integration across case management, plan generation, offline validation, oracle provenance, fail-closed host protection, complete 8-outcome classification, SQLite persistence across process restarts, retry caps, finding lifecycle transition gating, standardized CLI exit codes (0–4), Phase 1/2 backward compatibility, and target repository immutability.

---

## Installation

Ensure Python 3.11+ is installed.

### Option A: Install via Standalone Distribution Wheel (.whl)
For standard operator workstations without requiring a git checkout:
```bash
python3 -m venv ~/forge-venv
source ~/forge-venv/bin/activate
pip install forge_core-0.1.0b1-py3-none-any.whl
```

### Option B: Developer / Editable Installation
For development, inspection, and benchmark execution:
```bash
git clone https://github.com/thisisanimesh01/FORGE.git
cd FORGE
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Verified CLI Usage

### System Diagnostics & Shell Completion
```bash
# Verify environment, Python, Git, SQLite, and Docker status
forge doctor

# Inspect sandbox runtime health, container configuration, and isolation limits
forge sandbox doctor

# Install shell auto-completion (bash, zsh, fish, or powershell)
forge --install-completion zsh
```

### Supervised End-to-End Engineering Workflow: Intake & Context (Phase 5 Milestone 1)
```bash
# 1. Create a task intake specification from an issue description
forge workflow intake create --type issue --repo /path/to/repo --issue-title "Division by zero" --issue-text "divide(10, 0) crash" --affected-file pkg/calculator.py --output intake.json

# 2. Create intake from a specific failing test command (optionally specifying affected implementation files)
forge workflow intake create --type test --repo /path/to/repo --test-file tests/test_calc.py --test-command "pytest tests/test_calc.py::test_zero" --affected-file pkg/calculator.py --output intake_test.json

# 3. Create intake from raw unified diff or patch file
forge workflow intake create --type diff --repo /path/to/repo --diff-file changes.diff --output intake_diff.json

# 4. Create intake from an existing review finding in SQLite
forge workflow intake create --type finding --repo /path/to/repo --finding-id find_01 --output intake_finding.json

# 5. Validate an intake specification offline and against repository
forge workflow intake validate intake.json --repo /path/to/repo

# 6. Display intake specification
forge workflow intake show intake.json

# 7. Assemble scoped repository context for a validated intake specification
forge workflow context build intake.json --output context.json

# 8. Assemble context with custom character budget
forge workflow context build intake.json --budget 15000 --output context.json

# 9. Inspect assembled context metadata
forge workflow context show context.json

# 10. Display the rendered markdown prompt ready for model consumption
forge workflow context show context.json --prompt-only
```

### Supervised End-to-End Engineering Workflow: Lifecycle & State Machine (Phase 5 Milestone 2)
```bash
# 1. Start an engineering workflow from a validated task intake specification
forge workflow start --intake intake.json

# 2. Start and auto-advance to INTAKE_VALIDATED and REPO_ANALYZED
forge workflow start --intake intake.json --repo /path/to/repo --auto-advance

# 3. Inspect workflow status and append-only event ledger timeline
forge workflow status wf_eng_38b1f8ac9210
forge workflow status wf_eng_38b1f8ac9210 --format json

# 4. List engineering workflows stored in SQLite
forge workflow list
forge workflow list --state awaiting_human_review
forge workflow list --intake-id intake_001

# 5. Safely resume a blocked or interrupted workflow
forge workflow resume wf_eng_38b1f8ac9210

# 6. Submit mandatory human review decision (approve or reject)
forge workflow review wf_eng_38b1f8ac9210 --decision approve --notes "Verified fix offline"
forge workflow review wf_eng_38b1f8ac9210 --decision reject --notes "Fix violates policy"
```

### Automated Evidence Chaining & Defect Reproduction (Phase 5 Milestone 3)
```bash
# 1. Execute defect reproduction and chain validated evidence into workflow
forge workflow repro wf_eng_38b1f8ac9210

# 2. Execute reproduction with explicit repository path and test command override
forge workflow repro wf_eng_38b1f8ac9210 --repo /path/to/repo --test-command "pytest tests/test_calc.py"

# 3. Plan evidence-grounded repair candidate for a reproduced workflow
forge workflow plan-repair wf_eng_38b1f8ac9210 --repo /path/to/repo

# 4. Plan repair with specific provider, model, and scratch directory
forge workflow plan-repair wf_eng_38b1f8ac9210 --provider ollama --model qwen2.5-coder:7b --scratch-dir /tmp/scratch

# 5. Sequentially advance workflow through intake, context, reproduction, and repair planning
forge workflow advance wf_eng_38b1f8ac9210 --repo /path/to/repo
```

### Multi-Check Regression & Safety Verification (Phase 5 Milestone 4)
```bash
# 1. Execute isolated multi-check verification against repair candidate
forge workflow verify wf_eng_38b1f8ac9210

# 2. Verify with repository override and dedicated scratch directory
forge workflow verify wf_eng_38b1f8ac9210 --repo /path/to/repo --scratch-dir /tmp/scratch

# 3. Verify skipping optional lint or security checks
forge workflow verify wf_eng_38b1f8ac9210 --no-lint --no-security

# 4. Verify with explicit baseline test command override
forge workflow verify wf_eng_38b1f8ac9210 --baseline-test "pytest -q tests/test_core.py"

# 5. Verify and output structured JSON report
forge workflow verify wf_eng_38b1f8ac9210 --format json

# 6. Sequentially advance workflow all the way through multi-check verification to human review gate
forge workflow advance wf_eng_38b1f8ac9210 --repo /path/to/repo --verify
```

### Certified Engineering Dossier & Operator Export (Phase 5 Milestone 5)
```bash
# 1. Export an approved workflow into a certified engineering dossier package
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output

# 2. Export with repository path for HEAD revision drift validation
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --repo /path/to/repo

# 3. Export with cryptographic HMAC-SHA256 signature using an operator key
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --key "operator-secret-token" --key-id "sec-ops-1"

# 4. Export with key loaded securely from file and force overwrite
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --force --key-file /path/to/key.txt

# 5. Export and output machine-readable JSON summary
forge workflow export wf_eng_38b1f8ac9210 --output-dir ./dossier_output --format json

# 6. Validate an exported engineering dossier offline (checks SHA-256 digests and file completeness)
forge workflow validate-dossier ./dossier_output

# 7. Validate an exported engineering dossier with HMAC-SHA256 signature verification
forge workflow validate-dossier ./dossier_output --key "operator-secret-token"
forge workflow validate-dossier ./dossier_output --key-file /path/to/key.txt --format json
```

### End-to-End Workflow Acceptance & Reliability Suite (Phase 5 Milestone 6)
```bash
# 1. Execute the full Phase 5 end-to-end acceptance suite (30 scenarios across 10 domains)
forge workflow test

# 2. Execute acceptance suite and output machine-readable JSON report
forge workflow test --format json

# 3. Export acceptance test report to a markdown or json file
forge workflow test --output report.md
forge workflow test --format json --output report.json

# 4. Filter acceptance suite by specific scenario ID
forge workflow test --scenario P5_ACC01

# 5. Filter acceptance suite by domain category
forge workflow test --category human_review_gate
forge workflow test --category adversarial_intake

# 6. Opt-in to live Docker sandbox execution if available
forge workflow test --docker-live

# 7. Alias command for acceptance suite
forge workflow acceptance
```

### Evidence-Grounded Repair Planning & Isolated Verification (Phase 4 Milestones 1–2)
```bash
# Generate a bounded repair plan and candidate patch from an audited finding
forge repair plan find_01 --repo /path/to/repo --commit a1b2c3d4e5f6 --provider ollama

# Generate plan and export full structured JSON artifact
forge repair plan find_01 --output plans/plan_find_01.json --format json

# Inspect an existing repair plan and unified diff patch candidate
forge repair show plans/plan_find_01.json
forge repair show rpl_find_01_a1b2c3d4_7e3b9a1d

# Validate an existing repair plan offline against safety, scope, and budget rules
forge repair validate plans/plan_find_01.json

# List historical repair plans stored in SQLite
forge repair list
forge repair list --finding-id find_01

# Safely apply a candidate patch in an isolated scratch workspace and verify inside sandbox
forge repair verify rpl_find_01_a1b2c3d4_7e3b9a1d --repo /path/to/repo

# Verify a plan JSON file with custom verification command and JSON output
forge repair verify plans/plan_find_01.json --repo /path/to/repo --command "pytest tests/test_parser.py" --format json

# List historical verification attempts for a repair plan
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d
forge repair attempts rpl_find_01_a1b2c3d4_7e3b9a1d --format json
```

### Repair Quality & Regression Benchmarking (Phase 4 Milestone 3)
```bash
# Run full offline repair quality benchmark suite (Markdown report)
forge benchmark repair

# Run using the repair command alias
forge repair benchmark

# Export benchmark results as formatted JSON
forge benchmark repair --format json

# Filter for a specific fixture category
forge benchmark repair --fixture REP01_syntax_error

# Export benchmark report to file
forge benchmark repair --output reports/repair_benchmark_report.md

# Opt-in live provider evaluation (requires explicit provider flag)
forge benchmark repair --mode provider --provider ollama --model qwen2.5-coder:7b
```

### Controlled Repair Orchestration & Human Review Gate (Phase 4 Milestone 4)
```bash
# Start an end-to-end repair workflow for a finding (plan-only dry-run mode)
forge repair workflow start find_001 --repo . --commit a1b2c3d4e5f6 --dry-run

# Start full workflow with sandboxed verification execution
forge repair workflow start find_001 --repo . --commit a1b2c3d4e5f6

# View workflow lifecycle state, candidate diff, and sequential audit trail
forge repair workflow status rwf_find_001_a1b2c3d4_abc123

# Export status report as formatted JSON
forge repair workflow status rwf_find_001_a1b2c3d4_abc123 --format json

# List repair workflows, optionally filtered by state or finding
forge repair workflow list
forge repair workflow list --state awaiting_human_review
forge repair workflow list --finding-id find_001 --format json

# Resume a blocked or paused workflow
forge repair workflow resume rwf_find_001_a1b2c3d4_abc123

# Submit mandatory human review decision (approve or reject)
forge repair workflow review rwf_find_001_a1b2c3d4_abc123 --decision approve --notes "Verified fix against regression suite"
forge repair workflow review rwf_find_001_a1b2c3d4_abc123 --decision reject --notes "Incomplete edge case handling"
```

### End-to-End Repair Reliability & Adversarial Acceptance Suite (Phase 4 Milestone 5)
```bash
# Execute the full 26-scenario end-to-end acceptance suite (Markdown report)
forge repair test

# Run via acceptance alias
forge repair acceptance

# Run a specific acceptance scenario by ID (e.g., ACC01, ACC15, ACC26)
forge repair test --scenario ACC01

# Export machine-readable JSON acceptance report with automated secret scrubbing
forge repair test --format json --output reports/acceptance_report.json
```

### Publication Audit, Verification & Ledger Governance (Phase 9 Milestone 4)
```bash
# Run comprehensive 14-point publication security audit for an approved repair
forge repair publish audit rep_12345

# View chronological 22-stage publication timeline
forge repair publish timeline rep_12345

# Verify cryptographic hash-chain integrity of repair execution events
forge repair publish audit-verify rep_12345

# Generate complete publication audit report in Markdown format
forge repair publish report rep_12345 --format markdown

# Generate complete publication audit report in JSON format saved to file
forge repair publish report rep_12345 --format json --output reports/audit_rep_12345.json

# List historical publication records and governance outcomes
forge repair publish history --limit 20

# Top-level CLI aliases
forge publish audit rep_12345
forge publish timeline rep_12345
forge publish audit-verify rep_12345
forge publish report rep_12345
forge publish history
```

### Remote Pull Request Creation & Upstream Integration (Phase 9 Milestone 3)
```bash
# Validate remote publication preconditions for an approved repair (zero-mutation verification)
forge repair publish remote-validate rep_12345

# Validate remote publication with custom remote and JSON output
forge repair publish remote-validate rep_12345 --remote origin --json

# Push verified staged commit to authorized remote branch
forge repair publish push rep_12345

# Create upstream pull request on hosting provider (e.g. GitHub)
forge repair publish pr rep_12345 --base main

# Create draft pull request
forge repair publish pr rep_12345 --base main --draft

# View high-level remote publication status
forge repair publish remote-status rep_12345

# Check live pull request status on upstream provider
forge repair publish pr-status rep_12345

# Inspect comprehensive audit trail, remote records, and provider API operations
forge repair publish remote-inspect rep_12345

# Top-level CLI aliases
forge publish remote-validate rep_12345
forge publish push rep_12345
forge publish pr rep_12345 --base main
forge publish remote-status rep_12345
forge publish remote-inspect rep_12345
```

### Isolated Branch Staging & Commit Creation (Phase 9 Milestone 2)
```bash
# Stage an approved repair branch and commit inside an isolated publication scratch workspace
forge repair publish stage rep_12345

# Stage with a custom compliant branch name
forge repair publish stage rep_12345 --branch forge/fix-login-null-pointer

# Explicit commit alias for staging and commit creation
forge repair publish commit rep_12345

# View publication and staged commit status for a repair
forge repair publish status rep_12345

# Inspect metadata, touched files, and commit hashes for a staged commit
forge repair publish inspect sc_1234567890ab

# Inspect staged commit in JSON format
forge repair publish inspect sc_1234567890ab --format json

# Top-level CLI aliases
forge publish stage rep_12345
forge publish status rep_12345
forge publish inspect sc_1234567890ab
```

### Safe Git Publication Planning & Validation (Phase 9 Milestone 1)
```bash
# Validate publication preconditions for an approved repair (fail-closed check)
forge repair publish validate rep_12345

# Formulate and persist a deterministic publication plan for an approved repair
forge repair publish plan rep_12345

# Formulate a publication plan with a custom compliant branch name
forge repair publish plan rep_12345 --branch forge/fix-login-null-pointer

# Output publication plan as JSON for external tooling integration
forge repair publish plan rep_12345 --format json

# Display detailed publication request and plan for a repair
forge repair publish show rep_12345

# List recent publication plans across all repairs
forge repair publish list
```

### Controlled Software Repair Review & Operator Approval (Phase 8 Milestone 6)
```bash
# Review candidate repair dossier with verified facts and diff
forge repair review rep_12345

# Review candidate repair dossier in JSON format for automation
forge repair review rep_12345 --format json

# Export candidate repair dossier Markdown to file
forge repair review rep_12345 --output dossier.md

# Operator explicitly approves candidate repair with authorized downstream action
forge repair approve rep_12345 \
  --actor "lead_engineer@company.com" \
  --reason "Verified defect resolution and 0 regressions." \
  --action export_patch \
  --ttl 48.0

# Operator explicitly rejects candidate repair
forge repair reject rep_12345 \
  --actor "lead_engineer@company.com" \
  --reason "Unacceptable architectural side effects in implementation."

# Operator requests changes with actionable feedback
forge repair request-changes rep_12345 \
  --actor "lead_engineer@company.com" \
  --reason "Guard logic correct, but exception message requires clarification." \
  --feedback "Change 'Rate too high' to 'Discount rate must be between 0.0 and 1.0'."

# Inspect immutable operator review decision history and audit trail
forge repair approval-history rep_12345
```

### Isolated Defect Reproduction Contracts (Phase 3 Milestone 1)
```bash
# Validate reproduction contract schema and security bounds offline
forge sandbox check contracts/reproduce_example.json

# Execute defect reproduction inside an isolated sandbox or report fail-closed skip
forge sandbox reproduce contracts/reproduce_example.json -o reports/repro_result.json
```

### Reproduction Case Management & Evidence Capture (Phase 3 Milestone 2)
```bash
# Create a tracked reproduction case pinned to target commit SHA
forge repro create contracts/auth_defect.json --commit a1b2c3d4e5f6 --max-retries 1

# List reproduction cases (optionally filter by finding ID)
forge repro list
forge repro list --finding-id find_01
forge repro list --format json

# Inspect a reproduction case and its full attempt history
forge repro show case_find_01_7e3b9a1d4c20
forge repro show case_find_01_7e3b9a1d4c20 --format json

# Classify a reproduction case and its attempt history
forge repro classify case_find_01_7e3b9a1d4c20
forge repro classify case_find_01_7e3b9a1d4c20 --format json

# Execute or resume a reproduction case attempt inside the sandbox
forge repro run case_find_01_7e3b9a1d4c20
forge repro run case_find_01_7e3b9a1d4c20 --resume
forge repro run case_find_01_7e3b9a1d4c20 --format json
```

### Deterministic Reproduction Planning (Phase 3 Milestone 3)
```bash
# Generate a bounded reproduction plan for a finding
forge plan generate find_01 \
  --commit a1b2c3d4e5f6 \
  --repo https://github.com/example/repo.git \
  --command '["pytest", "tests/test_parser.py::test_off_by_one"]' \
  --expected "Loop index must remain < len(arr)" \
  --provenance trusted_fixture \
  --output plans/plan_find_01.json

# Validate a plan offline without execution (fail-secure policy check)
forge plan validate plans/plan_find_01.json

# Classify a plan's execution viability and oracle provenance
forge plan classify plans/plan_find_01.json

# Inspect plan metadata, strategy rationale, and provenance
forge plan show plans/plan_find_01.json

# Validate and execute a reproduction plan inside the sandbox
forge plan run plans/plan_find_01.json --workspace /path/to/workspace
```

### Diff-Aware Code Review (Phase 2 Milestones 1, 2, 5, & 6)
```bash
# Review latest commit changes against previous revision
forge review <repo-path> --base HEAD~1 --head HEAD

# Review branch differences
forge review <repo-path> --base main --head feature-branch

# Offline static review without LLM assistance
forge review <repo-path> --base main --head HEAD --no-llm

# Output review as JSON or standards-compliant SARIF 2.1.0
forge review <repo-path> --format json
forge review <repo-path> --format sarif -o reports/review.sarif
```

### CI Review Policy Enforcement (Phase 2 Milestone 6)
```bash
# Evaluate review findings against default CI policy
forge policy check reports/review.json

# Gate CI pipeline using custom declarative policy
forge policy check reports/review.json --policy docs/examples/ci_review_policy.json

# Output machine-readable policy decision for CI/CD pipelines
forge policy check reports/review.json --format json
```

### Review Quality & Security Benchmarks (Phase 2 Milestones 3, 4, & 5)
```bash
# Run standard review quality benchmarks
forge benchmark review

# Run held-out adversarial benchmark corpus
forge benchmark review --corpus heldout

# Run security dataflow benchmark corpus
forge benchmark review --corpus security

# Output benchmark metrics to JSON
forge benchmark review --format json -o reports/review_benchmark.json
```

### Repository Auditing & Investigation (Phase 1)
```bash
# Safely acquire and inspect repository structure (read-only)
forge inspect <repo-url-or-path> --no-llm

# Multi-turn LLM investigation (supports local Ollama or cloud providers)
forge investigate <repo-url-or-path> --provider ollama --model qwen2.5-coder:1.5b --max-steps 10

# Targeted commit investigation with custom instruction
forge investigate <repo-url> --commit <commit-sha> --instruction "Audit dependencies and security" --provider ollama --model qwen2.5-coder:1.5b --max-steps 12

# Export audit report
forge report <run-id> --format markdown -o reports/run.md

# Audit history & findings
forge runs
forge findings list
forge findings show <finding-id>

# Phase 1 repository benchmark suite
forge benchmark run --mode static
forge benchmark run --mode llm
```

The investigation engine supports multi-tier tool-calling protocols: native OpenAI function-calling wire frames, direct JSON objects, markdown code fences, and `<tool_call>` tags. Parameter schemas are strictly validated against tool specifications, and investigations without executed tools or findings cannot falsely complete.

#### Finding & Evidence Persistence Lifecycles
- **Authorized `record_finding` Tool**: Autonomous agents record concrete defects using the structured `record_finding` tool, persisting `Finding` records in SQLite in the initial `suspected` lifecycle state.
- **Server-Verified Evidence**: FORGE enforces zero-trust against model-provided excerpts. When a finding is recorded, the engine independently verifies path containment within the repository workspace, rejects symlink escapes, reads the ground-truth lines directly from disk, and computes a cryptographic SHA-256 digest linked to the newly created `EvidenceRecord`. Hallucinated line excerpts or fabricated source fragments are rejected at the persistence boundary.
- **Deterministic Evidence-to-Claim Validation**: Category-specific checks validate evidence relevance before persistence. For Python files, static AST parsing deterministically verifies syntax error claims (such as alleged missing colons): if the target file parses cleanly, false positive syntax claims are rejected. Line ranges must contain non-whitespace executable code and sufficient context for the defect category.
- **Auditable Duplicate Finding Prevention**: Deduplication detects overlapping source ranges with matching defect categories or high textual similarity, preventing duplicate findings from polluting reports while linking additional distinct evidence to existing findings when applicable.
- **Category & Evidence Consistency**: Enforces internal consistency between defect categories, titles, descriptions, and source code constructs (e.g. rejecting loop defect classifications that describe syntax errors or cite non-loop dictionary entries).
- **Uncertainty Preservation & Bounded Confidence**: Static AST checks prove or disprove syntax claims, but cannot prove semantic defects (e.g. logic bugs). For unverified semantic claims, FORGE preserves explicit uncertainty in `suspected` state, downgrades unearned `confirmed` confidence to `high`, and attaches explicit limitation disclosures.
- **Turn Budget Visibility & Penultimate Synthesis**: Investigation loops provide dynamic step-budget tracking (`[Turn Budget: Step M of N]`). On the penultimate step, the model receives an explicit synthesis warning. On the final step, tool availability is scoped strictly to `record_finding`, forcing compact models to synthesize findings rather than exploring further.
- **Same-File Pagination Safeguards**: Successive reads of the same file are tracked across turns; advisory warnings are emitted at $\ge 3$ reads, and execution halts with safety instructions at $> 4$ reads to prevent compact models from exhausting turn budgets on pagination loops.
- **Truthful Investigation Outcomes**: When turn budgets expire without final synthesis, investigations are recorded honestly as `MAX_STEPS_EXCEEDED` rather than falsely marked `COMPLETED`. Investigations with zero findings explicitly report `Findings discovered: 0 (No defect findings identified)`.

---

## Benchmark Metrics & Evaluation

FORGE measures detection accuracy against curated ground-truth fixtures:
- **True Positive (TP)**: Target defect correctly detected in affected file and symbol.
- **False Positive (FP)**: Defect falsely claimed on clean control fixtures or unaffected files.
- **False Negative (FN)**: Target defect missed by the analysis.
- **True Negative (TN)**: Clean control fixture correctly identified with 0 defect findings.
- **Precision**: $\frac{TP}{TP + FP}$ (N/A if no defects reported).
- **Recall**: $\frac{TP}{TP + FN}$ (N/A if no seeded defects present).
- **F1 Score**: Harmonic mean of Precision and Recall.
- **Case Pass Rate**: Percentage of benchmark cases where outcome matched ground truth.

---

## Sandboxing & Fail-Closed Security Policy

FORGE enforces strict container isolation when running code from target repositories:
- **Network Isolation**: Containers are launched with `--network none`; dynamic calls have zero external network access.
- **Read-Only Filesystem**: The target workspace is mounted read-only (`--mount type=bind,...,readonly`); repository code cannot mutate host files.
- **Non-Root Execution**: Runs strictly as UID:GID `1000:1000`.
- **Privilege Dropping**: Configured with `--security-opt no-new-privileges` and `--cap-drop ALL`.
- **Fail-Closed Host Protection**: When Docker is unavailable, dynamic execution is marked `SKIPPED_SANDBOX_UNAVAILABLE` (or `BLOCKED`). FORGE strictly refuses to fall back to executing untrusted repository code directly on the host machine.
- **Real Integration Validation**: Opt-in integration tests verify all isolation boundaries against a live Docker daemon:
  ```bash
  FORGE_DOCKER_TESTS=1 pytest tests/integration/test_docker_sandbox_integration.py
  ```

---

## Configuration

Copy `.env.example` to `.env` and configure as needed:
```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `FORGE_LLM_PROVIDER` | `ollama` | Provider: `ollama` (default local), `groq`, `gemini`, `deepseek`, or `fake` (offline) |
| `FORGE_LLM_MODEL` | *(empty)* | Model identifier override (e.g. `qwen2.5-coder:1.5b`; provider defaults used when empty) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Base URL for local Ollama server (compatible with Friday convention) |
| `GROQ_API_KEY` | *(empty)* | API key for Groq Cloud (compatible with Friday convention) |
| `GEMINI_API_KEY` | *(empty)* | API key for Google Gemini (compatible with Friday convention) |
| `DEEPSEEK_API_KEY` | *(empty)* | API key for DeepSeek (compatible with Friday convention) |
| `FORGE_ALLOW_CLOUD_FALLBACK` | `false` | Security guard: silent cloud fallback is strictly forbidden |
| `FORGE_LLM_API_BASE` | *(empty)* | Custom endpoint base URL override (vLLM, custom proxies) |
| `FORGE_MAX_STEPS` | `10` | Max investigation iterations |
| `FORGE_DOCKER_IMAGE` | `python:3.11-slim` | Base container image for sandboxed execution |

### Local Ollama Setup & Usage

FORGE standardizes on local Ollama execution (`http://localhost:11434/v1`). Hosted/cloud Ollama inference is explicitly rejected.

1. **Install and Run Ollama**: Ensure Ollama is running locally:
   ```bash
   ollama serve
   ```
2. **Download Model**:
   ```bash
   ollama pull qwen2.5-coder:1.5b
   ```
3. **Configure Environment**:
   ```bash
   export FORGE_LLM_PROVIDER=ollama
   export FORGE_LLM_MODEL=qwen2.5-coder:1.5b
   export OLLAMA_BASE_URL=http://localhost:11434  # default
   ```
4. **Guarantees & Troubleshooting**:
   - **No Silent Cloud Routing**: Requesting `ollama_cloud` fails immediately with `LLMUnavailableError`.
   - **Local Network Failure**: If local Ollama is down or connection is refused, FORGE fails closed with `LLMUnavailableError` without falling back to cloud providers or fake mocks (`FORGE_ALLOW_CLOUD_FALLBACK=false`).
   - **Model Availability**: If the requested model is not installed locally, Ollama returns an error and FORGE surfaces the failure honestly.
   - **Structured Output Support**: FORGE sends an explicit OpenAI-compatible `response_format` with `json_schema` derived from `RepairModelResponseSchema` to ensure strict JSON output.
   - **Syntax Error Recovery & Bounded Retries**: If a model generates malformed JSON (such as a trailing comma before a closing brace), FORGE rejects permissive rewriting and instead performs a bounded retry (max 3 retries, 120s timeout, 50,000 char limit) with concise diagnostic feedback so the local model can correct its output. If retries are exhausted, it fails closed safely.

---

## Documentation & Architecture References

- Engineering Rules & Mandatory Release Gates: [`AGENTS.md`](AGENTS.md)
- Phase 1 Specification: [`docs/specs/phase-1.md`](docs/specs/phase-1.md)
- Phase 2 Specifications:
  - Diff Review: [`docs/specs/phase-2-milestone-1.md`](docs/specs/phase-2-milestone-1.md)
  - Evidence Calibration: [`docs/specs/phase-2-milestone-2.md`](docs/specs/phase-2-milestone-2.md)
  - Review Benchmarks: [`docs/specs/phase-2-milestone-3.md`](docs/specs/phase-2-milestone-3.md)
  - Adversarial Generalization: [`docs/specs/phase-2-milestone-4.md`](docs/specs/phase-2-milestone-4.md)
  - Security Dataflow Analysis: [`docs/specs/phase-2-milestone-5.md`](docs/specs/phase-2-milestone-5.md)
  - CI Review Policy & SARIF: [`docs/specs/phase-2-milestone-6.md`](docs/specs/phase-2-milestone-6.md)
- Phase 3 Specifications:
  - Sandbox Execution Contracts: [`docs/specs/phase-3-milestone-1.md`](docs/specs/phase-3-milestone-1.md)
  - Case Management & Evidence Capture: [`docs/specs/phase-3-milestone-2.md`](docs/specs/phase-3-milestone-2.md)
  - Deterministic Reproduction Planning: [`docs/specs/phase-3-milestone-3.md`](docs/specs/phase-3-milestone-3.md)
  - Outcome Classification & Evidence Validation: [`docs/specs/phase-3-milestone-4.md`](docs/specs/phase-3-milestone-4.md)
  - Reproduction Reliability & Regression Hardening: [`docs/specs/phase-3-milestone-5.md`](docs/specs/phase-3-milestone-5.md)
  - End-to-End Reproduction Acceptance: [`docs/specs/phase-3-milestone-6.md`](docs/specs/phase-3-milestone-6.md)
- Phase 3→4 Transition & Pre-Phase 4:
  - Multi-Provider Configuration & Credential Reuse: [`docs/specs/phase-3-provider-transition.md`](docs/specs/phase-3-provider-transition.md)
  - Local Ollama & Multi-Provider LLM Integration: [`docs/specs/pre-phase-4-llm-providers.md`](docs/specs/pre-phase-4-llm-providers.md)
  - Local Ollama Standardization & Cloud Removal: [`docs/specs/local-ollama-standardization.md`](docs/specs/local-ollama-standardization.md)
  - Ollama Cloud Provider Integration (Superseded): [`docs/specs/ollama-cloud-provider.md`](docs/specs/ollama-cloud-provider.md)
  - Removal of OpenAI API Provider: [`docs/specs/remove-openai-provider.md`](docs/specs/remove-openai-provider.md)
  - Phase 4 Readiness & Sandbox Gate: [`docs/specs/phase-4-readiness.md`](docs/specs/phase-4-readiness.md)
- Phase 4 Specifications:
  - Evidence-Grounded Repair Planning & Patch Generation: [`docs/specs/phase-4-m1-repair-planning.md`](docs/specs/phase-4-m1-repair-planning.md)
  - Isolated Patch Application & Verification: [`docs/specs/phase-4-m2-patch-verification.md`](docs/specs/phase-4-m2-patch-verification.md)
  - Repair Quality & Regression Benchmarking: [`docs/specs/phase-4-m3-repair-benchmarks.md`](docs/specs/phase-4-m3-repair-benchmarks.md)
  - Controlled Repair Orchestration & Lifecycle: [`docs/specs/phase-4-m4-repair-orchestration.md`](docs/specs/phase-4-m4-repair-orchestration.md)
  - End-to-End Repair Reliability & Adversarial Testing: [`docs/specs/phase-4-m5-reliability.md`](docs/specs/phase-4-m5-reliability.md)
  - Final Acceptance & Release Readiness: [`docs/specs/phase-4-m6-acceptance.md`](docs/specs/phase-4-m6-acceptance.md)
  - Docker Integration Validation & Deterministic Test Repair: [`docs/specs/docker-integration-validation.md`](docs/specs/docker-integration-validation.md)
- Phase 5 Specifications:
  - Supervised End-to-End Engineering Workflow Architecture & Roadmap: [`docs/specs/phase-5-roadmap.md`](docs/specs/phase-5-roadmap.md)
  - Unified Task Intake & Repository Context Pipeline: [`docs/specs/phase-5-m1-intake-context.md`](docs/specs/phase-5-m1-intake-context.md)
  - Supervised Workflow State Machine & Transactional Ledger: [`docs/specs/phase-5-m2-workflow-ledger.md`](docs/specs/phase-5-m2-workflow-ledger.md)
  - Automated Evidence Chaining from Reproduction to Repair: [`docs/specs/phase-5-m3-evidence-chaining.md`](docs/specs/phase-5-m3-evidence-chaining.md)
  - Multi-Check Regression & Safety Verification: [`docs/specs/phase-5-m4-multi-check-verification.md`](docs/specs/phase-5-m4-multi-check-verification.md)
  - Certified Engineering Dossier & Operator Export: [`docs/specs/phase-5-m5-dossier-export.md`](docs/specs/phase-5-m5-dossier-export.md)
  - End-to-End Workflow Acceptance, Reliability & Release: [`docs/specs/phase-5-m6-acceptance.md`](docs/specs/phase-5-m6-acceptance.md)
  - Repair Planner Grounding, Applicability & Scope Safety: [`docs/specs/repair-planner-reliability.md`](docs/specs/repair-planner-reliability.md)
  - Local Ollama Structured Output & Syntax Error Recovery: [`docs/specs/local-ollama-structured-output.md`](docs/specs/local-ollama-structured-output.md)
  - Patch Generation Reliability & Diff Syntax Enforcement: [`docs/specs/patch-generation-reliability.md`](docs/specs/patch-generation-reliability.md)
  - Structured Patch Construction: [`docs/specs/structured-patch-construction.md`](docs/specs/structured-patch-construction.md)
  - Structured Proposal Wire-Schema Reliability: [`docs/specs/structured-proposal-wire-schema.md`](docs/specs/structured-proposal-wire-schema.md)
  - Deterministic Patch Anchoring & Minimal Replacements: [`docs/specs/patch-anchoring-minimal-replacements.md`](docs/specs/patch-anchoring-minimal-replacements.md)
  - Deterministic Edit Anchoring & Source-Grounded Repair: [`docs/specs/deterministic-edit-anchoring.md`](docs/specs/deterministic-edit-anchoring.md)
  - Repair Reliability & Generalization: [`docs/specs/repair-reliability-generalization.md`](docs/specs/repair-reliability-generalization.md)
- Example CI Policy: [`docs/examples/ci_review_policy.json`](docs/examples/ci_review_policy.json)
