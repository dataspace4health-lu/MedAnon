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
    output_format: str = Query(
        "ndjson",
        description="Output format: 'ndjson' (default), 'json' (Bundle), or 'xml'",
    ),
):
    """Generate synthetic FHIR Patient resources from a de-identified input dataset.

    Accepts de-identified FHIR resources (NDJSON, JSON Bundle, or XML),
    extracts statistical distributions, and returns *count* synthetic Patient
    resources (plus optional Conditions) in the requested output format.

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
            output_format=output_format,
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

    # Proxy returns raw bytes — pass through with correct content-type
    if isinstance(result, bytes):
        fmt = output_format.lower()
        if fmt == "json":
            proxy_ct = "application/json"
        elif fmt == "xml":
            proxy_ct = "application/fhir+xml"
        else:
            proxy_ct = "application/x-ndjson"

        async def _passthrough():
            yield result

        return StreamingResponse(_passthrough(), media_type=proxy_ct)

    # Local generation returns SyntheticResult
    from fastapi.responses import Response as _Response

    all_resources = list(result.patients) + list(result.conditions)
    fmt = output_format.lower()

    if fmt == "json":
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": r} for r in all_resources],
        }
        return _Response(
            content=_json_dumps(bundle),
            media_type="application/json",
            headers={"X-Synthetic-Engine": result.engine_used},
        )
    elif fmt == "xml":
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<Bundle xmlns="http://hl7.org/fhir"><type><value value="collection"/></type>']
        for r in all_resources:
            rt = r.get("resourceType", "Resource")
            lines.append(f'<entry><resource><{rt}>')
            for k, v in r.items():
                if k == "resourceType":
                    continue
                safe_v = str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f'<{k}><value value="{safe_v}"/></{k}>')
            lines.append(f'</{rt}></resource></entry>')
        lines.append('</Bundle>')
        return _Response(
            content="\n".join(lines),
            media_type="application/fhir+xml",
            headers={"X-Synthetic-Engine": result.engine_used},
        )

    async def _stream():
        for r in all_resources:
            yield _json_dumps(r) + "\n"

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Synthetic-Engine": result.engine_used},
    )
