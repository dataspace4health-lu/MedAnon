"""FHIR server integration endpoints: from-server, everything, and-upload, round-trip."""

import logging
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import pipeline.config as config

from api.deps import (
    get_settings_dep,
    limiter,
    _get_url_from_request_or_env,
    _runtime_settings,
    _unwrap_parameters_payload,
    _validate_dynamic_settings,
)
from api.schemas.fhir_ops import (
    AndUploadRequest,
    BulkExportRequest,
    CohortRequest,
    EverythingRequest,
    FromServerRequest,
    RoundTripRequest,
)

router = APIRouter()
logger = logging.getLogger("medanon")


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------

def _get_service():
    """Return a FhirServerService wired to the HTTP FHIR client adapter."""
    from api.services.fhir_server import FhirServerService
    from integrations.fhir.adapter import HttpFhirClientAdapter
    return FhirServerService(HttpFhirClientAdapter())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post('/process/from-server')
@limiter.limit("10/minute")
async def process_from_server(
    request: Request,
    req: FromServerRequest = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Fetch FHIR resources directly from a FHIR server, anonymize, and return NDJSON.

    Request body (JSON):
    ```json
    {
      "server_url": "http://host:8080/fhir",
      "resource_types": ["Patient", "Observation"],
      "params": {"_count": 100},
      "token": "optional-bearer-token"
    }
    ```

    Returns streaming NDJSON — one anonymized resource per line.
    """
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    token = req.token or os.environ.get("FHIR_SOURCE_TOKEN")
    svc = _get_service()
    try:
        resource_types = svc.resolve_resource_types(
            server_url, req.resource_types, token, req.timeout
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach FHIR server: {exc}") from exc
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        async for line in svc.stream_from_server(
            server_url, resource_types, req.params, token, req.timeout, runtime_settings
        ):
            if await request.is_disconnected():
                logger.info("from-server: client disconnected, stopping stream")
                break
            yield line + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post('/process/everything')
@limiter.limit("10/minute")
async def process_everything(
    request: Request,
    req: EverythingRequest = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Fetch all resources via FHIR $everything, anonymize, and return NDJSON.

    Calls ``GET {server_url}/{resource_type}/{resource_id}/$everything`` and
    de-identifies every resource in the response Bundle.

    Request body (JSON):
    ```json
    {
      "server_url":    "http://host:8080/fhir",
      "resource_type": "Patient",
      "resource_id":   "DDME",
      "params":        {"_count": 50},
      "token":         "optional-bearer-token"
    }
    ```

    Returns streaming NDJSON — one anonymized resource per line.
    """
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    token = req.token or os.environ.get("FHIR_SOURCE_TOKEN")
    runtime_settings = _runtime_settings(settings)
    svc = _get_service()

    async def _generate():
        async for line in svc.stream_everything(
            server_url, req.resource_type, req.resource_id,
            req.params, token, req.timeout, runtime_settings,
        ):
            if await request.is_disconnected():
                logger.info("everything: client disconnected, stopping stream")
                break
            yield line + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post("/process/and-upload")
@limiter.limit("10/minute")
async def process_and_upload(
    request: Request,
    req: AndUploadRequest = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """De-identify a FHIR resource (or Bundle) and upload the result to a FHIR server.

    Request body (JSON):
    ```json
    {
      "target_server_url": "http://hapi-fhir:8080/fhir",
      "resource": { ...FHIR resource or Bundle... },
      "target_token": "optional-bearer-token",
      "timeout": 30
    }
    ```

    Returns a JSON summary:
    ```json
    { "uploaded": 2, "errors": 0, "results": [...] }
    ```
    """
    target_url = await _get_url_from_request_or_env(
        req.target_server_url, "FHIR_TARGET_URL", "target_server_url"
    )
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN")

    resource, dynamic_settings = _unwrap_parameters_payload(req.resource)
    if dynamic_settings:
        await _validate_dynamic_settings(dynamic_settings)
    runtime_settings = _runtime_settings(settings, dynamic_settings)

    svc = _get_service()
    try:
        return await svc.process_and_upload(
            resource, target_url, target_token, req.timeout, runtime_settings
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "process_and_upload error: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="De-identification error") from exc


@router.post("/process/round-trip")
@limiter.limit("10/minute")
async def process_round_trip(
    request: Request,
    req: RoundTripRequest = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Fetch from a source FHIR server, de-identify, and upload to a target server.

    Request body (JSON):
    ```json
    {
      "source_server_url": "http://source-fhir:8080/fhir",
      "target_server_url": "http://hapi-fhir:8080/fhir",
      "resource_types":    ["Patient", "Observation"],
      "params":            {"_count": 100},
      "source_token":      "optional",
      "target_token":      "optional",
      "timeout":           30
    }
    ```

    If ``resource_types`` is omitted, types are auto-discovered from
    the source server's ``/metadata`` capability statement.

    Returns streaming NDJSON — one status line per resource.
    """
    source_url = await _get_url_from_request_or_env(
        req.source_server_url, "FHIR_SOURCE_URL", "source_server_url"
    )
    target_url = await _get_url_from_request_or_env(
        req.target_server_url, "FHIR_TARGET_URL", "target_server_url"
    )
    source_token = req.source_token or os.environ.get("FHIR_SOURCE_TOKEN")
    target_token = req.target_token or os.environ.get("FHIR_TARGET_TOKEN")
    svc = _get_service()
    try:
        resource_types = svc.resolve_resource_types(
            source_url, req.resource_types, source_token, req.timeout
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach FHIR server: {exc}") from exc
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        async for line in svc.stream_round_trip(
            source_url, target_url, resource_types, req.params,
            source_token, target_token, req.timeout, runtime_settings,
        ):
            if await request.is_disconnected():
                logger.info("round-trip: client disconnected, stopping stream")
                break
            yield line + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post('/process/bulk-export')
@limiter.limit("5/minute")
async def process_bulk_export(
    request: Request,
    req: BulkExportRequest = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Initiate a FHIR Bulk Data Export ($export), de-identify results, and return NDJSON.

    Request body (JSON):
    ```json
    {
      "server_url":    "http://host:8080/fhir",
      "level":         "system",
      "resource_type": "Patient",
      "type_filter":   "Patient,Observation",
      "since":         "2024-01-01T00:00:00Z",
      "token":         "optional-bearer-token",
      "timeout":       30
    }
    ```

    Returns streaming NDJSON — one anonymized resource per line.
    """
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    if req.level == "type" and not req.resource_type:
        raise HTTPException(status_code=422, detail="resource_type is required for type-level export")
    token = req.token or os.environ.get("FHIR_SOURCE_TOKEN")
    runtime_settings = _runtime_settings(settings)
    svc = _get_service()

    async def _generate():
        async for line in svc.stream_bulk_export(
            server_url, req.level, req.resource_type, req.type_filter,
            req.since, token, req.timeout, runtime_settings,
        ):
            if await request.is_disconnected():
                logger.info("bulk-export: client disconnected, stopping stream")
                break
            yield line + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post('/process/cohort')
@limiter.limit("10/minute")
async def process_cohort(
    request: Request,
    req: CohortRequest = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Search for patients matching a condition code and export their full records.

    Two-phase workflow: searches for resources matching `search_type` + `search_params`,
    extracts unique Patient references, then calls $everything for each patient.

    Request body (JSON):
    ```json
    {
      "server_url":    "http://host:8080/fhir",
      "search_type":   "Condition",
      "search_params": {"code": "E11"},
      "everything_params": {"_count": 50},
      "token":         "optional-bearer-token",
      "timeout":       30
    }
    ```

    Returns streaming NDJSON — one anonymized resource per line.
    """
    server_url = await _get_url_from_request_or_env(req.server_url, "FHIR_SOURCE_URL")
    token = req.token or os.environ.get("FHIR_SOURCE_TOKEN")
    runtime_settings = _runtime_settings(settings)
    svc = _get_service()

    async def _generate():
        async for line in svc.stream_cohort(
            server_url, req.search_type, req.search_params,
            req.everything_params, token, req.timeout, runtime_settings,
        ):
            if await request.is_disconnected():
                logger.info("cohort: client disconnected, stopping stream")
                break
            yield line + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")

