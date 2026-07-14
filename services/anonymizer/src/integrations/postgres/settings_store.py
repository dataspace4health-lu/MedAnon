"""PostgreSQL-backed instance-settings store.

A single, deployment-wide row of admin-managed application defaults: which FHIR
source/target the app is wired to, the default de-identification rule profile,
and the assessment defaults the UI pre-fills. This is *instance* configuration
(one shared config per deployment), not per-user preferences, and holds no
secrets (FHIR credentials live encrypted in the connector store).

PostgreSQL-only, mirroring the connector stores: self-healing DDL (usable when
the feature lands on an existing app-db volume) and a single-row upsert. There
is no SQLite fallback; when no app DB is configured the API returns the built-in
:data:`DEFAULT_SETTINGS` and rejects writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2 import errors as _pg_errors
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.settings_store.postgres")

# There is exactly one settings row per deployment; this fixed key addresses it.
_SINGLETON_ID = "default"

# Canonical instance defaults. Kept in one place so the store, the service, and
# a no-app-db deployment all agree on the starting configuration.
DEFAULT_SETTINGS: dict = {
    "config_profile": "auto",
    "fhir_page_size": 1000,
    "dataset_id": "fhir-dataset",
    "source_system": "hapi-fhir (source)",
    "scan_max_resources": 20000,
    "active_source_id": "source",
    "active_target_id": "target",
}

# Lost-race errors from concurrent ``CREATE … IF NOT EXISTS`` at startup — the
# object exists either way, so these are treated as success (mirrors the
# connector / trust-profile / workflow stores).
_BENIGN_DDL = (
    _pg_errors.DuplicateTable,
    _pg_errors.DuplicateObject,
    _pg_errors.UniqueViolation,
)


class PostgresSettingsStore:
    """Single-row, admin-managed instance settings. No secrets."""

    _SCHEMA_DDL = """
        CREATE SCHEMA IF NOT EXISTS medanon;
        CREATE TABLE IF NOT EXISTS medanon.app_settings (
            id                 TEXT PRIMARY KEY,
            config_profile     TEXT    NOT NULL DEFAULT 'auto',
            fhir_page_size     INTEGER NOT NULL DEFAULT 1000,
            dataset_id         TEXT    NOT NULL DEFAULT 'fhir-dataset',
            source_system      TEXT    NOT NULL DEFAULT 'hapi-fhir (source)',
            scan_max_resources INTEGER NOT NULL DEFAULT 20000,
            active_source_id   TEXT    NOT NULL DEFAULT 'source',
            active_target_id   TEXT    NOT NULL DEFAULT 'target',
            updated_at         TEXT,
            updated_by         TEXT
        );
    """

    # Columns a client may write, in a stable order for the upsert statement.
    _WRITABLE = (
        "config_profile",
        "fhir_page_size",
        "dataset_id",
        "source_system",
        "scan_max_resources",
        "active_source_id",
        "active_target_id",
    )

    def __init__(self, pool: ThreadedConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    # ── pool plumbing (mirrors the connector stores) ─────────────────────────

    def _get_conn(self):
        from integrations.postgres.pool import get_conn

        return get_conn(self._pool)

    def _put_conn(self, conn) -> None:
        from integrations.postgres.pool import safe_putconn

        safe_putconn(self._pool, conn)

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(self._SCHEMA_DDL)
            conn.commit()
        except _BENIGN_DDL:
            conn.rollback()
            logger.debug("app_settings schema already created concurrently")
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    # ── public surface ───────────────────────────────────────────────────────

    def get(self) -> dict:
        """Return the effective settings, falling back to :data:`DEFAULT_SETTINGS`.

        A deployment that has never saved settings has no row yet; that is not an
        error — the built-in defaults are the effective configuration until an
        admin saves. ``updated_at``/``updated_by`` are ``None`` in that case.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT config_profile, fhir_page_size, dataset_id, "
                    "source_system, scan_max_resources, active_source_id, "
                    "active_target_id, updated_at, updated_by "
                    "FROM medanon.app_settings WHERE id = %s",
                    (_SINGLETON_ID,),
                )
                row = cur.fetchone()
        finally:
            self._put_conn(conn)
        if row is None:
            return {**DEFAULT_SETTINGS, "updated_at": None, "updated_by": None}
        return dict(row)

    def update(self, fields: dict, *, updated_by: str | None = None) -> dict:
        """Merge ``fields`` into the single settings row and return the result.

        Partial update: only the keys present in ``fields`` (and recognised as
        writable) change; everything else keeps its current value (or its
        default on first save). Returns the full effective settings.
        """
        current = self.get()
        merged = {k: current[k] for k in self._WRITABLE}
        for key, value in fields.items():
            if key in self._WRITABLE and value is not None:
                merged[key] = value
        now = datetime.now(timezone.utc).isoformat()

        cols = (*self._WRITABLE, "updated_at", "updated_by")
        placeholders = ", ".join(["%s"] * (len(self._WRITABLE) + 3))
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in (*self._WRITABLE, "updated_at", "updated_by")
        )
        values = (
            _SINGLETON_ID,
            *(merged[c] for c in self._WRITABLE),
            now,
            updated_by,
        )
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO medanon.app_settings (id, {', '.join(cols)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT (id) DO UPDATE SET {updates}",
                    values,
                )
            conn.commit()
        finally:
            self._put_conn(conn)
        return {**merged, "updated_at": now, "updated_by": updated_by}
