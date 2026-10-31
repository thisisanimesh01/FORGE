"""Builder and serializers for investigation reports in JSON and Markdown formats."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import shutil
import sys

from forge.config import redact_secrets, get_settings
from forge.reporting.models import (
    REPORT_SCHEMA_VERSION,
    InvestigationReport,
    ReportEvidenceItem,
    ReportFinding,
    ReportTimelineEvent,
)
from forge.schemas import EventType, FindingLifecycleState
from forge.storage.store import ForgeStore, StoreError


class ReportError(Exception):
    """Raised when report construction or export fails."""
    pass


class ReportBuilder:
    """Builds a structured InvestigationReport from stored run data."""

    def __init__(self, store: ForgeStore):
        self.store = store

    def build_report(self, run_id: str) -> InvestigationReport:
        """Construct an InvestigationReport for the specified run."""
        run = self.store.get_run(run_id)
        if not run:
            raise ReportError(f"Run '{run_id}' not found in database.")

        settings = get_settings()

        # Compute durations
        started_dt = run.started_at
        completed_dt = run.completed_at
        if completed_dt:
            duration = max(0.0, (completed_dt - started_dt).total_seconds())
        else:
            duration = max(0.0, (datetime.now(timezone.utc) - started_dt).total_seconds())

        # System environment diagnostics
        docker_available = bool(shutil.which("docker"))
        env_diag = {
            "python_version": sys.version.split()[0],
            "llm_provider": settings.llm_provider,
            "docker_available": docker_available,
            "secret_redaction": "active",
        }

        capabilities_unavailable = []
        if not docker_available:
            capabilities_unavailable.append(
                "Docker container sandbox unavailable: runtime code checks skipped or executed safely in-process."
            )
        if settings.llm_provider == "fake":
            capabilities_unavailable.append(
                "Live LLM provider unconfigured: investigation performed offline using deterministic fake provider."
            )

        # Retrieve findings and evidence
        raw_findings = self.store.list_findings(run_id=run_id)
        all_evidence = []
        findings_list: List[ReportFinding] = []

        findings_by_severity: Dict[str, int] = {}
        findings_by_state: Dict[str, int] = {}

        for f in raw_findings:
            # Track statistics
            sev = f.severity.value
            state = f.lifecycle_state.value
            findings_by_severity[sev] = findings_by_severity.get(sev, 0) + 1
            findings_by_state[state] = findings_by_state.get(state, 0) + 1

            # Get linked evidence
            finding_evidence_items: List[ReportEvidenceItem] = []
            ev_list = self.store.list_evidence_for_finding(f.finding_id)
            for ev in ev_list:
                item = ReportEvidenceItem(
                    evidence_id=ev.evidence_id,
                    evidence_type=ev.evidence_type.value,
                    description=redact_secrets(ev.description),
                    file_path=ev.file_path,
                    start_line=ev.start_line,
                    end_line=ev.end_line,
                    provenance=ev.provenance,
                    content_snippet=redact_secrets(ev.content[:500] + ("..." if len(ev.content) > 500 else "")),
                )
                finding_evidence_items.append(item)
                if item not in all_evidence:
                    all_evidence.append(item)

            findings_list.append(
                ReportFinding(
                    finding_id=f.finding_id,
                    title=redact_secrets(f.title),
                    description=redact_secrets(f.description),
                    severity=f.severity.value,
                    confidence=f.confidence.value,
                    lifecycle_state=f.lifecycle_state.value,
                    affected_files=f.affected_files,
                    limitations=redact_secrets(f.limitations or "") if f.limitations else None,
                    evidence_items=finding_evidence_items,
                )
            )

        # Query events timeline (bounded to first 100 events)
        raw_events = self.store.list_run_events(run_id=run_id)
        events_timeline: List[ReportTimelineEvent] = []
        for ev in raw_events[:100]:
            # Synthesize short human summary
            summary = ""
            if ev.event_type == EventType.RUN_CREATED:
                summary = f"Run initialized for target: {ev.payload.get('target', '')}"
            elif ev.event_type == EventType.LLM_REQUEST_STARTED:
                summary = f"LLM Turn {ev.payload.get('step', '?')} started"
            elif ev.event_type == EventType.TOOL_CALL_REQUESTED:
                summary = f"Tool called: {ev.payload.get('tool_name', '?')}"
            elif ev.event_type == EventType.TOOL_CALL_COMPLETED:
                summary = f"Tool finished: {ev.payload.get('tool_name', '?')}"
            elif ev.event_type == EventType.FINDING_CREATED:
                summary = f"Finding created: {ev.payload.get('title', '?')}"
            elif ev.event_type == EventType.FINDING_VALIDATION_FAILED:
                err_type = str(ev.payload.get("error_type", "ValidationError")).strip()
                title = str(ev.payload.get("title", "")).strip()
                f_path = str(ev.payload.get("file", "")).strip()
                lines = str(ev.payload.get("lines", "")).strip()
                cat = ev.payload.get("category")
                msg = str(ev.payload.get("message", "")).strip().replace("\n", " ")
                loc_str = f" at {f_path}:{lines}" if f_path and lines else (f" in {f_path}" if f_path else "")
                title_str = f" for '{title}'" if title else ""
                cat_str = f" [{cat}]" if cat else ""
                brief_msg = f": {msg[:120]}..." if len(msg) > 120 else (f": {msg}" if msg else "")
                summary = f"Finding validation failed [{err_type}]{cat_str}{title_str}{loc_str}{brief_msg}"
            elif ev.event_type == EventType.FINDING_DUPLICATE_DETECTED:
                title = str(ev.payload.get("title", "")).strip()
                f_path = str(ev.payload.get("file", "")).strip()
                lines = str(ev.payload.get("lines", "")).strip()
                msg = str(ev.payload.get("message", "")).strip().replace("\n", " ")
                loc_str = f" at {f_path}:{lines}" if f_path and lines else (f" in {f_path}" if f_path else "")
                title_str = f" '{title}'" if title else ""
                brief_msg = f": {msg[:120]}..." if len(msg) > 120 else (f": {msg}" if msg else "")
                summary = f"Duplicate finding rejected{title_str}{loc_str}{brief_msg}"
            elif ev.event_type == EventType.FINDING_STATE_TRANSITIONED:
                summary = f"Finding {ev.payload.get('finding_id', '?')} transitioned to {ev.payload.get('to_state', '?')}"
            elif ev.event_type == EventType.RUN_FAILED:
                cat = ev.payload.get("failure_category")
                err = ev.payload.get("error_message") or ev.payload.get("error")
                stage = ev.payload.get("stage")
                step = ev.payload.get("step")
                cat_str = f" [{cat}]" if cat else ""
                stage_str = f" at {stage}" if stage else ""
                step_str = f" (turn {step})" if step else ""
                err_str = f": {err[:120]}..." if err and len(err) > 120 else (f": {err}" if err else "")
                if cat or err:
                    summary = f"Run failed{cat_str}{stage_str}{step_str}{err_str}"
                else:
                    summary = f"Run finished with status: {ev.payload.get('status', 'failed')}"
            elif ev.event_type == EventType.RUN_COMPLETED:
                summary = f"Run completed successfully: {ev.payload.get('status', 'completed')}"
            else:
                summary = f"{ev.event_type.value}"

            events_timeline.append(
                ReportTimelineEvent(
                    sequence=ev.sequence,
                    event_type=ev.event_type.value,
                    timestamp=ev.timestamp.isoformat(),
                    summary=redact_secrets(summary),
                )
            )

        rejected_events = [
            ev for ev in raw_events
            if ev.event_type in (EventType.FINDING_VALIDATION_FAILED, EventType.FINDING_DUPLICATE_DETECTED)
        ]
        summary_counts = {
            "total_findings": len(findings_list),
            "findings_by_severity": findings_by_severity,
            "findings_by_state": findings_by_state,
            "total_evidence": len(all_evidence),
            "total_events": len(raw_events),
            "rejected_claims_count": len(rejected_events),
        }

        # Extract failure details and final synthesis
        summary_dict = self.store.get_run_summary_dict(run_id) if hasattr(self.store, "get_run_summary_dict") else {}
        failure_details = summary_dict.get("failure_details")
        if not failure_details and run.status == "failed":
            for ev in reversed(raw_events):
                if ev.event_type == EventType.RUN_FAILED and ("error" in ev.payload or "failure_category" in ev.payload):
                    cat = ev.payload.get("failure_category", "unknown")
                    msg = redact_secrets(str(ev.payload.get("error_message") or ev.payload.get("error", "")))
                    failure_details = {
                        "category": cat,
                        "failure_category": cat,
                        "message": msg,
                        "error_message": msg,
                        "stage": ev.payload.get("stage", "unknown"),
                        "step": ev.payload.get("step", 0),
                        "retryable": ev.payload.get("retryable", False),
                        "timestamp": ev.payload.get("timestamp", ev.timestamp.isoformat()),
                    }
                    break
        elif failure_details:
            cat = failure_details.get("category") or failure_details.get("failure_category", "unknown")
            msg = redact_secrets(str(failure_details.get("message") or failure_details.get("error_message", "")))
            failure_details["category"] = cat
            failure_details["failure_category"] = cat
            failure_details["message"] = msg
            failure_details["error_message"] = msg

        final_synthesis = summary_dict.get("final_synthesis")

        return InvestigationReport(
            schema_version=REPORT_SCHEMA_VERSION,
            run_id=run.run_id,
            target=run.target,
            status=run.status,
            started_at=run.started_at.isoformat(),
            completed_at=run.completed_at.isoformat() if run.completed_at else None,
            duration_seconds=round(duration, 2),
            system_environment=env_diag,
            capabilities_unavailable=capabilities_unavailable,
            summary_counts=summary_counts,
            findings=findings_list,
            evidence=all_evidence,
            events_timeline=events_timeline,
            failure_details=failure_details,
            final_synthesis=final_synthesis,
        )


InvestigationReportBuilder = ReportBuilder


def report_to_json(report: InvestigationReport, indent: int = 2) -> str:
    """Serialize report to deterministic formatted JSON."""
    data = report.model_dump()
    return json.dumps(data, indent=indent, sort_keys=True)


def report_to_markdown(report: InvestigationReport) -> str:
    """Format report into clean, readable GitHub-flavored Markdown."""
    lines = [
        f"# FORGE Investigation Report: `{report.run_id}`",
        "",
        f"- **Target**: `{report.target}`",
        f"- **Status**: `{report.status.upper()}`",
        f"- **Started**: {report.started_at}",
        f"- **Completed**: {report.completed_at or 'In Progress'}",
        f"- **Duration**: {report.duration_seconds}s",
        f"- **Report Schema Version**: `{report.schema_version}`",
        "",
    ]

    # Capabilities Unavailable / Security Notice
    if report.capabilities_unavailable:
        lines.append("## Operational Notices & Constraints")
        for cap in report.capabilities_unavailable:
            lines.append(f"- ⚠️ {cap}")
        lines.append("")

    # Summary Statistics
    counts = report.summary_counts
    lines.append("## Executive Summary")
    lines.append(f"- **Total Findings**: {counts.get('total_findings', 0)}")
    lines.append(f"- **Total Evidence Records**: {counts.get('total_evidence', 0)}")
    lines.append(f"- **Total Audit Events**: {counts.get('total_events', 0)}")
    lines.append(f"- **Rejected / Disproven Claims**: {counts.get('rejected_claims_count', 0)}")
    lines.append("")

    # Failure Details if run failed
    if report.failure_details:
        fd = report.failure_details
        lines.append("## Investigation Failure Details")
        if fd.get("category"):
            lines.append(f"- **Failure Category**: `{fd.get('category')}`")
        if fd.get("stage"):
            stage_str = f"`{fd.get('stage')}`"
            if fd.get("step"):
                stage_str += f" (Turn {fd.get('step')})"
            lines.append(f"- **Execution Stage**: {stage_str}")
        if "retryable" in fd:
            lines.append(f"- **Retryable**: {'Yes' if fd.get('retryable') else 'No'}")
        if fd.get("message"):
            lines.append(f"- **Error Message**: {fd.get('message')}")
        lines.append("")

    # Severity & Lifecycle Breakdowns
    sev_counts = counts.get("findings_by_severity", {})
    state_counts = counts.get("findings_by_state", {})

    lines.append("### Findings Breakdown")
    lines.append("| Severity | Count | Lifecycle State | Count |")
    lines.append("|---|---|---|---|")
    severities = ["critical", "high", "medium", "low", "info"]
    states = ["suspected", "reproduced", "root_caused", "fixed", "verified"]
    for i in range(max(len(severities), len(states))):
        s_name = severities[i] if i < len(severities) else ""
        s_cnt = sev_counts.get(s_name, 0) if s_name else ""
        st_name = states[i] if i < len(states) else ""
        st_cnt = state_counts.get(st_name, 0) if st_name else ""
        lines.append(f"| {s_name.upper() if s_name else ''} | {s_cnt} | {st_name.upper() if st_name else ''} | {st_cnt} |")
    lines.append("")

    # Detailed Findings
    lines.append("## Findings & Evidence")
    if not report.findings:
        lines.append("*No findings recorded for this run.*")
        lines.append("")
    else:
        for f in report.findings:
            lines.append(f"### [{f.severity.upper()}] {f.title} (`{f.finding_id}`)")
            lines.append(f"- **State**: `{f.lifecycle_state.upper()}`")
            lines.append(f"- **Confidence**: `{f.confidence.upper()}`")
            if f.affected_files:
                lines.append(f"- **Affected Files**: {', '.join(f'`{p}`' for p in f.affected_files)}")
            lines.append(f"- **Description**: {f.description}")
            if f.limitations:
                lines.append(f"- **Limitations / Uncertainties**: {f.limitations}")

            if f.evidence_items:
                lines.append("")
                lines.append("#### Supporting Evidence")
                for ev in f.evidence_items:
                    loc = f" (`{ev.file_path}:{ev.start_line}-{ev.end_line}`)" if ev.file_path else ""
                    lines.append(f"- **`{ev.evidence_id}`** [{ev.evidence_type}]{loc}: {ev.description}")
                    if ev.content_snippet:
                        lines.append("  ```")
                        lines.append(f"  {ev.content_snippet.strip()}")
                        lines.append("  ```")
            else:
                lines.append("- *Evidence*: None attached (unverified claim).")
            lines.append("")

    # Final Synthesis & Assessment
    if report.final_synthesis:
        lines.append("## Final Synthesis & Assessment")
        lines.append(report.final_synthesis.strip())
        lines.append("")

    # Timeline of Events
    if report.events_timeline:
        lines.append("## Audit Event Log (Sample)")
        lines.append("| Seq | Timestamp | Event Type | Summary |")
        lines.append("|---|---|---|---|")
        for ev in report.events_timeline[:25]:
            lines.append(f"| {ev.sequence} | {ev.timestamp[:19]} | `{ev.event_type}` | {ev.summary} |")
        if len(report.events_timeline) > 25:
            lines.append(f"| ... | ... | *(+{len(report.events_timeline) - 25} more events)* | ... |")
        lines.append("")

    return "\n".join(lines)


def export_report(
    report: InvestigationReport,
    output_path: Path,
    output_format: str = "markdown",
) -> Path:
    """Safely export report to specified path in JSON or Markdown format."""
    resolved_path = output_path.resolve()

    # Safety check: do not write outside user workspace or root directories unless explicitly allowed
    if resolved_path.is_dir():
        filename = f"forge_report_{report.run_id}.{'json' if output_format == 'json' else 'md'}"
        target_file = resolved_path / filename
    else:
        target_file = resolved_path

    target_file.parent.mkdir(parents=True, exist_ok=True)

    if output_format.lower() == "json":
        content = report_to_json(report)
    else:
        content = report_to_markdown(report)

    target_file.write_text(content, encoding="utf-8")
    return target_file
