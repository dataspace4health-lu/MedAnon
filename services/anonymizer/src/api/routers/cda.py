"""CDA (HL7 v3) de-identification endpoint.

POST /process/cda — de-identify a single CDA / CCDA document through the full
rule engine (CdaAdapter → process_data_batch → validation barrier).
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api.deps import MAX_BODY_BYTES, limiter
from api.services.cda import CdaService
from pipeline.exceptions import NormalizationError
from pipeline.processor import PiiLeakError

router = APIRouter()
logger = logging.getLogger("medanon")

_service = CdaService()


@router.post("/process/cda")
@limiter.limit("30/minute")
async def process_cda(request: Request):
    """De-identify a single CDA / CCDA document.

    Accepts CDA XML as the request body.  Patient demographics are mapped to
    the FHIR engine, de-identified per the (optional) ``?config_profile=``
    (default ``auto``), and written back into the document.  Returns the
    de-identified CDA XML.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    if not body:
        raise HTTPException(status_code=422, detail="Request body is empty")

    try:
        xml_text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422, detail="Request body must be UTF-8 encoded"
        ) from exc

    config_profile = request.query_params.get("config_profile") or "auto"

    try:
        result = await _service.process_single(xml_text, config_profile)
    except PiiLeakError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "pii_leak_detected", "message": str(exc)},
        ) from exc
    except NormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "process_cda: unexpected error: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="CDA processing error") from exc

    return Response(content=result, media_type="application/xml; charset=utf-8")
