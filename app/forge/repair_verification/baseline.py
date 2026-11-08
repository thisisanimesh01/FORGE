"""Phase 12 M5: Baseline Verification Runner.

Executes verification plans against an isolated baseline snapshot:
- Validates baseline snapshot tree digest prior to execution.
- Copies immutable snapshot to a temporary baseline execution workspace.
- Runs identical check vectors under identical sandbox isolation boundaries.
- Distinguishes pre-existing test failures from repair regressions.
- Cleans up temporary baseline execution workspace in finally block.
"""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
from typing import List, Optional, Tuple

from forge.repair_execution.schemas import RepairWorkspace
from forge.repair_execution.workspace import compute_tree_digest
from forge.repair_verification.integrity import VerificationIntegrityChecker
from forge.repair_verification.result_parser import PytestResultParser
from forge.repair_verification.sandbox import VerificationSandbox
from forge.repair_verification.schemas import (
    CheckExecutionResult,
    CheckOutcome,
    SuiteExecutionSummary,
    VerificationPlan,
    VerificationPolicy,
)


class BaselineVerificationRunner:
    """Coordinates baseline verification against immutable baseline snapshots."""

    def __init__(self, sandbox: Optional[VerificationSandbox] = None) -> None:
        self.sandbox = sandbox or VerificationSandbox()

    def run_baseline(
        self,
        plan: VerificationPlan,
        workspace: RepairWorkspace,
    ) -> SuiteExecutionSummary:
        """Execute verification plan against isolated baseline snapshot."""
        # 1. Locate and verify baseline snapshot
        snap_path = self._resolve_baseline_snapshot(workspace)
        if not snap_path or not snap_path.exists():
            return SuiteExecutionSummary(
                execution_type="baseline",
                workspace_id=workspace.workspace_id,
                tree_digest="",
                checks=[],
                all_passed=False,
                completed_at=datetime.now(timezone.utc).isoformat(),
                sandbox_available=self.sandbox.is_available(),
                sandbox_details={"error": "Baseline snapshot directory not found"},
            )

        is_intact, err = VerificationIntegrityChecker.verify_baseline_snapshot(
            snapshot_path=snap_path,
            expected_tree_digest=plan.expected_baseline_digest,
        )
        if not is_intact:
            return SuiteExecutionSummary(
                execution_type="baseline",
                workspace_id=workspace.workspace_id,
                tree_digest="",
                checks=[],
                all_passed=False,
                completed_at=datetime.now(timezone.utc).isoformat(),
                sandbox_available=self.sandbox.is_available(),
                sandbox_details={"error": f"Corrupted baseline snapshot: {err}"},
            )

        # 2. Allocate temporary execution directory for baseline run
        temp_dir = Path(tempfile.mkdtemp(prefix=f"forge_base_run_{workspace.workspace_id}_"))
        try:
            # Copy snapshot contents to temp_dir
            for item in snap_path.iterdir():
                dest = temp_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, symlinks=True)
                else:
                    shutil.copy2(item, dest)

            # 3. Execute all selected checks
            check_results: List[CheckExecutionResult] = []
            all_passed = True

            for chk in plan.selected_checks:
                sbx_res = self.sandbox.execute_check(
                    check=chk,
                    workspace_path=temp_dir,
                    policy=plan.policy,
                    run_id=f"base_{workspace.workspace_id}",
                )
                parsed = PytestResultParser.parse_result(check=chk, sandbox_res=sbx_res)
                check_results.append(parsed)
                if parsed.outcome != CheckOutcome.PASSED:
                    all_passed = False

            return SuiteExecutionSummary(
                execution_type="baseline",
                workspace_id=workspace.workspace_id,
                tree_digest=plan.expected_baseline_digest,
                checks=check_results,
                all_passed=all_passed,
                completed_at=datetime.now(timezone.utc).isoformat(),
                sandbox_available=self.sandbox.is_available(),
                sandbox_details={"snapshot_path": str(snap_path)},
            )
        finally:
            # Clean up temp execution dir
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    def _resolve_baseline_snapshot(self, workspace: RepairWorkspace) -> Optional[Path]:
        """Locate baseline snapshot directory from workspace metadata."""
        if workspace.baseline_snapshot_path:
            p = Path(workspace.baseline_snapshot_path)
            if p.exists():
                return p

        # Check standard convention: adjacent directory <workspace_id>_baseline
        ws_path = Path(workspace.isolated_workspace_path)
        adj_snap = ws_path.parent / f"{workspace.workspace_id}_baseline"
        if adj_snap.exists():
            return adj_snap

        return None
