"""FHIR server integration endpoints: from-server, everything, and-upload, round-trip."""

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import pipeline.config as config
from pipeline.processor import process_data
from integrations.fhir.client import (
    fetch_all_resource_types,
    fetch_everything,
    get_capability_statement,
    post_resource,
    upload_resources,
)

from api.deps import (
    MAX_BODY_BYTES,
    get_settings_dep,
    limiter,
    _get_url_from_request_or_env,
    _runtime_settings,
    _unwrap_parameters_payload,
    _validate_dynamic_settings,
)

router = APIRouter()
logger = logging.getLogger("medanon")


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

async def _parse_json_body(request: Request) -> dict:
    """Read and parse a JSON request body; raises 422 on invalid JSON."""
    body = await request.body()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc


def _get_timeout(req_data: dict, default: float = 30.0) -> float:
    """Extract and validate the timeout field; raises 422 on bad value."""
    try:
        return float(req_data.get("timeout", default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid timeout value: {exc}") from exc


def _resolve_resource_types(server_url: str, req_data: dict, token, timeout: float) -> list:
    """Return resource_types from request data or auto-discover from /metadata.

    Raises 502 if the source server is unreachable.
    """
    resource_types = req_data.get("resource_types")
    if not resource_types:
        try:
            resource_types = get_capability_statement(server_url, token=token, timeout=timeout)
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach FHIR server: {exc}",
            ) from exc
    return resource_types


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post('/process/from-server')
@limiter.limit("10/minute")
async def process_from_server(
    request: Request,
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
    req_data = await _parse_json_body(request)

    server_url = await _get_url_from_request_or_env(
        req_data, "server_url", "FHIR_SOURCE_URL"
    )

    extra_params = req_data.get("params") or {}
    token = req_data.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = _get_timeout(req_data)
    resource_types = _resolve_resource_types(server_url, req_data, token, timeout)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for _rt, resource in fetch_all_resource_types(
            server_url, resource_types, params=extra_params, token=token, timeout=timeout
        ):
            if await request.is_disconnected():
                logger.info("from-server: client disconnected, stopping stream")
                break
            try:
                result = await asyncio.to_thread(process_data, resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                logger.error("Error processing resource type=%s: %s", _rt, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": "processing error", "resourceType": _rt}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post('/process/everything')
@limiter.limit("10/minute")
async def process_everything(
    request: Request,
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
    req_data = await _parse_json_body(request)

    server_url = await _get_url_from_request_or_env(
        req_data, "server_url", "FHIR_SOURCE_URL"
    )

    resource_type = req_data.get("resource_type")
    if not resource_type:
        raise HTTPException(status_code=422, detail="resource_type is required")

    resource_id = req_data.get("resource_id")
    if not resource_id:
        raise HTTPException(status_code=422, detail="resource_id is required")

    extra_params = req_data.get("params") or {}
    token = req_data.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = _get_timeout(req_data)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for resource in fetch_everything(
            server_url, resource_type, resource_id,
            params=extra_params, token=token, timeout=timeout,
        ):
            if await request.is_disconnected():
                logger.info("everything: client disconnected, stopping stream")
                break
            try:
                result = await asyncio.to_thread(process_data, resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                _rtype = resource.get("resourceType", resource_type)
                logger.error("Error processing resource type=%s: %s", _rtype, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": "processing error", "resourceType": _rtype}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post("/process/and-upload")
@limiter.limit("10/minute")
async def process_and_upload(
    request: Request,
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
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds {MAX_BODY_BYTES // (1024*1024)} MB limit")
    try:
        req_data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    target_url = await _get_url_from_request_or_env(
        req_data, "target_server_url", "FHIR_TARGET_URL"
    )

    resource = req_data.get("resource")
    if not resource or not isinstance(resource, dict):
        raise HTTPException(status_code=422, detail="resource must be a FHIR JSON object")

    target_token = req_data.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = _get_timeout(req_data)

    resource, dynamic_settings = _unwrap_parameters_payload(resource)
    if dynamic_settings:
        await _validate_dynamic_settings(dynamic_settings)
    runtime_settings = _runtime_settings(settings, dynamic_settings)

    try:
        deidentified = await asyncio.to_thread(process_data, resource, runtime_settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Do not log exception — traceback may contain PHI
        logger.error("process_and_upload: de-identification error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="De-identification error") from exc

    # Flatten Bundle entries or wrap single resource into a list
    if isinstance(deidentified, dict) and deidentified.get("resourceType") == "Bundle":
        resources_to_upload = [
            entry["resource"]
            for entry in deidentified.get("entry", [])
            if isinstance(entry.get("resource"), dict)
        ]
    elif isinstance(deidentified, list):
        resources_to_upload = deidentified
    else:
        resources_to_upload = [deidentified]

    results = list(await asyncio.to_thread(
        lambda: list(upload_resources(target_url, resources_to_upload, token=target_token, timeout=timeout))
    ))
    uploaded = sum(1 for r in results if r["success"])
    errors = sum(1 for r in results if not r["success"])

    logger.info("process_and_upload: uploaded=%d errors=%d target=%s", uploaded, errors, target_url)
    return {"uploaded": uploaded, "errors": errors, "results": results}


@router.post("/process/round-trip")
@limiter.limit("10/minute")
async def process_round_trip(
    request: Request,
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
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds {MAX_BODY_BYTES // (1024*1024)} MB limit")

    req_data = await _parse_json_body(request)

    source_url = await _get_url_from_request_or_env(
        req_data, "source_server_url", "FHIR_SOURCE_URL"
    )
    target_url = await _get_url_from_request_or_env(
        req_data, "target_server_url", "FHIR_TARGET_URL"
    )

    extra_params = req_data.get("params") or {}
    source_token = req_data.get("source_token") or os.environ.get("FHIR_SOURCE_TOKEN")
    target_token = req_data.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = _get_timeout(req_data)
    resource_types = _resolve_resource_types(source_url, req_data, source_token, timeout)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for _rt, resource in fetch_all_resource_types(
            source_url, resource_types, params=extra_params, token=source_token, timeout=timeout
        ):
            if await request.is_disconnected():
                logger.info("round-trip: client disconnected, stopping stream")
                break
            try:
                deidentified = await asyncio.to_thread(process_data, resource, runtime_settings)
                resp = await asyncio.to_thread(post_resource, target_url, deidentified, token=target_token, timeout=timeout)
                yield json.dumps({
                    "resourceType": _rt,
                    "target_id": resp.get("id"),
                    "status": "ok",
                }) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                logger.error("round_trip error type=%s: %s", _rt, type(exc).__name__, exc_info=False)
                yield json.dumps({
                    "resourceType": _rt,
                    "status": "error",
                    "error": "processing error",
                }) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")
