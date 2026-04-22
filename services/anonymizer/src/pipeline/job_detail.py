"""Job detail cache — stores per-job parsed result (resource counts, field analysis).

Allows the frontend to load previously-parsed job results without re-downloading
and re-parsing the NDJSON output.

Backends (in priority order):
  1. PostgreSQL — when MEDANON_APP_DB_URL is set
  2. SQLite     — fallback (default path /output/job_details.db)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("medanon.job_detail")

_DEFAULT_DB = "/output/job_details.db"

_job_detail_store: Any = None


class SqliteJobDetailStore:
    """SQLite-backed job detail cache. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get("MEDANON_JOB_DETAIL_DB", _DEFAULT_DB)
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
                CREATE TABLE IF NOT EXISTS job_details (
                    job_id     TEXT PRIMARY KEY,
                    detail     TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

    def get(self, job_id: str) -> dict | None:
        """Return cached detail for *job_id*, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail FROM job_details WHERE job_id=?", (job_id,)
            ).fetchone()
        return json.loads(row["detail"]) if row else None

    def set(self, job_id: str, detail: dict) -> None:
        """Upsert cached detail for *job_id*."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO job_details (job_id, detail, created_at)"
                " VALUES (?,?,?)",
                (job_id, json.dumps(detail), now),
            )

    def delete(self, job_id: str) -> None:
        """Remove cached detail for *job_id* (no-op if absent)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM job_details WHERE job_id=?", (job_id,))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def init_job_detail_store(db_path: str | None = None, store: Any = None) -> Any:
    """Initialise and return the module-level job detail store singleton.

    If *store* is provided (e.g. a PostgresJobDetailStore), it is used directly.
    Otherwise a SqliteJobDetailStore is created at *db_path*.
    """
    global _job_detail_store
    _job_detail_store = store if store is not None else SqliteJobDetailStore(db_path)
    _log.info("job_detail_store initialised")
    return _job_detail_store


def get_job_detail_store() -> Any:
    return _job_detail_store
