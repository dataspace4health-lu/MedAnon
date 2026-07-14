"""TabularAdapter — de-identify CSV / Excel / Parquet by column.

Tabular data has no fixed field map (columns are arbitrary), so it uses a
distinct rule dialect: rules target columns directly with a ``column:<name>``
matcher rather than a FHIRPath expression.  Each row is treated as a flat
synthetic resource ``{"resourceType": "TabularRow", "<col>": value, …}`` and the
existing actions (redact / generalize / perturb / substitute / cryptohash /
scrub_text / nlp_*) are applied to the matched columns unchanged — a
``column:foo`` rule is dispatched as the path ``TabularRow.foo``.

Example profile (``format: tabular``)::

    rules:
      - match: "column:patient_name"
        action: redact
      - match: "column:DOB"
        action: generalize
        params: { strategy: date_year }
      - match: "column:Diagnosis Notes"
        action: nlp_scrub

Supported input formats: CSV (stdlib), Excel ``.xlsx`` (needs ``openpyxl``),
Parquet (needs ``pyarrow``).  The xlsx/parquet paths raise
:class:`NormalizationError` with a clear message when the optional dependency
is absent.

Round-trip: ``parse`` keeps the column order + source format on the instance;
``serialize`` writes the de-identified rows back in the same format.
``can_target_fhir_server()`` is False — tabular output never reaches the FHIR
target server.
"""

from __future__ import annotations

import io
import re

from pipeline.exceptions import NormalizationError

_COLUMN_PREFIX = "column:"
_TABLE_PREFIX = "table:"
_ROW_TYPE = "TabularRow"


