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
    agents,
    analytics,
    api_keys,
    audit,
    auth as auth_router,
    cda,
    configs,
    dashboard,
    dicom,
    fhir_bulk,
    fhir_server,
    hl7v2,
    jobs,
    process,
    processing_runs,
    scoring,
    sql_source,
    synthetic,
    tabular,
    workflows,
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

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    """Replaces deprecated @app.on_event("startup"|"shutdown") (F.17).

    The pre-yield block performs all startup work (formerly ``_startup``);
    the post-yield block performs cleanup (formerly ``_shutdown``).
    """
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="MedAnon", version="2.0.0", lifespan=lifespan)

# Optional OpenTelemetry tracing (no-op unless MEDANON_OTEL_ENABLED=true).
# Must run after `app` is created and before any route is registered so
# FastAPIInstrumentor can wrap the underlying ASGI app.
from utils.tracing import setup_tracing  # noqa: E402

setup_tracing(app)

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


async def _startup() -> None:
    # Publish build metadata so dashboards can correlate behaviour with
    # deployments.  Labels are never high-cardinality (one tuple per process).
    try:
        from utils.metrics import BUILD_INFO

        _version = os.environ.get("MEDANON_VERSION", "dev")
        _git_sha = os.environ.get("GIT_SHA", "unknown")[:12]
        BUILD_INFO.labels(version=_version, git_sha=_git_sha).set(1)
    except Exception:
        pass  # metrics are optional in some test contexts

    # Refuse to start with plain (un-keyed) hashing outside a dev environment.
    # Plain SHA3 is reversible via rainbow tables — must never reach production.
    _allow_plain = os.environ.get("MEDANON_HASH_ALLOW_PLAIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    _is_dev = os.environ.get("ENVIRONMENT", "").strip().lower() in (
        "dev",
        "development",
        "test",
        "testing",
    )
    if _allow_plain and not _is_dev:
        raise RuntimeError(
            "MEDANON_HASH_ALLOW_PLAIN=true is set but ENVIRONMENT is not 'dev' or 'test'. "
            "Plain SHA3 hashing is not safe in production — set MEDANON_HASH_KEY instead."
        )

    # Log the centralized connection pool budget and check thread concurrency.
    try:
        from utils.pool_budget import check_thread_budget, log_pool_budget

        log_pool_budget()
        check_thread_budget()
    except Exception:
        pass

    # Opt-in Redis L2 cache + gPAS canary (via shared factory)
    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    app_db_url = os.environ.get("MEDANON_APP_DB_URL", "").strip()

    from pipeline.jobs.store_factory import (
        check_gpas_canary,
        select_job_store,
        setup_redis_cache,
        setup_staging,
    )

    await setup_redis_cache(redis_url)
    await check_gpas_canary(redis_url)

    # Validate Redis durability config — AOF must be enabled in production
    # so that an unexpected restart does not lose queued jobs (RDB snapshots
    # alone may be up to 60 s stale).  Soft warning by default; set
    # MEDANON_REQUIRE_REDIS_AOF=true to fail startup when AOF is off.
    if redis_url:
        try:
            from utils.redis_pool import get_redis

            _r = get_redis(redis_url, decode_responses=True)
            cfg = _r.config_get("appendonly") or {}
            if str(cfg.get("appendonly", "")).lower() != "yes":
                msg = (
                    "redis_aof_disabled — durability at risk; set "
                    "appendonly=yes in redis.conf or MEDANON_REQUIRE_REDIS_AOF=false to silence"
                )
                if (
                    os.environ.get("MEDANON_REQUIRE_REDIS_AOF", "false").lower()
                    == "true"
                ):
                    raise RuntimeError(msg)
                logger.warning(msg)
            else:
                logger.info("redis_aof_enabled")
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning("redis_aof_check_skipped: %s", exc)

    # Idempotency-Key store: Redis when available (cross-replica), in-memory
    # fallback otherwise (single-replica deployments only).
    try:
        from utils.idempotency import (
            LocalIdempotencyStore,
            RedisIdempotencyStore,
            init_store as _init_idem_store,
        )

        idem_client = None
        if redis_url:
            try:
                from utils.redis_pool import get_redis

                idem_client = get_redis(redis_url, decode_responses=True)
                idem_client.ping()
            except Exception:
                logger.warning("idempotency_redis_unavailable_falling_back_to_local")
                idem_client = None
        if idem_client is not None:
            _init_idem_store(RedisIdempotencyStore(idem_client))
        else:
            _init_idem_store(LocalIdempotencyStore())
    except Exception:
        logger.warning("idempotency_store_init_failed", exc_info=True)

    # Async job queue — select backend:
    #   1. MEDANON_REDIS_URL → RedisJobStore (event-driven Streams)
    #   2. MEDANON_APP_DB_URL → PostgresJobStore (LISTEN/NOTIFY + FOR UPDATE SKIP LOCKED)
    #   3. Fallback → SqliteJobStore (polling, local dev)
    max_concurrent = int(os.environ.get("MEDANON_JOB_WORKERS", "3"))
    pg_pool = None  # shared PostgreSQL pool for all app-state stores
    try:
        from pipeline.jobs import init_job_store
        from pipeline.jobs import worker as _worker

        job_store, pg_pool = await select_job_store(redis_url, app_db_url)
        # C11 guard: refuse SQLite when MEDANON_REQUIRE_DURABLE_STORE=true.
        from pipeline.jobs.store_factory import assert_durable_store_or_exit

        assert_durable_store_or_exit(job_store, role="api")
        if pg_pool is not None:
            app.state.pg_pool = pg_pool

        store = init_job_store(store=job_store)
        _worker.init_worker(store, max_concurrent=max_concurrent)

        # Staging store — two-phase large-scale export
        staging_url = os.environ.get("MEDANON_STAGING_DB_URL", "").strip()
        staging_store = await setup_staging(staging_url, app_db_url, pg_pool)
        if staging_store is not None:
            _worker.init_staging(staging_store)
            app.state.staging_store = staging_store

        worker_enabled = os.environ.get(
            "MEDANON_WORKER_ENABLED", "false"
        ).strip().lower() in ("true", "1", "yes")
        if worker_enabled:
            from utils.tasks import retain_task

            retain_task(_supervised_worker_loop(_worker), name="worker_loop")
            logger.info("job_worker started max_concurrent=%d", max_concurrent)
        else:
            logger.info("job_worker disabled (MEDANON_WORKER_ENABLED=false)")
    except Exception as exc:
        logger.warning("job_worker_start_failed: %s", exc)

    # Ensure a shared PostgreSQL pool exists for app-state stores (config,
    # subscriptions, processing runs, API keys, SQL source connections) whenever
    # MEDANON_APP_DB_URL is set — even when Redis is the job store. Otherwise
    # ``select_job_store`` only creates the pool when Postgres is the job-store
    # backend, leaving these stores to fall back to SQLite or fail to initialise
    # despite the app database being available. ``get_pool`` is an idempotent
    # singleton, so this is a no-op when the pool already exists.
    if pg_pool is None and app_db_url:
        try:
            from integrations.postgres.pool import get_pool

            pg_pool = get_pool(app_db_url)
            app.state.pg_pool = pg_pool
            logger.info("app_db_pool=postgres (shared app-state pool)")
        except Exception as exc:
            logger.warning("app_db_pool_init_failed: %s", exc)

    # FHIR Subscription store — PostgreSQL when app-db available, else SQLite
    try:
        from pipeline.subscriptions import init_subscription_store

        if pg_pool:
            from integrations.postgres.subscription_store import (
                PostgresSubscriptionStore,
            )

            sub_store = PostgresSubscriptionStore(pg_pool)
            init_subscription_store(store=sub_store)
            logger.info("subscription_store=postgres")
        else:
            sub_db = os.environ.get(
                "MEDANON_SUBSCRIPTION_DB", "/output/subscriptions.db"
            )
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

    # SQL-source connection store — PostgreSQL only (saved encrypted credentials)
    if pg_pool:
        try:
            from integrations.postgres.sql_connection_store import (
                PostgresSqlConnectionStore,
            )
            from pipeline.sql_connection import init_sql_connection_store

            init_sql_connection_store(PostgresSqlConnectionStore(pg_pool))
            logger.info("sql_connection_store=postgres")
        except Exception as exc:
            logger.warning("sql_connection_store_start_failed: %s", exc)

    # Workflow (DAG) engine — PostgreSQL only ("staging is the ledger"); the
    # engine schedules steps through the same job store the worker drains.
    workflows_enabled = os.environ.get(
        "MEDANON_WORKFLOWS_ENABLED", "true"
    ).strip().lower() in ("true", "1", "yes")
    if pg_pool and workflows_enabled and store is not None:
        try:
            from integrations.postgres.workflow_store import PostgresWorkflowStore
            from pipeline.workflows import init_workflow_engine

            wf_store = PostgresWorkflowStore(pg_pool)
            wf_store.ensure_schema()
            init_workflow_engine(wf_store, store)
            logger.info("workflow_engine=postgres")
        except Exception as exc:
            logger.warning("workflow_engine_start_failed: %s", exc)
    elif workflows_enabled and not pg_pool:
        logger.info(
            "workflow_engine disabled — requires MEDANON_APP_DB_URL "
            "(Postgres is the workflow ledger)"
        )

    # Processing run store — PostgreSQL when app-db available, else SQLite
    try:
        from pipeline.processing_run import init_processing_run_store

        if pg_pool:
            from integrations.postgres.processing_run_store import (
                PostgresProcessingRunStore,
            )

            pr_store = PostgresProcessingRunStore(pg_pool)
            init_processing_run_store(store=pr_store)
            logger.info("processing_run_store=postgres")
        else:
            pr_db = os.environ.get(
                "MEDANON_PROCESSING_RUN_DB", "/output/processing_runs.db"
            )
            init_processing_run_store(pr_db)
            logger.info("processing_run_store=sqlite path=%s", pr_db)
    except Exception as exc:
        logger.warning("processing_run_store_start_failed: %s", exc)

    # Per-client API key store — PostgreSQL only (no SQLite fallback for key management).
    # Wiring this store makes X-API-Key mandatory on protected endpoints. Set
    # MEDANON_API_KEY_STORE_ENABLED=false to keep the app DB configured (jobs,
    # configs, etc.) while running the API in keyless OPEN MODE for local dev.
    key_store_enabled = os.environ.get(
        "MEDANON_API_KEY_STORE_ENABLED",
        "true",
    ).strip().lower() not in ("false", "0", "no")
    if pg_pool and key_store_enabled:
        try:
            from integrations.postgres.api_key_store import PostgresApiKeyStore
            from api.auth import init_api_key_store

            ak_store = PostgresApiKeyStore(pg_pool)
            init_api_key_store(store=ak_store)
            logger.info("api_key_store=postgres")
        except Exception as exc:
            logger.warning("api_key_store_start_failed: %s", exc)
    elif pg_pool:
        logger.info("api_key_store=disabled (MEDANON_API_KEY_STORE_ENABLED=false)")

    # OIDC JWKS warmup — eagerly fetch public keys so first JWT validation is fast.
    # Non-fatal: if Keycloak is not yet up the warmup logs a warning and continues;
    # the JWKS client will retry on the first real request.
    try:
        from integrations.oidc.validator import oidc_enabled, warmup as _oidc_warmup

        if oidc_enabled():
            await asyncio.to_thread(_oidc_warmup)
    except Exception as exc:
        logger.warning("oidc_warmup_failed: %s", exc)

    # Job detail cache — PostgreSQL when app-db available, else SQLite
    try:
        from pipeline.job_detail import init_job_detail_store

        if pg_pool:
            from integrations.postgres.job_detail_store import PostgresJobDetailStore

            jd_store = PostgresJobDetailStore(pg_pool)
            init_job_detail_store(store=jd_store)
            logger.info("job_detail_store=postgres")
        else:
            jd_db = os.environ.get("MEDANON_JOB_DETAIL_DB", "/output/job_details.db")
            init_job_detail_store(jd_db)
            logger.info("job_detail_store=sqlite path=%s", jd_db)
    except Exception as exc:
        logger.warning("job_detail_store_start_failed: %s", exc)

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

        from utils.tasks import retain_task

        retain_task(_prewarm_nlp(), name="nlp_prewarm")


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


