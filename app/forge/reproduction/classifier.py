"""Deterministic reproduction outcome classifier and evidence validator.

Part of Phase 3, Milestone 4: Reproduction Outcome Classification and Evidence Validation.

Provides:
- Strongly typed versioned outcomes: REPRODUCED, NOT_REPRODUCED, UNRELATED_FAILURE,
  ENVIRONMENT_BLOCKED, TIMEOUT, FLAKY, INVALID_PLAN, INCONCLUSIVE.
- Deterministic reason codes and explicit precedence rules.
- Strict oracle presence and provenance verification (never infer reproduction from
  generic nonzero exit or model assertion).
- Conservative handling of stale revisions, truncated evidence, cleanup failures,
  and inconsistent multi-attempt histories.
- Complete idempotency and secret redaction.
"""

from datetime import datetime, timezone
from enum import Enum
import hashlib
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from forge.config import redact_secrets
from forge.reproduction.models import (
    FieldProvenance,
    ReproductionAttempt,
    ReproductionCase,
)
from forge.reproduction.planner import (
    PlanValidationResult,
    ReproductionPlan,
    ReproductionStrategy,
)
from forge.sandbox.contracts import (
    ReproductionOutcome,
    ReproductionRequest,
    ReproductionResult,
)


CLASSIFIER_VERSION = "1.0.0"

_MAX_DIAGNOSTIC_CHARS = 4000

# Trusted provenance sources permitted to act as an oracle
_TRUSTED_ORACLE_SOURCES = {
    FieldProvenance.TRUSTED_FIXTURE,
    FieldProvenance.STATIC_ANALYSIS,
}

# Known Python / test harness exception indicators
_UNRELATED_EXCEPTION_PATTERNS = [
    re.compile(r"SyntaxError:\s", re.IGNORECASE),
    re.compile(r"ModuleNotFoundError:\s*No module named", re.IGNORECASE),
    re.compile(r"ImportError:\s*cannot import name", re.IGNORECASE),
    re.compile(r"pip:\s*command not found", re.IGNORECASE),
    re.compile(r"pytest:\s*command not found", re.IGNORECASE),
    re.compile(r"FileNotFoundError:\s*\[Errno 2\]", re.IGNORECASE),
    re.compile(r"PermissionError:\s*\[Errno 13\]", re.IGNORECASE),
    re.compile(r"InternalServerError", re.IGNORECASE),
]


class ClassificationOutcome(str, Enum):
    """Canonical, versioned reproduction outcome taxonomy."""
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    UNRELATED_FAILURE = "unrelated_failure"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    TIMEOUT = "timeout"
    FLAKY = "flaky"
    INVALID_PLAN = "invalid_plan"
    INCONCLUSIVE = "inconclusive"


class ClassificationReasonCode(str, Enum):
    """Machine-readable, deterministic reason codes for classification decisions."""
    ORACLE_VIOLATION_CONFIRMED = "oracle_violation_confirmed"
    ORACLE_OBSERVED_NO_VIOLATION = "oracle_observed_no_violation"
    ORACLE_ABSENT_OR_UNTRUSTED = "oracle_absent_or_untrusted"
    GENERIC_NONZERO_EXIT = "generic_nonzero_exit"
    UNRELATED_EXCEPTION = "unrelated_exception"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    PLAN_POLICY_VIOLATION = "plan_policy_violation"
    STALE_REVISION_MISMATCH = "stale_revision_mismatch"
    EVIDENCE_TRUNCATED_OR_MISSING = "evidence_truncated_or_missing"
    CLEANUP_FAILURE = "cleanup_failure"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    INCONSISTENT_RETRIES = "inconsistent_retries"
    AMBIGUOUS_OUTPUT_SIGNALS = "ambiguous_output_signals"


class ReproductionClassification(BaseModel):
    """Structured, immutable record of an evaluated reproduction outcome."""
    model_config = ConfigDict(frozen=True)

    classification_id: str = Field(description="Deterministic classification record ID")
    outcome: ClassificationOutcome = Field(description="Classified outcome")
    reason_code: ClassificationReasonCode = Field(description="Deterministic reason code")
    explanation: str = Field(description="Human-readable sanitized explanation")
    classifier_version: str = Field(default=CLASSIFIER_VERSION, description="Classifier schema version")
    case_id: Optional[str] = Field(default=None, description="Associated case ID")
    plan_id: Optional[str] = Field(default=None, description="Associated plan ID")
    attempt_id: Optional[str] = Field(default=None, description="Associated attempt ID if applicable")
    attempt_number: Optional[int] = Field(default=None, description="Attempt sequence number")
    evidence_id: Optional[str] = Field(default=None, description="Supporting evidence ID")
    is_reproduced: bool = Field(default=False, description="Whether reproduction was verified")
    oracle_present: bool = Field(default=False, description="Whether an independent oracle was defined")
    oracle_verified: bool = Field(default=False, description="Whether the oracle was independently sourced")
    provenance: Dict[str, FieldProvenance] = Field(default_factory=dict, description="Field provenance")
    details: Dict[str, Any] = Field(default_factory=dict, description="Additional diagnostic context")
    classified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def generate_classification_id(case_id: str, attempt_number: Optional[int] = None) -> str:
    """Compute a deterministic classification ID from case ID and attempt number."""
    if attempt_number is not None:
        raw = f"{case_id}::attempt_{attempt_number}"
    else:
        raw = f"{case_id}::aggregate"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"cls_{digest}"


