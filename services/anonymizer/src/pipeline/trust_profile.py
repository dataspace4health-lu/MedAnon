"""Trust-profile store singleton + SQLite backend.

A *trust profile* is a named, reusable selection of Trust Gate audit phases (and,
later, sector targets + threshold overrides) so an operator can tune exactly what
the pre-privacy quality barrier measures instead of one generic pass. The
anonymizer owns these profiles (the Trust Gate service stays stateless); the
selected profile's ``phases`` are passed to the service per request via
``integrations/trust_gate/client``.

Backends (mirrors ``pipeline/processing_run``):
  1. PostgreSQL  when MEDANON_APP_DB_URL is set (``integrations/postgres/trust_profile_store``)
  2. SQLite      fallback, always available (default /output/trust_profiles.db)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from utils.sqlite_store import connect as sqlite_connect

from domain.trust import (  # noqa: F401  re-exported for existing call sites
    PHASE_IDS,
    USE_CASE_IDS,
    _SYSTEM_PROFILES,
    validate_phases,
    validate_use_case,
)

_log = logging.getLogger("medanon.trust_profile")

_DEFAULT_DB = "/output/trust_profiles.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteTrustProfileStore:
    """SQLite-backed trust-profile store. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get("MEDANON_TRUST_PROFILE_DB", _DEFAULT_DB)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_columns()
        self._seed_system_profiles()

    def _connect(self) -> "AbstractContextManager[sqlite3.Connection]":
        """WAL connection, committed and **closed** on exit. See utils.sqlite_store."""
        return sqlite_connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_profiles (
                    name         TEXT PRIMARY KEY,
                    description  TEXT NOT NULL DEFAULT '',
                    phases       TEXT NOT NULL DEFAULT '[]',
                    thresholds   TEXT NOT NULL DEFAULT '{}',
                    targets      TEXT NOT NULL DEFAULT '[]',
                    intended_use TEXT NOT NULL DEFAULT '',
                    use_case     TEXT NOT NULL DEFAULT '',
                    is_system    INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
                """
            )

    def _migrate_columns(self) -> None:
        """Backfill additive columns on DBs created before they existed.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``; gate on ``PRAGMA table_info``.
        """
        with self._connect() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(trust_profiles)")
            }
            if "use_case" not in cols:
                conn.execute(
                    "ALTER TABLE trust_profiles ADD COLUMN use_case TEXT NOT NULL DEFAULT ''"
                )

    def _seed_system_profiles(self) -> None:
        now = _now()
        with self._connect() as conn:
            for p in _SYSTEM_PROFILES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trust_profiles
                        (name, description, phases, thresholds, targets, intended_use,
                         use_case, is_system, created_at, updated_at)
                    VALUES (?, ?, ?, '{}', '[]', ?, ?, 1, ?, ?)
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

    def list_all(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trust_profiles ORDER BY is_system DESC, created_at ASC"
            ).fetchall()
        return [_row_to_dict(dict(r)) for r in rows]

    def get(self, name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trust_profiles WHERE name = ?", (name,)
            ).fetchone()
        return _row_to_dict(dict(row)) if row else None

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
        now = _now()
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO trust_profiles
                        (name, description, phases, thresholds, targets, intended_use,
                         use_case, is_system, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        name,
                        description,
                        json.dumps(phases),
                        json.dumps(thresholds or {}),
                        json.dumps(targets or []),
                        intended_use or "",
                        use_case or "",
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Trust profile '{name}' already exists.") from exc
        _log.info("trust_profile_created name=%s phases=%d", name, len(phases))
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
        new = {
            "description": meta["description"] if description is None else description,
            "phases": meta["phases"] if phases is None else phases,
            "thresholds": meta["thresholds"] if thresholds is None else thresholds,
            "targets": meta["targets"] if targets is None else targets,
            "intended_use": meta["intended_use"]
            if intended_use is None
            else intended_use,
            "use_case": meta["use_case"] if use_case is None else use_case,
        }
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trust_profiles
                   SET description = ?, phases = ?, thresholds = ?, targets = ?,
                       intended_use = ?, use_case = ?, updated_at = ?
                 WHERE name = ?
                """,
                (
                    new["description"],
                    json.dumps(new["phases"]),
                    json.dumps(new["thresholds"]),
                    json.dumps(new["targets"]),
                    new["intended_use"],
                    new["use_case"],
                    _now(),
                    name,
                ),
            )
        _log.info("trust_profile_updated name=%s", name)
        return self.get(name)  # type: ignore[return-value]

    def delete(self, name: str) -> None:
        meta = self.get(name)
        if meta is None:
            raise KeyError(f"Trust profile '{name}' not found.")
        if meta["is_system"]:
            raise PermissionError(f"System trust profile '{name}' cannot be deleted.")
        with self._connect() as conn:
            conn.execute("DELETE FROM trust_profiles WHERE name = ?", (name,))
        _log.info("trust_profile_deleted name=%s", name)


