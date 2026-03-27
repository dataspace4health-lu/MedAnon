"""MedAnon FastAPI application — entry point.

Wires together middleware, health/status endpoints, and the four endpoint routers.
Business logic lives in api/routers/; shared dependencies in api/deps.py.
"""

import json
import logging
import os
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response

try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
except ImportError:
    CONTENT_TYPE_LATEST = "text/plain"
    def generate_latest():
        return b""

import pipeline.config as config
from utils.logging import REQUEST_ID, setup_logging
from utils.metrics import REQUEST_COUNT, REQUEST_LATENCY
from api.auth import get_auth_context, log_audit, OPEN_PATHS, ENDPOINT_ROLES
from api.deps import (
    MAX_BODY_BYTES,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    get_settings,
    limiter,
)
from api.routers import analytics, fhir_server, process, synthetic

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("medanon")

_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "MEDANON_CORS_ORIGINS", ""
).split(",") if o.strip()]

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="MedAnon", version="2.0.0")
app.state.limiter = limiter
if RateLimitExceeded is not None:
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Enforce API key auth + RBAC on protected endpoints."""
    if request.url.path in OPEN_PATHS:
        return await call_next(request)
    try:
        auth_ctx = get_auth_context(request)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    request.state.auth = auth_ctx
    required = ENDPOINT_ROLES.get(request.url.path)
    if required and not auth_ctx.has_role(required):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """Log every request to the structured audit log."""
    response = await call_next(request)
    auth = getattr(request.state, "auth", None)
    log_audit(request, response.status_code, auth)
    return response


@app.middleware("http")
async def enforce_body_size(request: Request, call_next):
    """Reject requests whose body exceeds MAX_BODY_BYTES.

    Checks Content-Length when present (fast path) and also streams
    chunked/unknown-length bodies to enforce the limit before the full
    payload is buffered into memory.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl_int = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
        if cl_int > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit",
            )
    elif request.method in ("POST", "PUT", "PATCH"):
        # No Content-Length header (chunked transfer) — read incrementally
        received = 0
        chunks = []
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit",
                )
            chunks.append(chunk)
        # Reassemble and stash so downstream `await request.body()` still works
        body = b"".join(chunks)

        async def receive():
            return {"type": "http.request", "body": body}

        request._receive = receive
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Propagate or generate an X-Request-ID for end-to-end tracing."""
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = REQUEST_ID.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        REQUEST_ID.reset(token)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Record per-endpoint request count and latency for Prometheus."""
    start = time.perf_counter()
    response = await call_next(request)
    REQUEST_LATENCY.labels(endpoint=request.url.path).observe(
        time.perf_counter() - start
    )
    REQUEST_COUNT.labels(
        endpoint=request.url.path,
        status_code=str(response.status_code),
    ).inc()
    return response


# ---------------------------------------------------------------------------
# Health / status endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def read_root(settings: config.Settings = Depends(get_settings)):
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    """Liveness probe — returns 200 when the process is alive."""
    return {"status": "ok"}


@app.get("/ready")
def readiness(request: Request):
    """Readiness probe — verifies that configured upstream services are reachable.

    Checks /metadata on gPAS and FHIR servers (when their URLs are configured).
    Returns 200 + {"ready": true} when all checks pass, 503 otherwise.
    Timeout is controlled by MEDANON_READY_TIMEOUT (default 5 s).

    When an API key is configured, unauthenticated callers receive only
    {"ready": true/false} without upstream service details to avoid
    leaking internal network topology.
    """
    import urllib.request as _ureq
    import urllib.error
    timeout = float(os.environ.get("MEDANON_READY_TIMEOUT", "5.0"))
    checks: dict[str, str] = {}

    gpas_url = os.environ.get("GPAS_URL", "")
    if gpas_url:
        try:
            _ureq.urlopen(gpas_url.rstrip("/"), timeout=timeout)
            checks["gpas"] = "ok"
        except urllib.error.HTTPError:
            # Any HTTP response (400, 401, etc.) means the service is reachable
            checks["gpas"] = "ok"
        except Exception as exc:
            logger.debug("readiness: gpas unreachable: %s", exc)
            checks["gpas"] = "error"

    fhir_url = os.environ.get("FHIR_SOURCE_URL", "")
    if fhir_url:
        try:
            _ureq.urlopen(f"{fhir_url.rstrip('/')}/metadata", timeout=timeout)
            checks["fhir"] = "ok"
        except Exception as exc:
            logger.debug("readiness: fhir unreachable: %s", exc)
            checks["fhir"] = "error"

    # NLP engine readiness (when Presidio/spaCy is expected)
    nlp_model = os.environ.get("MEDANON_NLP_MODEL", "")
    if nlp_model:
        try:
            from integrations.nlp.detector import _get_analyzer
            _get_analyzer()
            checks["nlp"] = "ok"
        except Exception as exc:
            logger.debug("readiness: nlp engine not ready: %s", exc)
            checks["nlp"] = "error"

    ready = all(v == "ok" for v in checks.values())

    # /ready is in OPEN_PATHS so auth_middleware never sets request.state.auth.
    # Resolve auth explicitly here: in open mode (no API key) everyone is admin;
    # with an API key only valid-key callers see the dependency details.
    from api.auth import get_auth_context as _get_auth_ctx
    try:
        _get_auth_ctx(request)
        caller_authenticated = True
    except Exception:
        caller_authenticated = False

    body: dict = {"ready": ready}
    if caller_authenticated:
        body["checks"] = checks

    return Response(
        content=json.dumps(body),
        status_code=200 if ready else 503,
        media_type="application/json",
    )


@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

app.include_router(process.router)
app.include_router(fhir_server.router)
app.include_router(analytics.router)
app.include_router(synthetic.router)
