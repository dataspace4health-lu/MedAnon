"""Core processing endpoints: /process, /process/ndjson, /process/raw, /process/batch."""

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import pipeline.config as config
from pipeline.io_formats import parse_payload_bytes, serialize_payload

from api.deps import (
    MAX_BODY_BYTES,
    get_settings_dep,
    limiter,
    _runtime_settings,
    _unwrap_to_resources,
    _unwrap_parameters_payload,
    _validate_dynamic_settings,
)
from api.services.processing import ProcessingError, ProcessingService

router = APIRouter()
logger = logging.getLogger("medanon")

_service = ProcessingService()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/process")
@limiter.limit("60/minute")
async def process(
    request: Request,
    resource: dict | list = Body(...),
    settings: config.Settings = Depends(get_settings_dep),
):
    """Process a single FHIR resource or a FHIR Bundle.

    Accepts any valid FHIR JSON object or a JSON array of resources.
    Applies the configured rules and returns the pseudonymized/de-identified result.
    """

    resource, dynamic_settings = _unwrap_parameters_payload(resource)
    if dynamic_settings:
        await _validate_dynamic_settings(dynamic_settings)
    runtime_settings = _runtime_settings(settings, dynamic_settings)

    try:
        return await _service.process_resource(resource, runtime_settings)
    except ProcessingError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


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
        async for line in _service.process_ndjson_lines(lines, runtime_settings):
            if await request.is_disconnected():
                logger.info("NDJSON: client disconnected")
                break
            yield line + "\n"

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

        result = await _service.process_resource(payload, runtime_settings)
        text, media_type = serialize_payload(result, out_format=output_format)
        return Response(content=text, media_type=media_type)
    except ProcessingError as exc:
        if exc.status == 422:
            raise HTTPException(status_code=422, detail="Invalid input") from exc
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
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

    Bundles are processed as a whole to preserve cross-resource reference
    rewriting, then each entry resource is streamed as NDJSON.
    Non-Bundle inputs are unwrapped and streamed individually.
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

    runtime_settings = _runtime_settings(settings)

    # Bundles are processed as a whole so cross-resource reference rewriting
    # (pre/post ID snapshot + _rewrite_references) is preserved.
    is_bundle = isinstance(payload, dict) and payload.get("resourceType") == "Bundle"

    async def _generate():
        if is_bundle:
            async for line in _service.process_bundle_stream(payload, runtime_settings):
                if await request.is_disconnected():
                    logger.info("process_batch: client disconnected")
                    break
                yield line + "\n"
        else:
            resources = _unwrap_to_resources(payload)
            async for line in _service.process_resource_stream(resources, runtime_settings):
                if await request.is_disconnected():
                    logger.info("process_batch: client disconnected")
                    break
                yield line + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")

