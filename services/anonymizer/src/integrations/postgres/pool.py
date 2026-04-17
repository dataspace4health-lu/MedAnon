"""Shared PostgreSQL connection pool singleton.

Provides a single ``ThreadedConnectionPool`` instance that can be shared across
all application-state stores (jobs, subscriptions, config, staging) to avoid
creating multiple independent pools against the same database.

Usage::

    from integrations.postgres.pool import get_pool, close_pool

    pool = get_pool("postgresql://user:pass@host:5432/medanon")
    conn = pool.getconn()
    try:
        ...
    finally:
        pool.putconn(conn)
"""

from __future__ import annotations

import logging
import threading

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.postgres")

_lock = threading.Lock()
_pool: ThreadedConnectionPool | None = None
_db_url: str | None = None


def get_pool(
    db_url: str,
    minconn: int = 5,
    maxconn: int = 25,
) -> ThreadedConnectionPool:
    """Return the shared connection pool, creating it on first call.

    Subsequent calls with the **same** *db_url* return the existing pool.
    Calling with a *different* URL raises ``RuntimeError`` — call
    ``close_pool()`` first if a reconnect is needed.
    """
    global _pool, _db_url

    if _pool is not None:
        if db_url != _db_url:
            raise RuntimeError(
                f"Pool already initialised for {_db_url!r}; "
                f"close_pool() before connecting to {db_url!r}"
            )
        return _pool

    with _lock:
        # Double-check under lock
        if _pool is not None:
            return _pool

        logger.info("creating shared PostgreSQL pool (min=%d, max=%d)", minconn, maxconn)
        _pool = ThreadedConnectionPool(minconn, maxconn, db_url)
        _db_url = db_url
        return _pool


def close_pool() -> None:
    """Close the shared pool and release all connections."""
    global _pool, _db_url

    with _lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
            _db_url = None
            logger.info("shared PostgreSQL pool closed")
