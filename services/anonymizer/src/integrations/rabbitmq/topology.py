"""RabbitMQ topology — exchanges, queues, dead-lettering, retry.

Idempotently declared at startup. Layout (per stage in
``messages.STAGES`` except ``fetch``, which is produced locally):

    exchange  medanon.workflow  (topic, durable)
        └─ binding  wf.{stage}.#  → queue  medanon.stage.{stage}
    exchange  medanon.dlx       (topic, durable)   [dead-letter + retry routing]
        ├─ binding  dlq.{stage}.#   → queue  medanon.stage.{stage}.dlq
        └─ binding  retry.{stage}.# → queue  medanon.stage.{stage}.retry
                                       (x-message-ttl → re-deliver to main)

Each main queue dead-letters to ``medanon.dlx`` so a rejected message lands in
its ``.dlq``. The ``.retry`` queue holds a message for a TTL then dead-letters
it back to the workflow exchange for another attempt (classic delayed-retry).

Routing keys are ``{kind}.{stage}.{tenant}`` so per-tenant queues become a
binding change once multi-tenancy lands; today ``tenant`` is always ``default``.
"""

from __future__ import annotations

import os

WORKFLOW_EXCHANGE = "medanon.workflow"
DLX_EXCHANGE = "medanon.dlx"

# Delay (ms) a message waits in the retry queue before being redelivered.
RETRY_TTL_MS = int(os.environ.get("MEDANON_AMQP_RETRY_TTL_MS", "30000"))


def queue_name(stage: str) -> str:
    return f"medanon.stage.{stage}"


def dlq_name(stage: str) -> str:
    return f"medanon.stage.{stage}.dlq"


def retry_queue_name(stage: str) -> str:
    return f"medanon.stage.{stage}.retry"


def routing_key(stage: str, tenant: str = "default", *, kind: str = "wf") -> str:
    """Routing key for publishing to a stage. ``kind`` ∈ {wf, retry, dlq}."""
    return f"{kind}.{stage}.{tenant}"


def _queue_type_args() -> dict:
    qt = os.environ.get("MEDANON_AMQP_QUEUE_TYPE", "classic").strip().lower()
    # quorum queues are recommended on K8s; classic for single-node compose.
    return {"x-queue-type": "quorum"} if qt == "quorum" else {}


async def declare_topology(channel, stages: list[str]) -> dict:
    """Idempotently declare exchanges + per-stage queues. Returns the queues.

    *channel* is an aio-pika channel. Safe to call repeatedly (RabbitMQ DDL is
    idempotent when arguments match).
    """
    import aio_pika

    wf_exchange = await channel.declare_exchange(
        WORKFLOW_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
    )
    dlx_exchange = await channel.declare_exchange(
        DLX_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
    )

    queues: dict[str, object] = {}
    base_args = _queue_type_args()

    for stage in stages:
        # Main work queue — dead-letters to the DLX on reject/expire.
        main = await channel.declare_queue(
            queue_name(stage),
            durable=True,
            arguments={
                **base_args,
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-dead-letter-routing-key": routing_key(stage, kind="dlq"),
            },
        )
        await main.bind(wf_exchange, routing_key=f"wf.{stage}.#")

        # Dead-letter queue — terminal failures land here for operator triage.
        dlq = await channel.declare_queue(
            dlq_name(stage), durable=True, arguments=base_args
        )
        await dlq.bind(dlx_exchange, routing_key=f"dlq.{stage}.#")

        # Retry queue — holds for a TTL then dead-letters BACK to the workflow
        # exchange (delayed redelivery to the main queue).
        retry = await channel.declare_queue(
            retry_queue_name(stage),
            durable=True,
            arguments={
                **base_args,
                "x-message-ttl": RETRY_TTL_MS,
                "x-dead-letter-exchange": WORKFLOW_EXCHANGE,
                "x-dead-letter-routing-key": routing_key(stage, kind="wf"),
            },
        )
        await retry.bind(dlx_exchange, routing_key=f"retry.{stage}.#")

        queues[stage] = main

    return queues
