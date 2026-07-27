"""PostgreSQL-backed job detail cache.

Stores per-job parsed result (resource counts, field analysis) so the frontend
can load previously-parsed results without re-downloading the NDJSON output.
Follows the same pool pattern as PostgresProcessingRunStore.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.job_detail_store")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.job_details (
    job_id     TEXT PRIMARY KEY,
    detail     JSONB NOT NULL,
    created_at TEXT NOT NULL
);
"""


class PostgresJobDetailStore:
    """PostgreSQL-backed job detail cache."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def ensure_schema(self) -> None:
        """Create schema/table if missing.

        ``CREATE … IF NOT EXISTS`` is not atomic against implicit composite-type
        creation, so concurrent startup across app workers can collide on
        ``pg_type``/``pg_class``. The objects exist either way  treat those
        specific races as success (mirrors ``workflow_store``).
        """
        from psycopg2 import errors as _pg_errors

        _benign = (
            _pg_errors.DuplicateTable,
            _pg_errors.DuplicateObject,
            _pg_errors.UniqueViolation,
        )
        conn = self._get_conn()
        try:
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(_DDL)
                logger.info("job detail schema ready")
            except _benign:
                conn.rollback()
                logger.debug("job detail schema already created concurrently")
        finally:
            self._put_conn(conn)

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def get(self, job_id: str) -> dict | None:
        """Return cached detail for *job_id*, or None if not found."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT detail FROM medanon.job_details WHERE job_id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
            if row is None:
                return None
            detail = row["detail"]
            # psycopg2 auto-deserialises JSONB → dict; guard against TEXT fallback
            return detail if isinstance(detail, dict) else json.loads(detail)
        finally:
            self._put_conn(conn)

    def set(self, job_id: str, detail: dict) -> None:
        """Upsert cached detail for *job_id*."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.job_details (job_id, detail, created_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (job_id) DO UPDATE SET detail = EXCLUDED.detail
                        """,
                        (job_id, psycopg2.extras.Json(detail), now),
                    )
        finally:
            self._put_conn(conn)

    def delete(self, job_id: str) -> None:
        """Remove cached detail for *job_id* (no-op if absent)."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM medanon.job_details WHERE job_id = %s",
                        (job_id,),
                    )
        finally:
            self._put_conn(conn)
