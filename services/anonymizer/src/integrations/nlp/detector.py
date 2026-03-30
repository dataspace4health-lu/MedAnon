"""NLP-based PHI/PII detection and tokenization using Microsoft Presidio.

Uses Named Entity Recognition (NER) + built-in pattern recognizers to detect
PHI/PII that pure regex cannot reliably find: person names, locations, ages,
medical license numbers, and GDPR Art.9 NRP categories.

Designed to complement ``scrub_text`` (regex-based).  Running both actions on
the same field provides defence-in-depth coverage:
  1. ``scrub_text``  — fast, deterministic regex for structured patterns
                       (SSN, email, phone, ISO dates, ZIP codes …)
  2. ``nlp_detect``  — NLP/NER for unstructured prose (names, addresses,
                       medical identifiers mentioned in free text)

Key parameters (``params``):
    entities    list | 'all' | 'healthcare'  (default: 'healthcare')
                Which Presidio entity types to detect.
    threshold   float  (default: 0.4)
                Minimum confidence score — a lower value increases recall
                at the cost of more false positives.
    language    str    (default: 'en')
    mode        'tokenize' | 'redact'        (default: 'tokenize')
                ``tokenize`` → deterministic surrogate [[TYPE_N]]
                ``redact``   → static placeholder [TYPE]
    html        bool   (default: False)
                When ``True``, treat the matched field value as XHTML —
                only text nodes are scrubbed; markup is preserved.

Supported entity types (Presidio defaults + spaCy NER):
  PERSON, DATE_TIME, LOCATION, PHONE_NUMBER, EMAIL_ADDRESS,
  US_SSN, US_PASSPORT, US_DRIVER_LICENSE, MEDICAL_LICENSE,
  AGE, NRP, CREDIT_CARD, IBAN_CODE, US_BANK_NUMBER, US_ITIN, IP_ADDRESS, URL
"""

import logging
import threading
from html import escape
from html.parser import HTMLParser

from utils.fhirpath import find_nodes

log = logging.getLogger("medanon.nlp")

# ---------------------------------------------------------------------------
# Entity catalogue
# ---------------------------------------------------------------------------

# Healthcare-relevant entity types for HIPAA Safe Harbor + GDPR Art.9(h)
HEALTHCARE_ENTITIES = [
    "PERSON",           # names — NER (cannot be caught by regex alone)
    "DATE_TIME",        # dates mentioned in narrative prose
    "LOCATION",         # addresses / cities from NER context
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "MEDICAL_LICENSE",
    "AGE",
    "NRP",              # Nationality / Religion / Political opinion (GDPR Art.9)
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_BANK_NUMBER",
    "US_ITIN",
    "IP_ADDRESS",
    "URL",
]

# ---------------------------------------------------------------------------
# Presidio engine singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

_ENGINE_LOCK = threading.Lock()
_ANALYZER = None


def _get_analyzer():
    """Return the process-level Presidio AnalyzerEngine, initializing once."""
    global _ANALYZER
    if _ANALYZER is not None:
        return _ANALYZER
    with _ENGINE_LOCK:
        if _ANALYZER is not None:
            return _ANALYZER
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            log.info("Initializing Presidio AnalyzerEngine with en_core_web_lg …")
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
                }
            )
            _ANALYZER = AnalyzerEngine(nlp_engine=provider.create_engine())
            log.info("Presidio ready — %d entity types available", len(HEALTHCARE_ENTITIES))
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Presidio / spaCy.  "
                "Ensure these packages are installed: "
                "presidio-analyzer, spacy, en_core_web_lg.  "
                f"Original error: {exc}"
            ) from exc
    return _ANALYZER


# ---------------------------------------------------------------------------
# Token state
# ---------------------------------------------------------------------------

