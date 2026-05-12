"""Shared Redis client pool singleton.

Problem: each ``redis.Redis.from_url(...)`` call creates its own
``ConnectionPool`` (default ``max_connections=50``).  The anonymizer has 9
distinct call sites — three of which were per-request — producing up to
~450 idle sockets per replica plus thrashing on hot endpoints like
``/health`` (probed every 10 s by k8s).

This module exposes a process-wide singleton client per ``(url,
decode_responses)`` pair so the same pool is reused everywhere.  Pool size,
socket timeouts, and proactive health checks are configured in one place via
environment variables:

    MEDANON_REDIS_MAX_CONNECTIONS   pool size cap (default 50)
    MEDANON_REDIS_SOCKET_TIMEOUT    blocking-op timeout, seconds (default 5)
    MEDANON_REDIS_CONNECT_TIMEOUT   TCP connect timeout, seconds (default 2)
    MEDANON_REDIS_HEALTHCHECK_SEC   ``health_check_interval`` (default 30)

Hot-loop callers (RedisJobStore, gPAS RedisCircuitBreaker) keep their own
clients with caller-specific socket timeouts because they need different
blocking semantics — those are explicitly documented at their call sites.

Thread-safe: ``redis.ConnectionPool`` itself is thread-safe; the singleton
dict is guarded by a lock for the rare initialisation race.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("medanon.redis_pool")

_CLIENTS: dict[tuple[str, bool], object] = {}
_MODULE_ID: int | None = None  # id of the cached redis module — invalidates on swap
_LOCK = threading.Lock()


def get_redis(url: str, *, decode_responses: bool = True):
    """Return a process-wide cached ``redis.Redis`` client for *url*.

    Subsequent calls with the same ``(url, decode_responses)`` pair return
    the same client instance, sharing its underlying ``ConnectionPool``.

    Returns ``None`` only if the ``redis`` package is not installed.

    The client is **not** pinged here — callers that need fail-fast
    semantics should call ``.ping()`` explicitly.  Pinging in this factory
    would defeat the lazy-connect design: ``/health`` calls would pay
    a round-trip on every request even though the cached client already
    knows the connection is good.
    """
    global _MODULE_ID

    if not url:
        raise ValueError("redis url is required")

    try:
        import redis as _redis
    except ImportError:  # pragma: no cover — packaged in requirements.txt
        logger.warning("redis package missing — get_redis returning None")
        return None

    # If sys.modules["redis"] was swapped (e.g. tests install a mock),
    # the cached clients reference a stale module — drop the cache.
    if _MODULE_ID is not None and _MODULE_ID != id(_redis):
        with _LOCK:
            if _MODULE_ID != id(_redis):
                _CLIENTS.clear()
                _MODULE_ID = id(_redis)

    key = (url, decode_responses)
    cached = _CLIENTS.get(key)
    if cached is not None:
        return cached

    with _LOCK:
        cached = _CLIENTS.get(key)
        if cached is not None:
            return cached

        # ``StrictRedis`` is an alias of ``Redis`` in modern redis-py (>=3),
        # but using StrictRedis keeps compat with code/tests that only
        # provide that name.
        client_cls = getattr(_redis, "StrictRedis", None) or getattr(_redis, "Redis", None)
        if client_cls is None:
            logger.warning("redis module has neither StrictRedis nor Redis class")
            return None

        max_conn = int(os.environ.get("MEDANON_REDIS_MAX_CONNECTIONS", "50"))
        socket_timeout = float(os.environ.get("MEDANON_REDIS_SOCKET_TIMEOUT", "5"))
        connect_timeout = float(os.environ.get("MEDANON_REDIS_CONNECT_TIMEOUT", "2"))
        health_interval = int(os.environ.get("MEDANON_REDIS_HEALTHCHECK_SEC", "30"))

        client = client_cls.from_url(
            url,
            decode_responses=decode_responses,
            socket_timeout=socket_timeout,
            socket_connect_timeout=connect_timeout,
            health_check_interval=health_interval,
            max_connections=max_conn,
            retry_on_timeout=True,
        )
        _CLIENTS[key] = client
        _MODULE_ID = id(_redis)
        # One INFO line per (url, decode) pair — quiet in steady state.
        masked = url.split("@")[-1] if "@" in url else url
        logger.info(
            "redis_pool_init host=%s decode=%s max_connections=%d socket_timeout=%.1fs",
            masked, decode_responses, max_conn, socket_timeout,
        )
        return client


def reset_for_tests() -> None:
    """Clear cached clients. Test-only helper."""
    global _MODULE_ID
    with _LOCK:
        _CLIENTS.clear()
        _MODULE_ID = None


def close_all() -> None:
    """Close every cached client.  Call only on shutdown."""
    global _MODULE_ID
    with _LOCK:
        for client in _CLIENTS.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best effort
                pass
        _CLIENTS.clear()
        _MODULE_ID = None
