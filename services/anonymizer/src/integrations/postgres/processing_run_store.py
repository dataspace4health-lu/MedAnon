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
        """Idempotently reconcile an existing table with the shape this store expects.

        The ``medanon.processing_runs`` table is created by the app-db init; this
        backfills the Trust Gate column for deployments that predate it and
        migrates ``created_at`` off the legacy ``TEXT`` type.  Both steps are
        no-ops once applied.

        ``created_at`` was originally declared ``TEXT``  a direct port of the
        SQLite DDL.  Every windowed aggregate in :meth:`get_stats` compares it
        against ``NOW()``, which PostgreSQL rejects outright ("operator does not
        exist: text >= timestamp with time zone"), so the whole stats endpoint
        500s.  The ``USING`` cast parses the ISO-8601 strings this store has
        always written, and restores the ``created_at DESC`` index for the
        window scan.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE medanon.processing_runs "
                        "ADD COLUMN IF NOT EXISTS trust_passport JSONB"
                    )
                    cur.execute(
                        """
                        DO $$
                        BEGIN
                            IF EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_schema = 'medanon'
                                  AND table_name   = 'processing_runs'
                                  AND column_name  = 'created_at'
                                  AND data_type   <> 'timestamp with time zone'
                            ) THEN
                                ALTER TABLE medanon.processing_runs
                                    ALTER COLUMN created_at TYPE TIMESTAMPTZ
                                    USING created_at::timestamptz;
                            END IF;
                        END $$;
                        """
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

    def get_stats(
        self, window_days: int = 90, config_profile: str | None = None
    ) -> dict:
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
                    #
                    # Key names must match what ScoreCollector.summary() persists
                    # (pipeline/scoring/engine.py): `avg_composite`, `avg_utility`,
                    # `avg_quality`, `batch_privacy`, `pii_leak_blocked`.  There is
                    # no `privacy_score`/`utility_score`/`quality_score`/`blocked`
                    # key  reading those yields SQL NULL and the dashboard renders
                    # an em dash for every average and 0 for every block.
                    #
                    # Scale: `avg_composite` is already 0-100, but `avg_utility` and
                    # `avg_quality` are 0-1 module scores and privacy is derived from
                    # `1 - risk_score` (clamped, mirroring engine._composite).  The UI
                    # renders `Math.round(v)%`, so normalise all three to 0-100 here.
                    window_cutoff = f"NOW() - INTERVAL '{window_days} days'"
                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*) FILTER (WHERE score IS NOT NULL)   AS scored_runs,
                            COUNT(*) FILTER (
                                WHERE COALESCE(
                                    (score->>'pii_leak_blocked')::boolean, false
                                )
                            )                                            AS blocked_runs,
                            AVG((score->>'avg_composite')::float)        AS avg_composite,
                            AVG(
                                GREATEST(0.0, LEAST(1.0,
                                    1.0 - (score->'batch_privacy'->>'risk_score')::float
                                )) * 100.0
                            )                                            AS avg_privacy,
                            AVG((score->>'avg_utility')::float * 100.0)  AS avg_utility,
                            AVG((score->>'avg_quality')::float * 100.0)  AS avg_quality
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
