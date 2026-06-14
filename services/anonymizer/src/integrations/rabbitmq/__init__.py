"""RabbitMQ integration — macro-stage work streaming (opt-in).

Public surface:
    ``StageMessage``        — versioned, metadata-only work-pointer message.
    ``AmqpClient``          — robust connection + confirm-mode publisher.
    ``BrokerPort``          — Protocol so tests can inject a FakeBroker.
    ``declare_topology``    — idempotent exchange/queue/DLX declaration.
    ``get_amqp_client`` / ``init_amqp_client`` — module-level singleton helpers.

NOTHING in a published message may contain PHI — messages carry only job and
partition identifiers. The Postgres staging tables remain the durable ledger;
RabbitMQ is transport. Importing this package never imports aio-pika at module
load time (soft dependency) — the client imports it lazily on connect.
"""

from integrations.rabbitmq.messages import (  # noqa: F401
    STAGE_QUEUES,
    StageMessage,
)
from integrations.rabbitmq.topology import (  # noqa: F401
    DLX_EXCHANGE,
    WORKFLOW_EXCHANGE,
    queue_name,
    routing_key,
)
