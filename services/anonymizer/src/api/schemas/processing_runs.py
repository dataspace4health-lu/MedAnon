"""Pydantic models for processing run history endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ProcessingRunResponse(BaseModel):
    id: str
    # TIMESTAMPTZ on PostgreSQL, ISO-8601 string on SQLite; pydantic accepts both
    # and serialises to ISO-8601 either way.
    created_at: datetime
    endpoint: str
    config_profile: str
    config_hash: str | None = None
    resource_count: int
    error_count: int
    duration_ms: int
    input_type: str
    summary: dict | None = None
    score: dict | None = None
    trust_passport: dict | None = None


class ProcessingRunListResponse(BaseModel):
    runs: list[ProcessingRunResponse]
    total: int


class ProcessingRunStatsResponse(BaseModel):
    total_runs: int
    scored_runs: int = 0
    blocked_runs: int = 0
    avg_composite: float | None = None
    avg_privacy: float | None = None
    avg_utility: float | None = None
    avg_quality: float | None = None
    total_resources: int
    runs_by_endpoint: dict[str, int]
    runs_by_profile: dict[str, int]
