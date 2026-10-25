"""Corpus loader and schema validator for benchmark test cases."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from forge.benchmarks.models import BenchmarkCase, BenchmarkCategory
from forge.repository.validation import assert_contained_path, SecurityError


class BenchmarkCorpusError(Exception):
    """Raised when fixture discovery, loading, or validation fails."""
    pass


DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures" / "benchmarks"


class CorpusLoader:
    """Discovers, validates, and loads benchmark cases from disk."""

    def __init__(self, fixtures_dir: Optional[Path] = None):
        self.fixtures_dir = (fixtures_dir or DEFAULT_FIXTURES_DIR).resolve()

    def discover_cases(self) -> List[BenchmarkCase]:
        """Discover and validate all benchmark cases in the fixture directory in deterministic order."""
        if not self.fixtures_dir.exists() or not self.fixtures_dir.is_dir():
            raise BenchmarkCorpusError(f"Benchmark fixtures directory not found: {self.fixtures_dir}")

        cases: List[BenchmarkCase] = []
        seen_ids = set()

        for case_dir in sorted(self.fixtures_dir.iterdir()):
            if not case_dir.is_dir():
                continue

            # Ensure case_dir is strictly contained inside fixtures_dir
            assert_contained_path(case_dir, self.fixtures_dir)
            assert_contained_path(case_dir.resolve(), self.fixtures_dir)

            meta_file = case_dir / "case.json"
            if not meta_file.exists() or not meta_file.is_file():
                continue

            case = self.load_case(meta_file, case_dir)
            if case.case_id in seen_ids:
                raise BenchmarkCorpusError(f"Duplicate benchmark case ID discovered: '{case.case_id}'")

            seen_ids.add(case.case_id)
            cases.append(case)

        return sorted(cases, key=lambda c: c.case_id)

    def load_case(self, meta_file: Path, case_dir: Path) -> BenchmarkCase:
        """Parse and validate an individual benchmark case JSON file."""
        try:
            raw = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BenchmarkCorpusError(f"Malformed JSON in {meta_file}: {exc}")

        try:
            case = BenchmarkCase(
                case_id=raw["case_id"],
                name=raw["name"],
                description=raw["description"],
                category=BenchmarkCategory(raw["category"]),
                target_file=raw["target_file"],
                target_symbol=raw.get("target_symbol"),
                expected_defect=raw["expected_defect"],
                execution_requirements=raw.get("execution_requirements", {}),
                fixture_dir=case_dir,
            )
        except Exception as exc:
            raise BenchmarkCorpusError(f"Schema validation error in {meta_file}: {exc}")

        # Validate path containment for target_file inside fixture_dir
        target_path = case_dir / case.target_file
        try:
            assert_contained_path(target_path, case_dir)
        except SecurityError as sec_err:
            raise BenchmarkCorpusError(f"Security error in {case.case_id}: target_file escapes directory: {sec_err}")

        return case
