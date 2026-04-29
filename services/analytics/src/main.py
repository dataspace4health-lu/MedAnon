"""Analytics microservice — /v1/analyse/risk and /v1/generate/synthetic.

Lightweight service containing only analytics logic. No gPAS, no Presidio,
no fhirpathpy. Typical image size: ~200 MB vs the 1.5 GB anonymizer monolith.

Opt-in Strangler Fig: the anonymizer proxies to this service when
ANALYTICS_SERVICE_URL is set; otherwise runs analytics locally. Default
behaviour of the anonymizer is completely unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(level="INFO")
logger = logging.getLogger("analytics")

app = FastAPI(title="MedAnon Analytics", version="1.0.0")

# ---------------------------------------------------------------------------
# Prometheus metrics (optional — degrades gracefully if package absent)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _REQUESTS = Counter(
        "medanon_requests_total",
        "Total HTTP requests received by the analytics service",
        ["endpoint", "status_code", "medanon_service"],
    )
    _LATENCY = Histogram(
        "medanon_request_duration_seconds",
        "Analytics service request latency in seconds",
        ["endpoint", "medanon_service"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    )
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False


def _inc_request(endpoint: str, status: int) -> None:
    if _PROM_AVAILABLE:
        _REQUESTS.labels(endpoint=endpoint, status_code=str(status), medanon_service="analytics").inc()


def _observe_latency(endpoint: str, duration: float) -> None:
    if _PROM_AVAILABLE:
        _LATENCY.labels(endpoint=endpoint, medanon_service="analytics").observe(duration)


@app.get("/metrics")
def metrics():
    if not _PROM_AVAILABLE:
        return Response("# prometheus_client not installed\n", media_type="text/plain")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Minimal body parser (NDJSON + JSON — no XML, no fhirpathpy)
# ---------------------------------------------------------------------------

def _parse_body(body: bytes, content_type: str) -> list[dict]:
    """Parse NDJSON or JSON body into a flat list of FHIR resource dicts."""
    ct = content_type.lower()
    if "ndjson" in ct or "x-ndjson" in ct:
        return [
            json.loads(line)
            for line in body.splitlines()
            if line.strip()
        ]
    data = json.loads(body)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if data.get("resourceType") == "Bundle":
            return [e["resource"] for e in data.get("entry", []) if "resource" in e]
        return [data]
    return []


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Risk analysis
# ---------------------------------------------------------------------------

@app.post("/v1/analyse/risk")
async def analyse_risk(request: Request):
    """Compute re-identification risk metrics on de-identified FHIR resources."""
    from risk import assess_risk_resources

    body = await request.body()
    content_type = request.headers.get("content-type", "")
    _t0 = time.monotonic()
    try:
        resources = _parse_body(body, content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    try:
        report = await asyncio.to_thread(assess_risk_resources, resources)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("analyse_risk error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Risk analysis error") from exc

    _inc_request("/v1/analyse/risk", 200)
    _observe_latency("/v1/analyse/risk", time.monotonic() - _t0)
    return JSONResponse(content=report)


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------

@app.post("/v1/generate/synthetic")
async def generate_synthetic(
    request: Request,
    count: int = Query(100, ge=1, le=10000),
    seed: int | None = Query(None),
    engine: str = Query("auto"),
    include_conditions: bool = Query(False),
    count_per_patient: int = Query(2, ge=0, le=10),
):
    """Generate synthetic FHIR Patient resources from a de-identified dataset."""
    try:
        from synthetic_sdv import (
            SDV_AVAILABLE,
            generate_synthetic_patients_sdv as _gen_patients_sdv,
            generate_synthetic_conditions_sdv as _gen_conditions_sdv,
        )
    except ImportError:
        SDV_AVAILABLE = False

    from synthetic import generate_synthetic_patients, generate_synthetic_conditions

    body = await request.body()
    content_type = request.headers.get("content-type", "")
    try:
        resources = _parse_body(body, content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    patients = [r for r in resources if r.get("resourceType") == "Patient"]
    if not patients:
        raise HTTPException(
            status_code=422,
            detail="No Patient resources found — provide de-identified Patient FHIR resources",
        )

    use_sdv = False
    if engine == "sdv":
        if not SDV_AVAILABLE:
            raise HTTPException(
                status_code=422,
                detail="SDV engine requested but sdv package is not installed",
            )
        use_sdv = True
    elif engine == "auto":
        use_sdv = SDV_AVAILABLE
    elif engine != "stdlib":
        raise HTTPException(status_code=422, detail=f"Unknown engine '{engine}'. Choose: auto, sdv, stdlib")

    _t0 = time.monotonic()
    try:
        if use_sdv:
            synthetic = await asyncio.to_thread(
                _gen_patients_sdv, patients, count=count, seed=seed
            )
        else:
            synthetic = await asyncio.to_thread(
                generate_synthetic_patients, patients, count=count, seed=seed
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("generate_synthetic error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Synthetic generation error") from exc

    synthetic_conditions: list[dict] = []
    if include_conditions:
        conditions = [r for r in resources if r.get("resourceType") == "Condition"]
        if conditions:
            try:
                if use_sdv:
                    synthetic_conditions = await asyncio.to_thread(
                        _gen_conditions_sdv, conditions, synthetic,
                        count_per_patient=count_per_patient, seed=seed,
                    )
                else:
                    synthetic_conditions = await asyncio.to_thread(
                        generate_synthetic_conditions, conditions, synthetic,
                        count_per_patient=count_per_patient, seed=seed,
                    )
            except ValueError:
                pass

    engine_used = "sdv" if use_sdv else "stdlib"

    async def _stream():
        for patient in synthetic:
            yield json.dumps(patient) + "\n"
        for condition in synthetic_conditions:
            yield json.dumps(condition) + "\n"

    _inc_request("/v1/generate/synthetic", 200)
    _observe_latency("/v1/generate/synthetic", time.monotonic() - _t0)
    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Synthetic-Engine": engine_used},
    )
