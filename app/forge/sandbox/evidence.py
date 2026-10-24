"""Integration helpers connecting SandboxResult to Evidence and Event history."""

from typing import Optional

from forge.analysis.evidence import create_evidence
from forge.schemas import (
    EvidenceRecord,
    EvidenceType,
    EventType,
)
from forge.sandbox.models import ExecutionStatus, SandboxResult
from forge.storage.store import ForgeStore


def record_sandbox_result_in_store(
    result: SandboxResult,
    store: ForgeStore,
    finding_id: Optional[str] = None,
    attach_as_evidence: bool = True,
) -> Optional[EvidenceRecord]:
    """Record events and optional evidence for a sandbox execution result in the persistent store.

    Guarantees:
    - Records appropriate EventType (SANDBOX_EXECUTION_COMPLETED, FAILED, or UNAVAILABLE).
    - If executed and evidence requested, creates an EvidenceRecord with provenance 'forge:sandbox'.
    - If status was REJECTED or UNAVAILABLE, no misleading execution evidence is attached.
    """
    event_payload = {
        "request_id": result.request_id,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "isolation_established": result.isolation_established,
        "failure_classification": result.failure_classification.value,
    }
    if result.error_message:
        event_payload["error_message"] = result.error_message

    # Verify run exists in store before attempting to record
    run = store.get_run(result.run_id)
    if not run:
        raise ValueError(f"Run '{result.run_id}' does not exist in store")

    # If finding_id is specified, verify it exists and belongs to this run
    if finding_id:
        finding = store.get_finding(finding_id)
        if not finding:
            raise ValueError(f"Finding '{finding_id}' does not exist in store")
        if finding.run_id and finding.run_id != result.run_id:
            raise ValueError(
                f"Finding '{finding_id}' belongs to run '{finding.run_id}', which does not match result run '{result.run_id}'"
            )

    # Record event
    if result.status == ExecutionStatus.SUCCEEDED:
        event_type = EventType.SANDBOX_EXECUTION_COMPLETED
    elif result.status == ExecutionStatus.UNAVAILABLE:
        event_type = EventType.SANDBOX_UNAVAILABLE
    else:
        event_type = EventType.SANDBOX_EXECUTION_FAILED

    store.append_event(result.run_id, event_type, event_payload)


    # Attach evidence only if an actual isolated execution occurred
    if attach_as_evidence and result.isolation_established:
        ev_type = EvidenceType.TEST_RESULT if result.status == ExecutionStatus.SUCCEEDED else EvidenceType.COMMAND_RESULT
        desc = (
            f"Sandbox execution of command {result.runtime_metadata.get('command')} "
            f"[Status: {result.status.value}, Exit code: {result.exit_code}]"
        )
        content = (
            f"=== STDOUT ===\n{result.stdout}\n"
            f"=== STDERR ===\n{result.stderr}\n"
        )
        details = {
            "request_id": result.request_id,
            "status": result.status.value,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "isolation_established": result.isolation_established,
            "failure_classification": result.failure_classification.value,
        }

        evidence = create_evidence(
            run_id=result.run_id,
            evidence_type=ev_type,
            description=desc,
            content=content,
            finding_id=finding_id,
            details=details,
            provenance="forge:sandbox",
        )
        store.attach_evidence(evidence)
        return evidence

    return None
