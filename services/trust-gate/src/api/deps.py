"""Shared route dependencies: the persistence-store accessors.

Both raise 503 when no store is configured, so read endpoints fail clearly instead
of returning empty results as if the store were simply empty.
"""

from __future__ import annotations

from fastapi import HTTPException

from store import get_findings_store, get_passport_store

_NO_STORE = (
    "no {} store configured (set TRUST_GATE_STORE_DB_URL or TRUST_GATE_STORE_DB)"
)


def require_store():
    store = get_passport_store()
    if store is None:
        raise HTTPException(status_code=503, detail=_NO_STORE.format("passport"))
    return store


def require_findings():
    store = get_findings_store()
    if store is None:
        raise HTTPException(status_code=503, detail=_NO_STORE.format("findings"))
    return store
