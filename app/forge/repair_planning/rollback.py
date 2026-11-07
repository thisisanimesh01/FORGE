"""Phase 12 M3: Rollback Strategy and Reversal Planning.

Constructs explicit, structured rollback plans specifying changes to reverse,
trigger conditions, cache/config invalidations, and acknowledged limitations.
"""

from pathlib import Path
from typing import List, Optional

from forge.repair_planning.schemas import (
    RepairApproach,
    RequiredChange,
    RollbackPlan,
)


class RollbackPlanBuilder:
    """Constructs safe, bounded rollback procedures for proposed repair plans."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path.resolve()

    def build_rollback_plan(
        self,
        affected_files: List[str],
        affected_symbols: Optional[List[str]] = None,
        required_changes: Optional[List[RequiredChange]] = None,
        approach: Optional[RepairApproach] = None,
    ) -> RollbackPlan:
        """Construct a detailed RollbackPlan for the proposed repair."""
        symbols = affected_symbols or []
        changes = required_changes or []

        changes_to_reverse: List[str] = []
        if changes:
            for chg in changes:
                loc = f"{chg.file_path}"
                if chg.start_line is not None and chg.end_line is not None:
                    loc += f" lines {chg.start_line}-{chg.end_line}"
                changes_to_reverse.append(
                    f"Revert {chg.change_type} on {loc}: restore previous implementation of '{chg.symbol_id or 'target block'}'."
                )
        else:
            for f in affected_files:
                changes_to_reverse.append(f"Revert all modifications to '{f}' to previous git commit state.")

        files_and_symbols = list(set(affected_files + symbols))

        # Dependencies to restore
        dependencies_to_restore = [
            "Preserve existing dependency lockfile and package pins; no new package dependencies permitted."
        ]

        # Data & Configuration Considerations
        data_config: List[str] = [
            "Clear cached AST index and incremental graph cache for modified files.",
        ]
        if approach == RepairApproach.CONFIGURATION_CORRECTION:
            data_config.append("Restore original configuration files and environment variable defaults.")
        elif approach == RepairApproach.RESOURCE_MANAGEMENT:
            data_config.append("Verify all open file descriptors and connection pools are closed during rollback.")

        # Rollback triggers
        rollback_triggers = [
            "Targeted verification test suite fails after applying the repair.",
            "Any regression detected in broader test suite execution.",
            "SyntaxError or ImportError raised upon loading modified module(s).",
            "Performance regression exceeding 10% in relevant benchmark checks.",
            "Unexpected unhandled exception or crash observed during verification.",
        ]

        # Known limitations
        known_limitations = [
            "Automated rollback relies on git working-tree restoration; external state changes cannot be reverted.",
            "In-memory state of already running processes requires process restart to take effect.",
            "Concurrency races during live rollback are not guarded without process synchronization.",
        ]

        return RollbackPlan(
            changes_to_reverse=changes_to_reverse,
            files_and_symbols_involved=sorted(files_and_symbols),
            dependencies_to_restore=dependencies_to_restore,
            data_configuration_considerations=data_config,
            rollback_triggers=rollback_triggers,
            known_limitations=known_limitations,
        )
