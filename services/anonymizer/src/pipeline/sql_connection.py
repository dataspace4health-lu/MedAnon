"""SQL-source connection store singleton.

Module-level holder for the active :class:`PostgresSqlConnectionStore`,
initialised during FastAPI startup when the app database (PostgreSQL) is
available.  The store is PostgreSQL-only  there is no SQLite fallback for saved
connection credentials.
"""

from __future__ import annotations

from typing import Any

_sql_connection_store: Any = None


def init_sql_connection_store(store) -> None:
    """Set the module-level connection store (called at startup)."""
    global _sql_connection_store
    _sql_connection_store = store


def get_sql_connection_store():
    """Return the active store, or None when the app DB is not configured."""
    return _sql_connection_store
