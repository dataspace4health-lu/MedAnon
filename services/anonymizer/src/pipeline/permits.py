"""Permit service layer (WS8b)  CRUD + lifecycle over a permit store.

Wraps :mod:`pipeline.governance` so the router stays thin: it validates input,
drives the domain state machine, persists via the store, and raises typed
errors the router maps to HTTP status codes. The store is pluggable  an
in-memory store for local dev/tests, a Postgres store in production (following
the ``integrations.postgres.trust_profile_store`` pattern; selected here so the
swap is a one-line change).
"""

from __future__ import annotations

import uuid
from typing import Any

from pipeline.governance import (
    InMemoryPermitStore,
    Permit,
    PermitStatus,
    PermitTransitionError,
)


class PermitNotFoundError(KeyError):
    """Raised when a permit id does not exist."""


# Module-level store singleton. Defaults to in-memory (correct for single-process
# dev and the test suite); a durable Postgres-backed store is swapped in at
# startup via :func:`init_permit_store` when ``MEDANON_APP_DB_URL`` is set, so
# permits survive a restart (EHDS Art 79 auditability). Mirrors the
# ``init_*_store``/``get_*_store`` idiom used by ``pipeline.trust_profile`` etc.
_store: Any = InMemoryPermitStore()


def init_permit_store(*, store: Any = None) -> Any:
    """Install the process-wide permit store (called once at startup).

    Passing ``store=None`` resets to a fresh in-memory store  used by tests
    that want isolation without threading a store through every call.
    """
    global _store
    _store = store if store is not None else InMemoryPermitStore()
    return _store


def get_permit_store() -> Any:
    """Return the active process-wide permit store."""
    return _store


def _store_backend() -> Any:
    return _store


class PermitService:
    """Application service for the data-permit lifecycle."""

    def __init__(self, store: Any = None) -> None:
        # Capture only an *explicitly injected* store (used by tests). Otherwise
        # resolve the process-wide store lazily on every access via the ``_store``
        # property  a service constructed at import time (the permits router's
        # module-level singleton) must pick up the durable store installed later
        # by ``init_permit_store()`` at startup. Previously the router froze to
        # the pre-startup in-memory default, so permits never reached Postgres
        # and a create on one worker 404'd on submit against another.
        self._explicit_store = store

    @property
    def _store(self) -> Any:
        return (
            self._explicit_store
            if self._explicit_store is not None
            else _store_backend()
        )

    # -- create / read -----------------------------------------------------
    def create(self, data: dict[str, Any]) -> Permit:
        """Create a permit in DRAFT from a partial dict (id auto-assigned)."""
        payload = dict(data)
        payload.setdefault("id", f"permit-{uuid.uuid4().hex[:12]}")
        payload["status"] = PermitStatus.DRAFT.value  # always start as draft
        permit = Permit.from_dict(payload)
        return self._store.save(permit)

    def get(self, permit_id: str) -> Permit:
        permit = self._store.get(permit_id)
        if permit is None:
            raise PermitNotFoundError(permit_id)
        return permit

    def list(self) -> list[Permit]:
        return self._store.list()

    # -- lifecycle ---------------------------------------------------------
    def _apply(self, permit_id: str, op, *args) -> Permit:
        permit = self.get(permit_id)
        op(permit, *args)  # raises PermitTransitionError on illegal move
        return self._store.save(permit)

    def submit(self, permit_id: str) -> Permit:
        return self._apply(permit_id, Permit.submit)

    def approve(self, permit_id: str, by: str) -> Permit:
        return self._apply(permit_id, Permit.approve, by)

    def reject(self, permit_id: str, by: str, reason: str = "") -> Permit:
        return self._apply(permit_id, Permit.reject, by, reason)

    def revoke(self, permit_id: str, by: str, reason: str = "") -> Permit:
        return self._apply(permit_id, Permit.revoke, by, reason)


__all__ = [
    "PermitService",
    "PermitNotFoundError",
    "PermitTransitionError",
    "init_permit_store",
    "get_permit_store",
]
