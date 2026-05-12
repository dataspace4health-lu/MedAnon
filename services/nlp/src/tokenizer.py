"""Token state management, entity detection cache, span replacement, and XHTML scrubbing.

Provides the deterministic surrogate-token system used by NLP scrubbing in
tokenize mode, the functools.lru_cache-backed detection cache that avoids
repeated spaCy inference for identical texts, and the XHTML-safe text-node
walker for FHIR Narrative divs.
"""

from __future__ import annotations

import functools
import os
import re
import threading
from html import escape
from html.parser import HTMLParser

from recognizers import _get_analyzer

# ---------------------------------------------------------------------------
# Token state
# ---------------------------------------------------------------------------

_GLOBAL_TOKEN_STATE: dict = {"next": {}, "map": {}, "reverse": {}}
_GLOBAL_TOKEN_LOCK = threading.Lock()
_TOKEN_STATE_MAX_ENTRIES = 100_000
_token_state_overflow_warned = False

# ---------------------------------------------------------------------------
# Entity detection cache
# Uses functools.lru_cache (C-implemented) for O(1) eviction without
# Python-level locking.
# ---------------------------------------------------------------------------

_DETECTION_CACHE_MAX = 20_000
# Texts longer than this threshold are detected directly without caching.
# A very long FHIR Narrative can occupy the same cache slot as thousands of
# short ID strings, degrading hit-rate and inflating memory usage.
_DETECTION_CACHE_MAX_TEXT_LEN = int(os.environ.get("NLP_CACHE_MAX_TEXT_LEN", "8192"))

