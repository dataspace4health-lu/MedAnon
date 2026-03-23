"""Analytics endpoints: /analyse/risk."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pipeline.io_formats import parse_payload_bytes
from analytics.risk import assess_risk_resources as _assess_risk_resources

from api.deps import MAX_BODY_BYTES, limiter, _unwrap_to_resources

router = APIRouter()
logger = logging.getLogger("medanon")


@router.post("/analyse/risk")
@limiter.limit("30/minute")
async def analyse_risk(request: Request):
    """Compute re-identification risk metrics on de-identified FHIR resources.

    Accepts NDJSON, JSON (single resource or Bundle), or XML input.
    Format is auto-detected from the Content-Type header.

    Patient resources supply quasi-identifiers (gender, birth year, zip prefix)
    for k-anonymity.  Condition resources are correlated via ``subject.reference``
    and used for l-diversity.  Other resource types are ignored.

    Returns a JSON risk report with:
    - k-anonymity (min_k, equivalence classes)
    - Prosecutor / journalist / marketer re-identification risk scores
    - Risk level: low (k>=5) / medium (k>=3) / high (k>=2) / critical (k=1)
    - l-diversity (if Condition resources are present)
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

    try:
        report = _assess_risk_resources(resources)
    except ValueError as exc:
        logger.warning("analyse_risk: value error: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("analyse_risk: unexpected error type=%s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Risk analysis error") from exc

    return JSONResponse(content=report)
