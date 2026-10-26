"""Corpus loader and ground-truth manifest manager for repair quality benchmarks."""

import json
from pathlib import Path
from typing import List, Optional

from forge.benchmarks.repair_models import (
    ExpectedPatchConstraints,
    ExpectedRepairOutcomeSpec,
    RepairBenchmarkCategory,
    RepairBenchmarkFixtureMeta,
)
from forge.repository.validation import assert_contained_path
from forge.schemas import FindingSeverity

DEFAULT_REPAIR_FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "fixtures"
    / "repair_benchmarks"
)


class RepairCorpusError(Exception):
    """Raised when repair benchmark fixture discovery or loading fails."""
    pass


class RepairCorpusLoader:
    """Discovers, validates, and loads repair benchmark fixtures from disk."""

    def __init__(self, fixtures_dir: Optional[Path] = None):
        self.fixtures_dir = (fixtures_dir or DEFAULT_REPAIR_FIXTURES_DIR).resolve()

    def discover_fixtures(self) -> List[RepairBenchmarkFixtureMeta]:
        """Discover and validate all repair fixtures in deterministic order."""
        if not self.fixtures_dir.exists() or not self.fixtures_dir.is_dir():
            raise RepairCorpusError(f"Repair benchmark fixtures directory not found: {self.fixtures_dir}")

        fixtures: List[RepairBenchmarkFixtureMeta] = []
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
                raise RepairCorpusError(f"Duplicate repair fixture ID discovered: '{fixture.fixture_id}'")
            seen_ids.add(fixture.fixture_id)
            fixtures.append(fixture)

        return fixtures

    def load_fixtures(self) -> List[RepairBenchmarkFixtureMeta]:
        """Convenience alias for discover_fixtures."""
        return self.discover_fixtures()

    def get_fixture(self, fixture_id: str) -> RepairBenchmarkFixtureMeta:
        """Retrieve a specific fixture by fixture_id."""
        for f in self.discover_fixtures():
            if f.fixture_id == fixture_id:
                return f
        raise KeyError(f"Fixture not found: '{fixture_id}'")

    def load_fixture(self, meta_file: Path, fixture_dir: Path) -> RepairBenchmarkFixtureMeta:
        """Parse and validate a single repair fixture metadata file."""
        try:
            content = meta_file.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as e:
            raise RepairCorpusError(f"Failed to read or parse fixture metadata '{meta_file}': {e}")

        # Load optional diff patch file if present
        diff_file = fixture_dir / "patch.diff"
        scripted_diff = None
        if diff_file.exists() and diff_file.is_file():
            scripted_diff = diff_file.read_text(encoding="utf-8")

        try:
            return RepairBenchmarkFixtureMeta(
                fixture_id=data["fixture_id"],
                name=data.get("name", data["fixture_id"]),
                description=data.get("description", ""),
                category=RepairBenchmarkCategory(data["category"]),
                target_file=data["target_file"],
                base_commit_sha=data.get("base_commit_sha", "a1b2c3d4e5f67890"),
                severity=FindingSeverity(data.get("severity", "high")),
                expected=ExpectedRepairOutcomeSpec.model_validate(data["expected"]),
                constraints=ExpectedPatchConstraints.model_validate(data["constraints"]),
                scripted_response=data.get("scripted_response"),
                scripted_diff=scripted_diff or data.get("scripted_diff"),
                fixture_dir=fixture_dir.resolve(),
            )
        except Exception as e:
            raise RepairCorpusError(f"Invalid repair fixture schema in '{meta_file}': {e}")
