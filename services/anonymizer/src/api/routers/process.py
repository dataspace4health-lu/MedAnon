"""Core processing endpoints: /process, /process/ndjson, /process/raw, /process/batch."""

import asyncio
import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from typing import Any

import pipeline.config as config
from pipeline.io_formats import parse_payload_bytes, serialize_payload
from pipeline.processor import process_data

from api.deps import (
    MAX_BODY_BYTES,
    get_settings_dep,
    limiter,
    _runtime_settings,
    _unwrap_to_resources,
    _unwrap_parameters_payload,
    _validate_dynamic_settings,
)

router = APIRouter()
logger = logging.getLogger("medanon")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/process")
@limiter.limit("60/minute")
async def process(
    request: Request,
    resource: Any = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Process a single FHIR resource or a FHIR Bundle.

    Accepts any valid FHIR JSON object or a JSON array of resources.
    Applies the configured rules and returns the pseudonymized/de-identified result.
    """
    if not isinstance(resource, (dict, list)):
        raise HTTPException(status_code=422, detail="Request body must be a FHIR JSON object or array")

    resource, dynamic_settings = _unwrap_parameters_payload(resource)
    if dynamic_settings:
        await _validate_dynamic_settings(dynamic_settings)
    runtime_settings = _runtime_settings(settings, dynamic_settings)

    resource_type = resource.get("resourceType", "unknown") if isinstance(resource, dict) else "array"
    logger.info("Processing request: resourceType=%s", resource_type)

    try:
        result = await asyncio.to_thread(process_data, resource, runtime_settings)
        logger.info("Processing complete: resourceType=%s", resource_type)
        return result
    except ValueError as exc:
        logger.warning("Validation error processing resourceType=%s: %s", resource_type, exc)
        raise HTTPException(status_code=422, detail="Invalid input") from exc
    except NotImplementedError as exc:
        logger.warning("Not-implemented action for resourceType=%s: %s", resource_type, exc)
        raise HTTPException(status_code=400, detail="Unsupported operation") from exc
    except Exception as exc:
        # Do not use logger.exception — traceback may contain PHI from resource processing
        logger.error("Unexpected error processing resourceType=%s: %s", resource_type, type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Unexpected processing error") from exc


@router.post("/process/ndjson")
@limiter.limit("20/minute")
async def process_ndjson(
    request: Request,
    settings: config.Settings = Depends(get_settings_dep),
):
    """Process a stream of newline-delimited FHIR resources (NDJSON / x-ndjson).

    Each non-empty line must be a valid JSON object representing a FHIR resource.
    Lines starting with '//' are treated as comments and skipped.
    Returns a streaming NDJSON response — one processed JSON object per line.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit")

    lines = body.decode("utf-8").splitlines()
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for lineno, raw in enumerate(lines, start=1):
            if await request.is_disconnected():
                logger.info("NDJSON: client disconnected at line %d", lineno)
                break
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("NDJSON line %d: JSON parse error — %s", lineno, exc)
                yield json.dumps({"error": f"line {lineno}: invalid JSON — {exc}"}) + "\n"
                continue
            try:
                result = await asyncio.to_thread(process_data, resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                logger.error("NDJSON line %d: processing error: %s", lineno, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": f"line {lineno}: processing error"}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post('/process/raw')
@limiter.limit("60/minute")
async def process_raw(
    request: Request,
    output_format: str = 'json',
    input_format: str = 'auto',
    settings: config.Settings = Depends(get_settings_dep),
):
    """Black-box endpoint supporting JSON, XML, and NDJSON input/output.

    Input detection:
    - input_format=auto uses Content-Type to infer json/xml/ndjson
    - input_format can be forced to json/xml/ndjson

    Output:
    - output_format in {json, xml, ndjson}
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit")

    try:
        payload = parse_payload_bytes(
            body,
            in_format=input_format,
            content_type=request.headers.get('content-type'),
        )
    except ValueError as exc:
        logger.warning("parse error in /process/raw: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid input format") from exc

    try:
        payload, dynamic_settings = _unwrap_parameters_payload(payload)
        if dynamic_settings:
            await _validate_dynamic_settings(dynamic_settings)
        runtime_settings = _runtime_settings(settings, dynamic_settings)

        if isinstance(payload, list):
            result = [await asyncio.to_thread(process_data, item, runtime_settings) for item in payload]
        else:
            result = await asyncio.to_thread(process_data, payload, runtime_settings)

        text, media_type = serialize_payload(result, out_format=output_format)
        return Response(content=text, media_type=media_type)
    except ValueError as exc:
        logger.warning("validation error in /process/raw: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid input") from exc
    except Exception as exc:
        # Do not use logger.exception — traceback may contain PHI
        logger.error('Unexpected error in /process/raw: %s', type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail='Unexpected processing error') from exc


@router.post("/process/batch")
@limiter.limit("20/minute")
async def process_batch(
    request: Request,
    settings: config.Settings = Depends(get_settings_dep),
):
    """Process FHIR resources in any format (JSON, NDJSON, XML) and stream NDJSON output.

    Accepts:
    - ``application/x-ndjson`` — one resource per line (same as /process/ndjson)
    - ``application/json`` / ``application/fhir+json`` — single resource or Bundle
    - ``application/xml`` / ``application/fhir+xml`` — single resource or Bundle

    Bundles are automatically unwrapped: each ``entry.resource`` is processed
    individually.  Returns a streaming NDJSON response.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")
    try:
        payload = parse_payload_bytes(body, content_type=content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    resources = _unwrap_to_resources(payload)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for idx, resource in enumerate(resources):
            if await request.is_disconnected():
                logger.info("process_batch: client disconnected at resource %d", idx)
                break
            try:
                result = await asyncio.to_thread(process_data, resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                logger.error("process_batch resource %d: %s", idx, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": f"resource {idx}: processing error"}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")
