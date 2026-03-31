"""Tokenize PHI from free-text and HTML narrative fields.

This action applies configurable regex-based patterns to detect and
tokenize Protected Health Information (PHI) in unstructured text fields
such as ``*.note.text``, annotations, and FHIR XHTML narratives
(``*.text.div``).

Supported ``mode`` values (set via ``params['mode']``):

    text  (default) — tokenize plain-text or Markdown fields in-place by
                                        replacing matched PHI spans with deterministic tokens.

    html_tokenize   — tokenize PHI in XHTML text nodes while preserving
                                        markup and attributes.

    html            — replace the entire XHTML field with a single safe
                    FHIR-compliant ``<div>`` placeholder.  This is the
                    recommended mode for ``*.text.div`` fields because FHIR
                    narratives routinely contain patient names, dates, and
                    addresses inline.

Supported pattern keys (``params['patterns']``, default ``'all'``):

  date_iso     — ISO dates / dateTimes:  2023-01-15,  1991-01-04T00:00:00
  date_us      — US short dates:         01/15/2023
  date_written — Written dates:          January 15, 2023
  phone        — US phone numbers:       (555) 867-5309,  +1 555 867 5309
  ssn          — US SSN:                 123-45-6789
  email        — e-mail addresses
  ip           — IPv4 addresses
  mrn          — MRN markers:            MRN: 12345,  MR#67890
    national_id  — national/passport ID markers
    account      — account/insurance/member/policy number markers
    nationality  — sensitive NRP labels (e.g. "nationality: German")
    religion     — sensitive NRP labels (e.g. "religion: Muslim")
    political    — sensitive NRP labels (e.g. "political opinion: ...")
  url          — Bare HTTP/HTTPS URLs
  zipcode      — US ZIP codes:           12345,  12345-6789

Optional params:

  names         — list of name strings to redact (case-insensitive
                  literal match, e.g. ``["Smith", "John"]``).
  extract_names — if ``true``, automatically extract names from the
                  resource's own ``name`` field (FHIR Patient / Practitioner).
  placeholders  — dict overriding default replacement tokens, e.g.
                  ``{"date_iso": "[DATE]", "phone": "[PHONE_NUM]"}``.
    mapping_scope — ``resource`` (default), ``bundle`` or ``global_run``
                                    controls token consistency scope.
"""

import re
import threading
from copy import deepcopy

from utils.fhirpath import find_nodes

# ---------------------------------------------------------------------------
# Regex pattern catalogue
# ---------------------------------------------------------------------------

