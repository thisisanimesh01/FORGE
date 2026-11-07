# Phase 12, Milestone 3: Repair Planning Engine

**Milestone Report**
**Repository:** `FORGE`
**Milestone:** Phase 12 M3 — Repair Planning Engine
**Status:** Completed
**Branch:** `phase-12-m3-repair-planning`

---

## 1. Executive Summary

Phase 12 Milestone 3 implements the **Repair Planning Engine** for FORGE. The engine transforms verified defect diagnoses (Phase 12 M1) and causal explanations (Phase 12 M2) into structured, minimal, risk-aware repair plans without modifying repository source code or executing untrusted instructions.

The milestone enforces non-negotiable safety and certainty invariants:
1. **Planning-Only Boundary**: M3 is strictly an analytical planning milestone. It must never write application source files, apply patches, execute code, run shell commands, create Git commits, or push changes.
2. **Mandatory Invariants in Code**: Every generated plan enforces `approval_required = True` and `execution_permitted = False` via Pydantic model validators, raising validation errors if any execution is attempted.
3. **Root-Cause Grounding & Certainty Gating**:
   - `PROVEN` root causes support high-confidence `ACTIONABLE` repair plans.
   - `PLAUSIBLE` root causes yield `CONDITIONAL` plans with explicit uncertainty limitations.
   - `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` preserves separate candidate repair plans and strategies.
   - `INSUFFICIENT_EVIDENCE` and `UNRESOLVED` causes strictly produce non-actionable plans requiring manual triage, prohibiting automated code modifications.
4. **Strategy Minimality**: Prefers the smallest safe repair addressing the verified root cause (e.g. localized logic corrections or input validation guards) over broad refactoring or unrequested dependency changes.
5. **Verified Verification & Rollback**: Recommends test suites verified to exist on disk while explicitly labeling proposed tests. Generates explicit rollback procedures including reverse changes, trigger conditions, and acknowledged limitations.
6. **Read-Only ToolRegistry Integration**: Exposes 6 bounded, read-only tools with secret redaction and schema validation.

---

## 2. Architecture & Component Design

The implementation is packaged under `app/forge/repair_planning/`:

```
app/forge/repair_planning/
├── __init__.py           # Package exports and public interfaces
├── schemas.py            # Strict Pydantic models for plans, strategies, risks, and tools
├── evidence.py           # Evidence collection, path containment, and freshness validation
├── strategies.py         # Candidate repair strategy generator and multi-criteria ranking
├── risk.py               # Deterministic risk analyzer and dependency reach evaluator
├── rollback.py           # Rollback plan builder specifying reverse steps and triggers
├── validation.py         # Plan invariant validator enforcing safety and certainty rules
├── planner.py            # RepairPlanningEngine orchestrating end-to-end plan construction
└── tools.py              # RepairPlanningTools integrating 6 tools into ToolRegistry
```

### 2.1 Strict Data Contracts (`schemas.py`)

All models enforce `ConfigDict(extra="forbid")` and explicit character budgets:

- **Taxonomy Enums**:
  - `RepairApproach`: `LOCAL_LOGIC_CORRECTION`, `INPUT_VALIDATION`, `ERROR_HANDLING`, `RESOURCE_MANAGEMENT`, `DEPENDENCY_INTEGRATION`, `CONCURRENCY_CORRECTION`, `SECURITY_HARDENING`, `CONFIGURATION_CORRECTION`, `TEST_COVERAGE_IMPROVEMENT`, `UNKNOWN_OR_MANUAL_INVESTIGATION`.
  - `StrategyComplexity`: `LOW`, `MEDIUM`, `HIGH`.
  - `StrategyInvasiveness`: `MINIMAL`, `MODERATE`, `EXTENSIVE`.
  - `StrategyReversibility`: `HIGH`, `MODERATE`, `LOW`.
  - `RiskSeverity`: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`.
  - `RiskLikelihood`: `LOW`, `MEDIUM`, `HIGH`.
  - `RepairPlanStatus`: `ACTIONABLE`, `CONDITIONAL`, `MULTIPLE_CANDIDATES`, `INSUFFICIENT_EVIDENCE`, `UNRESOLVED`.

- **Core Plan Models**:
  - `RepairPlan`: Includes `plan_id`, `finding_id`, `root_cause_reference`, `status`, `summary`, `problem_statement`, `repair_objective`, `selected_strategy_id`, `candidate_strategies`, `affected_files`, `affected_symbols`, `required_changes`, `preconditions`, `dependencies`, `risk_assessment`, `expected_side_effects`, `verification_plan`, `rollback_plan`, `supporting_evidence`, `confidence`, `limitations`, `approval_required` (strictly `True`), `execution_permitted` (strictly `False`), and `created_at`.
  - `RepairStrategy`: Encapsulates strategy title, description, approach, affected files/symbols, supporting evidence IDs, expected benefits, risks, complexity, invasiveness, reversibility, test requirements, rejection reasons, `is_recommended`, and deterministic ranking `score`.
  - `RepairRiskAssessment`: Detailed breakdown of overall risk, likelihood, heuristic risk score in $[0.0, 1.0]$, affected components, potential regressions, security implications, data integrity implications, dependency implications, and narrative rationale.
  - `VerificationPlan`: Distinguishes `existing_targeted_tests` (verified on disk) from `recommended_new_tests` (explicitly labeled proposed tests), regression test scope, security checks, and static analysis requirements.
  - `RollbackPlan`: Defines `changes_to_reverse`, `files_and_symbols_involved`, `dependencies_to_restore`, `data_configuration_considerations`, `rollback_triggers`, and `known_limitations`.
  - `RequiredChange`: Localized modification specifying `file_path`, `symbol_id`, `start_line`, `end_line`, `change_type`, `description`, and `rationale`.
  - `RepairEvidence`: Grounded evidence record with path containment, start/end lines, SHA-256 digest, and `is_fresh` flag.

### 2.2 Evidence Collection & Freshness Validation (`evidence.py`)

The `RepairEvidenceCollector`:
- Gathers grounded evidence from `RootCauseReport` (primary root cause, causal chains, symptom locations, store evidence references) and `DiagnosisResult` (candidate locations, evaluated hypotheses).
- Enforces repository containment using `assert_contained_path` to prevent path traversal outside workspace bounds.
- Rejects protected files via `is_protected_path` (e.g. `.env`, `.git/config`, `*.db`).
- Audits file existence, line bounds ($1 \le \text{start\_line} \le \text{end\_line} \le \text{total\_lines}$), and content hash consistency. Marks stale evidence and records explicit limitations if files have changed since diagnosis.

### 2.3 Candidate Strategy Generation & Minimality (`strategies.py`)

The `RepairStrategyGenerator`:
- Evaluates root cause node type, role, defect category, and causal chain edges.
- Maps defect mechanisms to candidate strategies:
  - Input validation or precondition violations $\rightarrow$ `INPUT_VALIDATION`.
  - Calculation, bounds, or branch logic $\rightarrow$ `LOCAL_LOGIC_CORRECTION`.
  - Unhandled exceptions $\rightarrow$ `ERROR_HANDLING`.
  - Resource lifecycle $\rightarrow$ `RESOURCE_MANAGEMENT`.
  - Security boundaries $\rightarrow$ `SECURITY_HARDENING`.
- Preserves alternative approaches and generates a disfavored broad architectural refactor strategy to enforce minimality.
- Evaluates strategies across invasiveness, complexity, and reversibility, computing a deterministic score in $[0.0, 1.0]$. The highest-scoring non-rejected strategy with `MINIMAL` invasiveness is selected as recommended.
- If root-cause status is `INSUFFICIENT_EVIDENCE` or `UNRESOLVED`, generates `UNKNOWN_OR_MANUAL_INVESTIGATION` with non-actionable rejection reasons.

### 2.4 Risk Assessment & Dependency Reach (`risk.py`)

The `RepairRiskAnalyzer`:
- Evaluates change scope: counts affected files and symbols.
- Inspects call graph dependency reach: queries `IncrementalRepositoryIndex` for incoming callers of affected symbols.
  - $\ge 5$ callers $\rightarrow$ High dependency reach.
  - $1\text{--}4$ callers $\rightarrow$ Moderate dependency reach.
  - $0$ callers $\rightarrow$ Isolated / internal reach.
- Audits public API compatibility: checks whether modified symbols are public (non-underscored).
- Scans for security boundaries (auth, token, secret, cert, crypt, validat, sanitiz, sandbox).
- Scans for data integrity boundaries (db, store, persist, delete, schema).
- Checks test coverage on disk for affected files; elevates risk likelihood if automated tests are absent.
- Accounts for causal uncertainty (`PLAUSIBLE` or `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` elevates regression risk).

### 2.5 Verification & Rollback Planning (`rollback.py` & `planner.py`)

- **Verification**: Queries disk for test files matching affected module names (`tests/unit/test_<name>.py`, `tests/test_<name>.py`). Explicitly formats proposed new tests with `[PROPOSED]` to avoid fabricating non-existent test files.
- **Rollback**: Formulates exact inverse code modifications, dependency lockfile pins, cache invalidation directives, concrete rollback trigger conditions, and acknowledged rollback limitations (e.g. external network calls or persistent state).

### 2.6 Invariant & Safety Validation (`validation.py`)

The `RepairPlanValidator` verifies:
1. `approval_required == True`
2. `execution_permitted == False`
3. All affected files exist within the workspace boundary and are non-protected regular files.
4. Line ranges for required changes are valid.
5. Root-cause certainty constraints:
   - `PROVEN` $\rightarrow$ `ACTIONABLE`
   - `PLAUSIBLE` $\rightarrow$ `CONDITIONAL`
   - `MULTIPLE_PLAUSIBLE_ROOT_CAUSES` $\rightarrow$ `MULTIPLE_CANDIDATES`
   - `INSUFFICIENT_EVIDENCE` / `UNRESOLVED` $\rightarrow$ non-actionable status, no active repair strategy.
6. Verification test integrity: all tests in `existing_targeted_tests` exist on disk; all recommended new tests are explicitly labeled as proposed.
7. Strategy minimality: prevents selecting `EXTENSIVE` refactoring when `MINIMAL` local fixes exist.

### 2.7 ToolRegistry Integration (`tools.py`)

`RepairPlanningTools` registers 6 typed, read-only tools with `ToolRegistry`:
1. `create_repair_plan`: Generates an evidence-grounded repair plan from diagnostic or failure signals.
2. `compare_repair_strategies`: Compares candidate strategies across complexity, invasiveness, and reversibility.
3. `assess_repair_risk`: Analyzes potential side effects, dependency reach, and regressions.
4. `explain_repair_plan`: Summarizes plan objectives, selected strategy, and trade-offs.
5. `validate_repair_plan`: Audits a plan against safety and certainty invariants.
6. `generate_verification_plan`: Formulates verified on-disk test suites and proposed tests.

---

## 3. Benchmark Results

Measured with `app/forge/benchmarks/benchmark_phase12_m3_repair_planning.py`:

| Operation | Latency (Mean) | Latency (Median) | Notes |
| :--- | :--- | :--- | :--- |
| **Evidence Collection & Freshness Audit** | 5.105 ms | 5.016 ms | Audits disk content and SHA-256 hashes |
| **Candidate Strategy Generation** | 0.122 ms | 0.118 ms | 3 candidate strategies synthesized |
| **Strategy Comparison & Ranking** | 0.008 ms | 0.008 ms | Deterministic multi-criteria scoring |
| **Risk Assessment & Dependency Reach** | 0.320 ms | 0.232 ms | Call graph inspection and scope analysis |
| **Verification-Plan Construction** | 0.309 ms | 0.285 ms | Disk test verification & proposal formatting |
| **Rollback-Plan Construction** | 0.027 ms | 0.025 ms | Reverse steps, triggers, and limitations |
| **Plan Invariant Validation** | 1.339 ms | 1.250 ms | 7 safety invariants verified |
| **Full Deterministic Planning (Cold)** | 10.83 ms | — | Initial plan generation |
| **Full Deterministic Planning (Warm)** | 8.22 ms | 7.78 ms | Min: 6.67 ms, Max: 11.19 ms across 25 iterations |
| **Peak Memory Overhead** | 127.46 MB | — | Includes full workspace AST symbol index |

*Inference Separation*: Zero external cloud or local Ollama LLM latency involved in deterministic baseline.

---

## 4. Test Verification

- **Dedicated M3 Test Suite**: `tests/unit/test_phase12_m3_repair_planning.py`
  - 34 dedicated unit tests covering plan construction, evidence freshness, strategy ranking, risk scoring, rollback specifications, safety invariants, and ToolRegistry execution.
  - **Result: 34 passed in 6.19s**.
- **Full Regression Suite**: All existing test suites (M1–M2, Phase 11 repository intelligence, sandbox adapters, and storage) continue to pass with zero regressions.

---

## 5. Security and Safety Guarantees

1. **No Application File Writes**: `RepairPlanningEngine` and `RepairPlanningTools` perform read-only analysis. Source files are strictly unmodified during planning.
2. **Approval Mandatory**: `approval_required` is hardcoded to `True` and validated by model validators.
3. **Execution Blocked**: `execution_permitted` is hardcoded to `False`. Code repair execution is strictly deferred to Phase 12 M4.
4. **Secret Sanitization**: Tool outputs pass through `redact_secrets` and enforce a 50,000-character budget.
5. **Path Containment**: Path traversal (`../../`) and protected files (`.env`, `.git`) are strictly rejected.

---

## 6. Audit Findings Remediation & Immutability Guarantees

Following an independent technical audit, two key safety and integrity mechanisms were reinforced:

### 6.1 Immutable `RepairPlan` & Copy Safety
- **Enforced Immutability**: `RepairPlan.model_config` specifies `validate_assignment=True` and `frozen=True`. Direct in-place mutation of safety flags (`plan.execution_permitted = True` or `plan.approval_required = False`) is intercepted and rejected with a Pydantic `ValidationError`.
- **Copy Revalidation**: In Pydantic v2, `model_copy(update=...)` bypasses validators by default. `RepairPlan` explicitly overrides `model_copy(update=...)` to revalidate the updated plan against all schema and safety invariants via `model_validate`, prohibiting any attempt to bypass constraints via shallow copying.
- **Trust Boundary Enforcement**: `RepairPlanValidator.validate_plan()` and `RepairPlanningTools.validate_repair_plan()` perform deep reconstruction and invariant revalidation whenever externally supplied plans or serialized plan JSON is accepted.
- **Non-Actionable Plan Changes Prohibited**: `validate_safety_invariants` explicitly forbids `required_changes` when plan status is `INSUFFICIENT_EVIDENCE` or `UNRESOLVED`.

### 6.2 Mandatory Stale-Evidence Status Downgrade
- **Pre-Constructed Report Override Prohibited**: A pre-constructed `RootCauseReport` with `status=PROVEN` cannot override stale, modified, or missing source files on disk.
- **Freshness Reassessment**: `RepairPlanningEngine` audits evidence freshness, line range bounds ($start\_line \le end\_line \le total\_lines$), and SHA-256 slice digests against current repository files. If any supporting evidence is stale, drifted, or unverified:
  - `plan_status` is automatically downgraded to `INSUFFICIENT_EVIDENCE`.
  - `confidence` is capped to `LOW`.
  - All candidate repair strategies are marked non-recommended (`is_recommended=False`) and assigned explicit rejection reasons.
  - Active repair strategy selection and required code changes are prohibited (`selected_strategy_id = None`, `required_changes = []`).
  - Stale evidence details are preserved and detailed in `plan.limitations`.
- **Race Condition Prevention**: `RepairPlanningEngine` re-evaluates file existence and SHA-256 hashes immediately before assembling the final plan to guard against concurrent disk modifications during plan synthesis.
