"""Instance-settings service — deployment-wide admin-managed application defaults.

Thin business layer over :class:`~integrations.postgres.settings_store.PostgresSettingsStore`.
Holds no secrets (FHIR credentials live encrypted in the connector store).

Read is always available: with no app DB configured the built-in
:data:`~integrations.postgres.settings_store.DEFAULT_SETTINGS` are the effective
configuration, so the app still has something to run on. Writes require the
durable store and raise :class:`SettingsStoreUnavailable` otherwise.
"""

from __future__ import annotations

import logging

from integrations.postgres.settings_store import DEFAULT_SETTINGS

logger = logging.getLogger("medanon")


class SettingsStoreUnavailable(Exception):
    """Raised when a write is attempted but no durable settings store exists."""


class SettingsService:
    """Read/update the single deployment-wide settings row."""

    def get(self) -> dict:
        """Return the effective settings (persisted, or built-in defaults)."""
        from pipeline.app_settings import get_settings_store

        store = get_settings_store()
        if store is None:
            return {**DEFAULT_SETTINGS, "updated_at": None, "updated_by": None}
        return store.get()

    def update(self, fields: dict, *, updated_by: str | None = None) -> dict:
        """Merge ``fields`` into the settings row. Requires the durable store."""
        from pipeline.app_settings import get_settings_store

        store = get_settings_store()
        if store is None:
            raise SettingsStoreUnavailable(
                "Settings store is not initialised — set MEDANON_APP_DB_URL "
                "(PostgreSQL) to persist instance settings."
            )
        return store.update(fields, updated_by=updated_by)
