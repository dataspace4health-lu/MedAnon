"""AMQP client  robust connection + confirm-mode publisher.

Wraps aio-pika with:
  * ``connect_robust`` auto-reconnect (re-declares topology on reconnect),
  * publisher confirms (a publish that isn't acked within the timeout raises),
  * a circuit breaker so a dead broker fails fast instead of hanging publishes,
  * Prometheus counters.

The :class:`BrokerPort` Protocol lets tests inject a ``FakeBroker`` without a
real RabbitMQ. aio-pika is imported lazily inside ``connect`` so importing this
module never requires the dependency.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Protocol, runtime_checkable

from integrations.rabbitmq.messages import StageMessage
from integrations.rabbitmq.topology import (
    WORKFLOW_EXCHANGE,
    declare_topology,
    routing_key,
)
from utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger("medanon.amqp")

_PUBLISH_TIMEOUT = float(os.environ.get("MEDANON_AMQP_PUBLISH_TIMEOUT_SEC", "10"))


@runtime_checkable
class BrokerPort(Protocol):
    """Minimal publish/consume surface the pipeline depends on."""

    async def publish(self, msg: StageMessage, *, kind: str = "wf") -> None: ...

    async def queue_depth(self, stage: str) -> int: ...


class AmqpClient:
    """Robust aio-pika connection with a confirm-mode publish channel."""

    def __init__(self, url: str, stages: list[str]) -> None:
        self._url = url
        self._stages = stages
        self._conn = None
        self._channel = None
        self._exchange = None
        self._lock = asyncio.Lock()
        self._cb = CircuitBreaker(
            name="amqp",
            failure_threshold=int(
                os.environ.get("MEDANON_AMQP_CB_FAILURE_THRESHOLD", "5")
            ),
            recovery_timeout_sec=float(
                os.environ.get("MEDANON_AMQP_CB_RECOVERY_TIMEOUT_SEC", "30")
            ),
        )

    async def connect(self) -> None:
        """Open a robust connection + confirm channel and declare topology."""
        import aio_pika

        async with self._lock:
            if self._conn is not None and not self._conn.is_closed:
                return
            self._conn = await aio_pika.connect_robust(self._url)
            # publisher_confirms=True → publish() awaits the broker ack.
            self._channel = await self._conn.channel(publisher_confirms=True)
            await self._channel.set_qos(
                prefetch_count=int(os.environ.get("MEDANON_AMQP_PREFETCH", "2"))
            )
            await declare_topology(self._channel, self._stages)
            self._exchange = await self._channel.get_exchange(WORKFLOW_EXCHANGE)
            logger.info("amqp_connected stages=%s", ",".join(self._stages))

    async def publish(self, msg: StageMessage, *, kind: str = "wf") -> None:
        """Publish a stage message with a publisher-confirm + timeout.

        Raises on circuit-open or confirm timeout  callers treat a publish
        failure as "leave it in the ledger; the reaper will republish".
        """
        import aio_pika
        from utils.metrics import AMQP_PUBLISH_FAILURES, AMQP_PUBLISHED

        if not self._cb.allow_request():
            raise RuntimeError("AMQP circuit breaker is OPEN")
        try:
            if self._exchange is None:
                await self.connect()
            body = aio_pika.Message(
                msg.to_bytes(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=msg.message_id,
                content_type="application/json",
            )
            rk = routing_key(msg.stage, msg.tenant, kind=kind)
            await asyncio.wait_for(
                self._exchange.publish(body, routing_key=rk),
                timeout=_PUBLISH_TIMEOUT,
            )
            self._cb.record_success()
            AMQP_PUBLISHED.labels(stage=msg.stage).inc()
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                self._cb.record_timeout()
            else:
                self._cb.record_failure()
            AMQP_PUBLISH_FAILURES.inc()
            logger.warning("amqp_publish_failed stage=%s: %s", msg.stage, exc)
            raise

    async def queue_depth(self, stage: str) -> int:
        """Passive-declare the stage queue and return its message count."""
        from integrations.rabbitmq.topology import queue_name

        if self._channel is None:
            await self.connect()
        q = await self._channel.declare_queue(queue_name(stage), passive=True)
        return q.declaration_result.message_count

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: AmqpClient | None = None


def amqp_enabled() -> bool:
    return bool(os.environ.get("MEDANON_AMQP_URL", "").strip())


def consumer_stages() -> list[str]:
    raw = os.environ.get("MEDANON_AMQP_CONSUMER_STAGES", "deid,score,upload")
    return [s.strip() for s in raw.split(",") if s.strip()]


async def init_amqp_client(stages: list[str] | None = None) -> AmqpClient | None:
    """Create + connect the singleton from MEDANON_AMQP_URL. None if disabled."""
    global _client
    url = os.environ.get("MEDANON_AMQP_URL", "").strip()
    if not url:
        return None
    if _client is None:
        # Declare topology for ALL post-fetch stages so producers and any
        # consumer can rely on every queue existing.
        from integrations.rabbitmq.messages import STAGES

        _client = AmqpClient(url, stages or [s for s in STAGES if s != "fetch"])
        await _client.connect()
    return _client


def get_amqp_client() -> AmqpClient | None:
    return _client


async def publish_partitions(
    broker,
    *,
    workflow_id: str,
    job_id: str,
    partition_ids: list[int],
    stage: str = "deid",
    config_hash: str = "",
    tenant: str = "default",
    trace_id: str = "",
    max_queue_depth: int | None = None,
) -> int:
    """Publish one message per partition to *stage*, with depth backpressure.

    Returns the count published. Publish failures are swallowed per-partition
    (the partition stays ``unclaimed`` in the ledger and the reaper republishes)
    so a transient broker hiccup never aborts the whole fan-out.
    """
    if max_queue_depth is None:
        max_queue_depth = int(os.environ.get("MEDANON_AMQP_MAX_QUEUE_DEPTH", "1000"))
    published = 0
    for pid in partition_ids:
        # Backpressure: pause when the stage queue is saturated.
        try:
            depth = await broker.queue_depth(stage)
            while depth >= max_queue_depth:
                await asyncio.sleep(0.5)
                depth = await broker.queue_depth(stage)
        except Exception:  # depth probe is best-effort
            pass
        msg = StageMessage(
            workflow_id=workflow_id,
            job_id=job_id,
            step_id=stage,
            stage=stage,
            partition_id=pid,
            config_hash=config_hash,
            tenant=tenant,
            trace_id=trace_id,
        )
        try:
            await broker.publish(msg)
            published += 1
        except Exception as exc:
            logger.warning(
                "publish_partition_failed job=%s partition=%d: %s", job_id, pid, exc
            )
    return published