_PATTERNS = {
    # ISO date / dateTime: 2023-01-15, 1991-01-04T00:00:00Z
    'date_iso': (
        re.compile(
            r'\b\d{4}-\d{2}-\d{2}'
            r'(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b'
        ),
        '[DATE]',
    ),
    # US short date: 01/15/2023, 1/5/23
    'date_us': (
        re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b'),
        '[DATE]',
    ),
    # Written date: January 15, 2023
    'date_written': (
        re.compile(
            r'\b(?:January|February|March|April|May|June|July|August|September|'
            r'October|November|December)\s+\d{1,2},?\s+\d{4}\b',
            re.IGNORECASE,
        ),
        '[DATE]',
    ),
    # US phone: (555) 867-5309, 555-867-5309, +1 555 867 5309
    # ReDoS note: use a plain character class [.\- ] instead of \s to avoid
    # nested quantifier paths that cause catastrophic backtracking.
    'phone': (
        re.compile(
            r'(?<!\d)'
            r'(?:\+?1[.\- ]?)?'
            r'(?:\(\d{3}\)|\d{3})'
            r'[.\- ]?\d{3}[.\- ]?\d{4}'
            r'(?!\d)'
        ),
        '[PHONE]',
    ),
    # US Social Security Number: 123-45-6789
    'ssn': (
        re.compile(r'\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b'),
        '[SSN]',
    ),
    # E-mail address
    'email': (
        re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),
        '[EMAIL]',
    ),
    # IPv4 address (avoid matching version strings like "1.2.3")
    'ip': (
        re.compile(
            r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
            r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
        ),
        '[IP]',
    ),
    # IPv6 addresses: full, compressed (::), and IPv4-mapped (::ffff:x.x.x.x)
    'ipv6': (
        re.compile(
            r'(?<![:\w])'
            r'(?:'
            # Full 8-group: 2001:0db8:85a3:0000:0000:8a2e:0370:7334
            r'(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}'
            r'|'
            # Compressed with :: anywhere
            r'(?:[0-9a-fA-F]{1,4}:){1,7}:'
            r'|'
            r'(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}'
            r'|'
            r'(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}'
            r'|'
            r'(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}'
            r'|'
            r'(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}'
            r'|'
            r'(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}'
            r'|'
            r'[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}'
            r'|'
            # :: alone or with trailing groups
            r':(?::[0-9a-fA-F]{1,4}){1,7}'
            r'|'
            r'::(?:[fF]{4}:)?(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'
            r')'
            r'(?![:\w])'
        ),
        '[IP]',
    ),
    # MRN markers: "MRN: 123456", "MR#67890", "MRN123456"
    'mrn': (
        re.compile(r'\b(?:MRN|MR)\s*[:#]?\s*\d+\b', re.IGNORECASE),
        '[MRN]',
    ),
    # National/passport identifiers when explicitly labeled
    # Upper bound on token length avoids ReDoS on non-matching long strings.
    'national_id': (
        re.compile(
            r'\b(?:national\s*id|nid|passport(?:\s*number)?|id(?:\s*number)?)\s*'
            r'[:#-]?\s*[A-Z0-9\-]{4,50}\b',
            re.IGNORECASE,
        ),
        '[NATIONAL_ID]',
    ),
    # Account / insurance / policy identifiers when explicitly labeled
    'account': (
        re.compile(
            r'\b(?:account|acct|insurance|member|policy)\s*'
            r'(?:id|number|no\.?|#)?\s*[:#-]?\s*[A-Z0-9\-]{4,50}\b',
            re.IGNORECASE,
        ),
        '[ACCOUNT]',
    ),
    # GDPR Art. 9 special categories (NRP) when explicitly labeled
    'nationality': (
        re.compile(
            r'\b(?:nationality|citizenship)\s*[:\-]\s*[A-Za-z][A-Za-z\-\s]{1,40}\b',
            re.IGNORECASE,
        ),
        '[NATIONALITY]',
    ),
    'religion': (
        re.compile(
            r'\b(?:religion|faith)\s*[:\-]\s*[A-Za-z][A-Za-z\-\s]{1,40}\b',
            re.IGNORECASE,
        ),
        '[RELIGION]',
    ),
    'political': (
        re.compile(
            r'\b(?:political(?:\s+opinion|\s+affiliation)?|party)\s*[:\-]\s*'
            r'[A-Za-z][A-Za-z\-\s]{1,80}\b',
            re.IGNORECASE,
        ),
        '[POLITICAL]',
    ),
    # Bare HTTP/HTTPS URLs
    'url': (
        re.compile(r'\bhttps?://[^\s<>"\')\]]+'),
        '[URL]',
    ),
    # US ZIP code: 12345 or 12345-6789
    # Negative lookbehind/lookahead prevents matching within longer numbers or decimals
    'zipcode': (
        re.compile(r'(?<![.\d])\b\d{5}(?:-\d{4})?\b(?!\d)'),
        '[ZIP]',
    ),
}

# Patterns applied when params['patterns'] == 'all'
_ALL_PATTERNS = list(_PATTERNS.keys())

# FHIR text.div must be valid XHTML with the FHIR namespace on the root element.
_REDACTED_DIV = (
    '<div xmlns="http://www.w3.org/1999/xhtml">'
    'This resource has been de-identified.'
    '</div>'
)

