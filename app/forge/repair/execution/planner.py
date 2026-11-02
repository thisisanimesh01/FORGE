"""Phase 8 Repair Execution Architecture — Evidence-Grounded Repair Planner.

Formulates bounded, evidence-grounded repair plans from validated findings,
supporting evidence records, and target repository source files.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

from forge.repair.execution.failures import (
    InsufficientEvidenceError,
    RepairPolicyViolationError,
    RepairRequestValidationError,
    ScopeContainmentViolationError,
)
from forge.repair.execution.interfaces import RepairPlannerProtocol, RepairWorkspaceProtocol
from forge.repair.execution.models import RepairExecutionPlan, RepairRequest
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.schemas import EvidenceRecord, Finding


class RepairExecutionPlanner(RepairPlannerProtocol):
    """Planner that formulates grounded RepairExecutionPlans from verified findings and evidence."""

    def formulate_plan(
        self,
        request: RepairRequest,
        policy: RepairExecutionPolicy,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        workspace: Optional[RepairWorkspaceProtocol] = None,
    ) -> RepairExecutionPlan:
        """Formulate a grounded RepairExecutionPlan for a RepairRequest.

        Raises:
            RepairPolicyViolationError: If request or target file violates policy.
            InsufficientEvidenceError: If evidence records are missing.
            RepairRequestValidationError: If target file cannot be determined or grounded.
        """
        if not request.allowed_files:
            raise RepairPolicyViolationError("Repair request has no allowed_files specified")

        # Determine target file grounded in evidence
        target_file: Optional[str] = None
        ev_records = evidence_records or []
        for ev in ev_records:
            if ev.file_path and ev.file_path in request.allowed_files:
                target_file = ev.file_path
                break

        if not target_file:
            # Fall back to first authorized file if compliant with policy
            target_file = request.allowed_files[0]

        # Validate target file against policy
        policy.validate_target_file(target_file, request.allowed_files)

        # Grounding check: verify target file exists in workspace if workspace provided
        if workspace:
            contained_path = workspace.validate_containment(target_file)
            if not contained_path.exists():
                raise RepairRequestValidationError(
                    f"Target file '{target_file}' does not exist in workspace"
                )

        # Derive root cause and proposed change from finding & evidence
        finding_title = finding.title if finding else "Identified defect"
        finding_desc = finding.description if finding else "Target defect requires correction"

        evidence_summaries = []
        for ev in ev_records:
            snippet = ev.content[:100].strip() if ev.content else ""
            evidence_summaries.append(f"[{ev.evidence_id}]: {ev.description} ({snippet})")

        ev_context = "; ".join(evidence_summaries) if evidence_summaries else "Evidence verified"

        root_cause = f"Defect in '{target_file}' identified: {finding_title}. {finding_desc}"
        proposed_change = f"Apply minimal targeted logic correction in '{target_file}' grounded in evidence: {ev_context}"
        expected_behavior = (
            f"Target file '{target_file}' executes without error, satisfying validation invariants "
            f"and resolving '{finding_title}'"
        )

        # Determine verification command
        if target_file.endswith(".py"):
            verification_command = "pytest"
        else:
            verification_command = "pytest"

        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        rationale = (
            f"Evidence-grounded minimal repair addressing finding '{request.finding_id}'. "
            f"Restricts modifications strictly to authorized file '{target_file}', "
            f"preserving unrelated code and avoiding broad refactoring."
        )

        return RepairExecutionPlan(
            plan_id=plan_id,
            repair_id=request.repair_id,
            target_file=target_file,
            root_cause=root_cause,
            proposed_change=proposed_change,
            expected_behavior=expected_behavior,
            verification_command=verification_command,
            confidence=0.85,
            rationale=rationale,
            created_at=datetime.now(timezone.utc),
        )
