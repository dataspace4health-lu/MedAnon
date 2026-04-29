"""Idempotency-Key support for mutating HTTP endpoints.

Stripe-style ``Idempotency-Key`` header: clients send an opaque key on a
mutating request; the server caches the response keyed by ``(api_key, idem_key)``
for a TTL window so that retries return the original response without
re-executing the operation.

Conflict semantics (RFC 9457-aligned):
    * Header missing                → no idempotency check; execute normally.
    * Key seen, body hash matches   → return cached response (HTTP status preserved).
    * Key seen, body hash differs   → 409 Conflict (key reused for a different request).

Storage backends:
    * ``LocalIdempotencyStore`` — in-process dict with TTL eviction.  Single-instance
      only.  Used when Redis is unavailable.
    * ``RedisIdempotencyStore`` — shared across replicas via Redis ``SET key val EX ttl``.
      Selected automatically when a Redis client is provided at startup.

The store is intentionally separate from ``utils.cache.CacheBackend`` because
the access pattern (write-once, read-many, fixed TTL, JSON values) and the
failure semantics (cache miss must execute, not error) are different.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Protocol

logger = logging.getLogger("medanon.idempotency")

# How long a cached response is replayable.  24 h matches Stripe's default.
DEFAULT_TTL_SEC = int(os.environ.get("MEDANON_IDEMPOTENCY_TTL_SEC", "86400"))

# Maximum permitted Idempotency-Key length.  Stripe caps at 255; we follow.
_MAX_KEY_LEN = 255


def hash_body(body: bytes | str | dict | list) -> str:
    """Return a deterministic SHA-256 hex digest for *body*.

    Dicts/lists are JSON-canonicalised first so logically equal payloads
    produce the same hash regardless of key order or whitespace.
    """
    if isinstance(body, (dict, list)):
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    elif isinstance(body, str):
        payload = body.encode("utf-8")
    else:
        payload = body
    return hashlib.sha256(payload).hexdigest()


class IdempotencyStore(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict, ttl_sec: int) -> None: ...


class LocalIdempotencyStore:
    """Thread-safe in-memory store with timestamp-based TTL eviction.

    Single-instance only.  Suitable for development and single-replica
    deployments.  Entries are expired lazily on read; a soft cap of 10 000
    entries triggers a sweep when exceeded.
    """

    _SOFT_CAP = 10_000

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, dict]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: str, value: dict, ttl_sec: int) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl_sec, value)
            if len(self._data) > self._SOFT_CAP:
                self._sweep()

    def _sweep(self) -> None:
        now = time.time()
        expired = [k for k, (exp, _) in self._data.items() if exp < now]
        for k in expired:
            self._data.pop(k, None)


class RedisIdempotencyStore:
    """Redis-backed store; cross-replica safe via ``SET key val EX ttl``.

    Errors swallowed: a Redis outage degrades to "no idempotency" rather than
    failing the request.  This matches ``utils.cache.RedisCache`` behaviour.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, key: str) -> dict | None:
        try:
            raw = self._client.get(self._k(key))
        except Exception:
            logger.debug("idempotency_redis_get_failed key=%s", key, exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("idempotency_redis_corrupt_value key=%s", key)
            return None

    def put(self, key: str, value: dict, ttl_sec: int) -> None:
        try:
            self._client.set(self._k(key), json.dumps(value), ex=ttl_sec)
        except Exception:
            logger.debug("idempotency_redis_put_failed key=%s", key, exc_info=True)

    @staticmethod
    def _k(key: str) -> str:
        return f"medanon:idem:{key}"


# Module-level singleton selected at startup (see api/main.py).
_store: IdempotencyStore | None = None


def init_store(store: IdempotencyStore) -> None:
    """Install the active idempotency store (called from app startup)."""
    global _store
    _store = store
    logger.info("idempotency_store=%s", type(store).__name__)


def get_store() -> IdempotencyStore | None:
    """Return the active store or *None* if not yet initialised."""
    return _store


def validate_key(key: str | None) -> str | None:
    """Validate the client-supplied ``Idempotency-Key`` header.

    Returns the trimmed key, or *None* if the header is absent/blank.
    Raises :class:`ValueError` for malformed keys (too long, control chars).
    """
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > _MAX_KEY_LEN:
        raise ValueError(f"Idempotency-Key exceeds {_MAX_KEY_LEN} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        raise ValueError("Idempotency-Key contains control characters")
    return key


def lookup_or_conflict(scope: str, key: str, body_hash: str) -> dict | None:
    """Look up a previous response for ``(scope, key)`` and validate body hash.

    *scope* should namespace the key by endpoint identity (typically the
    route's path) so that the same idempotency key reused on a different
    endpoint does not accidentally replay an unrelated cached response.

    Returns the cached entry (with keys ``status`` and ``body``) on a hit,
    or ``None`` when no entry exists.  Raises :class:`KeyError` when the
    key was reused with a different body hash — the caller should translate
    that into HTTP 409 Conflict.
    """
    store = get_store()
    if store is None:
        return None
    composite = f"{scope}:{key}"
    entry = store.get(composite)
    if entry is None:
        return None
    if entry.get("body_hash") != body_hash:
        raise KeyError("idempotency_key_reused_with_different_body")
    return entry


def remember(
    scope: str, key: str, body_hash: str, status: int, body: Any,
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> None:
    """Store a successful response for future replay under ``(scope, key)``.

    Failures (4xx/5xx) are intentionally not cached so a transient downstream
    error doesn't pin a bad response under the client's idempotency key.
    """
    if not (200 <= status < 300):
        return
    store = get_store()
    if store is None:
        return
    composite = f"{scope}:{key}"
    store.put(
        composite,
        {"body_hash": body_hash, "status": status, "body": body},
        ttl_sec,
    )
