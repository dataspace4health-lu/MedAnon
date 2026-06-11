"""JobStorePort — structural Protocol for async job-store backends.

Formalizes the previously implicit (duck-typed) contract shared by the three
job-store backends so a new backend (e.g. an AMQP-fronted store) and the
workflow engine can depend on an interface rather than a concrete class:

* :class:`~pipeline.jobs.store.SqliteJobStore` (polling, single-container dev)
* :class:`~integrations.postgres.job_store.PostgresJobStore` (LISTEN/NOTIFY)
* :class:`~integrations.redis.job_store.RedisJobStore` (Streams + consumer groups)

Mirrors the ``pipeline.ports.PseudonymizerPort`` convention: pure interface,
no infrastructure imports. Backends already satisfy this structurally — the
Protocol exists for type-checking and ``isinstance`` assertions, not to force
inheritance.

Optional capabilities (``cancel``, ``list_jobs``, ``get_queue_depth``,
``notify_new_job``) remain ``hasattr``-checked at call sites: not every backend
implements every one, and SQLite should not grow no-op stubs just to satisfy a
wider Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.jobs import Job


@runtime_checkable
class JobStorePort(Protocol):
    """Minimal interface every job-store backend implements."""

    def create(self, job_type: str, params: dict) -> Job:
        """Persist a new PENDING job and return it."""
        ...

    def get(self, job_id: str) -> Job | None:
        """Fetch a job by ID; return None if not found."""
        ...

    def update(self, job: Job) -> None:
        """Persist status, result_path, error, and checkpoint_data for *job*."""
        ...

    def update_checkpoint(self, job_id: str, data: dict) -> None:
        """Persist only the checkpoint_data for an in-progress job."""
        ...

    def next_pending(self) -> Job | None:
        """Atomically claim the oldest PENDING job (→ RUNNING), or None."""
        ...


@runtime_checkable
class NotifyingJobStorePort(JobStorePort, Protocol):
    """Job store that can wake idle workers when a job is enqueued.

    Redis (Streams) and Postgres (NOTIFY) implement this meaningfully; SQLite
    provides a no-op ``notify_new_job`` and polls instead.
    """

    def notify_new_job(self, job_id: str) -> None:
        """Signal workers that *job_id* is ready to claim."""
        ...
