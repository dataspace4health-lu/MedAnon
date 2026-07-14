"""Workflow (DAG) orchestration endpoints.

POST   /workflows             submit a DAG of steps, returns 202
POST   /workflows/template    submit from a named template (bulk-export | cohort)
GET    /workflows             list workflows (optional ?status=)
GET    /workflows/{id}        workflow + per-step status
DELETE /workflows/{id}        cancel a workflow

RBAC: the auth middleware maps /v1/workflows → analyst. Steps whose job_type
reads source FHIR or writes a target (bulk-export/cohort/bulk-import) require
admin  enforced per-request here since it depends on the submitted graph.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request

from api.auth import AuthContext
from api.deps import limiter
from api.schemas.workflows import (
    TemplateWorkflowRequest,
    WorkflowListResponse,
    WorkflowResponse,
    WorkflowSubmitRequest,
)
from api.services.workflows import (
    WorkflowNotFound,
    WorkflowService,
    WorkflowUnavailable,
    to_response,
)

router = APIRouter()
logger = logging.getLogger("medanon")

_service = WorkflowService()
_RATE_JOBS_SUBMIT = os.environ.get("MEDANON_RATE_JOBS_SUBMIT", "30/minute")


def _enforce_step_roles(request: Request, step_job_types: list[str]) -> None:
    required = _service.required_role(step_job_types)
    if required == "admin":
        auth: AuthContext | None = getattr(request.state, "auth", None)
        if auth is None or not auth.has_role("admin"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Admin role required: this workflow contains steps that "
                    "read source FHIR or write to a target server."
                ),
            )


def _unavailable(exc: WorkflowUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/workflows", status_code=202, response_model=WorkflowResponse)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_workflow(request: Request, body: WorkflowSubmitRequest):
    _enforce_step_roles(request, [s.job_type for s in body.steps])
    try:
        wf = _service.submit(
            body.name,
            [s.model_dump() for s in body.steps],
            body.params,
        )
    except WorkflowUnavailable as exc:
        raise _unavailable(exc)
    except ValueError as exc:  # DAG validation
        raise HTTPException(status_code=422, detail=str(exc))
    return to_response(wf)


@router.post("/workflows/template", status_code=202, response_model=WorkflowResponse)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_template_workflow(request: Request, body: TemplateWorkflowRequest):
    from pipeline.workflows.templates import TEMPLATES

    builder = TEMPLATES.get(body.template)
    if builder is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown template {body.template!r}; valid: {sorted(TEMPLATES)}",
        )
    # Determine roles from the template's resulting steps.
    step_types = [s.job_type for s in builder(body.params)]
    _enforce_step_roles(request, step_types)
    try:
        wf = _service.submit_template(body.template, body.name, body.params)
    except WorkflowUnavailable as exc:
        raise _unavailable(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return to_response(wf)


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        workflows = _service.list(status, limit, offset)
    except WorkflowUnavailable as exc:
        raise _unavailable(exc)
    return {"workflows": [to_response(w) for w in workflows]}


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(request: Request, workflow_id: str):
    try:
        wf = _service.get(workflow_id)
    except WorkflowUnavailable as exc:
        raise _unavailable(exc)
    except WorkflowNotFound:
        raise HTTPException(status_code=404, detail="workflow not found")
    return to_response(wf)


@router.delete("/workflows/{workflow_id}", status_code=202)
async def cancel_workflow(request: Request, workflow_id: str):
    try:
        _service.cancel(workflow_id)
    except WorkflowUnavailable as exc:
        raise _unavailable(exc)
    except WorkflowNotFound:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"status": "cancelling", "workflow_id": workflow_id}
