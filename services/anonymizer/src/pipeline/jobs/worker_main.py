"""Standalone worker entry point — no FastAPI, no HTTP server.

Usage::

    python -m pipeline.jobs.worker_main

Initializes the job store, optional staging store, and runs the worker loop
with graceful SIGTERM handling.  Designed for a dedicated ``worker`` Docker
service using the same image as the anonymizer but a different CMD.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("medanon.worker_main")


async def _main() -> None:
    from pipeline.jobs import init_job_store
    from pipeline.jobs import worker as _worker

    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    max_concurrent = int(os.environ.get("MEDANON_JOB_WORKERS", "3"))

    # Opt-in Redis L2 cache for gPAS pseudonym sharing (with retry)
    if redis_url:
        retries = 5
        backoff = 2.0
        for attempt in range(1, retries + 1):
            try:
                from utils.cache import RedisCache, configure_cache
                configure_cache(RedisCache(redis_url))
                logger.info("gpas_cache=redis")
                break
            except Exception as exc:
                if attempt < retries:
                    logger.warning(
                        "redis_cache_setup_failed attempt=%d/%d: %s — retrying in %.0fs",
                        attempt, retries, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    logger.warning("redis_cache_setup_failed falling_back=local: %s", exc)

    # Job store — prefer Redis (with retry), fall back to SQLite
    job_store = None
    if redis_url:
        retries = 5
        backoff = 2.0
        for attempt in range(1, retries + 1):
            try:
                from integrations.redis.job_store import RedisJobStore
                job_store = RedisJobStore(redis_url)
                logger.info("job_store=redis")
                break
            except Exception as exc:
                if attempt < retries:
                    logger.warning(
                        "redis_job_store_failed attempt=%d/%d: %s — retrying in %.0fs",
                        attempt, retries, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    logger.warning("redis_job_store_failed falling_back=sqlite: %s", exc)

    store = init_job_store(store=job_store)
    _worker.init_worker(store, max_concurrent=max_concurrent)

    # Staging store (opt-in)
    staging_url = os.environ.get("MEDANON_STAGING_DB_URL", "").strip()
    if staging_url:
        retries = 3
        backoff = 2.0
        for attempt in range(1, retries + 1):
            try:
                from integrations.staging.store import StagingStore
                retention_days = int(os.environ.get("MEDANON_STAGING_RETENTION_DAYS", "30"))
                staging_store = StagingStore(staging_url, retention_days=retention_days)
                staging_store.ensure_schema()
                _worker.init_staging(staging_store)
                logger.info("staging_store=postgres retention_days=%d", retention_days)
                break
            except Exception as exc:
                if attempt < retries:
                    logger.warning(
                        "staging_store_setup_failed attempt=%d/%d: %s — retrying in %.0fs",
                        attempt, retries, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    logger.warning("staging_store_setup_failed falling_back=streaming: %s", exc)

    # Graceful shutdown: stop accepting new jobs on SIGTERM/SIGINT,
    # let in-progress jobs finish (up to graceful-timeout).
    stop_event = asyncio.Event()

    def _signal_handler(sig, _frame):
        logger.info("received %s — initiating graceful shutdown", signal.Signals(sig).name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Supervised worker loop with auto-restart on crash
    backoff_restart = 1.0
    max_backoff = 30.0
    while not stop_event.is_set():
        try:
            logger.info("worker_loop starting max_concurrent=%d", max_concurrent)
            # Run worker_loop until it returns or stop_event is set
            worker_task = asyncio.create_task(_worker.worker_loop())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {worker_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            if stop_event.is_set():
                break
            # worker_loop returned unexpectedly — restart
            logger.warning("worker_loop returned unexpectedly, restarting in %.1fs", backoff_restart)
            await asyncio.sleep(backoff_restart)
            backoff_restart = min(backoff_restart * 2, max_backoff)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("worker_loop crashed: %s — restarting in %.1fs", exc, backoff_restart)
            await asyncio.sleep(backoff_restart)
            backoff_restart = min(backoff_restart * 2, max_backoff)

    logger.info("worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
