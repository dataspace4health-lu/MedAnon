"""Unified action registry for the anonymizer pipeline.

Consolidates all action dispatching — de-identification, pseudonymization,
and de-pseudonymization — into a single pipeline-layer module.  This keeps
the ``action_dispatcher`` free of direct imports from the integrations layer.

NLP detection uses a lazy adapter pattern so the Presidio/spaCy stack is
only imported when needed (and can be swapped for a remote backend).
"""

from __future__ import annotations

import logging
import os
import re
import threading

from utils.fhirpath import not_implemented, find_nodes
from actions.redact import redact_by_path
from actions.cryptohash import cryptohash_by_path
from actions.perturb import perturb_by_path
from actions.substitute import substitute_by_path
from actions.generalize import generalize_by_path
from actions.scrub_text import scrub_text_by_path
from actions.encrypt import encrypt_by_path
from actions.decrypt import decrypt_by_path

_log = logging.getLogger("medanon.actions")

# ---------------------------------------------------------------------------
# NLP adapter — resolved lazily on first call
# ---------------------------------------------------------------------------

_nlp_adapter = None
_NLP_UNAVAILABLE = object()  # sentinel: tried to init and returned no adapter
_nlp_adapter_lock = threading.Lock()
_nlp_adapter_initialised = False  # True once the lock section has run (even if no URL)

# NLP failure mode: "redact" (default, safe fallback) | "raise" (hard fail)
# "skip" was removed — it silently passed unscrubbed PHI through when NLP was unavailable.
_NLP_FAIL_MODE = os.environ.get("MEDANON_NLP_FAIL_MODE", "redact").strip().lower()
if _NLP_FAIL_MODE not in ("redact", "raise"):
    _log.error(
        "Invalid MEDANON_NLP_FAIL_MODE=%r — only 'redact' and 'raise' are supported. "
        "Falling back to 'redact' (safe default).",
        _NLP_FAIL_MODE,
    )
    _NLP_FAIL_MODE = "redact"

# NLP detector helpers — imported lazily once on first use, then cached.
_nlp_resolve_entities = None
_nlp_scrub_xhtml = None


def _ensure_nlp_detector_imports():
    """One-time import of NLP detector helpers (avoids per-call import overhead)."""
    global _nlp_resolve_entities, _nlp_scrub_xhtml
    if _nlp_resolve_entities is not None:
        return
    from integrations.nlp.utils import (
        _resolve_entities as _re,
        _scrub_xhtml_text_nodes as _sx,
    )

    _nlp_resolve_entities = _re
    _nlp_scrub_xhtml = _sx


def _get_nlp_adapter():
    """Return the NLP detector adapter, creating it once on first use.

    Requires ``NLP_SERVICE_URL`` to be set — the NLP engine runs exclusively
    as the NLP microservice (``RemoteNlpAdapter``).  Returns ``None`` if the
    env var is absent — callers must handle this gracefully.

    Thread-safe: the initialisation block runs exactly once under a lock.
    Fast path: after initialisation the lock is never acquired again.
    """
    global _nlp_adapter, _nlp_adapter_initialised
    # Fast path — if _nlp_adapter is already set (by init or by test injection),
    # skip the lock entirely. Using `is not None` rather than the initialised flag
    # means tests can inject a mock by simply assigning the module attribute.
    if _nlp_adapter is not None:
        return None if _nlp_adapter is _NLP_UNAVAILABLE else _nlp_adapter
    with _nlp_adapter_lock:
        # Re-check under the lock: another thread may have initialised first.
        if _nlp_adapter is not None:
            return None if _nlp_adapter is _NLP_UNAVAILABLE else _nlp_adapter
        nlp_url = os.environ.get("NLP_SERVICE_URL", "")
        if nlp_url:
            from integrations.nlp.adapter import RemoteNlpAdapter
            _nlp_adapter = RemoteNlpAdapter()
        else:
            _log.warning(
                "NLP_SERVICE_URL not set — NLP scrubbing unavailable. "
                "Set NLP_SERVICE_URL=http://nlp-lb:8200 to enable."
            )
            _nlp_adapter = _NLP_UNAVAILABLE
        _nlp_adapter_initialised = True
    return None if _nlp_adapter is _NLP_UNAVAILABLE else _nlp_adapter


