"""PostgreSQL-backed trust-profile store.

Durable home for Trust Gate audit profiles (selectable phases + threshold/target
overrides). Same public surface as ``pipeline.trust_profile.SqliteTrustProfileStore``
so the API layer is backend-agnostic. Table is self-created via
:meth:`ensure_schema` (mirrors ``workflow_store``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from pipeline.trust_profile import _SYSTEM_PROFILES

logger = logging.getLogger("medanon.trust_profile_store.postgres")

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

CREATE TABLE IF NOT EXISTS medanon.trust_profiles (
    name         TEXT PRIMARY KEY,
    description  TEXT NOT NULL DEFAULT '',
    phases       JSONB NOT NULL DEFAULT '[]',
    thresholds   JSONB NOT NULL DEFAULT '{}',
    targets      JSONB NOT NULL DEFAULT '[]',
    intended_use TEXT NOT NULL DEFAULT '',
    use_case     TEXT NOT NULL DEFAULT '',
    is_system    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backfill columns for deployments created before these existed.
ALTER TABLE medanon.trust_profiles
    ADD COLUMN IF NOT EXISTS intended_use TEXT NOT NULL DEFAULT '';
ALTER TABLE medanon.trust_profiles
    ADD COLUMN IF NOT EXISTS use_case TEXT NOT NULL DEFAULT '';
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresTrustProfileStore:
    """PostgreSQL-backed trust-profile store."""

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def ensure_schema(self) -> None:
        """Create schema/table if missing, then seed system profiles.

        ``CREATE … IF NOT EXISTS`` can race across app workers on implicit
        catalog objects; treat those specific duplicate races as success
        (mirrors ``workflow_store``).
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
                logger.info("trust_profile schema ready")
            except _benign:
                conn.rollback()
                logger.debug("trust_profile schema already created concurrently")
        finally:
            self._put_conn(conn)
        self._seed_system_profiles()

    def _seed_system_profiles(self) -> None:
        now = _now()
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    for p in _SYSTEM_PROFILES:
                        cur.execute(
                            """
                            INSERT INTO medanon.trust_profiles
                                (name, description, phases, intended_use, use_case,
                                 is_system, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
                            ON CONFLICT (name) DO NOTHING
                            """,
                            (
                                p["name"],
                                p["description"],
                                json.dumps(p["phases"]),
                                p.get("intended_use", ""),
                                p.get("use_case", ""),
                                now,
                                now,
                            ),
                        )
        finally:
            self._put_conn(conn)

    def list_all(self) -> list[dict]:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.trust_profiles "
                        "ORDER BY is_system DESC, created_at ASC"
                    )
                    rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            self._put_conn(conn)

    def get(self, name: str) -> dict | None:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM medanon.trust_profiles WHERE name = %s", (name,)
                    )
                    row = cur.fetchone()
            return _row_to_dict(row) if row else None
        finally:
            self._put_conn(conn)

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    def create(
        self,
        name: str,
        description: str,
        phases: list[str],
        thresholds: dict | None = None,
        targets: list | None = None,
        intended_use: str = "",
        use_case: str = "",
    ) -> dict:
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute(
                            """
                            INSERT INTO medanon.trust_profiles
                                (name, description, phases, thresholds, targets,
                                 intended_use, use_case, is_system)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                            """,
                            (
                                name,
                                description,
                                json.dumps(phases),
                                json.dumps(thresholds or {}),
                                json.dumps(targets or []),
                                intended_use or "",
                                use_case or "",
                            ),
                        )
                    except psycopg2.IntegrityError as exc:
                        conn.rollback()
                        raise ValueError(
                            f"Trust profile '{name}' already exists."
                        ) from exc
        finally:
            self._put_conn(conn)
        logger.info("trust_profile_created name=%s phases=%d", name, len(phases))
        return self.get(name)  # type: ignore[return-value]

    def update(
        self,
        name: str,
        *,
        description: str | None = None,
        phases: list[str] | None = None,
        thresholds: dict | None = None,
        targets: list | None = None,
        intended_use: str | None = None,
        use_case: str | None = None,
    ) -> dict:
        meta = self.get(name)
        if meta is None:
            raise KeyError(f"Trust profile '{name}' not found.")
        if meta["is_system"]:
            raise PermissionError(f"System trust profile '{name}' is read-only.")
        new_desc = meta["description"] if description is None else description
        new_phases = meta["phases"] if phases is None else phases
        new_thr = meta["thresholds"] if thresholds is None else thresholds
        new_tgt = meta["targets"] if targets is None else targets
        new_use = meta["intended_use"] if intended_use is None else intended_use
        new_uc = meta["use_case"] if use_case is None else use_case
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.trust_profiles
                           SET description=%s, phases=%s, thresholds=%s, targets=%s,
                               intended_use=%s, use_case=%s, updated_at=NOW()
                         WHERE name=%s
                        """,
                        (
                            new_desc,
                            json.dumps(new_phases),
                            json.dumps(new_thr),
                            json.dumps(new_tgt),
                            new_use,
                            new_uc,
                            name,
                        ),
                    )
        finally:
            self._put_conn(conn)
        logger.info("trust_profile_updated name=%s", name)
        return self.get(name)  # type: ignore[return-value]

    def delete(self, name: str) -> None:
        meta = self.get(name)
        if meta is None:
            raise KeyError(f"Trust profile '{name}' not found.")
        if meta["is_system"]:
            raise PermissionError(
                f"System trust profile '{name}' cannot be deleted."
            )
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM medanon.trust_profiles WHERE name = %s", (name,)
                    )
        finally:
            self._put_conn(conn)
        logger.info("trust_profile_deleted name=%s", name)


def _row_to_dict(row: dict) -> dict:
    def _coerce(val, default):
        if isinstance(val, (list, dict)):
            return val
        if val is None:
            return default
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            return default

    return {
        "name": row["name"],
        "description": row.get("description", ""),
        "phases": _coerce(row.get("phases"), []),
        "thresholds": _coerce(row.get("thresholds"), {}),
        "targets": _coerce(row.get("targets"), []),
        "intended_use": row.get("intended_use", "") or "",
        "use_case": row.get("use_case", "") or "",
        "is_system": bool(row.get("is_system")),
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }
