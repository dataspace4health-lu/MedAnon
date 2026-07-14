"""File connector: CSV, Excel (.xlsx/.xls), NDJSON, JSON → assess pipeline.

Format is auto-detected from filename extension; falls back to probing content.
Returns either:
  ("fhir",    list[dict])               — route to assess / assess_batch
  ("tabular", dict[str, list[dict]])    — route to assess_omop via tabular_to_omop

Excel sheets are returned as separate tables keyed by sheet name.
CSV is returned as a single table keyed by the filename stem (or the caller's
override name).

Security notes
- defusedxml is NOT used here because openpyxl already rejects external entity
  expansion in OOXML. JSON parsing is stdlib only (no external parsers).
- No shell execution; all parsing is in-process from bytes.
"""

from __future__ import annotations

import csv
import io
import json

_OPENPYXL_AVAILABLE = False
try:
    import openpyxl  # type: ignore[import-untyped]

    _OPENPYXL_AVAILABLE = True
except ImportError:
    pass

_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB hard cap


class ConnectorError(ValueError):
    """Raised when parsing fails with a user-actionable message."""


def parse_file(
    content: bytes,
    filename: str,
    sheet: str | None = None,
    table_name: str | None = None,
) -> tuple[str, list[dict] | dict[str, list[dict]]]:
    """Parse uploaded file bytes.

    Args:
        content:    Raw file bytes.
        filename:   Original filename (used for format detection).
        sheet:      Excel sheet name to restrict to (all sheets if None).
        table_name: Override the table key for CSV (defaults to filename stem).

    Returns:
        ``("fhir", [resource, ...])`` or ``("tabular", {table: [row, ...]})``.
    """
    if len(content) > _MAX_FILE_BYTES:
        raise ConnectorError(
            f"File too large ({len(content) // 1024 // 1024} MB). Maximum is 50 MB."
        )

    name_lower = filename.lower().strip()

    if name_lower.endswith(".xlsx") or name_lower.endswith(".xls"):
        return _parse_excel(content, sheet)
    if name_lower.endswith(".csv") or name_lower.endswith(".tsv"):
        sep = "\t" if name_lower.endswith(".tsv") else ","
        tname = table_name or _stem(filename)
        return _parse_csv(content, tname, sep)
    if name_lower.endswith(".ndjson") or name_lower.endswith(".jsonl"):
        return _parse_ndjson(content)
    if name_lower.endswith(".json"):
        return _parse_json(content)

    # Unknown extension — probe in order: JSON, NDJSON, CSV
    for fn in (_parse_json, _parse_ndjson):
        try:
            return fn(content)
        except ConnectorError:
            pass
    try:
        return _parse_csv(content, table_name or "records", ",")
    except ConnectorError:
        pass

    raise ConnectorError(
        f"Cannot detect format for '{filename}'. "
        "Supported extensions: .json, .ndjson, .jsonl, .csv, .tsv, .xlsx"
    )


# ---------------------------------------------------------------------------
# Format parsers
# ---------------------------------------------------------------------------


def _parse_json(content: bytes) -> tuple[str, list[dict] | dict[str, list[dict]]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"Invalid JSON: {exc}") from exc

    # FHIR Bundle
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        resources = [
            e["resource"]
            for e in data.get("entry", [])
            if isinstance(e, dict) and isinstance(e.get("resource"), dict)
        ]
        return "fhir", resources

    # Single FHIR resource
    if isinstance(data, dict) and "resourceType" in data:
        return "fhir", [data]

    # List of resources or rows
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "resourceType" in data[0]:
            return "fhir", data
        return "tabular", {"records": [r for r in data if isinstance(r, dict)]}

    # Dict of tables: {table_name: [rows]}
    if isinstance(data, dict):
        first = next(iter(data.values()), None)
        if isinstance(first, list):
            return "tabular", {k: v for k, v in data.items() if isinstance(v, list)}

    raise ConnectorError(
        "JSON structure not recognised. Expected a FHIR resource, Bundle, "
        "list of resources/rows, or {table: [rows]} object."
    )


def _parse_ndjson(content: bytes) -> tuple[str, list[dict] | dict[str, list[dict]]]:
    text = content.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ConnectorError("Empty NDJSON file.")
    records: list[dict] = []
    for i, line in enumerate(lines, 1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"Line {i}: invalid JSON — {exc}") from exc
        if isinstance(obj, dict):
            records.append(obj)
    if not records:
        raise ConnectorError("NDJSON file contained no object lines.")
    if "resourceType" in records[0]:
        return "fhir", records
    return "tabular", {"records": records}


def _parse_csv(
    content: bytes, table_name: str, sep: str
) -> tuple[str, dict[str, list[dict]]]:
    # Strip UTF-8 BOM if present
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    rows = [dict(row) for row in reader]
    if not rows:
        raise ConnectorError("CSV file is empty or has no data rows.")
    return "tabular", {table_name: rows}


def _parse_excel(
    content: bytes, sheet: str | None
) -> tuple[str, dict[str, list[dict]]]:
    if not _OPENPYXL_AVAILABLE:
        raise ConnectorError(
            "Excel support requires the 'openpyxl' package. "
            "Install it or convert the file to CSV first."
        )
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ConnectorError(f"Cannot open Excel file: {exc}") from exc

    sheets_to_read = [sheet] if sheet else wb.sheetnames
    result: dict[str, list[dict]] = {}

    for sname in sheets_to_read:
        if sname not in wb.sheetnames:
            wb.close()
            raise ConnectorError(
                f"Sheet '{sname}' not found. Available: {wb.sheetnames}"
            )
        ws = wb[sname]
        rows_iter = ws.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if not header_row:
            continue
        headers = [
            str(c) if c is not None else f"col_{i}" for i, c in enumerate(header_row)
        ]
        rows = [
            {h: v for h, v in zip(headers, row)}
            for row in rows_iter
            if any(v is not None for v in row)
        ]
        if rows:
            result[sname] = rows

    wb.close()
    if not result:
        raise ConnectorError("No data found in the Excel file.")
    return "tabular", result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stem(filename: str) -> str:
    """Return filename without extension, safe for use as a table key."""
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name or "records"
