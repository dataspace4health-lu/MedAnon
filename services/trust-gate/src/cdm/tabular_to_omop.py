"""Tabular / SQL source -> OMOP CDM.

The provider submits rows grouped by OMOP table. Columns may already use OMOP
names, or a per-table ``mapping`` ({omop_col: source_col}) renames them. Light
numeric coercion is applied to the well-known numeric OMOP columns so range and
type checks behave; everything else is carried through unchanged.
"""

from __future__ import annotations

from cdm.omop_model import CORE_TABLES, OmopData

# OMOP columns we coerce to numbers when possible (ids, years, measured values).
_INT_HINTS = ("_concept_id", "_id", "year_of_birth", "month_of_birth", "day_of_birth")
_FLOAT_HINTS = ("value_as_number", "quantity", "range_low", "range_high")


def _coerce(col: str, value):
    if value is None or value == "":
        return None
    if any(col.endswith(h) or col == h for h in _FLOAT_HINTS):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if any(col.endswith(h) for h in _INT_HINTS):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return value


def tabular_to_omop(
    rows_by_table: dict[str, list[dict]],
    mapping: dict[str, dict[str, str]] | None = None,
) -> OmopData:
    """Build :class:`OmopData` from tabular rows grouped by OMOP table.

    ``mapping[table] = {omop_col: source_col}`` renames source columns to OMOP
    column names; tables without a mapping are assumed to already use OMOP names.
    Unknown tables are ignored (only CORE_TABLES are assessed).
    """
    mapping = mapping or {}
    omop = OmopData()
    for table, rows in (rows_by_table or {}).items():
        if table not in CORE_TABLES or not isinstance(rows, list):
            # Record non-empty submitted tables we don't model, so the passport can
            # disclose them instead of silently dropping them.
            if table not in CORE_TABLES and isinstance(rows, list) and rows:
                omop.ignored_tables.append(table)
            continue
        colmap = mapping.get(table) or {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if colmap:
                norm = {
                    omop_col: row.get(src_col) for omop_col, src_col in colmap.items()
                }
            else:
                norm = dict(row)
            omop.add(table, {k: _coerce(k, v) for k, v in norm.items()})
    return omop
