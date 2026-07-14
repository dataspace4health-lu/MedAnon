"""PostgreSQL-backed data-permit store (TEHDAS2 D7.2 §2 governance, EHDS Art 79).

Durable home for data permits so the governance decisions made during a run
(permit approval, disclosure binding) survive a restart — Art 79 auditability
requires the permit that authorised a release to remain queryable afterwards.

Same public surface as :class:`pipeline.governance.store.InMemoryPermitStore`
(``save``/``get``/``list``/``delete``/``bulk_load``) so the service layer is
backend-agnostic. Table is self-created via :meth:`ensure_schema` (mirrors
``trust_profile_store``/``workflow_store``). Serialisation round-trips through
the domain model's own :meth:`Permit.to_dict` / :meth:`Permit.from_dict`, so
the store never re-implements field/enum/date handling.
"""

from __future__ import annotations

import json
import logging

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from domain.permit import Permit

logger = logging.getLogger("medanon.permit_store.postgres")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.permits (
    id              TEXT PRIMARY KEY,
    purpose         TEXT NOT NULL DEFAULT '',
    legal_basis     TEXT NOT NULL DEFAULT '',
    controller      TEXT NOT NULL DEFAULT '',
    recipient       TEXT NOT NULL DEFAULT '',
    allowed_paths   JSONB NOT NULL DEFAULT '[]',
    restrictions    JSONB NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',
    decided_by      TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    valid_from      TIMESTAMPTZ,
    valid_until     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Auditability query paths (Art 79): find active permits / by recipient / by status.
CREATE INDEX IF NOT EXISTS permits_status_idx    ON medanon.permits (status);
CREATE INDEX IF NOT EXISTS permits_recipient_idx ON medanon.permits (recipient);
"""


class PostgresPermitStore:
    """PostgreSQL-backed permit store."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def ensure_schema(self) -> None:
        """Create schema/table/indexes if missing.

        ``CREATE … IF NOT EXISTS`` can race across app workers on implicit
        catalog objects; treat those specific duplicate races as success
        (mirrors ``trust_profile_store``/``workflow_store``).
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
                logger.info("permit schema ready")
            except _benign:
                conn.rollback()
                logger.debug("permit schema already created concurrently")
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Public API (same interface as InMemoryPermitStore)
    # ------------------------------------------------------------------

    def save(self, permit: Permit) -> Permit:
        """Upsert *permit* (overwrite on id — matches in-memory save semantics)."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.permits
                            (id, purpose, legal_basis, controller, recipient,
                             allowed_paths, restrictions, status, decided_by,
                             decision_reason, valid_from, valid_until, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            purpose = EXCLUDED.purpose,
                            legal_basis = EXCLUDED.legal_basis,
                            controller = EXCLUDED.controller,
                            recipient = EXCLUDED.recipient,
                            allowed_paths = EXCLUDED.allowed_paths,
                            restrictions = EXCLUDED.restrictions,
                            status = EXCLUDED.status,
                            decided_by = EXCLUDED.decided_by,
                            decision_reason = EXCLUDED.decision_reason,
                            valid_from = EXCLUDED.valid_from,
                            valid_until = EXCLUDED.valid_until
                        """,
                        (
                            permit.id,
                            permit.purpose,
                            permit.legal_basis,
                            permit.controller,
                            permit.recipient,
                            json.dumps(list(permit.allowed_paths)),
                            json.dumps(list(permit.restrictions)),
                            permit.status.value,
                            permit.decided_by,
                            permit.decision_reason,
                            permit.valid_from,
                            permit.valid_until,
                            permit.created_at,
                        ),
                    )
        finally:
            self._put_conn(conn)
        return permit

    def get(self, permit_id: str) -> Permit | None:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.permits WHERE id = %s", (permit_id,)
                    )
                    row = cur.fetchone()
            return _row_to_permit(row) if row else None
        finally:
            self._put_conn(conn)

    def list(self) -> list[Permit]:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM medanon.permits ORDER BY created_at ASC")
                    rows = cur.fetchall()
            return [_row_to_permit(r) for r in rows]
        finally:
            self._put_conn(conn)

    def delete(self, permit_id: str) -> bool:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM medanon.permits WHERE id = %s", (permit_id,)
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def bulk_load(self, permits) -> None:
        for p in permits:
            self.save(p)


def _row_to_permit(row: dict) -> Permit:
    """Reconstruct a :class:`Permit` from a ``RealDictCursor`` row.

    Rebuilds the dict shape :meth:`Permit.from_dict` expects: JSONB columns
    come back as Python lists, TIMESTAMPTZ columns as timezone-aware
    ``datetime`` objects (``from_dict`` accepts both those and ISO strings),
    and ``status`` as its enum value string.
    """
    return Permit.from_dict(
        {
            "id": row["id"],
            "purpose": row.get("purpose", ""),
            "legal_basis": row.get("legal_basis", ""),
            "controller": row.get("controller", ""),
            "recipient": row.get("recipient", ""),
            "allowed_paths": row.get("allowed_paths") or [],
            "restrictions": row.get("restrictions") or [],
            "status": row.get("status", "draft"),
            "decided_by": row.get("decided_by", ""),
            "decision_reason": row.get("decision_reason", ""),
            "valid_from": row.get("valid_from"),
            "valid_until": row.get("valid_until"),
            "created_at": row.get("created_at"),
        }
    )
