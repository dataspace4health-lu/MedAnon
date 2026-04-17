"""Pluggable cache backend for gPAS result caching.

Two implementations:

- ``LocalLruCache``: per-process sharded LRU dict (default — preserves existing behaviour).
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
    def flush(self) -> int: ...
    def get_many(self, keys: list[tuple]) -> dict[tuple, str]: ...
    def set_many(self, items: dict[tuple, str]) -> None: ...


# ---------------------------------------------------------------------------
# Local sharded LRU (default — 8 shards with independent locks)
# ---------------------------------------------------------------------------

_NUM_SHARDS = 8


class _LruShard:
    """A single shard of the sharded LRU cache."""

    __slots__ = ("_cache", "_lock", "_maxsize")

    def __init__(self, maxsize: int) -> None:
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: tuple) -> str | None:
        with self._lock:
            value = self._cache.get(key)
            if value is not None:
                del self._cache[key]
                self._cache[key] = value
            return value

    def set(self, key: tuple, value: str) -> None:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self._maxsize:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = value

    def flush(self) -> int:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count


class LocalLruCache:
    """Thread-safe per-process sharded LRU cache.

    Splits entries across N shards (default 8), each with its own lock.
    This reduces lock contention when many threads access the cache concurrently.
    """

    def __init__(self, maxsize: int = _DEFAULT_MAX) -> None:
        shard_size = max(1, maxsize // _NUM_SHARDS)
        self._shards = [_LruShard(shard_size) for _ in range(_NUM_SHARDS)]
        self._maxsize = maxsize

    def _shard_for(self, key: tuple) -> _LruShard:
        return self._shards[hash(key) % _NUM_SHARDS]

    def get(self, key: tuple) -> str | None:
        return self._shard_for(key).get(key)

    def set(self, key: tuple, value: str) -> None:
        self._shard_for(key).set(key, value)

    def flush(self) -> int:
        return sum(s.flush() for s in self._shards)

    def get_many(self, keys: list[tuple]) -> dict[tuple, str]:
        """Batch get: acquire each shard lock once for all keys in that shard."""
        shard_keys: dict[int, list[tuple]] = {}
        for key in keys:
            idx = hash(key) % _NUM_SHARDS
            shard_keys.setdefault(idx, []).append(key)
        result: dict[tuple, str] = {}
        for idx, ks in shard_keys.items():
            shard = self._shards[idx]
            with shard._lock:
                for key in ks:
                    value = shard._cache.get(key)
                    if value is not None:
                        del shard._cache[key]
                        shard._cache[key] = value
                        result[key] = value
        return result

    def set_many(self, items: dict[tuple, str]) -> None:
        """Batch set: acquire each shard lock once for all keys in that shard."""
        shard_items: dict[int, list[tuple]] = {}
        for key in items:
            idx = hash(key) % _NUM_SHARDS
            shard_items.setdefault(idx, []).append(key)
        for idx, ks in shard_items.items():
            shard = self._shards[idx]
            with shard._lock:
                for key in ks:
                    value = items[key]
                    if key in shard._cache:
                        del shard._cache[key]
                    elif len(shard._cache) >= shard._maxsize:
                        oldest_key = next(iter(shard._cache))
                        del shard._cache[oldest_key]
                    shard._cache[key] = value


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

    def flush(self) -> int:
        """Delete all keys matching this cache's prefix. Returns count deleted."""
        try:
            count = 0
            cursor = 0
            pattern = self._prefix + "*"
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=500)
                if keys:
                    self._client.unlink(*keys)
                    count += len(keys)
                if cursor == 0:
                    break
            _cache_log.info("redis_cache_flushed count=%d prefix=%s", count, self._prefix)
            return count
        except Exception as exc:
            _cache_log.warning("redis_cache_flush_failed: %s", exc)
            return 0

    def get_many(self, keys: list[tuple]) -> dict[tuple, str]:
        if not keys:
            return {}
        try:
            redis_keys = [self._make_key(k) for k in keys]
            values = self._client.mget(redis_keys)
            result: dict[tuple, str] = {}
            for key, value in zip(keys, values):
                if value is not None:
                    result[key] = value
            return result
        except Exception as exc:
            _cache_log.warning("redis_mget_failed falling_through: %s", exc)
            return {}

    def set_many(self, items: dict[tuple, str]) -> None:
        if not items:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            for key, value in items.items():
                pipe.set(self._make_key(key), value, ex=self._ttl)
            pipe.execute()
        except Exception as exc:
            _cache_log.warning("redis_pipeline_set_failed skipping: %s", exc)


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

    def flush(self) -> int:
        """Flush both L1 and L2 caches. Returns total entries removed."""
        l1_count = self._l1.flush()
        l2_count = self._l2.flush()
        return l1_count + l2_count

    def get_many(self, keys: list[tuple]) -> dict[tuple, str]:
        result = self._l1.get_many(keys)
        missing = [k for k in keys if k not in result]
        if missing:
            l2_result = self._l2.get_many(missing)
            if l2_result:
                self._l1.set_many(l2_result)
                result.update(l2_result)
        return result

    def set_many(self, items: dict[tuple, str]) -> None:
        self._l1.set_many(items)
        self._l2.set_many(items)


# ---------------------------------------------------------------------------
# Module-level backend instance + injection helper
# ---------------------------------------------------------------------------

_cache_backend: CacheBackend = LocalLruCache()


def configure_cache(backend: CacheBackend) -> None:
    """Replace the active cache backend (called once at API startup)."""
    global _cache_backend
    _cache_backend = backend
    _cache_log.info("cache_backend=%s", type(backend).__name__)


def flush_cache() -> int:
    """Flush the active cache backend. Returns count of entries removed."""
    return _cache_backend.flush()
