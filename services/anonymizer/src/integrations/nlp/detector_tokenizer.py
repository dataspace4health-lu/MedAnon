"""Token state management, entity detection cache, span replacement, and XHTML scrubbing.

Provides the deterministic surrogate-token system used by NLP scrubbing in
tokenize mode, the functools.lru_cache-backed detection cache that avoids
repeated spaCy inference for identical texts, and the XHTML-safe text-node
walker for FHIR Narrative divs.
"""

from __future__ import annotations

import functools
import threading
from html import escape
from html.parser import HTMLParser

from integrations.nlp.detector_recognizers import _get_analyzer

# ---------------------------------------------------------------------------
# Token state
# ---------------------------------------------------------------------------

_GLOBAL_TOKEN_STATE: dict = {"next": {}, "map": {}, "reverse": {}}
_GLOBAL_TOKEN_LOCK = threading.Lock()
_TOKEN_STATE_MAX_ENTRIES = 100_000

# ---------------------------------------------------------------------------
# Entity detection cache
# Uses functools.lru_cache (C-implemented) for O(1) eviction without
# Python-level locking.
# ---------------------------------------------------------------------------

_DETECTION_CACHE_MAX = 20_000


@functools.lru_cache(maxsize=_DETECTION_CACHE_MAX)
def _detect_entities_cached(
    text: str, entities: tuple, threshold: float, language: str
) -> tuple:
    """Run Presidio entity detection, caching results by text content.

    Returns a tuple of ``(start, end, entity_type)`` tuples sorted descending
    by start position, ready for right-to-left span replacement.
    """
    analyzer = _get_analyzer()
    presidio_results = analyzer.analyze(
        text=text, entities=list(entities), language=language
    )
    return tuple(sorted(
        (
            (r.start, r.end, r.entity_type)
            for r in presidio_results
            if r.score >= threshold
        ),
        key=lambda h: h[0],
        reverse=True,
    ))


def reset_detection_cache() -> None:
    """Clear the detection cache. Useful between test runs."""
    _detect_entities_cached.cache_clear()


def reset_global_token_state() -> None:
    """Clear the global NLP token state. Call between batch runs to prevent unbounded growth."""
    with _GLOBAL_TOKEN_LOCK:
        _GLOBAL_TOKEN_STATE["next"].clear()
        _GLOBAL_TOKEN_STATE["map"].clear()
        _GLOBAL_TOKEN_STATE["reverse"].clear()


def _evict_if_needed(token_state: dict, limit: int = _TOKEN_STATE_MAX_ENTRIES) -> None:
    """Drop the oldest 25% of entries when the map exceeds *limit*."""
    if len(token_state["map"]) <= limit:
        return
    evict_count = len(token_state["map"]) // 4
    keys_to_drop = list(token_state["map"].keys())[:evict_count]
    for key in keys_to_drop:
        token = token_state["map"].pop(key, None)
        if token:
            token_state["reverse"].pop(token, None)


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

    hits = _detect_entities_cached(text, tuple(entities), threshold, language)
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
