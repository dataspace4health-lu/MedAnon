"""Pydantic request/response models for the workflow (DAG) API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkflowStepRequest(BaseModel):
    id: str = Field(..., description="Unique step id within the workflow")
    job_type: str = Field(..., description="Existing job/executor type")
    params: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class WorkflowSubmitRequest(BaseModel):
    name: str = Field(..., description="Human-readable workflow name")
    steps: list[WorkflowStepRequest] = Field(
        ..., min_length=1, description="DAG steps"
    )
    params: dict = Field(default_factory=dict)


class TemplateWorkflowRequest(BaseModel):
    """Build a workflow from a named template (bulk-export | cohort)."""

    template: str = Field(..., description="Template name")
    name: str = Field("", description="Optional workflow name override")
    params: dict = Field(default_factory=dict)


class WorkflowStepResponse(BaseModel):
    id: str
    job_type: str
    depends_on: list[str]
    status: str
    job_id: str | None = None
    error: str | None = None


class WorkflowResponse(BaseModel):
    id: str
    name: str
    status: str
    backend: str
    steps: list[WorkflowStepResponse]
    created_at: str
    updated_at: str


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowResponse]
