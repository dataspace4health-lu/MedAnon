"""Transformation-passport report store singleton (D7.2 §5.5.1 / Art 79).

A risk-driven export produces an anonymous Transformation Passport; persisting
it here makes it a durable, queryable *report* independent of the job's own
retention. Mirrors the ``init_*_store`` / ``get_*_store`` idiom used by
``pipeline.permits``  Postgres-backed at startup when ``MEDANON_APP_DB_URL``
is set, otherwise a no-op (passports still ride on the job checkpoint, so the
per-job view keeps working without a DB).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("medanon.reports")

# Postgres-backed store installed at startup, or None (no durable report index).
_store: Any = None


def init_passport_store(*, store: Any = None) -> Any:
    """Install the process-wide passport report store (called once at startup)."""
    global _store
    _store = store
    return _store


def get_passport_store() -> Any:
    """Return the active passport report store, or None when not configured."""
    return _store


def init_passport_store_from_pool(pg_pool: Any) -> Any:
    """Install the Postgres report store from *pg_pool* (None → no-op store).

    The single wiring path shared by ``api.main`` and ``pipeline.jobs.worker_main``.
    Both processes MUST install the store: the API serves ``GET /v1/reports``,
    but the **worker** is what runs risk-driven exports and calls
    ``save_passport`` — and that call is a silent no-op in a process where the
    store was never installed, so a worker without it drops every passport.

    Never raises: a broken report store must not take startup down.
    """
    global _store
    if pg_pool is None:
        _store = None
        logger.info("passport_store=none (set MEDANON_APP_DB_URL for durable reports)")
        return None
    try:
        from integrations.postgres.passport_store import PostgresPassportStore

        store = PostgresPassportStore(pg_pool)
        store.ensure_schema()
        _store = store
        logger.info("passport_store=postgres")
    except Exception as exc:  # noqa: BLE001
        _store = None
        logger.warning("passport_store_start_failed: %s", exc)
    return _store


def save_passport(job_id: str, passport: dict) -> None:
    """Best-effort persist of a completed job's passport. Never raises.

    A missing store (no Postgres) is a silent no-op; a PII-guard rejection or
    a DB error is logged but never propagated  persisting the report must not
    fail the export.
    """
    store = _store
    if store is None or not job_id or not isinstance(passport, dict):
        return
    try:
        store.save(job_id, passport)
    except Exception as exc:  # noqa: BLE001  report persistence is best-effort
        logger.warning(
            "passport_persist_failed job=%s error=%s", job_id, type(exc).__name__
        )
