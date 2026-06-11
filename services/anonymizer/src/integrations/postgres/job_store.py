"""PostgreSQL-backed job store for long-running pipeline operations.

Drop-in replacement for ``SqliteJobStore`` that uses a shared PostgreSQL
database via the ``integrations.postgres.pool`` connection pool.

Key advantages over SQLite:
  - ``next_pending()`` uses ``FOR UPDATE SKIP LOCKED`` so that multiple
    workers can claim jobs concurrently without contention.
  - ``notify_new_job()`` uses ``NOTIFY`` to wake idle workers instantly
    (instead of SQLite's 2-second polling).
  - Full ACID with row-level locking; safe for multi-instance deployments.
"""

from __future__ import annotations

import json
import logging
import select
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from domain.jobs import Job, JobStatus

logger = logging.getLogger("medanon.jobs.postgres")

_NOTIFY_CHANNEL = "medanon_jobs"


class PostgresJobStore:
    """PostgreSQL-backed job store using a shared connection pool."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    # ------------------------------------------------------------------
    # Public API (same interface as SqliteJobStore / RedisJobStore)
    # ------------------------------------------------------------------

    def create(self, job_type: str, params: dict) -> Job:
        """Persist a new PENDING job and return it."""
        job = Job(id=str(uuid.uuid4()), type=job_type, params=params)
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.jobs
                            (id, type, params, status, created_at, updated_at,
                             result_path, error, checkpoint_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            job.id,
                            job.type,
                            json.dumps(job.params),
                            job.status.value,
                            job.created_at,
                            job.updated_at,
                            job.result_path,
                            job.error,
                            json.dumps(job.checkpoint_data)
                            if job.checkpoint_data is not None
                            else None,
                        ),
                    )
        finally:
            self._put_conn(conn)
        logger.info("job_created id=%s type=%s", job.id, job.type)
        return job

    def get(self, job_id: str) -> Job | None:
        """Fetch a job by ID; returns None if not found."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM medanon.jobs WHERE id = %s", (job_id,))
                    row = cur.fetchone()
            return _row_to_job(row) if row else None
        finally:
            self._put_conn(conn)

    def update(self, job: Job) -> None:
        """Persist status, result_path, error, and checkpoint_data changes."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.jobs
                           SET status = %s, updated_at = %s, result_path = %s,
                               error = %s, checkpoint_data = %s
                         WHERE id = %s
                        """,
                        (
                            job.status.value,
                            job.updated_at,
                            job.result_path,
                            job.error,
                            json.dumps(job.checkpoint_data)
                            if job.checkpoint_data is not None
                            else None,
                            job.id,
                        ),
                    )
        finally:
            self._put_conn(conn)

    def update_checkpoint(self, job_id: str, data: dict) -> None:
        """Persist only the checkpoint_data column for an in-progress job."""
        updated_at = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.jobs
                           SET checkpoint_data = %s, updated_at = %s
                         WHERE id = %s
                        """,
                        (json.dumps(data), updated_at, job_id),
                    )
        finally:
            self._put_conn(conn)

    def next_pending(self) -> Job | None:
        """Atomically claim the oldest PENDING job using FOR UPDATE SKIP LOCKED.

        Returns the job (now marked 'running') or None if the queue is empty.
        Unlike SQLite's plain SELECT, concurrent workers will never claim the
        same job.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        WITH next AS (
                            SELECT id
                              FROM medanon.jobs
                             WHERE status = 'pending'
                             ORDER BY created_at
                             LIMIT 1
                             FOR UPDATE SKIP LOCKED
                        )
                        UPDATE medanon.jobs j
                           SET status = 'running',
                               updated_at = %s
                          FROM next
                         WHERE j.id = next.id
                        RETURNING j.*
                        """,
                        (datetime.now(timezone.utc).isoformat(),),
                    )
                    row = cur.fetchone()
            return _row_to_job(row) if row else None
        finally:
            self._put_conn(conn)

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        before_created_at: str | None = None,
    ) -> list[Job]:
        """List jobs with optional filtering, ordered by created_at descending.

        *before_created_at* enables keyset pagination: pass the ``created_at``
        of the last job from the previous page to avoid an O(n) offset scan.
        """
        query = "SELECT * FROM medanon.jobs WHERE TRUE"
        params: list = []
        if status:
            query += " AND status = %s"
            params.append(status)
        if job_type:
            query += " AND type = %s"
            params.append(job_type)
        if before_created_at:
            query += " AND created_at < %s"
            params.append(before_created_at)
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [_row_to_job(row) for row in rows]
        finally:
            self._put_conn(conn)

    def notify_new_job(self, job_id: str) -> None:
        """Send a PostgreSQL NOTIFY to wake idle workers."""
        conn = self._get_conn()
        try:
            # NOTIFY must be outside a WITH block that auto-commits, because
            # psycopg2 wraps statements in transactions.  We commit explicitly.
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(f"NOTIFY {_NOTIFY_CHANNEL}, %s", (job_id,))
            finally:
                conn.autocommit = False
        finally:
            self._put_conn(conn)

    def cancel(self, job_id: str) -> bool:
        """Mark a pending or running job as cancelled.

        Returns True if the status was updated, False if the job was already
        in a terminal state or not found.
        """
        updated_at = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.jobs
                           SET status = 'cancelled', updated_at = %s
                         WHERE id = %s AND status IN ('pending', 'running')
                        """,
                        (updated_at, job_id),
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def wait_for_job(self, timeout: float = 2.0) -> str | None:
        """Block until a NOTIFY arrives on the jobs channel, or *timeout* expires.

        Uses a dedicated connection (not from the pool) with LISTEN so the
        pooled connections remain available for queries.

        Returns the job_id payload, or None on timeout.
        """
        conn = self._get_conn()
        try:
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(f"LISTEN {_NOTIFY_CHANNEL}")
                # select.select blocks until the socket is readable or timeout
                if select.select([conn], [], [], timeout) == ([], [], []):
                    return None
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    return notify.payload or None
                return None
            finally:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"UNLISTEN {_NOTIFY_CHANNEL}")
                except Exception:
                    pass
                conn.autocommit = False
        finally:
            self._put_conn(conn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_job(row: dict) -> Job:
    """Convert a psycopg2 RealDictRow to a Job dataclass."""
    raw_params = row["params"]
    params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params

    raw_cp = row.get("checkpoint_data")
    if raw_cp is None:
        checkpoint_data = None
    elif isinstance(raw_cp, str):
        checkpoint_data = json.loads(raw_cp)
    else:
        checkpoint_data = raw_cp  # already a dict from JSONB

    created_at = row["created_at"]
    if not isinstance(created_at, str):
        created_at = created_at.isoformat() if created_at else None

    updated_at = row["updated_at"]
    if not isinstance(updated_at, str):
        updated_at = updated_at.isoformat() if updated_at else None

    return Job(
        id=row["id"],
        type=row["type"],
        params=params,
        status=JobStatus(row["status"]),
        created_at=created_at,
        updated_at=updated_at,
        result_path=row.get("result_path"),
        error=row.get("error"),
        checkpoint_data=checkpoint_data,
    )
