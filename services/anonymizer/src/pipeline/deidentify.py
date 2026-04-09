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
                adapter.analyze_and_replace("test", ["PERSON"], 0.4, "en", "tokenize", {"next": {}, "map": {}, "reverse": {}})
                _nlp_adapter = adapter
            except Exception as exc:
                _log.warning("NLP adapter unavailable (Presidio/spaCy not installed): %s", exc)
                _nlp_adapter = _NLP_UNAVAILABLE
                return None
    return _nlp_adapter


def nlp_detect_by_path(resource: dict, el: dict, params: dict) -> None:
    """NLP-based PHI detection — delegates to the configured NLP adapter.

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
        _log.error("nlp_find_nodes_failed path=%s — redacting field as safety fallback", path)
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
                current["div"] = _nlp_scrub_xhtml(current["div"], scrub_fn)
            elif isinstance(current, str):
                node[field] = _nlp_scrub_xhtml(current, scrub_fn)
        elif isinstance(current, str):
            node[field] = scrub_fn(current)
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    current[i] = scrub_fn(v)

    _apply(nodes, key)


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
    "nlp_detect": nlp_detect_by_path,
}

# ---------------------------------------------------------------------------
# Pseudonymization actions (non-gPAS — pure crypto transforms)
# ---------------------------------------------------------------------------

pseudo_actions = {
    "gpas_pseudonymize": None,   # sentinel — handled by batch Pass 2
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
        not_implemented(f'Method {action} is not implemented')
    return resource


def perform_pseudonymization(action, resource, el, params):
    handler = pseudo_actions.get(action)
    if handler is None and action in pseudo_actions:
        # Sentinel action (gpas_pseudonymize) — should be deferred to batch pass
        not_implemented(
            f'Action {action} must be dispatched via batch Pass 2, not per-element'
        )
    elif handler:
        handler(resource, el, params)
    else:
        not_implemented(f'Method {action} is not implemented')
    return resource


def perform_depseudonymization(action, resource, el, params):
    handler = depseudo_actions.get(action)
    if handler is None and action in depseudo_actions:
        # Sentinel — need gPAS infrastructure loaded via dispatcher
        _load_gpas_depseudo(action, resource, el, params)
    elif handler:
        handler(resource, el, params)
    else:
        not_implemented(f'Method {action} is not implemented')
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
