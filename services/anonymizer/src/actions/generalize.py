"""Generalize quasi-identifiers to reduce re-identification risk.

Supported generalization strategies (set via ``params['strategy']``):

  date_year        truncate a date/dateTime to the year only
                    "1991-01-04"         → "1991"
                    "1991-01-04T00:00:00" → "1991"

                    Optional ``cap_age`` (int) enables the HIPAA Safe Harbor
                    90+ rule: any date implying an age above the cap collapses
                    to one constant floor year so the 90+ cohort is a single
                    indistinguishable category (also honoured by date_decade
                    and date_year_month). ``cap_age: 89`` on a birthDate maps
                    everyone older than 89 to the same year. Off unless set.

  date_year_month  truncate to year-month
                    "1980-02-04"         → "1980-02"

  date_year_instant  truncate to year but keep a valid FHIR instant format
                    "2024-03-15T10:30:00Z" → "2024-01-01T00:00:00Z"
                    Use for fields typed as instant (e.g. meta.lastUpdated)
                    where year-only would fail FHIR schema validation.

  age_bracket      convert a date to an age bracket string
                    "1991-01-04" → "30-39"  (bracket_size defaults to 10)

  number_round     round a number to the nearest ``precision``
                    90 with precision=10 → 90
                    94 with precision=10 → 90

  zip_prefix       keep only the first ``prefix_len`` chars of a string
                    "12345" with prefix_len=3 → "123"

  category         map a value to a broader category using a lookup table
                    supplied via ``params['mapping']``  (dict)

  redact_if_rare   redact if the value is in a configured rare-value
                    denylist, otherwise keep unchanged. Useful for
                    demographic/QI categories D7.2 §3.3.1-3.3.2 flags as
                    high-risk in small counts (rare ethnicity/nationality
                    values, rare disease codes)  declare the known-rare
                    values via ``params['rare_values']`` (list of strings)
                    since this per-element action has no visibility into the
                    dataset's actual value frequencies (that needs the
                    dataset-wide lattice solver in ``pipeline.privacy``).
                    "1991-01-04" with rare_values=["1991-01-04"] → "[REDACTED]"
                    "Native Hawaiian" with rare_values=[...] → "[REDACTED]"
                    "White" (not in rare_values) → "White" (unchanged)
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from utils.fhirpath import find_nodes

_log = logging.getLogger("medanon.generalize")


# -- date parsing helpers -----------------------------------------------------
#
# FHIR dates are always ISO 8601, so the ISO fast-path below is tried first and
# preserves the original behaviour exactly.  Tabular exports (CSV/Excel), by
# contrast, carry dates in many locale formats (``02/04/1980``, ``04.02.1980``,
# ``Feb 4 1980`` …), which the ISO-only regex silently passed through unchanged.
# The fallbacks below recover the year/month from those layouts.

# Common non-ISO layouts found in spreadsheet exports, tried in order only when
# the value is not already ISO-prefixed.
_FALLBACK_DATE_FORMATS = (
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
)

# A 4-digit year (1900-2099) appearing anywhere in the string.  Year is
# unambiguous regardless of day/month ordering, so this safely handles formats
# like ``02/04/1980`` where full date parsing would be ambiguous.
_YEAR_RE = re.compile(r"(?:^|\D)((?:19|20)\d{2})(?:\D|$)")


def _parse_date_loose(value):
    """Best-effort parse of a date string to a :class:`datetime.date`.

    Tries the ISO ``YYYY-MM-DD`` prefix first (covering ``date`` and
    ``dateTime``), then the common non-ISO spreadsheet layouts.  Returns
    ``None`` when nothing matches so callers can fall back to a year-only
    extraction or pass the value through unchanged.
    """
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _extract_year(value):
    """Return the 4-digit year string for a date in any layout, else ``None``."""
    s = str(value).strip()
    m = re.match(r"(\d{4})", s)  # ISO fast-path (unchanged behaviour)
    if m:
        return m.group(1)
    m = _YEAR_RE.search(s)  # year anywhere, e.g. '02/04/1980'
    return m.group(1) if m else None


# -- core generalization functions -------------------------------------------


def _generalize_date_year(value):
    """Extract just the year from a date or dateTime string (any layout)."""
    year = _extract_year(value)
    return year if year is not None else str(value).strip()


def _generalize_date_year_instant(value):
    """Truncate an instant/dateTime to year, returning a valid FHIR instant.

    "2024-03-15T10:30:00Z"   → "2024-01-01T00:00:00Z"
    "2024-03-15T10:30:00+02" → "2024-01-01T00:00:00Z"
    """
    year = _extract_year(value)
    return f"{year}-01-01T00:00:00Z" if year is not None else str(value).strip()


def _generalize_date_year_month(value):
    """Extract year-month from a date or dateTime string (any layout)."""
    s = str(value).strip()
    m = re.match(r"(\d{4}-\d{2})", s)
    if m:
        return m.group(1)
    d = _parse_date_loose(s)
    return d.strftime("%Y-%m") if d is not None else s


def _generalize_date_decade(value):
    """Truncate a date/dateTime to the decade start year (e.g. '1991-03-15' → '1990').

    Collapses all years in the same decade into one bucket, substantially
    improving k-anonymity for birth dates while preserving rough age cohort.
    Outputs a valid FHIR year (YYYY) rather than the non-standard '199x' form.
    """
    year = _extract_year(value)
    return f"{year[:3]}0" if year is not None else str(value).strip()


def _age_from_value(value):
    """Return the current age in whole years for a birth date (any layout).

    Returns ``None`` when no year can be recovered, so callers can fall back to
    passing the value through unchanged.
    """
    birth = _parse_date_loose(value)
    if birth is None:
        year = _extract_year(value)
        if year is None:
            return None
        birth = date(int(year), 1, 1)
    today = date.today()
    return (
        today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    )


def _capped_year_or_none(value, params):
    """Return the single floor-year category when age-capping is enabled and the
    value implies an age above the cap, else ``None`` (caller applies its normal
    strategy).

    HIPAA Safe Harbor §164.514(b)(2)(i)(C): ages over 89  and any date element
    indicative of such an age, including the year  must be collapsed into one
    "90 or older" category. This is opt-in per rule via ``params['cap_age']``
    (the highest age kept as-is; typically 89). All subjects above the cap map
    to the same constant FHIR year (``reference_year - (cap_age + 1)``) so they
    become indistinguishable while the field stays a valid FHIR ``date``.
    ``reference_year`` defaults to the current year; set it explicitly for
    reproducible output across calendar years.
    """
    cap = params.get("cap_age")
    if cap is None:
        return None
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        return None
    age = _age_from_value(value)
    if age is None or age <= cap:
        return None
    ref = params.get("reference_year")
    ref_year = int(ref) if ref is not None else date.today().year
    return str(ref_year - (cap + 1))


def _generalize_age_bracket(value, bracket_size=10):
    """Convert a birth date string (any layout) to an age bracket like '30-39'."""
    age = _age_from_value(value)
    if age is None:
        return str(value).strip()[:10]
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
    return (
        int(rounded)
        if isinstance(value, int)
        or (isinstance(value, float) and rounded == int(rounded))
        else rounded
    )


def _generalize_zip_prefix(value, prefix_len=3):
    """Keep only the first ``prefix_len`` characters."""
    s = str(value)
    return s[:prefix_len] if len(s) > prefix_len else s


def _generalize_category(value, mapping, unmapped="[REDACTED]"):
    """Map a value to a broader category via a dict.

    Values absent from *mapping* use *unmapped* as a safe fallback
    (default: ``"[REDACTED]"``).  Without this, an unmapped value would pass
    through unchanged and leak PHI silently.  Set ``params['unmapped']`` in
    the rule config to override the fallback label.
    """
    result = mapping.get(str(value))
    if result is None:
        _log.warning(
            "category_unmapped resource_field has a value not in the mapping  "
            "replacing with fallback %r",
            unmapped,
        )
        return unmapped
    return result


def _generalize_redact_if_rare(value, rare_values, replacement="[REDACTED]"):
    """Redact *value* when it is in the configured rare-value denylist.

    D7.2 §3.3.1/§3.3.2: rare categorical values (uncommon ethnicity/nationality
    entries, rare disease codes) are quasi-identifiers on their own  a low
    dataset frequency for that value increases re-identification risk even
    when the field itself (e.g. "ethnicity") is otherwise unremarkable.
    ``rare_values`` is author-declared (this action has no dataset-wide
    frequency visibility); values not listed pass through unchanged.
    """
    return replacement if str(value) in rare_values else value


# -- strategy dispatcher -----------------------------------------------------

_STRATEGIES = {
    "date_year": lambda v, p: _capped_year_or_none(v, p) or _generalize_date_year(v),
    "date_year_month": lambda v, p: (
        _capped_year_or_none(v, p) or _generalize_date_year_month(v)
    ),
    "date_year_instant": lambda v, p: _generalize_date_year_instant(v),
    "date_decade": lambda v, p: (
        _capped_year_or_none(v, p) or _generalize_date_decade(v)
    ),
    "age_bracket": lambda v, p: _generalize_age_bracket(v, p.get("bracket_size", 10)),
    "number_round": lambda v, p: _generalize_number_round(v, p.get("precision", 10)),
    "zip_prefix": lambda v, p: _generalize_zip_prefix(
        v, p.get("prefix_length", p.get("prefix_len", 3))
    ),
    "category": lambda v, p: _generalize_category(
        v, p.get("mapping", {}), p.get("unmapped", "[REDACTED]")
    ),
    "redact_if_rare": lambda v, p: _generalize_redact_if_rare(
        v,
        frozenset(str(x) for x in p.get("rare_values", [])),
        p.get("replacement", "[REDACTED]"),
    ),
}


def _generalize_value(value, params):
    """Apply the configured generalization strategy to a single value."""
    strategy = params.get("strategy", "date_year")
    fn = _STRATEGIES.get(strategy)
    if fn is None:
        raise ValueError(
            f"Unknown generalize strategy: {strategy}. "
            f"Supported: {', '.join(sorted(_STRATEGIES))}"
        )
    return fn(value, params)


# -- node walker (same pattern as other SPE-FHIR-BlackBox actions) ------------------


def _generalize_nodes(node: Any, key: str, value: Any, params: dict) -> None:
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


def generalize_by_path(resource: dict, el: dict, params: dict) -> None:
    """Generalize a quasi-identifier matched by FHIRPath.

    Required params:
        strategy: one of date_year, date_year_month, age_bracket,
                  number_round, zip_prefix, category

    Optional params (strategy-dependent):
        bracket_size: age bracket width (default 10)
        precision: rounding precision for numbers (default 10)
        prefix_length: number of characters to keep for zip (default 3;
                       also accepted as prefix_len for backward compatibility)
        mapping: dict for category strategy
        unmapped: fallback label for category values not in the mapping
                  (default: "[REDACTED]")
    """
    path = el["path"].split(".")[1:]
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in generalize  "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(resource, path[:-1], [])
    _generalize_nodes(ret, path[-1], el["value"], params)