# ---------------------------------------------------------------------------
# Backpressure: 503 + Retry-After when the job queue is saturated.
# Keeps the routers free of try/except boilerplate around every submit call.
# ---------------------------------------------------------------------------
from domain.jobs import JobQueueFull as _JobQueueFull  # noqa: E402


@app.exception_handler(_JobQueueFull)
async def _job_queue_full_handler(request: Request, exc: _JobQueueFull):
    retry_after = os.environ.get("MEDANON_QUEUE_FULL_RETRY_AFTER", "60")
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "pending": exc.pending,
            "cap": exc.cap,
        },
        headers={"Retry-After": retry_after},
    )


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
    try:
        from pipeline.rule_matcher import sample_cache_metrics

        sample_cache_metrics()
    except Exception:
        pass  # never fail a metrics scrape on observability code
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

app.include_router(auth_router.router)
app.include_router(process.router, prefix="/v1")
app.include_router(fhir_server.router, prefix="/v1")
app.include_router(analytics.router, prefix="/v1")
app.include_router(synthetic.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")
app.include_router(workflows.router, prefix="/v1")
app.include_router(configs.router, prefix="/v1")
app.include_router(scoring.router, prefix="/v1")
app.include_router(processing_runs.router, prefix="/v1")
app.include_router(audit.router, prefix="/v1")
app.include_router(admin.router, prefix="/v1")
app.include_router(agents.router, prefix="/v1")
app.include_router(api_keys.router, prefix="/v1")
app.include_router(dashboard.router, prefix="/v1")

app.include_router(dicom.router, prefix="/v1")
app.include_router(hl7v2.router, prefix="/v1")
app.include_router(cda.router, prefix="/v1")
app.include_router(tabular.router, prefix="/v1")
app.include_router(sql_source.router, prefix="/v1")
app.include_router(fhir_bulk.router, prefix="/fhir")
app.include_router(fhir_subscriptions.router)
app.include_router(smart.router)
