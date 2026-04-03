"""PostgreSQL staging store for large-scale de-identification jobs.

Provides an intermediate table so that Phase 1 (FHIR fetch) and Phase 2
(de-identify + write NDJSON) are fully decoupled:

  - A crash after Phase 1 completes → Phase 2 restarts from staging, no FHIR re-fetch.
  - ON CONFLICT DO NOTHING on (job_id, resource_id) → automatic dedup across pages.
  - SELECT … FOR UPDATE SKIP LOCKED → safe for future parallel workers.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.staging")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.staged_resources (
    id            BIGSERIAL PRIMARY KEY,
    job_id        TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_json JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,
    CONSTRAINT uq_job_resource UNIQUE (job_id, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_staged_job_status
    ON medanon.staged_resources (job_id, status);

CREATE INDEX IF NOT EXISTS idx_staged_expires
    ON medanon.staged_resources (expires_at)
    WHERE expires_at IS NOT NULL;
"""


class StagingStore:
    """Thread-safe PostgreSQL staging store.

    All public methods acquire a connection from the pool for the duration of
    the call and release it immediately after.  Safe to call from multiple
    threads (asyncio.to_thread workers).
    """

    def __init__(self, db_url: str, retention_days: int = 30) -> None:
        self._db_url = db_url
        self._retention_days = retention_days
        self._pool: ThreadedConnectionPool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create schema + table + indexes if they don't already exist."""
        if self._pool is None:
            self._pool = ThreadedConnectionPool(2, 10, self._db_url)
        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(_DDL)
            logger.info("staging schema ready (retention_days=%d)", self._retention_days)
        finally:
            self._pool.putconn(conn)

    def _get_conn(self):
        if self._pool is None:
            raise RuntimeError("StagingStore.ensure_schema() must be called before use")
        return self._pool.getconn()

    def _put_conn(self, conn) -> None:
        if self._pool:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def stage_batch(self, job_id: str, resources: list[dict]) -> int:
        """Insert resources into staging, skipping duplicates.

        Returns the number of rows actually inserted (may be < len(resources)
        when duplicates are skipped via ON CONFLICT DO NOTHING).
        """
        if not resources:
            return 0

        expires_sql = (
            f"NOW() + INTERVAL '{self._retention_days} days'"
            if self._retention_days > 0
            else "NULL"
        )

        rows = []
        for idx, resource in enumerate(resources):
            rtype = resource.get("resourceType", "Unknown")
            rid = resource.get("id")
            resource_id = f"{rtype}/{rid}" if rid else f"{rtype}/auto-{idx}"
            rows.append((job_id, resource_id, rtype, json.dumps(resource)))

        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO medanon.staged_resources
                            (job_id, resource_id, resource_type, resource_json, expires_at)
                        VALUES %s
                        ON CONFLICT (job_id, resource_id) DO NOTHING
                        """,
                        [(job_id, rid, rtype, rjson, None)
                         for job_id, rid, rtype, rjson in rows],
                        template=f"(%s, %s, %s, %s::jsonb, {expires_sql})",
                    )
                    return cur.rowcount
        finally:
            self._put_conn(conn)

    def mark_done(self, job_id: str, staged_ids: list[int]) -> None:
        """Mark a list of staged row IDs as processed (processing → done)."""
        if not staged_ids:
            return
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'done', processed_at = NOW()
                         WHERE job_id = %s AND id = ANY(%s)
                           AND status = 'processing'
                        """,
                        (job_id, staged_ids),
                    )
        finally:
            self._put_conn(conn)

    def mark_error(self, job_id: str, staged_id: int, error: str) -> None:
        """Mark a single staged row as errored."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'error', error = %s, processed_at = NOW()
                         WHERE job_id = %s AND id = %s
                        """,
                        (error[:2000], job_id, staged_id),
                    )
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_pending_batch(self, job_id: str, limit: int = 100) -> list[dict]:
        """Return up to *limit* pending rows, atomically marking them 'processing'.

        Uses a CTE with ``FOR UPDATE SKIP LOCKED`` to claim rows in one
        statement — no window between SELECT and UPDATE where a crash could
        leave rows invisible to other workers.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        WITH batch AS (
                            SELECT id
                              FROM medanon.staged_resources
                             WHERE job_id = %s AND status = 'pending'
                             ORDER BY id
                             LIMIT %s
                             FOR UPDATE SKIP LOCKED
                        )
                        UPDATE medanon.staged_resources sr
                           SET status = 'processing'
                          FROM batch
                         WHERE sr.id = batch.id
                        RETURNING sr.id, sr.resource_id, sr.resource_type, sr.resource_json
                        """,
                        (job_id, limit),
                    )
                    return [dict(row) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def get_all_resources(self, job_id: str) -> Iterable[dict]:
        """Iterate over every resource for a job (for re-processing)."""
        conn = self._get_conn()
        try:
            with conn.cursor(
                name=f"cursor_all_{job_id}",
                cursor_factory=psycopg2.extras.RealDictCursor,
            ) as cur:
                cur.execute(
                    """
                    SELECT id, resource_id, resource_type, resource_json
                      FROM medanon.staged_resources
                     WHERE job_id = %s
                     ORDER BY id
                    """,
                    (job_id,),
                )
                for row in cur:
                    yield dict(row)
        finally:
            self._put_conn(conn)

    def count_by_status(self, job_id: str) -> dict[str, int]:
        """Return ``{pending, done, error, total}`` counts for a job."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT status, COUNT(*) AS n
                          FROM medanon.staged_resources
                         WHERE job_id = %s
                         GROUP BY status
                        """,
                        (job_id,),
                    )
                    result: dict[str, int] = {"pending": 0, "done": 0, "error": 0}
                    for status, n in cur.fetchall():
                        result[status] = int(n)
                    result["total"] = sum(result.values())
                    return result
        finally:
            self._put_conn(conn)

    def reset_pending(self, job_id: str) -> int:
        """Reset done/error rows for a job back to 'pending' (for re-processing).

        Rows in ``processing`` state are left untouched to avoid interfering
        with an active worker.  Use ``recover_stale_processing`` to reclaim
        rows that have been stuck in ``processing`` for too long.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'pending', processed_at = NULL, error = NULL
                         WHERE job_id = %s AND status != 'processing'
                        """,
                        (job_id,),
                    )
                    return cur.rowcount
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Delete rows whose ``expires_at`` has passed, in batches.

        Deletes up to 10 000 rows per transaction to avoid long-running
        locks.  Loops until no more expired rows remain.  Returns total
        deleted count.
        """
        total_deleted = 0
        conn = self._get_conn()
        try:
            while True:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            DELETE FROM medanon.staged_resources
                             WHERE id IN (
                                SELECT id FROM medanon.staged_resources
                                 WHERE expires_at < NOW()
                                 LIMIT 10000
                             )
                            """
                        )
                        deleted = cur.rowcount
                total_deleted += deleted
                if deleted < 10000:
                    break
            return total_deleted
        finally:
            self._put_conn(conn)

    def recover_stale_processing(self, timeout_minutes: int = 10) -> int:
        """Reset rows stuck in ``processing`` for longer than *timeout_minutes*.

        Rows that have been in ``processing`` state for too long indicate a
        worker crash between ``get_pending_batch`` and ``mark_done``.
        Resetting them to ``pending`` allows them to be picked up again.

        Returns the number of rows recovered.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'pending'
                         WHERE status = 'processing'
                           AND fetched_at < NOW() - INTERVAL '%s minutes'
                        """,
                        (timeout_minutes,),
                    )
                    recovered = cur.rowcount
            if recovered:
                logger.info("recovered %d stale processing rows (timeout=%dmin)", recovered, timeout_minutes)
            return recovered
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            try:
                self._pool.closeall()
            except Exception:
                pass
            self._pool = None
