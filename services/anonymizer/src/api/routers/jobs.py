"""Async job queue endpoints.

POST   /jobs/bulk-export          — queue a bulk export job, returns 202
POST   /jobs/cohort               — queue a cohort export job, returns 202
POST   /jobs/patient-export       — queue a patient $everything export job, returns 202
POST   /jobs/batch-patient-export — queue a multi-patient $everything export job, returns 202
POST   /jobs/bulk-import          — upload a completed NDJSON to a target FHIR server (parallel), returns 202
POST   /jobs/risk-driven-export   — k-anonymity guaranteed export (lattice search + suppression), returns 202
GET    /jobs                      — list jobs with optional filtering
GET    /jobs/{job_id}             — poll job status
DELETE /jobs/{job_id}             — cancel a pending or running job
GET    /jobs/{job_id}/result      — download completed NDJSON result
DELETE /jobs/{job_id}/result      — delete a completed NDJSON result file
POST   /jobs/{job_id}/reprocess   — re-process staged rows with a new config profile
POST   /jobs/{job_id}/upload-to-target — upload completed results to target FHIR server
GET    /jobs/{job_id}/staged-stats — staging row counts (pending/done/error/total)
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from api.deps import _get_url_from_request_or_env, _validate_server_url, limiter
from api.schemas.jobs import (
    BatchPatientExportRequest,
    BulkExportJobRequest,
    BulkImportJobRequest,
    CohortJobRequest,
    PatientExportJobRequest,
    RiskDrivenExportJobRequest,
    UploadToTargetRequest,
)
from utils import audit
from utils import idempotency as _idem
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

# Rate limits — overridable via env. Defaults are intentionally conservative
# because each submission can spawn a long-running, resource-heavy job.
_RATE_JOBS_SUBMIT = os.environ.get("MEDANON_RATE_JOBS_SUBMIT", "30/minute")


async def _resolve_optional_target_url(user_url: str | None) -> str | None:
    """Validate and return a caller-supplied target URL, or None.

    Unlike the old behaviour, this helper does NOT fall back to the
    ``FHIR_TARGET_URL`` environment variable.  Export jobs (bulk-export,
    cohort, patient-export) should only upload to a target when the caller
    explicitly requests it — silent auto-injection caused unwanted uploads.

    Use :func:`_resolve_import_target_url` for bulk-import jobs, which do
    require a target and support the env-var fallback.
    """
    if user_url:
        await _validate_server_url(user_url)
        return user_url.rstrip("/")
    return None


async def _resolve_import_target_url(user_url: str | None) -> str | None:
    """Return a validated target URL for bulk-import jobs.

    Validates the caller-supplied URL when provided; falls back to the
    ``FHIR_TARGET_URL`` environment variable when the caller omits it.
    Returns ``None`` when neither is available (the endpoint will 400).
    """
    if user_url:
        await _validate_server_url(user_url)
        return user_url.rstrip("/")
    env_val = os.environ.get("FHIR_TARGET_URL", "").rstrip("/")
    return env_val or None


def _check_idempotency(request: Request, scope: str, body_model: BaseModel):
    """Return (idem_key, body_hash, cached_response_or_None).

    Job-submission endpoints are obvious idempotency targets: a network blip
    after the server enqueued the job would otherwise produce a duplicate
    bulk-export run.  The cached response (the original ``job_dict``) is
    returned unchanged so the client recovers the same ``job_id``.
    """
    try:
        idem_key = _idem.validate_key(request.headers.get("Idempotency-Key"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not idem_key:
        return None, "", None
    body_hash = _idem.hash_body(body_model.model_dump(mode="json", exclude_none=True))
    try:
        cached = _idem.lookup_or_conflict(scope, idem_key, body_hash)
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key reused with a different request body",
        )
    return idem_key, body_hash, cached


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/jobs/bulk-export", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_bulk_export(req: BulkExportJobRequest, request: Request):
    """Queue a bulk-export + de-identify job. Returns 202 immediately.

    Poll ``GET /v1/jobs/{job_id}`` for status.
    Download via ``GET /v1/jobs/{job_id}/result`` when status is ``done``.
    When ``target_url`` is set (or ``FHIR_TARGET_URL`` env var), de-identified
    resources are also uploaded to that server via idempotent PUT.
    """
    idem_key, body_hash, cached = _check_idempotency(request, "/v1/jobs/bulk-export", req)
    if cached is not None:
        return JSONResponse(status_code=cached["status"], content=cached["body"])
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    target_url = await _resolve_optional_target_url(req.target_url)
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_bulk_export,
            server_url,
            {
                "level": req.level,
                "resource_type": req.resource_type,
                "type_filter": req.type_filter,
                "since": req.since,
                "token": req.token,
                "timeout": req.timeout,
                "config_profile": req.config_profile,
                "target_url": target_url,
                "target_token": target_token,
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="bulk-export",
        resource_id=job_dict.get("id", ""),
        action="submit",
    )
    if idem_key:
        _idem.remember("/v1/jobs/bulk-export", idem_key, body_hash, 202, job_dict)
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/cohort", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_cohort(req: CohortJobRequest, request: Request):
    """Queue a cohort export + de-identify job. Returns 202 immediately."""
    idem_key, body_hash, cached = _check_idempotency(request, "/v1/jobs/cohort", req)
    if cached is not None:
        return JSONResponse(status_code=cached["status"], content=cached["body"])
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    target_url = await _resolve_optional_target_url(req.target_url)
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_cohort,
            server_url,
            {
                "search_type": req.search_type,
                "search_params": req.search_params,
                "everything_params": req.everything_params,
                "token": req.token,
                "timeout": req.timeout,
                "config_profile": req.config_profile,
                "target_url": target_url,
                "target_token": target_token,
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="cohort",
        resource_id=job_dict.get("id", ""),
        action="submit",
    )
    if idem_key:
        _idem.remember("/v1/jobs/cohort", idem_key, body_hash, 202, job_dict)
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/patient-export", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_patient_export(req: PatientExportJobRequest, request: Request):
    """Queue a patient $everything export + de-identify job. Returns 202 immediately."""
    idem_key, body_hash, cached = _check_idempotency(request, "/v1/jobs/patient-export", req)
    if cached is not None:
        return JSONResponse(status_code=cached["status"], content=cached["body"])
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    target_url = await _resolve_optional_target_url(req.target_url)
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_patient_export,
            server_url,
            {
                "patient_id": req.patient_id,
                "patient_name": req.patient_name,
                "token": req.token,
                "timeout": req.timeout,
                "config_profile": req.config_profile,
                "target_url": target_url,
                "target_token": target_token,
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="patient-export",
        resource_id=job_dict.get("id", ""),
        action="submit",
    )
    if idem_key:
        _idem.remember("/v1/jobs/patient-export", idem_key, body_hash, 202, job_dict)
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/batch-patient-export", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_batch_patient_export(req: BatchPatientExportRequest, request: Request):
    """Queue a batch patient $everything export + de-identify job. Returns 202 immediately.

    Fetches $everything for each patient ID in parallel, de-duplicates shared
    resources, de-identifies, and writes a single combined NDJSON result.
    """
    idem_key, body_hash, cached = _check_idempotency(request, "/v1/jobs/batch-patient-export", req)
    if cached is not None:
        return JSONResponse(status_code=cached["status"], content=cached["body"])
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    target_url = await _resolve_optional_target_url(req.target_url)
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_batch_patient_export,
            server_url,
            {
                "patient_ids": req.patient_ids,
                "patient_names": req.patient_names,
                "token": req.token,
                "timeout": req.timeout,
                "config_profile": req.config_profile,
                "target_url": target_url,
                "target_token": target_token,
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="batch-patient-export",
        resource_id=job_dict.get("id", ""),
        action="submit",
    )
    if idem_key:
        _idem.remember("/v1/jobs/batch-patient-export", idem_key, body_hash, 202, job_dict)
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/bulk-import", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_bulk_import(req: BulkImportJobRequest, request: Request):
    """Queue a bulk-import job that uploads a completed NDJSON to a target FHIR server.

    Reads the source NDJSON identified by ``job_id`` (result of a completed
    bulk-export / cohort / patient-export job) or an explicit ``ndjson_path``,
    then uploads de-identified resources using parallel tier-aware FHIR batch
    Bundles.  Returns 202 immediately.

    Poll ``GET /v1/jobs/{job_id}`` for status.  When ``status=done`` the
    response checkpoint contains ``uploaded`` and ``errors`` counts.

    - Falls back to ``FHIR_TARGET_URL`` env when ``target_url`` is not in the request.
    - 400 if no target URL is available at all.
    - 503 if the job store is not initialised.
    """
    idem_key, body_hash, cached = _check_idempotency(request, "/v1/jobs/bulk-import", req)
    if cached is not None:
        return JSONResponse(status_code=cached["status"], content=cached["body"])
    target_url = await _resolve_import_target_url(req.target_url)
    if not target_url:
        raise HTTPException(
            status_code=400,
            detail="No target URL provided and FHIR_TARGET_URL env var is not set",
        )
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_bulk_import,
            {
                "job_id": req.job_id,
                "ndjson_path": req.ndjson_path,
                "target_url": target_url,
                "target_token": target_token,
                "timeout": req.timeout,
                "parallel": req.parallel,
                "batch_size": req.batch_size,
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="bulk-import",
        resource_id=job_dict.get("id", ""),
        action="submit",
    )
    if idem_key:
        _idem.remember("/v1/jobs/bulk-import", idem_key, body_hash, 202, job_dict)
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/risk-driven-export", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_risk_driven_export(req: RiskDrivenExportJobRequest, request: Request):
    """Queue a risk-driven k-anonymity export job. Returns 202 immediately.

    Three-phase job:
    1. Fetch FHIR resources → PostgreSQL staging table.
    2. Scan the full QI distribution → lattice search → optimal GeneralizationPlan.
    3. Apply the plan (QI overwrite + Patient suppression) → NDJSON.

    The ``GET /v1/jobs/{job_id}`` response includes an ``achieved_privacy`` block
    with the achieved k, suppression count, generalization levels per QI, and
    whether the target guarantee was met within the suppression cap.

    **Requires** ``MEDANON_STAGING_DB_URL`` to be configured (returns 400 otherwise).
    """
    idem_key, body_hash, cached = _check_idempotency(
        request, "/v1/jobs/risk-driven-export", req
    )
    if cached is not None:
        return JSONResponse(status_code=cached["status"], content=cached["body"])

    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL", request)
    if not server_url:
        raise HTTPException(
            status_code=400,
            detail="No server_url provided and FHIR_SOURCE_URL env var is not set",
        )
    await _validate_server_url(server_url)

    token = req.token or os.environ.get("FHIR_SOURCE_TOKEN") or None
    try:
        job_dict = await asyncio.to_thread(
            _service.submit_risk_driven_export,
            server_url,
            {
                "resource_type": req.resource_type,
                "type_filter": req.type_filter,
                "since": req.since,
                "token": token,
                "timeout": req.timeout,
                "config_profile": req.config_profile,
                "privacy_model": req.privacy_model,
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="risk-driven-export",
        resource_id=job_dict.get("job_id", ""),
        action="submit",
    )
    if idem_key:
        _idem.remember(
            "/v1/jobs/risk-driven-export", idem_key, body_hash, 202, job_dict
        )
    return JSONResponse(status_code=202, content=job_dict)


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
    """List jobs in the DLQ — those that exceeded ``MEDANON_JOB_MAX_RETRIES``.

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
    - 307 redirect when result is stored in S3/MinIO (``MEDANON_RESULT_STORAGE=s3``).
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

    # S3 result: redirect the client to a presigned MinIO URL (HTTP 307).
    # The client downloads directly from MinIO, bypassing the anonymizer.
    if result_path.startswith("s3://"):
        from integrations.storage import get_result_storage

        url = get_result_storage().get_download_url(result_path)
        return RedirectResponse(url=url, status_code=307)

    return FileResponse(
        result_path,
        media_type="application/x-ndjson",
        filename=f"job_{job_id}.ndjson",
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
async def upload_job_to_target(
    job_id: str, req: UploadToTargetRequest | None = None
):
    """Upload a completed job's de-identified resources to the target FHIR server.

    Uses idempotent PUT so repeated calls are safe (no duplicates).
    Reads the NDJSON result server-side — no client round-trip for large files.

    - Falls back to ``FHIR_TARGET_URL`` env when ``target_url`` is not provided.
    - 404 if the job does not exist.
    - 409 if the job is not yet ``done``.
    - 410 if the result file has been cleaned up.
    - 400 if no target URL is available.
    """
    if req is None:
        req = UploadToTargetRequest()
    resolved_url = await _resolve_import_target_url(req.target_url)
    if not resolved_url:
        raise HTTPException(
            status_code=400,
            detail="No target URL provided and FHIR_TARGET_URL env var is not set",
        )
    resolved_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN") or None
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

    Returns 404 when no detail has been saved yet — the client should then
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
    Upserts — safe to call multiple times.
    """
    store = _get_detail_store()
    await asyncio.to_thread(store.set, job_id, body.model_dump())
