"""Climbable generalization hierarchies for quasi-identifier attributes.

Each *kind* maps to an ordered sequence of levels (level 0 = least generalised,
top level = full suppression sentinel).  Every level is a callable
``(value: str) -> str``  applying a higher level produces a coarser output.

Existing strategy functions from ``actions/generalize.py`` are re-used directly
(not reimplemented) so there is one source of truth for the transformation logic.

Supported kinds
---------------
  date      full → year_month → year → decade → suppress
  zip       full → prefix(4) → prefix(3) → prefix(2) → prefix(1) → suppress
  age       exact → bracket(5) → bracket(10) → bracket(20) → suppress
  numeric   exact → round(10) → round(100) → suppress
  category  exact → suppress   (no intermediate; mapping-based strategies stay
                                in static config, not here)

Backward compatibility
----------------------
``actions/generalize.py`` ``_STRATEGIES`` and the static ``generalize`` action
are completely unaffected.  This module is a *superset* consumed only by the
risk-driven executor path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


# Re-use the canonical implementation from the existing action module.
# Import lazily to avoid circular imports at module load time (actions may not
# be on the path when running tests outside the full app).
def _date_year(v: str) -> str:
    from actions.generalize import _generalize_date_year

    return _generalize_date_year(v)


def _date_year_month(v: str) -> str:
    from actions.generalize import _generalize_date_year_month

    return _generalize_date_year_month(v)


def _date_decade(v: str) -> str:
    from actions.generalize import _generalize_date_decade

    return _generalize_date_decade(v)


def _zip_prefix(n: int) -> Callable[[str], str]:
    def _fn(v: str) -> str:
        from actions.generalize import _generalize_zip_prefix

        return _generalize_zip_prefix(v, prefix_len=n)

    _fn.__name__ = f"zip_prefix_{n}"
    return _fn


def _age_bracket(size: int) -> Callable[[str], str]:
    def _fn(v: str) -> str:
        from actions.generalize import _generalize_age_bracket

        return _generalize_age_bracket(v, bracket_size=size)

    _fn.__name__ = f"age_bracket_{size}"
    return _fn


def _number_round(precision: int) -> Callable[[str], str]:
    def _fn(v: str) -> str:
        from actions.generalize import _generalize_number_round

        result = _generalize_number_round(v, precision=precision)
        return str(result)

    _fn.__name__ = f"number_round_{precision}"
    return _fn


# Sentinel value written to QI fields when a record is fully suppressed.
# Chosen to match what analytics/risk._is_qi_suppressed() treats as suppressed
# so the existing k-anonymity measurement code degrades gracefully.
SUPPRESSED_SENTINEL = "[SUPPRESSED]"


def _suppress(v: str) -> str:  # noqa: ARG001
    return SUPPRESSED_SENTINEL


def _identity(v: str) -> str:
    return v


# ---------------------------------------------------------------------------
# Hierarchy definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneralizationHierarchy:
    """Ordered sequence of generalization levels for one QI attribute kind.

    ``levels[0]`` = least generalised (identity or near-identity).
    ``levels[-1]`` = full suppression.
    """

    kind: str
    levels: tuple[Callable[[str], str], ...] = field(compare=False)

    def max_level(self) -> int:
        """Index of the highest (most general) level."""
        return len(self.levels) - 1

    def apply(self, level: int, value: str) -> str:
        """Apply *level* to *value*; clamps to [0, max_level]."""
        idx = max(0, min(level, self.max_level()))
        return self.levels[idx](value)


# Registry: kind string → hierarchy
_HIERARCHIES: dict[str, GeneralizationHierarchy] = {
    "date": GeneralizationHierarchy(
        kind="date",
        levels=(
            _identity,  # 0  keep as-is
            _date_year_month,  # 1  YYYY-MM
            _date_year,  # 2  YYYY
            _date_decade,  # 3  decade start year (e.g. 1990)
            _suppress,  # 4  [SUPPRESSED]
        ),
    ),
    "zip": GeneralizationHierarchy(
        kind="zip",
        levels=(
            _identity,  # 0  keep full postal code
            _zip_prefix(4),  # 1  first 4 chars
            _zip_prefix(3),  # 2  first 3 chars (HIPAA Safe Harbor default)
            _zip_prefix(2),  # 3  first 2 chars
            _zip_prefix(1),  # 4  first char only
            _suppress,  # 5  [SUPPRESSED]
        ),
    ),
    "age": GeneralizationHierarchy(
        kind="age",
        levels=(
            _identity,  # 0  keep exact birth date
            _age_bracket(5),  # 1  5-year brackets
            _age_bracket(10),  # 2  10-year brackets (most common)
            _age_bracket(20),  # 3  20-year brackets
            _suppress,  # 4  [SUPPRESSED]
        ),
    ),
    "numeric": GeneralizationHierarchy(
        kind="numeric",
        levels=(
            _identity,  # 0  exact
            _number_round(10),  # 1  nearest 10
            _number_round(100),  # 2  nearest 100
            _suppress,  # 3  [SUPPRESSED]
        ),
    ),
    # category: minimal  exact value or suppressed.
    # Intermediate mappings (e.g. ICD codes → disease category) are config-
    # authored and applied by the normal static generalize action; they are not
    # part of the automated lattice search.
    "category": GeneralizationHierarchy(
        kind="category",
        levels=(
            _identity,  # 0  keep as-is
            _suppress,  # 1  [SUPPRESSED]
        ),
    ),
}

KNOWN_KINDS: frozenset[str] = frozenset(_HIERARCHIES)


def get_hierarchy(kind: str) -> GeneralizationHierarchy:
    """Return the hierarchy for *kind* or raise ``ValueError``."""
    h = _HIERARCHIES.get(kind)
    if h is None:
        raise ValueError(
            f"Unknown quasi-identifier kind {kind!r}. "
            f"Supported: {', '.join(sorted(KNOWN_KINDS))}"
        )
    return h


def level_value(kind: str, level: int, value: str) -> str:
    """Apply *level* of hierarchy *kind* to *value*.

    Convenience wrapper for callers that don't cache the hierarchy object.
    """
    return get_hierarchy(kind).apply(level, value)


def max_level(kind: str) -> int:
    """Return the maximum level index for *kind*."""
    return get_hierarchy(kind).max_level()