def nlp_scrub_by_path(resource: dict, el: dict, params: dict) -> None:
    """NLP-based PHI scrubbing — delegates to the configured NLP adapter.

    Replaces detected PHI spans with deterministic tokens (``[[PERSON_1]]``)
    or redaction placeholders (``[PERSON]``), depending on the ``mode`` param.

    Preserves the same (resource, el, params) contract as all other actions.
    Delegates to the NLP microservice via ``RemoteNlpAdapter``; requires
    ``NLP_SERVICE_URL`` to be set.
    """
    _ensure_nlp_detector_imports()

    entities = _nlp_resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    mode = str(params.get("mode", "tokenize"))
    use_html = bool(params.get("html", False))
    token_state = params.get("_token_state") or {"next": {}, "map": {}, "reverse": {}}

    adapter = _get_nlp_adapter()
    if adapter is None:
        if _NLP_FAIL_MODE == "raise":
            raise RuntimeError(f"NLP adapter unavailable — cannot scrub {el['path']}")
        _log.error("nlp_unavailable path=%s — redacting as safety fallback", el["path"])
        redact_by_path(resource, el, params.get("redact_params", {}))
        return

    def scrub_fn(text: str) -> str:
        return adapter.analyze_and_replace(
            text, entities, threshold, language, mode, token_state
        )

    path = el["path"]
    parts = path.split(".")
    if len(parts) < 2:
        return

    key = parts[-1]
    parent_path = parts[1:-1]

    try:
        nodes = find_nodes(resource, parent_path, [])
    except Exception:
        _log.error(
            "nlp_find_nodes_failed path=%s — redacting field as safety fallback", path
        )
        redact_by_path(resource, el, {})
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
                try:
                    current["div"] = _nlp_scrub_xhtml(current["div"], scrub_fn)
                except Exception:
                    _log.error(
                        "nlp_scrub_failed path=%s.%s — redacting field", path, field
                    )
                    current["div"] = "[REDACTED]"
            elif isinstance(current, str):
                try:
                    node[field] = _nlp_scrub_xhtml(current, scrub_fn)
                except Exception:
                    _log.error(
                        "nlp_scrub_failed path=%s.%s — redacting field", path, field
                    )
                    node[field] = "[REDACTED]"
        elif isinstance(current, str):
            try:
                node[field] = scrub_fn(current)
            except Exception:
                _log.error("nlp_scrub_failed path=%s.%s — redacting field", path, field)
                node[field] = "[REDACTED]"
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    try:
                        current[i] = scrub_fn(v)
                    except Exception:
                        _log.error(
                            "nlp_scrub_failed path=%s.%s[%d] — redacting field",
                            path,
                            field,
                            i,
                        )
                        current[i] = "[REDACTED]"

    _apply(nodes, key)


# ---------------------------------------------------------------------------
# NLP conditional action — detect first, act per entity type
# ---------------------------------------------------------------------------

