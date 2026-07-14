"""PostgreSQL-backed Transformation Passport store (D7.2 §5.5.1, EHDS Art 79).

Durable, queryable home for the anonymous documentation bundle a risk-driven
export produces  the privacy model + achieved k/l/t, tools, privacy-risk
assessment, and disclosure decision. Persisting it here (independent of the
job's own retention) gives the auditability trail Art 79 wants: what was done
to a released dataset remains answerable after the job is purged.

Safe to store: the passport is anonymous **by construction**  it carries only
counts, model parameters, metrics, and the disclosure verdict, never PHI or
record-level values (see ``pipeline.transformation_passport``). We defensively
reject a payload that fails a light PII sniff before writing, so a future
passport change can never silently persist identifiers.

Same self-creating-schema pattern as ``permit_store`` / ``trust_profile_store``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.passport_store.postgres")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.transformation_passports (
    job_id      TEXT PRIMARY KEY,
    permit_id   TEXT,
    decision    TEXT,
    passport    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS passports_permit_idx  ON medanon.transformation_passports (permit_id);
CREATE INDEX IF NOT EXISTS passports_created_idx ON medanon.transformation_passports (created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PassportPiiError(ValueError):
    """Raised when a passport payload trips the pre-write PII guard."""


# Keys whose presence in a passport would signal record-level / identifying
# leakage  the passport must never carry these. Cheap structural guard.
_FORBIDDEN_KEYS = frozenset(
    {
        "identifier",
        "name",
        "birthdate",
        "address",
        "telecom",
        "ssn",
        "mrn",
        "nhs_number",
    }
)


def assert_pii_safe(passport: dict) -> None:
    """Raise :class:`PassportPiiError` if *passport* contains a forbidden key.

    A structural sniff (not a content scan): the passport is anonymous by
    construction, so any of these keys appearing anywhere in the nested dict
    means the builder regressed  refuse to persist rather than store PII.
    """

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).strip().lower() in _FORBIDDEN_KEYS:
                    raise PassportPiiError(
                        f"passport contains forbidden key {k!r}  refusing to persist"
                    )
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(passport)


class PostgresPassportStore:
    """PostgreSQL-backed transformation-passport store."""

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
                logger.info("passport schema ready")
            except _benign:
                conn.rollback()
                logger.debug("passport schema already created concurrently")
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, job_id: str, passport: dict) -> None:
        """Persist *passport* for *job_id* (upsert). Fails closed on a PII sniff."""
        assert_pii_safe(passport)
        permit_id = (passport.get("identification") or {}).get("permit_id")
        decision = (passport.get("disclosure") or {}).get("decision")
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.transformation_passports
                            (job_id, permit_id, decision, passport, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (job_id) DO UPDATE SET
                            permit_id = EXCLUDED.permit_id,
                            decision = EXCLUDED.decision,
                            passport = EXCLUDED.passport
                        """,
                        (
                            job_id,
                            permit_id,
                            decision,
                            json.dumps(passport),
                            _now(),
                        ),
                    )
        finally:
            self._put_conn(conn)

    def get(self, job_id: str) -> dict | None:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT passport FROM medanon.transformation_passports WHERE job_id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
            return row["passport"] if row else None
        finally:
            self._put_conn(conn)

    def list(self, limit: int = 200) -> list[dict]:
        """Return recent passport index rows (job_id/permit_id/decision/created_at)."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT job_id, permit_id, decision, created_at
                        FROM medanon.transformation_passports
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (int(limit),),
                    )
                    rows = cur.fetchall()
            return [
                {
                    "job_id": r["job_id"],
                    "permit_id": r["permit_id"],
                    "decision": r["decision"],
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
