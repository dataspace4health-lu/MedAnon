"""Dataset-discovery endpoint: /catalog/descriptor (D7.2 §4.3, EHDS Art 55/78).

Emits a HealthDCAT-AP JSON-LD ``dcat:Dataset`` descriptor for a released dataset,
optionally enriched from its Transformation Passport (inline, or fetched by
``job_id`` from the durable report store). Discovery metadata only; no PHI.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.deps import MAX_BODY_BYTES, limiter

router = APIRouter()
logger = logging.getLogger("medanon")


@router.post("/catalog/descriptor")
@limiter.limit("30/minute")
async def catalog_descriptor(request: Request):
    """Build a HealthDCAT-AP descriptor.

    JSON body: ``{"dataset": {...}, "passport": {...}?, "job_id": "..."?}``.
    ``dataset.title`` is required. When ``job_id`` is given and no inline
    ``passport`` is supplied, the stored passport for that job is used.
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

    dataset = payload.get("dataset")
    if not isinstance(dataset, dict):
        raise HTTPException(status_code=422, detail="dataset object is required")

    passport = payload.get("passport")
    job_id = payload.get("job_id")
    if passport is None and job_id:
        from pipeline.reports import get_passport_store

        store = get_passport_store()
        if store is not None:
            try:
                passport = store.get(str(job_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("catalog passport fetch failed: %s", type(exc).__name__)

    try:
        from pipeline.governance.healthdcat import build_healthdcat_descriptor

        descriptor = build_healthdcat_descriptor(dataset=dataset, passport=passport)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("catalog_descriptor error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(
            status_code=500, detail="Descriptor generation error"
        ) from exc

    return JSONResponse(content=descriptor, media_type="application/ld+json")