_DEFAULT_ENTITY_ACTIONS: dict[str, str] = {
    # --- Presidio built-in entities ---
    "PERSON": "redact",
    "DATE_TIME": "generalize",
    "AGE": "generalize",
    "LOCATION": "redact",
    "PHONE_NUMBER": "redact",
    "EMAIL_ADDRESS": "redact",
    "US_SSN": "redact",
    "US_PASSPORT": "redact",
    "US_DRIVER_LICENSE": "redact",
    "MEDICAL_LICENSE": "redact",
    "NRP": "redact",
    "CREDIT_CARD": "redact",
    "IBAN_CODE": "redact",
    "US_BANK_NUMBER": "redact",
    "US_ITIN": "redact",
    "IP_ADDRESS": "redact",
    "URL": "keep",
    # --- Custom recognizer entities ---
    "STREET_ADDRESS": "redact",
    "INTL_PHONE": "redact",
    "FAX_NUMBER": "redact",
    # EU national IDs
    "UK_NINO": "redact",
    "FR_NIR": "redact",
    "DE_SVNR": "redact",
    "NL_BSN": "redact",
    "IT_CF": "redact",
    "ES_DNI": "redact",
    "CH_AHV": "redact",
    "BE_NN": "redact",
    # Healthcare IDs
    "MRN": "redact",
    "UK_NHS": "redact",
    "DE_KVNR": "redact",
    "EU_EHIC": "redact",
    # Postcodes
    "UK_POSTCODE": "redact",
    "EU_POSTCODE": "redact",
    # Labeled markers
    "DOB_MARKER": "generalize",
    "AGE_MARKER": "generalize",
    "NATIONAL_ID_LABEL": "redact",
    "ACCOUNT_LABEL": "redact",
    "LICENSE_PLATE": "redact",
    # GDPR Art.9
    "NATIONALITY_LABEL": "redact",
    "RELIGION_LABEL": "redact",
    "POLITICAL_LABEL": "redact",
    "ETHNICITY_LABEL": "redact",
    # GDPR Art.9 / HIPAA — bare inline demographic (no label prefix required)
    "GENDER": "redact",
    "RACE_ETHNICITY": "redact",
    # EU dates
    "EU_DATE": "generalize",
    "EU_DATE_WRITTEN": "generalize",
    # Synthetic artifacts
    "SYNTHEA_SEED": "redact",
    # Clinical note PHI — inline administrative / geographic
    "INSURANCE_STATUS": "redact",
    "GEO_COORDINATES": "redact",
    # Healthcare / payer organisations
    "ORGANIZATION": "redact",
    # Financial / fiscal identifiers
    "SWIFT_BIC": "redact",
    "EU_VAT": "redact",
    "US_ROUTING": "redact",
    "CRYPTO_WALLET": "redact",
    # HIPAA Safe Harbor extra identifiers
    "US_DEA": "redact",
    "US_NPI": "redact",
    "MAC_ADDRESS": "redact",
    "IMEI": "redact",
    "VIN": "redact",
    "FDA_UDI": "redact",
    "UUID": "redact",
    "BEARER_TOKEN": "redact",
    "USERNAME_HANDLE": "redact",
    # GDPR Art.9 — genetic data
    "GENETIC_VARIANT": "redact",
}

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_AGE_RE = re.compile(r"\b(\d+)\b")


# Priority order for resolving overlapping NLP spans.  Higher number wins when
# two detected spans share characters.  Specific ID/contact patterns outrank
# generic NER labels (PERSON / LOCATION / DATE_TIME) so that a credit-card
# number tagged simultaneously as ACCOUNT_LABEL and DRIVER_LICENSE collapses
# to the most specific tag instead of being double-replaced.
_ENTITY_PRIORITY: dict[str, int] = {
    "DRIVER_LICENSE": 90,
    "US_SSN": 90,
    "US_PASSPORT": 90,
    "EU_PASSPORT": 90,
    "EU_NATIONAL_ID": 90,
    "IBAN": 90,
    "IBAN_CODE": 90,
    "CREDIT_CARD": 90,
    "BEARER_TOKEN": 92,
    "GENETIC_VARIANT": 90,
    "VIN": 90,
    "IMEI": 90,
    "FDA_UDI": 90,
    "US_DEA": 90,
    "US_NPI": 90,
    "US_ROUTING": 90,
    "MAC_ADDRESS": 88,
    "UUID": 88,
    "CRYPTO_WALLET": 88,
    "SWIFT_BIC": 88,
    "EU_VAT": 88,
    "USERNAME_HANDLE": 70,
    "MRN": 85,
    "MEDICAL_LICENSE": 85,
    "EMAIL_ADDRESS": 85,
    "INTL_PHONE": 85,
    "PHONE_NUMBER": 80,
    "IP_ADDRESS": 80,
    "ACCOUNT_LABEL": 60,
    "NATIONAL_ID_LABEL": 60,
    "DOB_MARKER": 55,
    "EU_DATE": 55,
    "EU_DATE_WRITTEN": 55,
    "DATE_TIME": 50,
    "PERSON": 40,
    "LOCATION": 35,
    "ORGANIZATION": 35,
    "AGE": 30,
}


