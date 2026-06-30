"""Core processing endpoints: /process, /process/ndjson, /process/raw, /process/batch."""

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import pipeline.config as config
from pipeline.io_formats import parse_payload_bytes, serialize_payload
from utils.tasks import retain_task

from api.deps import (
    MAX_BODY_BYTES,
    get_settings_dep,
    limiter,
    _runtime_settings,
    _extract_full_urls,
    _unwrap_to_resources,
    _unwrap_parameters_payload,
    _validate_dynamic_settings,
)
from pipeline.intake_gate import IntakeBlocked, enforce_intake
from api.services import stream_trailer
from api.services.processing import ProcessingError, ProcessingService
from api.services.scoring_helpers import (
    _is_scoring_enabled,
    _get_config_profile,
    make_collector,
    check_and_persist_with_leak,
    apply_pii_leak_override,
    score_json_line,
    persist_run,
)
from utils import idempotency as _idem

router = APIRouter()
logger = logging.getLogger("medanon")

_RATE_PROCESS = os.environ.get("MEDANON_RATE_PROCESS", "200/minute")
_RATE_NDJSON = os.environ.get("MEDANON_RATE_NDJSON", "60/minute")
_RATE_RAW = os.environ.get("MEDANON_RATE_RAW", "200/minute")
_RATE_BATCH = os.environ.get("MEDANON_RATE_BATCH", "60/minute")

_service = ProcessingService()


def _attach_pii_warning(result, pii_leak: dict) -> None:
    """Embed a non-blocking coverage warning into a JSON result for the client.

    Used when ``MEDANON_GATE_IDENTIFIER_MODE=warn`` and the only finding is a
    config-coverage gap (no detected PII). The output is released; the warning
    rides along under ``meta.tag`` so the UI can surface "weak config" without
    a 422 block. Mutates dict resources only — Bundles attach to the Bundle root.
    """
    if not isinstance(result, dict):
        return
    tag = {
        "system": "https://medanon/coverage-warning",
        "code": "weak-config",
        "display": pii_leak.get("message", "")[:1000],
    }
    meta = result.setdefault("meta", {})
    if isinstance(meta, dict):
        meta.setdefault("tag", []).append(tag)


def _run_intake_gate(
    resources_list, profile, dataset_id, trust_profile=None, full_urls=None
):
    """Run the pre-privacy Trust Gate barrier before de-identification.

    No-op unless ``TRUST_GATE_SERVICE_URL`` is set. In ``TRUST_GATE_MODE=block``
    a BLOCK verdict raises HTTP 422; otherwise the passport is advisory. Returns
    the Quality Passport dict so callers can persist it with the processing run.

    ``trust_profile`` (the ``?trust_profile=`` query param) names a stored audit
    profile whose phase selection scopes what the Trust Gate measures.
    ``full_urls`` are Bundle entry.fullUrl values forwarded so the gate can
    resolve intra-bundle urn:uuid / absolute references.
    """
    try:
        return enforce_intake(
            resources_list,
            profile,
            dataset_id=dataset_id,
            source_types=["fhir"],
            trust_profile=trust_profile,
            full_urls=full_urls,
        )
    except IntakeBlocked as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "intake_blocked",
                "message": str(exc),
                "passport": exc.passport,
            },
        ) from exc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/process")
