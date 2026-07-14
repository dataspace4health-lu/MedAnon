"""PostgreSQL-backed release ledger for cumulative-exposure analysis (D7.2 §5.5.7).

Records one row per released dataset so a *new* release can be assessed against
prior releases to the same recipient for repeat exposure and differencing risk
(see ``pipeline.cumulative_exposure``). Stored fields are anonymous: a keyed
one-way population fingerprint (never the subject ids), the QI signature, counts,
and the permit / recipient it went to.

Same self-creating-schema pattern as ``passport_store`` / ``permit_store``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.release_ledger.postgres")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.release_ledger (
    release_id    TEXT PRIMARY KEY,
    permit_id     TEXT,
    recipient     TEXT,
    qi_signature  TEXT,
    record_count  INTEGER NOT NULL DEFAULT 0,
    fingerprint   JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS release_permit_idx    ON medanon.release_ledger (permit_id);
CREATE INDEX IF NOT EXISTS release_recipient_idx ON medanon.release_ledger (recipient);
CREATE INDEX IF NOT EXISTS release_created_idx   ON medanon.release_ledger (created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresReleaseLedger:
    """Durable ledger of releases keyed by permit / recipient."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def ensure_schema(self) -> None:
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
                logger.info("release ledger schema ready")
            except _benign:
                conn.rollback()
                logger.debug("release ledger schema already created concurrently")
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        release_id: str,
        *,
        fingerprint: list[str],
        permit_id: str | None = None,
        recipient: str | None = None,
        qi_signature: str = "",
        record_count: int = 0,
    ) -> None:
        """Append (or upsert) a release row. Fingerprint must already be keyed-hashed."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.release_ledger
                            (release_id, permit_id, recipient, qi_signature,
                             record_count, fingerprint, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (release_id) DO UPDATE SET
                            permit_id = EXCLUDED.permit_id,
                            recipient = EXCLUDED.recipient,
                            qi_signature = EXCLUDED.qi_signature,
                            record_count = EXCLUDED.record_count,
                            fingerprint = EXCLUDED.fingerprint
                        """,
                        (
                            release_id,
                            permit_id,
                            recipient,
                            qi_signature,
                            int(record_count),
                            json.dumps(fingerprint),
                            _now(),
                        ),
                    )
        finally:
            self._put_conn(conn)

    def prior_releases(
        self,
        *,
        permit_id: str | None = None,
        recipient: str | None = None,
        exclude_release_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Load prior release rows scoped to a permit and/or recipient.

        At least one of ``permit_id`` / ``recipient`` must be given  cumulative
        exposure is only meaningful within a governance scope, never globally.
        """
        if not permit_id and not recipient:
            raise ValueError(
                "permit_id or recipient is required to scope prior releases"
            )

        clauses: list[str] = []
        params: list[object] = []
        if permit_id:
            clauses.append("permit_id = %s")
            params.append(permit_id)
        if recipient:
            clauses.append("recipient = %s")
            params.append(recipient)
        if exclude_release_id:
            clauses.append("release_id <> %s")
            params.append(exclude_release_id)
        where = " AND ".join(clauses)
        params.append(int(limit))

        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT release_id, qi_signature, record_count, fingerprint, created_at
                        FROM medanon.release_ledger
                        WHERE {where}
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        tuple(params),
                    )
                    rows = cur.fetchall()
            return [
                {
                    "release_id": r["release_id"],
                    "qi_signature": r["qi_signature"],
                    "record_count": r["record_count"],
                    "fingerprint": r["fingerprint"],
                    "created_at": (
                        r["created_at"].isoformat()
                        if hasattr(r["created_at"], "isoformat")
                        else r["created_at"]
                    ),
                }
                for r in rows
            ]
        finally:
            self._put_conn(conn)
