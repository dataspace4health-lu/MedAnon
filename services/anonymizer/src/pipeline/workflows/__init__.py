"""Workflow (DAG) orchestration over the existing job system.

Public surface:
    ``WorkflowEngine``         — schedules DAG steps as ordinary Jobs.
    ``init_workflow_engine``   — module-level singleton setter.
    ``get_workflow_engine``    — singleton accessor (None when not configured).
"""

from pipeline.workflows.engine import (  # noqa: F401
    WorkflowEngine,
    WORKFLOW_PARAM_KEY,
    get_workflow_engine,
    init_workflow_engine,
)
