"""Execution harness for running static and LLM-assisted benchmark evaluations.

Static mode applies a set of category-independent heuristic text-pattern rules to
every Python source file in the fixture directory.  Fixture metadata
(category, expected_defect, target_symbol) is intentionally not consulted during
detection — it is only used *after* the detector finishes, in the evaluation step
that compares detector output against fixture labels.

This separation prevents circular evaluation: expected fixture labels cannot
manufacture detections.

Limitations of static mode:
- Text-pattern heuristics may produce false positives on correct code.
- They may miss defects expressed differently from the calibrated patterns.
- Results should be read as "patterns observed by heuristic scan", not as
  "bugs confirmed by sound static analysis".
"""

import re
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from forge.benchmarks.corpus import CorpusLoader
from forge.benchmarks.metrics import calculate_metrics
from forge.benchmarks.models import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkCase,
    BenchmarkCategory,
    BenchmarkRunResult,
    CaseExecutionResult,
    CaseOutcome,
)
from forge.config import get_settings
from forge.investigation.loop import InvestigationLoop
from forge.investigation.tools import ToolRegistry
from forge.llm.fake import FakeLLMProvider
from forge.llm.provider import LLMProvider
from forge.repository import (
    detect_repository,
    build_symbol_index,
    load_instruction_files,
    RepositoryMap,
)
from forge.schemas import Finding, FindingSeverity
from forge.storage.store import ForgeStore


# ---------------------------------------------------------------------------
# Category-independent static detection rules.
# Each entry: (rule_id, check_fn, title, description, severity)
#   check_fn(content: str) -> bool  — returns True when the pattern is observed.
#
# Rules are applied uniformly to every .py file in every fixture regardless of
# the fixture's expected category or defect label.  case.category is never
# consulted here.
# ---------------------------------------------------------------------------
_STATIC_RULES: List[Tuple[str, Callable[[str], bool], str, str, FindingSeverity]] = [
    (
        "off_by_one_le_boundary",
        lambda c: bool(re.search(r"while\s+\w+\s*<=\s*\w+\s*:", c)),
        "Off-by-one: while loop uses '<=' upper-bound operator",
        (
            "Loop condition uses '<=' which may allow off-by-one access when the "
            "upper bound equals collection length (e.g. right = len(arr))."
        ),
        FindingSeverity.HIGH,
    ),
    (
        "unguarded_numeric_cast",
        lambda c: bool(re.search(r"\bint\s*\(\s*\w+\s*\[", c)),
        "Unguarded integer cast on subscript expression",
        (
            "int() applied directly to a subscript expression without surrounding "
            "try/except or type guard."
        ),
        FindingSeverity.MEDIUM,
    ),
    (
        "os_path_join_no_containment",
        lambda c: (
            bool(re.search(r"\bos\.path\.join\s*\(", c))
            and "startswith(" not in c
        ),
        "os.path.join without path containment verification",
        (
            "Path assembled via os.path.join with no startswith() containment "
            "check found in the same file."
        ),
        FindingSeverity.HIGH,
    ),
    (
        "chained_dict_subscript",
        lambda c: bool(re.search(r'\w+\[["\'][^\]]*["\']\]\s*\[["\']', c)),
        "Chained dict subscript access without error handling",
        (
            "Nested dict/list access via chained bracket notation with no .get() "
            "or try/except guard detected."
        ),
        FindingSeverity.MEDIUM,
    ),
    (
        "percent_direct_subtraction",
        lambda c: bool(
            re.search(
                r"=\s*\w+\s*-\s*\w*(?:percent|discount|rate)\w*",
                c,
                re.IGNORECASE,
            )
        ),
        "Direct subtraction of a percentage or rate variable",
        (
            "A percentage, discount, or rate variable is subtracted directly "
            "instead of computing a fractional amount (e.g. base * rate / 100)."
        ),
        FindingSeverity.HIGH,
    ),
]


