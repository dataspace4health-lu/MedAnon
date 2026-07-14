"""PostgreSQL-backed workflow (DAG) store.

The durable ledger for :class:`~domain.workflows.Workflow` and its steps,
consistent with "Postgres staging is the batch ledger". Workflows require
``MEDANON_APP_DB_URL``  there is intentionally no SQLite workflow store
(the API returns 503 when no app-db is configured).

Tables (also declared in ``sql/init.sql`` for fresh-volume bootstrap; this
store self-creates them via :meth:`ensure_schema` for existing deployments):

* ``medanon.workflows``        one row per workflow (status, backend, ref)
* ``medanon.workflow_steps``   one row per step (status, job_id, deps)

Step status transitions use a compare-and-set ``WHERE status = <expected>``
so concurrent worker callbacks cannot double-advance a step.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from domain.workflows import (
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
)

logger = logging.getLogger("medanon.workflow_store.postgres")


_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.workflows (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    params       JSONB NOT NULL DEFAULT '{}',
    backend      TEXT NOT NULL DEFAULT 'internal',
    external_ref TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS medanon.workflow_steps (
    workflow_id TEXT NOT NULL REFERENCES medanon.workflows(id) ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    job_type    TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}',
    depends_on  TEXT[] NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    job_id      TEXT,
    attempt     INT NOT NULL DEFAULT 0,
    error       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workflow_id, step_id)
);

CREATE INDEX IF NOT EXISTS idx_workflows_status ON medanon.workflows(status);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_job ON medanon.workflow_steps(job_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresWorkflowStore:
    """PostgreSQL-backed workflow + step ledger."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create schema/tables/indexes if missing.

        ``CREATE … IF NOT EXISTS`` is not atomic against implicit composite-type
        creation, so concurrent startup across app workers can collide on
        ``pg_type``/``pg_class``. The objects exist either way  treat those
        specific races as success (mirrors ``sql_connection_store``).
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
                logger.info("workflow schema ready")
            except _benign:
                conn.rollback()
                logger.debug("workflow schema already created concurrently")
        finally:
            self._put_conn(conn)

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create(self, workflow: Workflow) -> Workflow:
        """Persist a new workflow and its steps in one transaction."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.workflows
                            (id, name, status, params, backend, external_ref)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            workflow.id,
                            workflow.name,
                            workflow.status.value,
                            json.dumps(workflow.params),
                            workflow.backend,
                            workflow.external_ref,
                        ),
                    )
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO medanon.workflow_steps
                            (workflow_id, step_id, job_type, params, depends_on,
                             status, job_id, attempt, error)
                        VALUES %s
                        """,
                        [
                            (
                                workflow.id,
                                s.id,
                                s.job_type,
                                json.dumps(s.params),
                                s.depends_on,
                                s.status.value,
                                s.job_id,
                                s.attempt,
                                s.error,
                            )
                            for s in workflow.steps
                        ],
                    )
            logger.info(
                "workflow_created id=%s steps=%d backend=%s",
                workflow.id,
                len(workflow.steps),
                workflow.backend,
            )
            return workflow
        finally:
            self._put_conn(conn)

    def set_workflow_status(self, workflow_id: str, status: WorkflowStatus) -> None:
        self._exec(
            "UPDATE medanon.workflows SET status=%s, updated_at=NOW() WHERE id=%s",
            (status.value, workflow_id),
        )

    def compare_and_set_workflow_status(
        self,
        workflow_id: str,
        expected: WorkflowStatus,
        new: WorkflowStatus,
    ) -> bool:
        """Atomically move a workflow expected→new. Returns True iff it changed.

        Without this guard two concurrent worker callbacks (a final-step
        completion settling the workflow DONE, racing a cancel() setting it
        CANCELLED) would both issue unconditional UPDATEs and the last writer
        would win  so a cancelled workflow could flip back to DONE. The CAS
        precondition makes terminal transitions safe under multi-worker
        (Redis/Postgres) job stores.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE medanon.workflows SET status=%s, updated_at=NOW() "
                        "WHERE id=%s AND status=%s",
                        (new.value, workflow_id, expected.value),
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def set_step_job(self, workflow_id: str, step_id: str, job_id: str) -> None:
        self._exec(
            "UPDATE medanon.workflow_steps SET job_id=%s, updated_at=NOW() "
            "WHERE workflow_id=%s AND step_id=%s",
            (job_id, workflow_id, step_id),
        )

    def compare_and_set_step_status(
        self,
        workflow_id: str,
        step_id: str,
        expected: StepStatus,
        new: StepStatus,
        *,
        error: str | None = None,
    ) -> bool:
        """Atomically move a step expected→new. Returns True iff it changed.

        The CAS guard prevents two concurrent worker callbacks (or a callback
        racing the reconciliation sweep) from advancing the same step twice.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.workflow_steps
                           SET status=%s, error=%s, updated_at=NOW()
                         WHERE workflow_id=%s AND step_id=%s AND status=%s
                        """,
                        (new.value, error, workflow_id, step_id, expected.value),
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def _exec(self, sql: str, params: tuple) -> None:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, workflow_id: str) -> Workflow | None:
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM medanon.workflows WHERE id=%s", (workflow_id,)
                )
                wrow = cur.fetchone()
                if not wrow:
                    return None
                cur.execute(
                    "SELECT * FROM medanon.workflow_steps WHERE workflow_id=%s "
                    "ORDER BY step_id",
                    (workflow_id,),
                )
                srows = cur.fetchall()
            return _row_to_workflow(wrow, srows)
        finally:
            self._put_conn(conn)

    def get_by_job(self, job_id: str) -> tuple[str, str] | None:
        """Return ``(workflow_id, step_id)`` for the step owning *job_id*."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT workflow_id, step_id FROM medanon.workflow_steps "
                    "WHERE job_id=%s",
                    (job_id,),
                )
                row = cur.fetchone()
            return (row[0], row[1]) if row else None
        finally:
            self._put_conn(conn)

    def list_workflows(
        self, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Workflow]:
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if status:
                    cur.execute(
                        "SELECT * FROM medanon.workflows WHERE status=%s "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (status, limit, offset),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM medanon.workflows "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                wrows = cur.fetchall()
                if not wrows:
                    return []
                ids = [w["id"] for w in wrows]
                cur.execute(
                    "SELECT * FROM medanon.workflow_steps "
                    "WHERE workflow_id = ANY(%s) ORDER BY step_id",
                    (ids,),
                )
                steps_by_wf: dict[str, list] = {}
                for srow in cur.fetchall():
                    steps_by_wf.setdefault(srow["workflow_id"], []).append(srow)
            return [_row_to_workflow(w, steps_by_wf.get(w["id"], [])) for w in wrows]
        finally:
            self._put_conn(conn)

    def list_non_terminal(self) -> list[Workflow]:
        """Workflows still pending/running  for the reconciliation sweep."""
        out: list[Workflow] = []
        for st in ("pending", "running"):
            out.extend(self.list_workflows(status=st, limit=500))
        return out


def _row_to_workflow(wrow: dict, srows: list) -> Workflow:
    steps = [
        WorkflowStep(
            id=s["step_id"],
            job_type=s["job_type"],
            params=s["params"]
            if isinstance(s["params"], dict)
            else json.loads(s["params"] or "{}"),
            depends_on=list(s["depends_on"] or []),
            status=StepStatus(s["status"]),
            job_id=s["job_id"],
            attempt=s["attempt"],
            error=s["error"],
        )
        for s in srows
    ]
    params = wrow["params"]
    if not isinstance(params, dict):
        params = json.loads(params or "{}")
    return Workflow(
        id=wrow["id"],
        name=wrow["name"],
        steps=steps,
        status=WorkflowStatus(wrow["status"]),
        params=params,
        backend=wrow.get("backend", "internal"),
        external_ref=wrow.get("external_ref"),
        created_at=str(wrow["created_at"]),
        updated_at=str(wrow["updated_at"]),
    )
