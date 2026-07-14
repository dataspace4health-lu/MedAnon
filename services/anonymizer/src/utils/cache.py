"""Pluggable cache backend for gPAS result caching.

Two implementations:

- ``LocalLruCache``: per-process sharded LRU dict (default  preserves existing behaviour).
- ``RedisCache``: opt-in Redis-backed L2 cache for cross-replica pseudonym sharing.

Injection point: call ``configure_cache(RedisCache(...))`` once from the
FastAPI startup event when ``MEDANON_REDIS_URL`` is set.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import os
import threading
from typing import Protocol, runtime_checkable

_cache_log = logging.getLogger("medanon.cache")

# Sizing: a 130K-resource bulk export touches ~300K–400K unique values (IDs +
# cross-resource references). The LRU must hold the entire working set to avoid
# evictions that force re-fetches from gPAS (~150 ms each).
# Default raised from 50K → 300K; tune via MEDANON_CACHE_MAX_ENTRIES.
# Memory: ~150 bytes/entry × 300K ≈ 45 MB  well within the 3 GB anonymizer budget.
_DEFAULT_MAX = int(os.environ.get("MEDANON_CACHE_MAX_ENTRIES", "300000"))


# ---------------------------------------------------------------------------
# Protocol (structural typing  no ABC overhead)
# ---------------------------------------------------------------------------


@runtime_checkable
class CacheBackend(Protocol):
    def get(self, key: tuple) -> str | None: ...
    def set(self, key: tuple, value: str) -> None: ...
    def flush(self) -> int: ...
    def get_many(self, keys: list[tuple]) -> dict[tuple, str]: ...
    def set_many(self, items: dict[tuple, str]) -> None: ...


# ---------------------------------------------------------------------------
# Local sharded LRU (default  8 shards with independent locks)
# ---------------------------------------------------------------------------

_NUM_SHARDS = 8


class _LruShard:
    """A single shard of the sharded LRU cache."""

    __slots__ = ("_cache", "_lock", "_maxsize")

    def __init__(self, maxsize: int) -> None:
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: tuple) -> str | None:
        with self._lock:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
            return value

    def set(self, key: tuple, value: str) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = value
            else:
                if len(self._cache) >= self._maxsize:
                    self._cache.popitem(last=False)
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
                        shard._cache.move_to_end(key)
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
                        shard._cache.move_to_end(key)
                        shard._cache[key] = value
                    else:
                        if len(shard._cache) >= shard._maxsize:
                            shard._cache.popitem(last=False)
                        shard._cache[key] = value


# ---------------------------------------------------------------------------
# Redis L2 (opt-in for multi-replica deployments)
# ---------------------------------------------------------------------------


class RedisCache:
    """Redis-backed cache for cross-replica pseudonym sharing.

    Tuple keys are serialised to a Redis-compatible string using the ASCII
    Unit Separator (0x1f) as delimiter.

    TTL behaviour:
      ttl=None (default)  entries are written with no expiry.  Correct for
        pseudonym mappings, which are permanent facts derived from the gPAS
        vault.  Entries survive Redis restarts when AOF persistence is enabled.
      ttl=<seconds>       entries expire after the given number of seconds.
        Use only for non-permanent data (e.g. session tokens, rate-limit keys).

    Redis errors are swallowed with a warning log so a Redis outage degrades
    gracefully to direct gPAS calls rather than surfacing as processing errors.
    """

    def __init__(
        self,
        redis_url: str,
        ttl: int | None = None,
        key_prefix: str = "medanon:gpas:",
    ) -> None:
        # Use the process-wide shared pool so the gPAS cache, idempotency
        # store, audit writer, and health probe share a single set of
        # connections instead of each opening their own pool of 50.
        from utils.redis_pool import get_redis

        client = get_redis(redis_url, decode_responses=True)
        if client is None:
            # redis package missing  caller must handle by falling back to L1
            raise RuntimeError("redis package not installed; cannot create RedisCache")
        self._client = client
        self._ttl = ttl
        self._prefix = key_prefix

    def _make_key(self, key: tuple) -> str:
        # Use ASCII Unit Separator (\\x1f) as delimiter  faster than JSON serialization
        # and safe because FHIR values, URIs, and domain names never contain this byte.
        return self._prefix + "\x1f".join(str(x) for x in key)

    def get(self, key: tuple) -> str | None:
        try:
            return self._client.get(self._make_key(key))
        except Exception as exc:
            _cache_log.warning("redis_get_failed falling_through: %s", exc)
            return None

    def set(self, key: tuple, value: str) -> None:
        try:
            if self._ttl is not None:
                self._client.set(self._make_key(key), value, ex=self._ttl)
            else:
                self._client.set(self._make_key(key), value)
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
            _cache_log.info(
                "redis_cache_flushed count=%d prefix=%s", count, self._prefix
            )
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
                if self._ttl is not None:
                    pipe.set(self._make_key(key), value, ex=self._ttl)
                else:
                    pipe.set(self._make_key(key), value)
            pipe.execute()
        except Exception as exc:
            _cache_log.warning("redis_pipeline_set_failed skipping: %s", exc)


# ---------------------------------------------------------------------------
# Tiered L1/L2 cache (local in front of Redis)
# ---------------------------------------------------------------------------


class TieredCache:
    """L1 local LRU in front of L2 Redis  read: L1 → L2 → miss (promote on L2 hit), write: both."""

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