class ReproductionOutcomeClassifier:
    """Deterministic, policy-enforcing classifier for reproduction attempts and case histories.

    Precedence order:
    1. Plan / Revision Validity -> INVALID_PLAN
    2. Environment / Sandbox Boundary -> ENVIRONMENT_BLOCKED
    3. Timeout Quota Exceeded -> TIMEOUT
    4. Execution Integrity (interruption, missing evidence, cleanup failure) -> INCONCLUSIVE
    5. Multi-attempt inconsistency -> FLAKY
    6. Oracle Presence & Evaluation:
       - Untrusted/absent oracle -> UNRELATED_FAILURE (if error) or INCONCLUSIVE (if pass)
       - Trusted oracle violation confirmed -> REPRODUCED
       - Trusted oracle observed clean run -> NOT_REPRODUCED
       - Non-matching error output / generic exit -> UNRELATED_FAILURE
       - Ambiguous / conflicting signals -> INCONCLUSIVE
    """

    def __init__(self, classifier_version: str = CLASSIFIER_VERSION):
        self.version = classifier_version

    def _is_trusted_oracle(self, provenance: Optional[FieldProvenance]) -> bool:
        """Verify whether expected behavior provenance qualifies as an independent oracle."""
        if provenance is None:
            return False
        return provenance in _TRUSTED_ORACLE_SOURCES

    def _sanitize(self, text: str) -> str:
        """Redact secrets and bound string length."""
        redacted = redact_secrets(text)
        if len(redacted) > _MAX_DIAGNOSTIC_CHARS:
            return redacted[:_MAX_DIAGNOSTIC_CHARS] + " ... [TRUNCATED]"
        return redacted

    def classify_attempt(
        self,
        attempt: ReproductionAttempt,
        plan: Optional[ReproductionPlan] = None,
        case: Optional[ReproductionCase] = None,
        validation_result: Optional[PlanValidationResult] = None,
        is_stale_revision: bool = False,
        is_interrupted: bool = False,
    ) -> ReproductionClassification:
        """Classify a single reproduction attempt with full provenance and reason code."""
        case_id = case.case_id if case else attempt.case_id
        plan_id = plan.plan_id if plan else (case.contract.request_id if case else None)
        evidence_id = attempt.evidence_id
        attempt_id = attempt.attempt_id
        attempt_number = attempt.attempt_number

        # ------------------------------------------------------------------ #
        # Precedence 1: Plan & Revision Validity
        # ------------------------------------------------------------------ #
        if is_stale_revision:
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.INVALID_PLAN,
                reason_code=ClassificationReasonCode.STALE_REVISION_MISMATCH,
                explanation="Repository revision does not match the pinned case revision; execution refused.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=False,
                oracle_verified=False,
                details={"is_stale_revision": True},
            )

        if validation_result and not validation_result.is_valid:
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.INVALID_PLAN,
                reason_code=ClassificationReasonCode.PLAN_POLICY_VIOLATION,
                explanation=self._sanitize(
                    f"Plan validation failed: {'; '.join(validation_result.violations)}"
                ),
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=False,
                oracle_verified=False,
                details={"violations": validation_result.violations},
            )

        if plan and plan.strategy == ReproductionStrategy.INSUFFICIENT_INFORMATION:
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.INVALID_PLAN,
                reason_code=ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED,
                explanation="Plan has strategy INSUFFICIENT_INFORMATION; cannot be executed as a valid test.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=False,
                oracle_verified=False,
            )

        # ------------------------------------------------------------------ #
        # Precedence 2: Environment & Sandbox Availability
        # ------------------------------------------------------------------ #
        env_blocked_outcomes = {
            ReproductionOutcome.BLOCKED,
            ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE,
            ReproductionOutcome.ENVIRONMENT_BLOCKED,
        }
        if attempt.outcome in env_blocked_outcomes:
            diag = attempt.diagnostic_message or "Sandbox environment unavailable; fail-closed."
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.ENVIRONMENT_BLOCKED,
                reason_code=ClassificationReasonCode.ENVIRONMENT_UNAVAILABLE,
                explanation=self._sanitize(f"Environment blocked: {diag}"),
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=False,
                oracle_verified=False,
            )

        # ------------------------------------------------------------------ #
        # Precedence 3: Timeout Quota
        # ------------------------------------------------------------------ #
        if attempt.outcome == ReproductionOutcome.TIMEOUT:
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.TIMEOUT,
                reason_code=ClassificationReasonCode.TIMEOUT_EXCEEDED,
                explanation=self._sanitize(
                    f"Execution exceeded timeout limit ({attempt.duration_ms:.0f}ms); terminated."
                ),
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=False,
                oracle_verified=False,
            )

        # ------------------------------------------------------------------ #
        # Precedence 4: Execution Integrity & Evidence Quality
        # ------------------------------------------------------------------ #
        if is_interrupted:
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.INCONCLUSIVE,
                reason_code=ClassificationReasonCode.EXECUTION_INTERRUPTED,
                explanation="Execution was aborted or interrupted before an outcome could be determined.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
            )

        if attempt.cleanup_status != "cleaned":
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.INCONCLUSIVE,
                reason_code=ClassificationReasonCode.CLEANUP_FAILURE,
                explanation=f"Sandbox scratch cleanup reported status '{attempt.cleanup_status}'; evidence inconclusive.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
            )

        # ------------------------------------------------------------------ #
        # Precedence 5: Oracle Verification & Evidence Evaluation
        # ------------------------------------------------------------------ #
        oracle_provenance: Optional[FieldProvenance] = None
        expected_patterns: List[str] = []
        expected_exit_codes: List[int] = [1]

        if plan:
            oracle_provenance = plan.provenance.expected_behavior_source
            expected_patterns = plan.expected_output_patterns
            expected_exit_codes = plan.expected_exit_codes or [1]
        elif case:
            oracle_provenance = (
                case.provenance_map.get("expected_behavior")
                or case.provenance_map.get("hypothesis")
                or case.provenance_map.get("finding_id")
            )
            expected_patterns = case.contract.command.expected_output_patterns
            expected_exit_codes = case.contract.command.expected_exit_codes or [1]


        oracle_trusted = self._is_trusted_oracle(oracle_provenance)

        # Check for untrusted oracle
        if not oracle_trusted:
            if attempt.exit_code == 0:
                explanation = (
                    "Execution passed with exit code 0, but no independently sourced oracle "
                    "was provided to confirm the absence of defect."
                )
                return ReproductionClassification(
                    classification_id=generate_classification_id(case_id, attempt_number),
                    outcome=ClassificationOutcome.INCONCLUSIVE,
                    reason_code=ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED,
                    explanation=explanation,
                    classifier_version=self.version,
                    case_id=case_id,
                    plan_id=plan_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    evidence_id=evidence_id,
                    is_reproduced=False,
                    oracle_present=False,
                    oracle_verified=False,
                )
            else:
                explanation = (
                    f"Process exited with non-zero code {attempt.exit_code}, but expected behavior "
                    "lacks trusted provenance. Reproduction cannot be claimed without an independent oracle."
                )
                return ReproductionClassification(
                    classification_id=generate_classification_id(case_id, attempt_number),
                    outcome=ClassificationOutcome.UNRELATED_FAILURE,
                    reason_code=ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED,
                    explanation=explanation,
                    classifier_version=self.version,
                    case_id=case_id,
                    plan_id=plan_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    evidence_id=evidence_id,
                    is_reproduced=False,
                    oracle_present=False,
                    oracle_verified=False,
                )

        # Oracle is present and trusted.
        # Case A: Exit code is 0 -> Test passed cleanly.
        if attempt.exit_code == 0 and not attempt.matched_patterns:
            explanation = (
                "Independent oracle executed and observed no defect violation (exit code 0). "
                "Note: Passing tests do not prove the defect is fixed outside tested conditions."
            )
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.NOT_REPRODUCED,
                reason_code=ClassificationReasonCode.ORACLE_OBSERVED_NO_VIOLATION,
                explanation=explanation,
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=True,
                oracle_verified=True,
            )

        # Check for unrelated Python harness/syntax errors in stderr
        combined_output = f"{attempt.stdout_excerpt}\n{attempt.stderr_excerpt}"
        for pat in _UNRELATED_EXCEPTION_PATTERNS:
            if pat.search(combined_output):
                # An unrelated syntax/import error occurred
                match_str = pat.search(combined_output).group(0) # type: ignore[union-attr]
                explanation = (
                    f"Execution failed with an unrelated exception ({match_str.strip()}) rather than the target defect."
                )
                return ReproductionClassification(
                    classification_id=generate_classification_id(case_id, attempt_number),
                    outcome=ClassificationOutcome.UNRELATED_FAILURE,
                    reason_code=ClassificationReasonCode.UNRELATED_EXCEPTION,
                    explanation=self._sanitize(explanation),
                    classifier_version=self.version,
                    case_id=case_id,
                    plan_id=plan_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    evidence_id=evidence_id,
                    is_reproduced=False,
                    oracle_present=True,
                    oracle_verified=True,
                    details={"unrelated_pattern": match_str},
                )

        # Case B: Exit code matches expected exit codes AND expected patterns matched.
        exit_code_matches = attempt.exit_code in expected_exit_codes
        pattern_matches = len(attempt.matched_patterns) > 0

        # If patterns were defined and matched, or exit code matched when patterns were verified:
        if pattern_matches and exit_code_matches:
            explanation = (
                f"Defect successfully reproduced: Process exited with code {attempt.exit_code} "
                f"and matched expected defect patterns {attempt.matched_patterns} under trusted oracle."
            )
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.REPRODUCED,
                reason_code=ClassificationReasonCode.ORACLE_VIOLATION_CONFIRMED,
                explanation=self._sanitize(explanation),
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=True,
                oracle_present=True,
                oracle_verified=True,
                details={
                    "matched_patterns": attempt.matched_patterns,
                    "exit_code": attempt.exit_code,
                },
            )

        # Case C: Output was truncated such that patterns could not be verified
        if attempt.stdout_truncated or attempt.stderr_truncated:
            explanation = "Execution output was truncated, preventing conclusive verification of defect patterns."
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.INCONCLUSIVE,
                reason_code=ClassificationReasonCode.EVIDENCE_TRUNCATED_OR_MISSING,
                explanation=explanation,
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=True,
                oracle_verified=True,
            )

        # Case D: Process exited non-zero, but NO expected patterns matched.
        # "Never infer reproduction from a nonzero exit code or generic error message alone."
        if not pattern_matches and expected_patterns:
            explanation = (
                f"Process exited with code {attempt.exit_code}, but failed to match any expected "
                f"defect patterns {expected_patterns}. Classified as unrelated failure."
            )
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.UNRELATED_FAILURE,
                reason_code=ClassificationReasonCode.GENERIC_NONZERO_EXIT,
                explanation=self._sanitize(explanation),
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=True,
                oracle_verified=True,
                details={"exit_code": attempt.exit_code},
            )


        # Case E: Generic non-zero exit with no expected patterns defined
        if attempt.exit_code != 0:
            explanation = (
                f"Process exited with code {attempt.exit_code}, but no specific output pattern was defined. "
                "Non-zero exit alone is insufficient to confirm reproduction."
            )
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, attempt_number),
                outcome=ClassificationOutcome.UNRELATED_FAILURE,
                reason_code=ClassificationReasonCode.GENERIC_NONZERO_EXIT,
                explanation=explanation,
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                evidence_id=evidence_id,
                is_reproduced=False,
                oracle_present=True,
                oracle_verified=True,
            )

        # Fallthrough: Ambiguous signals
        return ReproductionClassification(
            classification_id=generate_classification_id(case_id, attempt_number),
            outcome=ClassificationOutcome.INCONCLUSIVE,
            reason_code=ClassificationReasonCode.AMBIGUOUS_OUTPUT_SIGNALS,
            explanation="Signals from stdout, stderr, and exit codes were contradictory or inconclusive.",
            classifier_version=self.version,
            case_id=case_id,
            plan_id=plan_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            evidence_id=evidence_id,
            is_reproduced=False,
            oracle_present=oracle_trusted,
            oracle_verified=oracle_trusted,
        )

    def classify_case_history(
        self,
        case: ReproductionCase,
        attempts: List[ReproductionAttempt],
        plan: Optional[ReproductionPlan] = None,
    ) -> ReproductionClassification:
        """Classify aggregate case outcome across multi-attempt history, detecting FLAKY signals."""
        case_id = case.case_id
        plan_id = plan.plan_id if plan else case.contract.request_id

        if not attempts:
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, None),
                outcome=ClassificationOutcome.INCONCLUSIVE,
                reason_code=ClassificationReasonCode.EVIDENCE_TRUNCATED_OR_MISSING,
                explanation="No reproduction attempts have been executed for this case.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                is_reproduced=False,
            )

        # Classify each attempt individually
        classified_attempts = [
            self.classify_attempt(att, plan=plan, case=case) for att in attempts
        ]

        # Single attempt
        if len(classified_attempts) == 1:
            single = classified_attempts[0]
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, None),
                outcome=single.outcome,
                reason_code=single.reason_code,
                explanation=single.explanation,
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=single.attempt_id,
                attempt_number=single.attempt_number,
                evidence_id=single.evidence_id,
                is_reproduced=single.is_reproduced,
                oracle_present=single.oracle_present,
                oracle_verified=single.oracle_verified,
                details=single.details,
            )

        # Multi-attempt evaluation: detect inconsistency (FLAKY)
        outcomes = [ca.outcome for ca in classified_attempts]

        has_reproduced = ClassificationOutcome.REPRODUCED in outcomes
        has_not_reproduced = ClassificationOutcome.NOT_REPRODUCED in outcomes

        # Explicit conflict: Reproduced in one attempt, but passed (Not Reproduced) in another
        if has_reproduced and has_not_reproduced:
            explanation = (
                f"Inconsistent outcomes across {len(attempts)} attempts: "
                "Defect reproduced in some runs but tests passed in others. "
                "Marked as FLAKY to prevent false bug confirmation."
            )
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, None),
                outcome=ClassificationOutcome.FLAKY,
                reason_code=ClassificationReasonCode.INCONSISTENT_RETRIES,
                explanation=explanation,
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                is_reproduced=False,
                oracle_present=True,
                oracle_verified=True,
                details={
                    "attempt_outcomes": [o.value for o in outcomes],
                    "attempts_count": len(attempts),
                },
            )

        # All reproduced
        if all(o == ClassificationOutcome.REPRODUCED for o in outcomes):
            latest = classified_attempts[-1]
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, None),
                outcome=ClassificationOutcome.REPRODUCED,
                reason_code=ClassificationReasonCode.ORACLE_VIOLATION_CONFIRMED,
                explanation=f"Defect consistently reproduced across all {len(attempts)} attempts.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=latest.attempt_id,
                attempt_number=latest.attempt_number,
                evidence_id=latest.evidence_id,
                is_reproduced=True,
                oracle_present=True,
                oracle_verified=True,
                details={"attempts_count": len(attempts)},
            )

        # All not reproduced
        if all(o == ClassificationOutcome.NOT_REPRODUCED for o in outcomes):
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, None),
                outcome=ClassificationOutcome.NOT_REPRODUCED,
                reason_code=ClassificationReasonCode.ORACLE_OBSERVED_NO_VIOLATION,
                explanation=f"Tests consistently passed across all {len(attempts)} attempts (no defect observed).",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                is_reproduced=False,
                oracle_present=True,
                oracle_verified=True,
                details={"attempts_count": len(attempts)},
            )

        # If latest attempt resolved a transient timeout or environment failure
        # (e.g. attempt 1 timeout, attempt 2 reproduced)
        if outcomes[-1] == ClassificationOutcome.REPRODUCED and all(
            o in {ClassificationOutcome.TIMEOUT, ClassificationOutcome.ENVIRONMENT_BLOCKED}
            for o in outcomes[:-1]
        ):
            latest = classified_attempts[-1]
            return ReproductionClassification(
                classification_id=generate_classification_id(case_id, None),
                outcome=ClassificationOutcome.REPRODUCED,
                reason_code=ClassificationReasonCode.ORACLE_VIOLATION_CONFIRMED,
                explanation=f"Defect reproduced on attempt #{latest.attempt_number} following transient retry.",
                classifier_version=self.version,
                case_id=case_id,
                plan_id=plan_id,
                attempt_id=latest.attempt_id,
                attempt_number=latest.attempt_number,
                evidence_id=latest.evidence_id,
                is_reproduced=True,
                oracle_present=True,
                oracle_verified=True,
            )

        # Otherwise use the latest attempt with multi-attempt details
        latest = classified_attempts[-1]
        return ReproductionClassification(
            classification_id=generate_classification_id(case_id, None),
            outcome=latest.outcome,
            reason_code=latest.reason_code,
            explanation=latest.explanation,
            classifier_version=self.version,
            case_id=case_id,
            plan_id=plan_id,
            attempt_id=latest.attempt_id,
            attempt_number=latest.attempt_number,
            evidence_id=latest.evidence_id,
            is_reproduced=latest.is_reproduced,
            oracle_present=latest.oracle_present,
            oracle_verified=latest.oracle_verified,
            details={
                "attempt_outcomes": [o.value for o in outcomes],
                "attempts_count": len(attempts),
            },
        )