class BenchmarkRunner:
    """Orchestrates benchmark suite executions across test fixtures.

    Static mode applies the module-level ``_STATIC_RULES`` to Python source
    files without consulting fixture metadata during detection.  LLM mode runs
    the full ``InvestigationLoop`` and reports findings the investigation system
    produced independently of the fixture labels.
    """

    def __init__(
        self,
        loader: Optional[CorpusLoader] = None,
        store: Optional[ForgeStore] = None,
        provider: Optional[LLMProvider] = None,
    ) -> None:
        self.loader = loader or CorpusLoader()
        self.store = store
        self.provider = provider or FakeLLMProvider()

    def run_suite(
        self,
        mode: str = "static",
        case_filter: Optional[str] = None,
    ) -> BenchmarkRunResult:
        """Run all discovered benchmark fixtures in the requested mode."""
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        benchmark_run_id = f"bm_{uuid.uuid4().hex[:12]}"

        cases = self.loader.discover_cases()
        if case_filter:
            cases = [
                c
                for c in cases
                if c.case_id == case_filter or case_filter.lower() in c.case_id.lower()
            ]

        case_results: List[CaseExecutionResult] = []
        for case in cases:
            res = self.evaluate_case(case, mode=mode, benchmark_run_id=benchmark_run_id)
            case_results.append(res)

        completed_at = datetime.now(timezone.utc).isoformat()
        duration = round(time.monotonic() - start_time, 2)
        metrics = calculate_metrics(case_results)

        settings = get_settings()
        env_info: Dict[str, Any] = {
            "python_version": sys.version.split()[0],
            "mode": mode,
            "docker_available": bool(shutil.which("docker")),
            "provider": settings.llm_provider if mode == "llm" else "offline_static",
        }

        return BenchmarkRunResult(
            schema_version=BENCHMARK_SCHEMA_VERSION,
            benchmark_run_id=benchmark_run_id,
            mode=mode,
            corpus_version="1.0.0",
            started_at=started_at,
            completed_at=completed_at,
            total_duration_seconds=duration,
            system_environment=env_info,
            metrics=metrics,
            case_results=case_results,
        )

    def evaluate_case(
        self,
        case: BenchmarkCase,
        mode: str = "static",
        benchmark_run_id: str = "bm_run",
    ) -> CaseExecutionResult:
        """Evaluate a single benchmark case.

        Detection (calling _run_static_analysis or _run_llm_investigation) is
        completely separated from evaluation (comparing detector output to fixture
        labels).  Fixture metadata is only consulted in the evaluation section,
        never inside the detector.
        """
        start_time = time.monotonic()

        # Fail-closed: if isolation is required but Docker is unavailable, skip
        # rather than fall back to host execution.
        if case.execution_requirements.requires_sandbox:
            if not shutil.which("docker"):
                return CaseExecutionResult(
                    case_id=case.case_id,
                    category=case.category,
                    outcome=CaseOutcome.SKIPPED_UNAVAILABLE,
                    has_defect_expected=case.expected_defect.has_defect,
                    defect_detected=False,
                    duration_seconds=round(time.monotonic() - start_time, 3),
                    error_message="Docker unavailable; sandbox execution refused on host.",
                )

        if not case.fixture_dir or not case.fixture_dir.exists():
            return CaseExecutionResult(
                case_id=case.case_id,
                category=case.category,
                outcome=CaseOutcome.ERROR,
                has_defect_expected=case.expected_defect.has_defect,
                defect_detected=False,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error_message=f"Fixture directory '{case.fixture_dir}' does not exist.",
            )

        try:
            intel = detect_repository(case.fixture_dir)
            symbols = build_symbol_index(case.fixture_dir)
            instructions = load_instruction_files(case.fixture_dir)

            repo_map = RepositoryMap(
                target=str(case.fixture_dir),
                intelligence=intel,
                symbol_index=symbols,
                instructions=instructions,
            )

            # ---------------------------------------------------------------
            # DETECTION PHASE — no fixture metadata consulted here
            # ---------------------------------------------------------------
            findings: List[Finding] = []
            if mode == "static":
                findings = self._run_static_analysis(case, repo_map)
            else:
                findings = self._run_llm_investigation(case, repo_map, benchmark_run_id)

            duration = round(time.monotonic() - start_time, 3)

            # ---------------------------------------------------------------
            # EVALUATION PHASE — fixture labels consulted only here
            # ---------------------------------------------------------------
            defect_detected = len(findings) > 0

            # matched_target_file: at least one finding explicitly names the
            # expected target file in its affected_files list.
            matched_file = any(
                any(case.target_file in aff for aff in f.affected_files)
                for f in findings
            )

            matched_symbol = False
            if case.target_symbol:
                matched_symbol = any(
                    case.target_symbol.lower() in f.title.lower()
                    or case.target_symbol.lower() in f.description.lower()
                    for f in findings
                )

            # Pass/fail is determined solely by expected-defect presence vs
            # detector output.  case.category is NOT used in the outcome decision
            # so that all categories (including clean_control and prompt_injection)
            # are evaluated through the same pipeline.
            if case.expected_defect.has_defect:
                # Defect expected: PASS only when a finding was produced that
                # names the expected target file.
                outcome = (
                    CaseOutcome.PASS
                    if (defect_detected and matched_file)
                    else CaseOutcome.FAIL
                )
            else:
                # No defect expected (clean control, prompt injection, …):
                # PASS only when the detector produced no findings.
                outcome = CaseOutcome.PASS if not defect_detected else CaseOutcome.FAIL

            return CaseExecutionResult(
                case_id=case.case_id,
                category=case.category,
                outcome=outcome,
                has_defect_expected=case.expected_defect.has_defect,
                defect_detected=defect_detected,
                matched_target_file=matched_file,
                matched_target_symbol=matched_symbol,
                findings_count=len(findings),
                duration_seconds=duration,
                details={
                    "findings": [f.title for f in findings],
                    "mode": mode,
                },
            )

        except Exception as exc:
            return CaseExecutionResult(
                case_id=case.case_id,
                category=case.category,
                outcome=CaseOutcome.ERROR,
                has_defect_expected=case.expected_defect.has_defect,
                defect_detected=False,
                duration_seconds=round(time.monotonic() - start_time, 3),
                error_message=str(exc),
            )

    def _run_static_analysis(
        self, case: BenchmarkCase, repo_map: RepositoryMap
    ) -> List[Finding]:
        """Apply ``_STATIC_RULES`` uniformly to all .py files in the fixture directory.

        Detection decisions are made solely from file content.  No fixture
        metadata (category, expected_defect, target_symbol, expected_outcome) is
        consulted during this step, preventing circular evaluation.

        Each (rule, file) pair produces at most one finding (deduplication by
        ``finding_id``).  Findings record the actual file name in
        ``affected_files``; the caller's evaluation step then decides whether
        that file matches the expected target.

        Limitations: text-pattern heuristics may produce false positives on
        correct code and miss real defects expressed differently.
        """
        findings: List[Finding] = []
        seen_finding_ids: set = set()

        fixture_dir = case.fixture_dir
        if not fixture_dir or not fixture_dir.exists():
            return findings

        # Scan every .py file in the fixture directory (non-recursive flat layout).
        py_files = sorted(
            f for f in fixture_dir.iterdir() if f.is_file() and f.suffix == ".py"
        )

        for src_file in py_files:
            try:
                content = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel_path = src_file.name

            for rule_id, check_fn, title, description, severity in _STATIC_RULES:
                try:
                    triggered = check_fn(content)
                except Exception:
                    triggered = False

                if not triggered:
                    continue

                # Deduplicate: same rule on same file → exactly one finding.
                finding_id = f"static_{rule_id}__{src_file.stem}"
                if finding_id in seen_finding_ids:
                    continue
                seen_finding_ids.add(finding_id)

                findings.append(
                    Finding(
                        finding_id=finding_id,
                        title=f"{title}: {rel_path}",
                        description=f"{description} (detected in: {rel_path})",
                        severity=severity,
                        # affected_files is set from the scanned file, NOT from
                        # case.target_file, so matched_file check is meaningful.
                        affected_files=[rel_path],
                    )
                )

        return findings

    def _run_llm_investigation(
        self,
        case: BenchmarkCase,
        repo_map: RepositoryMap,
        benchmark_run_id: str,
    ) -> List[Finding]:
        """Run the full InvestigationLoop against the fixture workspace.

        Findings are produced by the loop from its tool call observations.
        The fixture's expected_defect and category are never passed to the loop.
        """
        settings = get_settings()
        store = self.store or ForgeStore(db_path=settings.db_path)
        run_id = store.create_run(
            target=str(case.fixture_dir),
            run_id=f"{benchmark_run_id}_{case.case_id}",
        )

        tool_reg = ToolRegistry(
            workspace_path=case.fixture_dir,
            repo_map=repo_map,
            store=store,
            run_id=run_id,
        )

        loop = InvestigationLoop(
            provider=self.provider,
            tool_registry=tool_reg,
            store=store,
            run_id=run_id,
            target=str(case.fixture_dir),
            repo_map=repo_map,
            max_steps=5,
        )

        result = loop.run(
            user_instruction="Audit this codebase for potential security or functional defects."
        )
        return result.findings
