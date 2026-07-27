"""Core processing endpoints: /process, /process/ndjson, /process/raw, /process/batch."""

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

import pipeline.config as config
from pipeline.io_formats import parse_payload_bytes, serialize_payload
from utils.permit_context import permit_scope
from utils.tasks import retain_task

from api.deps import (
    MAX_BODY_BYTES,
    get_settings_dep,
    limiter,
    resolve_active_permit,
    _runtime_settings,
    _extract_full_urls,
    _unwrap_to_resources,
    _unwrap_parameters_payload,
    _validate_dynamic_settings,
)
from pipeline.intake_gate import IntakeBlocked, enforce_intake
from api.services import stream_trailer
from api.services.processing import ProcessingError, ProcessingService
from pipeline.scoring_helpers import (
    _is_scoring_enabled,
    _get_config_profile,
    attach_audit_report,
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


def _split_interactive_lines(lines: list[str]) -> tuple[str, str]:
    """Split the streamed NDJSON into clean data + a manifest sidecar.

    Released data must never carry the transformation manifest inline  it ships
    as a separate artifact. Parses each de-identified line, extracts the manifest
    from ``meta.tag`` (server-side, authoritative), strips it from the resource,
    and returns ``(clean_ndjson, manifest_ndjson)``. Unparseable/error lines pass
    through to the data unchanged and contribute no manifest line.
    """
    from pipeline.manifest import extract_manifest_entries, strip_manifest_tag
    from utils.json_fast import dumps as _dumps, loads as _loads

    clean: list[str] = []
    manifest: list[str] = []
    for line in lines:
        try:
            resource = _loads(line)
        except (ValueError, TypeError):
            clean.append(line)
            continue
        if isinstance(resource, dict) and "error" not in resource:
            entries = extract_manifest_entries(resource)
            strip_manifest_tag(resource)
            manifest.append(
                _dumps(
                    {
                        "resourceType": resource.get("resourceType", "Unknown"),
                        "id": resource.get("id"),
                        "rules": entries,
                    }
                )
            )
            clean.append(_dumps(resource))
        else:
            clean.append(line)
    tail = "\n" if clean else ""
    return "\n".join(clean) + tail, ("\n".join(manifest) + ("\n" if manifest else ""))


async def _deliver_interactive(
    destination_id: str,
    lines: list[str],
    profile: str,
    score: dict | None,
    count: int,
) -> None:
    """Deliver the interactive de-identified output to S3 (best-effort).

    The browser download is the primary result; this is an opt-in side delivery,
    so a failure is logged rather than surfaced (the client already has the data).
    Delivers three correlated artifacts under distinct prefixes (data/, manifests/,
    audit/) with a shared generated stem  the manifest is split OUT of the data
    so the released clinical data never carries it inline (matches the job path).
    """
    import uuid
    from types import SimpleNamespace

    from integrations.storage.delivery import deliver_content
    from pipeline.jobs.audit_artifact import build_audit

    stem = uuid.uuid4().hex
    clean_ndjson, manifest_ndjson = _split_interactive_lines(lines)
    job_like = SimpleNamespace(
        id=stem,
        type="process-batch",
        params={"config_profile": profile, "destination_id": destination_id},
        status=None,
        created_at=None,
        checkpoint_data=None,
    )
    try:
        delivered: dict[str, str] = {}
        delivered["data"] = await asyncio.to_thread(
            deliver_content,
            clean_ndjson,
            suffix=".ndjson",
            destination_id=destination_id,
            key_id=stem,
            artifact="data",
        )
        if manifest_ndjson:
            delivered["manifest"] = await asyncio.to_thread(
                deliver_content,
                manifest_ndjson,
                suffix=".manifest.ndjson",
                destination_id=destination_id,
                key_id=stem,
                artifact="manifest",
            )
        summary = {"total_resources": count, "config_profile": profile, "score": score}
        audit = build_audit(job_like, summary=summary, delivered=delivered)
        await asyncio.to_thread(
            deliver_content,
            json.dumps(audit),
            suffix=".audit.json",
            destination_id=destination_id,
            key_id=stem,
            artifact="audit",
        )
        logger.info(
            "process_batch_delivered stem=%s artifacts=%s", stem, list(delivered)
        )
    except Exception:
        logger.warning(
            "process_batch_delivery_failed stem=%s dest=%s",
            stem,
            destination_id,
            exc_info=True,
        )


_PERMIT_ID_DESC = (
    "Active data-permit id (TEHDAS2 D7.2 §4.4) scoping pseudonymisation keys "
    "and gPAS domains to this permit, so the same subject cannot be linked "
    "across permits. Required in regulated mode for any profile using "
    "cryptohash/tokenize/date_shift/gpas_pseudonymize/gpas_depseudonymize."
)


def _attach_pii_warning(result, pii_leak: dict) -> None:
    """Embed a non-blocking coverage warning into a JSON result for the client.

    Used when ``MEDANON_GATE_IDENTIFIER_MODE=warn`` and the only finding is a
    config-coverage gap (no detected PII). The output is released; the warning
    rides along under ``meta.tag`` so the UI can surface "weak config" without
    a 422 block. Mutates dict resources only  Bundles attach to the Bundle root.
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
    permit_id: str | None = Query(None, description=_PERMIT_ID_DESC),
):
    """Process a single FHIR resource or a FHIR Bundle.

    Accepts any valid FHIR JSON object or a JSON array of resources.
    Applies the configured rules and returns the pseudonymized/de-identified result.
    """
    resolve_active_permit(permit_id)
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
        with permit_scope(permit_id):
            result = await _service.process_resource(resource, runtime_settings)
    except ProcessingError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    pii_leak = None
    if _is_scoring_enabled():
        pii_leak = await check_and_persist_with_leak(
            result, "/v1/process", runtime_settings, t0, trust_passport=passport or None
        )
    elif passport:
        # Scoring off but the gate produced a passport  persist it on its own so
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
        # Real PII detected  block output entirely, return 422 with leak info.
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
    permit_id: str | None = Query(None, description=_PERMIT_ID_DESC),
):
    """Process a stream of newline-delimited FHIR resources (NDJSON / x-ndjson).

    Each non-empty line must be a valid JSON object representing a FHIR resource.
    Lines starting with '//' are treated as comments and skipped.
    Returns a streaming NDJSON response  one processed JSON object per line.
    """
    resolve_active_permit(permit_id)
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
        with permit_scope(permit_id):
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


# Accepted NDJSON content types (only the *type* is checked  no size limit).
_NDJSON_CTYPES = frozenset(
    {
        "application/x-ndjson",
        "application/ndjson",
        "application/octet-stream",
        "text/plain",
        "",
    }
)

# Optional safety valve for /process/stream. 0 (default) = unlimited. When set,
# a request over the cap is rejected up front (413) if it declares a large
# Content-Length, and truncated with an explicit error line if it streams past
# the cap without one (chunked upload).
_STREAM_MAX_BYTES = int(os.environ.get("MEDANON_STREAM_MAX_BYTES", "0"))


@router.post("/process/stream")
@limiter.limit(_RATE_NDJSON)
async def process_stream(
    request: Request,
    settings: config.Settings = Depends(get_settings_dep),
    permit_id: str | None = Query(None, description=_PERMIT_ID_DESC),
):
    """De-identify NDJSON as a true stream  **no request-body size limit**.

    Reads the request body incrementally (never buffering the whole payload) and
    streams de-identified NDJSON back, one processed resource per line in input
    order. Only ``MEDANON_BATCH_SIZE`` resources are held at a time, so the input
    may be arbitrarily large (millions of resources in one request).

    Only the ``Content-Type`` is validated (must be NDJSON); this endpoint is
    exempt from the global body-size guard. Stream the file with a chunked / large
    upload, e.g. ``curl --data-binary @big.ndjson -H 'Content-Type: application/x-ndjson'``.

    Note: the always-on output PII barrier still runs per resource (fail-closed).
    The opt-in pre-privacy Trust Gate and score trailer are skipped here  they
    require buffering the whole input, which this endpoint deliberately avoids.
    """
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in _NDJSON_CTYPES:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be NDJSON (application/x-ndjson).",
        )
    # Optional cap: reject up front when the declared size already exceeds it.
    if _STREAM_MAX_BYTES:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _STREAM_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Body exceeds MEDANON_STREAM_MAX_BYTES ({_STREAM_MAX_BYTES})",
            )
    resolve_active_permit(permit_id)
    runtime_settings = _runtime_settings(settings)

    # Mutable state shared with the line reader: total bytes + truncation flag.
    stream_state = {"bytes": 0, "truncated": False}

    async def _lines():
        """Yield UTF-8 lines from the raw request stream (bounded buffer).

        Enforces the optional MEDANON_STREAM_MAX_BYTES cap on the fly; once the
        response has started streaming a status code can no longer change, so an
        over-cap chunked upload is stopped and flagged for an error trailer.
        """
        buf = bytearray()
        async for chunk in request.stream():
            stream_state["bytes"] += len(chunk)
            if _STREAM_MAX_BYTES and stream_state["bytes"] > _STREAM_MAX_BYTES:
                stream_state["truncated"] = True
                return
            buf.extend(chunk)
            start = 0
            while True:
                nl = buf.find(b"\n", start)
                if nl < 0:
                    break
                yield bytes(buf[start:nl]).decode("utf-8", "replace")
                start = nl + 1
            del buf[:start]
        if buf.strip():
            yield bytes(buf).decode("utf-8", "replace")

    async def _generate():
        # Note: no request.is_disconnected() poll here  it reads the same ASGI
        # ``receive`` channel as request.stream() and would corrupt the input.
        # StreamingResponse already aborts the generator on client disconnect.
        with permit_scope(permit_id):
            async for line in _service.process_ndjson_stream(
                _lines(), runtime_settings
            ):
                yield line + "\n"
        if stream_state["truncated"]:
            yield (
                json.dumps(
                    {
                        "error": "stream_max_bytes_exceeded",
                        "limit_bytes": _STREAM_MAX_BYTES,
                    }
                )
                + "\n"
            )

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@router.post("/process/raw")
@limiter.limit(_RATE_RAW)
async def process_raw(
    request: Request,
    output_format: str = "json",
    input_format: str = "auto",
    settings: config.Settings = Depends(get_settings_dep),
    permit_id: str | None = Query(None, description=_PERMIT_ID_DESC),
):
    """Black-box endpoint supporting JSON, XML, and NDJSON input/output.

    Input detection:
    - input_format=auto uses Content-Type to infer json/xml/ndjson
    - input_format can be forced to json/xml/ndjson

    Output:
    - output_format in {json, xml, ndjson}
    """
    resolve_active_permit(permit_id)
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
        with permit_scope(permit_id):
            result = await _service.process_resource(payload, runtime_settings)

        pii_leak = None
        if _is_scoring_enabled():
            pii_leak = await check_and_persist_with_leak(
                result,
                "/v1/process/raw",
                runtime_settings,
                t0,
                trust_passport=passport or None,
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
            # Real PII detected  block output, return 422 with pii_leak payload.
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
        # Do not use logger.exception  traceback may contain PHI
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
    permit_id: str | None = Query(None, description=_PERMIT_ID_DESC),
    destination_id: str | None = Query(
        None,
        description="When set, also deliver the de-identified data + audit record "
        "to this saved S3 destination (distinct data/ and audit/ prefixes, shared "
        "stem). The browser download is unaffected.",
    ),
):
    """Process FHIR resources in any format (JSON, NDJSON, XML) and stream NDJSON output.

    Accepts:
    - ``application/x-ndjson``  one resource per line (same as /process/ndjson)
    - ``application/json`` / ``application/fhir+json``  single resource or Bundle
    - ``application/xml`` / ``application/fhir+xml``  single resource or Bundle

    Bundles are processed as a whole to preserve cross-resource reference
    rewriting, then each entry resource is streamed as NDJSON.
    Non-Bundle inputs are unwrapped and streamed individually.
    """
    resolve_active_permit(permit_id)
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
        # Accumulate the de-identified output only when an S3 delivery was
        # requested (single-patient scale  bounded). No buffering otherwise.
        delivered_lines: list[str] | None = [] if destination_id else None
        with permit_scope(permit_id):
            if is_bundle:
                async for line in _service.process_bundle_stream(
                    payload, runtime_settings
                ):
                    if await request.is_disconnected():
                        logger.info("process_batch: client disconnected")
                        disconnected = True
                        break
                    score_json_line(collector, line, runtime_settings)
                    if delivered_lines is not None:
                        delivered_lines.append(line)
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
                    if delivered_lines is not None:
                        delivered_lines.append(line)
                    yield line + "\n"
                    count += 1
        score = collector.aggregate() if collector else None
        pii_leak = apply_pii_leak_override(score) if score else None
        if destination_id and not disconnected and delivered_lines is not None:
            await _deliver_interactive(
                destination_id, delivered_lines, profile, score, count
            )
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
                    score=attach_audit_report(
                        score, collector, settings=runtime_settings
                    ),
                    trust_passport=passport or None,
                )
            )

    return StreamingResponse(_generate(), media_type="application/x-ndjson")
