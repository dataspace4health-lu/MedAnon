"""Name scrubbing and HTML narrative redaction for FHIR text fields.

PII detection via regex patterns has been consolidated into Presidio custom
recognizers (see ``integrations/nlp/detector.py``).  This module now provides:

    1. **Name extraction and scrubbing**  extract patient/practitioner names
       from ``resource.name`` and redact literal occurrences in text fields.

    2. **HTML narrative modes**  ``html`` (replace entire div with safe
       placeholder) and ``html_tokenize`` (scrub text nodes, preserve markup).

    3. **Token state management**  deterministic ``[[TYPE_N]]`` surrogate
       tokens with configurable scope (resource, bundle, global_run).

Supported ``mode`` values (set via ``params['mode']``):

    text  (default)  scrub names in plain-text fields in-place.

    html_tokenize    scrub names in XHTML text nodes while preserving
                      markup and attributes.

    html             replace the entire XHTML field with a single safe
                    FHIR-compliant ``<div>`` placeholder.

Optional params:

  names          list of name strings to redact (case-insensitive
                  literal match, e.g. ``["Smith", "John"]``).
  extract_names  if ``true``, automatically extract names from the
                  resource's own ``name`` field (FHIR Patient / Practitioner).
  placeholders   dict overriding default replacement tokens.
  mapping_scope  ``resource`` (default), ``bundle`` or ``global_run``
                  controls token consistency scope.
"""

from __future__ import annotations

import re
import threading
from copy import deepcopy
from integrations.nlp.utils import _scrub_xhtml_text_nodes, _tokenize
from utils.fhirpath import find_nodes

# ---------------------------------------------------------------------------
# Regex patterns  most PII detection is consolidated into Presidio custom
# recognizers in integrations/nlp/detector.py.  Patterns kept here are used
# by scrub_text_by_path() when the Presidio stack is unavailable or not
# configured (e.g. plain scrub_text rules without an NLP service).
#
# street_address  combined US + EU + PO Box pattern derived from the Presidio
#   custom recognizers in detector.py.  Kept here so that scrub_text rules
#   with ``patterns: street_address`` fire on any valueString / free-text
#   field regardless of LOINC code or FHIR resource structure.
# ---------------------------------------------------------------------------

_STREET_ADDRESS_RE = re.compile(
    r"(?:"
    # US street address: number + street name + type + optional apt/suite/city/state/zip
    r"\b\d{1,6}\s+"
    r"(?:[A-Z][a-z''\-]*\.?\s+){1,4}"
    r"(?:Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Road|Rd\.?|Lane|Ln\.?|Way|Court|Ct\.?|Place|Pl\.?|Circle|Cir\.?|"
    r"Trail|Trl\.?|Terrace|Ter\.?|Parkway|Pkwy\.?|Highway|Hwy\.?|"
    r"Alley|Approach|Bay|Brook|Burg|Bypass|Byway|"
    r"Causeway|Center|Common|Commons|Corner|Corners|Course|"
    r"Cove|Creek|Crossing|Dam|Divide|Estate|Estates|"
    r"Expressway|Extension|Falls|Ferry|Field|Fields|Flat|Flats|"
    r"Ford|Forge|Fork|Forks|Freeway|Garden|Gardens|Gateway|Glen|"
    r"Green|Grove|Harbor|Haven|Heights|Hollow|"
    r"Isle|Junction|Key|Knoll|Lake|Landing|Light|Loaf|Lock|Lodge|Loop|"
    r"Mall|Manor|Meadow|Meadows|Mill|Mission|Mount|Neck|"
    r"Orchard|Oval|Overpass|Parade|Park|Pass|Path|Pike|Pine|"
    r"Plain|Plains|Plaza|Point|Port|Prairie|Promenade|"
    r"Ramp|Ranch|Rapid|Rapids|Rest|Ridge|River|Route|Row|Run|"
    r"Shore|Spring|Springs|Spur|Square|Station|Stravenue|Stream|Summit|"
    r"Trace|Track|Trafficway|Tunnel|Turnpike|Union|Valley|Viaduct|View|Village|"
    r"Vista|Walk|Well|Wells)"
    r"(?:\s+(?:Apt|Apartment|Unit|Suite|Ste|Bldg|Building|Floor|Fl|Rm|Room|#)\.?\s*\d{1,5})?"
    r"(?:[,\s]+(?:[A-Z][a-z''\-]+\s*)+)?"
    r"(?:[,\s]+[A-Z]{2})?"
    r"(?:[,\s]+\d{5}(?:-\d{4})?)?"
    r"|"
    # German: Hauptstraße 12, Marktplatz 3a
    r"\b(?:[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|str\.?|gasse|weg|platz|allee|ring|damm|ufer))"
    r"\s+\d{1,5}(?:\s?[a-zA-Z])?"
    r"|"
    # French: Rue de la Paix 10
    r"\b(?:(?:Rue|Avenue|Boulevard|Bd\.?|Chemin|Place|Allée|Impasse|Passage|Quai)"
    r"(?:\s+(?:de|du|des|la|le|l')?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
    r"\s+\d{1,5}"
    r"|"
    # Italian: Via Garibaldi 5
    r"\b(?:(?:Via|Viale|Piazza|Corso|Largo|Vicolo)"
    r"(?:\s+(?:dei?|del|della|delle|degli)?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
    r"\s*,?\s*\d{1,5}"
    r"|"
    # Spanish: Calle Mayor 7
    r"\b(?:(?:Calle|Avenida|Avda\.?|Paseo|Plaza|Carrera|Camino)"
    r"(?:\s+(?:de|del|la|las|los)?\s*[A-ZÀ-Ü][a-zà-ü]+){1,4})"
    r"\s*,?\s*\d{1,5}"
    r"|"
    # Dutch: Kalverstraat 12
    r"\b(?:[A-Z][a-z]+(?:straat|laan|weg|gracht|plein|singel|kade|dijk))"
    r"\s+\d{1,5}(?:\s?[a-zA-Z])?"
    r"|"
    # PO Box (all languages)
    r"\b(?:P\.?O\.?\s*Box|Postfach|Boîte\s+Postale|BP|Casella\s+Postale|CP|"
    r"Apartado(?:\s+de\s+Correos)?|Postbus)\s*[:#]?\s*\d{1,10}\b"
    r")",
    re.IGNORECASE,
)

