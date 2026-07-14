"""Async-job lifecycle endpoints (list / poll / cancel / result / detail).

Submission endpoints live in :mod:`api.routers.jobs_submit`; shared helpers in
:mod:`api.routers.jobs_common`.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from api.schemas.jobs import UploadToTargetRequest
from api.routers.jobs_common import (
    _resolve_import_target_url,
    _resolve_target_url,
    _service,
    effective_target_id,
)
from api.services.jobs import (
    JobNotComplete,
    JobNotFound,
    JobResultMissing,
    JobStoreUnavailable,
)
from utils import audit
import os

router = APIRouter()
logger = logging.getLogger("medanon")

# Serve S3-backed results by 307-redirecting to a presigned URL instead of
# proxying the bytes. OFF by default: the presigned URL is signed for the
# internal MINIO_ENDPOINT, which a browser cannot resolve, and is plain http://
# (blocked as mixed content from an HTTPS page). Enable only when the object
# store is directly reachable by the client over the same scheme.
_S3_PRESIGNED_REDIRECT: bool = os.environ.get(
    "MEDANON_S3_PRESIGNED_REDIRECT", "false"
).strip().lower() in ("1", "true", "yes")


@router.get("/jobs")
async def list_jobs(
    status: str | None = Query(
        None, description="Filter by status: pending, running, done, error, dead"
    ),
    type: str | None = Query(
        None, description="Filter by job type: bulk-export, cohort"
    ),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """List all jobs with optional status/type filtering and pagination."""
    try:
        return await asyncio.to_thread(
            _service.list_jobs,
            status=status,
            job_type=type,
            limit=limit,
            offset=offset,
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")


@router.get("/jobs/dead")
async def list_dead_jobs(limit: int = Query(50, ge=1, le=200)):
    """List jobs in the DLQ  those that exceeded ``MEDANON_JOB_MAX_RETRIES``.

    Returned shape matches the regular job dict.  Operators typically use this
    to triage upstream failures (gPAS down, malformed config, poisoned input)
    before deciding whether to ``POST /v1/jobs/{job_id}/requeue``.
    """
    try:
        return await asyncio.to_thread(
            _service.list_jobs,
            status="dead",
            job_type=None,
            limit=limit,
            offset=0,
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")


@router.post("/jobs/{job_id}/requeue", status_code=202)
async def requeue_dead_job(job_id: str):
    """Manually rescue a poisoned job from the DLQ.

    Resets ``status`` to ``pending`` and clears the retry counter so the worker
    gives the job a fresh budget.  Use after fixing the upstream cause.

    - 404 if the job does not exist.
    - 409 if the job is not in ``dead`` state.
    """
    try:
        job_dict = await asyncio.to_thread(_service.requeue_dead_job, job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    audit.emit(
        "job.requeue",
        resource_id=job_id,
        resource_type=job_dict.get("type", ""),
        action="requeue",
    )
    return JSONResponse(status_code=202, content=job_dict)


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Return current status and metadata for the given job."""
    try:
        return await asyncio.to_thread(_service.get_status, job_id)
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
        return await asyncio.to_thread(_service.cancel_job, job_id)
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
    - S3-backed results (``MEDANON_RESULT_STORAGE=s3``) are proxied through this
      endpoint as a chunked stream. Set ``MEDANON_S3_PRESIGNED_REDIRECT=true`` to
      307-redirect to a presigned URL instead  only valid when the object store
      is reachable by the client.
    """
    try:
        result_path = await asyncio.to_thread(_service.get_result_path, job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except JobNotComplete as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except JobResultMissing:
        raise HTTPException(status_code=410, detail="Result file not available")

    # Derive media type + download filename from the stored result's extension.
    # Tabular-batch and sql-export jobs write a ZIP (one member per file);
    # everything else streams NDJSON. Sending the correct Content-Type avoids a
    # mislabelled ZIP download.
    if result_path.lower().endswith(".zip"):
        media_type = "application/zip"
        download_name = f"job_{job_id}.zip"
    else:
        media_type = "application/x-ndjson"
        download_name = f"job_{job_id}.ndjson"

    if result_path.startswith("s3://"):
        from integrations.storage import get_result_storage

        storage = get_result_storage()

        # Presigned-redirect mode is OPT-IN. A presigned URL is signed for the
        # *internal* MINIO_ENDPOINT (e.g. http://minio:9000), which a browser
        # can neither resolve nor load from an HTTPS page (mixed content). Only
        # enable this where the object store is reachable by the client.
        if _S3_PRESIGNED_REDIRECT:
            url = storage.get_download_url(result_path)
            if url:
                return RedirectResponse(url=url, status_code=307)

        # Default: proxy the bytes through the API. Chunked, so a multi-GB
        # result never lands in this process's memory, and MinIO credentials
        # are never exposed to the client.
        return StreamingResponse(
            storage.iter_bytes(result_path),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )

    return FileResponse(
        result_path,
        media_type=media_type,
        filename=download_name,
    )


@router.delete("/jobs/{job_id}/result", status_code=204)
async def delete_job_result(job_id: str):
    """Delete the NDJSON result file for a completed job.

    - 404 if the job does not exist.
    - 409 if the job is not yet ``done``.
    - 410 if the result file has already been removed.
    """
    try:
        deleted = await asyncio.to_thread(_service.delete_result, job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except JobNotComplete as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except JobResultMissing:
        raise HTTPException(status_code=410, detail="Result file not available")
    if not deleted:
        raise HTTPException(status_code=410, detail="Result file already removed")
    return Response(status_code=204)


@router.post("/jobs/{job_id}/reprocess", status_code=202)
async def reprocess_job(job_id: str, config_profile: str = Query("auto")):
    """Queue a re-processing job that replays staged rows with a (new) config profile.

    Requires staging to be configured (``MEDANON_STAGING_DB_URL``).
    Returns 404 if the source job does not exist.
    Returns 503 if the job store is not initialised.
    """
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_reprocess, job_id, config_profile=config_profile
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/{job_id}/upload-to-target")
async def upload_job_to_target(job_id: str, req: UploadToTargetRequest | None = None):
    """Upload a completed job's de-identified resources to the target FHIR server.

    Uses idempotent PUT so repeated calls are safe (no duplicates).
    Reads the NDJSON result server-side  no client round-trip for large files.

    - Falls back to ``FHIR_TARGET_URL`` env when ``target_url`` is not provided.
    - 404 if the job does not exist.
    - 409 if the job is not yet ``done``.
    - 410 if the result file has been cleaned up.
    - 400 if no target URL is available.
    """
    if req is None:
        req = UploadToTargetRequest()
    target_id = effective_target_id(req.target_id, req.target_url)
    if target_id:
        # Saved target server: URL from the store, token resolved server-side.
        from pipeline.jobs.source_resolver import resolve_target_token

        resolved_url = await _resolve_target_url(target_id, req.target_url)
        resolved_token = resolve_target_token({"target_id": target_id})
    else:
        resolved_url = await _resolve_import_target_url(req.target_url)
        resolved_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
    if not resolved_url:
        raise HTTPException(
            status_code=400,
            detail="No target URL provided and FHIR_TARGET_URL env var is not set",
        )
    try:
        result = await asyncio.to_thread(
            _service.upload_job_to_target,
            job_id,
            resolved_url,
            target_token=resolved_token,
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except JobNotComplete as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except JobResultMissing:
        raise HTTPException(status_code=410, detail="Result file not available")
    return result


@router.get("/jobs/{job_id}/staged-stats")
async def get_staged_stats(job_id: str):
    """Return staging row counts for a bulk-export or cohort job.

    Returns ``{pending, done, error, total}`` when staging is configured,
    or ``{"staging": "unavailable"}`` when ``MEDANON_STAGING_DB_URL`` is not set.
    """
    try:
        return await asyncio.to_thread(_service.get_staged_stats, job_id)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")


# ---------------------------------------------------------------------------
# Job detail cache (parsed result: resource counts + field/PII analysis)
# ---------------------------------------------------------------------------


class _JobDetailBody(BaseModel):
    resource_counts: dict = {}
    total_resources: int = 0
    pii_data: dict = {}
    field_summary: dict = {}


def _get_detail_store():
    from pipeline.job_detail import get_job_detail_store

    store = get_job_detail_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Job detail store not initialised")
    return store


@router.get("/jobs/{job_id}/detail")
async def get_job_detail(job_id: str):
    """Return cached parsed-result detail for a completed job.

    Returns 404 when no detail has been saved yet  the client should then
    download and parse the NDJSON result, then POST the parsed data back.
    """
    store = _get_detail_store()
    detail = await asyncio.to_thread(store.get, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No cached detail for this job")
    return detail


@router.post("/jobs/{job_id}/detail", status_code=204, response_class=Response)
async def save_job_detail(job_id: str, body: _JobDetailBody):
    """Save parsed-result detail for a completed job.

    Called by the frontend after parsing the NDJSON output so subsequent
    selections of this job load instantly without re-downloading the file.
    Upserts  safe to call multiple times.
    """
    store = _get_detail_store()
    await asyncio.to_thread(store.set, job_id, body.model_dump())
