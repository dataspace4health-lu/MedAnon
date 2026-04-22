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
                    "%s_failed attempt=%d/%d: %s — retrying in %.0fs",
                    label, attempt, retries, exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            else:
                logger.warning(
                    "%s_failed falling_back=%s: %s", label, fallback_label, exc,
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
            _make_redis, label="redis_job_store", fallback_label="next",
        )
        if job_store is not None:
            logger.info("job_store=redis")

    # 2. PostgreSQL
    if job_store is None and app_db_url:
        def _make_postgres():
            from integrations.postgres.pool import get_pool
            from integrations.postgres.job_store import PostgresJobStore
            pool = get_pool(app_db_url)
            return PostgresJobStore(pool), pool

        result = await _retry_async(
            _make_postgres, label="postgres_job_store", fallback_label="sqlite",
        )
        if result is not None:
            job_store, pg_pool = result
            logger.info("job_store=postgres")

    # 3. SQLite fallback (init_job_store handles it when job_store is None)
    return job_store, pg_pool


async def setup_redis_cache(redis_url: str) -> None:
    """Configure tiered gPAS cache (local LRU + Redis L2)."""
    if not redis_url:
        return

    def _make_cache():
        from utils.cache import (
            LocalLruCache, RedisCache, TieredCache, configure_cache,
        )
        configure_cache(TieredCache(LocalLruCache(), RedisCache(redis_url)))
        return True  # sentinel to distinguish success from retry exhaustion

    ok = await _retry_async(
        _make_cache, label="redis_cache_setup", fallback_label="local",
    )
    if ok:
        logger.info("gpas_cache=tiered(local+redis)")


async def check_gpas_canary(redis_url: str) -> None:
    """Run gPAS cache coherence check — detect stale Redis after DB wipe."""
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
        _make_staging, retries=3, label="staging_store_setup",
        fallback_label="streaming",
    )
    if result is not None:
        logger.info("staging_store=postgres retention_days=%d", retention_days)
    return result