_PATTERNS: dict = {
    "street_address": (_STREET_ADDRESS_RE, "[ADDRESS]"),
}

_ALL_PATTERNS: list = list(_PATTERNS)

# FHIR text.div must be valid XHTML with the FHIR namespace on the root element.
_REDACTED_DIV = (
    '<div xmlns="http://www.w3.org/1999/xhtml">'
    "This resource has been de-identified."
    "</div>"
)

_GLOBAL_TOKEN_STATE = {"next": {}, "map": {}, "reverse": {}}
_GLOBAL_TOKEN_LOCK = threading.Lock()


def reset_global_token_state():
    """Clear the global token state. Call between batch runs to prevent unbounded growth."""
    with _GLOBAL_TOKEN_LOCK:
        _GLOBAL_TOKEN_STATE["next"].clear()
        _GLOBAL_TOKEN_STATE["map"].clear()
        _GLOBAL_TOKEN_STATE["reverse"].clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_patterns(patterns_param):
    """Return a list of pattern keys to apply."""
    if patterns_param == "all":
        return _ALL_PATTERNS
    if isinstance(patterns_param, str):
        keys = [p.strip() for p in patterns_param.split(",")]
    else:
        keys = list(patterns_param)
    unknown = set(keys) - set(_PATTERNS)
    if unknown:
        raise ValueError(
            f"Unknown scrub_text pattern(s): {sorted(unknown)}. "
            f"Supported: {', '.join(sorted(_PATTERNS))}. "
            f"NOTE: SSN, phone, email, IP, MRN, date, URL, and zip patterns were "
            f"moved to Presidio custom recognizers in medanon v2. Replace "
            f"'action: scrub_text' with 'action: nlp_scrub' or 'action: nlp_detect_act' "
            f"to use the consolidated NLP detection pipeline."
        )
    return keys


def _token_prefix_for_pattern(pattern_key):
    return pattern_key.upper()


def _scrub(
    text,
    pattern_keys,
    names,
    placeholder_overrides,
    tokenize,
    token_state,
    token_lock=None,
):
    """Apply PHI regex patterns and name tokenization/redaction to *text*."""
    for key in pattern_keys:
        compiled, default_ph = _PATTERNS[key]
        if tokenize:
            token_prefix = _token_prefix_for_pattern(key)

            def repl(match, lock=token_lock):
                return _tokenize(match.group(0), token_prefix, token_state, lock)

            text = compiled.sub(repl, text)
        else:
            ph = placeholder_overrides.get(key, default_ph)
            text = compiled.sub(ph, text)

    # Name scrubbing  case-insensitive literal match on each token
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        if tokenize:
            regex = re.compile(re.escape(name), re.IGNORECASE)

            def repl_name(match, lock=token_lock):
                return _tokenize(match.group(0), "NAME", token_state, lock)

            text = regex.sub(repl_name, text)
        else:
            text = re.sub(re.escape(name), "[NAME]", text, flags=re.IGNORECASE)

    return text


