"""Async job store for long-running pipeline operations.

Jobs are persisted to SQLite at MEDANON_JOB_DB (default /output/jobs.db).
The store uses WAL mode and is safe to access from multiple threads.

Public surface:
    ``init_job_store(db_path)``  initialise the module-level singleton.
    ``SqliteJobStore``           SQLite backend (default).
    ``JobStore``                 backward-compat alias for ``SqliteJobStore``.
    ``Job``                      dataclass representing a single job (from domain.jobs).
    ``JobStatus``                Enum: pending | running | done | error (from domain.jobs).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import AbstractContextManager
import uuid
from datetime import datetime, timezone
from pathlib import Path

from domain.jobs import Job, JobStatus  # noqa: F401  re-exported for callers
from utils.sqlite_store import connect as sqlite_connect

_jobs_log = logging.getLogger("medanon.jobs")
_DEFAULT_DB = "/output/jobs.db"


class SqliteJobStore:
    """SQLite-backed job store. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get("MEDANON_JOB_DB", _DEFAULT_DB)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> "AbstractContextManager[sqlite3.Connection]":
        """WAL connection, committed and **closed** on exit. See utils.sqlite_store."""
        return sqlite_connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id              TEXT PRIMARY KEY,
                    type            TEXT NOT NULL,
                    params          TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    result_path     TEXT,
                    error           TEXT,
                    checkpoint_data TEXT
                )
            """)
            # Migrate existing tables that predate the checkpoint_data column.
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN checkpoint_data TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
            # Indexes for next_pending() and list_jobs() to avoid full table scans
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_type_created ON jobs(type, created_at)"
            )

    def create(self, job_type: str, params: dict) -> Job:
        """Persist a new PENDING job and return it."""
        job = Job(id=str(uuid.uuid4()), type=job_type, params=params)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
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
        _jobs_log.info("job_created id=%s type=%s", job.id, job.type)
        return job

    def get(self, job_id: str) -> Job | None:
        """Fetch a job by ID; returns None if not found."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def update(self, job: Job) -> None:
        """Persist status, result_path, error, and checkpoint_data changes for *job*."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, result_path=?, error=?, checkpoint_data=? WHERE id=?",
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

    def update_checkpoint(self, job_id: str, data: dict) -> None:
        """Persist only the checkpoint_data column for an in-progress job."""
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET checkpoint_data=?, updated_at=? WHERE id=?",
                (json.dumps(data), updated_at, job_id),
            )

    def next_pending(self) -> Job | None:
        """Atomically claim the oldest PENDING job, or return None.

        Uses UPDATE-in-CTE to avoid TOCTOU race between SELECT and UPDATE
        when multiple workers or threads poll concurrently.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE jobs
                   SET status = ?, updated_at = ?
                 WHERE id = (
                    SELECT id FROM jobs
                     WHERE status = ?
                     ORDER BY created_at
                     LIMIT 1
                 )
                RETURNING *
                """,
                (JobStatus.RUNNING.value, now, JobStatus.PENDING.value),
            ).fetchone()
        return _row_to_job(row) if row else None

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
        *offset* is still accepted for backward compatibility but should be 0
        when keyset pagination is active.
        """
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if status:
            query += " AND status=?"
            params.append(status)
        if job_type:
            query += " AND type=?"
            params.append(job_type)
        if before_created_at:
            query += " AND created_at < ?"
            params.append(before_created_at)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def notify_new_job(self, job_id: str) -> None:
        """No-op for SQLite  the worker polls."""

    def cancel(self, job_id: str) -> bool:
        """Mark a pending or running job as cancelled.

        Returns True if the status was updated, False if the job was already
        in a terminal state (done, error, cancelled) or not found.
        """
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='cancelled', updated_at=? "
                "WHERE id=? AND status IN ('pending', 'running')",
                (updated_at, job_id),
            )
            return cur.rowcount > 0


# Backward-compat alias
JobStore = SqliteJobStore


def _row_to_job(row: sqlite3.Row) -> Job:
    raw_cp = row["checkpoint_data"] if "checkpoint_data" in row.keys() else None
    return Job(
        id=row["id"],
        type=row["type"],
        params=json.loads(row["params"]),
        status=JobStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result_path=row["result_path"],
        error=row["error"],
        checkpoint_data=json.loads(raw_cp) if raw_cp else None,
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
