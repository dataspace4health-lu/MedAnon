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

    def ensure_schema(self) -> None:
        """Idempotently add the ``trust_passport`` column to an existing table.

        The ``medanon.processing_runs`` table is created by the app-db init; this
        only backfills the Trust Gate column for deployments that predate it.
        ``ADD COLUMN IF NOT EXISTS`` is a no-op when the column already exists.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE medanon.processing_runs "
                        "ADD COLUMN IF NOT EXISTS trust_passport JSONB"
                    )
        finally:
            self._put_conn(conn)

    def create(self, run: dict) -> None:
        """Insert a processing run record."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.processing_runs
                            (id, created_at, endpoint, config_profile, config_hash,
                             resource_count, error_count, duration_ms,
                             input_type, summary, score, trust_passport)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            run["id"],
                            run["created_at"],
                            run["endpoint"],
                            run.get("config_profile", "auto"),
                            run.get("config_hash"),
                            run.get("resource_count", 0),
                            run.get("error_count", 0),
                            run.get("duration_ms", 0),
                            run.get("input_type", ""),
                            psycopg2.extras.Json(run.get("summary")),
                            psycopg2.extras.Json(run.get("score")),
                            psycopg2.extras.Json(run.get("trust_passport")),
                        ),
                    )
        finally:
            self._put_conn(conn)

    def get(self, run_id: str) -> dict | None:
        """Fetch a single run by ID."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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

    def get_stats(self, window_days: int = 90, config_profile: str | None = None) -> dict:
        """Aggregate statistics across recent runs.

        ``window_days`` limits the look-back window (default 90 days).
        ``config_profile`` restricts all aggregates to a single profile when set.
        All-time totals include rows outside the window; score averages and
        breakdown tables use the window to reflect recent activity.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    profile_filter = ""
                    profile_params: list = []
                    if config_profile:
                        profile_filter = "AND config_profile = %s"
                        profile_params = [config_profile]

                    # All-time totals.
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*)                         AS total_runs,
                            COALESCE(SUM(resource_count), 0) AS total_resources
                        FROM medanon.processing_runs
                        WHERE 1=1 {profile_filter}
                        """,
                        profile_params,
                    )
                    totals = cur.fetchone()

                    # Recent window aggregates.
                    window_cutoff = f"NOW() - INTERVAL '{window_days} days'"
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*) FILTER (WHERE score IS NOT NULL)   AS scored_runs,
                            COUNT(*) FILTER (
                                WHERE score IS NOT NULL
                                AND (score->>'blocked')::boolean = true
                            )                                            AS blocked_runs,
                            AVG((score->>'avg_composite')::float)        AS avg_composite,
                            AVG((score->>'privacy_score')::float)        AS avg_privacy,
                            AVG((score->>'utility_score')::float)        AS avg_utility,
                            AVG((score->>'quality_score')::float)        AS avg_quality
                        FROM medanon.processing_runs
                        WHERE created_at >= {window_cutoff} {profile_filter}
                        """,
                        profile_params,
                    )
                    agg = cur.fetchone()

                    cur.execute(
                        f"""
                        SELECT endpoint, COUNT(*) AS cnt
                        FROM medanon.processing_runs
                        WHERE created_at >= {window_cutoff} {profile_filter}
                        GROUP BY endpoint
                        ORDER BY cnt DESC
                        LIMIT 20
                        """,
                        profile_params,
                    )
                    by_endpoint = {r["endpoint"]: r["cnt"] for r in cur.fetchall()}

                    cur.execute(
                        f"""
                        SELECT config_profile, COUNT(*) AS cnt
                        FROM medanon.processing_runs
                        WHERE created_at >= {window_cutoff} {profile_filter}
                        GROUP BY config_profile
                        ORDER BY cnt DESC
                        LIMIT 20
                        """,
                        profile_params,
                    )
                    by_profile = {r["config_profile"]: r["cnt"] for r in cur.fetchall()}

            def _round(v) -> float | None:
                return round(float(v), 1) if v is not None else None

            return {
                "total_runs": totals["total_runs"],
                "scored_runs": agg["scored_runs"] or 0,
                "blocked_runs": agg["blocked_runs"] or 0,
                "avg_composite": _round(agg["avg_composite"]),
                "avg_privacy": _round(agg["avg_privacy"]),
                "avg_utility": _round(agg["avg_utility"]),
                "avg_quality": _round(agg["avg_quality"]),
                "total_resources": totals["total_resources"] or 0,
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