@limiter.limit(_RATE_PROCESS)
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

    # Idempotency-Key support: optional header replays a prior response when
    # the same key + same body is seen again within the TTL window.
    try:
        idem_key = _idem.validate_key(request.headers.get("Idempotency-Key"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body_hash = _idem.hash_body(resource) if idem_key else ""
    if idem_key:
        try:
            cached = _idem.lookup_or_conflict("/v1/process", idem_key, body_hash)
        except KeyError:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key reused with a different request body",
            )
        if cached is not None:
            return cached["body"]

    # Pre-privacy Trust Gate barrier (no-op unless TRUST_GATE_SERVICE_URL set).
    profile = _get_config_profile(runtime_settings)
    gate_resources = _unwrap_to_resources(resource)
    passport = await asyncio.to_thread(
        _run_intake_gate,
        gate_resources,
        profile,
        "/v1/process",
        request.query_params.get("trust_profile"),
        _extract_full_urls(resource),
    )

    t0 = time.monotonic()
    try:
        result = await _service.process_resource(resource, runtime_settings)
    except ProcessingError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    pii_leak = None
    if _is_scoring_enabled():
        pii_leak = await check_and_persist_with_leak(
            result, "/v1/process", runtime_settings, t0, trust_passport=passport or None
        )
    elif passport:
        # Scoring off but the gate produced a passport — persist it on its own so
        # every gated path keeps its Quality Passport (mirrors /process/batch).
        retain_task(
            persist_run(
                endpoint="/v1/process",
                config_profile=profile,
                resource_count=len(gate_resources),
                error_count=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
                input_type=(
                    "Bundle"
                    if isinstance(resource, dict)
                    and resource.get("resourceType") == "Bundle"
                    else "array"
                ),
                trust_passport=passport,
            )
        )

    if pii_leak and pii_leak.get("leaked"):
        # Real PII detected — block output entirely, return 422 with leak info.
        raise HTTPException(
            status_code=422,
            detail={"code": "pii_leak_detected", "pii_leak": pii_leak},
        )
    if pii_leak:
        # Warning-only (weak config, no detected PII): release output, but attach
        # the coverage warning so the client can surface it without blocking.
        _attach_pii_warning(result, pii_leak)

    if idem_key:
        _idem.remember("/v1/process", idem_key, body_hash, 200, result)
    return result


@router.post("/process/ndjson")
@limiter.limit(_RATE_NDJSON)
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
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )

    lines = body.decode("utf-8").splitlines()
    runtime_settings = _runtime_settings(settings)
    profile = _get_config_profile(runtime_settings)

    # Pre-privacy Trust Gate barrier. Runs before streaming starts so a BLOCK
    # (in block mode) returns 422 before any bytes are sent. No-op unless
    # TRUST_GATE_SERVICE_URL is set; the passport is persisted with the run.
    gate_resources: list[dict] = []
    for _ln in lines:
        _s = _ln.strip()
        if not _s or _s.startswith("//"):
            continue
        try:
            _obj = json.loads(_s)
        except ValueError:
            continue
        if isinstance(_obj, dict):
            gate_resources.append(_obj)
    passport = await asyncio.to_thread(
        _run_intake_gate,
        gate_resources,
        profile,
        "/v1/process/ndjson",
        request.query_params.get("trust_profile"),
    )

    async def _generate():
        collector = make_collector(profile)
        count = 0
        t0 = time.monotonic()
        disconnected = False
        async for line in _service.process_ndjson_lines(lines, runtime_settings):
            if await request.is_disconnected():
                logger.info("NDJSON: client disconnected")
                disconnected = True
                break
            score_json_line(collector, line, runtime_settings)
            yield line + "\n"
            count += 1
        score = collector.aggregate() if collector else None
        pii_leak = apply_pii_leak_override(score) if score else None
        if not disconnected:
            yield stream_trailer(count, score, pii_leak=pii_leak) + "\n"
        if score is not None or passport:
            retain_task(
                persist_run(
                    endpoint="/v1/process/ndjson",
                    config_profile=profile,
                    resource_count=count,
                    error_count=score.get("error_count", 0) if score else 0,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    input_type="ndjson",
                    summary={"total_resources": count},
                    score=score,
                    trust_passport=passport or None,
                )
            )

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post("/process/raw")
@limiter.limit(_RATE_RAW)
async def process_raw(
    request: Request,
    output_format: str = "json",
    input_format: str = "auto",
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
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )

    try:
        payload = parse_payload_bytes(
            body,
            in_format=input_format,
            content_type=request.headers.get("content-type"),
        )
    except ValueError as exc:
        logger.warning("parse error in /process/raw: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid input format") from exc

    try:
        payload, dynamic_settings = _unwrap_parameters_payload(payload)
        if dynamic_settings:
            await _validate_dynamic_settings(dynamic_settings)
        runtime_settings = _runtime_settings(settings, dynamic_settings)

        # Pre-privacy Trust Gate barrier (no-op unless TRUST_GATE_SERVICE_URL set).
        # A BLOCK (in block mode) raises HTTP 422, caught by the HTTPException
        # re-raise below before any output is serialized.
        profile = _get_config_profile(runtime_settings)
        gate_resources = _unwrap_to_resources(payload)
        passport = await asyncio.to_thread(
            _run_intake_gate,
            gate_resources,
            profile,
            "/v1/process/raw",
            request.query_params.get("trust_profile"),
            _extract_full_urls(payload),
        )

        t0 = time.monotonic()
        result = await _service.process_resource(payload, runtime_settings)

        pii_leak = None
        if _is_scoring_enabled():
            pii_leak = await check_and_persist_with_leak(
                result, "/v1/process/raw", runtime_settings, t0, trust_passport=passport or None
            )
        elif passport:
            retain_task(
                persist_run(
                    endpoint="/v1/process/raw",
                    config_profile=profile,
                    resource_count=len(gate_resources),
                    error_count=0,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    input_type="raw",
                    trust_passport=passport,
                )
            )

        if pii_leak and pii_leak.get("leaked"):
            # Real PII detected — block output, return 422 with pii_leak payload.
            raise HTTPException(
                status_code=422,
                detail={"code": "pii_leak_detected", "pii_leak": pii_leak},
            )

        text, media_type = serialize_payload(result, out_format=output_format)
        headers = {}
        if pii_leak:
            # Warning-only (weak config): release output, signal via header so the
            # serialized body (which may be XML/NDJSON) is not mutated.
            headers["X-Medanon-Coverage-Warning"] = pii_leak.get("message", "")[:500]
        return Response(content=text, media_type=media_type, headers=headers)
    except HTTPException:
        raise  # let our 422 pii_leak_detected (and others) pass through
    except ProcessingError as exc:
        if exc.status == 422:
            raise HTTPException(status_code=422, detail="Invalid input") from exc
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except ValueError as exc:
        logger.warning("validation error in /process/raw: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid input") from exc
    except Exception as exc:
        # Do not use logger.exception — traceback may contain PHI
        logger.error(
            "Unexpected error in /process/raw: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(
            status_code=500, detail="Unexpected processing error"
        ) from exc


