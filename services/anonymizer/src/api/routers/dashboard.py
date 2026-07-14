"""Backend-For-Frontend dashboard endpoint.

Aggregates several existing reads (health, readiness, recent jobs, processing
run stats) into a single response so the React SPA dashboard renders without a
request waterfall.

Each section is fetched in parallel and degrades to ``null`` (or empty list) on
upstream failure  partial results are preferable to a fully failing dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter

from api.schemas.dashboard import (
    DashboardSummaryResponse,
    HealthSnapshot,
    JobSummary,
    ReadinessSnapshot,
)
from pipeline.health import HealthCheckService
from api.services.jobs import JobService, JobStoreUnavailable

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])
logger = logging.getLogger("medanon")

_health_service = HealthCheckService()
_job_service = JobService()


#  useDashboardSummary.ts polls this endpoint every 5s. check_readiness's
# default MEDANON_READY_TIMEOUT (5s, +2s grace = 7s worst case) is tuned for
# the infrequent k8s /ready probe, not a 5s-interval BFF section  a single
# down advisory microservice (analytics/scoring/trust_gate/ai) would then
# make every poll take longer than the poll interval itself. Cap it well
# under 5s here so that can't happen.
_DASHBOARD_READINESS_TIMEOUT = 1.5


async def _readiness_section() -> ReadinessSnapshot:
    try:
        checks = await asyncio.to_thread(
            _health_service.check_readiness, _DASHBOARD_READINESS_TIMEOUT
        )
    except Exception:
        logger.debug("dashboard_readiness_failed", exc_info=True)
        return ReadinessSnapshot(status="unknown", checks={})
    all_ok = all(v == "ok" for v in checks.values()) if checks else True
    return ReadinessSnapshot(status="ok" if all_ok else "degraded", checks=checks)


async def _recent_jobs_section(limit: int) -> list[JobSummary]:
    try:
        jobs = await asyncio.to_thread(_job_service.list_jobs, None, None, limit, 0)
    except JobStoreUnavailable:
        return []
    except Exception:
        logger.debug("dashboard_recent_jobs_failed", exc_info=True)
        return []
    return [
        JobSummary(
            id=str(j.get("id", "")),
            type=str(j.get("type", "")),
            status=str(j.get("status", "")),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in jobs
    ]


async def _processing_stats_section():
    """Optional  only populated when the processing-run store is available."""
    try:
        from pipeline.processing_run import get_processing_run_store

        store = get_processing_run_store()
        if store is None:
            return None
        return await asyncio.to_thread(store.get_stats)
    except Exception:
        logger.debug("dashboard_processing_stats_failed", exc_info=True)
        return None


@router.get("/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(limit: int = 5):
    """Return a one-shot dashboard snapshot.

    *limit* caps the number of recent jobs returned (default 5, max 50).
    """
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    ready, recent_jobs, processing_stats = await asyncio.gather(
        _readiness_section(),
        _recent_jobs_section(limit),
        _processing_stats_section(),
    )

    return DashboardSummaryResponse(
        health=HealthSnapshot(
            status="ok",
            version=os.environ.get("MEDANON_VERSION", "dev"),
        ),
        ready=ready,
        recent_jobs=recent_jobs,
        processing_runs=processing_stats,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
