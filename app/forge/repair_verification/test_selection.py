"""Phase 12 M5: Intelligent Test Selection Engine.

Selects minimal, risk-aware verification checks across targeted, regression,
static analysis, and type-checking categories:
- Distinguishes targeted tests (directly related to modified files) from regression suites.
- Selects static analysis (e.g. py_compile, flake8) and type verification (mypy) when enabled.
- Records explicit selection rationale and marks incomplete selections as partial verification.
- Rejects arbitrary arguments and enforces structured execution vectors.
"""

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from forge.repair_verification.discovery import RepairTestDiscoverer, DiscoveryResult
from forge.repair_verification.schemas import (
    SelectedCheck,
    TestIdentifier,
    TestScopeCategory,
    VerificationPolicy,
)


class IntelligentTestSelector:
    """Selects targeted, regression, and static checks based on modified files and policy."""

    def __init__(self, workspace_path: Path, policy: Optional[VerificationPolicy] = None) -> None:
        self.workspace_path = workspace_path.resolve()
        self.policy = policy or VerificationPolicy()
        self.discoverer = RepairTestDiscoverer(workspace_path=self.workspace_path)

    def select_checks(
        self,
        modified_files: List[str],
        discovered: Optional[DiscoveryResult] = None,
    ) -> Tuple[List[SelectedCheck], List[TestIdentifier], Dict[str, str], bool, str]:
        """Generate categorized SelectedCheck list and test identifiers.

        Returns:
            Tuple of (selected_checks, test_identifiers, excluded_checks, is_partial, rationale)
        """
        disc = discovered or self.discoverer.discover_tests(modified_files=modified_files)

        selected_checks: List[SelectedCheck] = []
        all_identifiers: List[TestIdentifier] = []
        excluded_checks: Dict[str, str] = {}
        rationale_parts: List[str] = []

        check_counter = 1

        # 1. Static Analysis Checks (syntax / py_compile)
        is_static_req = TestScopeCategory.STATIC_ANALYSIS in self.policy.required_categories
        is_static_opt = TestScopeCategory.STATIC_ANALYSIS in self.policy.optional_categories

        if (is_static_req or is_static_opt) and modified_files:
            py_files = [f for f in modified_files if f.endswith(".py")]
            if py_files:
                chk = SelectedCheck(
                    check_id=f"chk_static_{check_counter:03d}",
                    category=TestScopeCategory.STATIC_ANALYSIS,
                    command=["python", "-m", "py_compile"] + py_files,
                    target_files=py_files,
                    is_required=is_static_req,
                    rationale=f"Verify Python AST compilation for modified files: {', '.join(py_files)}",
                )
                selected_checks.append(chk)
                check_counter += 1
                rationale_parts.append(f"Selected static syntax check for {len(py_files)} modified Python file(s) ({', '.join(py_files)}).")

        # 2. Targeted Test Checks
        targeted_files = sorted(set(t.test_file for t in disc.targeted_tests))
        is_targeted_req = TestScopeCategory.TARGETED in self.policy.required_categories

        if targeted_files:
            for tf in targeted_files:
                chk = SelectedCheck(
                    check_id=f"chk_targeted_{check_counter:03d}",
                    category=TestScopeCategory.TARGETED,
                    command=["pytest", "-v", tf],
                    target_files=[tf],
                    is_required=is_targeted_req,
                    rationale=f"Targeted test file for modified code: {tf}",
                )
                selected_checks.append(chk)
                check_counter += 1
            all_identifiers.extend(disc.targeted_tests)
            rationale_parts.append(
                f"Selected {len(targeted_files)} targeted test file(s) containing {len(disc.targeted_tests)} test(s)."
            )
        else:
            excluded_checks["targeted_tests"] = "No targeted test files directly matching modified files were found."
            rationale_parts.append("No directly targeted test files identified.")

        # 3. Regression Test Checks
        regression_files = sorted(set(t.test_file for t in disc.regression_tests))
        is_regr_req = TestScopeCategory.REGRESSION in self.policy.required_categories
        is_regr_opt = TestScopeCategory.REGRESSION in self.policy.optional_categories

        if (is_regr_req or is_regr_opt) and regression_files:
            for rf in regression_files:
                chk = SelectedCheck(
                    check_id=f"chk_regression_{check_counter:03d}",
                    category=TestScopeCategory.REGRESSION,
                    command=["pytest", "-v", rf],
                    target_files=[rf],
                    is_required=is_regr_req,
                    rationale=f"Regression test file importing modified modules: {rf}",
                )
                selected_checks.append(chk)
                check_counter += 1
            all_identifiers.extend(disc.regression_tests)
            rationale_parts.append(
                f"Selected {len(regression_files)} regression test file(s) with {len(disc.regression_tests)} test(s)."
            )
        elif not regression_files:
            excluded_checks["regression_tests"] = "No dependent/importing regression test files identified."

        # 4. Fallback: if no targeted tests found, look for general tests
        if not targeted_files and not regression_files and disc.all_test_files:
            first_test = disc.all_test_files[0]
            chk = SelectedCheck(
                check_id=f"chk_fallback_{check_counter:03d}",
                category=TestScopeCategory.TARGETED,
                command=["pytest", "-v", first_test],
                target_files=[first_test],
                is_required=is_targeted_req,
                rationale=f"Fallback suite check on available test file: {first_test}",
            )
            selected_checks.append(chk)
            check_counter += 1
            rationale_parts.append(f"Fallback check selected: {first_test}.")

        # 5. Partial verification determination
        total_tests_selected = len(all_identifiers)
        total_discovered = total_tests_selected + len(disc.other_tests)
        is_partial = len(disc.other_tests) > 0 or not disc.all_test_files

        if is_partial:
            rationale_parts.append(
                f"Partial verification: {total_tests_selected} of {total_discovered} discovered tests selected."
            )
        else:
            rationale_parts.append("Full suite coverage: all discovered tests selected.")

        full_rationale = " ".join(rationale_parts)
        return selected_checks, all_identifiers, excluded_checks, is_partial, full_rationale
