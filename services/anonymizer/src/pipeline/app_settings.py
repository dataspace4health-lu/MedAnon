"""Instance-settings store singleton.

Module-level holder for the active :class:`PostgresSettingsStore` (deployment-wide
admin-managed application defaults), initialised during FastAPI startup when the
app database (PostgreSQL) is available. PostgreSQL-only — there is no SQLite
fallback; when unset, the settings service serves the built-in defaults and
rejects writes. Mirrors :mod:`pipeline.connectors`.
"""

from __future__ import annotations

from typing import Any

_settings_store: Any = None


def init_settings_store(store) -> None:
    """Set the module-level instance-settings store (called at startup)."""
    global _settings_store
    _settings_store = store


def get_settings_store():
    """Return the active settings store, or None when no app DB is configured."""
    return _settings_store