_GLOBAL_TOKEN_STATE = {'next': {}, 'map': {}, 'reverse': {}}
_GLOBAL_TOKEN_LOCK = threading.Lock()
_TOKEN_STATE_MAX_ENTRIES = 100_000


def reset_global_token_state():
    """Clear the global token state. Call between batch runs to prevent unbounded growth."""
    with _GLOBAL_TOKEN_LOCK:
        _GLOBAL_TOKEN_STATE['next'].clear()
        _GLOBAL_TOKEN_STATE['map'].clear()
        _GLOBAL_TOKEN_STATE['reverse'].clear()


def _evict_if_needed(token_state, limit=_TOKEN_STATE_MAX_ENTRIES):
    """Drop the oldest 25% of entries when the map exceeds *limit*."""
    if len(token_state['map']) <= limit:
        return
    evict_count = len(token_state['map']) // 4
    keys_to_drop = list(token_state['map'].keys())[:evict_count]
    for key in keys_to_drop:
        token = token_state['map'].pop(key, None)
        if token:
            token_state['reverse'].pop(token, None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_patterns(patterns_param):
    """Return a list of pattern keys to apply."""
    if patterns_param == 'all':
        return _ALL_PATTERNS
    if isinstance(patterns_param, str):
        keys = [p.strip() for p in patterns_param.split(',')]
    else:
        keys = list(patterns_param)
    unknown = set(keys) - set(_PATTERNS)
    if unknown:
        raise ValueError(
            f"Unknown scrub_text pattern(s): {sorted(unknown)}. "
            f"Supported: {', '.join(sorted(_PATTERNS))}"
        )
    return keys


def _token_prefix_for_pattern(pattern_key):
    return {
        'date_iso': 'DATE',
        'date_us': 'DATE',
        'date_written': 'DATE',
        'phone': 'PHONE',
        'ssn': 'SSN',
        'email': 'EMAIL',
        'ip': 'IP',
        'ipv6': 'IP',
        'mrn': 'MRN',
        'national_id': 'NID',
        'account': 'ACCOUNT',
        'nationality': 'NATIONALITY',
        'religion': 'RELIGION',
        'political': 'POLITICAL',
        'url': 'URL',
        'zipcode': 'ZIP',
    }.get(pattern_key, pattern_key.upper())


def _tokenize_value(value, token_prefix, token_state, lock=None):
    """Return deterministic token for *value* and maintain reverse map."""
    if lock:
        with lock:
            return _tokenize_value_unlocked(value, token_prefix, token_state)
    return _tokenize_value_unlocked(value, token_prefix, token_state)


def _tokenize_value_unlocked(value, token_prefix, token_state):
    """Internal helper for _tokenize_value — assumes lock is already held if needed."""
    key = (token_prefix, value)
    if key in token_state['map']:
        return token_state['map'][key]

    _evict_if_needed(token_state)
    current = token_state['next'].get(token_prefix, 0) + 1
    token_state['next'][token_prefix] = current
    token = f"[[{token_prefix}_{current}]]"
    token_state['map'][key] = token
    token_state['reverse'][token] = value
    return token


def _scrub(text, pattern_keys, names, placeholder_overrides, tokenize, token_state, token_lock=None):
    """Apply PHI regex patterns and name tokenization/redaction to *text*."""
    for key in pattern_keys:
        compiled, default_ph = _PATTERNS[key]
        if tokenize:
            token_prefix = _token_prefix_for_pattern(key)

            def repl(match, lock=token_lock):
                return _tokenize_value(match.group(0), token_prefix, token_state, lock)

            text = compiled.sub(repl, text)
        else:
            ph = placeholder_overrides.get(key, default_ph)
            text = compiled.sub(ph, text)

    # Name scrubbing — case-insensitive literal match on each token
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        if tokenize:
            regex = re.compile(re.escape(name), re.IGNORECASE)

            def repl_name(match, lock=token_lock):
                return _tokenize_value(match.group(0), 'NAME', token_state, lock)

            text = regex.sub(repl_name, text)
        else:
            text = re.sub(re.escape(name), '[NAME]', text, flags=re.IGNORECASE)

    return text


def _extract_names_from_resource(resource):
    """Return a flat list of given/family name strings from a FHIR resource."""
    names = []
    for entry in resource.get('name', []):
        if not isinstance(entry, dict):
            continue
        if entry.get('family'):
            names.append(entry['family'])
        for given in entry.get('given', []):
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

    if mode == 'html':
        # Replace the entire XHTML narrative with a safe placeholder.
        # FHIR Narrative is usually an object at *.text with a nested div.
        if isinstance(node[key], dict) and isinstance(node[key].get('div'), str):
            node[key]['div'] = _REDACTED_DIV
        elif isinstance(node[key], str):
            node[key] = _REDACTED_DIV
    elif mode == 'html_tokenize':
        # Preserve XHTML structure and tokenize only text nodes.
        if isinstance(node[key], dict) and isinstance(node[key].get('div'), str):
            node[key]['div'] = _scrub_xhtml_text_nodes(node[key]['div'], scrub_fn)
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
      WARNING: This state grows indefinitely — call reset_global_token_state()
      between separate batch runs to prevent unbounded memory growth.
    - 'bundle'/'resource': uses per-call state (safe for API use).
    """
    if mapping_scope == 'global_run':
        return _GLOBAL_TOKEN_STATE

    # 'bundle' and 'resource' both get fresh state per action call.
    # Bundle-level consistency is preserved because scrub_text_by_path is
    # called once per matched field across the whole bundle in a single
    # process_data invocation.
    return {'next': {}, 'map': {}, 'reverse': {}}


def _scrub_xhtml_text_nodes(div_html, scrub_fn):
    """Tokenize text between tags while preserving XHTML structure.

    This avoids touching attributes like xmlns URLs and only scrubs visible
    narrative text content.
    """
    return re.sub(
        r'>([^<>]+)<',
        lambda m: '>' + scrub_fn(m.group(1)) + '<',
        div_html,
    )


# ---------------------------------------------------------------------------
# Public action entry point
# ---------------------------------------------------------------------------

def scrub_text_by_path(resource, el, params):
    """Scrub PHI from a free-text or HTML field matched by FHIRPath.

    Required params: none (all have defaults).

    Optional params:
        mode          : 'text' (default), 'html_tokenize', or 'html'
        patterns      : 'all' (default), a comma-separated string, or a list
                        of pattern keys (see module docstring)
        names         : list of literal name strings to redact
        extract_names : bool — auto-extract names from resource.name
        placeholders  : dict mapping pattern key → replacement token
        tokenize      : bool (default True). If False, uses static placeholders.
        mapping_scope : resource (default), bundle, or global_run
                        (bundle currently aliases global_run)
    """
    path = el['path'].split('.')[1:]
    if not path:
        return

    mode = params.get('mode', 'text')
    pattern_keys = _resolve_patterns(params.get('patterns', 'all'))
    placeholder_overrides = params.get('placeholders', {})
    tokenize = params.get('tokenize', True)
    mapping_scope = params.get('mapping_scope', 'resource')
    _VALID_SCOPES = {'resource', 'bundle', 'global_run'}
    if mapping_scope not in _VALID_SCOPES:
        raise ValueError(
            f"Invalid mapping_scope {mapping_scope!r}. Must be one of: {sorted(_VALID_SCOPES)}"
        )
    token_state = _get_token_state(params, mapping_scope)
    # Use lock when accessing global state for thread safety
    token_lock = _GLOBAL_TOKEN_LOCK if mapping_scope == 'global_run' else None

    names = list(params.get('names', []))
    if params.get('extract_names', False):
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

    if params.get('reversible', False):
        # Keep reverse mapping in-memory for this run only.
        params['_token_reverse_map'] = deepcopy(token_state['reverse'])
