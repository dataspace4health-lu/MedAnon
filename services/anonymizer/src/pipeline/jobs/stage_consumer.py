"""RabbitMQ stage consumer — drains partition messages and processes them.

One consumer per stage (``MEDANON_AMQP_CONSUMER_STAGES``). For the ``deid``
stage it: claims the specific partition (CAS — duplicate deliveries fail the
claim and are acked+skipped), runs the shared :func:`process_one_partition`,
stores the shard, marks the partition done, and — when it was the LAST open
partition — advances the workflow step and publishes the next stage's message.

Failure handling: a processing error releases the partition and either
republishes for retry (via the retry queue, up to ``max_attempts``) or
dead-letters it. The Postgres ledger is authoritative; broker loss only delays
work until the partition reaper republishes.

This module is imported and started only when ``MEDANON_AMQP_URL`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import os

from integrations.rabbitmq.client import get_amqp_client
from integrations.rabbitmq.messages import StageMessage
from integrations.rabbitmq.topology import queue_name

logger = logging.getLogger("medanon.amqp.consumer")


def _worker_id() -> str:
    import socket
    import uuid

    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def run_stage_consumer(stage: str, store, staging) -> None:
    """Consume + process messages for one stage until cancelled.

    *store* is the job store; *staging* is the StagingStore (the durable ledger).
    """
    client = get_amqp_client()
    if client is None or staging is None:
        logger.warning("stage_consumer_skipped stage=%s (no amqp/staging)", stage)
        return
    # Ensure connection + topology.
    await client.connect()
    channel = client._channel  # noqa: SLF001 — intentional: reuse the client's channel
    queue = await channel.get_queue(queue_name(stage))
    wid = _worker_id()
    logger.info("stage_consumer_started stage=%s worker=%s", stage, wid)

    async with queue.iterator() as it:
        async for message in it:
            try:
                await _handle_message(stage, message, store, staging, client, wid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one message kill the consumer
                logger.error(
                    "stage_consumer_unhandled stage=%s: %s", stage, exc, exc_info=True
                )
                # Nack without requeue → dead-letter via DLX (fail-closed).
                with _suppress():
                    await message.reject(requeue=False)


async def _handle_message(stage, message, store, staging, client, wid) -> None:
    from utils.metrics import AMQP_CONSUMED, AMQP_DLQ

    msg = StageMessage.from_bytes(message.body)
    job = store.get(msg.job_id)
    if job is None:
        # Orphan message (job purged) — ack and drop.
        await message.ack()
        AMQP_CONSUMED.labels(stage=stage, outcome="skip").inc()
        return

    # Idempotency short-circuit: already processed with the same stamp.
    if staging.is_partition_done(
        msg.job_id, msg.partition_id, config_hash=msg.config_hash
    ):
        await message.ack()
        AMQP_CONSUMED.labels(stage=stage, outcome="skip").inc()
        logger.debug(
            "stage_consumer_idempotent_skip job=%s partition=%d",
            msg.job_id,
            msg.partition_id,
        )
        await _maybe_advance(stage, msg, store, staging, client)
        return

    # Claim THIS partition (CAS). A duplicate delivery loses the claim → skip.
    claimed = await asyncio.to_thread(
        staging.claim_partition, msg.job_id, msg.partition_id, wid
    )
    if not claimed:
        await message.ack()
        AMQP_CONSUMED.labels(stage=stage, outcome="skip").inc()
        return

    try:
        processed = await asyncio.to_thread(
            _process_partition_sync, stage, msg, job, store, staging
        )
        await asyncio.to_thread(
            staging.complete_partition,
            msg.job_id,
            msg.partition_id,
            config_hash=msg.config_hash,
        )
        await message.ack()
        AMQP_CONSUMED.labels(stage=stage, outcome="done").inc()
        logger.info(
            "stage_consumer_done stage=%s job=%s partition=%d processed=%d",
            stage,
            msg.job_id,
            msg.partition_id,
            processed,
        )
        await _maybe_advance(stage, msg, store, staging, client)
    except Exception as exc:
        attempt = await asyncio.to_thread(
            staging.record_partition_error,
            msg.job_id,
            msg.partition_id,
            error_code=type(exc).__name__,
            error_message=str(exc)[:400],
        )
        max_attempts = int(os.environ.get("MEDANON_AMQP_MAX_ATTEMPTS", "5"))
        if attempt >= max_attempts:
            await asyncio.to_thread(
                staging.dead_letter_partition,
                msg.job_id,
                msg.partition_id,
                stage=stage,
                error_code=type(exc).__name__,
                error_message=str(exc)[:400],
                config_hash=msg.config_hash,
            )
            await message.reject(requeue=False)  # → DLX → .dlq
            AMQP_DLQ.labels(stage=stage).inc()
            AMQP_CONSUMED.labels(stage=stage, outcome="dead").inc()
            logger.error(
                "stage_consumer_dead_letter stage=%s job=%s partition=%d attempts=%d",
                stage,
                msg.job_id,
                msg.partition_id,
                attempt,
            )
        else:
            # Republish to the retry queue (delayed redelivery) with attempt+1.
            retry_msg = StageMessage(
                workflow_id=msg.workflow_id,
                job_id=msg.job_id,
                step_id=msg.step_id,
                stage=msg.stage,
                partition_id=msg.partition_id,
                config_hash=msg.config_hash,
                attempt=attempt + 1,
                tenant=msg.tenant,
                trace_id=msg.trace_id,
            )
            with _suppress():
                await client.publish(retry_msg, kind="retry")
            await message.ack()  # original consumed; retry copy is in flight
            AMQP_CONSUMED.labels(stage=stage, outcome="retry").inc()
            logger.warning(
                "stage_consumer_retry stage=%s job=%s partition=%d attempt=%d: %s",
                stage,
                msg.job_id,
                msg.partition_id,
                attempt,
                exc,
            )


def _process_partition_sync(stage, msg: StageMessage, job, store, staging) -> int:
    """Run the shared per-partition processor for the deid stage.

    Non-deid stages (score/upload) are job-granular placeholders for now — they
    ack immediately. The deid stage is where the heavy de-identification runs.
    """
    if stage != "deid":
        # score/upload stages operate at job granularity; the partition-level
        # message just advances the join. Real score/upload work is driven by
        # the executor; here we no-op so the join logic still fires.
        return 0

    from pipeline.config.service import get_settings
    from pipeline.jobs.staged_worker._core import process_one_partition
    from pipeline.jobs.summary import JobSummaryCollector
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params or {}
    profile = params.get("config_profile") or params.get("profile") or "auto"
    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()
    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )
    output_dir = os.environ.get("MEDANON_OUTPUT_DIR", "/output")

    # Resolve the representative resource_type for the partition.
    resource_type = "Unknown"
    for row in staging.iter_partition(job.id, "Unknown", msg.partition_id):
        resource_type = row.get("resource_type", "Unknown")
        break

    processed, _shard = process_one_partition(
        job,
        staging,
        settings,
        pseudonymizer,
        "skip",
        output_dir,
        "amqp_deid",
        collector,
        resource_type,
        msg.partition_id,
    )
    return processed


async def _maybe_advance(stage, msg: StageMessage, store, staging, client) -> None:
    """When all partitions are done, advance the workflow step + next stage.

    Postgres (count_open_partitions == 0) — not the broker — decides the join.
    Only the worker that observes zero open partitions publishes downstream, and
    the workflow-step CAS guarantees a single advance even under a race.
    """
    open_count = await asyncio.to_thread(staging.count_open_partitions, msg.job_id)
    if open_count > 0:
        return

    # Advance the owning workflow step (if this job is part of a workflow).
    try:
        from pipeline.workflows import get_workflow_engine

        engine = get_workflow_engine()
        if engine is not None and msg.workflow_id:
            # Mark the job done so the engine's terminal hook advances the DAG.
            from domain.jobs import JobStatus

            job = store.get(msg.job_id)
            if job is not None and job.status != JobStatus.DONE:
                job.status = JobStatus.DONE
                store.update(job)
                engine.on_job_terminal(job)
    except Exception as exc:
        logger.warning("stage_advance_workflow_failed job=%s: %s", msg.job_id, exc)

    # Publish the next stage's message (job-granular) if there is one.
    next_stage = _next_stage(stage)
    if next_stage is None:
        return
    nxt = StageMessage(
        workflow_id=msg.workflow_id,
        job_id=msg.job_id,
        step_id=next_stage,
        stage=next_stage,
        partition_id=0,
        config_hash=msg.config_hash,
        tenant=msg.tenant,
        trace_id=msg.trace_id,
    )
    with _suppress():
        await client.publish(nxt)
    logger.info("stage_join_advance job=%s %s -> %s", msg.job_id, stage, next_stage)


def _next_stage(stage: str) -> str | None:
    order = ["deid", "score", "upload"]
    try:
        i = order.index(stage)
    except ValueError:
        return None
    return order[i + 1] if i + 1 < len(order) else None


class _suppress:
    """Tiny context manager: swallow + log exceptions in best-effort steps."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            logger.debug("stage_consumer_best_effort_failed: %s", exc)
        return True