# DATE_TIME false-positive filter: spans that Presidio's DateRecognizer matches
# but are actually age expressions or dosage/duration patterns.
_DATE_FP_RE = re.compile(
    r"^\d{1,3}\s*-?\s*(?:year|yr|month|day|week|hour|hr)s?\s*-?\s*old\b"
    r"|^age\s*[:\-]?\s*\d{1,3}$"
    r"|^\d{1,3}\s+(?:day|week|month|hour|hr|minute|min)s?$"
    r"|^\d{1,3}\s+(?:day|week|month|hour|hr|minute|min)s?\s+(?:pack|supply|course|dose|tablet|capsule)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# L2 cache (Redis) — optional, set by main.py at startup via set_l2_cache().
# ``None`` disables L2 (L1 lru_cache still applies). All access is best-effort:
# Redis errors are logged inside cache.py and degrade silently to L1-only.
# ---------------------------------------------------------------------------

_l2_cache = None  # type: ignore[var-annotated]


def set_l2_cache(cache) -> None:
    """Install or remove the optional Redis L2 cache backend."""
    global _l2_cache
    _l2_cache = cache


def _detect_entities_uncached(
    text: str, entities: tuple, threshold: float, language: str
) -> tuple:
    """Run Presidio detection without any caching layer (compute path)."""
    analyzer = _get_analyzer()
    presidio_results = analyzer.analyze(
        text=text, entities=list(entities), language=language
    )
    return tuple(sorted(
        (
            (r.start, r.end, r.entity_type)
            for r in presidio_results
            if r.score >= threshold
            and not (r.entity_type == "DATE_TIME" and _DATE_FP_RE.match(text[r.start:r.end]))
        ),
        key=lambda h: h[0],
        reverse=True,
    ))


@functools.lru_cache(maxsize=_DETECTION_CACHE_MAX)
def _detect_entities_cached(
    text: str, entities: tuple, threshold: float, language: str
) -> tuple:
    """L1+L2 cached entity detection.

    Lookup order:
      1. functools.lru_cache (this decorator)              — RAM, ~µs
      2. Redis L2 cache (optional, set via set_l2_cache)   — network, ~ms
      3. Presidio + spaCy compute path                     — CPU, ~10-100 ms

    On L2 hit we still populate L1 implicitly via the lru_cache return path.
    On compute, both layers are written.

    Returns a tuple of ``(start, end, entity_type)`` tuples sorted descending
    by start position, ready for right-to-left span replacement.
    """
    # L2 lookup before paying the Presidio cost
    if _l2_cache is not None:
        l2_value = _l2_cache.get(text, entities, threshold, language)
        if l2_value is not None:
            return l2_value

    # Compute and write-through to L2
    result = _detect_entities_uncached(text, entities, threshold, language)
    if _l2_cache is not None:
        _l2_cache.set(text, entities, threshold, language, result)
    return result


def reset_detection_cache() -> None:
    """Clear the L1 detection cache. Useful between test runs.

    Does NOT clear the Redis L2 cache — call ``redis-cli FLUSHDB`` or wait
    for TTL expiry if a full reset is required.
    """
    _detect_entities_cached.cache_clear()


def _detect_entities(text: str, entities: tuple, threshold: float, language: str) -> tuple:
    """Run entity detection, bypassing the L1 LRU for very long texts.

    Long texts (e.g. FHIR Narrative divs) would each occupy a cache slot that
    could otherwise hold thousands of short strings, degrading cache hit-rate
    and inflating memory usage. Texts exceeding ``_DETECTION_CACHE_MAX_TEXT_LEN``
    skip L1 but still consult the L2 cache (if enabled), since long narratives
    repeat exactly across patients in many real-world datasets.
    """
    if len(text) > _DETECTION_CACHE_MAX_TEXT_LEN:
        if _l2_cache is not None:
            l2_value = _l2_cache.get(text, entities, threshold, language)
            if l2_value is not None:
                return l2_value
        result = _detect_entities_uncached(text, entities, threshold, language)
        if _l2_cache is not None:
            _l2_cache.set(text, entities, threshold, language, result)
        return result
    return _detect_entities_cached(text, entities, threshold, language)


def reset_global_token_state() -> None:
    """Clear the global NLP token state. Call between batch runs to prevent unbounded growth."""
    global _token_state_overflow_warned
    with _GLOBAL_TOKEN_LOCK:
        _GLOBAL_TOKEN_STATE["next"].clear()
        _GLOBAL_TOKEN_STATE["map"].clear()
        _GLOBAL_TOKEN_STATE["reverse"].clear()
        _token_state_overflow_warned = False


def _evict_if_needed(token_state: dict, limit: int = _TOKEN_STATE_MAX_ENTRIES) -> None:
    """Log a one-time warning when the token map exceeds *limit*; never evict.

    Evicting mid-job corrupts the reverse mapping for evicted values —
    de-tokenization breaks and re-encountered values get new token numbers,
    violating surrogate consistency.  Callers should reset token state
    between jobs with reset_global_token_state() instead.
    """
    if len(token_state["map"]) <= limit:
        return
    global _token_state_overflow_warned
    if not _token_state_overflow_warned:
        _token_state_overflow_warned = True
        import logging as _logging
        _logging.getLogger("nlp.tokenizer").warning(
            "token_state map exceeded limit=%d entries — no mid-run eviction "
            "to preserve surrogate consistency. Call reset_global_token_state() "
            "between jobs to reclaim memory.", limit,
        )


def _tokenize(value: str, entity_type: str, token_state: dict, lock=None) -> str:
    """Return a deterministic surrogate token for *value*."""
    if lock:
        with lock:
            return _tokenize_unlocked(value, entity_type, token_state)
    return _tokenize_unlocked(value, entity_type, token_state)


def _tokenize_unlocked(value: str, entity_type: str, token_state: dict) -> str:
    """Assign or retrieve the surrogate token — assumes lock already held if needed."""
    key = (entity_type, value)
    if key in token_state["map"]:
        return token_state["map"][key]
    _evict_if_needed(token_state)
    seq = token_state["next"].get(entity_type, 0) + 1
    token_state["next"][entity_type] = seq
    token = f"[[{entity_type}_{seq}]]"
    token_state["map"][key] = token
    token_state["reverse"][token] = value
    return token


# ---------------------------------------------------------------------------
# Core analyzer
# ---------------------------------------------------------------------------


def _analyze_and_replace(
    text: str,
    entities: list,
    threshold: float,
    language: str,
    mode: str,
    token_state: dict,
    token_lock=None,
) -> str:
    """Run Presidio NLP analysis on *text* and replace detected PHI spans.

    Detection is cached by text content so repeated identical strings skip
    re-inference. Only the replacement step runs on each call.
    """
    if not text or not text.strip():
        return text

    hits = _detect_entities(text, tuple(entities), threshold, language)
    if not hits:
        return text

    # Build replacement segments right-to-left, then reverse+join once — O(L+H).
    parts: list[str] = []
    cursor = len(text)
    for start, end, entity_type in hits:
        span = text[start:end]
        if not span.strip():
            continue
        if mode == "redact":
            replacement = f"[{entity_type}]"
        else:
            replacement = _tokenize(span, entity_type, token_state, token_lock)
        parts.append(text[end:cursor])
        parts.append(replacement)
        cursor = start
    parts.append(text[:cursor])
    parts.reverse()
    return "".join(parts)


# ---------------------------------------------------------------------------
# XHTML text-node walker
# ---------------------------------------------------------------------------


class _XHTMLTextScrubber(HTMLParser):
    """Walk XHTML, applying *scrub_fn* to every text node."""

    def __init__(self, scrub_fn):
        super().__init__(convert_charrefs=False)
        self._scrub = scrub_fn
        self._parts: list = []

    def handle_starttag(self, tag, attrs):
        attr_str = "".join(
            f" {n}" if v is None else f' {n}="{escape(v)}"' for n, v in attrs
        )
        self._parts.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag):
        self._parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        attr_str = "".join(
            f" {n}" if v is None else f' {n}="{escape(v)}"' for n, v in attrs
        )
        self._parts.append(f"<{tag}{attr_str}/>")

    def handle_data(self, data):
        self._parts.append(self._scrub(data))

    def handle_entityref(self, name):
        self._parts.append(f"&{name};")

    def handle_charref(self, name):
        self._parts.append(f"&#{name};")

    def handle_comment(self, data):
        self._parts.append(f"<!--{data}-->")

    def result(self) -> str:
        return "".join(self._parts)


def _scrub_xhtml_text_nodes(xhtml: str, scrub_fn) -> str:
    """Apply *scrub_fn* to every text node in *xhtml*, preserving markup."""
    p = _XHTMLTextScrubber(scrub_fn)
    p.feed(xhtml)
    return p.result()
