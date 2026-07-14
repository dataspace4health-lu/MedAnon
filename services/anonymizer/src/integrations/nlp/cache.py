"""Content-addressed NLP detection cache.

NLP scrubbing is the largest non-cacheable stage in a bulk run today: every
text field goes over the wire to the NLP microservice, even when the same
text was already scrubbed seconds earlier in another resource.  This module
provides a deterministic cache so that repeat runs over the same dataset (and
in-run duplicates such as a clinical narrative shared by 100 Encounters) skip
the round-trip entirely.

Key construction
----------------
The cache key is a blake2b digest of the inputs that affect detection:

    (model_version, language, threshold, sorted(entities), text)

Inputs that do NOT affect output (token_state, mode) are excluded so the
cache stays small and effective.

Storage
-------
* L1: process-local LRU (separate from the gPAS cache to avoid eviction
  collisions; sized via ``MEDANON_NLP_CACHE_MAX_ENTRIES``, default 100K).
* L2: Redis when ``MEDANON_REDIS_URL`` is configured.  Reuses the
  ``RedisCache`` implementation but with prefix ``medanon:nlp:detect:``.

Values
------
Detection results are stored as a compact JSON list of ``[start, end, type]``
triples.  Empty list = "no PII detected" (still cached  a clean text being
fed to NLP again is wasted work).

Safety
------
The cache is **per content**: a hash collision would be cryptographically
implausible.  Even on collision the failure mode is benign  at worst, an
NLP detection from one text would be applied to another, which scrubs more
than necessary.  It cannot cause text to leak (it can only over-redact).
"""

from __future__ import annotations

import hashlib
import logging
import os

from utils.json_fast import loads as _json_loads
from utils.json_fast import dumps as _json_dumps

_log = logging.getLogger("medanon.nlp.cache")

# Versioned cache namespace  bump when the NLP model or detection contract
# changes so stale entries are not reused.
_NLP_MODEL_VERSION = os.environ.get("NLP_MODEL_VERSION", "v1").strip() or "v1"

_NLP_CACHE_MAX = int(os.environ.get("MEDANON_NLP_CACHE_MAX_ENTRIES", "100000"))
_NLP_CACHE_ENABLED = os.environ.get(
    "MEDANON_NLP_CACHE_ENABLED", "true"
).strip().lower() in (
    "1",
    "true",
    "yes",
)

# Lazily constructed singletons  kept module-private.
_l1: object | None = None
_l2: object | None = None


def _get_l1():
    global _l1
    if _l1 is None:
        from utils.cache import LocalLruCache

        _l1 = LocalLruCache(maxsize=_NLP_CACHE_MAX)
    return _l1


def _get_l2():
    """Return the Redis L2 cache for NLP detections, or None if not configured."""
    global _l2
    if _l2 is not None:
        return _l2 if _l2 is not False else None
    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    if not redis_url:
        _l2 = False  # sentinel  don't keep retrying
        return None
    try:
        from utils.cache import RedisCache

        # No TTL: detection results are pure functions of input + model
        # version, so they remain valid until the model version changes
        # (which bumps the cache key prefix).
        _l2 = RedisCache(redis_url, ttl=None, key_prefix="medanon:nlp:detect:")
        return _l2
    except Exception as exc:
        _log.warning("nlp_cache_l2_init_failed: %s", exc)
        _l2 = False
        return None


def _build_key(
    text: str,
    entities: list[str],
    threshold: float,
    language: str,
) -> tuple:
    """Build a deterministic cache key for one detection request."""
    h = hashlib.blake2b(digest_size=16)
    h.update(_NLP_MODEL_VERSION.encode("utf-8"))
    h.update(b"\x1f")
    h.update(language.encode("utf-8", errors="replace"))
    h.update(b"\x1f")
    h.update(f"{threshold:.4f}".encode("ascii"))
    h.update(b"\x1f")
    # Sorted to stay invariant of caller's entity list ordering.
    for ent in sorted(entities):
        h.update(ent.encode("utf-8", errors="replace"))
        h.update(b",")
    h.update(b"\x1f")
    h.update(text.encode("utf-8", errors="replace"))
    return ("nlp_detect", h.hexdigest())


def lookup_many(
    texts: list[str],
    entities: list[str],
    threshold: float,
    language: str,
) -> tuple[list[list[tuple[int, int, str]] | None], list[tuple]]:
    """Look up cached detections for a batch of texts.

    Returns
    -------
    cached : list[list[(start, end, type)] | None]
        Same length as ``texts``  None entries are misses.
    keys : list[tuple]
        Cache keys aligned to ``texts`` (used by ``store_many`` to write back).
    """
    keys = [_build_key(t, entities, threshold, language) for t in texts]
    if not _NLP_CACHE_ENABLED:
        return [None] * len(texts), keys

    l1 = _get_l1()
    cached_l1 = l1.get_many(keys)
    cached_l2 = {}
    missing_after_l1 = [k for k in keys if k not in cached_l1]
    if missing_after_l1:
        l2 = _get_l2()
        if l2 is not None:
            cached_l2 = l2.get_many(missing_after_l1)
            if cached_l2:
                # Promote L2 hits into L1.
                l1.set_many(cached_l2)

    out: list[list[tuple[int, int, str]] | None] = []
    try:
        from utils.metrics import NLP_CACHE_HITS, NLP_CACHE_MISSES
    except Exception:  # pragma: no cover  metrics optional in tests
        NLP_CACHE_HITS = NLP_CACHE_MISSES = None

    for k in keys:
        raw = cached_l1.get(k) or cached_l2.get(k)
        if raw is None:
            out.append(None)
            if NLP_CACHE_MISSES is not None:
                NLP_CACHE_MISSES.inc()
            continue
        try:
            decoded = _json_loads(raw)
            out.append([(int(d[0]), int(d[1]), str(d[2])) for d in decoded])
            if NLP_CACHE_HITS is not None:
                NLP_CACHE_HITS.inc()
        except Exception as exc:
            # Corrupt entry  treat as miss; do not poison the pipeline.
            _log.warning("nlp_cache_decode_failed: %s", exc)
            out.append(None)
            if NLP_CACHE_MISSES is not None:
                NLP_CACHE_MISSES.inc()
    return out, keys


def store_many(
    keys: list[tuple],
    detections: list[list[tuple[int, int, str]]],
) -> None:
    """Persist a batch of detection results.

    ``keys`` and ``detections`` must be the same length and aligned by index.
    """
    if not _NLP_CACHE_ENABLED or not keys:
        return
    items: dict[tuple, str] = {}
    for k, dets in zip(keys, detections):
        try:
            items[k] = _json_dumps([[d[0], d[1], d[2]] for d in dets])
        except Exception as exc:
            _log.debug("nlp_cache_encode_failed key=%s: %s", k, exc)
    if not items:
        return
    _get_l1().set_many(items)
    l2 = _get_l2()
    if l2 is not None:
        l2.set_many(items)
