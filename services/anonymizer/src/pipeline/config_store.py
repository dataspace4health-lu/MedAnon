"""SQLite-backed index for de-identification configuration profiles.

Tracks metadata (name, description, created_at, is_system) for all profiles —
both the six bundled system configs and any user-defined configs saved at runtime.

The actual rule content lives in YAML files on disk. This store provides fast
listing and metadata queries without reading every YAML file on each request.

Public surface:
    ``init_config_store(db_path)``  — initialise the module-level singleton.
    ``ConfigStore``                 — SQLite backend.
    ``_config_store``               — module-level singleton (set by init_config_store).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("medanon.config_store")

_DEFAULT_DB = "/output/config_store.db"

# The six bundled profiles shipped with the application.
_SYSTEM_CONFIGS: list[dict] = [
    {"name": "minimal",    "description": "SHA3-256 hash + regex scrubbing. No external services required."},
    {"name": "gpas",       "description": "Production pseudonymization via gPAS with dual-pass NLP scrubbing."},
    {"name": "gdpr",       "description": "GDPR Art. 4(5) HMAC pseudonymization profile."},
    {"name": "hipaa",      "description": "HIPAA Safe Harbor (45 CFR §164.514(b)): 18 PHI categories."},
    {"name": "research",   "description": "IRB-grade: dates→year-month, IDs cryptohashed for longitudinal linkage."},
    {"name": "structural",    "description": "Structure-preserving: IDs via gPAS, PII→[REDACTED], dates→year."},
    {"name": "value-masking", "description": "Field-complete masking: no fields removed, all PII values replaced in place. IDs via gPAS, binary payloads cleared."},
]


class ConfigStore:
    """SQLite-backed config metadata index. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get("MEDANON_CONFIG_STORE_DB", _DEFAULT_DB)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._seed_system_configs()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS configs (
                    name        TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    created_at  TEXT NOT NULL,
                    is_system   INTEGER NOT NULL DEFAULT 0
                )
            """)

    def _seed_system_configs(self) -> None:
        """Insert system configs if not already present (idempotent)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for cfg in _SYSTEM_CONFIGS:
                conn.execute(
                    "INSERT OR IGNORE INTO configs (name, description, created_at, is_system) VALUES (?,?,?,1)",
                    (cfg["name"], cfg["description"], now),
                )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "name":        row["name"],
            "description": row["description"],
            "created_at":  row["created_at"],
            "is_system":   bool(row["is_system"]),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_all(self) -> list[dict]:
        """Return metadata for all configs — system first, then user-defined by created_at."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM configs ORDER BY is_system DESC, created_at ASC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, name: str) -> dict | None:
        """Return metadata for *name*, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM configs WHERE name=?", (name,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def create(self, name: str, description: str = "") -> dict:
        """Insert a new user-defined config entry. Raises ValueError if name already exists."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO configs (name, description, created_at, is_system) VALUES (?,?,?,0)",
                    (name, description, now),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"Config '{name}' already exists.")
        _log.info("config_created name=%s", name)
        return {"name": name, "description": description, "created_at": now, "is_system": False}

    def update_description(self, name: str, description: str) -> None:
        """Update the description of an existing config."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE configs SET description=? WHERE name=?",
                (description, name),
            )

    def delete(self, name: str) -> None:
        """Delete a user-defined config entry. Raises ValueError if system config."""
        meta = self.get(name)
        if meta is None:
            raise KeyError(f"Config '{name}' not found.")
        if meta["is_system"]:
            raise PermissionError(f"System config '{name}' cannot be deleted.")
        with self._connect() as conn:
            conn.execute("DELETE FROM configs WHERE name=?", (name,))
        _log.info("config_deleted name=%s", name)

    def exists(self, name: str) -> bool:
        """Return True if *name* exists in the index."""
        return self.get(name) is not None

    def is_system(self, name: str) -> bool:
        """Return True if *name* is a system (read-only) config."""
        meta = self.get(name)
        return meta["is_system"] if meta else False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_config_store: ConfigStore | None = None


def init_config_store(db_path: str | None = None) -> ConfigStore:
    """Initialise and return the module-level ConfigStore singleton."""
    global _config_store
    _config_store = ConfigStore(db_path)
    return _config_store
