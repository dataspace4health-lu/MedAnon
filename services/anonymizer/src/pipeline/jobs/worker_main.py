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
    from pipeline.jobs.store_factory import (
        check_gpas_canary,
        select_job_store,
        setup_redis_cache,
        setup_staging,
    )

    # Log the centralized connection pool budget so operators can verify sizing.
    try:
        from utils.pool_budget import log_pool_budget

        log_pool_budget()
    except Exception:
        pass

    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    app_db_url = os.environ.get("MEDANON_APP_DB_URL", "").strip()
    max_concurrent = int(os.environ.get("MEDANON_JOB_WORKERS", "3"))

    # Expose health/ready/metrics on a lightweight HTTP server (separate from the
    # FastAPI anonymizer).  Port is configurable; set to 0 to disable.
    metrics_port = int(os.environ.get("MEDANON_WORKER_METRICS_PORT", "9091"))
    if metrics_port > 0:
        try:
            from pipeline.jobs.worker_health import start_worker_health_server

            start_worker_health_server(metrics_port)
        except Exception as exc:
            logger.warning(
                "worker_health_server_failed port=%d: %s", metrics_port, exc
            )

    # Shared setup via store factory (identical to api/main.py)
    await setup_redis_cache(redis_url)
    await check_gpas_canary(redis_url)

    job_store, pg_pool = await select_job_store(redis_url, app_db_url)
    store = init_job_store(store=job_store)
    _worker.init_worker(store, max_concurrent=max_concurrent)

    staging_url = os.environ.get("MEDANON_STAGING_DB_URL", "").strip()
    staging_store = await setup_staging(staging_url, app_db_url, pg_pool)
    if staging_store is not None:
        _worker.init_staging(staging_store)

    # Graceful shutdown: stop accepting new jobs on SIGTERM/SIGINT,
    # let in-progress jobs finish (up to graceful-timeout).
    stop_event = asyncio.Event()

    def _signal_handler(sig, _frame):
        logger.info(
            "received %s — initiating graceful shutdown", signal.Signals(sig).name
        )
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
            logger.warning(
                "worker_loop returned unexpectedly, restarting in %.1fs",
                backoff_restart,
            )
            await asyncio.sleep(backoff_restart)
            backoff_restart = min(backoff_restart * 2, max_backoff)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(
                "worker_loop crashed: %s — restarting in %.1fs", exc, backoff_restart
            )
            await asyncio.sleep(backoff_restart)
            backoff_restart = min(backoff_restart * 2, max_backoff)

    logger.info("worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
