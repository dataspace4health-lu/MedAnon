"""FHIR R4 Bulk Data Access IG endpoints.

Spec: https://hl7.org/fhir/uv/bulkdata/

    GET  /fhir/$export                    — system-level bulk export
    GET  /fhir/Patient/$export            — patient-level bulk export
    GET  /fhir/Group/{group_id}/$export   — group-level bulk export
    GET  /fhir/export-status/{job_id}     — poll export status
    DELETE /fhir/export-status/{job_id}  — cancel pending export

All export-trigger endpoints return HTTP 202 with a ``Content-Location``
header pointing to the polling URL, as required by the spec.
"""

from __future__ import annotations

import asyncio
from utils.json_fast import loads as _json_loads, dumps as _json_dumps
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request, Response

from domain.jobs import JobStatus

from api.deps import limiter
from api.schemas.fhir_bulk import BulkExportManifest, BulkExportOutputFile
from api.services.jobs import JobService, JobStoreUnavailable

logger = logging.getLogger("medanon")

router = APIRouter()

_service = JobService()

_SUPPORTED_OUTPUT_FORMAT = "application/fhir+ndjson"

# Cached at module-load time so TestClient requests (which run after a
# patch.dict context has already exited) still see the configured value.
_FHIR_SOURCE_URL: str = os.environ.get("FHIR_SOURCE_URL", "").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _operation_outcome(severity: str, code: str, diagnostics: str) -> dict:
    """Return a minimal FHIR OperationOutcome dict."""
    return {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": severity,
                "code": code,
                "diagnostics": diagnostics,
            }
        ],
    }


def _require_fhir_source_url() -> str:
    """Return the FHIR source URL or raise HTTP 422 if not configured.

    Env-var read order:
      1. ``FHIR_SOURCE_URL`` in ``os.environ`` at call time:
         - ``None`` → not set; fall back to the module-level cache.
         - ``""``   → explicitly disabled; raise 422.
      2. ``_FHIR_SOURCE_URL`` module-level cache (captured at import time).
    """
    env_value = os.environ.get("FHIR_SOURCE_URL")
    url = _FHIR_SOURCE_URL if env_value is None else env_value.strip()
    if not url:
        raise HTTPException(
            status_code=422,
            detail="FHIR_SOURCE_URL env var is required for bulk export",
        )
    return url.rstrip("/")


def _validate_output_format(output_format: str) -> None:
    """Raise HTTP 422 if *output_format* is not the FHIR NDJSON media type."""
    if output_format != _SUPPORTED_OUTPUT_FORMAT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported _outputFormat '{output_format}'. "
                f"Only '{_SUPPORTED_OUTPUT_FORMAT}' is supported."
            ),
        )


def _base_url(request: Request) -> str:
    """Return the base URL of the application (no trailing slash)."""
    return str(request.base_url).rstrip("/")