@router.post("/process/batch")
@limiter.limit(_RATE_BATCH)
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
        raise HTTPException(
            status_code=422, detail=f"Could not parse input: {exc}"
        ) from exc

    runtime_settings = _runtime_settings(settings)
    profile = _get_config_profile(runtime_settings)

    # Bundles are processed as a whole so cross-resource reference rewriting
    # (pre/post ID snapshot + _rewrite_references) is preserved.
    is_bundle = isinstance(payload, dict) and payload.get("resourceType") == "Bundle"

    # Pre-privacy Trust Gate barrier. Runs before streaming begins so a BLOCK
    # (in block mode) can return HTTP 422 before any bytes are sent. No-op unless
    # TRUST_GATE_SERVICE_URL is set. The passport is persisted with the run.
    if is_bundle:
        gate_resources = [
            e["resource"]
            for e in payload.get("entry", [])
            if isinstance(e, dict) and isinstance(e.get("resource"), dict)
        ]
    else:
        gate_resources = _unwrap_to_resources(payload)
    passport = await asyncio.to_thread(
        _run_intake_gate,
        gate_resources,
        profile,
        "/v1/process/batch",
        request.query_params.get("trust_profile"),
        _extract_full_urls(payload),
    )

    async def _generate():
        collector = make_collector(profile)
        count = 0
        t0 = time.monotonic()
        disconnected = False
        if is_bundle:
            async for line in _service.process_bundle_stream(payload, runtime_settings):
                if await request.is_disconnected():
                    logger.info("process_batch: client disconnected")
                    disconnected = True
                    break
                score_json_line(collector, line, runtime_settings)
                yield line + "\n"
                count += 1
        else:
            resources = _unwrap_to_resources(payload)
            async for line in _service.process_resource_stream(
                resources, runtime_settings
            ):
                if await request.is_disconnected():
                    logger.info("process_batch: client disconnected")
                    disconnected = True
                    break
                score_json_line(collector, line, runtime_settings)
                yield line + "\n"
                count += 1
        score = collector.aggregate() if collector else None
        pii_leak = apply_pii_leak_override(score) if score else None
        if not disconnected:
            yield stream_trailer(count, score, pii_leak=pii_leak) + "\n"
        if score is not None or passport:
            retain_task(
                persist_run(
                    endpoint="/v1/process/batch",
                    config_profile=profile,
                    resource_count=count,
                    error_count=score.get("error_count", 0) if score else 0,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    input_type="Bundle" if is_bundle else "batch",
                    summary={"total_resources": count},
                    score=score,
                    trust_passport=passport or None,
                )
            )

    return StreamingResponse(_generate(), media_type="application/x-ndjson")
