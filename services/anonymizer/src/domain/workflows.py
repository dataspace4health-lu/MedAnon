"""Workflow domain types — a DAG of steps over the existing job system.

A :class:`Workflow` is a directed acyclic graph of :class:`WorkflowStep`\\ s.
Each step maps to an existing job ``type`` (``bulk-export``, ``cohort``,
``bulk-import``, ...) and is executed as an ordinary ``Job`` by the existing
worker — the workflow engine only schedules steps as their dependencies
complete. This keeps the in-process 4-stage micro-pipeline and all executors
untouched; orchestration lives one level up (job → workflow step).

The Postgres workflow store (``integrations.postgres.workflow_store``) is the
durable ledger for workflow/step state, consistent with "Postgres staging is
the batch ledger". These dataclasses are the in-memory representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StepStatus(str, Enum):
    PENDING = "pending"      # not yet scheduled (deps unmet)
    READY = "ready"          # deps met, enqueue in flight
    RUNNING = "running"      # job claimed by a worker
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"      # an upstream dependency failed
    CANCELLED = "cancelled"


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


# Terminal step states — a step in one of these will never run again.
TERMINAL_STEP_STATES = frozenset(
    {StepStatus.DONE, StepStatus.ERROR, StepStatus.SKIPPED, StepStatus.CANCELLED}
)


@dataclass
class WorkflowStep:
    id: str                              # unique within the workflow, e.g. "fetch"
    job_type: str                        # existing executor type
    params: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    job_id: str | None = None
    attempt: int = 0
    error: str | None = None


@dataclass
class Workflow:
    id: str
    name: str
    steps: list[WorkflowStep]
    status: WorkflowStatus = WorkflowStatus.PENDING
    params: dict = field(default_factory=dict)
    backend: str = "internal"            # internal | argo
    external_ref: str | None = None      # e.g. Argo Workflow name
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def step(self, step_id: str) -> WorkflowStep | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


class WorkflowValidationError(ValueError):
    """Raised when a workflow's step graph is not a valid DAG."""


def validate_dag(steps: list[WorkflowStep]) -> None:
    """Validate that *steps* form a DAG: unique ids, known deps, no cycles.

    Uses Kahn's algorithm — raises :class:`WorkflowValidationError` on a
    duplicate/empty step id, a ``depends_on`` referencing an unknown step, or
    any cycle.
    """
    if not steps:
        raise WorkflowValidationError("workflow has no steps")

    ids: set[str] = set()
    for s in steps:
        if not s.id:
            raise WorkflowValidationError("step has empty id")
        if s.id in ids:
            raise WorkflowValidationError(f"duplicate step id: {s.id!r}")
        ids.add(s.id)

    # Validate dependency references and build the in-degree map.
    indegree: dict[str, int] = {s.id: 0 for s in steps}
    adjacency: dict[str, list[str]] = {s.id: [] for s in steps}
    for s in steps:
        for dep in s.depends_on:
            if dep == s.id:
                raise WorkflowValidationError(f"step {s.id!r} depends on itself")
            if dep not in ids:
                raise WorkflowValidationError(
                    f"step {s.id!r} depends on unknown step {dep!r}"
                )
            adjacency[dep].append(s.id)
            indegree[s.id] += 1

    # Kahn topological sort.
    queue = [sid for sid, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if visited != len(steps):
        raise WorkflowValidationError("workflow graph contains a cycle")
