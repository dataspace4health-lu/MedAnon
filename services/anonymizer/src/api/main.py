"""MedAnon FastAPI application — entry point.

Wires together middleware, health/status endpoints, and the four endpoint routers.
Business logic lives in api/routers/; shared dependencies in api/deps.py.
"""

import asyncio
import json
import logging
import os
import re
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
from api.routers import (
    admin,
    analytics,
    audit,
    configs,
    dicom,
    fhir_bulk,
    fhir_server,
    hl7v2,
    jobs,
    process,
    scoring,
    synthetic,
)
from api.routers import fhir_subscriptions, smart

# Refresh fhir_bulk's module-level URL cache so that re-imports (e.g. during
# TestClient construction with patched env) capture the current FHIR_SOURCE_URL.
fhir_bulk._FHIR_SOURCE_URL = os.environ.get("FHIR_SOURCE_URL", "").strip()

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("medanon")

_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("MEDANON_CORS_ORIGINS", "").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="MedAnon", version="2.0.0")

# Whether the worker loop is healthy; checked by /ready.
_worker_healthy = True


async def _supervised_worker_loop(worker_module) -> None:
    """Run the worker loop with automatic restart on crash.

    Uses exponential backoff (1s, 2s, 4s, … max 30s) between restarts.
    Sets ``_worker_healthy`` to False while in a restart cycle so /ready
    can report the degraded state.
    """
    global _worker_healthy
    backoff = 1.0
    max_backoff = 30.0
    consecutive_failures = 0

    while True:
        try:
            _worker_healthy = True
            consecutive_failures = 0
            backoff = 1.0
            await worker_module.worker_loop()
            # worker_loop() should never return — if it does, restart.
            logger.warning("worker_loop returned unexpectedly, restarting")
        except asyncio.CancelledError:
            logger.info("worker_loop cancelled (shutdown)")
            return
        except Exception as exc:
            consecutive_failures += 1
            _worker_healthy = False
            logger.error(
                "worker_loop crashed (attempt %d): %s — restarting in %.1fs",
                consecutive_failures,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


@app.on_event("startup")
async def _startup() -> None:
    # NOTE: Do NOT set the BoundedExecutor as asyncio's default executor.
    # BoundedExecutor.submit() blocks the calling thread when the semaphore is
    # exhausted.  asyncio.to_thread() calls loop.run_in_executor(None, fn)
    # from the event loop thread — if the pool is full, the event loop itself
    # blocks, freezing all async request handling (deadlock).
    # The BoundedExecutor is used explicitly via get_executor() for CPU/IO work
    # that submits from worker threads, not from the event loop thread.

    # Opt-in Redis L2 cache for cross-replica gPAS result sharing
    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    if redis_url:
        try:
            from utils.cache import (
                LocalLruCache,
                RedisCache,
                TieredCache,
                configure_cache,
            )

            l1 = LocalLruCache()
            l2 = RedisCache(redis_url)
            configure_cache(TieredCache(l1, l2))
            logger.info("gpas_cache=tiered(local+redis)")
        except Exception as exc:
            logger.warning("redis_cache_setup_failed falling_back=local: %s", exc)

    # gPAS cache coherence check — detect stale Redis after DB wipe
    if redis_url:
        try:
            from integrations.gpas.canary import check_gpas_cache_coherence

            result = await asyncio.to_thread(check_gpas_cache_coherence, redis_url)
            if result.get("flushed"):
                logger.warning("gpas_canary: %s", result["reason"])
            elif result.get("checked"):
                logger.info("gpas_canary: %s", result["reason"])
            else:
                logger.debug("gpas_canary: %s", result.get("reason", "skipped"))
        except Exception as exc:
            logger.warning("gpas_canary_check_failed: %s", exc)

    # Async job queue — select backend:
    #   1. MEDANON_REDIS_URL → RedisJobStore (event-driven Streams)
    #   2. MEDANON_APP_DB_URL → PostgresJobStore (LISTEN/NOTIFY + FOR UPDATE SKIP LOCKED)
    #   3. Fallback → SqliteJobStore (polling, local dev)
    max_concurrent = int(os.environ.get("MEDANON_JOB_WORKERS", "3"))
    app_db_url = os.environ.get("MEDANON_APP_DB_URL", "").strip()
    pg_pool = None  # shared PostgreSQL pool for all app-state stores
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
                logger.warning("redis_job_store_failed falling_back=next: %s", exc)

        if job_store is None and app_db_url:
            try:
                from integrations.postgres.pool import get_pool
                from integrations.postgres.job_store import PostgresJobStore

                pg_pool = get_pool(app_db_url)
                job_store = PostgresJobStore(pg_pool)
                app.state.pg_pool = pg_pool
                logger.info("job_store=postgres")
            except Exception as exc:
                logger.warning("postgres_job_store_failed falling_back=sqlite: %s", exc)

        store = init_job_store(store=job_store)
        _worker.init_worker(store, max_concurrent=max_concurrent)

        # Staging store — two-phase large-scale export
        # Uses MEDANON_STAGING_DB_URL when set; otherwise falls back to MEDANON_APP_DB_URL.
        staging_url = (
            os.environ.get("MEDANON_STAGING_DB_URL", "").strip()
            or app_db_url
        )
        if staging_url:
            staging_store = None
            retries = 3
            backoff = 2.0
            for attempt in range(1, retries + 1):
                try:
                    from integrations.staging.store import StagingStore

                    retention_days = int(
                        os.environ.get("MEDANON_STAGING_RETENTION_DAYS", "30")
                    )
                    # Only share the app-db pool when both URLs are identical.
                    # If a separate MEDANON_STAGING_DB_URL is configured, pass
                    # pool=None so StagingStore creates its own pool with the
                    # correct connection string (pg_pool points to app_db_url).
                    shared_pool = pg_pool if staging_url == app_db_url else None
                    staging_store = StagingStore(
                        staging_url,
                        retention_days=retention_days,
                        pool=shared_pool,
                    )
                    staging_store.ensure_schema()
                    _worker.init_staging(staging_store)
                    app.state.staging_store = staging_store
                    logger.info(
                        "staging_store=postgres retention_days=%d", retention_days
                    )
                    break
                except Exception as exc:
                    if attempt < retries:
                        logger.warning(
                            "staging_store_setup_failed attempt=%d/%d: %s — retrying in %.0fs",
                            attempt,
                            retries,
                            exc,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 2
                    else:
                        logger.warning(
                            "staging_store_setup_failed falling_back=streaming: %s", exc
                        )

        worker_enabled = os.environ.get(
            "MEDANON_WORKER_ENABLED", "false"
        ).strip().lower() in ("true", "1", "yes")
        if worker_enabled:
            asyncio.create_task(_supervised_worker_loop(_worker))
            logger.info("job_worker started max_concurrent=%d", max_concurrent)
        else:
            logger.info("job_worker disabled (MEDANON_WORKER_ENABLED=false)")
    except Exception as exc:
        logger.warning("job_worker_start_failed: %s", exc)

    # FHIR Subscription store — PostgreSQL when app-db available, else SQLite
    try:
        from pipeline.subscriptions import init_subscription_store

        if pg_pool:
            from integrations.postgres.subscription_store import PostgresSubscriptionStore

            sub_store = PostgresSubscriptionStore(pg_pool)
            init_subscription_store(store=sub_store)
            logger.info("subscription_store=postgres")
        else:
            sub_db = os.environ.get("MEDANON_SUBSCRIPTION_DB", "/output/subscriptions.db")
            init_subscription_store(sub_db)
            logger.info("subscription_store=sqlite path=%s", sub_db)
    except Exception as exc:
        logger.warning("subscription_store_start_failed: %s", exc)

    # Config metadata store — PostgreSQL when app-db available, else SQLite
    try:
        from pipeline.config.store import init_config_store

        if pg_pool:
            from integrations.postgres.config_store import PostgresConfigStore

            cfg_store = PostgresConfigStore(pg_pool)
            init_config_store(store=cfg_store)
            logger.info("config_store=postgres")
        else:
            config_store_db = os.environ.get(
                "MEDANON_CONFIG_STORE_DB", "/output/config_store.db"
            )
            init_config_store(config_store_db)
            logger.info("config_store=sqlite path=%s", config_store_db)
    except Exception as exc:
        logger.warning("config_store_start_failed: %s", exc)

    # Pre-warm the NLP adapter (Presidio + spaCy) in the background so the
    # first user request is not blocked by the 3-second model load.
    # With gunicorn multi-worker, each worker process loads its own copy of
    # en_core_web_lg (~700 MB). Set MEDANON_NLP_PREWARM=false to skip prewarm
    # and load lazily on first request — saves N_workers × 700 MB at startup
    # cost of ~5 s cold-start on the first NLP request per worker.
    if os.environ.get("MEDANON_NLP_PREWARM", "false").lower() not in (
        "false",
        "0",
        "no",
    ):

        async def _prewarm_nlp() -> None:
            try:
                loop = asyncio.get_event_loop()
                from pipeline.deidentify import _get_nlp_adapter

                await loop.run_in_executor(None, _get_nlp_adapter)
                logger.info("nlp_prewarm complete")
            except Exception as exc:
                logger.debug("nlp_prewarm skipped: %s", exc)

        asyncio.create_task(_prewarm_nlp())


@app.on_event("shutdown")
async def _shutdown() -> None:
    staging_store = getattr(app.state, "staging_store", None)
    if staging_store is not None:
        try:
            staging_store.close()
            logger.info("staging_store closed")
        except Exception as exc:
            logger.warning("staging_store_close_failed: %s", exc)

    # Close the shared PostgreSQL pool (if any)
    pg_pool = getattr(app.state, "pg_pool", None)
    if pg_pool is not None:
        try:
            from integrations.postgres.pool import close_pool

            close_pool()
        except Exception as exc:
            logger.warning("postgres_pool_close_failed: %s", exc)


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
        auth_ctx = await asyncio.to_thread(get_auth_context, request)
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
                detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
            )
    elif request.method in ("POST", "PUT", "PATCH"):
        # No Content-Length header (chunked transfer) — wrap the receive
        # callable to count bytes as they flow through, without buffering
        # the entire body in a parallel list (avoids 2× peak memory).
        original_receive = request._receive
        byte_counter = [0]

        async def _counting_receive():
            message = await original_receive()
            chunk = message.get("body", b"")
            byte_counter[0] += len(chunk)
            if byte_counter[0] > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
                )
            return message

        request._receive = _counting_receive
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


