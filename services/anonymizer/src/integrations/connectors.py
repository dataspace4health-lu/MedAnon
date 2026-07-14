"""Dataspace-connector store singletons.

Module-level holders for the active :class:`PostgresSourceStore` (saved input
sources) and :class:`PostgresDestinationStore` (saved S3 output destinations),
initialised during FastAPI startup when the app database (PostgreSQL) is
available. Both are PostgreSQL-only  there is no SQLite fallback for saved
connection credentials, mirroring the SQL-source store.
"""

from __future__ import annotations

from typing import Any

_source_store: Any = None
_destination_store: Any = None


def init_source_store(store) -> None:
    """Set the module-level input-source store (called at startup)."""
    global _source_store
    _source_store = store


def get_source_store():
    """Return the active input-source store, or None when no app DB is configured."""
    return _source_store


def init_destination_store(store) -> None:
    """Set the module-level output-destination store (called at startup)."""
    global _destination_store
    _destination_store = store


def get_destination_store():
    """Return the active output-destination store, or None when no app DB."""
    return _destination_store
