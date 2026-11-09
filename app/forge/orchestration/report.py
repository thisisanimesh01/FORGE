"""Phase 13 M1: Engineering Report Generation.

Formats comprehensive, evidence-driven markdown summaries distinguishing each stage
of the autonomous engineering lifecycle with strict audit provenance and safety notices.
"""

from datetime import datetime, timezone
from typing import Optional

from forge.orchestration.schemas import EngineeringWorkflowResult, StageStatus, WorkflowState


class EngineeringReportGenerator:
    """Generates structured Markdown reports for engineering workflows."""

    @staticmethod
    def generate_markdown(result: EngineeringWorkflowResult) -> str:
        """Produce a complete markdown audit report for an engineering workflow."""
        completed_ts = (
            result.completed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            if result.completed_at
            else "In Progress / Interrupted"
        )
        created_ts = result.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")

        state_badge = f"**{result.state.value}**"
        if result.state == WorkflowState.COMPLETED:
            outcome_header = "### Workflow Outcome: SUCCESSFUL VERIFIED REPAIR"
        elif result.state == WorkflowState.BLOCKED:
            outcome_header = "### Workflow Outcome: BLOCKED BY SAFETY GATE"
        elif result.state == WorkflowState.FAILED:
            outcome_header = "### Workflow Outcome: FAILED WITH UNRESOLVED ERROR"
        elif result.state == WorkflowState.AWAITING_HUMAN_APPROVAL:
            outcome_header = "### Workflow Outcome: AWAITING HUMAN AUTHORIZATION"
        elif result.state == WorkflowState.CANCELLED:
            outcome_header = "### Workflow Outcome: WORKFLOW CANCELLED"
        else:
            outcome_header = f"### Workflow Outcome: {result.state.value}"

        lines = [
            f"# FORGE Engineering Orchestration Report",
            f"",
            f"**Workflow ID:** `{result.workflow_id}`  ",
            f"**Current State:** {state_badge}  ",
            f"**Repository Path:** `{result.repository_path}`  ",
            f"**Commit SHA:** `{result.commit_sha or 'Unspecified'}`  ",
            f"**Started At:** {created_ts}  ",
            f"**Completed At:** {completed_ts}  ",
            f"**Audit Event Count:** {result.event_count} cryptographically chained events  ",
            f"",
            f"---",
            f"",
            outcome_header,
            f"",
            f"> [!IMPORTANT]",
            f"> **Human Review Mandate:** Autonomous repair execution, verification, and quality scoring",
            f"> are advisory and audit-controlled. Software deployment or branch merging strictly requires human review.",
            f"",
            f"---",
            f"",
            f"## 1. Stage Execution Summary",
            f"",
            f"| Stage | Status | Completed At | Errors / Notes |",
            f"|---|---|---|---|",
        ]

        # Stage table rows
        stage_order = [
            "DIAGNOSIS",
            "ROOT_CAUSE",
            "PLANNING",
            "APPROVAL",
            "EXECUTION",
            "VERIFICATION",
            "QUALITY",
        ]
        for s_name in stage_order:
            rec = result.stages.get(s_name)
            if rec:
                c_at = rec.completed_at.strftime("%H:%M:%S") if rec.completed_at else "-"
                err = rec.error_message or "-"
                lines.append(f"| **{s_name}** | `{rec.status.value}` | {c_at} | {err} |")
            else:
                lines.append(f"| **{s_name}** | `NOT_STARTED` | - | - |")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 2. Evidence & Stage Provenance",
            f"",
            f"- **Diagnosis Reference:** `{result.diagnosis_id or 'None'}`",
            f"- **Root Cause Report:** `{result.root_cause_report_id or 'None'}`",
            f"- **Repair Plan ID:** `{result.repair_plan_id or 'None'}`",
            f"- **Repair Plan Digest:** `{result.repair_plan_digest or 'None'}`",
            f"- **Approval ID:** `{result.approval_id or 'Pending / None'}`",
            f"- **Approver Identity:** `{result.approver_identity or 'None'}`",
            f"- **Execution ID:** `{result.execution_id or 'None'}`",
            f"- **Isolated Workspace ID:** `{result.workspace_id or 'None'}`",
            f"- **Applied Patch Digest:** `{result.applied_patch_digest or 'None'}`",
            f"- **Baseline Tree Digest:** `{result.baseline_tree_digest or 'None'}`",
            f"- **Verification ID:** `{result.verification_id or 'None'}`",
            f"- **Verification Status:** `{result.verification_status or 'None'}`",
            f"- **Regression Classification:** `{result.regression_classification or 'None'}`",
            f"- **Quality Gate Decision:** `{result.quality_gate_decision or 'None'}`",
            f"- **Composite Quality Score:** `{f'{result.quality_score:.2f}' if result.quality_score is not None else 'None'}`",
            f"",
            f"---",
            f"",
            f"## 3. Policy Violations & Limitations",
            f"",
        ])

        if result.policy_violations:
            lines.append("### Active Policy Violations:")
            for pv in result.policy_violations:
                lines.append(f"- ⚠️ **VIOLATION:** {pv}")
            lines.append("")
        else:
            lines.append("No active quality policy violations observed.\n")

        if result.limitations:
            lines.append("### Observed Limitations:")
            for lim in result.limitations:
                lines.append(f"- ℹ️ {lim}")
            lines.append("")
        else:
            lines.append("No limitations recorded for this workflow.\n")

        lines.extend([
            f"---",
            f"",
            f"*Report generated by FORGE Autonomous Engineering Orchestrator (Phase 13 Milestone 1)*",
        ])

        return "\n".join(lines)
