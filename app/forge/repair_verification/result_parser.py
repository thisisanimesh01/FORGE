"""Phase 12 M5: Test Result Parser.

Parses test execution output from pytest, py_compile, and static tools:
- Extracts passed, failed, errored, skipped, xfailed, and xpassed counts.
- Detects collection errors and abnormal aborts.
- Validates consistency between process exit codes and parsed reports.
- Flags malformed or truncated reports to prevent false-positive verification.
- Calculates cryptographic digests of captured outputs.
"""

import hashlib
import re
from typing import List, Optional, Tuple

from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    SelectedCheck,
    TestExecutionResult,
)
from forge.sandbox.models import SandboxResult


PYTEST_SUMMARY_PATTERN = re.compile(
    r"=+\s*(?:(?P<passed>\d+)\s+passed)?(?:,\s*)?"
    r"(?:(?P<failed>\d+)\s+failed)?(?:,\s*)?"
    r"(?:(?P<errors>\d+)\s+errors?)?(?:,\s*)?"
    r"(?:(?P<skipped>\d+)\s+skipped)?(?:,\s*)?"
    r"(?:(?P<xfailed>\d+)\s+xfailed)?(?:,\s*)?"
    r"(?:(?P<xpassed>\d+)\s+xpassed)?.*=+",
    re.IGNORECASE,
)

PYTEST_TEST_LINE_PATTERN = re.compile(
    r"^([a-zA-Z0-9_\-./]+::[a-zA-Z0-9_\-]+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)",
    re.MULTILINE,
)

COLLECTION_ERROR_PATTERN = re.compile(
    r"(ERROR collecting|ImportError while importing|ModuleNotFoundError|SyntaxError)",
    re.IGNORECASE,
)


class PytestResultParser:
    """Parses and audits raw test output from sandbox execution."""

    @staticmethod
    def parse_result(
        check: SelectedCheck,
        sandbox_res: SandboxResult,
    ) -> CheckExecutionResult:
        """Parse SandboxResult into a structured CheckExecutionResult with consistency checks."""
        stdout = sandbox_res.stdout or ""
        stderr = sandbox_res.stderr or ""
        combined = f"{stdout}\n{stderr}"

        stdout_digest = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        stderr_digest = hashlib.sha256(stderr.encode("utf-8")).hexdigest()

        # Handle timeout
        if sandbox_res.timed_out:
            return CheckExecutionResult(
                check_id=check.check_id,
                category=check.category,
                command=check.command,
                exit_code=sandbox_res.exit_code,
                outcome=CheckOutcome.TIMEOUT,
                duration_ms=sandbox_res.duration_ms,
                timeout=True,
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                raw_output_snippet=combined[:1000],
            )

        # Handle static checks (py_compile)
        if "py_compile" in " ".join(check.command):
            if sandbox_res.exit_code == 0:
                return CheckExecutionResult(
                    check_id=check.check_id,
                    category=check.category,
                    command=check.command,
                    exit_code=0,
                    outcome=CheckOutcome.PASSED,
                    tests_passed=len(check.target_files) or 1,
                    duration_ms=sandbox_res.duration_ms,
                    stdout_digest=stdout_digest,
                    stderr_digest=stderr_digest,
                )
            else:
                return CheckExecutionResult(
                    check_id=check.check_id,
                    category=check.category,
                    command=check.command,
                    exit_code=sandbox_res.exit_code or 1,
                    outcome=CheckOutcome.FAILED,
                    tests_failed=len(check.target_files) or 1,
                    duration_ms=sandbox_res.duration_ms,
                    stdout_digest=stdout_digest,
                    stderr_digest=stderr_digest,
                    raw_output_snippet=combined[:1000],
                )

        # Handle pytest output
        passed_cnt = 0
        failed_cnt = 0
        errors_cnt = 0
        skipped_cnt = 0
        xfailed_cnt = 0
        xpassed_cnt = 0
        collection_errs = 0
        malformed = False

        # Check for collection errors
        if COLLECTION_ERROR_PATTERN.search(combined):
            collection_errs = 1

        # Match pytest summary line
        summary_match = PYTEST_SUMMARY_PATTERN.search(stdout)
        if summary_match:
            passed_cnt = int(summary_match.group("passed") or 0)
            failed_cnt = int(summary_match.group("failed") or 0)
            errors_cnt = int(summary_match.group("errors") or 0)
            skipped_cnt = int(summary_match.group("skipped") or 0)
            xfailed_cnt = int(summary_match.group("xfailed") or 0)
            xpassed_cnt = int(summary_match.group("xpassed") or 0)

        # Parse individual test items
        test_results: List[TestExecutionResult] = []
        for match in PYTEST_TEST_LINE_PATTERN.finditer(stdout):
            tid, status_str = match.groups()
            status_map = {
                "PASSED": CheckOutcome.PASSED,
                "FAILED": CheckOutcome.FAILED,
                "ERROR": CheckOutcome.ERROR,
                "SKIPPED": CheckOutcome.SKIPPED,
                "XFAIL": CheckOutcome.XFAILED,
                "XPASS": CheckOutcome.XPASSED,
            }
            test_results.append(
                TestExecutionResult(
                    test_id=tid,
                    outcome=status_map.get(status_str.upper(), CheckOutcome.ERROR),
                )
            )

        # Consistency checks
        exit_code = sandbox_res.exit_code

        # Inconsistency: exit code 0 but failures detected
        if exit_code == 0 and (failed_cnt > 0 or errors_cnt > 0 or collection_errs > 0):
            malformed = True
            outcome = CheckOutcome.ERROR
        # Exit code 0 and all good
        elif exit_code == 0 and (passed_cnt > 0 or xpassed_cnt > 0 or "passed" in stdout.lower()):
            outcome = CheckOutcome.PASSED
        # Collection error detected
        elif collection_errs > 0:
            outcome = CheckOutcome.COLLECTION_ERROR
            if exit_code == 0:
                malformed = True
        # Explicit failures reported
        elif failed_cnt > 0 or errors_cnt > 0:
            outcome = CheckOutcome.FAILED
        # Exit code non-zero with no summary or contradictory report
        elif exit_code != 0:
            if not summary_match and not test_results:
                malformed = True
            outcome = CheckOutcome.FAILED
        else:
            outcome = CheckOutcome.PASSED

        return CheckExecutionResult(
            check_id=check.check_id,
            category=check.category,
            command=check.command,
            exit_code=exit_code,
            outcome=outcome,
            tests_passed=passed_cnt,
            tests_failed=failed_cnt,
            tests_errored=errors_cnt,
            tests_skipped=skipped_cnt,
            tests_xfailed=xfailed_cnt,
            tests_xpassed=xpassed_cnt,
            collection_errors=collection_errs,
            test_results=test_results,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            duration_ms=sandbox_res.duration_ms,
            timeout=False,
            malformed_report=malformed,
            raw_output_snippet=combined[:1000],
        )
