"""PostgreSQL-backed per-client API key store.

Each API key is represented by a row in ``medanon.api_keys``.  Plain raw keys
are never stored  only a SHA-256 hex digest.  The raw key is returned once at
creation time and is never retrievable again.

Key lifecycle:
  - create()  → generates ID + raw key; stores hash; returns raw key once.
  - lookup_by_hash()  → find active, non-expired row by SHA-256(raw_key).
  - revoke()  → set revoked=TRUE (zero-downtime, no restart needed).
  - rotate()  → atomic INSERT new + revoke old in one transaction.
  - list_all() → metadata only, no raw keys.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.api_key_store")

_VALID_ROLES = frozenset({"admin", "analyst", "viewer"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "role": row["role"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked": bool(row["revoked"]),
        "last_used_at": row["last_used_at"],
        "description": row["description"],
    }


class PostgresApiKeyStore:
    """PostgreSQL-backed per-client API key store."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    # ------------------------------------------------------------------
    # Key generation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_raw_key() -> str:
        """Return a new secret API key string (returned once, never stored)."""
        return "mk_" + secrets.token_urlsafe(32)

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        """Return SHA-256 hex digest of *raw_key*."""
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def _generate_id() -> str:
        return "kid_" + secrets.token_hex(8)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        client_id: str,
        role: str = "analyst",
        expires_at: str | None = None,
        description: str = "",
    ) -> tuple[str, str, dict]:
        """Create a new API key.

        Returns ``(key_id, raw_key, metadata_dict)``.  *raw_key* is returned
        exactly once and not stored.
        """
        if role not in _VALID_ROLES:
            raise ValueError(f"Invalid role {role!r}. Allowed: {sorted(_VALID_ROLES)}")

        key_id = self._generate_id()
        raw_key = self._generate_raw_key()
        key_hash = self._hash_key(raw_key)
        now = _now_iso()

        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO medanon.api_keys
                            (id, key_hash, client_id, role, created_at, expires_at,
                             revoked, description)
                        VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
                        """,
                        (
                            key_id,
                            key_hash,
                            client_id,
                            role,
                            now,
                            expires_at,
                            description,
                        ),
                    )
        except psycopg2.IntegrityError:
            self._put_conn(conn)
            raise ValueError(f"API key ID collision  retry (id={key_id!r})")
        finally:
            self._put_conn(conn)

        meta = {
            "id": key_id,
            "client_id": client_id,
            "role": role,
            "created_at": now,
            "expires_at": expires_at,
            "revoked": False,
            "last_used_at": None,
            "description": description,
        }
        return key_id, raw_key, meta

    def lookup_by_hash(self, key_hash: str) -> dict | None:
        """Return the active, non-expired key row for *key_hash*, or None."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT * FROM medanon.api_keys
                        WHERE key_hash = %s
                          AND revoked = FALSE
                          AND (expires_at IS NULL OR expires_at > %s)
                        LIMIT 1
                        """,
                        (key_hash, _now_iso()),
                    )
                    row = cur.fetchone()
            return _row_to_dict(row) if row else None
        finally:
            self._put_conn(conn)

    def get(self, key_id: str) -> dict | None:
        """Return key metadata by ID, or None if not found."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.api_keys WHERE id = %s",
                        (key_id,),
                    )
                    row = cur.fetchone()
            return _row_to_dict(row) if row else None
        finally:
            self._put_conn(conn)

    def list_all(self) -> list[dict]:
        """Return all key metadata (no raw keys, no hashes)."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.api_keys ORDER BY created_at DESC"
                    )
                    rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            self._put_conn(conn)

    def revoke(self, key_id: str) -> bool:
        """Set *key_id* as revoked.  Returns True if the row existed."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE medanon.api_keys SET revoked = TRUE WHERE id = %s",
                        (key_id,),
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def rotate(self, key_id: str) -> tuple[str, str, dict]:
        """Atomically create a replacement key and revoke *key_id*.

        Returns ``(new_key_id, new_raw_key, new_metadata_dict)``.
        Raises ``KeyError`` if *key_id* does not exist.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Fetch original row
                    cur.execute(
                        "SELECT * FROM medanon.api_keys WHERE id = %s",
                        (key_id,),
                    )
                    original = cur.fetchone()
                    if original is None:
                        raise KeyError(f"API key {key_id!r} not found")

                    # Create replacement
                    new_id = self._generate_id()
                    new_raw = self._generate_raw_key()
                    new_hash = self._hash_key(new_raw)
                    now = _now_iso()

                    cur.execute(
                        """
                        INSERT INTO medanon.api_keys
                            (id, key_hash, client_id, role, created_at, expires_at,
                             revoked, description)
                        VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
                        """,
                        (
                            new_id,
                            new_hash,
                            original["client_id"],
                            original["role"],
                            now,
                            original["expires_at"],
                            original["description"],
                        ),
                    )
                    # Revoke old key
                    cur.execute(
                        "UPDATE medanon.api_keys SET revoked = TRUE WHERE id = %s",
                        (key_id,),
                    )
        finally:
            self._put_conn(conn)

        meta = {
            "id": new_id,
            "client_id": original["client_id"],
            "role": original["role"],
            "created_at": now,
            "expires_at": original["expires_at"],
            "revoked": False,
            "last_used_at": None,
            "description": original["description"],
        }
        return new_id, new_raw, meta

    def touch_last_used(self, key_id: str) -> None:
        """Update ``last_used_at`` to now (best-effort, errors logged but not raised)."""
        try:
            conn = self._get_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE medanon.api_keys SET last_used_at = %s WHERE id = %s",
                            (_now_iso(), key_id),
                        )
            finally:
                self._put_conn(conn)
        except Exception as exc:
            logger.debug("touch_last_used key_id=%s error=%s", key_id, exc)
