"""Pluggable cache backend for gPAS result caching.

Two implementations:

- ``LocalLruCache``: per-process LRU dict (default — preserves existing behaviour).
- ``RedisCache``: opt-in Redis-backed L2 cache for cross-replica pseudonym sharing.

Injection point: call ``configure_cache(RedisCache(...))`` once from the
FastAPI startup event when ``MEDANON_REDIS_URL`` is set.
"""

from __future__ import annotations

from utils.json_fast import dumps as _json_dumps
import logging
import threading
from typing import Protocol, runtime_checkable

_cache_log = logging.getLogger("medanon.cache")

_DEFAULT_MAX = 50_000


# ---------------------------------------------------------------------------
# Protocol (structural typing — no ABC overhead)
# ---------------------------------------------------------------------------


@runtime_checkable
class CacheBackend(Protocol):
    def get(self, key: tuple) -> str | None: ...
    def set(self, key: tuple, value: str) -> None: ...


# ---------------------------------------------------------------------------
# Local LRU (default — wraps the existing dict+lock approach)
# ---------------------------------------------------------------------------


class LocalLruCache:
    """Thread-safe per-process LRU cache with insertion-order eviction."""

    def __init__(self, maxsize: int = _DEFAULT_MAX) -> None:
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: tuple) -> str | None:
        with self._lock:
            value = self._cache.get(key)
            if value is not None:
                # Promote to most-recently-used position (move to end)
                del self._cache[key]
                self._cache[key] = value
            return value

    def set(self, key: tuple, value: str) -> None:
        with self._lock:
            if key in self._cache:
                # Move existing key to end (most-recently-used position)
                del self._cache[key]
            elif len(self._cache) >= self._maxsize:
                # Evict single oldest entry — O(1) via dict insertion order
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = value


# ---------------------------------------------------------------------------
# Redis L2 (opt-in for multi-replica deployments)
# ---------------------------------------------------------------------------


class RedisCache:
    """Redis-backed cache for cross-replica pseudonym sharing.

    Tuple keys are JSON-serialised to a Redis-compatible string.
    Values are stored as plain strings with a configurable TTL (default 1 hour).

    Redis errors are swallowed with a debug log so a Redis outage degrades
    gracefully to direct gPAS calls rather than surfacing as processing errors.
    """

    def __init__(
        self,
        redis_url: str,
        ttl: int = 3600,
        key_prefix: str = "medanon:gpas:",
    ) -> None:
        import redis as _redis  # lazy import — redis package is optional

        self._client = _redis.StrictRedis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=2,
            retry_on_timeout=True,
        )
        self._ttl = ttl
        self._prefix = key_prefix

    def _make_key(self, key: tuple) -> str:
        return self._prefix + _json_dumps(key)

    def get(self, key: tuple) -> str | None:
        try:
            return self._client.get(self._make_key(key))
        except Exception as exc:
            _cache_log.warning("redis_get_failed falling_through: %s", exc)
            return None

    def set(self, key: tuple, value: str) -> None:
        try:
            self._client.set(self._make_key(key), value, ex=self._ttl)
        except Exception as exc:
            _cache_log.warning("redis_set_failed skipping: %s", exc)


# ---------------------------------------------------------------------------
# Tiered L1/L2 cache (local in front of Redis)
# ---------------------------------------------------------------------------


class TieredCache:
    """L1 local LRU in front of L2 Redis — read: L1 → L2 → miss (promote on L2 hit), write: both."""

    def __init__(self, l1: LocalLruCache, l2: RedisCache) -> None:
        self._l1 = l1
        self._l2 = l2

    def get(self, key: tuple) -> str | None:
        value = self._l1.get(key)
        if value is not None:
            return value
        value = self._l2.get(key)
        if value is not None:
            self._l1.set(key, value)
        return value

    def set(self, key: tuple, value: str) -> None:
        self._l1.set(key, value)
        self._l2.set(key, value)


# ---------------------------------------------------------------------------
# Module-level backend instance + injection helper
# ---------------------------------------------------------------------------

_cache_backend: CacheBackend = LocalLruCache()


def configure_cache(backend: CacheBackend) -> None:
    """Replace the active cache backend (called once at API startup)."""
    global _cache_backend
    _cache_backend = backend
    _cache_log.info("cache_backend=%s", type(backend).__name__)
