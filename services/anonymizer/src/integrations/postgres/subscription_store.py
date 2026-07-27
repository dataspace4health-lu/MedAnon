"""PostgreSQL-backed FHIR R4 Subscription store.

Drop-in replacement for ``SqliteSubscriptionStore`` using the shared
PostgreSQL connection pool.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.subscriptions.postgres")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.subscriptions (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    criteria     TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    headers      TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    resource     TEXT NOT NULL
);
"""


class PostgresSubscriptionStore:
    """PostgreSQL-backed FHIR R4 Subscription store."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def ensure_schema(self) -> None:
        """Create schema/table if missing.

        ``CREATE … IF NOT EXISTS`` is not atomic against implicit composite-type
        creation, so concurrent startup across app workers can collide on
        ``pg_type``/``pg_class``. The objects exist either way  treat those
        specific races as success (mirrors ``workflow_store``).
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
                logger.info("subscription schema ready")
            except _benign:
                conn.rollback()
                logger.debug("subscription schema already created concurrently")
        finally:
            self._put_conn(conn)

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    # ------------------------------------------------------------------
    # Public API (same interface as SqliteSubscriptionStore)
    # ------------------------------------------------------------------

    def create(self, sub: dict) -> dict:
        """Persist a new subscription. Generates id if not present."""
        sub_copy = dict(sub)
        if not sub_copy.get("id"):
            sub_copy["id"] = str(uuid.uuid4())
        sub_copy["resourceType"] = "Subscription"
        sub_copy.setdefault("status", "active")
        now = datetime.now(timezone.utc).isoformat()
        sub_copy.setdefault("meta", {})["lastUpdated"] = now

        channel = sub_copy.get("channel", {})
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.subscriptions
                            (id, status, criteria, channel_type, endpoint,
                             headers, created_at, updated_at, resource)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            sub_copy["id"],
                            sub_copy.get("status", "active"),
                            sub_copy.get("criteria", ""),
                            channel.get("type", ""),
                            channel.get("endpoint", ""),
                            json.dumps(channel.get("header", [])),
                            now,
                            now,
                            json.dumps(sub_copy),
                        ),
                    )
        finally:
            self._put_conn(conn)
        logger.info(
            "subscription_created id=%s criteria=%s",
            sub_copy["id"],
            sub_copy.get("criteria"),
        )
        return sub_copy

    def get(self, sub_id: str) -> dict | None:
        """Return a subscription by id, or None if not found."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT resource FROM medanon.subscriptions WHERE id = %s",
                        (sub_id,),
                    )
                    row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            self._put_conn(conn)

    def update(self, sub_id: str, sub: dict) -> dict | None:
        """Replace a subscription by id. Returns None if not found."""
        existing = self.get(sub_id)
        if existing is None:
            return None
        sub_copy = dict(sub)
        sub_copy["id"] = sub_id
        now = datetime.now(timezone.utc).isoformat()
        sub_copy.setdefault("meta", {})["lastUpdated"] = now
        channel = sub_copy.get("channel", {})
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.subscriptions
                           SET status = %s, criteria = %s, channel_type = %s,
                               endpoint = %s, headers = %s, updated_at = %s,
                               resource = %s
                         WHERE id = %s
                        """,
                        (
                            sub_copy.get("status", "active"),
                            sub_copy.get("criteria", ""),
                            channel.get("type", ""),
                            channel.get("endpoint", ""),
                            json.dumps(channel.get("header", [])),
                            now,
                            json.dumps(sub_copy),
                            sub_id,
                        ),
                    )
        finally:
            self._put_conn(conn)
        return sub_copy

    def delete(self, sub_id: str) -> bool:
        """Delete a subscription by id. Returns True if a row was removed."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM medanon.subscriptions WHERE id = %s",
                        (sub_id,),
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def list_active(self) -> list[dict]:
        """Return all subscriptions with status='active'."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT resource FROM medanon.subscriptions WHERE status = 'active'"
                    )
                    rows = cur.fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            self._put_conn(conn)

    def list_all(self) -> list[dict]:
        """Return all subscriptions ordered by creation time descending."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT resource FROM medanon.subscriptions ORDER BY created_at DESC"
                    )
                    rows = cur.fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            self._put_conn(conn)
