"""PostgreSQL-backed stores for dataspace connectors.

Two saved-connection types power the dataspace I/O boundary:

* :class:`PostgresSourceStore`  saved **input sources** (currently FHIR servers).
  The bearer token is stored **encrypted** (the caller passes an already-encrypted
  token); this store never sees or returns plaintext.
* :class:`PostgresDestinationStore`  saved **S3 output destinations** (the
  de-identified file always lands here). The S3 secret key is stored encrypted.

Both mirror :class:`~integrations.postgres.sql_connection_store.PostgresSqlConnectionStore`:
self-healing DDL (usable when the feature is added to an existing app-db volume),
public read methods that omit the secret entirely, and a single secret-returning
method used only on the run/connect path.

PostgreSQL-only (no SQLite fallback)  saved credentials belong in the durable app
database, mirroring the SQL-source store and ``PostgresApiKeyStore``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2 import errors as _pg_errors
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.connector_store.postgres")

# Lost-race errors from concurrent ``CREATE … IF NOT EXISTS`` at startup  the
# object exists either way, so these are treated as success. See the equivalent
# guard in ``sql_connection_store`` / ``workflow_store`` / ``staging/store``.
_BENIGN_DDL = (
    _pg_errors.DuplicateTable,
    _pg_errors.DuplicateObject,
    _pg_errors.UniqueViolation,
)


class _BaseConnectorStore:
    """Shared connection-pool plumbing + idempotent schema bootstrap."""

    _SCHEMA_DDL = ""  # overridden by subclasses

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(self._SCHEMA_DDL)
            conn.commit()
        except _BENIGN_DDL:
            conn.rollback()
            logger.debug("connector schema already created concurrently")
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


class PostgresSourceStore(_BaseConnectorStore):
    """Saved FHIR-server connections, usable as a de-identification source and/or
    target. Token stored encrypted.

    A ``role`` of ``source`` | ``target`` | ``both`` scopes where a saved server may
    be selected. The ``ALTER … ADD COLUMN IF NOT EXISTS`` keeps the store usable on
    an app-db volume that predates the column (existing rows default to ``source``).
    """

    _SCHEMA_DDL = """
        CREATE SCHEMA IF NOT EXISTS medanon;
        CREATE TABLE IF NOT EXISTS medanon.source_connections (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'fhir',
            role        TEXT NOT NULL DEFAULT 'source',
            server_url  TEXT NOT NULL,
            token_enc   TEXT,
            created_at  TEXT NOT NULL
        );
        ALTER TABLE medanon.source_connections
            ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'source';
        CREATE INDEX IF NOT EXISTS idx_source_connections_created
            ON medanon.source_connections (created_at DESC);
    """

    def create(
        self,
        *,
        name: str,
        server_url: str,
        token_enc: str | None,
        kind: str = "fhir",
        role: str = "source",
    ) -> dict:
        """Insert a saved server (token already encrypted). Returns public dict."""
        source_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO medanon.source_connections
                        (id, name, kind, role, server_url, token_enc, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (source_id, name, kind, role, server_url, token_enc, now),
                )
            conn.commit()
        finally:
            self._put_conn(conn)
        return {
            "id": source_id,
            "name": name,
            "kind": kind,
            "role": role,
            "server_url": server_url,
            "has_token": bool(token_enc),
            "created_at": now,
        }

    def list_all(self, role: str | None = None) -> list[dict]:
        """Return saved servers (public  no token).

        When *role* is given, returns servers matching that role plus any ``both``.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if role:
                    cur.execute(
                        """
                        SELECT id, name, kind, role, server_url,
                               (token_enc IS NOT NULL) AS has_token, created_at
                        FROM medanon.source_connections
                        WHERE role = %s OR role = 'both'
                        ORDER BY created_at DESC
                        """,
                        (role,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, name, kind, role, server_url,
                               (token_enc IS NOT NULL) AS has_token, created_at
                        FROM medanon.source_connections ORDER BY created_at DESC
                        """
                    )
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def get(self, source_id: str) -> dict | None:
        """Return one server's public fields (no token), or None."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, kind, role, server_url,
                           (token_enc IS NOT NULL) AS has_token, created_at
                    FROM medanon.source_connections WHERE id = %s
                    """,
                    (source_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def get_encrypted_token(self, source_id: str) -> str | None:
        """Return the stored encrypted token for the run path (may be None)."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT token_enc FROM medanon.source_connections WHERE id = %s",
                    (source_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            self._put_conn(conn)

    def delete(self, source_id: str) -> bool:
        """Delete a source. Returns True if a row was removed."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM medanon.source_connections WHERE id = %s",
                    (source_id,),
                )
                removed = cur.rowcount > 0
            conn.commit()
            return removed
        finally:
            self._put_conn(conn)


class PostgresDestinationStore(_BaseConnectorStore):
    """Saved S3 output destinations. Secret key stored encrypted."""

    _SCHEMA_DDL = """
        CREATE SCHEMA IF NOT EXISTS medanon;
        CREATE TABLE IF NOT EXISTS medanon.output_destinations (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            endpoint       TEXT NOT NULL,
            region         TEXT,
            bucket         TEXT NOT NULL,
            key_prefix     TEXT NOT NULL DEFAULT '',
            access_key     TEXT NOT NULL,
            secret_key_enc TEXT NOT NULL,
            secure         BOOLEAN NOT NULL DEFAULT TRUE,
            path_style     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_output_destinations_created
            ON medanon.output_destinations (created_at DESC);
    """

    def create(
        self,
        *,
        name: str,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key_enc: str,
        region: str | None = None,
        key_prefix: str = "",
        secure: bool = True,
        path_style: bool = False,
    ) -> dict:
        """Insert a destination (secret already encrypted). Returns public dict."""
        dest_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO medanon.output_destinations
                        (id, name, endpoint, region, bucket, key_prefix,
                         access_key, secret_key_enc, secure, path_style, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        dest_id,
                        name,
                        endpoint,
                        region,
                        bucket,
                        key_prefix,
                        access_key,
                        secret_key_enc,
                        bool(secure),
                        bool(path_style),
                        now,
                    ),
                )
            conn.commit()
        finally:
            self._put_conn(conn)
        return self._public(
            {
                "id": dest_id,
                "name": name,
                "endpoint": endpoint,
                "region": region,
                "bucket": bucket,
                "key_prefix": key_prefix,
                "access_key": access_key,
                "secure": bool(secure),
                "path_style": bool(path_style),
                "created_at": now,
            }
        )

    @staticmethod
    def _public(row: dict) -> dict:
        """Strip the encrypted secret; access_key is non-sensitive (like a username)."""
        return {k: v for k, v in row.items() if k != "secret_key_enc"}

    def list_all(self) -> list[dict]:
        """Return all destinations (public  no secret key)."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, endpoint, region, bucket, key_prefix,
                           access_key, secure, path_style, created_at
                    FROM medanon.output_destinations ORDER BY created_at DESC
                    """
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def get(self, dest_id: str) -> dict | None:
        """Return one destination's public fields (no secret key), or None."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name, endpoint, region, bucket, key_prefix,
                           access_key, secure, path_style, created_at
                    FROM medanon.output_destinations WHERE id = %s
                    """,
                    (dest_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._put_conn(conn)

    def get_encrypted_secret(self, dest_id: str) -> str | None:
        """Return the stored encrypted secret key for the run path."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT secret_key_enc FROM medanon.output_destinations "
                    "WHERE id = %s",
                    (dest_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            self._put_conn(conn)

    def delete(self, dest_id: str) -> bool:
        """Delete a destination. Returns True if a row was removed."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM medanon.output_destinations WHERE id = %s",
                    (dest_id,),
                )
                removed = cur.rowcount > 0
            conn.commit()
            return removed
        finally:
            self._put_conn(conn)
