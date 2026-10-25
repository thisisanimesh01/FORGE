"""Corpus loader and ground-truth manifest manager for review benchmarks."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from forge.benchmarks.review_models import (
    ReviewBenchmarkFixtureMeta,
    ReviewBenchmarkCategory,
    ExpectedReviewOutcome,
    ExpectedReviewFinding,
)
from forge.repository.validation import assert_contained_path

DEFAULT_REVIEW_FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "fixtures"
    / "review_benchmarks"
)


class ReviewCorpusError(Exception):
    """Raised when review fixture discovery or loading fails."""
    pass


class ReviewCorpusLoader:
    """Discovers, validates, and loads review benchmark fixtures from disk."""

    def __init__(self, fixtures_dir: Optional[Path] = None):
        self.fixtures_dir = (fixtures_dir or DEFAULT_REVIEW_FIXTURES_DIR).resolve()

    def discover_fixtures(self) -> List[ReviewBenchmarkFixtureMeta]:
        """Discover and validate all review fixtures in deterministic order."""
        if not self.fixtures_dir.exists() or not self.fixtures_dir.is_dir():
            raise ReviewCorpusError(f"Review benchmark fixtures directory not found: {self.fixtures_dir}")

        fixtures: List[ReviewBenchmarkFixtureMeta] = []
        seen_ids = set()

        for fixture_dir in sorted(self.fixtures_dir.iterdir()):
            if not fixture_dir.is_dir():
                continue

            # Path containment assertion
            assert_contained_path(fixture_dir, self.fixtures_dir)
            assert_contained_path(fixture_dir.resolve(), self.fixtures_dir)

            meta_file = fixture_dir / "fixture.json"
            if not meta_file.exists() or not meta_file.is_file():
                continue

            fixture = self.load_fixture(meta_file, fixture_dir)
            if fixture.fixture_id in seen_ids:
                raise ReviewCorpusError(f"Duplicate review fixture ID discovered: '{fixture.fixture_id}'")

            seen_ids.add(fixture.fixture_id)
            fixtures.append(fixture)

        return sorted(fixtures, key=lambda f: f.fixture_id)

    def load_fixture(self, meta_file: Path, fixture_dir: Path) -> ReviewBenchmarkFixtureMeta:
        """Parse and validate an individual review fixture manifest."""
        try:
            raw = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReviewCorpusError(f"Malformed JSON in {meta_file}: {exc}")

        try:
            exp_data = raw["expected"]
            findings_data = exp_data.get("expected_findings", [])
            expected_findings = [ExpectedReviewFinding(**f) for f in findings_data]

            expected_outcome = ExpectedReviewOutcome(
                has_defects=exp_data.get("has_defects", True),
                expected_findings=expected_findings,
                allow_benign_clean=exp_data.get("allow_benign_clean", False),
                evidence_status_expected=exp_data.get("evidence_status_expected"),
                requires_docker=exp_data.get("requires_docker", False),
            )

            fixture = ReviewBenchmarkFixtureMeta(
                fixture_id=raw["fixture_id"],
                name=raw["name"],
                description=raw["description"],
                category=ReviewBenchmarkCategory(raw["category"]),
                base_revision=raw.get("base_revision", "HEAD~1"),
                head_revision=raw.get("head_revision", "HEAD"),
                expected=expected_outcome,
                fixture_dir=fixture_dir,
            )
            return fixture
        except Exception as exc:
            raise ReviewCorpusError(f"Schema validation error in {meta_file}: {exc}")
