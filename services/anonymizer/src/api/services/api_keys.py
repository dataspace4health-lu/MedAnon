"""Business logic for per-client API key management."""

from __future__ import annotations

import logging

logger = logging.getLogger("medanon.api_key_service")


class ApiKeyStoreUnavailable(Exception):
    pass


class ApiKeyNotFound(Exception):
    pass


class ApiKeyAlreadyRevoked(Exception):
    pass


def _get_store():
    """Fetch the API key store singleton (set by api/main.py _startup())."""
    import api.auth as _auth  # lazy import — avoids circular dependency at module load

    if _auth._api_key_store is None:
        raise ApiKeyStoreUnavailable(
            "API key store not initialised — MEDANON_APP_DB_URL required"
        )
    return _auth._api_key_store


class ApiKeyService:
    def create(
        self,
        client_id: str,
        role: str,
        expires_at: str | None,
        description: str,
    ) -> tuple[str, str, dict]:
        """Create a new key.  Returns (key_id, raw_key, metadata)."""
        store = _get_store()
        return store.create(
            client_id=client_id,
            role=role,
            expires_at=expires_at,
            description=description,
        )

    def list_all(self) -> list[dict]:
        return _get_store().list_all()

    def get(self, key_id: str) -> dict:
        store = _get_store()
        row = store.get(key_id)
        if row is None:
            raise ApiKeyNotFound(key_id)
        return row

    def revoke(self, key_id: str) -> None:
        store = _get_store()
        row = store.get(key_id)
        if row is None:
            raise ApiKeyNotFound(key_id)
        if row["revoked"]:
            raise ApiKeyAlreadyRevoked(key_id)
        store.revoke(key_id)

    def rotate(self, key_id: str) -> tuple[str, str, dict]:
        """Atomically rotate *key_id*.  Returns (new_id, new_raw_key, metadata)."""
        store = _get_store()
        try:
            return store.rotate(key_id)
        except KeyError:
            raise ApiKeyNotFound(key_id)