class TabularAdapter:
    """De-identify a CSV / Excel / Parquet file by column rule."""

    source_format = "tabular"

    def __init__(self, file_format: str = "csv", *, delimiter: str = ",") -> None:
        fmt = file_format.lower()
        if fmt not in ("csv", "xlsx", "parquet"):
            raise NormalizationError(f"Unsupported tabular format: {file_format!r}")
        self._format = fmt
        self._delimiter = delimiter
        self._columns: list[str] = []  # preserved column order
        self._rows: list[dict] = []  # parsed rows (column → value)

    # -- parse ----------------------------------------------------------------

    def parse(self, raw: bytes | str) -> list[dict]:
        """Parse the file into a list of row dicts, one per data row."""
        if self._format == "csv":
            self._parse_csv(raw)
        elif self._format == "xlsx":
            self._parse_xlsx(raw)
        else:
            self._parse_parquet(raw)
        # Tag each row as a synthetic resource so existing actions can address
        # columns via the ``TabularRow.<col>`` path.
        return [{"resourceType": _ROW_TYPE, **row} for row in self._rows]

    def inspect(self, raw: bytes | str, *, sample_rows: int = 5) -> dict:
        """Parse *raw* and return a column preview WITHOUT de-identifying anything.

        Powers the UI column-mapping step: the user sees each column's name and
        a few example values, then assigns an action per column.  No rules are
        applied here — this is read-only inspection.

        Returns
        ``{columns: [{name, samples: [...], recommended_action}], row_count, format}``.
        Sample values are truncated for safety/compactness.  ``recommended_action``
        is a heuristic suggestion (the UI pre-selects it; the user can override).
        """
        self.parse(raw)
        n = max(0, int(sample_rows))
        columns = []
        for col in self._columns:
            samples: list[str] = []
            for row in self._rows[:n]:
                val = row.get(col)
                if val is None or val == "":
                    continue
                s = str(val)
                samples.append(s[:80] + "…" if len(s) > 80 else s)
            columns.append(
                {
                    "name": col,
                    "samples": samples,
                    "recommended_action": recommend_column_action(col, samples),
                }
            )
        return {
            "format": self._format,
            "row_count": len(self._rows),
            "columns": columns,
        }

    def _parse_csv(self, raw: bytes | str) -> None:
        import csv

        text = (
            raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
        )
        if not text.strip():
            raise NormalizationError("Invalid CSV: input is empty")
        reader = csv.DictReader(io.StringIO(text), delimiter=self._delimiter)
        if reader.fieldnames is None:
            raise NormalizationError("Invalid CSV: no header row")
        self._columns = list(reader.fieldnames)
        self._rows = [dict(r) for r in reader]

    def _parse_xlsx(self, raw: bytes | str) -> None:
        try:
            import openpyxl
        except ImportError as exc:
            raise NormalizationError(
                "the 'openpyxl' package is required for Excel (.xlsx) de-identification"
            ) from exc
        if isinstance(raw, str):
            raise NormalizationError("Excel input must be raw bytes, not text")
        try:
            wb = openpyxl.load_workbook(
                io.BytesIO(raw), read_only=False, data_only=False
            )
        except Exception as exc:
            raise NormalizationError(f"Invalid Excel workbook: {exc}") from exc
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration as exc:
            raise NormalizationError("Invalid Excel: empty sheet") from exc
        self._columns = [str(h) if h is not None else "" for h in header]
        self._rows = []
        for row in rows_iter:
            self._rows.append(
                {
                    col: ("" if val is None else str(val))
                    for col, val in zip(self._columns, row)
                }
            )
        self._wb = wb  # retained for serialize

    def _parse_parquet(self, raw: bytes | str) -> None:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise NormalizationError(
                "the 'pyarrow' package is required for Parquet de-identification"
            ) from exc
        if isinstance(raw, str):
            raise NormalizationError("Parquet input must be raw bytes, not text")
        try:
            table = pq.read_table(io.BytesIO(raw))
        except Exception as exc:
            raise NormalizationError(f"Invalid Parquet file: {exc}") from exc
        self._columns = list(table.column_names)
        pydict = table.to_pylist()
        self._rows = [
            {
                col: ("" if r.get(col) is None else str(r.get(col)))
                for col in self._columns
            }
            for r in pydict
        ]

    # -- serialize ------------------------------------------------------------

    def serialize(self, resources: list[dict]) -> bytes:
        """Write the de-identified rows back in the source format."""
        # Strip the synthetic resourceType tag; keep only column values, in the
        # original column order.  Redacted columns may have been deleted from a
        # row dict — fall back to "" so every column stays aligned.
        rows = [{col: r.get(col, "") for col in self._columns} for r in resources]
        if self._format == "csv":
            return self._serialize_csv(rows)
        if self._format == "xlsx":
            return self._serialize_xlsx(rows)
        return self._serialize_parquet(rows)

    def _serialize_csv(self, rows: list[dict]) -> bytes:
        import csv

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=self._columns, delimiter=self._delimiter
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return buf.getvalue().encode("utf-8")

    def _serialize_xlsx(self, rows: list[dict]) -> bytes:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(self._columns)
        for r in rows:
            ws.append([r.get(col, "") for col in self._columns])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _serialize_parquet(self, rows: list[dict]) -> bytes:
        import pyarrow as pa
        import pyarrow.parquet as pq

        cols = {col: [r.get(col, "") for r in rows] for col in self._columns}
        table = pa.table(cols)
        buf = io.BytesIO()
        pq.write_table(table, buf)
        return buf.getvalue()

    def can_target_fhir_server(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Column-rule dispatch (the ``column:`` dialect)
# ---------------------------------------------------------------------------


def rules_for_table(rules: list[dict], table: str) -> list[dict]:
    """Resolve which rules apply to *table*, for the multi-table SQL source.

    Rules may target a specific table with the ``table:<table>/column:<col>``
    matcher, or any table with a bare ``column:<col>`` matcher.  This returns the
    subset relevant to *table*, with table-scoped matchers rewritten to plain
    ``column:<col>`` so the result can be handed straight to
    :func:`apply_column_rules` unchanged.

    * ``table:patients/column:mrn`` → included only for table ``patients``,
      rewritten to ``column:mrn``.
    * ``column:mrn`` → included for **every** table (global fallback).

    Table matching is case-insensitive (PostgreSQL folds unquoted identifiers).
    """
    table_lc = str(table).lower()
    resolved: list[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match", "")
        if not isinstance(match, str):
            continue
        if match.startswith(_TABLE_PREFIX):
            body = match[len(_TABLE_PREFIX) :]
            tbl, sep, col_part = body.partition("/")
            if not sep or tbl.strip().lower() != table_lc:
                continue
            if not col_part.startswith(_COLUMN_PREFIX):
                continue
            new_rule = dict(rule)
            new_rule["match"] = col_part  # already "column:<name>"
            resolved.append(new_rule)
        elif match.startswith(_COLUMN_PREFIX):
            resolved.append(rule)  # bare column rule → applies to all tables
    return resolved


def resolve_column_manifest(
    settings=None,
    columns=None,
    *,
    rules: list[dict] | None = None,
) -> list[dict]:
    """Return the transformation manifest for a tabular file (no cell values).

    Lists the ``column:<name>`` rules that apply to columns actually present in
    the file as ``{column, action, rule}`` entries — the tabular analogue of the
    FHIR transformation manifest, released as a separate artifact.
    """
    src_rules = rules if rules is not None else (getattr(settings, "rules", []) or [])
    # columns=None → don't filter (the rules are already table-scoped, e.g. SQL);
    # a column list → include only rules whose column is present in the file.
    present = set(columns) if columns is not None else None
    out: list[dict] = []
    for rule in src_rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match", "")
        if not isinstance(match, str) or not match.startswith(_COLUMN_PREFIX):
            continue
        column = match[len(_COLUMN_PREFIX) :].strip()
        action = rule.get("action")
        if not column or not action:
            continue
        if present is not None and column not in present:
            continue
        out.append(
            {"column": column, "action": action, "rule": rule.get("name") or match}
        )
    return out


def apply_column_rules(
    rows: list[dict],
    settings=None,
    *,
    rules: list[dict] | None = None,
) -> list[dict]:
    """Apply ``column:<name>`` rules to each tabular row in place.

    Each ``column:foo`` rule is dispatched to the standard action registry as
    the path ``TabularRow.foo``, so all existing actions work unchanged.  Rows
    are flat synthetic resources tagged ``resourceType=TabularRow``.

    Rule source: an explicit *rules* list takes precedence (used by the UI
    column-mapper, which authors rules on the fly); otherwise ``settings.rules``
    is used (a saved config profile).  ``processing_errors`` is read from
    *settings* when present, else defaults to ``skip`` (safe).

    Non-``column:`` rules are ignored for tabular input (they target FHIRPath,
    which has no meaning on a flat row).  In ``skip`` mode an action failure
    redacts the cell as a safety fallback; in ``raise`` mode it propagates.
    """
    from pipeline.deidentify import perform_deidentification
    from actions.redact import redact_by_path

    src_rules = rules if rules is not None else (getattr(settings, "rules", []) or [])
    processing_mode = str(getattr(settings, "processing_errors", "skip")).lower()

    # Pre-resolve the column-targeting rules once.  ``gpas_pseudonymize`` is a
    # batch-only action (one HTTP call per column) so it is split off and run in
    # a dedicated pass below; everything else is dispatched per element.
    column_rules: list[tuple[str, str, dict]] = []
    gpas_rules: list[tuple[str, dict]] = []
    for rule in src_rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match", "")
        if not isinstance(match, str) or not match.startswith(_COLUMN_PREFIX):
            continue
        column = match[len(_COLUMN_PREFIX) :].strip()
        action = rule.get("action")
        if not column or not action:
            continue
        params = dict(rule.get("params") or {})
        if action == "gpas_pseudonymize":
            gpas_rules.append((column, params))
        else:
            column_rules.append((column, action, params))

    if gpas_rules:
        _apply_gpas_columns(rows, gpas_rules, processing_mode)

    for row in rows:
        for column, action, params in column_rules:
            if column not in row:
                continue
            el = {"path": f"{_ROW_TYPE}.{column}", "value": row.get(column)}
            try:
                perform_deidentification(action, row, el, dict(params))
            except Exception:
                if processing_mode != "skip":
                    raise
                # Safety fallback: redact the cell so no raw value survives.
                try:
                    redact_by_path(row, el, {})
                except Exception:
                    row[column] = "[REDACTED]"
    return rows


def _apply_gpas_columns(
    rows: list[dict],
    gpas_rules: list[tuple[str, dict]],
    processing_mode: str,
) -> None:
    """Pseudonymize whole columns through gPAS, one batch call per column.

    gPAS is a trusted third party that persists the value→pseudonym mapping, so
    the same source value yields the same pseudonym everywhere — consistent
    within the file and across the whole dataset (enabling linkage without
    exposing the original).  Distinct non-empty values per column are gathered
    and sent in a single ``gpas_pseudonymize_batch`` call.

    On failure, ``skip`` mode redacts the affected cells (no raw value survives);
    ``raise`` mode propagates the error.  Any value missing from the gPAS
    response is redacted as a safety fallback.
    """
    from integrations.gpas.client import gpas_pseudonymize_batch

    for column, params in gpas_rules:
        values = sorted(
            {
                str(row[column])
                for row in rows
                if column in row and row[column] not in (None, "")
            }
        )
        if not values:
            continue
        try:
            mapping = gpas_pseudonymize_batch(values, params)
        except Exception:
            if processing_mode != "skip":
                raise
            mapping = {}  # skip mode → redact every affected cell below
        for row in rows:
            if column not in row:
                continue
            cur = row[column]
            if cur in (None, ""):
                continue
            row[column] = mapping.get(str(cur), "[REDACTED]")


# ---------------------------------------------------------------------------
# Column action recommendations (powers the UI column explorer)
# ---------------------------------------------------------------------------
#
# Heuristic suggestion of a de-identification action per column, returned by
# ``inspect()``.  Values match the UI's action vocabulary so the front-end can
# pre-select the dropdown directly:
#   none · redact · generalize_year · pseudonymize_gpas · tokenize · nlp_scrub

# Ordered (action, column-name pattern) pairs — first match wins, so the more
# specific identifier/date patterns are tried before the broad PII catch-all.
_RECOMMEND_NAME_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "pseudonymize_gpas",
        re.compile(
            r"(?:^|_|\b)(mrn|ssn|nhs|patient[_-]?id|subject[_-]?id|"
            r"record[_-]?id|case[_-]?id|identifier|account|insurance|policy|"
            r"member[_-]?id)(?:_|\b|$)"
        ),
    ),
    (
        "generalize_year",
        re.compile(
            r"(birth|dob|dod|death|deceased|admit|admission|discharge|"
            r"encounter|visit|onset|\bdate\b|_date|date_)"
        ),
    ),
    (
        "nlp_scrub",
        re.compile(
            r"(note|notes|comment|description|narrative|free[_-]?text|"
            r"\btext\b|reason|diagnosis|summary|history|remark|observation)"
        ),
    ),
    (
        "redact",
        re.compile(
            r"(name|first|last|surname|given|family|email|e[_-]?mail|phone|"
            r"fax|mobile|\btel\b|address|street|city|zip|postal|postcode|contact)"
        ),
    ),
)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_DATE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


def recommend_column_action(name: str, samples: list[str]) -> str:
    """Suggest a de-identification action for a column.

    Matches the column *name* against known identifier / date / free-text / PII
    keyword patterns first, then falls back to inspecting *samples* for emails,
    phone numbers, or date-shaped values.  Returns ``"none"`` (keep) when nothing
    looks sensitive — the UI pre-selects this and the user can always override.
    """
    n = str(name).lower()
    for action, pattern in _RECOMMEND_NAME_RULES:
        if pattern.search(n):
            return action

    for sample in samples:
        s = str(sample)
        if _EMAIL_RE.search(s) or _PHONE_RE.search(s):
            return "redact"
        if _DATE_RE.match(s):
            return "generalize_year"
    return "none"
