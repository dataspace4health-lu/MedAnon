"""Async job queue endpoints.

    POST   /jobs/bulk-export     — queue a bulk export job, returns 202
    POST   /jobs/cohort          — queue a cohort export job, returns 202
    GET    /jobs                 — list jobs with optional filtering
    GET    /jobs/{job_id}        — poll job status
    DELETE /jobs/{job_id}        — cancel a pending or running job
    GET    /jobs/{job_id}/result — download completed NDJSON result
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from api.deps import _get_url_from_request_or_env
from api.schemas.jobs import BulkExportJobRequest, CohortJobRequest
from api.services.jobs import (
    JobNotComplete,
    JobNotFound,
    JobResultMissing,
    JobService,
    JobStoreUnavailable,
)

router = APIRouter()
logger = logging.getLogger("medanon")

_service = JobService()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs/bulk-export", status_code=202)
async def submit_bulk_export(req: BulkExportJobRequest):
    """Queue a bulk-export + de-identify job. Returns 202 immediately.

    Poll ``GET /v1/jobs/{job_id}`` for status.
    Download via ``GET /v1/jobs/{job_id}/result`` when status is ``done``.
    """
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    try:
        job_dict = _service.submit_bulk_export(server_url, {
            "level": req.level,
            "resource_type": req.resource_type,
            "type_filter": req.type_filter,
            "since": req.since,
            "token": req.token,
            "timeout": req.timeout,
            "config_profile": req.config_profile,
        })
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/cohort", status_code=202)
async def submit_cohort(req: CohortJobRequest):
    """Queue a cohort export + de-identify job. Returns 202 immediately."""
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    try:
        job_dict = _service.submit_cohort(server_url, {
            "search_type": req.search_type,
            "search_params": req.search_params,
            "everything_params": req.everything_params,
            "token": req.token,
            "timeout": req.timeout,
            "config_profile": req.config_profile,
        })
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    return JSONResponse(status_code=202, content=job_dict)


@router.get("/jobs")
async def list_jobs(
    status: str | None = Query(None, description="Filter by status: pending, running, done, error"),
    type: str | None = Query(None, description="Filter by job type: bulk-export, cohort"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """List all jobs with optional status/type filtering and pagination."""
    try:
        return _service.list_jobs(
            status=status, job_type=type, limit=limit, offset=offset
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Return current status and metadata for the given job."""
    try:
        return _service.get_status(job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")


@router.delete("/jobs/{job_id}", status_code=200)
async def cancel_job(job_id: str):
    """Cancel a pending or running job.

    - Returns the updated job dict with ``status=cancelled``.
    - 404 if the job does not exist.
    - Jobs already in ``done`` or ``error`` state are returned as-is (no error).
    """
    try:
        return _service.cancel_job(job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")


@router.get("/jobs/{job_id}/result")
async def get_job_result(job_id: str):
    """Stream the de-identified NDJSON result for a completed job.

    - 404 if the job does not exist.
    - 409 if the job is not yet ``done``.
    - 410 if the result file has been cleaned up.
    """
    try:
        result_path = _service.get_result_path(job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except JobNotComplete as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except JobResultMissing:
        raise HTTPException(status_code=410, detail="Result file not available")
    return FileResponse(
        result_path,
        media_type="application/x-ndjson",
        filename=f"job_{job_id}.ndjson",
    )
