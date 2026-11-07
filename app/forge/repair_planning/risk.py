"""Phase 12 M3: Risk Assessment and Side-Effect Analysis.

Analyzes potential risks, dependency reach, regression likelihood, public API
compatibility, and security implications of proposed repair strategies using
deterministic, explainable graph and syntactic metrics.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from forge.causality.schemas import CausalStatus, RootCauseReport
from forge.repair_planning.schemas import (
    RepairApproach,
    RepairRiskAssessment,
    RiskLikelihood,
    RiskSeverity,
)
from forge.repository.incremental import IncrementalRepositoryIndex


class RepairRiskAnalyzer:
    """Evaluates the risk profile and potential side effects of a proposed repair."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.incremental_index = incremental_index

    def assess_risk(
        self,
        affected_files: List[str],
        affected_symbols: Optional[List[str]] = None,
        proposed_approach: Optional[RepairApproach] = None,
        root_cause_report: Optional[RootCauseReport] = None,
    ) -> RepairRiskAssessment:
        """Perform comprehensive deterministic risk assessment."""
        symbols = affected_symbols or []
        approach = proposed_approach or RepairApproach.LOCAL_LOGIC_CORRECTION

        risk_score = 0.20  # Baseline low risk
        severity = RiskSeverity.LOW
        likelihood = RiskLikelihood.LOW

        potential_regressions: List[str] = []
        security_implications: List[str] = []
        data_integrity_implications: List[str] = []
        dependency_implications: List[str] = []
        affected_components: List[str] = []
        evidence_refs: List[str] = []

        # -------------------------------------------------------------------
        # 1. Scope Evaluation (File & Symbol Counts)
        # -------------------------------------------------------------------
        file_count = len(affected_files)
        symbol_count = len(symbols)

        if file_count > 3:
            risk_score += 0.25
            potential_regressions.append(
                f"Multi-file modification ({file_count} files): changes span across multiple modules."
            )
        elif file_count > 1:
            risk_score += 0.10
            potential_regressions.append(
                f"Cross-module modification ({file_count} files affected)."
            )

        if symbol_count > 3:
            risk_score += 0.15
            potential_regressions.append(
                f"Multiple symbols affected ({symbol_count} symbols): increases interface synchronization risk."
            )

        for f in affected_files:
            mod_part = f.replace("/", ".").rstrip(".py")
            if mod_part not in affected_components:
                affected_components.append(mod_part)

        # -------------------------------------------------------------------
        # 2. Dependency Reach (Call Graph Inspection)
        # -------------------------------------------------------------------
        incoming_caller_count = 0
        callers: Set[str] = set()

        if self.incremental_index is not None and symbols:
            try:
                cg = self.incremental_index.get_call_graph()
                for sym in symbols:
                    for caller_id in cg.get_direct_callers(sym):
                        callers.add(caller_id)
                incoming_caller_count = len(callers)
            except Exception:
                pass

        if incoming_caller_count >= 5:
            risk_score += 0.25
            dependency_implications.append(
                f"High dependency reach: {incoming_caller_count} callers depend on modified symbol(s) ({', '.join(list(callers)[:3])}...)."
            )
        elif incoming_caller_count > 0:
            risk_score += 0.10
            dependency_implications.append(
                f"Moderate dependency reach: {incoming_caller_count} direct caller(s) identified in call graph."
            )
        else:
            dependency_implications.append(
                "Localized dependency reach: symbol is isolated or internal to module."
            )

        # -------------------------------------------------------------------
        # 3. Public API Compatibility
        # -------------------------------------------------------------------
        is_public_api = any(
            not s.split(".")[-1].startswith("_") and not s.split(":")[-1].startswith("_")
            for s in symbols
        )
        if is_public_api:
            risk_score += 0.10
            potential_regressions.append(
                "Public API modification: altering non-underscored symbol may impact external callers."
            )

        # -------------------------------------------------------------------
        # 4. Security & Sanitization Boundaries
        # -------------------------------------------------------------------
        sec_keywords = {"auth", "token", "secret", "cert", "crypt", "validat", "sanitiz", "perm", "sandbox"}
        is_security_sensitive = False

        for f in affected_files:
            f_lower = f.lower()
            if any(k in f_lower for k in sec_keywords):
                is_security_sensitive = True
                break

        for s in symbols:
            s_lower = s.lower()
            if any(k in s_lower for k in sec_keywords):
                is_security_sensitive = True
                break

        if is_security_sensitive or approach == RepairApproach.SECURITY_HARDENING:
            risk_score += 0.15
            security_implications.append(
                "Security boundary impact: proposed changes involve authentication, authorization, or input sanitization."
            )
        else:
            security_implications.append(
                "No direct security-sensitive boundary or credential handling detected."
            )

        # -------------------------------------------------------------------
        # 5. Data Integrity & Persistence
        # -------------------------------------------------------------------
        data_keywords = {"db", "store", "database", "persist", "write", "delete", "schema", "sqlite"}
        is_data_sensitive = False

        for f in affected_files:
            f_lower = f.lower()
            if any(k in f_lower for k in data_keywords):
                is_data_sensitive = True
                break

        if is_data_sensitive:
            risk_score += 0.15
            data_integrity_implications.append(
                "State/persistence boundary impact: changes touch storage or database entities."
            )
        else:
            data_integrity_implications.append(
                "Zero persistent storage or schema modifications required."
            )

        # -------------------------------------------------------------------
        # 6. Test Coverage in Workspace
        # -------------------------------------------------------------------
        has_tests = self._check_existing_test_coverage(affected_files)
        if not has_tests:
            risk_score += 0.15
            likelihood = RiskLikelihood.HIGH
            potential_regressions.append(
                "Zero automated test files detected covering the affected module(s)."
            )
        else:
            potential_regressions.append(
                "Existing test suite detected covering target module(s)."
            )

        # -------------------------------------------------------------------
        # 7. Causal Uncertainty
        # -------------------------------------------------------------------
        if root_cause_report:
            if root_cause_report.status == CausalStatus.PLAUSIBLE:
                risk_score += 0.10
                likelihood = RiskLikelihood.MEDIUM
                potential_regressions.append(
                    "Causal explanation is PLAUSIBLE (contains inferred links): potential for misdiagnosed edge case."
                )
            elif root_cause_report.status == CausalStatus.MULTIPLE_PLAUSIBLE_ROOT_CAUSES:
                risk_score += 0.20
                likelihood = RiskLikelihood.HIGH
                potential_regressions.append(
                    "Multiple competing plausible root causes exist: repairing one may leave other defects unaddressed."
                )

        # -------------------------------------------------------------------
        # 8. Aggregate Score & Severity
        # -------------------------------------------------------------------
        risk_score = min(max(round(risk_score, 2), 0.05), 0.95)

        if risk_score >= 0.70:
            severity = RiskSeverity.HIGH
            likelihood = RiskLikelihood.HIGH
        elif risk_score >= 0.45:
            severity = RiskSeverity.MEDIUM
            likelihood = RiskLikelihood.MEDIUM
        else:
            severity = RiskSeverity.LOW
            likelihood = RiskLikelihood.LOW

        explanation = (
            f"Risk score {risk_score:.2f} ({severity.value.upper()} severity, {likelihood.value} likelihood) "
            f"based on {file_count} affected file(s), {symbol_count} affected symbol(s), "
            f"{incoming_caller_count} incoming caller(s), and "
            f"{'security-sensitive' if is_security_sensitive else 'standard'} domain scope."
        )

        return RepairRiskAssessment(
            overall_risk=severity,
            likelihood=likelihood,
            risk_score=risk_score,
            affected_components=affected_components,
            potential_regressions=potential_regressions,
            security_implications=security_implications,
            data_integrity_implications=data_integrity_implications,
            dependency_implications=dependency_implications,
            explanation=explanation,
            supporting_evidence=evidence_refs,
        )

    def _check_existing_test_coverage(self, affected_files: List[str]) -> bool:
        """Check if any test files exist on disk for the affected files."""
        for f in affected_files:
            stem = Path(f).stem
            test_patterns = [
                f"test_{stem}.py",
                f"{stem}_test.py",
                f"tests/unit/test_{stem}.py",
                f"tests/test_{stem}.py",
            ]
            for pattern in test_patterns:
                cand = self.workspace_path / pattern
                if cand.exists() and cand.is_file():
                    return True

        # Check if any file in tests/ matches
        tests_dir = self.workspace_path / "tests"
        if tests_dir.exists() and tests_dir.is_dir():
            for f in affected_files:
                stem = Path(f).stem
                for p in tests_dir.rglob(f"*{stem}*.py"):
                    if p.is_file():
                        return True
        return False