def _collect_resource_types(result_path: str) -> list[str]:
    """Read up to 1000 lines of *result_path* and return unique resourceType values."""
    types_seen: list[str] = []
    seen_set: set[str] = set()
    try:
        with open(result_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 1000:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json_loads(line)
                    rt = obj.get("resourceType")
                    if rt and rt not in seen_set:
                        seen_set.add(rt)
                        types_seen.append(rt)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        return []
    return types_seen


# ---------------------------------------------------------------------------
# Export-trigger endpoints
# ---------------------------------------------------------------------------


@router.get("/$export")
@limiter.limit("5/minute")
async def system_export(
    request: Request,
    _since: str | None = Query(
        None, description="Only include resources modified after this datetime"
    ),
    _type: str | None = Query(
        None, description="Comma-separated FHIR resource types to export"
    ),
    _outputFormat: str = Query(
        _SUPPORTED_OUTPUT_FORMAT,
        description="Output format; only application/fhir+ndjson is supported",
    ),
) -> Response:
    """Initiate a system-level bulk export.

    Returns HTTP 202 with a ``Content-Location`` header pointing to the
    polling URL for this job.
    """
    _validate_output_format(_outputFormat)
    server_url = _require_fhir_source_url()

    try:
        job_dict = await asyncio.to_thread(
            _service.submit_bulk_export,
            server_url,
            {
                "level": "system",
                "since": _since,
                "type_filter": _type,
                "_fhir_bulk_spec": True,
                "request_url": str(request.url),
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")

    job_id = job_dict["job_id"]
    location = f"{_base_url(request)}/fhir/export-status/{job_id}"
    return Response(status_code=202, headers={"Content-Location": location})


@router.get("/Patient/$export")
@limiter.limit("5/minute")
async def patient_export(
    request: Request,
    _since: str | None = Query(
        None, description="Only include resources modified after this datetime"
    ),
    _type: str | None = Query(
        None, description="Comma-separated FHIR resource types to export"
    ),
    _outputFormat: str = Query(
        _SUPPORTED_OUTPUT_FORMAT,
        description="Output format; only application/fhir+ndjson is supported",
    ),
) -> Response:
    """Initiate a patient-level bulk export.

    Returns HTTP 202 with a ``Content-Location`` header pointing to the
    polling URL for this job.
    """
    _validate_output_format(_outputFormat)
    server_url = _require_fhir_source_url()

    try:
        job_dict = await asyncio.to_thread(
            _service.submit_bulk_export,
            server_url,
            {
                "level": "patient",
                "since": _since,
                "type_filter": _type,
                "_fhir_bulk_spec": True,
                "request_url": str(request.url),
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")

    job_id = job_dict["job_id"]
    location = f"{_base_url(request)}/fhir/export-status/{job_id}"
    return Response(status_code=202, headers={"Content-Location": location})


@router.get("/Group/{group_id}/$export")
@limiter.limit("5/minute")
async def group_export(
    request: Request,
    group_id: str,
    _since: str | None = Query(
        None, description="Only include resources modified after this datetime"
    ),
    _type: str | None = Query(
        None, description="Comma-separated FHIR resource types to export"
    ),
    _outputFormat: str = Query(
        _SUPPORTED_OUTPUT_FORMAT,
        description="Output format; only application/fhir+ndjson is supported",
    ),
) -> Response:
    """Initiate a group-level bulk export for *group_id*.

    Returns HTTP 202 with a ``Content-Location`` header pointing to the
    polling URL for this job.
    """
    _validate_output_format(_outputFormat)
    server_url = _require_fhir_source_url()

    try:
        job_dict = await asyncio.to_thread(
            _service.submit_bulk_export,
            server_url,
            {
                "level": "group",
                "group_id": group_id,
                "since": _since,
                "type_filter": _type,
                "_fhir_bulk_spec": True,
                "request_url": str(request.url),
            },
        )
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")

    job_id = job_dict["job_id"]
    location = f"{_base_url(request)}/fhir/export-status/{job_id}"
    return Response(status_code=202, headers={"Content-Location": location})


# ---------------------------------------------------------------------------
# Status polling + cancellation
# ---------------------------------------------------------------------------


@router.get("/export-status/{job_id}")
@limiter.limit("60/minute")
async def export_status(request: Request, job_id: str) -> Response:
    """Poll the status of a bulk export job.

    * HTTP 202 + ``X-Progress`` header while pending or running.
    * HTTP 200 + JSON manifest body when the export is complete.
    * HTTP 500 + OperationOutcome when the export failed.
    * HTTP 404 + OperationOutcome when the job is unknown.
    """
    try:
        store = _service._get_store()
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")

    job = await asyncio.to_thread(store.get, job_id)
    if job is None:
        outcome = _operation_outcome("error", "not-found", f"Job '{job_id}' not found")
        return Response(
            status_code=404,
            content=_json_dumps(outcome),
            media_type="application/json",
        )

    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        x_progress = f"status={job.status.value} job_id={job_id}"
        return Response(status_code=202, headers={"X-Progress": x_progress})

    if job.status == JobStatus.DONE:
        download_url = f"{_base_url(request)}/v1/jobs/{job_id}/result"
        result_path = job.result_path or ""

        resource_types = (
            await asyncio.to_thread(_collect_resource_types, result_path)
            if result_path
            else []
        )
        if resource_types:
            output = [
                BulkExportOutputFile(type=rt, url=download_url) for rt in resource_types
            ]
        else:
            output = [BulkExportOutputFile(type="Bundle", url=download_url)]

        manifest = BulkExportManifest(
            transactionTime=job.updated_at,
            request=job.params.get("request_url", ""),
            output=output,
        )
        return Response(
            status_code=200,
            content=manifest.model_dump_json(),
            media_type="application/json",
        )

    if job.status == JobStatus.CANCELLED:
        outcome = _operation_outcome(
            "information", "informational", "Export was cancelled"
        )
        return Response(
            status_code=410,
            content=_json_dumps(outcome),
            media_type="application/json",
        )

    # status == error
    diagnostics = job.error or "Export failed with an unknown error"
    outcome = _operation_outcome("error", "exception", diagnostics)
    return Response(
        status_code=500,
        content=_json_dumps(outcome),
        media_type="application/json",
    )


@router.delete("/export-status/{job_id}")
@limiter.limit("60/minute")
async def cancel_export(request: Request, job_id: str) -> Response:
    """Cancel a pending or running bulk export job.

    * HTTP 202 on successful cancellation.
    * HTTP 409 if the job has already completed (done or error).
    * HTTP 404 if the job is unknown.
    """

    def _cancel_sync() -> tuple[str, dict | None]:
        """Run all store I/O in a single thread to avoid blocking the event loop."""
        store = _service._get_store()
        job = store.get(job_id)
        if job is None:
            return "not_found", None
        if job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
            return "conflict", {
                "status": job.status.value,
            }
        job.status = JobStatus.CANCELLED
        job.error = "Cancelled by client request"
        store.update(job)
        return "cancelled", None

    try:
        outcome_tag, extra = await asyncio.to_thread(_cancel_sync)
    except JobStoreUnavailable:
        raise HTTPException(status_code=503, detail="Job store not initialised")

    if outcome_tag == "not_found":
        body = _operation_outcome("error", "not-found", f"Job '{job_id}' not found")
        return Response(
            status_code=404,
            content=_json_dumps(body),
            media_type="application/json",
        )
    if outcome_tag == "conflict":
        body = _operation_outcome(
            "error",
            "conflict",
            f"Cannot cancel job '{job_id}' with status '{extra['status']}'",
        )
        return Response(
            status_code=409,
            content=_json_dumps(body),
            media_type="application/json",
        )
    return Response(status_code=202)
