"""Workflow service  thin layer over the WorkflowEngine + workflow store."""

from __future__ import annotations

import logging

from domain.workflows import Workflow, WorkflowStep

logger = logging.getLogger("medanon")

# Step job_types that require admin (they read source FHIR / write to target).
_ADMIN_JOB_TYPES = frozenset({"bulk-export", "cohort", "bulk-import"})


class WorkflowUnavailable(Exception):
    """Raised when workflows are not configured (no app-db / engine)."""


class WorkflowNotFound(Exception):
    """Raised when a workflow id does not exist."""


class WorkflowService:
    """Coordinates workflow submission, status, and cancellation."""

    def _engine(self):
        from pipeline.workflows import get_workflow_engine

        engine = get_workflow_engine()
        if engine is None:
            raise WorkflowUnavailable(
                "Workflows require a PostgreSQL app database "
                "(set MEDANON_APP_DB_URL) and MEDANON_WORKFLOWS_ENABLED=true."
            )
        return engine

    def required_role(self, step_job_types: list[str]) -> str:
        """Highest role required across a workflow's step types."""
        if any(t in _ADMIN_JOB_TYPES for t in step_job_types):
            return "admin"
        return "analyst"

    def submit(self, name: str, steps: list[dict], params: dict) -> Workflow:
        engine = self._engine()
        wf_steps = [
            WorkflowStep(
                id=s["id"],
                job_type=s["job_type"],
                params=s.get("params", {}),
                depends_on=s.get("depends_on", []),
            )
            for s in steps
        ]
        return engine.submit(name, wf_steps, params)

    def submit_template(self, template: str, name: str, params: dict) -> Workflow:
        from pipeline.workflows.templates import TEMPLATES

        builder = TEMPLATES.get(template)
        if builder is None:
            raise ValueError(
                f"unknown template {template!r}; valid: {sorted(TEMPLATES)}"
            )
        engine = self._engine()
        steps = builder(params)
        return engine.submit(name or f"{template}-workflow", steps, params)

    def get(self, workflow_id: str) -> Workflow:
        engine = self._engine()
        wf = engine._wstore.get(workflow_id)
        if wf is None:
            raise WorkflowNotFound(workflow_id)
        return wf

    def list(self, status: str | None, limit: int, offset: int) -> list[Workflow]:
        engine = self._engine()
        return engine._wstore.list_workflows(status=status, limit=limit, offset=offset)

    def cancel(self, workflow_id: str) -> bool:
        engine = self._engine()
        if engine._wstore.get(workflow_id) is None:
            raise WorkflowNotFound(workflow_id)
        return engine.cancel(workflow_id)


def to_response(workflow: Workflow) -> dict:
    """Serialize a Workflow domain object to the API response shape."""
    return {
        "id": workflow.id,
        "name": workflow.name,
        "status": workflow.status.value,
        "backend": workflow.backend,
        "steps": [
            {
                "id": s.id,
                "job_type": s.job_type,
                "depends_on": s.depends_on,
                "status": s.status.value,
                "job_id": s.job_id,
                "error": s.error,
            }
            for s in workflow.steps
        ],
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }
