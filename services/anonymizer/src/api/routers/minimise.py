"""Data minimisation endpoint: /minimise/assess (D7.2 §3).

Reads a FHIR payload and returns a Minimisation Report — direct/quasi identifier
classification, granularity recommendations, and (optionally) purpose-limitation
flags. Evaluate-and-recommend only; no transformation is performed, so this runs
locally regardless of any microservice split.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from api.deps import MAX_BODY_BYTES, limiter

router = APIRouter()
logger = logging.getLogger("medanon")


@router.post("/minimise/assess")
@limiter.limit("30/minute")
async def minimise_assess(
    request: Request,
    purpose_paths: str | None = Query(
        None,
        description="Comma-separated FHIRPaths justified by the declared purpose; "
        "findings outside these are flagged as unjustified (purpose limitation).",
    ),
):
    """Assess data minimisation for a FHIR payload (NDJSON / JSON / Bundle / XML)."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")

    declared = (
        [p.strip() for p in purpose_paths.split(",") if p.strip()]
        if purpose_paths is not None
        else None
    )

    try:
        from pipeline.io_formats import parse_payload_bytes
        from api.deps import _unwrap_to_resources
        from pipeline.minimization import assess_minimisation

        payload = parse_payload_bytes(body, content_type=content_type)
        resources = _unwrap_to_resources(payload)
        report = assess_minimisation(resources, declared_paths=declared)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Could not parse input: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("minimise_assess error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(
            status_code=500, detail="Minimisation assessment error"
        ) from exc

    return JSONResponse(content=report)
