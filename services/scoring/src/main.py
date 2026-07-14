"""Scoring microservice — composite privacy/utility/quality scoring.

Stateless HTTP service mirroring the analytics/NLP extraction pattern.
Exposes:
- ``POST /v1/score`` — score a single de-identified resource
- ``POST /v1/score/batch`` — score a list of resources
- ``GET /health`` — liveness
- ``GET /metrics`` — Prometheus exposition

The anonymizer routes scoring through this service when ``SCORING_SERVICE_URL``
is set in its environment. On failure the anonymizer falls back to local
in-process scoring.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import Response

from engine import score_resource

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
_log = logging.getLogger("scoring")

app = FastAPI(
    title="Scoring Service",
    version="1.0.0",
    description="Composite privacy/utility/quality scoring for FHIR resources.",
)

REQUESTS = Counter(
    "scoring_requests_total",
    "Number of /v1/score requests by outcome",
    ["endpoint", "outcome"],
)
LATENCY = Histogram(
    "scoring_request_seconds",
    "Latency of scoring requests",
    ["endpoint"],
)


class ScoreRequest(BaseModel):
    original: dict[str, Any] | None = None
    deidentified: dict[str, Any] = Field(...)
    manifest_entries: list[dict[str, Any]] = Field(default_factory=list)
    config_profile: str = "auto"
    error_count: int = 0
    total_count: int = 1


class BatchScoreRequest(BaseModel):
    items: list[ScoreRequest] = Field(..., max_length=10000)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Readiness probe — scoring is stateless so /ready mirrors /health.

    Kept as a separate endpoint so K8s startup vs liveness probes can be
    tuned independently and so dashboards can distinguish "process up" from
    "can serve traffic".
    """
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/score")
def score_endpoint(req: ScoreRequest) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        result = score_resource(
            original=req.original,
            deidentified=req.deidentified,
            manifest_entries=req.manifest_entries,
            settings=None,
            config_profile=req.config_profile,
            error_count=req.error_count,
            total_count=req.total_count,
        )
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="score", outcome="error").inc()
        _log.exception("scoring failed")
        raise HTTPException(status_code=500, detail=f"scoring failed: {exc}") from exc

    REQUESTS.labels(endpoint="score", outcome="ok").inc()
    LATENCY.labels(endpoint="score").observe(time.monotonic() - t0)
    return result.to_dict()


@app.post("/v1/score/batch")
def score_batch_endpoint(req: BatchScoreRequest) -> dict[str, Any]:
    t0 = time.monotonic()
    results: list[dict[str, Any]] = []
    errors = 0
    for item in req.items:
        try:
            r = score_resource(
                original=item.original,
                deidentified=item.deidentified,
                manifest_entries=item.manifest_entries,
                settings=None,
                config_profile=item.config_profile,
                error_count=item.error_count,
                total_count=item.total_count,
            )
            results.append(r.to_dict())
        except Exception:  # noqa: BLE001
            errors += 1
            results.append({"error": "scoring_failed"})

    REQUESTS.labels(
        endpoint="score_batch", outcome="ok" if errors == 0 else "partial"
    ).inc()
    LATENCY.labels(endpoint="score_batch").observe(time.monotonic() - t0)
    return {"count": len(results), "errors": errors, "results": results}
