"""Phase 8 Repair Execution Architecture — Workspace Manager.

Coordinates the end-to-end lifecycle of isolated repair scratch workspaces:
provisioning, isolation verification, integrity auditing, execution inside
Docker sandboxes, and guaranteed teardown with persistent audit records.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import uuid

from forge.repair.execution.failures import (
    WorkspaceExecutionError,
    WorkspaceReuseForbiddenError,
)
from forge.repair.execution.models import (
    RepairActor,
    RepairExecutionEvent,
    RepairExecutionState,
    RepairRequest,
    WorkspaceRecord,
)
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.execution.storage import RepairExecutionStore
from forge.repair.execution.workspace import RepairWorkspace
from forge.sandbox.models import ExecutionLimits, SandboxResult


class RepairWorkspaceManager:
    """Orchestrates provisioning, containment validation, sandbox execution, and cleanup of repair workspaces."""

    def __init__(self, store: Optional[RepairExecutionStore] = None, db_path: Optional[Path] = None):
        self.store = store or RepairExecutionStore(db_path=db_path)

    def provision_workspace(
        self,
        request: RepairRequest,
        scratch_root: Optional[Union[str, Path]] = None,
        policy: Optional[RepairExecutionPolicy] = None,
    ) -> RepairWorkspace:
        """Provision a fresh isolated scratch workspace for a repair request and record it in persistent storage."""
        # Check if an active workspace already exists for this repair request
        existing_rec = self.store.get_workspace_record_for_repair(request.repair_id)
        if existing_rec and existing_rec.cleanup_status == "active":
            raise WorkspaceReuseForbiddenError(
                f"Active workspace '{existing_rec.workspace_id}' already exists for repair '{request.repair_id}'"
            )

        workspace = RepairWorkspace(
            target_repo_path=request.target_repo,
            base_commit_sha=request.base_commit_sha,
            scratch_root=scratch_root,
            policy=policy,
        )

        # Prepare workspace (validates git commit, copies files, captures baseline hashes)
        workspace.prepare()

        # Verify physical isolation and check Docker availability
        docker_available = workspace.verify_isolation()

        # Persist workspace record
        now = datetime.now(timezone.utc)
        record = WorkspaceRecord(
            workspace_id=workspace.workspace_id,
            repair_id=request.repair_id,
            target_repo=str(request.target_repo),
            base_commit_sha=request.base_commit_sha,
            workspace_path=str(workspace.workspace_path),
            provisioning_status="provisioned",
            isolation_verified=True,
            cleanup_status="active",
            metadata={
                "baseline_files_count": len(workspace.baseline_hashes),
                "docker_available": docker_available,
            },
            created_at=now,
            updated_at=now,
        )
        self.store.save_workspace_record(record)

        # Record audit event
        self.store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{uuid.uuid4().hex[:12]}",
                repair_id=request.repair_id,
                sequence=1,  # store._append_event_locked auto-assigns next sequence
                from_state=request.state,
                to_state=request.state,
                actor=RepairActor.ORCHESTRATOR,
                payload={
                    "action": "workspace_provisioned",
                    "workspace_id": workspace.workspace_id,
                    "workspace_path": str(workspace.workspace_path),
                    "baseline_files_count": len(workspace.baseline_hashes),
                    "docker_available": docker_available,
                },
                timestamp=now,
            )
        )

        return workspace

    def validate_workspace_integrity(
        self,
        workspace: RepairWorkspace,
        request: RepairRequest,
    ) -> Dict[str, Any]:
        """Validate filesystem integrity against baseline hashes and declared scope, recording audit event."""
        diff = workspace.check_integrity(allowed_files=request.allowed_files)

        now = datetime.now(timezone.utc)
        self.store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{uuid.uuid4().hex[:12]}",
                repair_id=request.repair_id,
                sequence=1,
                from_state=request.state,
                to_state=request.state,
                actor=RepairActor.VALIDATOR,
                payload={
                    "action": "workspace_integrity_verified",
                    "workspace_id": workspace.workspace_id,
                    "modified_count": len(diff["modified"]),
                    "added_count": len(diff["added"]),
                    "deleted_count": len(diff["deleted"]),
                    "modified_files": diff["modified"],
                    "added_files": diff["added"],
                    "deleted_files": diff["deleted"],
                },
                timestamp=now,
            )
        )
        return diff

    def execute_in_workspace(
        self,
        workspace: RepairWorkspace,
        request: RepairRequest,
        command: Union[str, List[str]],
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 30.0,
        read_only: bool = False,
        limits: Optional[ExecutionLimits] = None,
    ) -> SandboxResult:
        """Execute an approved command inside DockerSandbox with fail-closed guarantee and audit trail."""
        result = workspace.run_command_in_sandbox(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            read_only=read_only,
            limits=limits,
            run_id=request.run_id,
        )

        now = datetime.now(timezone.utc)
        cmd_name = command if isinstance(command, str) else command[0]
        self.store.record_event(
            RepairExecutionEvent(
                event_id=f"ree_{uuid.uuid4().hex[:12]}",
                repair_id=request.repair_id,
                sequence=1,
                from_state=request.state,
                to_state=request.state,
                actor=RepairActor.VERIFIER,
                payload={
                    "action": "sandbox_command_executed",
                    "workspace_id": workspace.workspace_id,
                    "command": cmd_name,
                    "status": result.status.value,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                },
                timestamp=now,
            )
        )

        return result

    def cleanup_workspace(
        self,
        workspace: RepairWorkspace,
        repair_id: Optional[str] = None,
    ) -> None:
        """Tear down isolated scratch workspace, updating persistent record and audit trail."""
        workspace.cleanup()

        self.store.update_workspace_record(
            workspace.workspace_id,
            {"cleanup_status": "cleaned"},
        )

        if repair_id:
            now = datetime.now(timezone.utc)
            self.store.record_event(
                RepairExecutionEvent(
                    event_id=f"ree_{uuid.uuid4().hex[:12]}",
                    repair_id=repair_id,
                    sequence=1,
                    from_state=None,
                    to_state=RepairExecutionState.CREATED,  # placeholder
                    actor=RepairActor.ORCHESTRATOR,
                    payload={
                        "action": "workspace_cleaned",
                        "workspace_id": workspace.workspace_id,
                        "workspace_path": str(workspace.workspace_path),
                    },
                    timestamp=now,
                )
            )
