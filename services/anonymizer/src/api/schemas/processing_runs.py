"""Pydantic models for processing run history endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ProcessingRunResponse(BaseModel):
    id: str
    created_at: str
    endpoint: str
    config_profile: str
    resource_count: int
    error_count: int
    duration_ms: int
    input_type: str
    summary: dict | None = None
    score: dict | None = None


class ProcessingRunListResponse(BaseModel):
    runs: list[ProcessingRunResponse]
    total: int


class ProcessingRunStatsResponse(BaseModel):
    total_runs: int
    avg_composite: float
    total_resources: int
    runs_by_endpoint: dict[str, int]
    runs_by_profile: dict[str, int]
