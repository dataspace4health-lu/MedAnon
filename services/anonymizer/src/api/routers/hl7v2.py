"""HL7 v2 de-identification endpoints.

    POST /process/hl7v2        — de-identify a single HL7 v2 message
    POST /process/hl7v2/batch  — de-identify a batch of HL7 v2 messages
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api.deps import MAX_BODY_BYTES, limiter
from api.services.hl7v2 import Hl7v2Service

router = APIRouter()
logger = logging.getLogger("medanon")

_service = Hl7v2Service()


@router.post("/process/hl7v2")
@limiter.limit("30/minute")
async def process_hl7v2(request: Request):
    """De-identify a single HL7 v2 message.

    Accepts a raw HL7 v2 message as plain text (``\\r``, ``\\n``, or ``\\r\\n``
    segment separators are all accepted).  Returns the de-identified message
    as ``text/plain`` with ``\\n`` segment separators.

    PHI scrubbed: patient name, DOB, address, phone, SSN/MRN, next-of-kin,
    attending/referring/consulting physicians, ordering provider, and insured
    details (see ``pipeline.hl7v2_deidentify.HL7V2_SCRUB_FIELDS`` for the
    complete field list).
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )

    try:
        message_text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Request body must be UTF-8 encoded") from exc

    try:
        result = await _service.process_single(message_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "process_hl7v2: unexpected error: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="HL7 v2 processing error") from exc

    return Response(content=result, media_type="text/plain; charset=utf-8")


@router.post("/process/hl7v2/batch")
@limiter.limit("10/minute")
async def process_hl7v2_batch(request: Request):
    """De-identify a batch of concatenated HL7 v2 messages.

    Accepts multiple HL7 v2 messages concatenated together (each starting
    with ``MSH|``) as plain text.  Each message is de-identified individually
    and the results are returned as ``text/plain`` joined by newlines.

    An empty body returns an empty 200 response.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )

    try:
        batch_text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Request body must be UTF-8 encoded") from exc

    try:
        result = await _service.process_batch(batch_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "process_hl7v2_batch: unexpected error: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="HL7 v2 batch processing error") from exc

    return Response(content=result, media_type="text/plain; charset=utf-8")
