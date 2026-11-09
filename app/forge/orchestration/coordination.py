"""Phase 13 M2: Task Coordination and Dependency Graph Management.

Manages DAG-based task dependencies, topological ordering, cycle detection,
dependency failure propagation, duplicate task prevention, and safe scheduling
of independent engineering subtasks.
"""

from collections import defaultdict, deque
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field

from forge.orchestration.persistence import OrchestrationStateStore
from forge.orchestration.retry import FailureCategory
from forge.orchestration.schemas import WorkflowStage


class TaskState(str, Enum):
    """Lifecycle states of a coordinated task."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


class DependencyCycleError(Exception):
    """Raised when a cyclic dependency is detected in the task DAG."""


class TaskSchedulingError(Exception):
    """Raised when an invalid task operation or state transition is attempted."""


TaskDependencyError = TaskSchedulingError


class TaskRecord(BaseModel):
    """Durable model representing a coordinated task in a workflow."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    workflow_id: str
    name: str
    stage: WorkflowStage
    state: TaskState = TaskState.PENDING
    priority: int = 0
    idempotency_key: str
    dependencies: List[str] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    failure_category: Optional[FailureCategory] = None
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    @property
    def prerequisite_task_ids(self) -> List[str]:
        return self.dependencies


class TaskCoordinator:
    """Coordinates DAG execution, dependency resolution, and task lifecycles."""

    def __init__(
        self,
        workflow_id: Optional[str] = None,
        store: Optional[OrchestrationStateStore] = None,
        state_store: Optional[OrchestrationStateStore] = None,
        max_concurrent_tasks: int = 4,
    ) -> None:
        self.workflow_id = workflow_id or "default"
        self.store = store or state_store
        self.max_concurrent_tasks = max(1, min(max_concurrent_tasks, 16))
        self._lock = threading.Lock()
        self._tasks: Dict[str, TaskRecord] = {}

        # Load persisted tasks if store and workflow_id are available
        if self.store and workflow_id:
            persisted = self.store.get_tasks(workflow_id)
            for t_dict in persisted:
                task = self._dict_to_task(t_dict)
                self._tasks[task.task_id] = task

    def _dict_to_task(self, t_dict: Dict[str, Any]) -> TaskRecord:
        return TaskRecord(
            task_id=t_dict["task_id"],
            workflow_id=t_dict["workflow_id"],
            name=t_dict["name"],
            stage=WorkflowStage(t_dict["stage"]),
            state=TaskState(t_dict["state"]),
            priority=t_dict.get("priority", 0),
            idempotency_key=t_dict["idempotency_key"],
            dependencies=t_dict.get("dependencies", []),
            retry_count=t_dict.get("retry_count", 0),
            max_retries=t_dict.get("max_retries", 3),
            failure_category=FailureCategory(t_dict["failure_category"]) if t_dict.get("failure_category") else None,
            error_message=t_dict.get("error_message"),
            result=t_dict.get("result"),
            created_at=t_dict["created_at"],
            started_at=t_dict.get("started_at"),
            completed_at=t_dict.get("completed_at"),
        )

    def create_task(
        self,
        workflow_id: str,
        task_id: str,
        stage: WorkflowStage,
        name: Optional[str] = None,
        priority: int = 0,
        prerequisite_task_ids: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
        max_retries: int = 3,
    ) -> TaskRecord:
        """Register a new task under a specific workflow."""
        import hashlib
        idemp = idempotency_key or hashlib.sha256(f"{workflow_id}:{task_id}:{stage.value}".encode()).hexdigest()
        self.workflow_id = workflow_id
        return self.register_task(
            task_id=task_id,
            name=name or task_id,
            stage=stage,
            idempotency_key=idemp,
            dependencies=prerequisite_task_ids,
            priority=priority,
            max_retries=max_retries,
        )

    def register_task(
        self,
        task_id: str,
        name: str,
        stage: WorkflowStage,
        idempotency_key: str,
        dependencies: Optional[List[str]] = None,
        priority: int = 0,
        max_retries: int = 3,
    ) -> TaskRecord:
        """Register a new task, preventing duplicates and cyclic dependencies."""
        with self._lock:
            # Duplicate check by task_id or idempotency_key
            for existing in self._tasks.values():
                if existing.task_id == task_id or existing.idempotency_key == idempotency_key:
                    return existing

            deps = dependencies or []

            # Check for non-existent dependencies
            for dep in deps:
                if dep not in self._tasks:
                    raise TaskSchedulingError(
                        f"Cannot register task '{task_id}': dependency '{dep}' is not registered."
                    )

            # Cycle detection: simulate adding edge
            self._validate_no_cycles(candidate_task_id=task_id, candidate_deps=deps)

            initial_state = TaskState.READY if not deps else TaskState.PENDING
            task = TaskRecord(
                task_id=task_id,
                workflow_id=self.workflow_id,
                name=name,
                stage=stage,
                state=initial_state,
                priority=priority,
                idempotency_key=idempotency_key,
                dependencies=deps,
                max_retries=max_retries,
            )
            self._tasks[task_id] = task

            if self.store:
                self.store.save_task(task.model_dump())

            return task

    def _validate_no_cycles(self, candidate_task_id: str, candidate_deps: List[str]) -> None:
        """Run topological cycle detection simulating the proposed task and dependencies."""
        graph: Dict[str, List[str]] = defaultdict(list)
        all_nodes: Set[str] = set(self._tasks.keys())
        all_nodes.add(candidate_task_id)

        for tid, t in self._tasks.items():
            for d in t.dependencies:
                graph[d].append(tid)

        for d in candidate_deps:
            graph[d].append(candidate_task_id)

        # Kahn's algorithm
        in_degree: Dict[str, int] = {node: 0 for node in all_nodes}
        for u in graph:
            for v in graph[u]:
                in_degree[v] += 1

        queue = deque([node for node in all_nodes if in_degree[node] == 0])
        visited_count = 0

        while queue:
            u = queue.popleft()
            visited_count += 1
            for v in graph[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        if visited_count != len(all_nodes):
            raise DependencyCycleError(
                f"Cyclic dependency detected: task '{candidate_task_id}' forms a directed cycle with dependencies {candidate_deps}."
            )

    def get_ready_tasks(self, workflow_id: Optional[str] = None) -> List[TaskRecord]:
        """Return tasks whose dependencies are completely satisfied, sorted by priority."""
        with self._lock:
            target_wf = workflow_id or self.workflow_id
            if self.store and workflow_id and workflow_id != self.workflow_id:
                persisted = self.store.get_tasks(workflow_id)
                for t_dict in persisted:
                    if t_dict["task_id"] not in self._tasks:
                        task = self._dict_to_task(t_dict)
                        self._tasks[task.task_id] = task

            ready: List[TaskRecord] = []
            for t in self._tasks.values():
                if target_wf and t.workflow_id != target_wf:
                    continue
                if t.state == TaskState.PENDING:
                    # Check dependencies
                    all_deps_completed = all(
                        self._tasks[dep].state == TaskState.COMPLETED
                        for dep in t.dependencies
                        if dep in self._tasks
                    )
                    any_dep_failed_or_blocked = any(
                        self._tasks[dep].state in (TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED)
                        for dep in t.dependencies
                        if dep in self._tasks
                    )

                    if any_dep_failed_or_blocked:
                        t.state = TaskState.BLOCKED
                        t.error_message = "Dependency failed or was cancelled."
                        if self.store:
                            self.store.save_task(t.model_dump())
                    elif all_deps_completed:
                        t.state = TaskState.READY
                        if self.store:
                            self.store.save_task(t.model_dump())
                        ready.append(t)
                elif t.state == TaskState.READY:
                    ready.append(t)

            # Sort by priority descending, then created_at ascending
            ready.sort(key=lambda x: (-x.priority, x.created_at))
            return ready

    def start_task(self, task_id: str) -> TaskRecord:
        """Transition task to RUNNING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise TaskSchedulingError(f"Task '{task_id}' not found.")
            if task.state != TaskState.READY:
                raise TaskSchedulingError(
                    f"Cannot start task '{task_id}': state is '{task.state.value}', expected 'READY'."
                )

            # Check concurrency limit
            running_count = sum(1 for t in self._tasks.values() if t.state == TaskState.RUNNING)
            if running_count >= self.max_concurrent_tasks:
                raise TaskSchedulingError(
                    f"Maximum concurrent task limit ({self.max_concurrent_tasks}) reached."
                )

            task.state = TaskState.RUNNING
            task.started_at = datetime.now(timezone.utc).isoformat()
            if self.store:
                self.store.save_task(task.model_dump())
            return task

    def complete_task(self, task_id: str, result: Optional[Dict[str, Any]] = None) -> TaskRecord:
        """Mark task as COMPLETED and update dependent tasks."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise TaskSchedulingError(f"Task '{task_id}' not found.")
            if task.state != TaskState.RUNNING:
                raise TaskSchedulingError(
                    f"Cannot complete task '{task_id}': state is '{task.state.value}', expected 'RUNNING'."
                )

            task.state = TaskState.COMPLETED
            task.completed_at = datetime.now(timezone.utc).isoformat()
            task.result = result
            if self.store:
                self.store.save_task(task.model_dump())

            # Evaluate dependents
            self._update_dependent_states()
            return task

    def fail_task(
        self,
        task_id: str,
        error_message: str,
        category: FailureCategory = FailureCategory.PERMANENT_EXECUTION_FAILURE,
    ) -> TaskRecord:
        """Mark task as FAILED and propagate failure to downstream dependents."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise TaskSchedulingError(f"Task '{task_id}' not found.")

            task.state = TaskState.FAILED
            task.completed_at = datetime.now(timezone.utc).isoformat()
            task.error_message = error_message
            task.failure_category = category
            if self.store:
                self.store.save_task(task.model_dump())

            # Propagate failure to all downstream tasks
            self._propagate_block(failed_task_id=task_id)
            return task

    def cancel_pending_tasks(self, reason: str = "Workflow cancelled") -> List[TaskRecord]:
        """Cancel all pending, ready, or blocked tasks in the workflow."""
        with self._lock:
            cancelled: List[TaskRecord] = []
            now_str = datetime.now(timezone.utc).isoformat()
            for task in self._tasks.values():
                if task.state in (TaskState.PENDING, TaskState.READY, TaskState.BLOCKED):
                    task.state = TaskState.CANCELLED
                    task.completed_at = now_str
                    task.error_message = reason
                    if self.store:
                        self.store.save_task(task.model_dump())
                    cancelled.append(task)
            return cancelled

    def _propagate_block(self, failed_task_id: str) -> None:
        """Recursively transition downstream dependent tasks to BLOCKED."""
        to_block = deque([failed_task_id])
        now_str = datetime.now(timezone.utc).isoformat()

        while to_block:
            current_id = to_block.popleft()
            for task in self._tasks.values():
                if current_id in task.dependencies and task.state in (TaskState.PENDING, TaskState.READY):
                    task.state = TaskState.BLOCKED
                    task.completed_at = now_str
                    task.error_message = f"Prerequisite task '{current_id}' failed or was blocked."
                    if self.store:
                        self.store.save_task(task.model_dump())
                    to_block.append(task.task_id)

    def _update_dependent_states(self) -> None:
        """Update PENDING tasks whose dependencies have completed to READY."""
        for task in self._tasks.values():
            if task.state == TaskState.PENDING:
                all_done = all(
                    self._tasks[d].state == TaskState.COMPLETED
                    for d in task.dependencies
                    if d in self._tasks
                )
                if all_done:
                    task.state = TaskState.READY
                    if self.store:
                        self.store.save_task(task.model_dump())

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Fetch task record by ID."""
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, workflow_id: Optional[str] = None) -> List[TaskRecord]:
        """Return list of all tasks ordered by priority and created_at."""
        with self._lock:
            target_wf = workflow_id or self.workflow_id
            if self.store and workflow_id:
                persisted = self.store.get_tasks(workflow_id)
                tasks = [self._dict_to_task(t) for t in persisted]
                tasks.sort(key=lambda x: (-x.priority, x.created_at))
                return tasks

            tasks = [t for t in self._tasks.values() if not target_wf or t.workflow_id == target_wf]
            tasks.sort(key=lambda x: (-x.priority, x.created_at))
            return tasks
