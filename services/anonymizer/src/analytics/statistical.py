"""Statistical-format aggregate release (TEHDAS2 D7.2 §5.5.4 / EHDS data request).

EHDS foresees two release forms: record-level anonymised/pseudonymised data and
an **anonymised statistical format** answering a *data request* (Art 2(2)(f)) with
aggregates rather than rows. This module produces that aggregate: a contingency
table (group-by counts) over chosen quasi-identifiers, protected by one of two
disclosure-control regimes:

- **Small-cell suppression** (the classic SDC approach; Eurostat/ONS): cells with
  a count below a threshold are suppressed (primary suppression), plus minimal
  **complementary suppression** so a suppressed cell cannot be recovered by
  subtraction from a fully-published margin.
- **Differential privacy** (:mod:`analytics.dp`): calibrated Laplace noise on
  every cell for a formal ``epsilon``-DP guarantee, with budget accounting.

Both may be combined (noise then suppress). The output is anonymous: cell keys +
protected counts + a disclosure-control descriptor. No record leaves this layer.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Dual-import shim: packaged in the monolith, flat in the analytics microservice.
try:  # pragma: no cover - import shim
    from analytics import dp
    from analytics.risk import _is_qi_suppressed, _read_path
except ImportError:  # pragma: no cover - analytics microservice layout
    import dp  # type: ignore[no-redirect]
    from risk import _is_qi_suppressed, _read_path

# Default small-cell threshold. 5 is the long-standing statistical-disclosure
# "rule of small numbers" floor used by national statistics offices (Eurostat
# 2010 SDC handbook; ONS). Tunable per request.
DEFAULT_MIN_CELL = 5

_SUPPRESSED = None  # sentinel: a suppressed cell carries a null count


def _generalize(raw: str, kind: str) -> str:
    """Coarsen a raw QI value to the requested granularity before grouping.

    Treated (already-generalised) input passes through with ``kind="raw"``.
    Extra kinds let a caller aggregate finer source data at a chosen level.
    """
    if not raw:
        return ""
    if kind in ("raw", "category", ""):
        return raw
    if kind == "year":
        return raw[:4]
    if kind == "month":
        return raw[:7]
    if kind == "decade":
        return f"{raw[:3]}0s" if len(raw) >= 4 and raw[:4].isdigit() else raw
    if kind in ("zip3", "zip"):
        return raw[:3]
    if kind == "zip2":
        return raw[:2]
    return raw


def _project_row(resource: dict, group_by: list[dict]) -> tuple[str, ...] | None:
    """Project one resource onto the group-by tuple, or None if not applicable."""
    rtype = group_by[0].get("resource_type")
    if rtype and resource.get("resourceType") != rtype:
        return None
    vals: list[str] = []
    for q in group_by:
        raw = _read_path(resource, q.get("path", ""))
        raw = "" if _is_qi_suppressed(raw) else raw
        vals.append(_generalize(raw, q.get("kind", "raw")))
    return tuple(vals)


def _apply_suppression(
    table: dict[tuple[str, ...], int], min_cell: int
) -> tuple[dict[tuple[str, ...], int | None], int]:
    """Primary + minimal complementary small-cell suppression.

    Primary: any cell ``0 < count < min_cell`` is suppressed. Complementary: if a
    group (rows sharing all but the last group-by key) has exactly one suppressed
    cell, its count is recoverable from the published margin, so the smallest
    remaining cell in that group is also suppressed. Zero cells are structural
    (never present) and are dropped, not suppressed.
    """
    protected: dict[tuple[str, ...], int | None] = {}
    suppressed = 0
    for key, count in table.items():
        if 0 < count < min_cell:
            protected[key] = _SUPPRESSED
            suppressed += 1
        else:
            protected[key] = count

    # Complementary suppression needs a margin to subtract from, i.e. at least
    # two group-by dimensions; with a single dimension there is no inner group.
    n_dims = len(next(iter(table))) if table else 0
    if n_dims < 2:
        return protected, suppressed

    # Group by everything except the last key to find single-suppression rows.
    groups: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for key in table:
        groups.setdefault(key[:-1], []).append(key)
    for members in groups.values():
        supp = [k for k in members if protected[k] is _SUPPRESSED]
        if len(supp) == 1:
            candidates = [
                k for k in members if protected[k] is not _SUPPRESSED and protected[k]
            ]
            if candidates:
                victim = min(candidates, key=lambda k: table[k])
                protected[victim] = _SUPPRESSED
                suppressed += 1
    return protected, suppressed


def build_statistical_release(
    resources: list[dict],
    *,
    group_by: list[dict],
    min_cell: int = DEFAULT_MIN_CELL,
    dp_params: dict | None = None,
    drop_empty: bool = True,
) -> dict[str, Any]:
    """Build an anonymised statistical-format aggregate release.

    Args:
        resources: de-identified FHIR resources to aggregate.
        group_by: ordered QI descriptors ``[{"path", "kind"?, "resource_type"?}]``.
        min_cell: small-cell suppression threshold (0 disables suppression).
        dp_params: optional ``{"epsilon", "delta"?, "unbounded"?}`` to add DP noise
            *before* suppression. When present, a DP accounting summary is attached.
        drop_empty: omit structural-zero cells from the output.

    Returns:
        Anonymous release dict: query echo, protected cells, totals, and the
        disclosure-control descriptor(s).
    """
    if not group_by:
        raise ValueError("group_by must list at least one quasi-identifier")

    # Infer the resource-type filter from the first path prefix (``Patient.…``)
    # when not set explicitly, so an aggregation over Patient QIs never counts a
    # non-Patient resource as an all-empty bucket.
    if "resource_type" not in group_by[0]:
        first_path = group_by[0].get("path", "")
        if "." in first_path and first_path.split(".", 1)[0][:1].isupper():
            group_by = [
                {**group_by[0], "resource_type": first_path.split(".", 1)[0]},
                *group_by[1:],
            ]

    keys = [q.get("path", f"g{i}") for i, q in enumerate(group_by)]
    counter: Counter[tuple[str, ...]] = Counter()
    total = 0
    for r in resources:
        row = _project_row(r, group_by)
        if row is None:
            continue
        counter[row] += 1
        total += 1

    table: dict[tuple[str, ...], int] = dict(counter)

    methods: list[str] = []
    accounting: dict[str, Any] | None = None
    if dp_params:
        epsilon = float(dp_params["epsilon"])
        unbounded = bool(dp_params.get("unbounded", True))
        acc = dp.PrivacyAccountant(
            epsilon=float(dp_params.get("budget_epsilon", epsilon))
        )
        table = dp.dp_histogram(
            table,
            epsilon,
            unbounded=unbounded,
            accountant=acc,
            label="statistical_release",
        )
        accounting = acc.summary()
        methods.append("differential_privacy")

    suppressed_count = 0
    if min_cell and min_cell > 1:
        protected, suppressed_count = _apply_suppression(table, min_cell)
        methods.append("small_cell_suppression")
    else:
        protected = dict(table)

    cells: list[dict[str, Any]] = []
    for key, count in protected.items():
        raw_count = table.get(key, 0)
        if drop_empty and raw_count == 0 and count is not _SUPPRESSED:
            continue
        cells.append(
            {
                "key": dict(zip(keys, key)),
                "count": None if count is _SUPPRESSED else count,
                "suppressed": count is _SUPPRESSED,
            }
        )
    cells.sort(key=lambda c: tuple(str(v) for v in c["key"].values()))

    return {
        "format": "statistical_aggregate",
        "query": {
            "group_by": [
                {"path": q.get("path"), "kind": q.get("kind", "raw")} for q in group_by
            ],
            "measure": "count",
            "min_cell": min_cell,
            "dp": dp_params or None,
        },
        "cells": cells,
        "totals": {
            "input_records": total,
            "cells": len(cells),
            "suppressed_cells": suppressed_count,
        },
        "disclosure_control": {"methods": methods or ["none"]},
        "privacy_accounting": accounting,
    }
