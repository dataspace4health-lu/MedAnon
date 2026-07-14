"""Permit store (TEHDAS2 D7.2 §2 governance).

An in-memory permit store for local dev and tests. A production deployment
backs permits with Postgres following the same pattern as
``integrations.postgres.trust_profile_store`` (LISTEN/NOTIFY-capable, keyed by
id); this interface is intentionally small so a DB-backed store can be dropped
in behind it.
"""

from __future__ import annotations

import threading
from typing import Iterable

from domain.permit import Permit


class InMemoryPermitStore:
    """Thread-safe dict-backed permit store."""

    def __init__(self) -> None:
        self._permits: dict[str, Permit] = {}
        self._lock = threading.Lock()

    def save(self, permit: Permit) -> Permit:
        with self._lock:
            self._permits[permit.id] = permit
        return permit

    def get(self, permit_id: str) -> Permit | None:
        with self._lock:
            return self._permits.get(permit_id)

    def list(self) -> list[Permit]:
        with self._lock:
            return list(self._permits.values())

    def delete(self, permit_id: str) -> bool:
        with self._lock:
            return self._permits.pop(permit_id, None) is not None

    def bulk_load(self, permits: Iterable[Permit]) -> None:
        with self._lock:
            for p in permits:
                self._permits[p.id] = p
