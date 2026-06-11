"""PostgreSQL-backed store for saved SQL-source connections.

Persists connection metadata in ``medanon.sql_connections``.  The password is
stored **encrypted** (the caller passes an already-encrypted token via
``integrations.sql_source.secrets.encrypt_secret``); this store never sees or
returns plaintext.  Public read methods (:meth:`list_all`, :meth:`get`) omit the
encrypted password entirely; only :meth:`get_encrypted_password` exposes it, and
only for the connect path.

PostgreSQL-only (no SQLite fallback) — saved credentials belong in the durable
app database, mirroring ``PostgresApiKeyStore``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.sql_connection_store.postgres")


class PostgresSqlConnectionStore:
    """PostgreSQL-backed saved SQL-source connection store."""

    # Self-healing DDL — keeps the store usable when the feature is added to an
    # existing deployment whose app-db volume predates this table (sql/init.sql
    # only runs on a fresh volume). Kept in sync with sql/init.sql.
    _SCHEMA_DDL = """
        CREATE SCHEMA IF NOT EXISTS medanon;
        CREATE TABLE IF NOT EXISTS medanon.sql_connections (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            host         TEXT NOT NULL,
            port         INTEGER NOT NULL DEFAULT 5432,
            dbname       TEXT NOT NULL,
            username     TEXT NOT NULL,
            password_enc TEXT NOT NULL,
            sslmode      TEXT NOT NULL DEFAULT 'prefer',
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sql_connections_created
            ON medanon.sql_connections (created_at DESC);
    """

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Idempotently create the table/index if missing (CREATE … IF NOT EXISTS).

        ``CREATE TABLE IF NOT EXISTS`` is NOT atomic against the implicit
        composite-type creation: when several app workers construct this store
        concurrently at startup, two sessions can both pass the catalog check
        and then collide on ``pg_type``/``pg_class`` (errors
        ``DuplicateTable``/``DuplicateObject``/``UniqueViolation``). The object
        exists either way, so those races are treated as success — only genuine
        errors propagate. See the equivalent guard in ``workflow_store`` and
        ``staging/store``.
        """
        from psycopg2 import errors as _pg_errors

        _BENIGN = (
            _pg_errors.DuplicateTable,
            _pg_errors.DuplicateObject,
            _pg_errors.UniqueViolation,
        )
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(self._SCHEMA_DDL)
            conn.commit()
        except _BENIGN:
            # Lost a concurrent CREATE race — the table/index now exists.
            conn.rollback()
            logger.debug("sql_connections schema already created concurrently")
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def create(
        self,
        *,
        name: str,
        host: str,
        port: int,
        dbname: str,
        username: str,
        password_enc: str,
        sslmode: str = "prefer",
    ) -> dict:
        """Insert a connection (password already encrypted). Returns public dict."""
        conn_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO medanon.sql_connections
                        (id, name, host, port, dbname, username, password_enc,
                         sslmode, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        conn_id,
                        name,
                        host,
                        int(port),
                        dbname,
                        username,
                        password_enc,
                        sslmode,
                        now,
                    ),
                )
            conn.commit()
        finally:
            self._put_conn(conn)
        return {
            "id": conn_id,
            "name": name,
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "username": username,
            "sslmode": sslmode,
            "created_at": now,
        }

    def list_all(self) -> list[dict]:
        """Return all connections (public — no password)."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, host, port, dbname, username, sslmode, created_at
                    FROM medanon.sql_connections ORDER BY created_at DESC
                    """
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def get(self, conn_id: str) -> dict | None:
        """Return one connection's public fields (no password), or None."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, host, port, dbname, username, sslmode, created_at
                    FROM medanon.sql_connections WHERE id = %s
                    """,
                    (conn_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def get_encrypted_password(self, conn_id: str) -> str | None:
        """Return the stored encrypted password token for the connect path."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT password_enc FROM medanon.sql_connections WHERE id = %s",
                    (conn_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            self._put_conn(conn)

    def delete(self, conn_id: str) -> bool:
        """Delete a connection. Returns True if a row was removed."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM medanon.sql_connections WHERE id = %s", (conn_id,)
                )
                removed = cur.rowcount > 0
            conn.commit()
            return removed
        finally:
            self._put_conn(conn)