def _extract_names_from_resource(resource):
    """Return a flat list of given/family name strings from a FHIR resource."""
    names = []
    for entry in resource.get("name", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("family"):
            names.append(entry["family"])
        for given in entry.get("given", []):
            if given:
                names.append(given)
    return names


# ---------------------------------------------------------------------------
# Node walker
# ---------------------------------------------------------------------------


def _apply_to_node(node, key, scrub_fn, mode):
    """Walk the found node(s) and apply *scrub_fn* or HTML redaction."""
    if isinstance(node, list):
        for item in node:
            _apply_to_node(item, key, scrub_fn, mode)
        return

    if not isinstance(node, dict) or key not in node:
        return

    if mode == "html":
        # Replace the entire XHTML narrative with a safe placeholder.
        # FHIR Narrative is usually an object at *.text with a nested div.
        if isinstance(node[key], dict) and isinstance(node[key].get("div"), str):
            node[key]["div"] = _REDACTED_DIV
        elif isinstance(node[key], str):
            node[key] = _REDACTED_DIV
    elif mode == "html_tokenize":
        # Preserve XHTML structure and tokenize only text nodes.
        if isinstance(node[key], dict) and isinstance(node[key].get("div"), str):
            node[key]["div"] = _scrub_xhtml_text_nodes(node[key]["div"], scrub_fn)
        elif isinstance(node[key], str):
            node[key] = _scrub_xhtml_text_nodes(node[key], scrub_fn)
    else:
        # In-place text scrubbing
        if isinstance(node[key], list):
            for idx, item in enumerate(node[key]):
                if isinstance(item, str):
                    node[key][idx] = scrub_fn(item)
        else:
            if isinstance(node[key], str):
                node[key] = scrub_fn(node[key])


def _get_token_state(params, mapping_scope):
    """Return mutable token-state dict according to selected scope.

    - 'global_run': shares state across all calls in a batch CLI run.
      WARNING: This state grows indefinitely  call reset_global_token_state()
      between separate batch runs to prevent unbounded memory growth.
    - 'bundle'/'resource': uses per-call state (safe for API use).
    """
    if mapping_scope == "global_run":
        return _GLOBAL_TOKEN_STATE

    # 'bundle' and 'resource' both get fresh state per action call.
    # Bundle-level consistency is preserved because scrub_text_by_path is
    # called once per matched field across the whole bundle in a single
    # process_data invocation.
    return {"next": {}, "map": {}, "reverse": {}}


# ---------------------------------------------------------------------------
# Public action entry point
# ---------------------------------------------------------------------------


def scrub_text_by_path(resource: dict, el: dict, params: dict) -> None:
    """Scrub names from a free-text or HTML field matched by FHIRPath.

    Required params: none (all have defaults).

    Optional params:
        mode          : 'text' (default), 'html_tokenize', or 'html'
        patterns      : 'all' (default)  retained for backward compatibility
        names         : list of literal name strings to redact
        extract_names : bool  auto-extract names from resource.name
        placeholders  : dict mapping pattern key → replacement token
        tokenize      : bool (default True). If False, uses static placeholders.
        mapping_scope : resource (default), bundle, or global_run
    """
    path = el["path"].split(".")[1:]
    if not path:
        return

    mode = params.get("mode", "text")
    pattern_keys = _resolve_patterns(params.get("patterns", "all"))
    placeholder_overrides = params.get("placeholders", {})
    tokenize = params.get("tokenize", True)
    mapping_scope = params.get("mapping_scope", "resource")
    _VALID_SCOPES = {"resource", "bundle", "global_run"}
    if mapping_scope not in _VALID_SCOPES:
        raise ValueError(
            f"Invalid mapping_scope {mapping_scope!r}. Must be one of: {sorted(_VALID_SCOPES)}"
        )
    token_state = _get_token_state(params, mapping_scope)
    # Use lock when accessing global state for thread safety
    token_lock = _GLOBAL_TOKEN_LOCK if mapping_scope == "global_run" else None

    names = list(params.get("names", []))
    if params.get("extract_names", False):
        names.extend(_extract_names_from_resource(resource))

    def _scrub_value(text):
        return _scrub(
            text,
            pattern_keys,
            names,
            placeholder_overrides,
            tokenize,
            token_state,
            token_lock,
        )

    parent_nodes = find_nodes(resource, path[:-1], [])
    _apply_to_node(parent_nodes, path[-1], _scrub_value, mode)

    if params.get("reversible", False):
        # Keep reverse mapping in-memory for this run only.
        params["_token_reverse_map"] = deepcopy(token_state["reverse"])
