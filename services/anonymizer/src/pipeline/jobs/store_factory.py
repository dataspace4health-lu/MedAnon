"""Unified store factory for job store, staging store, and Redis cache.

Both ``api/main.py`` and ``pipeline/jobs/worker_main.py`` call these helpers
so that backend selection, retry semantics, and fallback chains are identical
regardless of the entry point.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("medanon.store_factory")

_DEFAULT_RETRIES = 5
_DEFAULT_BACKOFF = 2.0
_MAX_BACKOFF = 30.0


async def _retry_async(
    fn,
    *,
    retries: int = _DEFAULT_RETRIES,
    backoff: float = _DEFAULT_BACKOFF,
    label: str = "operation",
    fallback_label: str = "next",
):
    """Call *fn()* with exponential backoff.  Returns the result or None."""
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt < retries:
                logger.warning(
                    "%s_failed attempt=%d/%d: %s  retrying in %.0fs",
                    label,
                    attempt,
                    retries,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            else:
                logger.warning(
                    "%s_failed falling_back=%s: %s",
                    label,
                    fallback_label,
                    exc,
                )
    return None


async def select_job_store(redis_url: str, app_db_url: str):
    """Select job store backend: Redis → PostgreSQL → SQLite.

    Returns ``(store, pg_pool_or_None)``.  The ``pg_pool`` is returned so
    callers can share it with other PostgreSQL-backed stores.
    """
    job_store = None
    pg_pool = None

    # 1. Redis
    if redis_url:

        def _make_redis():
            from integrations.redis.job_store import RedisJobStore

            return RedisJobStore(redis_url)

        job_store = await _retry_async(
            _make_redis,
            label="redis_job_store",
            fallback_label="next",
        )
        if job_store is not None:
            logger.info("job_store=redis")

    # 2. PostgreSQL
    if job_store is None and app_db_url:

        def _make_postgres():
            from integrations.postgres.pool import get_pool
            from integrations.postgres.job_store import PostgresJobStore

            pool = get_pool(app_db_url)
            store = PostgresJobStore(pool)
            # The API and the worker both resolve their job store here, so this is
            # the one seam that guarantees medanon.jobs exists before either uses
            # it. A failure retries, then falls back to SQLite, as before.
            store.ensure_schema()
            return store, pool

        result = await _retry_async(
            _make_postgres,
            label="postgres_job_store",
            fallback_label="sqlite",
        )
        if result is not None:
            job_store, pg_pool = result
            logger.info("job_store=postgres")

    # 3. SQLite fallback (init_job_store handles it when job_store is None)
    return job_store, pg_pool


def assert_durable_store_or_exit(
    job_store, *, role: str, require_durable: bool | None = None
) -> None:
    """Refuse to start with SQLite when running in multi-container mode.

    SQLite WAL mode is not safe across container boundaries  both the API
    and the dedicated worker bind-mount ``./output`` so a shared SQLite DB
    risks data loss and double-claim races.

    Behaviour:
      - ``role="worker"``: SQLite is *always* unsafe (the worker only exists
        as a separate container)  exit on SQLite regardless of env.
      - ``role="api"``: exit on SQLite only when
        ``MEDANON_REQUIRE_DURABLE_STORE=true`` *or* the dedicated worker is
        enabled separately (``MEDANON_WORKER_ENABLED=false`` here implies
        an external worker container).

    Set ``MEDANON_ALLOW_SQLITE_FALLBACK=true`` to override the guard for
    single-container local development.
    """
    if job_store is not None:  # Redis or PostgreSQL  durable, all good
        return
    if os.environ.get("MEDANON_ALLOW_SQLITE_FALLBACK", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        logger.warning(
            "sqlite_fallback_allowed role=%s  only safe in single-container "
            "local dev; set MEDANON_REDIS_URL or MEDANON_APP_DB_URL in production",
            role,
        )
        return

    if require_durable is None:
        require_durable = os.environ.get(
            "MEDANON_REQUIRE_DURABLE_STORE", ""
        ).lower() in ("true", "1", "yes")

    if role == "worker":
        # The dedicated worker container ALWAYS shares /output with the API
        # container  SQLite here is unsafe. No exception.
        msg = (
            "FATAL: worker_main started without a durable job store. "
            "Set MEDANON_REDIS_URL or MEDANON_APP_DB_URL  SQLite at "
            "/output/jobs.db is not safe across container boundaries. "
            "Override (single-container dev only): MEDANON_ALLOW_SQLITE_FALLBACK=true"
        )
        logger.error(msg)
        raise SystemExit(2)

    if require_durable:
        msg = (
            "FATAL: api startup with MEDANON_REQUIRE_DURABLE_STORE=true but "
            "neither MEDANON_REDIS_URL nor MEDANON_APP_DB_URL is reachable. "
            "Refusing SQLite fallback in production mode."
        )
        logger.error(msg)
        raise SystemExit(2)

    # Permissive default for the API (matches historical single-container dev)
    logger.warning(
        "sqlite_job_store role=api  single-container mode only. Set "
        "MEDANON_REDIS_URL or MEDANON_APP_DB_URL for multi-replica deployments."
    )


async def setup_redis_cache(redis_url: str) -> None:
    """Configure tiered gPAS cache (local LRU + Redis L2).

    Also logs the cache-key invariants that **must** be stable across runs for
    the warm-cache speedup to work:
      * GPAS_DOMAIN  keys are scoped per domain
      * GPAS_OPERATION (default ``pseudonymizeAllowCreate``)  must be the
        same on every run; switching to a different operation produces a
        different cache key and forces a full L2 miss.
    """
    # Emit the cache-key invariants regardless of whether L2 is configured
    # an operator inspecting logs after a slow second run needs this signal
    # even on local-only caching.
    _gpas_domain = os.environ.get("GPAS_DOMAIN", "<unset>")
    _gpas_operation = os.environ.get("GPAS_OPERATION", "pseudonymizeAllowCreate")
    logger.info(
        "gpas_cache_keys domain=%s operation=%s  must be stable across runs",
        _gpas_domain,
        _gpas_operation,
    )

    if not redis_url:
        logger.warning(
            "gpas_cache=local  MEDANON_REDIS_URL not set; pseudonym cache will "
            "NOT survive container restart and is NOT shared across replicas"
        )
        return

    # Pseudonym entries carry a TTL so they are EVICTABLE under memory pressure.
    #
    # Redis runs `maxmemory-policy volatile-lru`, which only evicts keys that
    # have an expiry. Job hashes already set one (integrations/redis/job_store),
    # so without a TTL here the pseudonym cache  the one unbounded, ever-growing
    # keyspace  was the only thing PINNED: Redis would evict recoverable job
    # metadata and then be OOM-killed by the container limit, taking the whole
    # warm cache with it. Giving these keys a TTL inverts that correctly: the
    # large cold cache becomes the natural LRU victim.
    #
    # Eviction is safe, never wrong: this is pure memoization over gPAS, which
    # is the vault and is deterministic for a given (value, domain). A miss
    # costs one round-trip and returns the SAME pseudonym, so linkage is
    # preserved. The TTL is long by default because a warm cache is worth ~72x
    # on re-export; shorten it only to bound Redis memory further.
    _cache_ttl = int(os.environ.get("MEDANON_GPAS_CACHE_TTL_SEC", str(30 * 24 * 3600)))

    def _make_cache():
        from utils.cache import (
            LocalLruCache,
            RedisCache,
            TieredCache,
            configure_cache,
        )

        configure_cache(
            TieredCache(
                LocalLruCache(),
                RedisCache(redis_url, ttl=_cache_ttl if _cache_ttl > 0 else None),
            )
        )
        return True  # sentinel to distinguish success from retry exhaustion

    ok = await _retry_async(
        _make_cache,
        label="redis_cache_setup",
        fallback_label="local",
    )
    if ok:
        logger.info("gpas_cache=tiered(local+redis)")


async def check_gpas_canary(redis_url: str) -> None:
    """Run gPAS cache coherence check  detect stale Redis after DB wipe."""
    if not redis_url:
        return
    try:
        from integrations.gpas.canary import check_gpas_cache_coherence

        result = check_gpas_cache_coherence(redis_url)
        if result.get("flushed"):
            logger.warning("gpas_canary: %s", result["reason"])
        elif result.get("checked"):
            logger.info("gpas_canary: %s", result["reason"])
    except Exception as exc:
        logger.warning("gpas_canary_check_failed: %s", exc)


async def setup_staging(
    staging_url: str,
    app_db_url: str,
    pg_pool=None,
    retention_days: int | None = None,
):
    """Set up the staging store.  Returns ``StagingStore`` or None."""
    url = staging_url or app_db_url
    if not url:
        return None

    if retention_days is None:
        retention_days = int(os.environ.get("MEDANON_STAGING_RETENTION_DAYS", "30"))

    def _make_staging():
        from integrations.staging.store import StagingStore

        shared_pool = pg_pool if url == app_db_url else None
        store = StagingStore(url, retention_days=retention_days, pool=shared_pool)
        store.ensure_schema()
        return store

    result = await _retry_async(
        _make_staging,
        retries=3,
        label="staging_store_setup",
        fallback_label="streaming",
    )
    if result is not None:
        logger.info("staging_store=postgres retention_days=%d", retention_days)
    return result
