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
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

logging.basicConfig(level="INFO")
logger = logging.getLogger("nlp")

app = FastAPI(title="MedAnon NLP", version="1.0.0")

# ---------------------------------------------------------------------------
# Prometheus metrics (optional — degrades gracefully if package absent)
# ---------------------------------------------------------------------------

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

    _REQUESTS = Counter(
        "medanon_requests_total",
        "Total HTTP requests received by the NLP service",
        ["endpoint", "status_code", "medanon_service"],
    )
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False


def _inc_request(endpoint: str, status: int) -> None:
    if _PROM_AVAILABLE:
        _REQUESTS.labels(endpoint=endpoint, status_code=str(status), medanon_service="nlp").inc()


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


class BatchDetectRequest(BaseModel):
    items: list[BatchDetectItem]
    token_state: dict[str, Any] | None = None


class BatchDetectResponse(BaseModel):
    results: list[str]
    token_state: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/detect", response_model=DetectResponse)
def detect(req: DetectRequest):
    """Run Presidio NER on *text* and return the scrubbed result."""
    from detector import _analyze_and_replace, _resolve_entities

    entities = _resolve_entities(req.entities)
    token_state = req.token_state or {"next": {}, "map": {}, "reverse": {}}

    try:
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
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("detect_error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="NLP detection error") from exc

    _inc_request("/v1/detect", 200)
    return DetectResponse(scrubbed_text=scrubbed, token_state=token_state)


@app.post("/v1/detect/batch", response_model=BatchDetectResponse)
def detect_batch(req: BatchDetectRequest):
    """Run Presidio NER on multiple texts, sharing token state across them."""
    from detector import _analyze_and_replace, _resolve_entities

    token_state = req.token_state or {"next": {}, "map": {}, "reverse": {}}
    results: list[str] = []

    for item in req.items:
        entities = _resolve_entities(item.entities)
        try:
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
        except RuntimeError as exc:
            logger.error("presidio_not_ready: %s", exc, exc_info=False)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("detect_batch_error: %s", type(exc).__name__, exc_info=False)
            raise HTTPException(status_code=500, detail="NLP detection error") from exc

    _inc_request("/v1/detect/batch", 200)
    return BatchDetectResponse(results=results, token_state=token_state)
