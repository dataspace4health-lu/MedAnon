"""Synthetic data generation endpoint: /generate/synthetic.

Strangler Fig: when ANALYTICS_SERVICE_URL is set, requests are proxied to the
standalone analytics microservice. Otherwise, generation runs locally (default).
"""

from utils.json_fast import dumps as _json_dumps
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from api.deps import MAX_BODY_BYTES, limiter
from api.services.synthetic import SyntheticDataService

router = APIRouter()
logger = logging.getLogger("medanon")

_service = SyntheticDataService()


@router.post("/generate/synthetic")
@limiter.limit("30/minute")
async def generate_synthetic(
    request: Request,
    count: int = Query(
        100,
        ge=1,
        le=10000,
        description="Number of synthetic Patient resources to generate",
    ),
    seed: int | None = Query(None, description="Random seed for reproducibility"),
    engine: str = Query(
        "auto",
        description="Engine: 'sdv' (GaussianCopula), 'stdlib' (weighted sampling), or 'auto' (SDV if available, else stdlib)",
    ),
    include_conditions: bool = Query(
        False,
        description="Also generate synthetic Conditions linked to the synthetic Patients",
    ),
    count_per_patient: int = Query(
        2,
        ge=0,
        le=10,
        description="Max Conditions per Patient (when include_conditions=true)",
    ),
):
    """Generate synthetic FHIR Patient resources from a de-identified input dataset.

    Accepts de-identified FHIR resources (NDJSON, JSON Bundle, or XML),
    extracts statistical distributions, and returns *count* synthetic Patient
    resources (plus optional Conditions) as NDJSON.

    Engine choices:
    - **auto** (default): Use SDV if installed, otherwise fall back to stdlib.
    - **sdv**: GaussianCopulaSynthesizer — learns multivariate correlations.
    - **stdlib**: Weighted per-attribute sampling — zero dependencies, always available.

    Synthetic resources are tagged with the ``SYN`` code in ``meta.tag`` so
    downstream systems can distinguish them from real de-identified data.

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
        result = await _service.generate(
            body,
            content_type,
            count=count,
            seed=seed,
            engine=engine,
            include_conditions=include_conditions,
            count_per_patient=count_per_patient,
        )
    except ValueError as exc:
        msg = str(exc)
        if "proxy" in msg.lower() or "service" in msg.lower():
            raise HTTPException(status_code=502, detail=msg) from exc
        raise HTTPException(status_code=422, detail=msg) from exc
    except Exception as exc:
        logger.error(
            "generate_synthetic: unexpected error: %s",
            type(exc).__name__,
            exc_info=False,
        )
        raise HTTPException(
            status_code=500, detail="Synthetic generation error"
        ) from exc

    # Proxy returns raw bytes
    if isinstance(result, bytes):

        async def _passthrough():
            yield result

        return StreamingResponse(_passthrough(), media_type="application/x-ndjson")

    # Local generation returns SyntheticResult
    async def _stream():
        for patient in result.patients:
            yield _json_dumps(patient) + "\n"
        for condition in result.conditions:
            yield _json_dumps(condition) + "\n"

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Synthetic-Engine": result.engine_used},
    )
