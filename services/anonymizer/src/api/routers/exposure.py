"""Cumulative-exposure endpoint: /exposure/assess (D7.2 §5.5.7).

Assess a release's population against prior releases to the same permit /
recipient for repeat exposure and differencing risk, and optionally record it in
the durable ledger. Input ids are pseudonymous; only a keyed one-way fingerprint
is ever stored.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from api.deps import MAX_BODY_BYTES, limiter

router = APIRouter()
logger = logging.getLogger("medanon")


@router.post("/exposure/assess")
@limiter.limit("30/minute")
async def exposure_assess(request: Request):
    """Assess (and optionally record) cumulative exposure for a release.

    JSON body: ``{"subject_ids": [...], "permit_id": "...", "recipient": "...",
    "qi_signature": [...]|"...", "release_id": "...", "record": false}``. One of
    ``permit_id`` / ``recipient`` is required to scope the comparison.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )

    try:
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Could not parse input: {exc}"
        ) from exc

    subject_ids = payload.get("subject_ids") or []
    if not isinstance(subject_ids, list):
        raise HTTPException(status_code=422, detail="subject_ids must be a list")

    try:
        from api.services.exposure import assess_and_record

        result = assess_and_record(
            subject_ids=[str(s) for s in subject_ids],
            permit_id=payload.get("permit_id"),
            recipient=payload.get("recipient"),
            qi_signature=payload.get("qi_signature", ""),
            release_id=payload.get("release_id"),
            record=bool(payload.get("record", False)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("exposure_assess error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(
            status_code=500, detail="Cumulative-exposure error"
        ) from exc

    from fastapi.responses import JSONResponse

    return JSONResponse(content=result)
