"""Analytics endpoints: /analyse/risk, /analyse/privacy-risk.

Strangler Fig: when ANALYTICS_SERVICE_URL is set, requests are proxied to the
standalone analytics microservice. Otherwise, analytics runs locally (default).
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.deps import MAX_BODY_BYTES, limiter
from api.services.analytics import RiskAnalysisService

router = APIRouter()
logger = logging.getLogger("medanon")

_service = RiskAnalysisService()


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

    When ANALYTICS_SERVICE_URL is set, proxies to the analytics microservice.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")

    try:
        report = await _service.analyse_risk(body, content_type)
    except ValueError as exc:
        msg = str(exc)
        if "service" in msg.lower() or "proxy" in msg.lower():
            raise HTTPException(status_code=502, detail=msg) from exc
        if "parse" in msg.lower() or "Could not" in msg:
            raise HTTPException(
                status_code=422, detail=f"Could not parse input: {exc}"
            ) from exc
        raise HTTPException(status_code=422, detail=msg) from exc
    except Exception as exc:
        logger.error(
            "analyse_risk: unexpected error type=%s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="Risk analysis error") from exc

    return JSONResponse(content=report)


@router.post("/analyse/privacy-risk")
@limiter.limit("30/minute")
async def analyse_privacy_risk(request: Request):
    """Full privacy-risk report (TEHDAS2 D7.2 §5.5.7): re-identification +
    inference + record-level distance metrics.

    JSON body: ``{"resources": [...], "synthetic": [...]?, "privacy_model": {...}?}``.
    ``resources`` is the released/real dataset (FHIR resource dicts); when
    ``synthetic`` is supplied, DCR/NNDR/τ-DCR and attribute-inference (CAP)
    are additionally computed against it.

    When ``ANALYTICS_SERVICE_URL`` is set, proxies to the analytics microservice
    (this was previously reachable only on that microservice directly  this
    endpoint makes it a first-class part of the anonymizer's own API surface).
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")

    try:
        report = await _service.analyse_privacy_risk(body, content_type)
    except ValueError as exc:
        msg = str(exc)
        if "service" in msg.lower() or "proxy" in msg.lower():
            raise HTTPException(status_code=502, detail=msg) from exc
        raise HTTPException(status_code=422, detail=msg) from exc
    except Exception as exc:
        logger.error(
            "analyse_privacy_risk: unexpected error type=%s",
            type(exc).__name__,
            exc_info=False,
        )
        raise HTTPException(
            status_code=500, detail="Privacy-risk analysis error"
        ) from exc

    return JSONResponse(content=report)
