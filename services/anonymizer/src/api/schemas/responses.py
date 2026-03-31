"""Response models for structured API responses."""

from __future__ import annotations

from pydantic import BaseModel


class UploadSummary(BaseModel):
    """Response for POST /process/and-upload."""

    uploaded: int
    errors: int
    results: list[dict]


class JobResponse(BaseModel):
    """Response for job creation and status queries."""

    job_id: str
    type: str
    status: str
    created_at: str
    updated_at: str
    result_path: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """Response for GET /ready."""

    ready: bool
    checks: dict[str, str] | None = None
