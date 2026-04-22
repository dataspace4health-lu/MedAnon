"""Processing run history endpoints.

GET    /v1/processing-runs             — list runs (paged, filterable)
GET    /v1/processing-runs/stats       — aggregate dashboard statistics
GET    /v1/processing-runs/{run_id}    — single run detail
DELETE /v1/processing-runs             — purge runs older than N days (admin)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from api.auth import AuthContext
from api.schemas.processing_runs import (
    ProcessingRunListResponse,
    ProcessingRunResponse,
    ProcessingRunStatsResponse,
)

router = APIRouter(prefix="/processing-runs", tags=["Processing Runs"])
logger = logging.getLogger("medanon")


def _require_admin(request: Request) -> None:
    """Raise 403 if the caller does not have admin role."""
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(status_code=403, detail="admin role required")


def _get_store():
    from pipeline.processing_run import get_processing_run_store

    store = get_processing_run_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Processing run store not available (requires PostgreSQL app-db)",
        )
    return store


@router.get("", response_model=ProcessingRunListResponse)
async def list_processing_runs(
    endpoint: str | None = Query(None, description="Filter by endpoint path"),
    config_profile: str | None = Query(None, description="Filter by config profile"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List recent processing runs with their scoring results."""
    store = _get_store()
    runs, total = await asyncio.to_thread(
        store.list_runs,
        endpoint=endpoint,
        config_profile=config_profile,
        limit=limit,
        offset=offset,
    )
    return {"runs": runs, "total": total}


@router.get("/stats", response_model=ProcessingRunStatsResponse)
async def processing_run_stats():
    """Aggregate statistics across all processing runs."""
    store = _get_store()
    stats = await asyncio.to_thread(store.get_stats)
    return stats


@router.get("/{run_id}", response_model=ProcessingRunResponse)
async def get_processing_run(run_id: str):
    """Get details of a single processing run."""
    store = _get_store()
    run = await asyncio.to_thread(store.get, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Processing run not found")
    return run


@router.delete("")
async def purge_processing_runs(
    request: Request,
    days: int = Query(30, ge=1, description="Delete runs older than this many days"),
):
    """Purge processing runs older than the specified number of days. Requires admin role."""
    _require_admin(request)
    store = _get_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    deleted = await asyncio.to_thread(store.delete_before, cutoff)
    return {"deleted": deleted, "cutoff": cutoff}
