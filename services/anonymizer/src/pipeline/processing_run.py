"""Processing run store singleton.

Module-level holder for the active processing run store.
Initialised during FastAPI startup.

Backends (in priority order):
  1. PostgreSQL — when MEDANON_APP_DB_URL is set
  2. SQLite     — fallback, always available (default path /output/processing_runs.db)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("medanon.processing_run")

_DEFAULT_DB = "/output/processing_runs.db"

_processing_run_store: Any = None


class SqliteProcessingRunStore:
    """SQLite-backed processing run store. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get("MEDANON_PROCESSING_RUN_DB", _DEFAULT_DB)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_runs (
                    id              TEXT PRIMARY KEY,
                    created_at      TEXT NOT NULL,
                    endpoint        TEXT NOT NULL,
                    config_profile  TEXT NOT NULL DEFAULT 'auto',
                    config_hash     TEXT,
                    resource_count  INTEGER NOT NULL DEFAULT 0,
                    error_count     INTEGER NOT NULL DEFAULT 0,
                    duration_ms     INTEGER NOT NULL DEFAULT 0,
                    input_type      TEXT NOT NULL DEFAULT '',
                    summary         TEXT,
                    score           TEXT,
                    trust_passport  TEXT
                )
            """)
            # Backfill: add columns on pre-existing databases.  SQLite ignores
            # the duplicate-column error when the column already exists.
            for ddl in (
                "ALTER TABLE processing_runs ADD COLUMN config_hash TEXT",
                "ALTER TABLE processing_runs ADD COLUMN trust_passport TEXT",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pr_created"
                " ON processing_runs(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pr_endpoint"
                " ON processing_runs(endpoint)"
            )

    def create(self, run: dict) -> None:
        """Insert a processing run record (idempotent on duplicate id)."""
        summary = run.get("summary")
        score = run.get("score")
        trust_passport = run.get("trust_passport")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processing_runs
                    (id, created_at, endpoint, config_profile, config_hash,
                     resource_count, error_count, duration_ms,
                     input_type, summary, score, trust_passport)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                    json.dumps(summary) if summary is not None else None,
                    json.dumps(score) if score is not None else None,
                    json.dumps(trust_passport) if trust_passport is not None else None,
                ),
            )

    def get(self, run_id: str) -> dict | None:
        """Fetch a single run by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM processing_runs WHERE id=?", (run_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

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
            where_clauses.append("endpoint=?")
            params.append(endpoint)
        if config_profile:
            where_clauses.append("config_profile=?")
            params.append(config_profile)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM processing_runs{where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM processing_runs{where_sql}"
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return [_row_to_dict(r) for r in rows], total

    def get_stats(self, window_days: int = 90) -> dict:
        """Aggregate statistics across recent runs.

        ``window_days`` limits the breakdown queries to the last N days so
        they stay index-friendly as history grows.  All-time totals
        (total_runs, total_resources) are still computed across the full table.
        """
        from datetime import timedelta

        window_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).isoformat()

        with self._connect() as conn:
            agg = conn.execute(
                """
                SELECT
                    COUNT(*)                                                               AS total_runs,
                    COUNT(CASE WHEN score IS NOT NULL THEN 1 END)                         AS scored_runs,
                    AVG(CAST(json_extract(score, '$.avg_composite') AS REAL))              AS avg_composite,
                    COALESCE(SUM(resource_count), 0)                                       AS total_resources
                FROM processing_runs
                WHERE created_at >= ?
                """,
                (window_cutoff,),
            ).fetchone()

            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_runs,
                    COALESCE(SUM(resource_count), 0) AS total_resources
                FROM processing_runs
                """
            ).fetchone()

            by_endpoint = {
                r["endpoint"]: r["cnt"]
                for r in conn.execute(
                    "SELECT endpoint, COUNT(*) AS cnt FROM processing_runs"
                    " WHERE created_at >= ? GROUP BY endpoint ORDER BY cnt DESC LIMIT 20",
                    (window_cutoff,),
                ).fetchall()
            }
            by_profile = {
                r["config_profile"]: r["cnt"]
                for r in conn.execute(
                    "SELECT config_profile, COUNT(*) AS cnt FROM processing_runs"
                    " WHERE created_at >= ? GROUP BY config_profile ORDER BY cnt DESC LIMIT 20",
                    (window_cutoff,),
                ).fetchall()
            }

        avg = agg["avg_composite"]
        return {
            "total_runs": totals["total_runs"],
            "scored_runs": agg["scored_runs"] or 0,
            "avg_composite": round(float(avg), 1) if avg is not None else None,
            "total_resources": totals["total_resources"] or 0,
            "runs_by_endpoint": by_endpoint,
            "runs_by_profile": by_profile,
        }

    def delete_before(self, before_iso: str) -> int:
        """Purge runs created before the given ISO timestamp. Returns count deleted."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM processing_runs WHERE created_at < ?", (before_iso,)
            )
            return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict:
    summary = row["summary"]
    score = row["score"]
    keys = row.keys()
    trust_passport = row["trust_passport"] if "trust_passport" in keys else None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "endpoint": row["endpoint"],
        "config_profile": row["config_profile"],
        "config_hash": row["config_hash"] if "config_hash" in keys else None,
        "resource_count": row["resource_count"],
        "error_count": row["error_count"],
        "duration_ms": row["duration_ms"],
        "input_type": row["input_type"],
        "summary": json.loads(summary) if summary else None,
        "score": json.loads(score) if score else None,
        "trust_passport": json.loads(trust_passport) if trust_passport else None,
    }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def init_processing_run_store(db_path: Any = None, store: Any = None) -> Any:
    """Initialise and return the module-level processing run store singleton.

    If *store* is provided (e.g. a PostgresProcessingRunStore), it is used directly.
    Otherwise a SqliteProcessingRunStore is created at *db_path*.

    Backwards-compat: if the first positional arg is not a string/path it is
    treated as the *store* (older callers used ``init_processing_run_store(store)``).
    """
    global _processing_run_store
    if db_path is not None and not isinstance(db_path, str):
        # Caller passed a store object positionally — accept it.
        # We deliberately don't accept ``os.PathLike`` here because mocks
        # auto-implement ``__fspath__`` and would falsely match.
        store = db_path
        db_path = None
    _processing_run_store = (
        store if store is not None else SqliteProcessingRunStore(db_path)
    )
    _log.info("processing_run_store initialised")
    return _processing_run_store


def get_processing_run_store() -> Any:
    return _processing_run_store
