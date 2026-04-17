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
_NLP_UNAVAILABLE = object()  # sentinel: tried to init and failed
_nlp_adapter_lock = threading.Lock()

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
    from integrations.nlp.detector import (
        _resolve_entities as _re,
        _scrub_xhtml_text_nodes as _sx,
    )

    _nlp_resolve_entities = _re
    _nlp_scrub_xhtml = _sx


def _get_nlp_adapter():
    """Return the NLP detector adapter, creating it once on first use.

    Uses ``RemoteNlpAdapter`` when ``NLP_SERVICE_URL`` is set, otherwise
    ``LocalPresidioAdapter``.  Returns ``None`` when neither Presidio nor a
    remote NLP service is available — callers must handle this gracefully.
    """
    global _nlp_adapter
    if _nlp_adapter is _NLP_UNAVAILABLE:
        return None
    if _nlp_adapter is not None:
        return _nlp_adapter
    with _nlp_adapter_lock:
        # Double-check after acquiring the lock
        if _nlp_adapter is _NLP_UNAVAILABLE:
            return None
        if _nlp_adapter is not None:
            return _nlp_adapter
        if os.environ.get("NLP_SERVICE_URL", ""):
            from integrations.nlp.adapter import RemoteNlpAdapter

            _nlp_adapter = RemoteNlpAdapter()
        else:
            try:
                from integrations.nlp.adapter import LocalPresidioAdapter

                adapter = LocalPresidioAdapter()
                # Trigger lazy Presidio init to fail fast
                adapter.analyze_and_replace(
                    "test",
                    ["PERSON"],
                    0.4,
                    "en",
                    "tokenize",
                    {"next": {}, "map": {}, "reverse": {}},
                )
                _nlp_adapter = adapter
            except Exception as exc:
                _log.warning(
                    "NLP adapter unavailable (Presidio/spaCy not installed): %s", exc
                )
                _nlp_adapter = _NLP_UNAVAILABLE
                return None
    return _nlp_adapter


def nlp_scrub_by_path(resource: dict, el: dict, params: dict) -> None:
    """NLP-based PHI scrubbing — delegates to the configured NLP adapter.

    Replaces detected PHI spans with deterministic tokens (``[[PERSON_1]]``)
    or redaction placeholders (``[PERSON]``), depending on the ``mode`` param.

    Preserves the same (resource, el, params) contract as all other actions.
    The adapter selection (local Presidio vs. remote HTTP) is resolved on first call.
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
    # EU dates
    "EU_DATE": "generalize",
    "EU_DATE_WRITTEN": "generalize",
    # Synthetic artifacts
    "SYNTHEA_SEED": "redact",
}

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_AGE_RE = re.compile(r"\b(\d+)\b")


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
        elif entity_type in ("AGE", "AGE_MARKER"):
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
        from integrations.nlp.detector import _tokenize
        replacement = _tokenize(span_text, entity_type, token_state)
    return text[:start] + replacement + text[end:]


def nlp_detect_act_by_path(resource: dict, el: dict, params: dict) -> None:
    """NLP-conditional action: detect PII first, apply entity-specific actions.

    Unlike ``nlp_scrub`` which always tokenizes all detected spans uniformly,
    this action maps each detected entity type to a configurable replacement
    strategy (redact, generalize, tokenize, keep).  When no entities are
    detected, the text is left unchanged and ``params["_no_change"]`` is set
    to signal the dispatcher to skip the manifest entry.
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

    def detect_and_replace(text: str) -> str:
        """Run detection on text, apply per-entity replacements.

        Spans are replaced right-to-left (descending by start position) so that
        each replacement only shifts characters to the right of all remaining
        spans, keeping their original (start, end) positions valid.
        """
        if not text or not text.strip():
            return text
        hits = adapter.detect(text, entities, threshold, language)
        if not hits:
            return text
        # Guarantee descending order regardless of adapter implementation.
        sorted_hits = sorted(hits, key=lambda h: h[0], reverse=True)
        for start, end, entity_type in sorted_hits:
            if not text[start:end].strip():
                continue
            ea = entity_actions.get(entity_type, default_action)
            text = _replace_span(text, start, end, entity_type, ea, token_state)
        return text

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

    def _apply(node, field):
        nonlocal changed
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
                    result = _nlp_scrub_xhtml(current["div"], detect_and_replace)
                    if result != current["div"]:
                        current["div"] = result
                        changed = True
                except Exception:
                    _log.error("nlp_detect_act_failed path=%s — redacting", path)
                    current["div"] = "[REDACTED]"
                    changed = True
            elif isinstance(current, str):
                try:
                    result = _nlp_scrub_xhtml(current, detect_and_replace)
                    if result != current:
                        node[field] = result
                        changed = True
                except Exception:
                    _log.error("nlp_detect_act_failed path=%s — redacting", path)
                    node[field] = "[REDACTED]"
                    changed = True
        elif isinstance(current, str):
            try:
                result = detect_and_replace(current)
                if result != current:
                    node[field] = result
                    changed = True
            except Exception:
                _log.error("nlp_detect_act_failed path=%s — redacting", path)
                node[field] = "[REDACTED]"
                changed = True
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    try:
                        result = detect_and_replace(v)
                        if result != v:
                            current[i] = result
                            changed = True
                    except Exception:
                        _log.error("nlp_detect_act_failed path=%s[%d] — redacting", path, i)
                        current[i] = "[REDACTED]"
                        changed = True

    _apply(nodes, key)

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