def _row_to_dict(row: dict) -> dict:
    def _loads(val, default):
        if isinstance(val, (list, dict)):
            return val
        try:
            return json.loads(val) if val else default
        except (ValueError, TypeError):
            return default

    created = row.get("created_at")
    updated = row.get("updated_at")
    return {
        "name": row["name"],
        "description": row.get("description", ""),
        "phases": _loads(row.get("phases"), []),
        "thresholds": _loads(row.get("thresholds"), {}),
        "targets": _loads(row.get("targets"), []),
        "intended_use": row.get("intended_use", "") or "",
        "use_case": row.get("use_case", "") or "",
        "is_system": bool(row.get("is_system")),
        "created_at": created
        if isinstance(created, str)
        else (created.isoformat() if created else None),
        "updated_at": updated
        if isinstance(updated, str)
        else (updated.isoformat() if updated else None),
    }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_trust_profile_store: Any = None


def init_trust_profile_store(db_path: str | None = None, *, store: Any = None) -> Any:
    """Initialise the module-level singleton. Pass ``store=`` for the Postgres
    backend, or a path/None for the SQLite fallback."""
    global _trust_profile_store
    _trust_profile_store = (
        store if store is not None else SqliteTrustProfileStore(db_path)
    )
    return _trust_profile_store


def get_trust_profile_store() -> Any:
    """Return the active store, or None when uninitialised."""
    return _trust_profile_store


def resolve_phases(name: str | None) -> list[str] | None:
    """Resolve a trust-profile name to its phase list, or None when unresolvable
    (no store, unknown name, or name falsy) so the caller falls back to all phases."""
    if not name:
        return None
    store = get_trust_profile_store()
    if store is None:
        return None
    meta = store.get(name)
    if not meta:
        return None
    phases = meta.get("phases") or []
    return phases or None


def resolve_targets(name: str | None) -> list[dict] | None:
    """Resolve a trust-profile name to its sector targets, or None when
    unresolvable (no store, unknown name, falsy name, or no targets set)."""
    if not name:
        return None
    store = get_trust_profile_store()
    if store is None:
        return None
    meta = store.get(name)
    if not meta:
        return None
    targets = meta.get("targets") or []
    return targets or None


def resolve_intended_use(name: str | None) -> str | None:
    """Resolve a trust-profile name to its declared intended use, or None."""
    if not name:
        return None
    store = get_trust_profile_store()
    if store is None:
        return None
    meta = store.get(name)
    if not meta:
        return None
    return meta.get("intended_use") or None


def resolve_use_case(name: str | None) -> str | None:
    """Resolve a trust-profile name to its declared use case, or None.

    The use case is forwarded to the Trust Gate, which maps it to a metric subset
    (decision-tree selection). None when unresolvable or unset on the profile."""
    if not name:
        return None
    store = get_trust_profile_store()
    if store is None:
        return None
    meta = store.get(name)
    if not meta:
        return None
    return meta.get("use_case") or None
