"""NLP microservice — POST /v1/detect.

Hosts Presidio + spaCy (en_core_web_lg) in isolation so the ~800 MB NLP
dependencies are not bundled into every anonymizer replica.

Single endpoint: POST /v1/detect
  Request:  {"text": "...", "entities": [...], "threshold": 0.4,
             "language": "en", "mode": "tokenize"}
  Response: {"scrubbed_text": "..."}

Opt-in Strangler Fig: the anonymizer delegates NLP detection to this service
when NLP_SERVICE_URL is set. Otherwise it runs detector.py locally (default).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

logging.basicConfig(level="INFO")
logger = logging.getLogger("nlp")

app = FastAPI(title="MedAnon NLP", version="1.0.0")

# Maximum number of items accepted by the batch endpoint. Prevents CPU/memory
# exhaustion — each Presidio+spaCy detection call is CPU-intensive. Configurable
# via NLP_MAX_BATCH_ITEMS; must stay in sync with MEDANON_STAGING_BATCH_SIZE.
_MAX_BATCH_ITEMS: int = int(os.environ.get("NLP_MAX_BATCH_ITEMS", "1000"))

# ---------------------------------------------------------------------------
# Prometheus metrics (optional — degrades gracefully if package absent)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _REQUESTS = Counter(
        "medanon_requests_total",
        "Total HTTP requests received by the NLP service",
        ["endpoint", "status_code", "medanon_service"],
    )
    _LATENCY = Histogram(
        "medanon_request_duration_seconds",
        "NLP service request latency in seconds",
        ["endpoint", "medanon_service"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    )
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False


def _inc_request(endpoint: str, status: int) -> None:
    if _PROM_AVAILABLE:
        _REQUESTS.labels(endpoint=endpoint, status_code=str(status), medanon_service="nlp").inc()


def _observe_latency(endpoint: str, duration: float) -> None:
    if _PROM_AVAILABLE:
        _LATENCY.labels(endpoint=endpoint, medanon_service="nlp").observe(duration)


@app.get("/metrics")
def metrics():
    if not _PROM_AVAILABLE:
        return Response("# prometheus_client not installed\n", media_type="text/plain")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DetectRequest(BaseModel):
    text: str
    entities: list[str] | str = "healthcare"
    threshold: float = 0.4
    language: str = "en"
    mode: str = "tokenize"
    # Per-call token state allows deterministic surrogate tokens across multiple
    # fields of the same resource when the caller passes state between requests.
    token_state: dict[str, Any] | None = None
    # When True, return raw detections without replacement.
    detect_only: bool = False


class DetectResponse(BaseModel):
    scrubbed_text: str
    # Return updated token state so callers can maintain cross-field consistency.
    token_state: dict[str, Any] = Field(default_factory=dict)


class BatchDetectItem(BaseModel):
    text: str
    entities: list[str] | str = "healthcare"
    threshold: float = 0.4
    language: str = "en"
    mode: str = "tokenize"
    detect_only: bool = False


class BatchDetectRequest(BaseModel):
    items: list[BatchDetectItem]
    token_state: dict[str, Any] | None = None


class BatchDetectResponse(BaseModel):
    results: list[str]
    token_state: dict[str, Any] = Field(default_factory=dict)
    detections: list[list] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _verify_detector_import():
    """Warm up Presidio/spaCy at startup and verify end-to-end detection works.

    A successful import alone does not confirm the spaCy model is loaded and
    functional.  Running a real detection call here surfaces broken model files
    before the service starts accepting traffic, so health checks are reliable.
    """
    try:
        from detector import _detect_entities_cached, _resolve_entities
        entities = tuple(_resolve_entities("healthcare"))
        # Minimal real detection — exercises the full Presidio + spaCy stack.
        _detect_entities_cached("John Smith DOB 1980-01-01", entities, 0.4, "en")
        logger.info("detector warm-up complete — Presidio/spaCy ready")
    except Exception as exc:
        logger.error("detector warm-up failed: %s", exc, exc_info=True)
        app.state.detector_ok = False
        return
    app.state.detector_ok = True


@app.get("/health")
def health():
    if not getattr(app.state, "detector_ok", False):
        return JSONResponse({"status": "error", "detail": "detector import failed"}, status_code=503)
    return {"status": "ok"}


@app.post("/v1/detect")
def detect(req: DetectRequest):
    """Run Presidio NER on *text* and return the scrubbed result.

    When ``detect_only=True``, return raw entity detections without replacement.
    """
    from detector import _analyze_and_replace, _detect_entities, _resolve_entities

    entities = _resolve_entities(req.entities)
    token_state = req.token_state or {"next": {}, "map": {}, "reverse": {}}
    _t0 = time.monotonic()

    try:
        if req.detect_only:
            hits = _detect_entities(
                req.text, tuple(entities), req.threshold, req.language
            )
            _inc_request("/v1/detect", 200)
            _observe_latency("/v1/detect", time.monotonic() - _t0)
            return {
                "scrubbed_text": req.text,
                "token_state": token_state,
                "detections": hits,
            }

        scrubbed = _analyze_and_replace(
            req.text,
            entities=entities,
            threshold=req.threshold,
            language=req.language,
            mode=req.mode,
            token_state=token_state,
            token_lock=None,
        )
    except RuntimeError as exc:
        logger.error("presidio_not_ready: %s", exc, exc_info=False)
        app.state.detector_ok = False  # reflect degraded state in /health
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("detect_error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="NLP detection error") from exc

    _inc_request("/v1/detect", 200)
    _observe_latency("/v1/detect", time.monotonic() - _t0)
    return DetectResponse(scrubbed_text=scrubbed, token_state=token_state)


@app.post("/v1/detect/batch")
def detect_batch(req: BatchDetectRequest):
    """Run Presidio NER on multiple texts, sharing token state across them.

    When all items have ``detect_only=True``, detection runs in a thread pool
    (spaCy releases the GIL for tokenisation and most pipeline components),
    giving ~2-4× speedup on multi-core hosts compared to sequential processing.
    Items with ``detect_only=False`` are processed sequentially so that token
    state mutations remain consistent.
    """
    if len(req.items) > _MAX_BATCH_ITEMS:
        raise HTTPException(
            status_code=422,
            detail=f"Batch too large: {len(req.items)} items exceeds limit of {_MAX_BATCH_ITEMS}",
        )

    from detector import _analyze_and_replace, _detect_entities, _resolve_entities

    token_state = req.token_state or {"next": {}, "map": {}, "reverse": {}}
    any_detect_only = any(item.detect_only for item in req.items)
    all_detect_only = all(item.detect_only for item in req.items)
    all_detections: list[list] | None = [] if any_detect_only else None
    _t0 = time.monotonic()

    # Fast path: all items are detect-only — no shared mutable token_state,
    # so we can run detections in parallel using a thread pool.
    if all_detect_only:
        from concurrent.futures import ThreadPoolExecutor

        def _detect_one(item: BatchDetectItem) -> list:
            entities = _resolve_entities(item.entities)
            try:
                return list(_detect_entities(
                    item.text, tuple(entities), item.threshold, item.language
                ))
            except RuntimeError as exc:
                logger.error("presidio_not_ready: %s", exc, exc_info=False)
                app.state.detector_ok = False
                raise
            except Exception as exc:
                logger.error("detect_batch_error: %s", type(exc).__name__, exc_info=False)
                raise HTTPException(status_code=500, detail="NLP detection error") from exc

        _workers = min(len(req.items), int(os.environ.get("NLP_BATCH_THREADS", "4")))
        with ThreadPoolExecutor(max_workers=_workers) as pool:
            hits_list = list(pool.map(_detect_one, req.items))

        results = [item.text for item in req.items]
        _inc_request("/v1/detect/batch", 200)
        _observe_latency("/v1/detect/batch", time.monotonic() - _t0)
        return {"results": results, "token_state": token_state, "detections": hits_list}

    # Slow path: mix of detect_only and analyze_and_replace — token_state must
    # flow sequentially to keep surrogate tokens consistent within the resource.
    results: list[str] = []
    for item in req.items:
        entities = _resolve_entities(item.entities)
        try:
            if item.detect_only:
                hits = _detect_entities(
                    item.text, tuple(entities), item.threshold, item.language
                )
                results.append(item.text)
                if all_detections is not None:
                    all_detections.append(hits)
            else:
                scrubbed = _analyze_and_replace(
                    item.text,
                    entities=entities,
                    threshold=item.threshold,
                    language=item.language,
                    mode=item.mode,
                    token_state=token_state,
                    token_lock=None,
                )
                results.append(scrubbed)
                if all_detections is not None:
                    all_detections.append([])
        except RuntimeError as exc:
            logger.error("presidio_not_ready: %s", exc, exc_info=False)
            app.state.detector_ok = False  # reflect degraded state in /health
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("detect_batch_error: %s", type(exc).__name__, exc_info=False)
            raise HTTPException(status_code=500, detail="NLP detection error") from exc

    _inc_request("/v1/detect/batch", 200)
    _observe_latency("/v1/detect/batch", time.monotonic() - _t0)
    response = {"results": results, "token_state": token_state}
    if all_detections is not None:
        response["detections"] = all_detections
    return response
