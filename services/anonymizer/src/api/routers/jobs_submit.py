"""Async-job submission endpoints (POST /v1/jobs/*).

Each endpoint enqueues a long-running job and returns HTTP 202 with a
``job_id``.  Lifecycle (poll / cancel / download) lives in
:mod:`api.routers.jobs_manage`.  Shared helpers are in
:mod:`api.routers.jobs_common`.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from api.deps import _get_url_from_request_or_env, _validate_server_url, limiter
from api.schemas.jobs import (
    BatchPatientExportRequest,
    BulkExportJobRequest,
    BulkImportJobRequest,
    CohortJobRequest,
    PatientExportJobRequest,
    RiskDrivenExportJobRequest,
    SqlExportJobRequest,
)
from api.routers.jobs_common import (
    _RATE_JOBS_SUBMIT,
    _check_idempotency,
    _resolve_import_target_url,
    _resolve_optional_target_url,
    _service,
)
from api.services.jobs import JobStoreUnavailable
from utils import audit
from utils import idempotency as _idem

router = APIRouter()
logger = logging.getLogger("medanon")


@router.post("/jobs/bulk-export", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_bulk_export(req: BulkExportJobRequest, request: Request):
    """Queue a bulk-export + de-identify job. Returns 202 immediately.

    Poll ``GET /v1/jobs/{job_id}`` for status.
    Download via ``GET /v1/jobs/{job_id}/result`` when status is ``done``.
    When ``target_url`` is set (or ``FHIR_TARGET_URL`` env var), de-identified
    resources are also uploaded to that server via idempotent PUT.
    """
    idem_key, body_hash, cached = _check_idempotency(
        request, "/v1/jobs/bulk-export", req
    )
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


# Per-batch upload cap for tabular-batch jobs (number of files).
_TABULAR_BATCH_MAX_FILES = int(os.environ.get("MEDANON_TABULAR_BATCH_MAX_FILES", "500"))
# Total upload size cap (bytes) across all files in one tabular-batch request.
_TABULAR_BATCH_MAX_BYTES = int(
    os.environ.get("MEDANON_TABULAR_BATCH_MAX_BYTES", str(200 * 1024 * 1024))
)
_TABULAR_BATCH_FORMATS = ("csv", "xlsx", "parquet")


@router.post("/jobs/tabular-batch", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_tabular_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    format: str = Form("csv"),
    config_profile: str = Form("auto"),
):
    """Queue an async job that de-identifies many tabular files with one profile.

    Multipart upload: ``files`` (one or more CSV/Excel/Parquet of the same
    ``format``) + ``config_profile`` (a saved profile whose ``column:`` rules
    are applied to every file).  Returns 202 with a ``job_id``; poll
    ``GET /v1/jobs/{job_id}`` and download the result ZIP from
    ``GET /v1/jobs/{job_id}/result`` when ``status=done``.
    """
    fmt = (format or "csv").lower()
    if fmt not in _TABULAR_BATCH_FORMATS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported format {format!r}; expected one of {_TABULAR_BATCH_FORMATS}",
        )
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")
    if len(files) > _TABULAR_BATCH_MAX_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"Too many files ({len(files)}); max is {_TABULAR_BATCH_MAX_FILES} per batch",
        )

    file_data: list[tuple[str, bytes]] = []
    total = 0
    for idx, upload in enumerate(files):
        raw = await upload.read()
        total += len(raw)
        if total > _TABULAR_BATCH_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Batch total size exceeds the "
                f"{_TABULAR_BATCH_MAX_BYTES // (1024 * 1024)} MB limit",
            )
        file_data.append((upload.filename or f"file_{idx}.{fmt}", raw))

    try:
        job_dict = await asyncio.to_thread(
            _service.submit_tabular_batch, file_data, fmt, config_profile
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="tabular-batch",
        resource_id=job_dict.get("job_id", ""),
        action="submit",
    )
    return JSONResponse(status_code=202, content=job_dict)


@router.post("/jobs/sql-export", status_code=202)
@limiter.limit(_RATE_JOBS_SUBMIT)
async def submit_sql_export(req: SqlExportJobRequest, request: Request):
    """Queue an async job that de-identifies selected source-DB tables to files.

    Returns 202 with a ``job_id``; poll ``GET /v1/jobs/{job_id}`` and download the
    result ZIP from ``GET /v1/jobs/{job_id}/result`` when ``status=done``.
    """
    params = {
        "connection_id": req.connection_id,
        "schema": req.schema_name,
        "tables": req.tables,
        "output_format": req.output_format.lower(),
        "config_profile": req.config_profile,
        "chunk_size": req.chunk_size,
    }
    if req.rules is not None:
        params["rules"] = req.rules
    try:
        job_dict = await asyncio.to_thread(_service.submit_sql_export, params)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")
    audit.emit(
        "job.create",
        resource_type="sql-export",
        resource_id=job_dict.get("job_id", ""),
        action="submit",
    )
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
    idem_key, body_hash, cached = _check_idempotency(
        request, "/v1/jobs/patient-export", req
    )
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
    idem_key, body_hash, cached = _check_idempotency(
        request, "/v1/jobs/batch-patient-export", req
    )
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
        _idem.remember(
            "/v1/jobs/batch-patient-export", idem_key, body_hash, 202, job_dict
        )
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
    idem_key, body_hash, cached = _check_idempotency(
        request, "/v1/jobs/bulk-import", req
    )
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

    server_url = await _get_url_from_request_or_env(
        req.server_url, "FHIR_SOURCE_URL", request
    )
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