# Pattern to normalize UUID and numeric segments in URL paths for Prometheus labels,
# preventing cardinality explosion from parameterized routes like /v1/jobs/{id}.
_PATH_NORMALIZE_RE = re.compile(
    r"/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE
)
_PATH_NUMERIC_RE = re.compile(r"/\d+(?=/|$)")


def _normalize_metric_path(path: str) -> str:
    """Replace UUID and numeric segments with placeholders to bound label cardinality."""
    path = _PATH_NORMALIZE_RE.sub("/{id}", path)
    path = _PATH_NUMERIC_RE.sub("/{id}", path)
    return path


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Record per-endpoint request count and latency for Prometheus."""
    start = time.perf_counter()
    response = await call_next(request)
    normalized = _normalize_metric_path(request.url.path)
    REQUEST_LATENCY.labels(endpoint=normalized).observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(
        endpoint=normalized,
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
    if not _worker_healthy:
        body["ready"] = False
        body["worker"] = "restarting"
        ready = False
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
app.include_router(scoring.router, prefix="/v1")
app.include_router(audit.router, prefix="/v1")
app.include_router(admin.router, prefix="/v1")

app.include_router(dicom.router, prefix="/v1")
app.include_router(hl7v2.router, prefix="/v1")
app.include_router(fhir_bulk.router, prefix="/fhir")
app.include_router(fhir_subscriptions.router)
app.include_router(smart.router)