def _merge_overlapping_spans(
    hits: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Resolve overlapping detected spans into a non-overlapping ordered list.

    NLP detectors (Presidio + custom recognizers) frequently emit overlapping
    spans for the same characters (e.g. ``"policy number FR-AXA-99887766"``
    is tagged as both ``ACCOUNT_LABEL`` and ``DRIVER_LICENSE``).  Replacing
    overlapping spans in reverse-start order corrupts the text — the longer
    span's recorded ``end`` no longer matches the shifted string after the
    inner span has been substituted, leaving fragments like
    ``[ACCOUNT_LABEL]ER_LICENSE]``.

    Strategy:
      1. Sort hits by ``(start, -length)`` so larger spans appear first at
         each start position.
      2. Walk the list keeping the earliest non-overlapping span.  When a new
         span overlaps the previously kept one, pick the winner via
         ``_ENTITY_PRIORITY`` (higher wins, ties broken by longer span).
      3. Return spans sorted by descending start so the caller's existing
         right-to-left replacement loop is correct.
    """
    if not hits:
        return []

    # Drop zero-width spans
    cleaned = [(s, e, t) for (s, e, t) in hits if e > s]
    if not cleaned:
        return []

    cleaned.sort(key=lambda h: (h[0], -(h[1] - h[0])))

    accepted: list[tuple[int, int, str]] = []
    for s, e, t in cleaned:
        if not accepted:
            accepted.append((s, e, t))
            continue
        last_s, last_e, last_t = accepted[-1]
        if s >= last_e:
            # No overlap — accept
            accepted.append((s, e, t))
            continue
        # Overlap — keep the higher-priority entity, ties broken by longer span
        prio_new = _ENTITY_PRIORITY.get(t, 50)
        prio_old = _ENTITY_PRIORITY.get(last_t, 50)
        len_new = e - s
        len_old = last_e - last_s
        if (prio_new, len_new) > (prio_old, len_old):
            accepted[-1] = (s, e, t)
        # Otherwise drop the new span

    accepted.sort(key=lambda h: h[0], reverse=True)
    return accepted


def _replace_span(
    text: str, start: int, end: int, entity_type: str,
    entity_action: str, token_state: dict,
) -> str:
    """Replace a single detected span using the entity-specific strategy."""
    span_text = text[start:end]
    if entity_action == "keep":
        return text
    if entity_action == "redact":
        replacement = f"[{entity_type}]"
    elif entity_action == "generalize":
        if entity_type in ("DATE_TIME", "DOB_MARKER", "EU_DATE", "EU_DATE_WRITTEN"):
            m = _YEAR_RE.search(span_text)
            replacement = m.group(1) if m else "[DATE]"
            # Date detectors often over-extend the span into the next word
            # (e.g. "12 April 1974, residin" or "18 April 2026. Follow").
            # If the span ends mid-word, walk left so we don't leave the
            # tail (e.g. "g" or "up") glued to the year.
            while end < len(text) and text[end].isalnum():
                end += 1
        elif entity_type == "AGE":
            m = _AGE_RE.search(span_text)
            if m:
                age = int(m.group(1))
                decade_lo = (age // 10) * 10
                replacement = f"{decade_lo}-{decade_lo + 9}"
            else:
                replacement = "[AGE]"
        else:
            replacement = f"[{entity_type}]"
    else:
        # tokenize (default)
        from integrations.nlp.utils import _tokenize
        replacement = _tokenize(span_text, entity_type, token_state)
    return text[:start] + replacement + text[end:]


def nlp_detect_act_by_path(resource: dict, el: dict, params: dict) -> None:
    """NLP-conditional action: detect PII first, apply entity-specific actions.

    Unlike ``nlp_scrub`` which always tokenizes all detected spans uniformly,
    this action maps each detected entity type to a configurable replacement
    strategy (redact, generalize, tokenize, keep).  When no entities are
    detected, the text is left unchanged and ``params["_no_change"]`` is set
    to signal the dispatcher to skip the manifest entry.

    Non-HTML fields are processed with a single ``detect_batch`` call that
    covers all matching text elements, reducing HTTP round-trips to the NLP
    microservice from N (one per element) to 1.  HTML/XHTML fields use a
    serial per-element path because XHTML scrubbing operates on a rendered
    tree that cannot be trivially batched.
    """
    _ensure_nlp_detector_imports()

    entities = _nlp_resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    use_html = bool(params.get("html", False))
    entity_actions = {**_DEFAULT_ENTITY_ACTIONS, **(params.get("entity_actions") or {})}
    default_action = entity_actions.get("default", "tokenize")
    token_state = params.get("_token_state") or {"next": {}, "map": {}, "reverse": {}}

    adapter = _get_nlp_adapter()
    if adapter is None:
        if _NLP_FAIL_MODE == "raise":
            raise RuntimeError(f"NLP adapter unavailable — cannot detect {el['path']}")
        _log.error("nlp_unavailable path=%s — redacting as safety fallback", el["path"])
        redact_by_path(resource, el, {})
        params["_actual_action"] = "nlp_detect_act/redact"
        return

    # Navigate to the field
    path = el["path"]
    parts = path.split(".")
    if len(parts) < 2:
        params["_no_change"] = True
        return

    key = parts[-1]
    parent_path = parts[1:-1]

    try:
        nodes = find_nodes(resource, parent_path, [])
    except Exception:
        _log.error(
            "nlp_detect_act_find_nodes_failed path=%s — redacting as safety fallback", path
        )
        redact_by_path(resource, el, {})
        params["_actual_action"] = "nlp_detect_act/redact"
        return

    changed = False

    # ------------------------------------------------------------------
    # HTML / XHTML path — serial, one detect call per xhtml node
    # ------------------------------------------------------------------
    if use_html:
        def detect_and_replace_html(text: str) -> str:
            if not text or not text.strip():
                return text
            hits = adapter.detect(text, entities, threshold, language)
            if not hits:
                return text
            sorted_hits = _merge_overlapping_spans(hits)
            for start, end, entity_type in sorted_hits:
                if not text[start:end].strip():
                    continue
                ea = entity_actions.get(entity_type, default_action)
                text = _replace_span(text, start, end, entity_type, ea, token_state)
            return text

        def _apply_html(node, field):
            nonlocal changed
            if isinstance(node, list):
                for item in node:
                    _apply_html(item, field)
                return
            if not isinstance(node, dict) or field not in node:
                return
            current = node[field]
            if isinstance(current, dict) and isinstance(current.get("div"), str):
                try:
                    result = _nlp_scrub_xhtml(current["div"], detect_and_replace_html)
                    if result != current["div"]:
                        current["div"] = result
                        changed = True
                except Exception:
                    _log.error("nlp_detect_act_failed path=%s — redacting", path)
                    current["div"] = "[REDACTED]"
                    changed = True
            elif isinstance(current, str):
                try:
                    result = _nlp_scrub_xhtml(current, detect_and_replace_html)
                    if result != current:
                        node[field] = result
                        changed = True
                except Exception:
                    _log.error("nlp_detect_act_failed path=%s — redacting", path)
                    node[field] = "[REDACTED]"
                    changed = True

        _apply_html(nodes, key)
        if not changed:
            params["_no_change"] = True
        else:
            params["_actual_action"] = "nlp_detect_act"
        return

    # ------------------------------------------------------------------
    # Plain-text path — two-phase: collect all strings, detect in one
    # batch call, then apply per-entity replacements.
    # ------------------------------------------------------------------

    # Phase 1: collect all (container, key_or_index, text) entries
    _entries: list[tuple] = []

    def _collect(node, field):
        if isinstance(node, list):
            for item in node:
                _collect(item, field)
            return
        if not isinstance(node, dict) or field not in node:
            return
        current = node[field]
        if isinstance(current, str) and current.strip():
            _entries.append((node, field, current))
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str) and v.strip():
                    _entries.append((current, i, v))

    _collect(nodes, key)

    if not _entries:
        params["_no_change"] = True
        return

    # Phase 2: single batch HTTP call to NLP microservice
    texts = [e[2] for e in _entries]
    try:
        if len(texts) == 1:
            batch_hits = [adapter.detect(texts[0], entities, threshold, language)]
        else:
            batch_hits = adapter.detect_batch(texts, entities, threshold, language)
    except Exception:
        _log.error(
            "nlp_detect_act_batch_failed path=%s — redacting as safety fallback", path
        )
        redact_by_path(resource, el, {})
        params["_actual_action"] = "nlp_detect_act/redact"
        return

    # Phase 3: apply per-entity replacements using returned hit lists
    for (container, key_or_idx, orig_text), hits in zip(_entries, batch_hits):
        if not hits:
            continue
        text = orig_text
        sorted_hits = _merge_overlapping_spans(hits)
        for start, end, entity_type in sorted_hits:
            if not text[start:end].strip():
                continue
            ea = entity_actions.get(entity_type, default_action)
            text = _replace_span(text, start, end, entity_type, ea, token_state)
        if text != orig_text:
            container[key_or_idx] = text
            changed = True

    if not changed:
        params["_no_change"] = True
    else:
        params["_actual_action"] = "nlp_detect_act"


# ---------------------------------------------------------------------------
# De-identification actions (pure transformations + NLP)
# ---------------------------------------------------------------------------

deident_actions = {
    "redact": redact_by_path,
    "perturb": perturb_by_path,
    "cryptohash": cryptohash_by_path,
    "substitute": substitute_by_path,
    "generalize": generalize_by_path,
    "scrub_text": scrub_text_by_path,
    "nlp_scrub": nlp_scrub_by_path,
    "nlp_detect": nlp_scrub_by_path,  # backward-compat alias
    "nlp_detect_act": nlp_detect_act_by_path,
}

# ---------------------------------------------------------------------------
# Pseudonymization actions (non-gPAS — pure crypto transforms)
# ---------------------------------------------------------------------------

pseudo_actions = {
    "gpas_pseudonymize": None,  # sentinel — handled by batch Pass 2
    "encrypt": encrypt_by_path,
}

# ---------------------------------------------------------------------------
# De-pseudonymization actions
# ---------------------------------------------------------------------------

depseudo_actions = {
    "gpas_depseudonymize": None,  # sentinel — requires gPAS infrastructure
    "decrypt": decrypt_by_path,
}

# Backward-compatible alias
actions = deident_actions


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------


def perform_deidentification(action, resource, el, params):
    if action in deident_actions:
        deident_actions[action](resource, el, params)
    else:
        not_implemented(f"Method {action} is not implemented")
    return resource


def perform_pseudonymization(action, resource, el, params):
    handler = pseudo_actions.get(action)
    if handler is None and action in pseudo_actions:
        # Sentinel action (gpas_pseudonymize) — should be deferred to batch pass
        not_implemented(
            f"Action {action} must be dispatched via batch Pass 2, not per-element"
        )
    elif handler:
        handler(resource, el, params)
    else:
        not_implemented(f"Method {action} is not implemented")
    return resource


def perform_depseudonymization(action, resource, el, params):
    handler = depseudo_actions.get(action)
    if handler is None and action in depseudo_actions:
        # Sentinel — need gPAS infrastructure loaded via dispatcher
        _load_gpas_depseudo(action, resource, el, params)
    elif handler:
        handler(resource, el, params)
    else:
        not_implemented(f"Method {action} is not implemented")
    return resource


_depseudo_lock = threading.Lock()


def _load_gpas_depseudo(action, resource, el, params):
    """Lazy-load gPAS de-pseudonymization for the rare depseudo path."""
    with _depseudo_lock:
        # Double-check after acquiring the lock
        handler = depseudo_actions.get("gpas_depseudonymize")
        if handler is not None:
            handler(resource, el, params)
            return
        from integrations.gpas.client import gpas_depseudonymize_by_path

        depseudo_actions["gpas_depseudonymize"] = gpas_depseudonymize_by_path
    gpas_depseudonymize_by_path(resource, el, params)
