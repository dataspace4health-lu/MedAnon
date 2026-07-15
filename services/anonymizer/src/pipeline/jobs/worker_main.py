"""Standalone worker entry point  no FastAPI, no HTTP server.

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
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
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
        from utils.pool_budget import check_thread_budget, log_pool_budget

        log_pool_budget()
        check_thread_budget()
    except Exception:
        pass

    # Inject the scoring engine's providers (remote client + NLP adapter) so the
    # shared medanon-core scoring engine works in the worker without importing
    # integrations.* itself. Mirrors api/main.py startup.
    try:
        from integrations.nlp.adapter import _get_nlp_adapter
        from integrations.scoring import get_remote_scoring_client
        from scoring.engine import set_remote_client_provider
        from scoring.privacy import set_nlp_adapter_provider

        set_remote_client_provider(get_remote_scoring_client)
        set_nlp_adapter_provider(_get_nlp_adapter)
    except Exception:
        logger.warning("scoring_provider_wiring_failed", exc_info=True)

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
            logger.warning("worker_health_server_failed port=%d: %s", metrics_port, exc)

    # Initialize OTel tracing for the worker process (no FastAPI, no HTTP instrumentation).
    try:
        from utils.tracing import setup_tracing_worker

        setup_tracing_worker()
    except Exception as _tracing_exc:
        logger.warning("tracing_init_failed: %s", _tracing_exc)

    # Shared setup via store factory (identical to api/main.py)
    await setup_redis_cache(redis_url)
    await check_gpas_canary(redis_url)

    job_store, pg_pool = await select_job_store(redis_url, app_db_url)
    # C11 guard: a dedicated worker container with SQLite is never safe.
    from pipeline.jobs.store_factory import assert_durable_store_or_exit

    assert_durable_store_or_exit(job_store, role="worker")
    store = init_job_store(store=job_store)
    _worker.init_worker(store, max_concurrent=max_concurrent)

    # ``select_job_store`` only creates the shared PostgreSQL pool when Postgres
    # is the JOB-STORE backend. With Redis as the job store, ``pg_pool`` stays
    # None even though MEDANON_APP_DB_URL is set  leaving every Postgres
    # app-state store (processing-run, workflow engine, sql-connection) to fall
    # back to SQLite or fail. Create the shared pool here so the worker behaves
    # like the API (which has the same fix). ``get_pool`` is an idempotent
    # singleton, so this is a no-op when the pool already exists.
    if pg_pool is None and app_db_url:
        try:
            from integrations.postgres.pool import get_pool

            pg_pool = get_pool(app_db_url)
            logger.info("app_db_pool=postgres (shared app-state pool)")
        except Exception as exc:
            logger.warning("app_db_pool_init_failed: %s", exc)

    # Initialize processing run store so scored job results are persisted to the
    # dashboard table.  Uses the same backend-selection logic as api/main.py.
    try:
        from pipeline.processing_run import init_processing_run_store

        if pg_pool is not None:
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

    # Job detail cache  the worker WRITES it at finalize (the API only reads).
    # Computing it here is what keeps the browser out of the data path: the UI
    # renders counts/PII/fields without ever downloading the NDJSON result.
    try:
        from pipeline.job_detail import init_job_detail_store

        if pg_pool is not None:
            from integrations.postgres.job_detail_store import PostgresJobDetailStore

            init_job_detail_store(store=PostgresJobDetailStore(pg_pool))
            logger.info("job_detail_store=postgres")
        else:
            jd_db = os.environ.get("MEDANON_JOB_DETAIL_DB", "/output/job_details.db")
            init_job_detail_store(jd_db)
            logger.info("job_detail_store=sqlite path=%s", jd_db)
    except Exception as exc:
        logger.warning("job_detail_store_start_failed: %s", exc)

    staging_url = os.environ.get("MEDANON_STAGING_DB_URL", "").strip()
    staging_store = await setup_staging(staging_url, app_db_url, pg_pool)
    if staging_store is not None:
        _worker.init_staging(staging_store)

    # SQL-source connection store  Postgres only. The worker executes
    # ``sql-export`` jobs which resolve a saved connection by id, so the worker
    # MUST initialise this store too (the API initialises its own copy). Without
    # it, sql-export fails with "SQL connection store is not initialised".
    if pg_pool is not None:
        try:
            from integrations.postgres.sql_connection_store import (
                PostgresSqlConnectionStore,
            )
            from pipeline.sql_connection import init_sql_connection_store

            init_sql_connection_store(PostgresSqlConnectionStore(pg_pool))
            logger.info("sql_connection_store=postgres")
        except Exception as exc:
            logger.warning("sql_connection_store_start_failed: %s", exc)

    # Dataspace connector stores  the worker resolves the input-source token
    # and the S3 output destination at finalize time, so it needs these stores
    # too (not just the API). Without them, publish_result cannot deliver.
    if pg_pool is not None:
        try:
            from integrations.postgres.connector_stores import (
                PostgresDestinationStore,
                PostgresSourceStore,
            )
            from integrations.connectors import (
                init_destination_store,
                init_source_store,
            )

            init_source_store(PostgresSourceStore(pg_pool))
            init_destination_store(PostgresDestinationStore(pg_pool))
            logger.info("connector_stores=postgres")
        except Exception as exc:
            logger.warning("connector_stores_start_failed: %s", exc)

    # Workflow engine  needed in the worker so the job terminal hook can
    # advance DAG steps. Postgres-only ("staging is the ledger").
    workflows_enabled = os.environ.get(
        "MEDANON_WORKFLOWS_ENABLED", "true"
    ).strip().lower() in ("true", "1", "yes")
    if pg_pool is not None and workflows_enabled:
        try:
            from integrations.postgres.workflow_store import PostgresWorkflowStore
            from pipeline.workflows import init_workflow_engine

            wf_store = PostgresWorkflowStore(pg_pool)
            wf_store.ensure_schema()
            init_workflow_engine(wf_store, store)
            logger.info("workflow_engine=postgres")
        except Exception as exc:
            logger.warning("workflow_engine_start_failed: %s", exc)

    # RabbitMQ stage consumers  opt-in (MEDANON_AMQP_URL). Each configured
    # stage gets its own consumer task; they drain partition messages and run
    # the shared per-partition processor. No-op (and no aio-pika import) when
    # AMQP is unset, so the default stack is unchanged.
    stage_consumer_tasks: list = []
    try:
        from integrations.rabbitmq.client import (
            amqp_enabled,
            consumer_stages,
            init_amqp_client,
        )

        if amqp_enabled() and staging_store is not None:
            await init_amqp_client()
            from pipeline.jobs.stage_consumer import run_stage_consumer

            for stage in consumer_stages():
                stage_consumer_tasks.append(
                    asyncio.create_task(
                        run_stage_consumer(stage, store, staging_store),
                        name=f"stage_consumer_{stage}",
                    )
                )
            logger.info(
                "amqp_stage_consumers_started stages=%s", ",".join(consumer_stages())
            )
        elif amqp_enabled():
            logger.warning(
                "amqp_enabled but staging store is unavailable  stage "
                "consumers not started (set MEDANON_APP_DB_URL/STAGING_DB_URL)"
            )
    except Exception as exc:
        logger.warning("amqp_stage_consumer_start_failed: %s", exc)

    # Graceful shutdown: stop accepting new jobs on SIGTERM/SIGINT,
    # let in-progress jobs finish (up to graceful-timeout).
    stop_event = asyncio.Event()

    def _signal_handler(sig, _frame):
        logger.info(
            "received %s  initiating graceful shutdown", signal.Signals(sig).name
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
            # worker_loop returned unexpectedly  restart
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
                "worker_loop crashed: %s  restarting in %.1fs", exc, backoff_restart
            )
            await asyncio.sleep(backoff_restart)
            backoff_restart = min(backoff_restart * 2, max_backoff)

    # Stop stage consumers on shutdown.
    for t in stage_consumer_tasks:
        t.cancel()
    for t in stage_consumer_tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    logger.info("worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
