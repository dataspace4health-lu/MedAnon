"""PostgreSQL-backed processing run store.

Persists per-request de-identification metadata and scoring aggregates.
Follows the same pool pattern as ``PostgresConfigStore``.
"""

from __future__ import annotations

import logging

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.processing_run_store")


class PostgresProcessingRunStore:
    """PostgreSQL-backed processing run persistence."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def _get_conn(self):
        from integrations.postgres.pool import get_conn
        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn
        safe_putconn(self._pool, conn)

    def create(self, run: dict) -> None:
        """Insert a processing run record."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.processing_runs
                            (id, created_at, endpoint, config_profile,
                             resource_count, error_count, duration_ms,
                             input_type, summary, score)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            run["id"],
                            run["created_at"],
                            run["endpoint"],
                            run.get("config_profile", "auto"),
                            run.get("resource_count", 0),
                            run.get("error_count", 0),
                            run.get("duration_ms", 0),
                            run.get("input_type", ""),
                            psycopg2.extras.Json(run.get("summary")),
                            psycopg2.extras.Json(run.get("score")),
                        ),
                    )
        finally:
            self._put_conn(conn)

    def get(self, run_id: str) -> dict | None:
        """Fetch a single run by ID."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor
                ) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.processing_runs WHERE id = %s",
                        (run_id,),
                    )
                    row = cur.fetchone()
            return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def list_runs(
        self,
        endpoint: str | None = None,
        config_profile: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List runs with optional filters, ordered by created_at DESC.

        Returns ``(rows, total_count)``.
        """
        where_clauses: list[str] = []
        params: list = []

        if endpoint:
            where_clauses.append("endpoint = %s")
            params.append(endpoint)
        if config_profile:
            where_clauses.append("config_profile = %s")
            params.append(config_profile)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor
                ) as cur:
                    cur.execute(
                        f"SELECT COUNT(*) AS cnt FROM medanon.processing_runs{where_sql}",
                        params,
                    )
                    total = cur.fetchone()["cnt"]

                    cur.execute(
                        f"SELECT * FROM medanon.processing_runs{where_sql}"
                        " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        params + [limit, offset],
                    )
                    rows = [dict(r) for r in cur.fetchall()]
            return rows, total
        finally:
            self._put_conn(conn)

    def get_stats(self) -> dict:
        """Aggregate statistics across all runs."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor
                ) as cur:
                    cur.execute(
                        """
                        SELECT
                            COUNT(*)                                             AS total_runs,
                            AVG((score->>'avg_composite')::float)                AS avg_composite,
                            COALESCE(SUM(resource_count), 0)                    AS total_resources
                        FROM medanon.processing_runs
                        """
                    )
                    agg = cur.fetchone()

                    cur.execute(
                        """
                        SELECT endpoint, COUNT(*) AS cnt
                        FROM medanon.processing_runs
                        GROUP BY endpoint
                        ORDER BY cnt DESC
                        """
                    )
                    by_endpoint = {
                        r["endpoint"]: r["cnt"] for r in cur.fetchall()
                    }

                    cur.execute(
                        """
                        SELECT config_profile, COUNT(*) AS cnt
                        FROM medanon.processing_runs
                        GROUP BY config_profile
                        ORDER BY cnt DESC
                        """
                    )
                    by_profile = {
                        r["config_profile"]: r["cnt"] for r in cur.fetchall()
                    }

            avg = agg["avg_composite"]
            return {
                "total_runs": agg["total_runs"],
                "avg_composite": round(float(avg), 1) if avg is not None else None,
                "total_resources": agg["total_resources"] or 0,
                "runs_by_endpoint": by_endpoint,
                "runs_by_profile": by_profile,
            }
        finally:
            self._put_conn(conn)

    def delete_before(self, before_iso: str) -> int:
        """Purge runs created before the given ISO timestamp. Returns count deleted."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM medanon.processing_runs WHERE created_at < %s",
                        (before_iso,),
                    )
                    return cur.rowcount
        finally:
            self._put_conn(conn)
