"""PostgreSQL-backed config metadata index.

Drop-in replacement for ``ConfigStore`` (SQLite) using the shared PostgreSQL
connection pool.  Tracks metadata (name, description, created_at, is_system)
for all de-identification profiles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.config_store.postgres")

# The seven bundled profiles shipped with the application.
_SYSTEM_CONFIGS: list[dict] = [
    {
        "name": "minimal",
        "description": "SHA3-256 hash + regex scrubbing. No external services required.",
    },
    {
        "name": "gpas",
        "description": "Production pseudonymization via gPAS with dual-pass NLP scrubbing.",
    },
    {"name": "gdpr", "description": "GDPR Art. 4(5) HMAC pseudonymization profile."},
    {
        "name": "hipaa",
        "description": "HIPAA Safe Harbor (45 CFR §164.514(b)): 18 PHI categories.",
    },
    {
        "name": "research",
        "description": "IRB-grade: dates→year-month, IDs cryptohashed for longitudinal linkage.",
    },
    {
        "name": "structural",
        "description": "Structure-preserving: IDs via gPAS, PII→[REDACTED], dates→year.",
    },
    {
        "name": "value-masking",
        "description": (
            "Field-complete masking: no fields removed, all PII values replaced "
            "in place. IDs via gPAS, binary payloads cleared."
        ),
    },
]


class PostgresConfigStore:
    """PostgreSQL-backed config metadata index."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool
        self._seed_system_configs()

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def _seed_system_configs(self) -> None:
        """Insert system configs if not already present (idempotent)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    for cfg in _SYSTEM_CONFIGS:
                        cur.execute(
                            """
                            INSERT INTO medanon.configs (name, description, created_at, is_system)
                            VALUES (%s, %s, %s, TRUE)
                            ON CONFLICT (name) DO NOTHING
                            """,
                            (cfg["name"], cfg["description"], now),
                        )
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Public API (same interface as ConfigStore)
    # ------------------------------------------------------------------

    def list_all(self) -> list[dict]:
        """Return metadata for all configs  system first, then user-defined."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.configs ORDER BY is_system DESC, created_at ASC"
                    )
                    rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            self._put_conn(conn)

    def get(self, name: str) -> dict | None:
        """Return metadata for *name*, or None if not found."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.configs WHERE name = %s", (name,)
                    )
                    row = cur.fetchone()
            return _row_to_dict(row) if row else None
        finally:
            self._put_conn(conn)

    def create(self, name: str, description: str = "") -> dict:
        """Insert a new user-defined config entry. Raises ValueError if exists."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute(
                            """
                            INSERT INTO medanon.configs (name, description, created_at, is_system)
                            VALUES (%s, %s, %s, FALSE)
                            """,
                            (name, description, now),
                        )
                    except psycopg2.IntegrityError:
                        conn.rollback()
                        raise ValueError(f"Config '{name}' already exists.")
        finally:
            self._put_conn(conn)
        logger.info("config_created name=%s", name)
        return {
            "name": name,
            "description": description,
            "created_at": now,
            "is_system": False,
        }

    def update_description(self, name: str, description: str) -> None:
        """Update the description of an existing config."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE medanon.configs SET description = %s WHERE name = %s",
                        (description, name),
                    )
        finally:
            self._put_conn(conn)

    def delete(self, name: str) -> None:
        """Delete a user-defined config entry."""
        meta = self.get(name)
        if meta is None:
            raise KeyError(f"Config '{name}' not found.")
        if meta["is_system"]:
            raise PermissionError(f"System config '{name}' cannot be deleted.")
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM medanon.configs WHERE name = %s", (name,))
        finally:
            self._put_conn(conn)
        logger.info("config_deleted name=%s", name)

    def exists(self, name: str) -> bool:
        """Return True if *name* exists in the index."""
        return self.get(name) is not None

    def is_system(self, name: str) -> bool:
        """Return True if *name* is a system (read-only) config."""
        meta = self.get(name)
        return meta["is_system"] if meta else False


def _row_to_dict(row: dict) -> dict:
    created_at = row["created_at"]
    if not isinstance(created_at, str):
        created_at = created_at.isoformat() if created_at else None
    return {
        "name": row["name"],
        "description": row["description"],
        "created_at": created_at,
        "is_system": bool(row["is_system"]),
    }
