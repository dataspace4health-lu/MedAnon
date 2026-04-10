"""Generalize quasi-identifiers to reduce re-identification risk.

Supported generalization strategies (set via ``params['strategy']``):

  date_year       — truncate a date/dateTime to the year only
                    "1991-01-04"         → "1991"
                    "1991-01-04T00:00:00" → "1991"

  date_year_month — truncate to year-month
                    "1980-02-04"         → "1980-02"

  date_year_instant — truncate to year but keep a valid FHIR instant format
                    "2024-03-15T10:30:00Z" → "2024-01-01T00:00:00Z"
                    Use for fields typed as instant (e.g. meta.lastUpdated)
                    where year-only would fail FHIR schema validation.

  age_bracket     — convert a date to an age bracket string
                    "1991-01-04" → "30-39"  (bracket_size defaults to 10)

  number_round    — round a number to the nearest ``precision``
                    90 with precision=10 → 90
                    94 with precision=10 → 90

  zip_prefix      — keep only the first ``prefix_len`` chars of a string
                    "12345" with prefix_len=3 → "123"

  category        — map a value to a broader category using a lookup table
                    supplied via ``params['mapping']``  (dict)

  redact_if_rare  — redact if the value matches a configurable list,
                    otherwise keep.  Useful for gender/ethnicity.
"""

import re
from datetime import date, datetime

from utils.fhirpath import find_nodes


# -- core generalization functions -------------------------------------------

def _generalize_date_year(value):
    """Extract just the year from a FHIR date or dateTime string."""
    s = str(value).strip()
    m = re.match(r'(\d{4})', s)
    return m.group(1) if m else s


def _generalize_date_year_instant(value):
    """Truncate an instant/dateTime to year, returning a valid FHIR instant.

    "2024-03-15T10:30:00Z"   → "2024-01-01T00:00:00Z"
    "2024-03-15T10:30:00+02" → "2024-01-01T00:00:00Z"
    """
    s = str(value).strip()
    m = re.match(r'(\d{4})', s)
    return f"{m.group(1)}-01-01T00:00:00Z" if m else s


def _generalize_date_year_month(value):
    """Extract year-month from a FHIR date or dateTime string."""
    s = str(value).strip()
    m = re.match(r'(\d{4}-\d{2})', s)
    return m.group(1) if m else s


def _generalize_age_bracket(value, bracket_size=10):
    """Convert a birth date string to an age bracket like '30-39'."""
    s = str(value).strip()[:10]  # take date portion
    try:
        birth = datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        try:
            birth = datetime.strptime(s[:4], '%Y').date().replace(month=1, day=1)
        except ValueError:
            return s
    today = date.today()
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    lower = (age // bracket_size) * bracket_size
    upper = lower + bracket_size - 1
    if upper >= 90:
        return "90+"
    return f"{lower}-{upper}"


def _generalize_number_round(value, precision=10):
    """Round a numeric value to the nearest ``precision``."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value
    rounded = round(n / precision) * precision
    return int(rounded) if isinstance(value, int) or (isinstance(value, float) and rounded == int(rounded)) else rounded


def _generalize_zip_prefix(value, prefix_len=3):
    """Keep only the first ``prefix_len`` characters."""
    s = str(value)
    return s[:prefix_len] if len(s) > prefix_len else s


def _generalize_category(value, mapping):
    """Map a value to a broader category via a dict."""
    return mapping.get(str(value), str(value))


# -- strategy dispatcher -----------------------------------------------------

_STRATEGIES = {
    'date_year': lambda v, p: _generalize_date_year(v),
    'date_year_month': lambda v, p: _generalize_date_year_month(v),
    'date_year_instant': lambda v, p: _generalize_date_year_instant(v),
    'age_bracket': lambda v, p: _generalize_age_bracket(v, p.get('bracket_size', 10)),
    'number_round': lambda v, p: _generalize_number_round(v, p.get('precision', 10)),
    'zip_prefix': lambda v, p: _generalize_zip_prefix(v, p.get('prefix_len', 3)),
    'category': lambda v, p: _generalize_category(v, p.get('mapping', {})),
}


def _generalize_value(value, params):
    """Apply the configured generalization strategy to a single value."""
    strategy = params.get('strategy', 'date_year')
    fn = _STRATEGIES.get(strategy)
    if fn is None:
        raise ValueError(f"Unknown generalize strategy: {strategy}. "
                         f"Supported: {', '.join(sorted(_STRATEGIES))}")
    return fn(value, params)


# -- node walker (same pattern as other SPE-FHIR-BlackBox actions) ------------------

def _generalize_nodes(node, key, value, params):
    if isinstance(node, list):
        for item in node:
            _generalize_nodes(item, key, value, params)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    node[key][idx] = _generalize_value(data, params)
        else:
            if node[key] == value:
                node[key] = _generalize_value(node[key], params)


# -- public action handler ---------------------------------------------------

def generalize_by_path(resource, el, params):
    """Generalize a quasi-identifier matched by FHIRPath.

    Required params:
        strategy: one of date_year, date_year_month, age_bracket,
                  number_round, zip_prefix, category

    Optional params (strategy-dependent):
        bracket_size: age bracket width (default 10)
        precision: rounding precision for numbers (default 10)
        prefix_len: number of characters to keep for zip (default 3)
        mapping: dict for category strategy
    """
    path = el['path'].split('.')[1:]
    if len(path) == 0:
        resource.clear()
        return
    ret = find_nodes(resource, path[:-1], [])
    _generalize_nodes(ret, path[-1], el['value'], params)
