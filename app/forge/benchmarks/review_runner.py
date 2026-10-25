"""Execution and scoring harness for review quality benchmarking.

Evaluates FORGE ReviewEngine against controlled, versioned git diff fixtures.
Separates detector execution from evaluation labels to prevent circular evaluation.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from forge.benchmarks.review_corpus import ReviewCorpusLoader
from forge.benchmarks.review_metrics import calculate_review_metrics
from forge.benchmarks.review_models import (
    REVIEW_BENCHMARK_SCHEMA_VERSION,
    ReviewBenchmarkFixtureMeta,
    ReviewBenchmarkRunResult,
    ReviewCaseExecutionResult,
    ReviewCaseOutcome,
)
from forge.config import redact_secrets
from forge.review.dedup import deduplicate_findings
from forge.review.engine import ReviewEngine
from forge.review.git import read_git_diff
from forge.review.models import ReviewFinding, ReviewReport
from forge.review.parser import parse_unified_diff
from forge.review.evidence import EvidenceCalibrationStatus


class ReviewBenchmarkRunner:
    """Orchestrates running review quality benchmarks and computing evaluations."""

    def __init__(self, loader: Optional[ReviewCorpusLoader] = None):
        self.loader = loader or ReviewCorpusLoader()

    def run_suite(
        self,
        mode: str = "static",
        fixture_filter: Optional[str] = None,
        provider: Optional[Any] = None,
    ) -> ReviewBenchmarkRunResult:
        """Run the full benchmark suite or filtered subset."""
        start_time = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        fixtures = self.loader.discover_fixtures()

        if fixture_filter:
            fixtures = [f for f in fixtures if f.fixture_id == fixture_filter]

        case_results: List[ReviewCaseExecutionResult] = []

        # System environment inspection
        docker_available = shutil.which("docker") is not None
        sys_env = {
            "docker_available": docker_available,
            "mode": mode,
            "fixtures_count": len(fixtures),
        }

        # Deterministic benchmark run ID
        run_hasher = hashlib.sha256()
        for f in fixtures:
            run_hasher.update(f.fixture_id.encode("utf-8"))
        run_hasher.update(mode.encode("utf-8"))
        benchmark_run_id = f"rev_bm_{run_hasher.hexdigest()[:12]}"

        for fixture in fixtures:
            case_res = self.evaluate_fixture(
                fixture=fixture,
                mode=mode,
                docker_available=docker_available,
                provider=provider,
            )
            case_results.append(case_res)

        metrics = calculate_review_metrics(case_results)
        completed_at = datetime.now(timezone.utc).isoformat()
        total_duration = round(time.monotonic() - start_time, 4)

        return ReviewBenchmarkRunResult(
            schema_version=REVIEW_BENCHMARK_SCHEMA_VERSION,
            benchmark_run_id=benchmark_run_id,
            mode=mode,
            corpus_version="1.0.0",
            started_at=started_at,
            completed_at=completed_at,
            total_duration_seconds=total_duration,
            system_environment=sys_env,
            metrics=metrics,
            case_results=case_results,
        )

    def evaluate_fixture(
        self,
        fixture: ReviewBenchmarkFixtureMeta,
        mode: str = "static",
        docker_available: bool = False,
        provider: Optional[Any] = None,
    ) -> ReviewCaseExecutionResult:
        """Execute review against fixture and evaluate predictions against ground truth."""
        case_start = time.monotonic()

        # Handle required sandbox when Docker is unavailable
        if fixture.expected.requires_docker and not docker_available:
            return ReviewCaseExecutionResult(
                fixture_id=fixture.fixture_id,
                category=fixture.category,
                outcome=ReviewCaseOutcome.SKIPPED_UNAVAILABLE,
                has_defects_expected=fixture.expected.has_defects,
                duration_seconds=round(time.monotonic() - case_start, 4),
                error_message="Skipped: Docker unavailable for sandbox runtime checks.",
            )

        fixture_dir = fixture.fixture_dir
        if not fixture_dir or not fixture_dir.exists():
            return ReviewCaseExecutionResult(
                fixture_id=fixture.fixture_id,
                category=fixture.category,
                outcome=ReviewCaseOutcome.ERROR,
                has_defects_expected=fixture.expected.has_defects,
                duration_seconds=round(time.monotonic() - case_start, 4),
                error_message=f"Fixture directory not found: {fixture_dir}",
            )

        # 1. Read diff from fixture repository
        # Check if fixture uses diff.patch directly or is a full git repo
        diff_patch_file = fixture_dir / "diff.patch"
        commit_msg_file = fixture_dir / "commit_msg.txt"
        commit_msg = commit_msg_file.read_text(encoding="utf-8") if commit_msg_file.exists() else None

        raw_diff: str = ""
        try:
            if diff_patch_file.exists():
                raw_diff = diff_patch_file.read_text(encoding="utf-8")
            else:
                raw_diff = read_git_diff(
                    fixture_dir,
                    base=fixture.base_revision,
                    head=fixture.head_revision,
                )
        except Exception as exc:
            return ReviewCaseExecutionResult(
                fixture_id=fixture.fixture_id,
                category=fixture.category,
                outcome=ReviewCaseOutcome.ERROR,
                has_defects_expected=fixture.expected.has_defects,
                duration_seconds=round(time.monotonic() - case_start, 4),
                error_message=f"Diff acquisition error: {exc}",
            )

        # 2. Execute Review Engine (detector NEVER sees fixture.expected)
        engine = ReviewEngine(provider=provider)
        try:
            diff_bundle = parse_unified_diff(
                raw_diff,
                base_revision=fixture.base_revision,
                head_revision=fixture.head_revision,
            )
            report = engine.review_diff(
                diff_bundle,
                target_path=fixture_dir,
                mode=mode,
                commit_message=commit_msg,
            )
        except Exception as exc:
            return ReviewCaseExecutionResult(
                fixture_id=fixture.fixture_id,
                category=fixture.category,
                outcome=ReviewCaseOutcome.ERROR,
                has_defects_expected=fixture.expected.has_defects,
                duration_seconds=round(time.monotonic() - case_start, 4),
                error_message=f"Review execution error: {exc}",
            )

        # 3. Deduplicate predictions
        predictions = deduplicate_findings(report.findings)

        # 4. Compare predictions against ground truth
        expected = fixture.expected
        expected_findings = list(expected.expected_findings)

        tp = 0
        fp = 0
        fn = 0
        tn = 0
        wrong_file = 0

        valid_evidence_cnt = 0
        total_evidence_cnt = 0

        loc_candidates = len(predictions)
        loc_matches = 0

        severities: Dict[str, int] = {}

        # Track evidence status and severities
        for pred in predictions:
            sev_str = pred.severity.value.lower()
            severities[sev_str] = severities.get(sev_str, 0) + 1

            for ev in pred.calibrated_evidence:
                total_evidence_cnt += 1
                if ev.status == EvidenceCalibrationStatus.VERIFIED:
                    valid_evidence_cnt += 1

        matched_expected_indices = set()

        for pred in predictions:
            # Check match against expected findings
            match_found = False
            for idx, exp_find in enumerate(expected_findings):
                if idx in matched_expected_indices:
                    continue

                # Match by file path
                if pred.file_path == exp_find.file_path:
                    # If line number expected, check line proximity (+/- 5 lines) or commit message
                    if exp_find.line_number is not None and pred.line_number is not None:
                        if abs(pred.line_number - exp_find.line_number) <= 5:
                            loc_matches += 1
                            match_found = True
                            matched_expected_indices.add(idx)
                            tp += 1
                            break
                    elif exp_find.file_path == "<commit_message>":
                        loc_matches += 1
                        match_found = True
                        matched_expected_indices.add(idx)
                        tp += 1
                        break
                    elif exp_find.line_number is None:
                        loc_matches += 1
                        match_found = True
                        matched_expected_indices.add(idx)
                        tp += 1
                        break
                else:
                    # Finding on completely different file than expected
                    pass

            if not match_found:
                # Prediction did not match any expected finding
                # Check if it was an attribution failure (wrong file)
                expected_files = {ef.file_path for ef in expected_findings}
                if expected_files and pred.file_path not in expected_files:
                    wrong_file += 1
                fp += 1

        # Any expected findings not matched count as False Negatives
        fn = len(expected_findings) - len(matched_expected_indices)

        # True Negatives for clean fixtures
        if not expected.has_defects:
            if len(predictions) == 0:
                tn = 1
                outcome = ReviewCaseOutcome.PASS
            else:
                # Falsely reported findings on a clean fixture
                outcome = ReviewCaseOutcome.FAIL
        else:
            # Defective fixture: passes if all expected defects are detected with 0 false positives
            if fn == 0 and fp == 0:
                outcome = ReviewCaseOutcome.PASS
            else:
                outcome = ReviewCaseOutcome.FAIL

        duration = round(time.monotonic() - case_start, 4)

        return ReviewCaseExecutionResult(
            fixture_id=fixture.fixture_id,
            category=fixture.category,
            outcome=outcome,
            has_defects_expected=expected.has_defects,
            defects_detected=len(predictions),
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            true_negatives=tn,
            wrong_file_attributions=wrong_file,
            valid_evidence_count=valid_evidence_cnt,
            total_evidence_count=total_evidence_cnt,
            findings_count=len(predictions),
            duration_seconds=duration,
            details={
                "location_candidates": loc_candidates,
                "location_matches": loc_matches,
                "severities": severities,
            },
        )
