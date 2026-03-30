"""Async job store for long-running pipeline operations.

Jobs are persisted to SQLite at MEDANON_JOB_DB (default /output/jobs.db).
The store uses WAL mode and is safe to access from multiple threads.

Public surface:
    ``init_job_store(db_path)`` — initialise the module-level singleton.
    ``SqliteJobStore``          — SQLite backend (default).
    ``JobStore``                — backward-compat alias for ``SqliteJobStore``.
    ``Job``                     — dataclass representing a single job (from medanon_core).
    ``JobStatus``               — Enum: pending | running | done | error (from medanon_core).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from medanon_core.domain import Job, JobStatus  # noqa: F401 — re-exported for callers

_jobs_log = logging.getLogger("medanon.jobs")
_DEFAULT_DB = "/output/jobs.db"


class SqliteJobStore:
    """SQLite-backed job store. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get("MEDANON_JOB_DB", _DEFAULT_DB)
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
                CREATE TABLE IF NOT EXISTS jobs (
                    id          TEXT PRIMARY KEY,
                    type        TEXT NOT NULL,
                    params      TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    result_path TEXT,
                    error       TEXT
                )
            """)

    def create(self, job_type: str, params: dict) -> Job:
        """Persist a new PENDING job and return it."""
        job = Job(id=str(uuid.uuid4()), type=job_type, params=params)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    job.type,
                    json.dumps(job.params),
                    job.status.value,
                    job.created_at,
                    job.updated_at,
                    job.result_path,
                    job.error,
                ),
            )
        _jobs_log.info("job_created id=%s type=%s", job.id, job.type)
        return job

    def get(self, job_id: str) -> Job | None:
        """Fetch a job by ID; returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def update(self, job: Job) -> None:
        """Persist status, result_path and error changes for *job*."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, result_path=?, error=? WHERE id=?",
                (job.status.value, job.updated_at, job.result_path, job.error, job.id),
            )

    def next_pending(self) -> Job | None:
        """Return the oldest PENDING job, or None if the queue is empty."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at LIMIT 1",
                (JobStatus.PENDING.value,),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """List jobs with optional filtering, ordered by created_at descending."""
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if status:
            query += " AND status=?"
            params.append(status)
        if job_type:
            query += " AND type=?"
            params.append(job_type)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def notify_new_job(self, job_id: str) -> None:
        """No-op for SQLite — the worker polls."""


# Backward-compat alias
JobStore = SqliteJobStore


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        params=json.loads(row["params"]),
        status=JobStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result_path=row["result_path"],
        error=row["error"],
    )


# ---------------------------------------------------------------------------
# Module-level singleton + injection helper
# ---------------------------------------------------------------------------

_job_store = None


def init_job_store(db_path: str | None = None, store=None):
    """Initialise the module-level job store singleton.

    If *store* is provided (e.g. a RedisJobStore), it is used directly.
    Otherwise a SqliteJobStore is created at *db_path*.
    """
    global _job_store
    _job_store = store if store is not None else SqliteJobStore(db_path)
    return _job_store
