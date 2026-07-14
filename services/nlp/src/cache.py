"""Optional Redis L2 cache for NLP entity-detection results.

The in-process LRU (``functools.lru_cache``) in tokenizer.py is the L1 cache
fast, but wiped on every restart. This module adds an optional shared Redis
cache so detection results survive restarts and are visible across replicas.

Falls back gracefully: if Redis is unreachable or the ``redis`` package is not
installed, NLP runs with L1 only.

Env vars:
    NLP_REDIS_URL          Redis connection URL (e.g. ``redis://:pw@redis:6379/2``)
                           Falls back to ``MEDANON_REDIS_URL`` if unset.
    NLP_REDIS_TTL_SEC      Cache entry TTL in seconds (default: 604800 = 7 days)
    NLP_REDIS_KEY_PREFIX   Key prefix override (default: ``medanon:nlp:detect:``)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("nlp.cache")

_DEFAULT_TTL_SEC = int(os.environ.get("NLP_REDIS_TTL_SEC", "604800"))
_KEY_PREFIX = os.environ.get("NLP_REDIS_KEY_PREFIX", "medanon:nlp:detect:")

# Prometheus L2 cache effectiveness counters (E5.6)  optional, degrade to no-op
# when prometheus_client is absent.  Exposed via the NLP service /metrics route.
try:
    from prometheus_client import Counter as _Counter

    _L2_HITS = _Counter(
        "medanon_nlp_l2_cache_hits_total",
        "NLP L2 (Redis) detection cache hits",
    )
    _L2_MISSES = _Counter(
        "medanon_nlp_l2_cache_misses_total",
        "NLP L2 (Redis) detection cache misses",
    )
except Exception:  # pragma: no cover - prometheus optional
    _L2_HITS = None
    _L2_MISSES = None


def _l2_inc(counter) -> None:
    if counter is not None:
        try:
            counter.inc()
        except Exception:
            pass


class RedisDetectionCache:
    """Thin Redis wrapper for ``_detect_entities_cached`` results.

    Cache key shape: ``<prefix><lang>:<threshold>:<entities-hash>:<text-sha256>``

    Stored value: JSON-encoded list of ``[start, end, entity_type]`` triples.
    """

    def __init__(self, url: str, ttl_sec: int = _DEFAULT_TTL_SEC) -> None:
        import redis  # imported lazily so missing dep degrades to L1-only

        self._client = redis.Redis.from_url(
            url,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=30,
        )
        self._ttl = ttl_sec
        # Fail fast if Redis is unreachable  caller catches and disables L2.
        self._client.ping()

    @staticmethod
    def _make_key(text: str, entities: tuple, threshold: float, language: str) -> str:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        ent_str = ",".join(sorted(entities)) if entities else ""
        ent_hash = hashlib.sha1(ent_str.encode("utf-8")).hexdigest()[:12]
        return f"{_KEY_PREFIX}{language}:{threshold}:{ent_hash}:{text_hash}"

    def get(
        self, text: str, entities: tuple, threshold: float, language: str
    ) -> Optional[tuple]:
        try:
            raw = self._client.get(self._make_key(text, entities, threshold, language))
        except Exception as exc:  # network / timeout  degrade to L1-only path
            logger.warning("nlp_l2_get_error: %s", type(exc).__name__)
            return None
        if raw is None:
            _l2_inc(_L2_MISSES)
            return None
        try:
            data = json.loads(raw)
            result = tuple((int(s), int(e), str(t)) for s, e, t in data)
            _l2_inc(_L2_HITS)
            return result
        except Exception as exc:
            logger.warning("nlp_l2_decode_error: %s", type(exc).__name__)
            return None

    def set(
        self,
        text: str,
        entities: tuple,
        threshold: float,
        language: str,
        value: tuple,
    ) -> None:
        try:
            payload = json.dumps([[s, e, t] for s, e, t in value])
            self._client.setex(
                self._make_key(text, entities, threshold, language),
                self._ttl,
                payload,
            )
        except Exception as exc:
            logger.warning("nlp_l2_set_error: %s", type(exc).__name__)


def init_l2_cache() -> Optional[RedisDetectionCache]:
    """Create the L2 cache if NLP_REDIS_URL / MEDANON_REDIS_URL is set.

    Returns ``None`` when L2 is disabled or unreachable  callers must handle
    the ``None`` case (run with L1 only).
    """
    url = os.environ.get("NLP_REDIS_URL") or os.environ.get("MEDANON_REDIS_URL")
    if not url:
        logger.info("nlp_l2_cache_disabled (no NLP_REDIS_URL / MEDANON_REDIS_URL)")
        return None
    try:
        cache = RedisDetectionCache(url)
        logger.info(
            "nlp_l2_cache_enabled url=%s ttl=%ds", _redact(url), _DEFAULT_TTL_SEC
        )
        return cache
    except Exception as exc:
        logger.warning(
            "nlp_l2_cache_init_failed: %s  running with L1 only", type(exc).__name__
        )
        return None


def _redact(url: str) -> str:
    """Strip credentials from a Redis URL for log output."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    return url[: scheme_sep + 3] + "***@" + url.split("@", 1)[1]
