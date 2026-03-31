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

from utils.fhirpath import not_implemented
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


def _get_nlp_adapter():
    """Return the NLP detector adapter, creating it once on first use.

    Uses ``RemoteNlpAdapter`` when ``NLP_SERVICE_URL`` is set, otherwise
    ``LocalPresidioAdapter``.
    """
    global _nlp_adapter
    if _nlp_adapter is not None:
        return _nlp_adapter
    if os.environ.get("NLP_SERVICE_URL", ""):
        from integrations.nlp.adapter import RemoteNlpAdapter
        _nlp_adapter = RemoteNlpAdapter()
    else:
        from integrations.nlp.adapter import LocalPresidioAdapter
        _nlp_adapter = LocalPresidioAdapter()
    return _nlp_adapter


def nlp_detect_by_path(resource: dict, el: dict, params: dict) -> None:
    """NLP-based PHI detection — delegates to the configured NLP adapter.

    Preserves the same (resource, el, params) contract as all other actions.
    The adapter selection (local Presidio vs. remote HTTP) is resolved on first call.
    """
    from integrations.nlp.detector import (
        _resolve_entities,
        _scrub_xhtml_text_nodes,
        find_nodes,
    )

    entities = _resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    mode = str(params.get("mode", "tokenize"))
    use_html = bool(params.get("html", False))
    token_state = params.get("_token_state") or {"next": {}, "map": {}, "reverse": {}}

    adapter = _get_nlp_adapter()

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


def _load_gpas_depseudo(action, resource, el, params):
    """Lazy-load gPAS de-pseudonymization for the rare depseudo path."""
    from integrations.gpas.client import gpas_depseudonymize_by_path
    depseudo_actions["gpas_depseudonymize"] = gpas_depseudonymize_by_path
    gpas_depseudonymize_by_path(resource, el, params)