_GLOBAL_TOKEN_STATE: dict = {"next": {}, "map": {}, "reverse": {}}
_GLOBAL_TOKEN_LOCK = threading.Lock()
_TOKEN_STATE_MAX_ENTRIES = 100_000


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
    """Internal helper for _tokenize — assumes lock is already held if needed."""
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
    """Run Presidio NLP analysis on *text* and replace detected PHI spans."""
    if not text or not text.strip():
        return text

    analyzer = _get_analyzer()
    results = analyzer.analyze(text=text, entities=entities, language=language)

    # Sort descending by start position to replace from right-to-left
    # so that earlier span offsets are not invalidated by replacements.
    hits = sorted(
        [r for r in results if r.score >= threshold],
        key=lambda r: r.start,
        reverse=True,
    )

    for hit in hits:
        span = text[hit.start : hit.end]
        if not span.strip():
            continue
        if mode == "redact":
            replacement = f"[{hit.entity_type}]"
        else:
            replacement = _tokenize(span, hit.entity_type, token_state, token_lock)
        text = text[: hit.start] + replacement + text[hit.end :]

    return text


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
            f" {n}" if v is None else f' {n}="{escape(v)}"'
            for n, v in attrs
        )
        self._parts.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag):
        self._parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        attr_str = "".join(
            f" {n}" if v is None else f' {n}="{escape(v)}"'
            for n, v in attrs
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
    p = _XHTMLTextScrubber(scrub_fn)
    p.feed(xhtml)
    return p.result()


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------

def _resolve_entities(param) -> list:
    if param in (None, "healthcare"):
        return list(HEALTHCARE_ENTITIES)
    if param == "all":
        return list(HEALTHCARE_ENTITIES)
    if isinstance(param, str):
        return [e.strip() for e in param.split(",")]
    return list(param)


# ---------------------------------------------------------------------------
# Public action entry point
# ---------------------------------------------------------------------------

def nlp_detect_by_path(resource: dict, el: dict, params: dict) -> None:
    """Detect and replace PHI/PII using NLP in the field matched by *el*.

    Modifies *resource* in-place (same contract as all other actions).

    When NLP_SERVICE_URL is set, delegates detection to the remote NLP
    microservice. Otherwise runs Presidio locally (default behaviour).
    """
    import os
    entities = _resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    mode = str(params.get("mode", "tokenize"))
    use_html = bool(params.get("html", False))
    # Use per-call state by default to prevent cross-request token contamination.
    # Pass params['_token_state'] = _GLOBAL_TOKEN_STATE explicitly for CLI batch
    # runs that require global_run token consistency.
    token_state = params.get("_token_state") or {"next": {}, "map": {}, "reverse": {}}
    # Use lock when accessing global state for thread safety
    token_lock = _GLOBAL_TOKEN_LOCK if token_state is _GLOBAL_TOKEN_STATE else None

    if os.environ.get("NLP_SERVICE_URL", ""):
        # Remote path — delegate to NLP microservice
        from integrations.nlp.remote_detector import analyze_and_replace_remote

        def scrub_fn(text: str) -> str:
            return analyze_and_replace_remote(
                text, entities, threshold, language, mode, token_state
            )
    else:
        def scrub_fn(text: str) -> str:
            return _analyze_and_replace(text, entities, threshold, language, mode, token_state, token_lock)

    path = el["path"]
    parts = path.split(".")
    if len(parts) < 2:
        return

    key = parts[-1]
    parent_path = parts[1:-1]  # strip the resource-type root segment

    try:
        nodes = find_nodes(resource, parent_path, [])
    except Exception:
        return

    def _apply(node, field):
        if isinstance(node, list):
            for item in node:
                _apply(item, field)
            return
        if not isinstance(node, dict) or field not in node:
            return
        current = node[field]
        if use_html:
            if isinstance(current, dict) and isinstance(current.get("div"), str):
                current["div"] = _scrub_xhtml_text_nodes(current["div"], scrub_fn)
            elif isinstance(current, str):
                node[field] = _scrub_xhtml_text_nodes(current, scrub_fn)
        elif isinstance(current, str):
            node[field] = scrub_fn(current)
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    current[i] = scrub_fn(v)

    _apply(nodes, key)
