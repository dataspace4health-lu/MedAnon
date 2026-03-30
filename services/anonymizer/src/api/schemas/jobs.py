"""Request schemas for the async job queue endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BulkExportJobRequest(BaseModel):
    """Request body for POST /v1/jobs/bulk-export."""

    server_url: str | None = None
    level: str = "system"
    resource_type: str | None = None
    type_filter: str | None = None
    since: str | None = None
    token: str | None = None
    timeout: float = 30.0
    config_profile: str = "auto"


class CohortJobRequest(BaseModel):
    """Request body for POST /v1/jobs/cohort."""

    server_url: str | None = None
    search_type: str
    search_params: dict[str, Any] = Field(default_factory=dict)
    everything_params: dict[str, Any] = Field(default_factory=dict)
    token: str | None = None
    timeout: float = 30.0
    config_profile: str = "auto"
