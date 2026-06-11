"""Shared PostgreSQL connection pool singleton.

Provides a single ``ThreadedConnectionPool`` instance that can be shared across
all application-state stores (jobs, subscriptions, config, staging) to avoid
creating multiple independent pools against the same database.

Resilience features:
  - DSN enrichment: ``connect_timeout=5`` and ``statement_timeout=30000``
  - Circuit breaker: trips after 3 failures, recovers after 15s
  - ``get_conn`` / ``safe_putconn`` helpers for all stores

Usage::

    from integrations.postgres.pool import get_pool, get_conn, safe_putconn

    pool = get_pool("postgresql://user:pass@host:5432/medanon")
    conn = get_conn(pool)
    try:
        ...
    finally:
        safe_putconn(pool, conn)
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import parse_qs, quote, urlparse, urlunparse

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger("medanon.postgres")

_lock = threading.Lock()
_pool: ThreadedConnectionPool | None = None
_db_url: str | None = None

# Circuit breaker for all PostgreSQL operations through the shared pool.
_pg_cb = CircuitBreaker(
    name="postgres-app",
    failure_threshold=int(os.environ.get("PG_CB_FAILURE_THRESHOLD", "3")),
    recovery_timeout_sec=float(os.environ.get("PG_CB_RECOVERY_TIMEOUT_SEC", "15")),
    window_sec=60,
    half_open_probes=2,
)


def _enrich_dsn(db_url: str) -> str:
    """Append connect_timeout and statement_timeout if not already present."""
    parsed = urlparse(db_url)
    qs = parse_qs(parsed.query)

    if "connect_timeout" not in qs:
        qs["connect_timeout"] = ["5"]
    if "options" not in qs:
        qs["options"] = ["-c statement_timeout=30000"]

    # libpq decodes percent-escapes only (NOT form-encoded ``+`` as space),
    # so we cannot use ``urlencode`` (which form-encodes spaces as ``+``)
    # for any value that may contain whitespace, e.g. ``options=-c foo=bar``.
    # Build the query string manually using ``quote`` with no safe chars so
    # spaces become ``%20``, which libpq parses correctly.
    parts = []
    for key, values in qs.items():
        for value in values:
            parts.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
    new_query = "&".join(parts)
    return urlunparse(parsed._replace(query=new_query))


def get_pool(
    db_url: str,
    minconn: int = 5,
    maxconn: int | None = None,
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

        if maxconn is None:
            from utils.pool_budget import pg_pool_budget

            maxconn = pg_pool_budget()

        enriched = _enrich_dsn(db_url)
        logger.info(
            "creating shared PostgreSQL pool (min=%d, max=%d)", minconn, maxconn
        )
        _pool = ThreadedConnectionPool(minconn, maxconn, enriched)
        _db_url = db_url  # store original URL for identity comparison
        return _pool


def get_conn(pool: ThreadedConnectionPool):
    """Acquire a connection from *pool*, guarded by the circuit breaker."""
    if not _pg_cb.allow_request():
        raise psycopg2.OperationalError("PostgreSQL circuit breaker is OPEN")
    try:
        conn = pool.getconn()
        _pg_cb.record_success()
        return conn
    except Exception:
        _pg_cb.record_failure()
        raise


def safe_putconn(pool: ThreadedConnectionPool, conn) -> None:
    """Return *conn* to *pool*, closing broken connections cleanly."""
    try:
        if conn.closed:
            pool.putconn(conn, close=True)
        else:
            pool.putconn(conn)
    except Exception:
        pass


def pool_health() -> dict:
    """Return health info for readiness probes."""
    info: dict = {"available": _pool is not None, "circuit_breaker": _pg_cb.stats}
    if _pool is not None:
        try:
            conn = _pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                info["ping"] = "ok"
            finally:
                safe_putconn(_pool, conn)
        except Exception as exc:
            info["ping"] = f"error: {exc}"
    return info


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
