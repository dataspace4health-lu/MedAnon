"""Pydantic models for the BFF (Backend-For-Frontend) dashboard endpoint."""

from __future__ import annotations

from pydantic import BaseModel

from api.schemas.processing_runs import ProcessingRunStatsResponse


class HealthSnapshot(BaseModel):
    status: str
    version: str | None = None


class ReadinessSnapshot(BaseModel):
    status: str
    checks: dict[str, str] = {}


class JobSummary(BaseModel):
    id: str
    type: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class DashboardSummaryResponse(BaseModel):
    """Aggregated snapshot powering the UI dashboard.

    Combines four upstream calls (health, readiness, recent jobs, processing-run
    stats) into a single round-trip so the SPA renders without a request waterfall.
    Each section is independently optional — a downstream failure degrades to
    ``null`` rather than failing the whole response.
    """

    health: HealthSnapshot
    ready: ReadinessSnapshot
    recent_jobs: list[JobSummary]
    processing_runs: ProcessingRunStatsResponse | None = None
    generated_at: str
