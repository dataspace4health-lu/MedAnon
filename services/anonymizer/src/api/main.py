"""MedAnon FastAPI application — entry point.

Wires together middleware, health/status endpoints, and the four endpoint routers.
Business logic lives in api/routers/; shared dependencies in api/deps.py.
"""

import asyncio
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
from api.auth import get_auth_context, get_required_role, log_audit, OPEN_PATHS
from api.deps import (
    MAX_BODY_BYTES,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    get_settings,
    limiter,
)
from api.routers import analytics, configs, dicom, fhir_bulk, fhir_server, hl7v2, jobs, process, synthetic
from api.routers import fhir_subscriptions, smart

# Refresh fhir_bulk's module-level URL cache so that re-imports (e.g. during
# TestClient construction with patched env) capture the current FHIR_SOURCE_URL.
fhir_bulk._FHIR_SOURCE_URL = os.environ.get("FHIR_SOURCE_URL", "").strip()

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("medanon")

_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "MEDANON_CORS_ORIGINS", ""
).split(",") if o.strip()]

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="MedAnon", version="2.0.0")


@app.on_event("startup")
async def _startup() -> None:
    # Opt-in Redis L2 cache for cross-replica gPAS result sharing
    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    if redis_url:
        try:
            from utils.cache import RedisCache, configure_cache
            configure_cache(RedisCache(redis_url))
            logger.info("gpas_cache=redis")
        except Exception as exc:
            logger.warning("redis_cache_setup_failed falling_back=local: %s", exc)

    # Async job queue — select backend based on MEDANON_REDIS_URL
    max_concurrent = int(os.environ.get("MEDANON_JOB_WORKERS", "3"))
    try:
        from pipeline.jobs import init_job_store
        from pipeline.jobs import worker as _worker

        job_store = None
        if redis_url:
            try:
                from integrations.redis.job_store import RedisJobStore
                job_store = RedisJobStore(redis_url)
                logger.info("job_store=redis")
            except Exception as exc:
                logger.warning("redis_job_store_failed falling_back=sqlite: %s", exc)

        store = init_job_store(store=job_store)
        _worker.init_worker(store, max_concurrent=max_concurrent)

        # Staging store — two-phase large-scale export (opt-in via MEDANON_STAGING_DB_URL)
        staging_url = os.environ.get("MEDANON_STAGING_DB_URL", "").strip()
        if staging_url:
            try:
                from integrations.staging.store import StagingStore
                retention_days = int(os.environ.get("MEDANON_STAGING_RETENTION_DAYS", "30"))
                staging_store = StagingStore(staging_url, retention_days=retention_days)
                staging_store.ensure_schema()
                _worker.init_staging(staging_store)
                logger.info("staging_store=postgres retention_days=%d", retention_days)
            except Exception as exc:
                logger.warning("staging_store_setup_failed falling_back=streaming: %s", exc)

        asyncio.create_task(_worker.worker_loop())
        logger.info("job_worker started max_concurrent=%d", max_concurrent)
    except Exception as exc:
        logger.warning("job_worker_start_failed: %s", exc)

    # FHIR Subscription store
    try:
        from pipeline.subscriptions import init_subscription_store
        sub_db = os.environ.get("MEDANON_SUBSCRIPTION_DB", "/output/subscriptions.db")
        init_subscription_store(sub_db)
        logger.info("subscription_store started path=%s", sub_db)
    except Exception as exc:
        logger.warning("subscription_store_start_failed: %s", exc)

    # Config metadata store (user-defined config profiles)
    try:
        from pipeline.config.store import init_config_store
        config_store_db = os.environ.get("MEDANON_CONFIG_STORE_DB", "/output/config_store.db")
        init_config_store(config_store_db)
        logger.info("config_store started path=%s", config_store_db)
    except Exception as exc:
        logger.warning("config_store_start_failed: %s", exc)

    # Pre-warm the NLP adapter (Presidio + spaCy) in the background so the
    # first user request is not blocked by the 3-second model load.
    async def _prewarm_nlp() -> None:
        try:
            loop = asyncio.get_event_loop()
            from pipeline.deidentify import _get_nlp_adapter
            await loop.run_in_executor(None, _get_nlp_adapter)
            logger.info("nlp_prewarm complete")
        except Exception as exc:
            logger.debug("nlp_prewarm skipped: %s", exc)

    asyncio.create_task(_prewarm_nlp())

app.state.limiter = limiter
if RateLimitExceeded is not None:
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
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
    required = get_required_role(request.url.path)
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
    from api.services.health import HealthCheckService
    checks = HealthCheckService().check_readiness()
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

app.include_router(process.router, prefix="/v1")
app.include_router(fhir_server.router, prefix="/v1")
app.include_router(analytics.router, prefix="/v1")
app.include_router(synthetic.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")
app.include_router(configs.router, prefix="/v1")
app.include_router(dicom.router, prefix="/v1")
app.include_router(hl7v2.router, prefix="/v1")
app.include_router(fhir_bulk.router, prefix="/fhir")
app.include_router(fhir_subscriptions.router)
app.include_router(smart.router)
